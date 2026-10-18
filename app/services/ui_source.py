from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from app.services import git_service


class UiSourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class UiBuildLayout:
    """Каталог UI (deploy/ui): compose и env.example; контекст сборки — app/."""

    ui_dir: Path
    context: Path
    dockerfile: Path
    env_example: Path | None

    @property
    def compose_dir(self) -> Path:
        return self.ui_dir


def instance_source_mode(inst: dict) -> str:
    mode = (inst.get("source_mode") or "repo").strip().lower()
    return mode if mode in ("repo", "path") else "repo"


def is_path_source(inst: dict) -> bool:
    return instance_source_mode(inst) == "path"


def normalize_source_path(raw: str) -> Path:
    text = (raw or "").strip()
    if not text:
        raise UiSourceError("укажите абсолютный путь к каталогу GrapesNTA")
    path = Path(text)
    if not path.is_absolute():
        raise UiSourceError("путь должен быть абсолютным, например /home/tester/GrapesNTA")
    return path.resolve()


def _layout_from_ui_dir(ui_dir: Path) -> UiBuildLayout | None:
    ui_dir = ui_dir.resolve()
    dockerfile = ui_dir / "app" / "Dockerfile"
    context = ui_dir / "app"
    if not dockerfile.is_file() or not context.is_dir():
        return None
    env_example = ui_dir / "env.example"
    return UiBuildLayout(
        ui_dir=ui_dir,
        context=context,
        dockerfile=dockerfile,
        env_example=env_example if env_example.is_file() else None,
    )


def find_ui_layout(root: Path) -> UiBuildLayout:
    root = root.resolve()
    if not root.is_dir():
        raise UiSourceError(f"каталог не найден: {root}")
    for candidate in (
        root / "deploy" / "ui",
        root / "ui",
        root,
    ):
        layout = _layout_from_ui_dir(candidate)
        if layout:
            return layout
    found = _find_ui_deep(root, max_depth=4)
    if found:
        return found
    raise UiSourceError(
        f"не найден каталог UI (ожидается app/Dockerfile) под {root}"
    )


def _find_ui_deep(root: Path, *, max_depth: int) -> UiBuildLayout | None:
    skip_names = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv"})
    queue: list[tuple[Path, int]] = [(root, 0)]
    while queue:
        current, depth = queue.pop(0)
        if depth > 0:
            layout = _layout_from_ui_dir(current)
            if layout:
                return layout
        if depth >= max_depth:
            continue
        try:
            children = sorted(current.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for child in children:
            if not child.is_dir():
                continue
            if child.name in skip_names or child.name.startswith("."):
                continue
            queue.append((child, depth + 1))
    return None


def resolve_path_layout(inst: dict) -> UiBuildLayout:
    root = normalize_source_path(inst.get("source_path") or "")
    return find_ui_layout(root)


def build_key_for_path_layout(layout: UiBuildLayout) -> str:
    return git_service.build_key_for_commit(
        layout.ui_dir.as_posix(),
        layout.dockerfile,
        layout.context,
        extra="path",
    )


def path_build_tag(build_key: str) -> str:
    return f"grapes-nta-test:path-{build_key[:12]}"


def path_version_id(build_key: str) -> str:
    return build_key[:12]
