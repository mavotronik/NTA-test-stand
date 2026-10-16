from __future__ import annotations

import fnmatch
import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

MANAGER_KEY = "manager"

_DEFAULT_LOOPBACK_PUBLIC_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def resolve_public_host(configured: str, request_host: str | None) -> str:
    """Хост для ссылок на UI-инстансы (тот же сервер, другой порт).

    Если в конфиге оставлен loopback по умолчанию, подставляется хост из запроса
    (как пользователь открыл панель), иначе — значение public_host из config.yaml.
    """
    configured = configured.strip()
    if configured not in _DEFAULT_LOOPBACK_PUBLIC_HOSTS:
        return configured
    if not request_host:
        return configured
    rh = request_host.strip().lower()
    if rh in _DEFAULT_LOOPBACK_PUBLIC_HOSTS:
        return configured
    return request_host.strip()


@dataclass
class Policy:
    git_remote_url: str
    allowed_branch_patterns: list[str]
    port_min: int
    port_max: int
    storage_root: str
    public_host: str
    clickhouse_cidrs: list[ipaddress._BaseNetwork]
    ch_connect_timeout_sec: int
    build_max_parallel: int
    ui_dockerfile: str
    ui_context: str
    deploy_max_parallel: int
    health_timeout_sec: int
    health_interval_sec: int
    host_metrics_refresh_sec: float
    host_disk_paths: list[str]


def _parse_cidrs(raw: list[str]) -> list[ipaddress._BaseNetwork]:
    out = []
    for item in raw:
        out.append(ipaddress.ip_network(item, strict=False))
    return out


def resolve_config_path(config_dir: Path, raw: str) -> Path:
    path = Path(raw)
    if path.is_absolute():
        return path.resolve()
    return (config_dir / path).resolve()


def load_policy_from_mapping(data: Mapping[str, Any], *, config_dir: Path | None = None) -> Policy:
    base = (config_dir or Path.cwd()).resolve()
    git = data.get("git") or {}
    ports = data.get("ports") or {}
    ch = data.get("clickhouse") or {}
    build = data.get("build") or {}
    deploy = data.get("deploy") or {}
    hm = data.get("host_metrics") or {}
    return Policy(
        git_remote_url=str(git.get("remote_url", "")),
        allowed_branch_patterns=list(git.get("allowed_branch_patterns") or ["main"]),
        port_min=int(ports.get("min", 3100)),
        port_max=int(ports.get("max", 3199)),
        storage_root=str(
            resolve_config_path(base, str(data.get("storage_root", "/var/lib/grapes-test-manager")))
        ),
        public_host=str(data.get("public_host", "127.0.0.1")),
        clickhouse_cidrs=_parse_cidrs(list(ch.get("allowed_host_cidrs") or []))
        if ch.get("allowed_host_cidrs")
        else [],
        ch_connect_timeout_sec=int(ch.get("connect_timeout_sec", 5)),
        build_max_parallel=int(build.get("max_parallel", 2)),
        ui_dockerfile=str(build.get("ui_dockerfile", "deploy/ui/app/Dockerfile")),
        ui_context=str(build.get("ui_context", "deploy/ui/app")),
        deploy_max_parallel=int(deploy.get("max_parallel", 2)),
        health_timeout_sec=int(deploy.get("health_timeout_sec", 120)),
        health_interval_sec=int(deploy.get("health_interval_sec", 3)),
        host_metrics_refresh_sec=float(hm.get("refresh_sec", 5)),
        host_disk_paths=list(hm.get("disk_paths") or ["/"]),
    )


def load_policy(path: Path) -> Policy:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return load_policy_from_mapping(data)


def branch_allowed(policy: Policy, branch: str) -> bool:
    branch = branch.strip()
    if not branch:
        return False
    for pat in policy.allowed_branch_patterns:
        if fnmatch.fnmatch(branch, pat):
            return True
    return False


def host_allowed(policy: Policy, host: str) -> bool:
    if not policy.clickhouse_cidrs:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in net for net in policy.clickhouse_cidrs)


@dataclass
class Settings:
    host: str
    port: int
    config_file: Path
    data_dir: Path
    secret_key: str
    fernet_key: bytes | None
    session_ttl_hours: int
    host_proc: Path
    bootstrap_admin_user: str
    bootstrap_admin_password: str
    skip_worker: bool
    policy: Policy = field(repr=False)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "manager.sqlite"

    @property
    def git_cache_dir(self) -> Path:
        return self.data_dir / "git-cache"

    @property
    def instances_dir(self) -> Path:
        return Path(self.policy.storage_root) / "instances"

    def instance_ui_data_dir(self, slug: str) -> Path:
        return self.instances_dir / slug / "data"

    # Совместимость со старым именем поля
    @property
    def policy_file(self) -> Path:
        return self.config_file


def config_file_path() -> Path:
    return Path(os.environ.get("CONFIG_FILE", "config.yaml"))


def load_config_mapping(path: Path | None = None) -> dict[str, Any]:
    cfg_path = path or config_file_path()
    if not cfg_path.exists():
        raise FileNotFoundError(f"config not found: {cfg_path}")
    text = cfg_path.read_text(encoding="utf-8")
    if text.count(f"{MANAGER_KEY}:") > 1:
        raise ValueError(
            f"{cfg_path}: несколько секций '{MANAGER_KEY}:' — YAML оставит только последнюю; "
            f"объедините в один блок (иначе пропадут bootstrap_admin_password и secret_key)"
        )
    return yaml.safe_load(text) or {}


def load_settings(path: Path | None = None) -> Settings:
    cfg_path = path or config_file_path()
    data = load_config_mapping(cfg_path)
    mgr = data.get(MANAGER_KEY) or {}
    cfg_dir = cfg_path.parent.resolve()
    policy = load_policy_from_mapping(data, config_dir=cfg_dir)
    data_dir = resolve_config_path(cfg_dir, str(mgr.get("data_dir", "./data")))
    data_dir.mkdir(parents=True, exist_ok=True)
    fernet_raw = str(mgr.get("fernet_key", "") or "").strip()
    fernet_key = fernet_raw.encode("utf-8") if fernet_raw else None
    return Settings(
        host=str(mgr.get("host", "127.0.0.1")),
        port=int(mgr.get("port", 8080)),
        config_file=cfg_path.resolve(),
        data_dir=data_dir,
        secret_key=str(mgr.get("secret_key", "dev-insecure-change-me")),
        fernet_key=fernet_key,
        session_ttl_hours=int(mgr.get("session_ttl_hours", 24)),
        host_proc=Path(str(mgr.get("host_proc", "/host/proc"))),
        bootstrap_admin_user=str(mgr.get("bootstrap_admin_user", "admin")),
        bootstrap_admin_password=str(mgr.get("bootstrap_admin_password", "") or ""),
        skip_worker=bool(mgr.get("skip_worker", False)),
        policy=policy,
    )
