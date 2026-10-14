from __future__ import annotations

import io
import tarfile
import time
from pathlib import Path
from typing import Any, Iterable

import docker
from docker.errors import NotFound
from docker.models.containers import Container


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
        data_mount_host.mkdir(parents=True, exist_ok=True)
        mount_src = str(data_mount_host.resolve())
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
