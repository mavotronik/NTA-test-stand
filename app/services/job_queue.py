from __future__ import annotations

from typing import Any

BUILD_DEPLOY_JOB_TYPES = ("build", "deploy", "build_run")
LIFECYCLE_JOB_TYPES = ("instance_stop", "instance_start", "instance_delete", "instance_rollback")


def pick_next_queued_job(
    queued: list[dict[str, Any]],
    *,
    building: int,
    deploying: int,
    build_max: int,
    deploy_max: int,
) -> dict[str, Any] | None:
    """Выбрать первую выполнимую задачу; lifecycle не блокируется лимитами сборки."""
    lifecycle = [j for j in queued if j.get("job_type") in LIFECYCLE_JOB_TYPES]
    if lifecycle:
        return lifecycle[0]
    for job in queued:
        jt = job.get("job_type")
        if jt == "build" and building < build_max:
            return job
        if jt == "deploy" and deploying < deploy_max:
            return job
        if jt == "build_run" and building < build_max and deploying < deploy_max:
            return job
    return None
