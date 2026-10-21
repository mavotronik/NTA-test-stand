from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.config import Settings
from app.db import Database, new_id, utc_now
from app.repositories import delete_instance, fail_orphan_running_jobs, mark_build_failed
from app.services import git_service
from app.services.docker_service import DockerService
from app.services.component_versions import build_instance_version_extra_env
from app.services.instance_runtime import instance_container_env, launch_instance_container
from app.services.job_queue import LIFECYCLE_JOB_TYPES, pick_next_queued_job
from app.services.ui_source import (
    build_key_for_path_layout,
    is_path_source,
    path_build_tag,
    path_version_id,
    resolve_path_layout,
)

LogFn = Callable[[str, str], None]

_LOG_BATCH_MAX_LINES = 80
_LOG_FLUSH_INTERVAL_SEC = 0.35


@dataclass(frozen=True)
class _InstanceBuildContext:
    build_key: str
    context: Path
    dockerfile: Path
    compose_dir: Path
    env_example: Path | None
    version_id: str
    build_git_commit: str
    log_label: str
    image_tag: str


def append_job_log(db: Database, job_id: str, line: str) -> None:
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO job_logs(job_id, line, created_at) VALUES (?,?,?)",
            (job_id, line[:4000], utc_now()),
        )


def flush_job_logs(db: Database, job_id: str, lines: list[str]) -> None:
    if not lines:
        return
    now = utc_now()
    with db.connection() as conn:
        conn.executemany(
            "INSERT INTO job_logs(job_id, line, created_at) VALUES (?,?,?)",
            [(job_id, line[:4000], now) for line in lines],
        )


class JobLogBatcher:
    def __init__(self, db: Database, job_id: str) -> None:
        self._db = db
        self._job_id = job_id
        self._buf: list[str] = []
        self._last_flush = time.monotonic()

    def append(self, line: str) -> None:
        if not line:
            return
        self._buf.append(line)
        if (
            len(self._buf) >= _LOG_BATCH_MAX_LINES
            or time.monotonic() - self._last_flush >= _LOG_FLUSH_INTERVAL_SEC
        ):
            self.flush()

    def flush(self) -> None:
        if not self._buf:
            return
        batch = self._buf
        self._buf = []
        self._last_flush = time.monotonic()
        flush_job_logs(self._db, self._job_id, batch)


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
        fail_orphan_running_jobs(self.db)
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="job-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                pass
            self._stop.wait(1.0)

    def _fetch_queued_jobs(self) -> list[dict]:
        with self.db.connection() as conn:
            rows = conn.execute(
                """
                SELECT j.*, i.slug, i.port, i.db_profile_id, i.git_branch
                FROM deployment_jobs j
                LEFT JOIN instances i ON i.id = j.instance_id
                WHERE j.status = 'queued'
                ORDER BY j.created_at
                LIMIT 50
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def _tick(self) -> None:
        queued = self._fetch_queued_jobs()
        if not queued:
            return
        with self._lock:
            building = self._building
            deploying = self._deploying
            build_max = self.settings.policy.build_max_parallel
            deploy_max = self.settings.policy.deploy_max_parallel
        job = pick_next_queued_job(
            queued,
            building=building,
            deploying=deploying,
            build_max=build_max,
            deploy_max=deploy_max,
        )
        if not job:
            return
        job_type = job["job_type"]
        if job_type in LIFECYCLE_JOB_TYPES:
            self._run_lifecycle(job)
            return
        if job_type == "build":
            with self._lock:
                if self._building >= build_max:
                    return
                self._building += 1
            try:
                self._run_build(dict(job))
            finally:
                with self._lock:
                    self._building -= 1
        elif job_type == "image_build":
            with self._lock:
                if self._building >= build_max:
                    return
                self._building += 1
            try:
                self._run_image_build(dict(job))
            finally:
                with self._lock:
                    self._building -= 1
        elif job_type == "deploy":
            with self._lock:
                if self._deploying >= deploy_max:
                    return
                self._deploying += 1
            try:
                self._run_deploy(dict(job))
            finally:
                with self._lock:
                    self._deploying -= 1
        elif job_type == "build_run":
            with self._lock:
                if self._building >= build_max or self._deploying >= deploy_max:
                    return
                self._building += 1
                self._deploying += 1
            try:
                self._run_build_run(dict(job))
            finally:
                with self._lock:
                    self._building -= 1
                    self._deploying -= 1

    def _run_lifecycle(self, job: dict) -> None:
        jt = job["job_type"]
        if jt == "instance_stop":
            self._run_instance_stop(job)
        elif jt == "instance_start":
            self._run_instance_start(job)
        elif jt == "instance_delete":
            self._run_instance_delete(job)
        elif jt == "instance_rollback":
            self._run_instance_rollback(job)

    def _run_instance_stop(self, job: dict) -> None:
        job_id = job["id"]
        instance_id = job["instance_id"]
        logs = JobLogBatcher(self.db, job_id)
        self._set_job(job_id, "running", "stop")
        try:
            slug = job["slug"]
            name = f"grapes-test-{slug}"
            logs.append(f"stopping {name}")
            self.docker.stop_remove(name)
            with self.db.connection() as conn:
                conn.execute(
                    "UPDATE instances SET state='stopped', container_id='', updated_at=? WHERE id=?",
                    (utc_now(), instance_id),
                )
            logs.flush()
            self._set_job(job_id, "done", "stopped")
        except Exception as err:
            logs.append(f"ERROR: {err}")
            logs.flush()
            self._set_job(job_id, "failed", "error", error=str(err))

    def _run_instance_start(self, job: dict) -> None:
        job_id = job["id"]
        instance_id = job["instance_id"]
        logs = JobLogBatcher(self.db, job_id)
        self._set_job(job_id, "running", "prepare")
        try:
            with self.db.connection() as conn:
                inst_row = conn.execute(
                    "SELECT * FROM instances WHERE id=?", (instance_id,)
                ).fetchone()
            if not inst_row:
                raise RuntimeError("instance missing")
            inst = dict(inst_row)
            image = (inst.get("image_tag") or "").strip()
            if not image:
                raise RuntimeError("no image on instance")
            with self.db.connection() as conn:
                conn.execute(
                    "UPDATE instances SET state='deploying', updated_at=? WHERE id=?",
                    (utc_now(), instance_id),
                )
            logs.append(f"starting grapes-test-{inst['slug']} with {image}")
            cid, ok = launch_instance_container(
                self.db, self.settings, self.docker, inst, image
            )
            with self.db.connection() as conn:
                conn.execute(
                    """
                    UPDATE instances SET state=?, container_id=?, updated_at=? WHERE id=?
                    """,
                    ("running" if ok else "failed", cid, utc_now(), instance_id),
                )
            logs.flush()
            if not ok:
                raise RuntimeError("health check failed")
            self._set_job(job_id, "done", "running")
        except Exception as err:
            logs.append(f"ERROR: {err}")
            logs.flush()
            with self.db.connection() as conn:
                conn.execute(
                    "UPDATE instances SET state='failed', updated_at=? WHERE id=?",
                    (utc_now(), instance_id),
                )
            self._set_job(job_id, "failed", "error", error=str(err))

    def _run_instance_rollback(self, job: dict) -> None:
        job_id = job["id"]
        instance_id = job["instance_id"]
        logs = JobLogBatcher(self.db, job_id)
        self._set_job(job_id, "running", "prepare")
        try:
            with self.db.connection() as conn:
                inst_row = conn.execute(
                    "SELECT * FROM instances WHERE id=?", (instance_id,)
                ).fetchone()
            if not inst_row:
                raise RuntimeError("instance missing")
            inst = dict(inst_row)
            image = (inst.get("rollback_image_tag") or "").strip()
            if not image:
                raise RuntimeError("nothing to rollback")
            with self.db.connection() as conn:
                conn.execute(
                    "UPDATE instances SET state='deploying', updated_at=? WHERE id=?",
                    (utc_now(), instance_id),
                )
            logs.append(f"rollback grapes-test-{inst['slug']} to {image}")
            cid, ok = launch_instance_container(
                self.db, self.settings, self.docker, inst, image
            )
            with self.db.connection() as conn:
                conn.execute(
                    """
                    UPDATE instances SET state=?, container_id=?, image_tag=?, updated_at=? WHERE id=?
                    """,
                    (
                        "running" if ok else "failed",
                        cid,
                        image if ok else inst.get("image_tag"),
                        utc_now(),
                        instance_id,
                    ),
                )
            logs.flush()
            if not ok:
                raise RuntimeError("health check failed")
            self._set_job(job_id, "done", "running")
        except Exception as err:
            logs.append(f"ERROR: {err}")
            logs.flush()
            with self.db.connection() as conn:
                conn.execute(
                    "UPDATE instances SET state='failed', updated_at=? WHERE id=?",
                    (utc_now(), instance_id),
                )
            self._set_job(job_id, "failed", "error", error=str(err))

    def _run_instance_delete(self, job: dict) -> None:
        job_id = job["id"]
        instance_id = job["instance_id"]
        logs = JobLogBatcher(self.db, job_id)
        self._set_job(job_id, "running", "stop")
        try:
            slug = job["slug"]
            name = f"grapes-test-{slug}"
            logs.append(f"removing container {name}")
            self.docker.stop_remove(name)
            logs.flush()
            delete_instance(self.db, instance_id, completing_job_id=job_id)
        except Exception as err:
            logs.append(f"ERROR: {err}")
            logs.flush()
            self._set_job(job_id, "failed", "error", error=str(err))

    def _run_build_run(self, job: dict) -> None:
        self._run_build(job, defer_deploy=True)
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT status FROM deployment_jobs WHERE id=?", (job["id"],)
            ).fetchone()
        if not row or row["status"] != "running":
            return
        self._run_deploy(job)

    @staticmethod
    def _build_source_dict(source: dict) -> dict:
        return {
            "source_mode": source.get("source_mode") or "repo",
            "source_path": source.get("source_path") or "",
            "git_branch": source.get("git_branch") or "main",
            "git_commit": source.get("git_commit") or "",
            "git_tag": source.get("git_tag") or "",
        }

    def _resolve_instance_build_context(self, inst: dict) -> _InstanceBuildContext:
        if is_path_source(inst):
            layout = resolve_path_layout(inst)
            bkey = build_key_for_path_layout(layout)
            vid = path_version_id(bkey)
            tag = path_build_tag(bkey)
            return _InstanceBuildContext(
                build_key=bkey,
                context=layout.context,
                dockerfile=layout.dockerfile,
                compose_dir=layout.compose_dir,
                env_example=layout.env_example,
                version_id=vid,
                build_git_commit=f"path:{vid}",
                log_label=f"path source {inst.get('source_path')}",
                image_tag=tag,
            )
        pol = self.settings.policy
        repo = Path(self.settings.policy.git_remote_url)
        commit = git_service.resolve_instance_commit(repo, inst, self.settings.policy)
        tag_name = (inst.get("git_tag") or "").strip()
        if tag_name:
            log_label = f"tag {tag_name} ({commit[:12]})"
        else:
            log_label = f"commit {commit}"
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
        env_ex = wt / "deploy" / "ui" / "env.example"
        return _InstanceBuildContext(
            build_key=bkey,
            context=ctx,
            dockerfile=df,
            compose_dir=ui_compose_dir,
            env_example=env_ex if env_ex.is_file() else None,
            version_id=commit[:12],
            build_git_commit=commit,
            log_label=log_label,
            image_tag=f"grapes-nta-test:{commit[:12]}",
        )

    def _run_build(self, job: dict, *, defer_deploy: bool = False) -> None:
        job_id = job["id"]
        logs = JobLogBatcher(self.db, job_id)
        self._set_job(job_id, "running", "resolve_commit")
        try:
            with self.db.connection() as conn:
                inst_row = conn.execute(
                    "SELECT * FROM instances WHERE id=?", (job["instance_id"],)
                ).fetchone()
            if not inst_row:
                raise RuntimeError("instance missing")
            inst = dict(inst_row)
            self._run_build_core(
                job_id,
                logs,
                source=inst,
                preset_build_id=job.get("build_id"),
                defer_deploy=defer_deploy,
            )
        except Exception as err:
            logs.append(f"ERROR: {err}")
            logs.flush()
            self._set_job(job_id, "failed", "error", error=str(err))

    def _run_image_build(self, job: dict) -> None:
        job_id = job["id"]
        logs = JobLogBatcher(self.db, job_id)
        self._set_job(job_id, "running", "resolve_commit")
        try:
            build_id = job.get("build_id")
            if not build_id:
                raise RuntimeError("build missing on image_build job")
            with self.db.connection() as conn:
                build_row = conn.execute(
                    "SELECT * FROM builds WHERE id=?", (build_id,)
                ).fetchone()
            if not build_row:
                raise RuntimeError("build record missing")
            source = self._build_source_dict(dict(build_row))
            self._run_build_core(
                job_id,
                logs,
                source=source,
                preset_build_id=build_id,
                defer_deploy=False,
            )
        except Exception as err:
            logs.append(f"ERROR: {err}")
            logs.flush()
            if job.get("build_id"):
                mark_build_failed(self.db, job["build_id"], str(err))
            self._set_job(job_id, "failed", "error", error=str(err))

    def _run_build_core(
        self,
        job_id: str,
        logs: JobLogBatcher,
        *,
        source: dict,
        preset_build_id: str | None,
        defer_deploy: bool,
    ) -> None:
        active_build_id: str | None = None
        src = self._build_source_dict(source)
        bc = self._resolve_instance_build_context(src)
        logs.append(bc.log_label)
        ctx = bc.context
        bkey = bc.build_key
        ui_compose_dir = bc.compose_dir
        logs.append(f"context {ctx}")
        with self.db.connection() as conn:
            existing = conn.execute(
                "SELECT * FROM builds WHERE build_key=? AND status='ready' ORDER BY created_at DESC LIMIT 1",
                (bkey,),
            ).fetchone()
        if existing:
            logs.append(f"reuse image {existing['image_tag']}")
            logs.flush()
            if preset_build_id and preset_build_id != existing["id"]:
                with self.db.connection() as conn:
                    conn.execute(
                        """
                        UPDATE builds SET status='removed', error=?, finished_at=COALESCE(finished_at, ?)
                        WHERE id=?
                        """,
                        (
                            f"reused existing image {existing['image_tag']}",
                            utc_now(),
                            preset_build_id,
                        ),
                    )
            if defer_deploy:
                self._set_job(job_id, "running", "deploy", build_id=existing["id"])
            else:
                self._set_job(job_id, "done", "ready", build_id=existing["id"])
            return
        build_id = preset_build_id or new_id()
        active_build_id = build_id
        tag = bc.image_tag
        self._set_job(job_id, "running", "docker_build", build_id=build_id)
        now = utc_now()
        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO builds(
                  id, build_key, git_commit, status, image_tag, created_at,
                  source_mode, source_path, git_branch, git_tag
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    build_id,
                    bkey,
                    bc.build_git_commit,
                    "building",
                    tag,
                    now,
                    src["source_mode"],
                    src["source_path"],
                    src["git_branch"],
                    src["git_tag"],
                ),
            )

        def log_cb(line: str) -> None:
            logs.append(line)
            logs.flush()

        build_date = utc_now()
        try:
            if self.docker.can_compose_ui_build(ui_compose_dir):
                logs.append(f"ui image build in {ui_compose_dir}")
                logs.flush()
                _, image_id = self.docker.build_ui_image_compose(
                    ui_compose_dir,
                    tag,
                    build_date=build_date,
                    log_callback=log_cb,
                )
            else:
                logs.append("docker compose unavailable; tar context build")
                logs.flush()
                _, image_id = self.docker.build_image(
                    context=ctx,
                    dockerfile="Dockerfile",
                    tag=tag,
                    buildargs={"BUILD_DATE": build_date},
                    log_callback=log_cb,
                )
            logs.flush()
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
            logs.append(f"ERROR: {err}")
            logs.flush()
            if active_build_id:
                mark_build_failed(self.db, active_build_id, str(err))
            self._set_job(job_id, "failed", "error", error=str(err))
            raise

    def _run_deploy(self, job: dict) -> None:
        job_id = job["id"]
        instance_id = job["instance_id"]
        logs = JobLogBatcher(self.db, job_id)
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
            bc = self._resolve_instance_build_context(inst)
            bkey = bc.build_key
            pinned_image = (inst.get("image_tag") or "").strip()
            if pinned_image:
                logs.append(f"using pinned image {pinned_image}")
                image_for_run = pinned_image
            else:
                with self.db.connection() as conn:
                    build = conn.execute(
                        "SELECT * FROM builds WHERE build_key=? AND status='ready' ORDER BY created_at DESC LIMIT 1",
                        (bkey,),
                    ).fetchone()
                if not build:
                    raise RuntimeError("no ready build; queue build first or set image on instance")
                image_for_run = build["image_tag"]
            example = bc.env_example
            preview_env = instance_container_env(
                self.settings,
                inst,
                prof,
                example_path=example,
                extra_env=build_instance_version_extra_env(self.settings, inst),
            )
            col_keys = [
                key for key in preview_env if key.startswith("CH_COL_") or key == "CH_MAC_STORAGE"
            ]
            if example and example.is_file():
                logs.append(f"ui mappings from {example.name}: {len(col_keys)} column keys")
            else:
                logs.append("WARNING: env.example missing; UI column defaults")
            name = f"grapes-test-{inst['slug']}"
            data_dir = self.settings.instance_ui_data_dir(inst["slug"])
            logs.append(f"ui data mount (host) {data_dir}")
            with self.db.connection() as conn:
                conn.execute(
                    """
                    UPDATE instances SET rollback_image_tag=?, state='deploying', updated_at=?
                    WHERE id=?
                    """,
                    (inst.get("image_tag") or "", utc_now(), instance_id),
                )
            logs.append(f"starting {name} on port {inst['port']}")
            self._set_job(job_id, "running", "healthcheck")
            cid, ok = launch_instance_container(
                self.db,
                self.settings,
                self.docker,
                inst,
                image_for_run,
                example_path=example,
            )
            if not ok:
                raise RuntimeError("health check failed")
            with self.db.connection() as conn:
                conn.execute(
                    """
                    UPDATE instances SET state='running', container_id=?,
                      rollback_config_json=?, updated_at=?
                    WHERE id=?
                    """,
                    (cid, json.dumps({"port": inst["port"]}), utc_now(), instance_id),
                )
            logs.flush()
            self._set_job(job_id, "done", "running")
        except Exception as err:
            logs.append(f"ERROR: {err}")
            logs.flush()
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
