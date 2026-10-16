from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
import time
from pathlib import Path
from typing import Any, Callable, Iterable

import docker
import yaml
from docker.errors import NotFound
from docker.models.containers import Container

# uid/gid пользователя app в образе deploy/ui/app/Dockerfile (как в deploy/deploy.sh ui_up)
UI_APP_UID = 1001
UI_APP_GID = 1001

COMPOSE_UI_BUILD_MAX_ATTEMPTS = 3
COMPOSE_UI_BUILD_RETRY_DELAY_SEC = 5


def ui_compose_build_uses_host_network(compose_file: Path, service: str = "grapes-nta") -> bool:
    """True, если build сервиса просит сеть хоста (Buildx требует --allow=network.host)."""
    data = yaml.safe_load(compose_file.read_text(encoding="utf-8")) or {}
    services = data.get("services") or {}
    spec = services.get(service) or {}
    if not isinstance(spec, dict):
        return False
    build = spec.get("build")
    if not isinstance(build, dict):
        return False
    return str(build.get("network") or "") == "host"


def ui_image_build_command(ui_dir: Path, *, build_date: str, service: str = "grapes-nta") -> list[str]:
    """Команда сборки образа UI.

    `docker compose build` не умеет выдать entitlement network.host. Если compose
    просит build.network: host, собираем тем же context/Dockerfile через buildx
    с --allow=network.host. Иначе оставляем compose build.
    """
    ui_dir = ui_dir.resolve()
    compose_file = ui_dir / "docker-compose.yml"
    if not ui_compose_build_uses_host_network(compose_file, service):
        return [
            "docker",
            "compose",
            "-f",
            str(compose_file),
            "build",
            "--build-arg",
            f"BUILD_DATE={build_date}",
            service,
        ]

    data = yaml.safe_load(compose_file.read_text(encoding="utf-8")) or {}
    spec = ((data.get("services") or {}).get(service) or {})
    build = spec.get("build") if isinstance(spec, dict) else {}
    if not isinstance(build, dict):
        build = {}
    context = (ui_dir / str(build.get("context") or ".")).resolve()
    dockerfile = str(build.get("dockerfile") or "Dockerfile")
    df_path = Path(dockerfile)
    if not df_path.is_absolute():
        df_path = context / df_path
    image = "grapes-nta:latest"
    if isinstance(spec, dict) and spec.get("image"):
        image = str(spec["image"])
    return [
        "docker",
        "buildx",
        "build",
        "--allow=network.host",
        "--network=host",
        "--progress=plain",
        "--load",
        "--build-arg",
        f"BUILD_DATE={build_date}",
        "-f",
        str(df_path),
        "-t",
        image,
        str(context),
    ]


def _stream_command_output(
    cmd: list[str],
    *,
    cwd: str,
    log_callback: Callable[[str], None] | None,
) -> tuple[int, str]:
    """Читает stdout/stderr построчно, чтобы лог сборки появлялся до её конца.

    Buildx без --progress=plain пишет кадры на TTY и в пайп почти ничего не отдаёт.
    """
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
    )
    lines: list[str] = []
    assert proc.stdout is not None
    finished = False
    try:
        for raw in proc.stdout:
            for part in raw.replace("\r", "\n").split("\n"):
                line = part.rstrip()
                if not line:
                    continue
                lines.append(line)
                if log_callback:
                    log_callback(line)
        finished = True
    finally:
        if not finished and proc.poll() is None:
            proc.kill()
        returncode = proc.wait()
        proc.stdout.close()
    return returncode, "\n".join(lines)


def compose_build_apk_dns_retryable(output: str) -> bool:
    """Кратковременный сбой apk в Alpine build (индекс не скачался из‑за DNS)."""
    text = output
    if "DNS: transient error" in text:
        return True
    if "unable to select packages" in text and "no such package" in text:
        return True
    return False


def prepare_ui_data_mount(host_dir: Path) -> Path:
    """Каталог на хосте для bind-mount в /app/server/data (sessions.db и пр.)."""
    host_dir = host_dir.resolve()
    host_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chown(host_dir, UI_APP_UID, UI_APP_GID)
    except OSError:
        # Менеджер без root (локальный запуск) не может chown → UI (uid 1001) пишет в mount.
        try:
            host_dir.chmod(0o777)
        except OSError:
            pass
    return host_dir


def _read_dockerignore(context: Path) -> set[str]:
    ignore_file = context / ".dockerignore"
    if not ignore_file.is_file():
        return set()
    patterns: set[str] = set()
    for line in ignore_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        patterns.add(line.rstrip("/"))
    return patterns


def _ignored(rel: str, patterns: set[str]) -> bool:
    parts = Path(rel).parts
    for pat in patterns:
        if pat.startswith("**"):
            if Path(rel).match(pat):
                return True
            continue
        if rel == pat or rel.startswith(pat + "/"):
            return True
        if pat in parts:
            return True
    return False


def _iter_build_files(context: Path) -> Iterable[Path]:
    context = context.resolve()
    patterns = _read_dockerignore(context)
    for path in context.rglob("*"):
        rel = path.relative_to(context).as_posix()
        if not rel:
            continue
        if _ignored(rel, patterns):
            continue
        if path.is_file():
            yield path


def _tar_build_context(context: Path) -> io.BytesIO:
    context = context.resolve()
    if not context.is_dir():
        raise RuntimeError(f"build context is not a directory: {context}")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for file_path in _iter_build_files(context):
            arcname = file_path.relative_to(context).as_posix()
            tar.add(file_path, arcname=arcname)
    buf.seek(0)
    return buf


class DockerService:
    def __init__(self) -> None:
        self._client = None

    @staticmethod
    def can_compose_ui_build(ui_dir: Path) -> bool:
        ui_dir = ui_dir.resolve()
        if not (ui_dir / "docker-compose.yml").is_file():
            return False
        if not (ui_dir / "app" / "Dockerfile").is_file():
            return False
        if not shutil.which("docker"):
            return False
        proc = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode == 0

    def build_ui_image_compose(
        self,
        ui_dir: Path,
        tag: str,
        *,
        build_date: str,
        log_callback: Callable[[str], None] | None = None,
    ) -> tuple[str, str]:
        """Сборка как ./deploy/deploy.sh ui: compose context ./app, тот же Dockerfile."""
        ui_dir = ui_dir.resolve()
        compose_file = ui_dir / "docker-compose.yml"
        if not compose_file.is_file():
            raise RuntimeError(f"missing {compose_file}")

        cmd = ui_image_build_command(ui_dir, build_date=build_date)
        if log_callback:
            log_callback(" ".join(cmd))
        returncode = 1
        combined = ""
        for attempt in range(1, COMPOSE_UI_BUILD_MAX_ATTEMPTS + 1):
            returncode, combined = _stream_command_output(
                cmd,
                cwd=str(ui_dir),
                log_callback=log_callback,
            )
            if returncode == 0:
                break
            if (
                attempt < COMPOSE_UI_BUILD_MAX_ATTEMPTS
                and compose_build_apk_dns_retryable(combined)
            ):
                if log_callback:
                    log_callback(
                        "docker compose build: apk/DNS transient error, "
                        f"retry {attempt + 1}/{COMPOSE_UI_BUILD_MAX_ATTEMPTS} "
                        f"in {COMPOSE_UI_BUILD_RETRY_DELAY_SEC}s"
                    )
                time.sleep(COMPOSE_UI_BUILD_RETRY_DELAY_SEC)
                continue
            msg = combined.strip() or "docker compose build failed"
            raise RuntimeError(msg)
        if returncode != 0:
            msg = combined.strip() or "docker compose build failed"
            raise RuntimeError(msg)

        base_tag = "grapes-nta:latest"
        if "-t" in cmd:
            base_tag = cmd[cmd.index("-t") + 1]
        tag_proc = subprocess.run(
            ["docker", "tag", base_tag, tag],
            capture_output=True,
            text=True,
            check=False,
        )
        if tag_proc.returncode != 0:
            msg = tag_proc.stderr.strip() or f"docker tag {base_tag} {tag} failed"
            raise RuntimeError(msg)

        img = self.client.images.get(tag)
        return tag, img.id or ""

    @property
    def client(self):
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    def build_image(
        self,
        context: Path,
        dockerfile: str,
        tag: str,
        buildargs: dict[str, str] | None = None,
        log_callback: Any = None,
    ) -> tuple[str, str]:
        context = context.resolve()
        if not context.is_dir():
            raise RuntimeError(f"build context not found: {context}")

        df_path = context / dockerfile if not Path(dockerfile).is_absolute() else Path(dockerfile)
        if not df_path.is_file():
            raise RuntimeError(f"Dockerfile not found: {df_path}")
        df_name = dockerfile if not Path(dockerfile).is_absolute() else df_path.relative_to(context).as_posix()

        # Контекст передаём tar-архивом: при docker.sock daemon на хосте не видит
        # пути внутри контейнера менеджера (/data/git-cache/...).
        tar_ctx = _tar_build_context(context)
        stream = self.client.api.build(
            fileobj=tar_ctx,
            custom_context=True,
            dockerfile=df_name,
            tag=tag,
            buildargs=buildargs or {},
            decode=True,
        )
        last_id = ""
        for chunk in stream:
            if "stream" in chunk and log_callback:
                log_callback(chunk["stream"].rstrip())
            if "error" in chunk:
                raise RuntimeError(chunk["error"])
            if "aux" in chunk and "ID" in chunk["aux"]:
                last_id = chunk["aux"]["ID"]
        img = self.client.images.get(tag)
        return tag, img.id or last_id

    def run_instance_container(
        self,
        *,
        name: str,
        image: str,
        port: int,
        env: dict[str, str],
        data_mount_host: Path,
        remove_old: bool = True,
    ) -> str:
        if remove_old:
            self.stop_remove(name)
        mount_src = str(prepare_ui_data_mount(data_mount_host))
        container = self.client.containers.run(
            image=image,
            name=name,
            detach=True,
            network_mode="host",
            environment={**env, "PORT": str(port)},
            volumes={
                mount_src: {"bind": "/app/server/data", "mode": "rw"},
            },
            restart_policy={"Name": "unless-stopped"},
        )
        return container.id

    def stop_remove(self, name: str) -> None:
        try:
            c = self.client.containers.get(name)
            c.stop(timeout=15)
            c.remove(force=True)
        except NotFound:
            pass

    def get_container(self, name: str) -> Container | None:
        try:
            return self.client.containers.get(name)
        except NotFound:
            return None

    def container_status(self, name: str) -> str:
        c = self.get_container(name)
        if not c:
            return "missing"
        c.reload()
        return c.status

    def wait_health_http(self, port: int, timeout_sec: int, interval_sec: int) -> bool:
        import urllib.error
        import urllib.request

        deadline = time.time() + timeout_sec
        url = f"http://127.0.0.1:{port}/api/health"
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=3) as resp:
                    if resp.status == 200:
                        return True
            except (urllib.error.URLError, TimeoutError):
                pass
            time.sleep(interval_sec)
        return False

    def list_grapes_test_containers(self) -> list[dict[str, Any]]:
        out = []
        for c in self.client.containers.list(all=True):
            if c.name.startswith("grapes-test-"):
                c.reload()
                out.append({
                    "name": c.name,
                    "id": c.short_id,
                    "status": c.status,
                    "image": (c.image.tags[0] if c.image.tags else c.image.short_id),
                })
        return sorted(out, key=lambda x: x["name"])
