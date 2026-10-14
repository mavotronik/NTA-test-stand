from __future__ import annotations

import json
from typing import Any

from app.db import Database, utc_now


def write_audit(
    db: Database,
    *,
    user_id: str | None,
    username: str | None,
    role: str | None,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
    ok: bool = True,
    error: str | None = None,
) -> None:
    safe_detail = _redact_detail(detail or {})
    with db.connection() as conn:
        conn.execute(
            """
            INSERT INTO audit_log(
              user_id, username, role, action, target_type, target_id,
              detail_json, ip, ok, error, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                user_id,
                username,
                role,
                action,
                target_type,
                target_id,
                json.dumps(safe_detail, ensure_ascii=False),
                ip,
                1 if ok else 0,
                error,
                utc_now(),
            ),
        )


def _redact_detail(detail: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in detail.items():
        lk = k.lower()
        if any(x in lk for x in ("password", "secret", "token", "enc")):
            out[k] = "***"
        elif isinstance(v, dict):
            out[k] = _redact_detail(v)
        else:
            out[k] = v
    return out
