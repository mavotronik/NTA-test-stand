from __future__ import annotations

import re
from typing import Any

from app.db import Database, new_id, utc_now
from app.services.job_queue import BUILD_DEPLOY_JOB_TYPES, LIFECYCLE_JOB_TYPES


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


def validate_slug(slug: str) -> bool:
    return bool(SLUG_RE.match(slug))


def list_instances(db: Database) -> list[dict[str, Any]]:
    with db.connection() as conn:
        rows = conn.execute(
            """
            SELECT i.*, p.name AS db_profile_name
            FROM instances i
            JOIN db_profiles p ON p.id = i.db_profile_id
            ORDER BY i.name
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_instance(db: Database, instance_id: str) -> dict[str, Any] | None:
    with db.connection() as conn:
        row = conn.execute(
            """
            SELECT i.*, p.name AS db_profile_name
            FROM instances i
            JOIN db_profiles p ON p.id = i.db_profile_id
            WHERE i.id=?
            """,
            (instance_id,),
        ).fetchone()
    return dict(row) if row else None


def port_owner(db: Database, port: int, exclude_id: str | None = None) -> dict[str, Any] | None:
    with db.connection() as conn:
        if exclude_id:
            row = conn.execute(
                "SELECT id, name, slug, port FROM instances WHERE port=? AND id<>?",
                (port, exclude_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, name, slug, port FROM instances WHERE port=?",
                (port,),
            ).fetchone()
    return dict(row) if row else None


def port_taken(db: Database, port: int, exclude_id: str | None = None) -> bool:
    return port_owner(db, port, exclude_id=exclude_id) is not None


def list_occupied_ports(db: Database, exclude_id: str | None = None) -> list[dict[str, Any]]:
    with db.connection() as conn:
        if exclude_id:
            rows = conn.execute(
                "SELECT port, name, slug FROM instances WHERE id<>? ORDER BY port",
                (exclude_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT port, name, slug FROM instances ORDER BY port"
            ).fetchall()
    return [dict(row) for row in rows]


def next_free_port(
    db: Database,
    port_min: int,
    port_max: int,
    exclude_id: str | None = None,
) -> int | None:
    taken = {int(row["port"]) for row in list_occupied_ports(db, exclude_id)}
    for port in range(port_min, port_max + 1):
        if port not in taken:
            return port
    return None


def create_instance(
    db: Database,
    *,
    slug: str,
    name: str,
    port: int,
    db_profile_id: str,
    git_branch: str,
    user_id: str,
    git_commit: str = "",
    image_tag: str = "",
    source_mode: str = "repo",
    source_path: str = "",
) -> str:
    iid = new_id()
    now = utc_now()
    with db.connection() as conn:
        conn.execute(
            """
            INSERT INTO instances(
              id, slug, name, port, state, db_profile_id, git_branch,
              git_commit, source_mode, source_path, image_tag,
              created_by, updated_by, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                iid,
                slug,
                name,
                port,
                "stopped",
                db_profile_id,
                git_branch,
                git_commit.strip(),
                source_mode,
                source_path.strip(),
                image_tag.strip(),
                user_id,
                user_id,
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO instance_configs(id, instance_id, version, config_json, created_at, created_by)
            VALUES (?,?,?,?,?,?)
            """,
            (new_id(), iid, 1, "{}", now, user_id),
        )
    return iid


def update_instance(
    db: Database,
    instance_id: str,
    *,
    name: str,
    port: int,
    db_profile_id: str,
    git_branch: str,
    git_commit: str,
    image_tag: str,
    user_id: str,
    source_mode: str = "repo",
    source_path: str = "",
) -> None:
    now = utc_now()
    with db.connection() as conn:
        conn.execute(
            """
            UPDATE instances SET
              name=?, port=?, db_profile_id=?, git_branch=?, git_commit=?,
              source_mode=?, source_path=?, image_tag=?,
              updated_by=?, updated_at=?, config_version=config_version + 1
            WHERE id=?
            """,
            (
                name,
                port,
                db_profile_id,
                git_branch,
                git_commit.strip(),
                source_mode,
                source_path.strip(),
                image_tag.strip(),
                user_id,
                now,
                instance_id,
            ),
        )


def list_ready_build_images(db: Database, limit: int = 30) -> list[dict[str, Any]]:
    with db.connection() as conn:
        rows = conn.execute(
            """
            SELECT id, git_commit, image_tag, status, created_at
            FROM builds
            WHERE status='ready' AND image_tag != ''
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_db_profiles(db: Database) -> list[dict[str, Any]]:
    with db.connection() as conn:
        rows = conn.execute("SELECT * FROM db_profiles ORDER BY name").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d.pop("password_enc", None)
        out.append(d)
    return out


def get_db_profile(db: Database, profile_id: str, *, include_secret: bool = False) -> dict[str, Any] | None:
    with db.connection() as conn:
        row = conn.execute("SELECT * FROM db_profiles WHERE id=?", (profile_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    if not include_secret:
        d.pop("password_enc", None)
    return d


def save_db_profile(
    db: Database,
    *,
    profile_id: str | None,
    name: str,
    host: str,
    port: int,
    database_name: str,
    username: str,
    password_enc: str,
    tls: bool,
    writable: bool,
    comment: str,
) -> str:
    now = utc_now()
    pid = profile_id or new_id()
    with db.connection() as conn:
        if profile_id:
            conn.execute(
                """
                UPDATE db_profiles SET name=?, host=?, port=?, database_name=?, username=?,
                  password_enc=?, tls=?, writable=?, comment=?, updated_at=?
                WHERE id=?
                """,
                (
                    name, host, port, database_name, username, password_enc,
                    1 if tls else 0, 1 if writable else 0, comment, now, pid,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO db_profiles(
                  id, name, host, port, database_name, username, password_enc,
                  tls, writable, comment, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    pid, name, host, port, database_name, username, password_enc,
                    1 if tls else 0, 1 if writable else 0, comment, now, now,
                ),
            )
    return pid


def update_profile_check(db: Database, profile_id: str, ok: bool, msg: str) -> None:
    with db.connection() as conn:
        conn.execute(
            """
            UPDATE db_profiles SET last_check_ok=?, last_check_at=?, last_check_error=?, updated_at=?
            WHERE id=?
            """,
            (1 if ok else 0, utc_now(), msg if not ok else "", utc_now(), profile_id),
        )


INSTANCE_ACTIVE_JOB_TYPES = BUILD_DEPLOY_JOB_TYPES + LIFECYCLE_JOB_TYPES


def get_instance_job(db: Database, instance_id: str, job_id: str) -> dict[str, Any] | None:
    with db.connection() as conn:
        row = conn.execute(
            "SELECT * FROM deployment_jobs WHERE id=? AND instance_id=?",
            (job_id, instance_id),
        ).fetchone()
    return dict(row) if row else None


def get_instance_active_job(db: Database, instance_id: str) -> dict[str, Any] | None:
    placeholders = ",".join("?" * len(INSTANCE_ACTIVE_JOB_TYPES))
    with db.connection() as conn:
        row = conn.execute(
            f"""
            SELECT * FROM deployment_jobs
            WHERE instance_id=? AND status IN ('queued','running')
              AND job_type IN ({placeholders})
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (instance_id, *INSTANCE_ACTIVE_JOB_TYPES),
        ).fetchone()
    return dict(row) if row else None


def queue_job(db: Database, instance_id: str, job_type: str, user_id: str) -> str:
    jid = new_id()
    with db.connection() as conn:
        active = conn.execute(
            f"""
            SELECT 1 FROM deployment_jobs
            WHERE instance_id=? AND status IN ('queued','running') AND job_type IN ({",".join("?" * len(INSTANCE_ACTIVE_JOB_TYPES))})
            LIMIT 1
            """,
            (instance_id, *INSTANCE_ACTIVE_JOB_TYPES),
        ).fetchone()
        if active:
            raise ValueError("conflicting job already active")
        conn.execute(
            """
            INSERT INTO deployment_jobs(id, instance_id, job_type, status, requested_by, created_at)
            VALUES (?,?,?,?,?,?)
            """,
            (jid, instance_id, job_type, "queued", user_id, utc_now()),
        )
    return jid


def list_jobs(db: Database, limit: int = 50) -> list[dict[str, Any]]:
    with db.connection() as conn:
        rows = conn.execute(
            """
            SELECT j.*, i.slug, i.name AS instance_name
            FROM deployment_jobs j
            JOIN instances i ON i.id = j.instance_id
            ORDER BY j.created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def reconcile_stale_builds(db: Database) -> None:
    """Синхронизировать builds.status='building' с финальным статусом job."""
    now = utc_now()
    with db.connection() as conn:
        rows = conn.execute(
            """
            SELECT b.id, b.status AS build_status, j.status AS job_status, j.error AS job_error
            FROM builds b
            JOIN deployment_jobs j ON j.build_id = b.id
            WHERE b.status = 'building'
              AND j.status IN ('failed', 'done')
              AND j.id = (
                SELECT j2.id FROM deployment_jobs j2
                WHERE j2.build_id = b.id
                ORDER BY j2.created_at DESC
                LIMIT 1
              )
            """
        ).fetchall()
        for row in rows:
            if row["job_status"] == "failed":
                conn.execute(
                    """
                    UPDATE builds SET status='failed', error=?, finished_at=COALESCE(finished_at, ?)
                    WHERE id=?
                    """,
                    (row["job_error"] or "build failed", now, row["id"]),
                )
            elif row["job_status"] == "done":
                conn.execute(
                    """
                    UPDATE builds SET status='ready', finished_at=COALESCE(finished_at, ?)
                    WHERE id=? AND image_id != ''
                    """,
                    (now, row["id"]),
                )


def list_builds(db: Database, limit: int = 50) -> list[dict[str, Any]]:
    reconcile_stale_builds(db)
    with db.connection() as conn:
        rows = conn.execute(
            """
            SELECT b.*,
              lj.id AS last_job_id,
              lj.status AS last_job_status,
              lj.instance_id AS last_instance_id,
              i.name AS instance_name
            FROM builds b
            LEFT JOIN deployment_jobs lj ON lj.id = (
              SELECT j.id FROM deployment_jobs j
              WHERE j.build_id = b.id
              ORDER BY j.created_at DESC
              LIMIT 1
            )
            LEFT JOIN instances i ON i.id = lj.instance_id
            ORDER BY b.created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["display_status"] = d["status"]
        if d["status"] == "building" and d.get("last_job_status") == "failed":
            d["display_status"] = "failed"
        elif d["status"] == "building" and d.get("last_job_status") == "done":
            d["display_status"] = "ready" if d.get("image_id") else "failed"
        out.append(d)
    return out


def get_build(db: Database, build_id: str) -> dict[str, Any] | None:
    builds = list_builds(db, limit=500)
    for b in builds:
        if b["id"] == build_id:
            return b
    return None


def find_build_retry_instance(db: Database, build_id: str) -> str | None:
    with db.connection() as conn:
        row = conn.execute(
            """
            SELECT j.instance_id FROM deployment_jobs j
            WHERE j.build_id=?
            ORDER BY j.created_at DESC
            LIMIT 1
            """,
            (build_id,),
        ).fetchone()
        if row:
            return row["instance_id"]
        build = conn.execute("SELECT git_commit FROM builds WHERE id=?", (build_id,)).fetchone()
        if not build:
            return None
        row = conn.execute(
            """
            SELECT j.instance_id FROM deployment_jobs j
            WHERE j.job_type='build'
            ORDER BY j.created_at DESC
            LIMIT 20
            """,
        ).fetchall()
        for r in row:
            iid = r["instance_id"]
            inst = conn.execute("SELECT git_commit FROM instances WHERE id=?", (iid,)).fetchone()
            if inst and (inst["git_commit"] or "").startswith((build["git_commit"] or "")[:12]):
                return iid
    return None


def delete_instance(
    db: Database,
    instance_id: str,
    *,
    completing_job_id: str | None = None,
) -> None:
    with db.connection() as conn:
        if completing_job_id:
            active = conn.execute(
                """
                SELECT 1 FROM deployment_jobs
                WHERE instance_id=? AND status IN ('queued', 'running') AND id<>?
                LIMIT 1
                """,
                (instance_id, completing_job_id),
            ).fetchone()
        else:
            active = conn.execute(
                """
                SELECT 1 FROM deployment_jobs
                WHERE instance_id=? AND status IN ('queued', 'running')
                LIMIT 1
                """,
                (instance_id,),
            ).fetchone()
        if active:
            raise ValueError("instance has active jobs")
        conn.execute("DELETE FROM instances WHERE id=?", (instance_id,))


def list_job_log_rows(
    db: Database,
    job_id: str,
    *,
    after_id: int = 0,
) -> list[dict[str, Any]]:
    with db.connection() as conn:
        if after_id > 0:
            rows = conn.execute(
                "SELECT id, line FROM job_logs WHERE job_id=? AND id>? ORDER BY id",
                (job_id, after_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, line FROM job_logs WHERE job_id=? ORDER BY id",
                (job_id,),
            ).fetchall()
    return [dict(r) for r in rows]


def delete_db_profile(db: Database, profile_id: str) -> None:
    with db.connection() as conn:
        used = conn.execute(
            "SELECT COUNT(*) AS c FROM instances WHERE db_profile_id=?", (profile_id,)
        ).fetchone()["c"]
        if used:
            raise ValueError("profile in use by instances")
        conn.execute("DELETE FROM db_profiles WHERE id=?", (profile_id,))


def mark_build_failed(db: Database, build_id: str, error: str) -> None:
    with db.connection() as conn:
        conn.execute(
            """
            UPDATE builds SET status='failed', error=?, finished_at=COALESCE(finished_at, ?)
            WHERE id=? AND status='building'
            """,
            (error[:2000], utc_now(), build_id),
        )


def list_audit(db: Database, limit: int = 100) -> list[dict[str, Any]]:
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def list_users(db: Database) -> list[dict[str, Any]]:
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT id, username, role, active, created_at, updated_at FROM users ORDER BY username"
        ).fetchall()
    return [dict(r) for r in rows]


def get_user(db: Database, user_id: str) -> dict[str, Any] | None:
    with db.connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def set_user_password(db: Database, user_id: str, password_hash: str) -> None:
    now = utc_now()
    with db.connection() as conn:
        conn.execute(
            "UPDATE users SET password_hash=?, updated_at=? WHERE id=?",
            (password_hash, now, user_id),
        )


def count_active_admins(db: Database) -> int:
    with db.connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE role='admin' AND active=1"
        ).fetchone()
    return int(row["c"])


def delete_user(db: Database, user_id: str) -> None:
    with db.connection() as conn:
        conn.execute("UPDATE instances SET created_by=NULL WHERE created_by=?", (user_id,))
        conn.execute("UPDATE instances SET updated_by=NULL WHERE updated_by=?", (user_id,))
        conn.execute("UPDATE instance_configs SET created_by=NULL WHERE created_by=?", (user_id,))
        conn.execute("UPDATE deployment_jobs SET requested_by=NULL WHERE requested_by=?", (user_id,))
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
