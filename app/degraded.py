from __future__ import annotations

import errno
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import psutil

FORCE_DEGRADED_ENV = "TSM_FORCE_DEGRADED"
FORCE_DEGRADED_REASON = (
    "Ограниченный режим включён принудительно (флаг --force-degraded или TSM_FORCE_DEGRADED)"
)


@dataclass(frozen=True)
class DegradedInfo:
    reason: str
    db_path: Path
    disk_full: bool
    forced: bool = False


def is_force_degraded_enabled() -> bool:
    return os.environ.get(FORCE_DEGRADED_ENV, "").strip().lower() in ("1", "true", "yes", "on")


_degraded: DegradedInfo | None = None


def degraded_info() -> DegradedInfo | None:
    return _degraded


def is_degraded() -> bool:
    return _degraded is not None


def set_degraded(info: DegradedInfo) -> None:
    global _degraded
    _degraded = info


def clear_degraded() -> None:
    global _degraded
    _degraded = None


def filesystem_free_bytes(path: Path) -> int | None:
    check = path if path.exists() else path.parent
    if not check.exists():
        check = check.parent
    try:
        return psutil.disk_usage(str(check)).free
    except (OSError, PermissionError):
        return None


def is_filesystem_full(path: Path) -> bool:
    free = filesystem_free_bytes(path)
    return free is not None and free == 0


def is_sqlite_disk_full(exc: BaseException) -> bool:
    if isinstance(exc, sqlite3.Error):
        msg = str(exc).lower()
        if "disk is full" in msg or "no space left" in msg:
            return True
    if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT):
        return True
    return False