from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, Request

from app.crypto_util import hash_password, verify_password
from app.db import Database, new_id, utc_now


ROLES = ("admin", "operator", "viewer")

ROLE_RANK = {"viewer": 1, "operator": 2, "admin": 3}


def require_role(user: dict[str, Any], min_role: str) -> None:
    if ROLE_RANK.get(user["role"], 0) < ROLE_RANK.get(min_role, 99):
        raise HTTPException(status_code=403, detail="Недостаточно прав")


def bootstrap_admin(db: Database, username: str, password: str) -> None:
    if not password:
        return
    with db.connection() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        if n:
            return
        now = utc_now()
        conn.execute(
            """
            INSERT INTO users(id, username, password_hash, role, active, created_at, updated_at)
            VALUES (?,?,?,?,1,?,?)
            """,
            (new_id(), username, hash_password(password), "admin", now, now),
        )


def authenticate(db: Database, username: str, password: str) -> dict[str, Any] | None:
    with db.connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username=? AND active=1", (username,)
        ).fetchone()
    if not row or not verify_password(row["password_hash"], password):
        return None
    return dict(row)


def create_session(db: Database, user_id: str, ttl_hours: int) -> tuple[str, str]:
    sid = new_id()
    csrf = secrets.token_urlsafe(32)
    exp = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).replace(microsecond=0).isoformat()
    with db.connection() as conn:
        conn.execute(
            """
            INSERT INTO sessions(id, user_id, csrf_token, expires_at, created_at)
            VALUES (?,?,?,?,?)
            """,
            (sid, user_id, csrf, exp, utc_now()),
        )
    return sid, csrf


def delete_session(db: Database, session_id: str) -> None:
    with db.connection() as conn:
        conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))


def get_session_user(db: Database, session_id: str) -> dict[str, Any] | None:
    with db.connection() as conn:
        row = conn.execute(
            """
            SELECT s.id AS session_id, s.csrf_token, s.expires_at,
                   u.id, u.username, u.role, u.active
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.id=?
            """,
            (session_id,),
        ).fetchone()
    if not row:
        return None
    exp = datetime.fromisoformat(row["expires_at"])
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp < datetime.now(timezone.utc):
        delete_session(db, session_id)
        return None
    if not row["active"]:
        return None
    return dict(row)


def session_from_request(request: Request) -> str | None:
    return request.cookies.get("tsm_session")


def verify_csrf(
    request: Request,
    user: dict[str, Any],
    csrf_token: str | None = None,
) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    token = (request.headers.get("X-CSRF-Token") or csrf_token or "").strip()
    if not token or token != user.get("csrf_token"):
        raise HTTPException(status_code=403, detail="CSRF validation failed")
