from __future__ import annotations

from pathlib import Path
from typing import Any

from app.config import Settings
from app.crypto_util import decrypt_secret, get_fernet
from app.db import Database
from app.repositories import get_db_profile
from app.services import git_service
from app.services.ui_source import UiSourceError, is_path_source, resolve_path_layout
from app.services.docker_service import DockerService

# Подключение и порт задаёт профиль БД и инстанс, не пример конфига UI.
_PROFILE_ENV_KEYS = frozenset(
    {
        "PORT",
        "CLICKHOUSE_URL",
        "CLICKHOUSE_USER",
        "CLICKHOUSE_PASSWORD",
        "CLICKHOUSE_DATABASE",
        "CLICKHOUSE_READ_USER",
        "CLICKHOUSE_READ_PASSWORD",
        "CLICKHOUSE_WRITE_USER",
        "CLICKHOUSE_WRITE_PASSWORD",
        "SESSION_COOKIE_SECURE",
    }
)


def parse_env_file(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out


def ui_mapping_env(example_path: Path | None) -> dict[str, str]:
    """Колонки, таблицы и прочий конфиг UI из deploy/ui/env.example."""
    if example_path is None or not example_path.is_file():
        return {}
    parsed = parse_env_file(example_path.read_text(encoding="utf-8"))
    return {key: value for key, value in parsed.items() if key not in _PROFILE_ENV_KEYS}


def ui_env_example_path(settings: Settings, inst: dict[str, Any]) -> Path | None:
    """env.example с коммита инстанса; для path — из указанного каталога."""
    if is_path_source(inst):
        try:
            layout = resolve_path_layout(inst)
            return layout.env_example
        except UiSourceError:
            return None
    repo = Path(settings.policy.git_remote_url)
    checkout = repo / "deploy" / "ui" / "env.example"
    try:
        commit = git_service.resolve_instance_commit(repo, inst, settings.policy)
        wt = git_service.ensure_worktree(
            repo,
            settings.git_cache_dir,
            commit,
            ui_context=settings.policy.ui_context,
            ui_dockerfile=settings.policy.ui_dockerfile,
        )
    except git_service.GitError:
        return checkout if checkout.is_file() else None
    path = wt / "deploy" / "ui" / "env.example"
    if path.is_file():
        return path
    return checkout if checkout.is_file() else None


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


def instance_container_env(
    settings: Settings,
    inst: dict[str, Any],
    prof: dict[str, Any],
    *,
    example_path: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    if example_path is None:
        example_path = ui_env_example_path(settings, inst)
    env = ui_mapping_env(example_path)
    env.update(basic_instance_env(settings, inst, prof))
    if extra_env:
        env.update(extra_env)
    return env


def launch_instance_container(
    db: Database,
    settings: Settings,
    docker: DockerService,
    inst: dict[str, Any],
    image: str,
    *,
    extra_env: dict[str, str] | None = None,
    example_path: Path | None = None,
) -> tuple[str, bool]:
    prof = get_db_profile(db, inst["db_profile_id"], include_secret=True)
    if not prof:
        raise RuntimeError("profile missing")
    env = instance_container_env(
        settings,
        inst,
        prof,
        example_path=example_path,
        extra_env=extra_env,
    )
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
