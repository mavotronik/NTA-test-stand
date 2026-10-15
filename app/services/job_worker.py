from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable

from app.config import Settings
from app.crypto_util import decrypt_secret, get_fernet
from app.db import Database, new_id, utc_now
from app.services import git_service
from app.services.docker_service import DockerService


LogFn = Callable[[str, str], None]


def append_job_log(db: Database, job_id: str, line: str) -> None:
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO job_logs(job_id, line, created_at) VALUES (?,?,?)",
            (job_id, line[:4000], utc_now()),
        )


class JobWorker:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self.docker = DockerService()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._building = 0
        self._deploying = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="job-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as err:
                pass
            self._stop.wait(1.0)

    def _tick(self) -> None:
        with self.db.connection() as conn:
            job = conn.execute(
                """
                SELECT j.*, i.slug, i.port, i.db_profile_id, i.git_branch
                FROM deployment_jobs j
                JOIN instances i ON i.id = j.instance_id
                WHERE j.status = 'queued'
                ORDER BY j.created_at
                LIMIT 1
                """
            ).fetchone()
        if not job:
            return
        job_type = job["job_type"]
        if job_type == "build":
            with self._lock:
                if self._building >= self.settings.policy.build_max_parallel:
                    return
                self._building += 1
            try:
                self._run_build(dict(job))
            finally:
                with self._lock:
                    self._building -= 1
        elif job_type == "deploy":
            with self._lock:
                if self._deploying >= self.settings.policy.deploy_max_parallel:
                    return
                self._deploying += 1
            try:
                self._run_deploy(dict(job))
            finally:
                with self._lock:
                    self._deploying -= 1
        elif job_type == "build_run":
            with self._lock:
                if (
                    self._building >= self.settings.policy.build_max_parallel
                    or self._deploying >= self.settings.policy.deploy_max_parallel
                ):
                    return
                self._building += 1
                self._deploying += 1
            try:
                self._run_build_run(dict(job))
            finally:
                with self._lock:
                    self._building -= 1
                    self._deploying -= 1

    def _run_build_run(self, job: dict) -> None:
        self._run_build(job, defer_deploy=True)
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT status FROM deployment_jobs WHERE id=?", (job["id"],)
            ).fetchone()
        if not row or row["status"] != "running":
            return
        self._run_deploy(job)

    def _prepare_ui_build_context(self, repo: Path, commit: str) -> tuple[Path, Path, Path, str, Path]:
        pol = self.settings.policy
        wt = git_service.ensure_worktree(
            repo,
            self.settings.git_cache_dir,
            commit,
            ui_context=pol.ui_context,
            ui_dockerfile=pol.ui_dockerfile,
        )
        ctx = (wt / pol.ui_context).resolve()
        df = (wt / pol.ui_dockerfile).resolve()
        ui_compose_dir = (wt / "deploy" / "ui").resolve()
        if not ctx.is_dir():
            raise RuntimeError(f"build context missing: {ctx} (worktree {wt})")
        if not df.is_file():
            raise RuntimeError(f"Dockerfile missing: {df}")
        bkey = git_service.build_key_for_commit(commit, df, ctx)
        return wt, ctx, df, bkey, ui_compose_dir

    def _run_build(self, job: dict, *, defer_deploy: bool = False) -> None:
        job_id = job["id"]
        active_build_id: str | None = None
        self._set_job(job_id, "running", "resolve_commit")
        try:
            repo = Path(self.settings.policy.git_remote_url)
            with self.db.connection() as conn:
                inst_row = conn.execute(
                    "SELECT * FROM instances WHERE id=?", (job["instance_id"],)
                ).fetchone()
            if not inst_row:
                raise RuntimeError("instance missing")
            inst = dict(inst_row)
            commit = git_service.resolve_instance_commit(repo, inst, self.settings.policy)
            append_job_log(self.db, job_id, f"commit {commit}")
            _wt, ctx, _df, bkey, ui_compose_dir = self._prepare_ui_build_context(repo, commit)
            append_job_log(self.db, job_id, f"context {ctx}")
            with self.db.connection() as conn:
                existing = conn.execute(
                    "SELECT * FROM builds WHERE build_key=? AND status='ready' ORDER BY created_at DESC LIMIT 1",
                    (bkey,),
                ).fetchone()
            if existing:
                append_job_log(self.db, job_id, f"reuse image {existing['image_tag']}")
                if defer_deploy:
                    self._set_job(job_id, "running", "deploy", build_id=existing["id"])
                else:
                    self._set_job(job_id, "done", "ready", build_id=existing["id"])
                return
            build_id = job.get("build_id") or new_id()
            active_build_id = build_id
            tag = f"grapes-nta-test:{commit[:12]}"
            self._set_job(job_id, "running", "docker_build", build_id=build_id)
            with self.db.connection() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO builds(id, build_key, git_commit, status, image_tag, created_at)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (build_id, bkey, commit, "building", tag, utc_now()),
                )

            def log_cb(line: str) -> None:
                if line:
                    append_job_log(self.db, job_id, line)

            build_date = utc_now()
            if self.docker.can_compose_ui_build(ui_compose_dir):
                append_job_log(self.db, job_id, f"docker compose build in {ui_compose_dir}")
                _, image_id = self.docker.build_ui_image_compose(
                    ui_compose_dir,
                    tag,
                    build_date=build_date,
                    log_callback=log_cb,
                )
            else:
                append_job_log(self.db, job_id, "docker compose unavailable; tar context build")
                _, image_id = self.docker.build_image(
                    context=ctx,
                    dockerfile="Dockerfile",
                    tag=tag,
                    buildargs={"BUILD_DATE": build_date},
                    log_callback=log_cb,
                )
            with self.db.connection() as conn:
                conn.execute(
                    """
                    UPDATE builds SET status='ready', image_id=?, finished_at=? WHERE id=?
                    """,
                    (image_id, utc_now(), build_id),
                )
            if defer_deploy:
                self._set_job(job_id, "running", "deploy", build_id=build_id)
            else:
                self._set_job(job_id, "done", "ready", build_id=build_id)
        except Exception as err:
            append_job_log(self.db, job_id, f"ERROR: {err}")
            if active_build_id:
                from app.repositories import mark_build_failed

                mark_build_failed(self.db, active_build_id, str(err))
            self._set_job(job_id, "failed", "error", error=str(err))

    def _run_deploy(self, job: dict) -> None:
        job_id = job["id"]
        instance_id = job["instance_id"]
        self._set_job(job_id, "running", "prepare")
        try:
            with self.db.connection() as conn:
                inst_row = conn.execute("SELECT * FROM instances WHERE id=?", (instance_id,)).fetchone()
                if not inst_row:
                    raise RuntimeError("instance missing")
                inst = dict(inst_row)
                prof_row = conn.execute(
                    "SELECT * FROM db_profiles WHERE id=?", (inst["db_profile_id"],)
                ).fetchone()
            prof = dict(prof_row) if prof_row else None
            if not prof:
                raise RuntimeError("profile missing")
            if prof["disabled"]:
                raise RuntimeError("db profile disabled")
            repo = Path(self.settings.policy.git_remote_url)
            commit = git_service.resolve_instance_commit(repo, inst, self.settings.policy)
            pinned_image = (inst.get("image_tag") or "").strip()
            build = None
            if pinned_image:
                append_job_log(self.db, job_id, f"using pinned image {pinned_image}")
                image_for_run = pinned_image
            else:
                _wt, _ctx, _df, bkey, _ui_dir = self._prepare_ui_build_context(repo, commit)
                with self.db.connection() as conn:
                    build = conn.execute(
                        "SELECT * FROM builds WHERE build_key=? AND status='ready' ORDER BY created_at DESC LIMIT 1",
                        (bkey,),
                    ).fetchone()
                if not build:
                    raise RuntimeError("no ready build; queue build first or set image on instance")
                image_for_run = build["image_tag"]
            fernet = get_fernet(self.settings.fernet_key, self.settings.secret_key)
            password = decrypt_secret(fernet, prof["password_enc"])
            version_date = utc_now()[:10]
            env = {
                "CLICKHOUSE_URL": f"{'https' if prof['tls'] else 'http'}://{prof['host']}:{prof['port']}",
                "CLICKHOUSE_USER": prof["username"],
                "CLICKHOUSE_PASSWORD": password,
                "CLICKHOUSE_DATABASE": prof["database_name"],
                "SESSION_COOKIE_SECURE": "false",
                "GRAPES_VERSION": commit[:12],
                "GRAPES_VERSION_DATE": version_date,
                "GIT_COMMIT": commit,
            }
            name = f"grapes-test-{inst['slug']}"
            data_dir = self.settings.instance_ui_data_dir(inst["slug"])
            append_job_log(self.db, job_id, f"ui data mount (host) {data_dir}")
            prev_tag = inst["image_tag"]
            prev_cfg = inst.get("rollback_config_json") or "{}"
            with self.db.connection() as conn:
                conn.execute(
                    """
                    UPDATE instances SET rollback_image_tag=?, state='deploying', updated_at=?
                    WHERE id=?
                    """,
                    (inst.get("image_tag") or "", utc_now(), instance_id),
                )
            append_job_log(self.db, job_id, f"starting {name} on port {inst['port']}")
            cid = self.docker.run_instance_container(
                name=name,
                image=image_for_run,
                port=int(inst["port"]),
                env=env,
                data_mount_host=data_dir,
            )
            self._set_job(job_id, "running", "healthcheck")
            ok = self.docker.wait_health_http(
                int(inst["port"]),
                self.settings.policy.health_timeout_sec,
                self.settings.policy.health_interval_sec,
            )
            if not ok:
                raise RuntimeError("health check failed")
            with self.db.connection() as conn:
                conn.execute(
                    """
                    UPDATE instances SET state='running', git_commit=?, image_tag=?, container_id=?,
                      rollback_config_json=?, updated_at=?
                    WHERE id=?
                    """,
                    (commit, image_for_run, cid, json.dumps({"port": inst["port"]}), utc_now(), instance_id),
                )
            self._set_job(job_id, "done", "running")
        except Exception as err:
            append_job_log(self.db, job_id, f"ERROR: {err}")
            with self.db.connection() as conn:
                conn.execute(
                    "UPDATE instances SET state='failed', updated_at=? WHERE id=?",
                    (utc_now(), instance_id),
                )
            self._set_job(job_id, "failed", "error", error=str(err))

    def _set_job(
        self,
        job_id: str,
        status: str,
        stage: str,
        *,
        build_id: str | None = None,
        error: str = "",
    ) -> None:
        with self.db.connection() as conn:
            fields = ["status=?", "stage=?", "error=?"]
            vals: list = [status, stage, error]
            if status == "running":
                fields.append("started_at=COALESCE(started_at, ?)")
                vals.append(utc_now())
            if status in ("done", "failed"):
                fields.append("finished_at=?")
                vals.append(utc_now())
            if build_id:
                fields.append("build_id=?")
                vals.append(build_id)
            vals.append(job_id)
            conn.execute(
                f"UPDATE deployment_jobs SET {', '.join(fields)} WHERE id=?",
                vals,
            )
