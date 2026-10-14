from __future__ import annotations

import re
from typing import Any

from app.db import Database, new_id, utc_now


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


def port_taken(db: Database, port: int, exclude_id: str | None = None) -> bool:
    with db.connection() as conn:
        if exclude_id:
            row = conn.execute(
                "SELECT 1 FROM instances WHERE port=? AND id<>?", (port, exclude_id)
            ).fetchone()
        else:
            row = conn.execute("SELECT 1 FROM instances WHERE port=?", (port,)).fetchone()
    return row is not None


def create_instance(
    db: Database,
    *,
    slug: str,
    name: str,
    port: int,
    db_profile_id: str,
    git_branch: str,
    user_id: str,
) -> str:
    iid = new_id()
    now = utc_now()
    with db.connection() as conn:
        conn.execute(
            """
            INSERT INTO instances(
              id, slug, name, port, state, db_profile_id, git_branch,
              created_by, updated_by, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                iid, slug, name, port, "stopped", db_profile_id, git_branch,
                user_id, user_id, now, now,
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


def queue_job(db: Database, instance_id: str, job_type: str, user_id: str) -> str:
    jid = new_id()
    with db.connection() as conn:
        active = conn.execute(
            """
            SELECT 1 FROM deployment_jobs
            WHERE instance_id=? AND status IN ('queued','running') AND job_type IN ('deploy','build')
            LIMIT 1
            """,
            (instance_id,),
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


def list_builds(db: Database, limit: int = 50) -> list[dict[str, Any]]:
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT * FROM builds ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


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
