from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.exception_handlers import http_exception_handler
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import auth
from app.auth import (
    authenticate,
    bootstrap_admin,
    create_session,
    delete_session,
    require_role,
    session_from_request,
    verify_csrf,
)
from app.config import branch_allowed
from app.crypto_util import encrypt_secret, get_fernet, hash_password
from app.db import Database, new_id, utc_now
from app.deps import (
    LOGIN_REQUIRED_DETAIL,
    _wants_json_response,
    current_user,
    get_db,
    get_metrics,
    get_settings,
    get_worker,
    optional_user,
)
from app.repositories import (
    create_instance,
    delete_db_profile,
    find_build_retry_instance,
    get_build,
    get_db_profile,
    get_instance,
    get_instance_active_job,
    get_instance_job,
    list_audit,
    list_builds,
    list_db_profiles,
    list_instances,
    list_job_log_rows,
    list_jobs,
    list_occupied_ports,
    list_ready_build_images,
    list_users,
    get_user,
    set_user_password,
    count_active_admins,
    delete_user,
    next_free_port,
    port_owner,
    queue_job,
    save_db_profile,
    update_instance,
    update_profile_check,
    validate_slug,
)
from app.services.audit import write_audit
from app.services.clickhouse_check import check_clickhouse_connection
from app.services import git_service

APP_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    db = Database(settings.db_path)
    bootstrap_admin(db, settings.bootstrap_admin_user, settings.bootstrap_admin_password)
    worker = None if settings.skip_worker else get_worker(settings, db)
    yield
    if worker:
        worker.stop()


app = FastAPI(title="Test Stand Manager", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


@app.exception_handler(HTTPException)
async def handle_http_exception(request: Request, exc: HTTPException):
    if exc.status_code == 401 and exc.detail == LOGIN_REQUIRED_DETAIL and not _wants_json_response(request):
        return RedirectResponse(url="/login", status_code=302)
    return await http_exception_handler(request, exc)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


JOB_TYPE_LABELS: dict[str, str] = {
    "build": "Сборка",
    "deploy": "Развёртывание",
    "build_run": "Сборка и запуск",
    "instance_stop": "Остановка инстанса",
    "instance_start": "Запуск инстанса",
    "instance_delete": "Удаление инстанса",
    "instance_rollback": "Откат образа",
}


def _job_log_lines(db: Database, job_id: str, *, after_id: int = 0) -> list[dict[str, Any]]:
    return list_job_log_rows(db, job_id, after_id=after_id)


def _instance_watch_job(db: Database, instance_id: str, job_param: str = "") -> dict[str, Any] | None:
    """Логи и «Открыть UI» только для «Собрать и запустить» (?job=…)."""
    if not job_param.strip():
        return None
    job = get_instance_job(db, instance_id, job_param.strip())
    if job and job["job_type"] == "build_run":
        return job
    return None


def _instance_current_task(db: Database, instance_id: str) -> dict[str, Any] | None:
    active = get_instance_active_job(db, instance_id)
    if active:
        return active
    with db.connection() as conn:
        row = conn.execute(
            "SELECT * FROM deployment_jobs WHERE instance_id=? ORDER BY created_at DESC LIMIT 1",
            (instance_id,),
        ).fetchone()
    return dict(row) if row else None


AUDIT_ACTION_LABELS: dict[str, str] = {
    "login": "Вход в систему",
    "login_failed": "Неудачный вход",
    "logout": "Выход",
    "instance_create": "Создание инстанса",
    "instance_update": "Изменение инстанса",
    "instance_stop": "Остановка инстанса",
    "instance_start": "Запуск инстанса",
    "instance_delete": "Удаление инстанса",
    "instance_rollback": "Откат образа",
    "build_queue": "Постановка сборки в очередь",
    "build_run_queue": "Сборка и запуск",
    "build_retry": "Повтор сборки",
    "deploy_queue": "Постановка развёртывания в очередь",
    "db_profile_save": "Сохранение профиля БД",
    "db_profile_test": "Проверка профиля БД",
    "db_profile_delete": "Удаление профиля БД",
    "user_create": "Создание пользователя",
    "user_password": "Смена пароля",
    "user_delete": "Удаление пользователя",
}

AUDIT_ACTION_ICONS: dict[str, str] = {
    "login": "mdi-login",
    "login_failed": "mdi-alert-circle-outline",
    "logout": "mdi-logout",
    "instance_create": "mdi-server-plus",
    "instance_update": "mdi-pencil",
    "instance_stop": "mdi-stop",
    "instance_start": "mdi-play",
    "instance_delete": "mdi-delete",
    "instance_rollback": "mdi-backup-restore",
    "build_queue": "mdi-docker",
    "build_run_queue": "mdi-play-circle",
    "build_retry": "mdi-refresh",
    "deploy_queue": "mdi-rocket-launch",
    "db_profile_save": "mdi-database-edit",
    "db_profile_test": "mdi-lan-check",
    "db_profile_delete": "mdi-database-remove",
    "user_create": "mdi-account-plus",
    "user_password": "mdi-key",
    "user_delete": "mdi-account-remove",
}

AUDIT_ACTION_TONE: dict[str, str] = {
    "login_failed": "danger",
    "instance_stop": "warning",
    "instance_delete": "warning",
    "instance_rollback": "warning",
    "db_profile_delete": "warning",
    "user_delete": "warning",
}


def _render(request: Request, name: str, ctx: dict[str, Any], status: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(name, {"request": request, **ctx}, status_code=status)


@app.get("/api/health")
def api_health() -> dict[str, str]:
    return {"ok": "true"}


@app.get("/api/host-metrics")
def api_host_metrics(metrics=Depends(get_metrics), user=Depends(current_user)) -> JSONResponse:
    return JSONResponse(metrics.as_dict())


@app.get("/partials/host-metrics", response_class=HTMLResponse)
def partial_host_metrics(request: Request, metrics=Depends(get_metrics), settings=Depends(get_settings), user=Depends(current_user)):
    snap = metrics.as_dict()
    return _render(
        request,
        "partials/host_metrics.html",
        {"metrics": snap, "user": user, "refresh_sec": settings.policy.host_metrics_refresh_sec},
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, user=Depends(optional_user)):
    if user:
        return RedirectResponse("/", status_code=302)
    return _render(request, "login.html", {"user": None})


@app.post("/login")
async def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Database = Depends(get_db),
    settings=Depends(get_settings),
):
    user = authenticate(db, username.strip(), password)
    ip = _client_ip(request)
    if not user:
        write_audit(db, user_id=None, username=username, role=None, action="login_failed", ip=ip, ok=False)
        return _render(request, "login.html", {"user": None, "error": "Неверный логин или пароль"}, status=401)
    sid, _csrf = create_session(db, user["id"], settings.session_ttl_hours)
    write_audit(db, user_id=user["id"], username=user["username"], role=user["role"], action="login", ip=ip)
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie("tsm_session", sid, httponly=True, samesite="lax", max_age=settings.session_ttl_hours * 3600)
    return resp


@app.post("/logout")
async def logout_post(request: Request, csrf_token: str = Form(""), db: Database = Depends(get_db)):
    sid = session_from_request(request)
    user = auth.get_session_user(db, sid) if sid else None
    if user:
        verify_csrf(request, user, csrf_token)
        delete_session(db, sid or "")
        write_audit(
            db,
            user_id=user["id"],
            username=user["username"],
            role=user["role"],
            action="logout",
            ip=_client_ip(request),
        )
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("tsm_session")
    return resp


@app.get("/", response_class=HTMLResponse)
async def overview(
    request: Request,
    db: Database = Depends(get_db),
    settings=Depends(get_settings),
    user=Depends(optional_user),
    metrics=Depends(get_metrics),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    instances = list_instances(db)
    jobs = list_jobs(db, 20)
    queued = sum(1 for j in jobs if j["status"] == "queued")
    return _render(
        request,
        "overview.html",
        {
            "user": user,
            "active": "overview",
            "instances": instances,
            "running_count": sum(1 for i in instances if i["state"] == "running"),
            "failed_count": sum(1 for i in instances if i["state"] == "failed"),
            "queued_jobs": queued,
            "refresh_sec": settings.policy.host_metrics_refresh_sec,
            "metrics": metrics.as_dict(),
        },
    )


@app.get("/instances", response_class=HTMLResponse)
async def instances_page(request: Request, db: Database = Depends(get_db), settings=Depends(get_settings), user=Depends(current_user)):
    message = request.query_params.get("message", "").replace("+", " ")
    error = request.query_params.get("error", "").replace("+", " ")
    return _render(
        request,
        "instances.html",
        {
            "user": user,
            "active": "instances",
            "instances": list_instances(db),
            "public_host": settings.policy.public_host,
            "message": message,
            "error": error,
        },
    )


def _instance_form_ctx(
    db: Database,
    settings,
    *,
    instance: dict | None = None,
    form: dict | None = None,
) -> dict:
    repo = Path(settings.policy.git_remote_url)
    branches = git_service.list_branches(repo, settings.policy) or ["main"]
    exclude_id = instance["id"] if instance else None
    occupied = list_occupied_ports(db, exclude_id=exclude_id)
    suggested = next_free_port(
        db,
        settings.policy.port_min,
        settings.policy.port_max,
        exclude_id=exclude_id,
    )
    base_form: dict[str, Any] = {}
    if instance:
        base_form = {
            "name": instance["name"],
            "port": instance["port"],
            "db_profile_id": instance["db_profile_id"],
            "git_branch": instance["git_branch"],
            "git_commit": instance.get("git_commit") or "",
            "image_tag": instance.get("image_tag") or "",
        }
    elif suggested is not None:
        base_form["port"] = suggested
    if form:
        base_form.update(form)
    return {
        "profiles": list_db_profiles(db),
        "branches": branches,
        "port_min": settings.policy.port_min,
        "port_max": settings.policy.port_max,
        "suggested_port": suggested,
        "occupied_ports_json": json.dumps(
            [
                {"port": int(row["port"]), "name": row["name"], "slug": row["slug"]}
                for row in occupied
            ],
            ensure_ascii=False,
        ),
        "ready_images": list_ready_build_images(db),
        "form": base_form,
        "instance": instance,
    }


def _port_conflict_message(owner: dict[str, Any], port: int) -> str:
    return f"Порт {port} уже занят инстансом «{owner['name']}»"


def _instance_action_redirect(instance_id: str, return_to: str, *, message: str = "") -> RedirectResponse:
    path = "/instances" if return_to == "instances" else f"/instances/{instance_id}"
    if message:
        path = f"{path}?message={message}"
    return RedirectResponse(path, status_code=302)


_BULK_INSTANCE_ACTIONS = {
    "start": "instance_start",
    "stop": "instance_stop",
    "rebuild": "build_run",
}


def _bulk_skip_reason(inst: dict[str, Any] | None, action: str) -> str | None:
    if not inst:
        return "не найден"
    if action == "start":
        if not (inst.get("image_tag") or "").strip():
            return "нет образа"
        if inst["state"] == "running":
            return "уже запущен"
        if inst["state"] == "deploying":
            return "идёт развёртывание"
    return None


def _instances_bulk_redirect(*, message: str = "", error: str = "") -> RedirectResponse:
    path = "/instances"
    params: list[str] = []
    if message:
        params.append(f"message={quote(message)}")
    if error:
        params.append(f"error={quote(error)}")
    if params:
        path = f"{path}?{'&'.join(params)}"
    return RedirectResponse(path, status_code=302)


@app.get("/instances/new", response_class=HTMLResponse)
async def instance_new_page(request: Request, db: Database = Depends(get_db), settings=Depends(get_settings), user=Depends(current_user)):
    require_role(user, "operator")
    ctx = _instance_form_ctx(db, settings)
    return _render(
        request,
        "instance_form.html",
        {"user": user, "active": "instances", **ctx},
    )


@app.post("/instances/new")
async def instance_new_post(
    request: Request,
    name: str = Form(...),
    slug: str = Form(...),
    port: int = Form(...),
    db_profile_id: str = Form(...),
    git_branch: str = Form(...),
    git_commit: str = Form(""),
    image_tag: str = Form(""),
    return_to: str = Form(""),
    csrf_token: str = Form(""),
    db: Database = Depends(get_db),
    settings=Depends(get_settings),
    user=Depends(current_user),
):
    require_role(user, "operator")
    verify_csrf(request, user, csrf_token)
    slug = slug.strip().lower()
    if not validate_slug(slug):
        raise HTTPException(400, "invalid slug")
    if port < settings.policy.port_min or port > settings.policy.port_max:
        raise HTTPException(400, "port out of range")
    owner = port_owner(db, port)
    if owner:
        ctx = _instance_form_ctx(
            db,
            settings,
            form={
                "name": name.strip(),
                "slug": slug,
                "port": port,
                "db_profile_id": db_profile_id,
                "git_branch": git_branch.strip(),
                "git_commit": git_commit,
                "image_tag": image_tag,
            },
        )
        return _render(
            request,
            "instance_form.html",
            {
                **ctx,
                "user": user,
                "active": "instances",
                "return_to": return_to,
                "port_error": _port_conflict_message(owner, port),
            },
            status=409,
        )
    if not branch_allowed(settings.policy, git_branch):
        raise HTTPException(400, "branch not allowed")
    prof = get_db_profile(db, db_profile_id)
    if not prof or prof.get("disabled"):
        raise HTTPException(400, "invalid profile")
    iid = create_instance(
        db,
        slug=slug,
        name=name.strip(),
        port=port,
        db_profile_id=db_profile_id,
        git_branch=git_branch.strip(),
        user_id=user["id"],
    )
    write_audit(
        db,
        user_id=user["id"],
        username=user["username"],
        role=user["role"],
        action="instance_create",
        target_type="instance",
        target_id=iid,
        ip=_client_ip(request),
    )
    return RedirectResponse(f"/instances/{iid}", status_code=302)


@app.get("/instances/{instance_id}/edit", response_class=HTMLResponse)
async def instance_edit_page(
    request: Request,
    instance_id: str,
    db: Database = Depends(get_db),
    settings=Depends(get_settings),
    user=Depends(current_user),
):
    require_role(user, "operator")
    inst = get_instance(db, instance_id)
    if not inst:
        raise HTTPException(404)
    ctx = _instance_form_ctx(db, settings, instance=inst)
    return _render(
        request,
        "instance_form.html",
        {"user": user, "active": "instances", **ctx},
    )


@app.post("/instances/{instance_id}/edit")
async def instance_edit_post(
    request: Request,
    instance_id: str,
    name: str = Form(...),
    port: int = Form(...),
    db_profile_id: str = Form(...),
    git_branch: str = Form(...),
    git_commit: str = Form(""),
    image_tag: str = Form(""),
    return_to: str = Form(""),
    csrf_token: str = Form(""),
    db: Database = Depends(get_db),
    settings=Depends(get_settings),
    user=Depends(current_user),
):
    require_role(user, "operator")
    verify_csrf(request, user, csrf_token)
    inst = get_instance(db, instance_id)
    if not inst:
        raise HTTPException(404)
    if port < settings.policy.port_min or port > settings.policy.port_max:
        raise HTTPException(400, "port out of range")
    owner = port_owner(db, port, exclude_id=instance_id)
    if owner:
        ctx = _instance_form_ctx(
            db,
            settings,
            instance=inst,
            form={
                "name": name.strip(),
                "port": port,
                "db_profile_id": db_profile_id,
                "git_branch": git_branch.strip(),
                "git_commit": git_commit,
                "image_tag": image_tag,
            },
        )
        return _render(
            request,
            "instance_form.html",
            {
                **ctx,
                "user": user,
                "active": "instances",
                "return_to": return_to,
                "port_error": _port_conflict_message(owner, port),
            },
            status=409,
        )
    if not branch_allowed(settings.policy, git_branch):
        raise HTTPException(400, "branch not allowed")
    prof = get_db_profile(db, db_profile_id)
    if not prof or prof.get("disabled"):
        raise HTTPException(400, "invalid profile")
    repo = Path(settings.policy.git_remote_url)
    commit_pin = git_commit.strip()
    if commit_pin:
        try:
            git_service.resolve_instance_commit(
                repo,
                {"git_commit": commit_pin, "git_branch": git_branch.strip()},
                settings.policy,
            )
        except git_service.GitError as err:
            ctx = _instance_form_ctx(
                db,
                settings,
                instance=inst,
                form={
                    "name": name.strip(),
                    "port": port,
                    "db_profile_id": db_profile_id,
                    "git_branch": git_branch.strip(),
                    "git_commit": git_commit,
                    "image_tag": image_tag,
                },
            )
            return _render(
                request,
                "instance_form.html",
                {"user": user, "active": "instances", "return_to": return_to, "error": str(err), **ctx},
                status=400,
            )
    update_instance(
        db,
        instance_id,
        name=name.strip(),
        port=port,
        db_profile_id=db_profile_id,
        git_branch=git_branch.strip(),
        git_commit=commit_pin,
        image_tag=image_tag.strip(),
        user_id=user["id"],
    )
    write_audit(
        db,
        user_id=user["id"],
        username=user["username"],
        role=user["role"],
        action="instance_update",
        target_type="instance",
        target_id=instance_id,
        ip=_client_ip(request),
    )
    if return_to == "instances":
        return RedirectResponse("/instances?message=instance+updated", status_code=302)
    return RedirectResponse(f"/instances/{instance_id}?message=instance+updated", status_code=302)


@app.get("/instances/{instance_id}", response_class=HTMLResponse)
async def instance_detail(request: Request, instance_id: str, db: Database = Depends(get_db), settings=Depends(get_settings), user=Depends(current_user)):
    inst = get_instance(db, instance_id)
    if not inst:
        raise HTTPException(404)
    message = request.query_params.get("message", "").replace("+", " ")
    error = request.query_params.get("error", "").replace("+", " ")
    with db.connection() as conn:
        jobs = [dict(r) for r in conn.execute(
            "SELECT * FROM deployment_jobs WHERE instance_id=? ORDER BY created_at DESC LIMIT 20",
            (instance_id,),
        ).fetchall()]
    job_param = request.query_params.get("job", "")
    watch_job = _instance_watch_job(db, instance_id, job_param)
    current_task = _instance_current_task(db, instance_id)
    active_job = get_instance_active_job(db, instance_id)
    watch_log_rows = _job_log_lines(db, watch_job["id"]) if watch_job else []
    return _render(
        request,
        "instance_detail.html",
        {
            "user": user,
            "active": "instances",
            "instance": inst,
            "jobs": jobs,
            "watch_job": watch_job,
            "watch_log_rows": watch_log_rows,
            "current_task": current_task,
            "active_job": active_job,
            "job_type_labels": JOB_TYPE_LABELS,
            "public_host": settings.policy.public_host,
            "message": message,
            "error": error,
        },
    )


@app.post("/instances/{instance_id}/rollback")
async def instance_rollback(
    request: Request,
    instance_id: str,
    csrf_token: str = Form(""),
    db: Database = Depends(get_db),
    user=Depends(current_user),
    _worker=Depends(get_worker),
):
    require_role(user, "operator")
    verify_csrf(request, user, csrf_token)
    inst = get_instance(db, instance_id)
    if not inst or not inst.get("rollback_image_tag"):
        raise HTTPException(400, "nothing to rollback")
    try:
        jid = queue_job(db, instance_id, "instance_rollback", user["id"])
    except ValueError as err:
        return RedirectResponse(f"/instances/{instance_id}?error={err}", status_code=302)
    write_audit(
        db,
        user_id=user["id"],
        username=user["username"],
        role=user["role"],
        action="instance_rollback",
        target_type="job",
        target_id=jid,
        ip=_client_ip(request),
    )
    return RedirectResponse(f"/instances/{instance_id}?message=rollback+queued", status_code=302)


@app.post("/instances/{instance_id}/build")
async def instance_build(
    request: Request,
    instance_id: str,
    csrf_token: str = Form(""),
    db: Database = Depends(get_db),
    user=Depends(current_user),
    _worker=Depends(get_worker),
):
    require_role(user, "operator")
    verify_csrf(request, user, csrf_token)
    inst = get_instance(db, instance_id)
    if not inst:
        raise HTTPException(404)
    try:
        jid = queue_job(db, instance_id, "build", user["id"])
    except ValueError as err:
        return RedirectResponse(f"/instances/{instance_id}?error={err}", status_code=302)
    write_audit(db, user_id=user["id"], username=user["username"], role=user["role"], action="build_queue", target_type="job", target_id=jid, ip=_client_ip(request))
    return RedirectResponse(f"/instances/{instance_id}?message=build+queued", status_code=302)


@app.post("/instances/{instance_id}/build-run")
async def instance_build_run(
    request: Request,
    instance_id: str,
    csrf_token: str = Form(""),
    db: Database = Depends(get_db),
    user=Depends(current_user),
    _worker=Depends(get_worker),
):
    require_role(user, "operator")
    verify_csrf(request, user, csrf_token)
    inst = get_instance(db, instance_id)
    if not inst:
        raise HTTPException(404)
    try:
        jid = queue_job(db, instance_id, "build_run", user["id"])
    except ValueError as err:
        return RedirectResponse(f"/instances/{instance_id}?error={err}", status_code=302)
    write_audit(
        db,
        user_id=user["id"],
        username=user["username"],
        role=user["role"],
        action="build_run_queue",
        target_type="job",
        target_id=jid,
        ip=_client_ip(request),
    )
    return RedirectResponse(f"/instances/{instance_id}?job={jid}", status_code=302)


@app.get("/instances/{instance_id}/jobs/{job_id}/status", response_class=HTMLResponse)
async def instance_job_status_partial(
    request: Request,
    instance_id: str,
    job_id: str,
    db: Database = Depends(get_db),
    settings=Depends(get_settings),
    user=Depends(current_user),
):
    inst = get_instance(db, instance_id)
    if not inst:
        raise HTTPException(404)
    job = get_instance_job(db, instance_id, job_id)
    if not job:
        raise HTTPException(404)
    return _render(
        request,
        "partials/instance_job_status.html",
        {
            "user": user,
            "instance": inst,
            "job": job,
            "job_type_labels": JOB_TYPE_LABELS,
            "public_host": settings.policy.public_host,
        },
    )


@app.get("/instances/{instance_id}/jobs/{job_id}/logs", response_class=HTMLResponse)
async def instance_job_logs_partial(
    request: Request,
    instance_id: str,
    job_id: str,
    db: Database = Depends(get_db),
    user=Depends(current_user),
):
    if not get_instance(db, instance_id):
        raise HTTPException(404)
    if not get_instance_job(db, instance_id, job_id):
        raise HTTPException(404)
    after_id = int(request.query_params.get("after_id", "0") or "0")
    log_rows = _job_log_lines(db, job_id, after_id=after_id)
    return _render(request, "partials/job_log_lines.html", {"log_rows": log_rows})


@app.get("/instances/{instance_id}/current-task", response_class=HTMLResponse)
async def instance_current_task_partial(
    request: Request,
    instance_id: str,
    db: Database = Depends(get_db),
    user=Depends(current_user),
):
    if not get_instance(db, instance_id):
        raise HTTPException(404)
    task = _instance_current_task(db, instance_id)
    return _render(
        request,
        "partials/instance_current_task.html",
        {"user": user, "task": task, "job_type_labels": JOB_TYPE_LABELS},
    )


@app.post("/instances/{instance_id}/deploy")
async def instance_deploy(
    request: Request,
    instance_id: str,
    csrf_token: str = Form(""),
    db: Database = Depends(get_db),
    user=Depends(current_user),
    _worker=Depends(get_worker),
):
    require_role(user, "operator")
    verify_csrf(request, user, csrf_token)
    inst = get_instance(db, instance_id)
    if not inst:
        raise HTTPException(404)
    prof = get_db_profile(db, inst["db_profile_id"], include_secret=False)
    if prof and prof.get("writable"):
        confirm = request.headers.get("X-Writable-Confirm")
        if confirm != "1" and not request.query_params.get("writable_ok"):
            pass
    try:
        jid = queue_job(db, instance_id, "deploy", user["id"])
    except ValueError as err:
        return RedirectResponse(f"/instances/{instance_id}?error={err}", status_code=302)
    write_audit(db, user_id=user["id"], username=user["username"], role=user["role"], action="deploy_queue", target_type="job", target_id=jid, ip=_client_ip(request))
    return RedirectResponse(f"/instances/{instance_id}?message=deploy+queued", status_code=302)


@app.post("/instances/{instance_id}/start")
async def instance_start(
    request: Request,
    instance_id: str,
    csrf_token: str = Form(""),
    return_to: str = Form(""),
    db: Database = Depends(get_db),
    user=Depends(current_user),
    _worker=Depends(get_worker),
):
    require_role(user, "operator")
    verify_csrf(request, user, csrf_token)
    inst = get_instance(db, instance_id)
    if not inst:
        raise HTTPException(404)
    image = (inst.get("image_tag") or "").strip()
    if not image:
        raise HTTPException(400, "no image on instance")
    if inst["state"] == "running":
        raise HTTPException(400, "already running")
    if inst["state"] == "deploying":
        raise HTTPException(409, "instance is deploying")
    try:
        jid = queue_job(db, instance_id, "instance_start", user["id"])
    except ValueError as err:
        path = "/instances" if return_to == "instances" else f"/instances/{instance_id}"
        return RedirectResponse(f"{path}?error={err}", status_code=302)
    write_audit(
        db,
        user_id=user["id"],
        username=user["username"],
        role=user["role"],
        action="instance_start",
        target_type="job",
        target_id=jid,
        ip=_client_ip(request),
    )
    return _instance_action_redirect(instance_id, return_to, message="start+queued")


@app.post("/instances/{instance_id}/stop")
async def instance_stop(
    request: Request,
    instance_id: str,
    csrf_token: str = Form(""),
    return_to: str = Form(""),
    db: Database = Depends(get_db),
    user=Depends(current_user),
    _worker=Depends(get_worker),
):
    require_role(user, "operator")
    verify_csrf(request, user, csrf_token)
    inst = get_instance(db, instance_id)
    if not inst:
        raise HTTPException(404)
    try:
        jid = queue_job(db, instance_id, "instance_stop", user["id"])
    except ValueError as err:
        path = "/instances" if return_to == "instances" else f"/instances/{instance_id}"
        return RedirectResponse(f"{path}?error={err}", status_code=302)
    write_audit(
        db,
        user_id=user["id"],
        username=user["username"],
        role=user["role"],
        action="instance_stop",
        target_type="job",
        target_id=jid,
        ip=_client_ip(request),
    )
    return _instance_action_redirect(instance_id, return_to, message="stop+queued")


@app.post("/instances/bulk")
async def instances_bulk_action(
    request: Request,
    action: str = Form(...),
    instance_ids: list[str] = Form(default=[]),
    csrf_token: str = Form(""),
    db: Database = Depends(get_db),
    user=Depends(current_user),
    _worker=Depends(get_worker),
):
    require_role(user, "operator")
    verify_csrf(request, user, csrf_token)
    job_type = _BULK_INSTANCE_ACTIONS.get(action)
    if not job_type:
        raise HTTPException(400, "unknown bulk action")
    seen: set[str] = set()
    queued = 0
    skipped = 0
    audit_action = {
        "start": "instance_start",
        "stop": "instance_stop",
        "rebuild": "build_run_queue",
    }[action]
    for iid in instance_ids:
        if iid in seen:
            continue
        seen.add(iid)
        inst = get_instance(db, iid)
        reason = _bulk_skip_reason(inst, action)
        if reason:
            skipped += 1
            continue
        try:
            jid = queue_job(db, iid, job_type, user["id"])
        except ValueError:
            skipped += 1
            continue
        queued += 1
        write_audit(
            db,
            user_id=user["id"],
            username=user["username"],
            role=user["role"],
            action=audit_action,
            target_type="job",
            target_id=jid,
            ip=_client_ip(request),
        )
    if not seen:
        return _instances_bulk_redirect(error="Не выбрано ни одного инстанса")
    parts: list[str] = []
    if queued:
        parts.append(f"В очередь: {queued}")
    if skipped:
        parts.append(f"пропущено: {skipped}")
    if queued:
        return _instances_bulk_redirect(message=", ".join(parts))
    return _instances_bulk_redirect(error=", ".join(parts) if parts else "Ни один инстанс не поставлен в очередь")


@app.post("/instances/{instance_id}/delete")
async def instance_delete(
    request: Request,
    instance_id: str,
    csrf_token: str = Form(""),
    db: Database = Depends(get_db),
    user=Depends(current_user),
    _worker=Depends(get_worker),
):
    require_role(user, "operator")
    verify_csrf(request, user, csrf_token)
    inst = get_instance(db, instance_id)
    if not inst:
        raise HTTPException(404)
    try:
        jid = queue_job(db, instance_id, "instance_delete", user["id"])
    except ValueError as err:
        return RedirectResponse(f"/instances/{instance_id}?error={err}", status_code=302)
    write_audit(
        db,
        user_id=user["id"],
        username=user["username"],
        role=user["role"],
        action="instance_delete",
        target_type="job",
        target_id=jid,
        ip=_client_ip(request),
    )
    return RedirectResponse(f"/instances/{instance_id}?message=delete+queued", status_code=302)


@app.get("/databases", response_class=HTMLResponse)
async def databases_page(request: Request, db: Database = Depends(get_db), user=Depends(current_user)):
    return _render(
        request,
        "databases.html",
        {
            "user": user,
            "active": "databases",
            "profiles": list_db_profiles(db),
            "message": request.query_params.get("message", ""),
            "error": request.query_params.get("error", ""),
        },
    )


@app.get("/databases/new", response_class=HTMLResponse)
async def database_new_page(request: Request, user=Depends(current_user)):
    require_role(user, "operator")
    return _render(request, "database_form.html", {"user": user, "active": "databases", "profile": None})


@app.get("/databases/{profile_id}/edit", response_class=HTMLResponse)
async def database_edit_page(request: Request, profile_id: str, db: Database = Depends(get_db), user=Depends(current_user)):
    require_role(user, "operator")
    prof = get_db_profile(db, profile_id)
    if not prof:
        raise HTTPException(404)
    return _render(request, "database_form.html", {"user": user, "active": "databases", "profile": prof})


@app.post("/databases/new")
async def database_save_new(
    request: Request,
    name: str = Form(...),
    host: str = Form(...),
    port: int = Form(8123),
    database_name: str = Form("default"),
    username: str = Form(...),
    password: str = Form(""),
    tls: str | None = Form(None),
    writable: str | None = Form(None),
    comment: str = Form(""),
    csrf_token: str = Form(""),
    db: Database = Depends(get_db),
    settings=Depends(get_settings),
    user=Depends(current_user),
):
    require_role(user, "operator")
    verify_csrf(request, user, csrf_token)
    fernet = get_fernet(settings.fernet_key, settings.secret_key)
    if not password:
        raise HTTPException(400, "password required")
    enc = encrypt_secret(fernet, password)
    pid = save_db_profile(
        db,
        profile_id=None,
        name=name.strip(),
        host=host.strip(),
        port=port,
        database_name=database_name.strip(),
        username=username.strip(),
        password_enc=enc,
        tls=bool(tls),
        writable=bool(writable),
        comment=comment.strip(),
    )
    write_audit(db, user_id=user["id"], username=user["username"], role=user["role"], action="db_profile_save", target_type="db_profile", target_id=pid, ip=_client_ip(request))
    return RedirectResponse("/databases", status_code=302)


@app.post("/databases/{profile_id}/edit")
async def database_save_edit(
    request: Request,
    profile_id: str,
    name: str = Form(...),
    host: str = Form(...),
    port: int = Form(8123),
    database_name: str = Form("default"),
    username: str = Form(...),
    password: str = Form(""),
    tls: str | None = Form(None),
    writable: str | None = Form(None),
    comment: str = Form(""),
    csrf_token: str = Form(""),
    db: Database = Depends(get_db),
    settings=Depends(get_settings),
    user=Depends(current_user),
):
    require_role(user, "operator")
    verify_csrf(request, user, csrf_token)
    fernet = get_fernet(settings.fernet_key, settings.secret_key)
    existing = get_db_profile(db, profile_id, include_secret=True)
    if not existing:
        raise HTTPException(404)
    if password:
        enc = encrypt_secret(fernet, password)
    else:
        enc = existing["password_enc"]
    pid = save_db_profile(
        db,
        profile_id=profile_id,
        name=name.strip(),
        host=host.strip(),
        port=port,
        database_name=database_name.strip(),
        username=username.strip(),
        password_enc=enc,
        tls=bool(tls),
        writable=bool(writable),
        comment=comment.strip(),
    )
    write_audit(db, user_id=user["id"], username=user["username"], role=user["role"], action="db_profile_save", target_type="db_profile", target_id=pid, ip=_client_ip(request))
    return RedirectResponse("/databases", status_code=302)


@app.post("/databases/{profile_id}/test")
async def database_test(
    request: Request,
    profile_id: str,
    csrf_token: str = Form(""),
    db: Database = Depends(get_db),
    settings=Depends(get_settings),
    user=Depends(current_user),
):
    require_role(user, "operator")
    verify_csrf(request, user, csrf_token)
    prof = get_db_profile(db, profile_id, include_secret=True)
    if not prof:
        raise HTTPException(404)
    fernet = get_fernet(settings.fernet_key, settings.secret_key)
    from app.crypto_util import decrypt_secret

    pwd = decrypt_secret(fernet, prof["password_enc"])
    ok, msg = await check_clickhouse_connection(
        settings.policy,
        host=prof["host"],
        port=int(prof["port"]),
        database=prof["database_name"],
        username=prof["username"],
        password=pwd,
        use_tls=bool(prof["tls"]),
    )
    update_profile_check(db, profile_id, ok, msg)
    write_audit(
        db,
        user_id=user["id"],
        username=user["username"],
        role=user["role"],
        action="db_profile_test",
        target_type="db_profile",
        target_id=profile_id,
        detail={"ok": ok},
        ip=_client_ip(request),
        ok=ok,
        error=None if ok else msg,
    )
    return RedirectResponse("/databases", status_code=302)


@app.post("/databases/{profile_id}/delete")
async def database_delete(
    request: Request,
    profile_id: str,
    csrf_token: str = Form(""),
    db: Database = Depends(get_db),
    user=Depends(current_user),
):
    require_role(user, "operator")
    verify_csrf(request, user, csrf_token)
    prof = get_db_profile(db, profile_id)
    if not prof:
        raise HTTPException(404)
    try:
        delete_db_profile(db, profile_id)
    except ValueError as err:
        return RedirectResponse(f"/databases?error={err}", status_code=302)
    write_audit(
        db,
        user_id=user["id"],
        username=user["username"],
        role=user["role"],
        action="db_profile_delete",
        target_type="db_profile",
        target_id=profile_id,
        ip=_client_ip(request),
    )
    return RedirectResponse("/databases?message=profile+deleted", status_code=302)


@app.get("/builds", response_class=HTMLResponse)
async def builds_page(request: Request, db: Database = Depends(get_db), user=Depends(current_user)):
    return _render(
        request,
        "builds.html",
        {
            "user": user,
            "active": "builds",
            "builds": list_builds(db),
            "message": request.query_params.get("message", ""),
            "error": request.query_params.get("error", ""),
        },
    )


@app.post("/builds/{build_id}/retry")
async def build_retry(
    request: Request,
    build_id: str,
    csrf_token: str = Form(""),
    db: Database = Depends(get_db),
    user=Depends(current_user),
):
    require_role(user, "operator")
    verify_csrf(request, user, csrf_token)
    build = get_build(db, build_id)
    if not build:
        raise HTTPException(404)
    status = build.get("display_status") or build.get("status")
    if status not in ("failed", "building"):
        return RedirectResponse("/builds?error=retry+only+for+failed+builds", status_code=302)
    instance_id = build.get("last_instance_id") or find_build_retry_instance(db, build_id)
    if not instance_id:
        return RedirectResponse("/builds?error=no+instance+for+retry", status_code=302)
    try:
        jid = queue_job(db, instance_id, "build", user["id"])
    except ValueError:
        return RedirectResponse("/builds?error=job+already+active", status_code=302)
    write_audit(
        db,
        user_id=user["id"],
        username=user["username"],
        role=user["role"],
        action="build_retry",
        target_type="build",
        target_id=build_id,
        detail={"job_id": jid, "instance_id": instance_id},
        ip=_client_ip(request),
    )
    return RedirectResponse(f"/deployments/{jid}", status_code=302)


@app.get("/deployments", response_class=HTMLResponse)
async def deployments_page(request: Request, db: Database = Depends(get_db), user=Depends(current_user)):
    return _render(request, "deployments.html", {"user": user, "active": "deployments", "jobs": list_jobs(db)})


@app.get("/deployments/{job_id}", response_class=HTMLResponse)
async def deployment_detail(request: Request, job_id: str, db: Database = Depends(get_db), user=Depends(current_user)):
    with db.connection() as conn:
        job = conn.execute("SELECT * FROM deployment_jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            raise HTTPException(404)
        log_rows = list_job_log_rows(db, job_id)
    return _render(
        request,
        "job_detail.html",
        {
            "user": user,
            "active": "deployments",
            "job": dict(job),
            "log_rows": log_rows,
            "job_type_labels": JOB_TYPE_LABELS,
        },
    )


@app.get("/deployments/{job_id}/logs", response_class=HTMLResponse)
async def deployment_logs_partial(request: Request, job_id: str, db: Database = Depends(get_db), user=Depends(current_user)):
    with db.connection() as conn:
        job = conn.execute("SELECT * FROM deployment_jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        raise HTTPException(404)
    after_id = int(request.query_params.get("after_id", "0") or "0")
    log_rows = list_job_log_rows(db, job_id, after_id=after_id)
    return _render(request, "partials/job_log_lines.html", {"log_rows": log_rows})


@app.get("/audit", response_class=HTMLResponse)
async def audit_page(request: Request, db: Database = Depends(get_db), user=Depends(current_user)):
    return _render(
        request,
        "audit.html",
        {
            "user": user,
            "active": "audit",
            "events": list_audit(db),
            "audit_action_labels": AUDIT_ACTION_LABELS,
            "audit_action_icons": AUDIT_ACTION_ICONS,
            "audit_action_tone": AUDIT_ACTION_TONE,
        },
    )


@app.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, db: Database = Depends(get_db), user=Depends(current_user)):
    require_role(user, "admin")
    return _render(request, "users.html", {"user": user, "active": "users", "users": list_users(db)})


@app.post("/users")
async def users_create(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("viewer"),
    csrf_token: str = Form(""),
    db: Database = Depends(get_db),
    user=Depends(current_user),
):
    require_role(user, "admin")
    verify_csrf(request, user, csrf_token)
    if role not in auth.ROLES:
        raise HTTPException(400, "bad role")
    now = utc_now()
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO users(id, username, password_hash, role, active, created_at, updated_at) VALUES (?,?,?,?,1,?,?)",
            (new_id(), username.strip(), hash_password(password), role, now, now),
        )
    write_audit(db, user_id=user["id"], username=user["username"], role=user["role"], action="user_create", detail={"username": username, "role": role}, ip=_client_ip(request))
    return RedirectResponse("/users", status_code=302)


@app.post("/users/{user_id}/password")
async def users_set_password(
    request: Request,
    user_id: str,
    password: str = Form(...),
    csrf_token: str = Form(""),
    db: Database = Depends(get_db),
    user=Depends(current_user),
):
    require_role(user, "admin")
    verify_csrf(request, user, csrf_token)
    target = get_user(db, user_id)
    if not target:
        raise HTTPException(404)
    pwd = password.strip()
    if not pwd:
        raise HTTPException(400, "password required")
    set_user_password(db, user_id, hash_password(pwd))
    write_audit(
        db,
        user_id=user["id"],
        username=user["username"],
        role=user["role"],
        action="user_password",
        target_type="user",
        target_id=user_id,
        detail={"username": target["username"]},
        ip=_client_ip(request),
    )
    return RedirectResponse("/users?message=password", status_code=302)


@app.post("/users/{user_id}/delete")
async def users_delete(
    request: Request,
    user_id: str,
    csrf_token: str = Form(""),
    db: Database = Depends(get_db),
    user=Depends(current_user),
):
    require_role(user, "admin")
    verify_csrf(request, user, csrf_token)
    if user_id == user["id"]:
        raise HTTPException(400, "cannot delete self")
    target = get_user(db, user_id)
    if not target:
        raise HTTPException(404)
    if target["role"] == "admin" and target["active"] and count_active_admins(db) <= 1:
        raise HTTPException(400, "last admin")
    delete_user(db, user_id)
    write_audit(
        db,
        user_id=user["id"],
        username=user["username"],
        role=user["role"],
        action="user_delete",
        target_type="user",
        target_id=user_id,
        detail={"username": target["username"]},
        ip=_client_ip(request),
    )
    return RedirectResponse("/users?message=deleted", status_code=302)
