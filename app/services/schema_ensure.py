from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.config import Settings
from app.crypto_util import decrypt_secret, get_fernet

LogFn = Callable[[str], None]

_ENSURE_SCRIPT = Path("deploy/schema/ensure-live.sh")
_ENSURE_LIST = Path("deploy/schema/ensure.list")
_DEFAULT_TIMEOUT_SEC = 600


class SchemaEnsureError(RuntimeError):
    pass


def repo_root_from_compose_dir(compose_dir: Path) -> Path:
    """Корень GrapesNTA: родитель deploy/ui."""
    return compose_dir.resolve().parent.parent


def schema_ensure_paths(repo_root: Path) -> tuple[Path, Path]:
    root = repo_root.resolve()
    return root / _ENSURE_SCRIPT, root / _ENSURE_LIST


def schema_ensure_available(repo_root: Path) -> bool:
    script, list_file = schema_ensure_paths(repo_root)
    return script.is_file() and list_file.is_file()


def clickhouse_ensure_env(settings: Settings, prof: dict[str, Any]) -> dict[str, str]:
    fernet = get_fernet(settings.fernet_key, settings.secret_key)
    password = decrypt_secret(fernet, prof["password_enc"])
    username = (prof["username"] or "").strip()
    host = (prof["host"] or "").strip()
    port = int(prof["port"])
    scheme = "https" if prof.get("tls") else "http"
    ch_url = f"{scheme}://{host}:{port}"
    return {
        "CH_URL": ch_url,
        "CH_USER": username,
        "CH_PASS": password,
        "GRANT_USER": username,
        "GRANT_PASS": password,
        "CLICKHOUSE_USER": username,
        "CLICKHOUSE_PASSWORD": password,
        "CLICKHOUSE_URL": ch_url,
    }


def run_schema_ensure(
    repo_root: Path,
    settings: Settings,
    prof: dict[str, Any],
    *,
    log: LogFn | None = None,
    timeout_sec: int = _DEFAULT_TIMEOUT_SEC,
) -> bool:
    """
    Применить ensure.list через ensure-live.sh.

    Returns True если скрипт выполнен успешно, False если в дереве нет schema ensure.
    Raises SchemaEnsureError при ненулевом коде выхода.
    """
    script, list_file = schema_ensure_paths(repo_root)
    if not script.is_file() or not list_file.is_file():
        return False

    def _log(line: str) -> None:
        if log and line:
            log(line.rstrip("\n"))

    ch_env = clickhouse_ensure_env(settings, prof)
    env = os.environ.copy()
    env.update(ch_env)

    _log(f"ensure ClickHouse schema ({list_file.name} from {repo_root})")
    proc = subprocess.Popen(
        ["bash", str(script.resolve())],
        cwd=str(repo_root.resolve()),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            _log(line)
        rc = proc.wait(timeout=max(1, timeout_sec))
    except subprocess.TimeoutExpired as err:
        proc.kill()
        proc.wait()
        raise SchemaEnsureError(f"schema ensure timed out after {timeout_sec}s") from err

    if rc != 0:
        raise SchemaEnsureError(f"schema ensure failed (exit {rc})")
    return True
