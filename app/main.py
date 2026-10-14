from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

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
    get_db_profile,
    get_instance,
    list_audit,
    list_builds,
    list_db_profiles,
    list_instances,
    list_jobs,
    list_users,
    port_taken,
    queue_job,
    save_db_profile,
    update_profile_check,
    validate_slug,
)
from app.services.audit import write_audit
from app.services.clickhouse_check import check_clickhouse_connection
from app.services.docker_service import DockerService
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
    return _render(
        request,
        "instances.html",
        {"user": user, "active": "instances", "instances": list_instances(db), "public_host": settings.policy.public_host},
    )


@app.get("/instances/new", response_class=HTMLResponse)
async def instance_new_page(request: Request, db: Database = Depends(get_db), settings=Depends(get_settings), user=Depends(current_user)):
    require_role(user, "operator")
    repo = Path(settings.policy.git_remote_url)
    branches = git_service.list_branches(repo, settings.policy)
    return _render(
        request,
        "instance_form.html",
        {
            "user": user,
            "active": "instances",
            "profiles": list_db_profiles(db),
            "branches": branches or ["main"],
            "port_min": settings.policy.port_min,
            "port_max": settings.policy.port_max,
            "form": {},
        },
    )


@app.post("/instances/new")
async def instance_new_post(
    request: Request,
    name: str = Form(...),
    slug: str = Form(...),
    port: int = Form(...),
    db_profile_id: str = Form(...),
    git_branch: str = Form(...),
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
    if port_taken(db, port):
        raise HTTPException(409, "port taken")
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


@app.get("/instances/{instance_id}", response_class=HTMLResponse)
async def instance_detail(request: Request, instance_id: str, db: Database = Depends(get_db), settings=Depends(get_settings), user=Depends(current_user)):
    inst = get_instance(db, instance_id)
    if not inst:
        raise HTTPException(404)
    message = request.query_params.get("message", "").replace("+", " ")
    with db.connection() as conn:
        jobs = [dict(r) for r in conn.execute(
            "SELECT * FROM deployment_jobs WHERE instance_id=? ORDER BY created_at DESC LIMIT 20",
            (instance_id,),
        ).fetchall()]
    return _render(
        request,
        "instance_detail.html",
        {
            "user": user,
            "active": "instances",
            "instance": inst,
            "jobs": jobs,
            "public_host": settings.policy.public_host,
            "message": message,
        },
    )


@app.post("/instances/{instance_id}/rollback")
async def instance_rollback(
    request: Request,
    instance_id: str,
    csrf_token: str = Form(""),
    db: Database = Depends(get_db),
    settings=Depends(get_settings),
    user=Depends(current_user),
):
    require_role(user, "operator")
    verify_csrf(request, user, csrf_token)
    inst = get_instance(db, instance_id)
    if not inst or not inst.get("rollback_image_tag"):
        raise HTTPException(400, "nothing to rollback")
    name = f"grapes-test-{inst['slug']}"
    docker = DockerService()
    data_dir = settings.instance_ui_data_dir(inst["slug"])
    prof = get_db_profile(db, inst["db_profile_id"], include_secret=True)
    if not prof:
        raise HTTPException(400, "profile missing")
    fernet = get_fernet(settings.fernet_key, settings.secret_key)
    from app.crypto_util import decrypt_secret

    pwd = decrypt_secret(fernet, prof["password_enc"])
    env = {
        "CLICKHOUSE_URL": f"{'https' if prof['tls'] else 'http'}://{prof['host']}:{prof['port']}",
        "CLICKHOUSE_USER": prof["username"],
        "CLICKHOUSE_PASSWORD": pwd,
        "CLICKHOUSE_DATABASE": prof["database_name"],
        "SESSION_COOKIE_SECURE": "false",
    }
    cid = docker.run_instance_container(
        name=name,
        image=inst["rollback_image_tag"],
        port=int(inst["port"]),
        env=env,
        data_mount_host=data_dir,
    )
    ok = docker.wait_health_http(int(inst["port"]), settings.policy.health_timeout_sec, settings.policy.health_interval_sec)
    with db.connection() as conn:
        conn.execute(
            "UPDATE instances SET state=?, container_id=?, image_tag=?, updated_at=? WHERE id=?",
            ("running" if ok else "failed", cid, inst["rollback_image_tag"], utc_now(), instance_id),
        )
    write_audit(db, user_id=user["id"], username=user["username"], role=user["role"], action="instance_rollback", target_type="instance", target_id=instance_id, ip=_client_ip(request), ok=ok)
    return RedirectResponse(f"/instances/{instance_id}", status_code=302)


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
    jid = queue_job(db, instance_id, "build", user["id"])
    write_audit(db, user_id=user["id"], username=user["username"], role=user["role"], action="build_queue", target_type="job", target_id=jid, ip=_client_ip(request))
    return RedirectResponse(f"/instances/{instance_id}?message=build+queued", status_code=302)


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
    jid = queue_job(db, instance_id, "deploy", user["id"])
    write_audit(db, user_id=user["id"], username=user["username"], role=user["role"], action="deploy_queue", target_type="job", target_id=jid, ip=_client_ip(request))
    return RedirectResponse(f"/instances/{instance_id}?message=deploy+queued", status_code=302)


@app.post("/instances/{instance_id}/stop")
async def instance_stop(
    request: Request,
    instance_id: str,
    csrf_token: str = Form(""),
    db: Database = Depends(get_db),
    user=Depends(current_user),
):
    require_role(user, "operator")
    verify_csrf(request, user, csrf_token)
    inst = get_instance(db, instance_id)
    if not inst:
        raise HTTPException(404)
    DockerService().stop_remove(f"grapes-test-{inst['slug']}")
    with db.connection() as conn:
        conn.execute(
            "UPDATE instances SET state='stopped', container_id='', updated_at=? WHERE id=?",
            (utc_now(), instance_id),
        )
    write_audit(db, user_id=user["id"], username=user["username"], role=user["role"], action="instance_stop", target_type="instance", target_id=instance_id, ip=_client_ip(request))
    return RedirectResponse(f"/instances/{instance_id}", status_code=302)


@app.get("/databases", response_class=HTMLResponse)
async def databases_page(request: Request, db: Database = Depends(get_db), user=Depends(current_user)):
    return _render(request, "databases.html", {"user": user, "active": "databases", "profiles": list_db_profiles(db)})


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


@app.get("/builds", response_class=HTMLResponse)
async def builds_page(request: Request, db: Database = Depends(get_db), user=Depends(current_user)):
    return _render(request, "builds.html", {"user": user, "active": "builds", "builds": list_builds(db)})


@app.get("/deployments", response_class=HTMLResponse)
async def deployments_page(request: Request, db: Database = Depends(get_db), user=Depends(current_user)):
    return _render(request, "deployments.html", {"user": user, "active": "deployments", "jobs": list_jobs(db)})


@app.get("/deployments/{job_id}", response_class=HTMLResponse)
async def deployment_detail(request: Request, job_id: str, db: Database = Depends(get_db), user=Depends(current_user)):
    with db.connection() as conn:
        job = conn.execute("SELECT * FROM deployment_jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            raise HTTPException(404)
        logs = [r["line"] for r in conn.execute(
            "SELECT line FROM job_logs WHERE job_id=? ORDER BY id", (job_id,)
        ).fetchall()]
    return _render(request, "job_detail.html", {"user": user, "active": "deployments", "job": dict(job), "logs": logs})


@app.get("/deployments/{job_id}/logs", response_class=HTMLResponse)
async def deployment_logs_partial(request: Request, job_id: str, db: Database = Depends(get_db), user=Depends(current_user)):
    with db.connection() as conn:
        job = conn.execute("SELECT * FROM deployment_jobs WHERE id=?", (job_id,)).fetchone()
        logs = [r["line"] for r in conn.execute(
            "SELECT line FROM job_logs WHERE job_id=? ORDER BY id", (job_id,)
        ).fetchall()]
    return _render(request, "partials/job_logs.html", {"user": user, "job": dict(job), "logs": logs})


@app.get("/audit", response_class=HTMLResponse)
async def audit_page(request: Request, db: Database = Depends(get_db), user=Depends(current_user)):
    return _render(request, "audit.html", {"user": user, "active": "audit", "events": list_audit(db)})


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
