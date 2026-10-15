from __future__ import annotations

from typing import Any

from app.config import Settings
from app.crypto_util import decrypt_secret, get_fernet
from app.db import Database
from app.repositories import get_db_profile
from app.services.docker_service import DockerService


def basic_instance_env(settings: Settings, inst: dict[str, Any], prof: dict[str, Any]) -> dict[str, str]:
    fernet = get_fernet(settings.fernet_key, settings.secret_key)
    pwd = decrypt_secret(fernet, prof["password_enc"])
    return {
        "CLICKHOUSE_URL": f"{'https' if prof['tls'] else 'http'}://{prof['host']}:{prof['port']}",
        "CLICKHOUSE_USER": prof["username"],
        "CLICKHOUSE_PASSWORD": pwd,
        "CLICKHOUSE_DATABASE": prof["database_name"],
        "SESSION_COOKIE_SECURE": "false",
    }


def launch_instance_container(
    db: Database,
    settings: Settings,
    docker: DockerService,
    inst: dict[str, Any],
    image: str,
    *,
    extra_env: dict[str, str] | None = None,
) -> tuple[str, bool]:
    prof = get_db_profile(db, inst["db_profile_id"], include_secret=True)
    if not prof:
        raise RuntimeError("profile missing")
    env = basic_instance_env(settings, inst, prof)
    if extra_env:
        env.update(extra_env)
    name = f"grapes-test-{inst['slug']}"
    data_dir = settings.instance_ui_data_dir(inst["slug"])
    cid = docker.run_instance_container(
        name=name,
        image=image,
        port=int(inst["port"]),
        env=env,
        data_mount_host=data_dir,
    )
    ok = docker.wait_health_http(
        int(inst["port"]),
        settings.policy.health_timeout_sec,
        settings.policy.health_interval_sec,
    )
    return cid, ok
