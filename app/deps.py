from __future__ import annotations

import sqlite3
import threading
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from app.auth import bootstrap_admin, get_session_user, session_from_request
from app.config import Settings, load_settings
from app.degraded import (
    DegradedInfo,
    clear_degraded,
    is_degraded,
    is_filesystem_full,
    is_force_degraded_enabled,
    is_sqlite_disk_full,
    set_degraded,
)
from app.db import Database
from app.host_metrics_db import HostMetricsStore
from app.services.host_metrics import HostMetricsCollector
from app.services.host_metrics_recorder import HostMetricsRecorder
from app.services.job_worker import JobWorker
from app.services.self_update import SelfUpdateService


@lru_cache
def get_settings() -> Settings:
    return load_settings()


DEGRADED_DETAIL = "panel degraded"


def _metrics_disk_paths(settings: Settings) -> list[str]:
    paths = list(settings.policy.host_disk_paths)
    try:
        data = str(settings.data_dir.resolve())
    except OSError:
        data = str(settings.data_dir)
    if data not in paths:
        paths.insert(0, data)
    return paths


def enter_degraded_mode(
    *,
    reason: str,
    db_path: Path,
    disk_full: bool,
    forced: bool = False,
) -> None:
    if is_degraded():
        return
    stop_host_metrics_recorder()
    stop_self_update_service()
    global _worker
    if _worker is not None:
        _worker.stop()
        _worker = None
    set_degraded(
        DegradedInfo(reason=reason, db_path=db_path, disk_full=disk_full, forced=forced)
    )


def get_db(settings: Annotated[Settings, Depends(get_settings)]) -> Database:
    if is_degraded():
        raise HTTPException(status_code=503, detail=DEGRADED_DETAIL)
    try:
        return Database(settings.db_path)
    except (sqlite3.DatabaseError, OSError) as exc:
        disk_full = is_sqlite_disk_full(exc) or is_filesystem_full(settings.db_path)
        enter_degraded_mode(
            reason=str(exc),
            db_path=settings.db_path,
            disk_full=disk_full,
        )
        raise HTTPException(status_code=503, detail=DEGRADED_DETAIL) from exc


_metrics: HostMetricsCollector | None = None
_metrics_store: HostMetricsStore | None = None
_metrics_recorder: HostMetricsRecorder | None = None
_worker: JobWorker | None = None
_self_update: SelfUpdateService | None = None
_recovery_lock = threading.Lock()


def _teardown_partial_recovery() -> None:
    stop_host_metrics_recorder()
    stop_self_update_service()
    global _worker
    if _worker is not None:
        _worker.stop()
        _worker = None


def attempt_degraded_recovery(settings: Settings) -> bool:
    """Пробует выйти из ограниченного режима без перезапуска процесса."""
    with _recovery_lock:
        if not is_degraded():
            return True
        if is_force_degraded_enabled():
            return False
        if is_filesystem_full(settings.db_path):
            return False
        try:
            db = Database(settings.db_path)
            bootstrap_admin(
                db,
                settings.bootstrap_admin_user,
                settings.bootstrap_admin_password,
            )
            if not settings.skip_worker:
                get_worker(settings, db)
            init_self_update_service(settings, db)
            init_host_metrics_recorder(settings)
        except (sqlite3.DatabaseError, OSError):
            _teardown_partial_recovery()
            return False
        clear_degraded()
        return True


def recovery_redirect_path(request: Request) -> str:
    sid = session_from_request(request)
    if not sid:
        return "/login"
    settings = get_settings()
    try:
        db = Database(settings.db_path)
        if get_session_user(db, sid):
            return "/"
    except (sqlite3.DatabaseError, OSError):
        pass
    return "/login"


def get_metrics(settings: Annotated[Settings, Depends(get_settings)]) -> HostMetricsCollector:
    global _metrics
    if _metrics is None:
        _metrics = HostMetricsCollector(settings.host_proc, _metrics_disk_paths(settings))
    return _metrics


def get_metrics_store(settings: Annotated[Settings, Depends(get_settings)]) -> HostMetricsStore:
    global _metrics_store
    if _metrics_store is None:
        _metrics_store = HostMetricsStore(settings.host_metrics_db_path)
    return _metrics_store


def init_host_metrics_recorder(settings: Settings) -> HostMetricsRecorder:
    global _metrics_recorder, _metrics, _metrics_store
    if _metrics is None:
        _metrics = HostMetricsCollector(settings.host_proc, _metrics_disk_paths(settings))
    if _metrics_store is None:
        _metrics_store = HostMetricsStore(settings.host_metrics_db_path)
    if _metrics_recorder is None:
        _metrics_recorder = HostMetricsRecorder(settings, _metrics_store, _metrics)
        _metrics_recorder.start()
    return _metrics_recorder


def stop_host_metrics_recorder() -> None:
    global _metrics_recorder
    if _metrics_recorder is not None:
        _metrics_recorder.stop()
        _metrics_recorder = None


def get_worker(
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[Database, Depends(get_db)],
) -> JobWorker:
    global _worker
    if _worker is None:
        _worker = JobWorker(settings, db)
        _worker.start()
    return _worker


def get_self_update_service(
    settings: Annotated[Settings, Depends(get_settings)],
) -> SelfUpdateService:
    global _self_update
    if _self_update is None:
        _self_update = SelfUpdateService(settings.self_update)
    return _self_update


def init_self_update_service(settings: Settings, db: Database) -> SelfUpdateService:
    """Вызывается из lifespan: аудит и старт фоновой проверки."""
    global _self_update
    from app.services.audit import write_audit

    svc = SelfUpdateService(settings.self_update)
    svc.set_audit_callback(
        lambda **kw: write_audit(db, **kw),
    )
    svc.start()
    _self_update = svc
    return svc


def stop_self_update_service() -> None:
    global _self_update
    if _self_update is not None:
        _self_update.stop()
        _self_update = None


LOGIN_REQUIRED_DETAIL = "login required"


def _wants_json_response(request: Request) -> bool:
    path = request.url.path
    if path.startswith("/api/"):
        return True
    accept = request.headers.get("accept", "")
    if "application/json" in accept and "text/html" not in accept:
        return True
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return True
    return False


async def current_user(request: Request, db: Annotated[Database, Depends(get_db)]):
    sid = session_from_request(request)
    if not sid:
        raise HTTPException(status_code=401, detail=LOGIN_REQUIRED_DETAIL)
    user = get_session_user(db, sid)
    if not user:
        raise HTTPException(status_code=401, detail=LOGIN_REQUIRED_DETAIL)
    return user


async def optional_user(request: Request, db: Annotated[Database, Depends(get_db)]):
    sid = session_from_request(request)
    if not sid:
        return None
    return get_session_user(db, sid)
