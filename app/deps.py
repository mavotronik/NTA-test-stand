from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from app.auth import get_session_user, session_from_request
from app.config import Settings, load_settings
from app.db import Database
from app.services.host_metrics import HostMetricsCollector
from app.services.job_worker import JobWorker


@lru_cache
def get_settings() -> Settings:
    return load_settings()


def get_db(settings: Annotated[Settings, Depends(get_settings)]) -> Database:
    return Database(settings.db_path)


_metrics: HostMetricsCollector | None = None
_worker: JobWorker | None = None


def get_metrics(settings: Annotated[Settings, Depends(get_settings)]) -> HostMetricsCollector:
    global _metrics
    if _metrics is None:
        _metrics = HostMetricsCollector(settings.host_proc, settings.policy.host_disk_paths)
    return _metrics


def get_worker(
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[Database, Depends(get_db)],
) -> JobWorker:
    global _worker
    if _worker is None:
        _worker = JobWorker(settings, db)
        _worker.start()
    return _worker


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
