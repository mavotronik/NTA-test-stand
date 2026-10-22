from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.db import utc_now
from app.services import git_service
from app.services.docker_service import (
    UI_APP_GID,
    UI_APP_UID,
    ComponentVersionRow,
    DockerService,
    prepare_ui_data_mount,
)
from app.services.ui_source import (
    UiSourceError,
    build_key_for_path_layout,
    is_path_source,
    path_version_id,
    resolve_path_layout,
)

COMPONENT_VERSIONS_FILENAME = "component-versions.tsv"


@dataclass(frozen=True)
class UiVersionMeta:
    grapes_version: str
    grapes_version_date: str
    git_commit: str


def instance_version_path_env() -> dict[str, str]:
    return {
        "UI_DATA_DIR": "/app/server/data",
        "COMPONENT_VERSIONS_FILE": "/app/server/data/component-versions.tsv",
    }


def parse_component_versions_tsv(text: str) -> dict[str, ComponentVersionRow]:
    out: dict[str, ComponentVersionRow] = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        name = parts[0].strip()
        if not name:
            continue
        out[name] = ComponentVersionRow(
            version=parts[1].strip(),
            version_date=parts[2].strip(),
            deployed_at=parts[3].strip(),
        )
    return out


def format_component_versions_tsv(rows: dict[str, ComponentVersionRow]) -> str:
    lines: list[str] = []
    for name in sorted(rows.keys()):
        row = rows[name]
        lines.append(f"{name}\t{row.version}\t{row.version_date}\t{row.deployed_at}")
    return "\n".join(lines) + ("\n" if lines else "")


def _deployed_at_z() -> str:
    return utc_now().replace("+00:00", "Z")


def _write_component_versions_file(data_dir: Path, merged: dict[str, ComponentVersionRow]) -> None:
    mount_dir = prepare_ui_data_mount(data_dir)
    path = mount_dir / COMPONENT_VERSIONS_FILENAME
    tmp = path.with_name(f"{COMPONENT_VERSIONS_FILENAME}.tmp")
    tmp.write_text(format_component_versions_tsv(merged), encoding="utf-8")
    try:
        tmp.chmod(0o644)
        os.chown(tmp, UI_APP_UID, UI_APP_GID)
    except OSError:
        pass
    tmp.replace(path)
    try:
        os.chown(path, UI_APP_UID, UI_APP_GID)
    except OSError:
        pass


def schema_version_for_tree(settings: Settings, repo_root: Path) -> tuple[str, str]:
    repo = Path(settings.policy.git_remote_url)
    version = git_service.component_version(repo, "schema", tree=repo_root.resolve())
    version_date = git_service.version_date_for_component_version(repo, version)
    return version, version_date


def record_schema_component_version(
    data_dir: Path,
    *,
    schema_version: str,
    schema_version_date: str,
) -> None:
    """Строка schema в component-versions.tsv (как note_component_version schema в deploy.sh)."""
    mount_dir = prepare_ui_data_mount(data_dir)
    path = mount_dir / COMPONENT_VERSIONS_FILENAME
    existing = parse_component_versions_tsv(path.read_text(encoding="utf-8")) if path.is_file() else {}
    merged = dict(existing)
    merged["schema"] = ComponentVersionRow(
        version=schema_version,
        version_date=schema_version_date,
        deployed_at=_deployed_at_z(),
    )
    _write_component_versions_file(data_dir, merged)


def write_instance_component_versions(
    data_dir: Path,
    docker: DockerService,
    *,
    ui_version: str,
    ui_version_date: str,
    deployed_at: str | None = None,
) -> None:
    """component-versions.tsv на data-mount инстанса (как deploy.sh → UI_DATA_DIR)."""
    mount_dir = prepare_ui_data_mount(data_dir)
    path = mount_dir / COMPONENT_VERSIONS_FILENAME
    existing = parse_component_versions_tsv(path.read_text(encoding="utf-8")) if path.is_file() else {}
    merged = dict(existing)
    for component_id, row in docker.read_host_component_versions().items():
        merged[component_id] = row
    merged["ui"] = ComponentVersionRow(
        version=ui_version,
        version_date=ui_version_date,
        deployed_at=deployed_at or _deployed_at_z(),
    )
    _write_component_versions_file(data_dir, merged)


def resolve_instance_ui_version_meta(settings: Settings, inst: dict) -> UiVersionMeta:
    if is_path_source(inst):
        try:
            layout = resolve_path_layout(inst)
            vid = path_version_id(build_key_for_path_layout(layout))
        except UiSourceError:
            vid = "unknown"
        return UiVersionMeta(
            grapes_version=vid,
            grapes_version_date="",
            git_commit=f"path:{vid}",
        )

    repo = Path(settings.policy.git_remote_url)
    tag_pin = (inst.get("git_tag") or "").strip()
    try:
        commit = git_service.resolve_instance_commit(repo, inst, settings.policy)
    except git_service.GitError:
        commit = ""

    if tag_pin:
        grapes_version = tag_pin
        grapes_version_date = git_service.version_date_for_component_version(repo, tag_pin)
        return UiVersionMeta(
            grapes_version=grapes_version,
            grapes_version_date=grapes_version_date,
            git_commit=commit,
        )

    grapes_version = commit[:12] if commit else "ui-unknown"
    grapes_version_date = ""
    if commit:
        try:
            wt = git_service.ensure_worktree(
                repo,
                settings.git_cache_dir,
                commit,
                ui_context=settings.policy.ui_context,
                ui_dockerfile=settings.policy.ui_dockerfile,
            )
            grapes_version = git_service.component_version(repo, "ui", tree=wt)
        except git_service.GitError:
            pass
        grapes_version_date = git_service.version_date_for_component_version(repo, grapes_version)
        if not grapes_version_date:
            grapes_version_date = git_service.commit_iso_date(repo, commit)

    return UiVersionMeta(
        grapes_version=grapes_version,
        grapes_version_date=grapes_version_date,
        git_commit=commit,
    )


def build_instance_version_extra_env(settings: Settings, inst: dict) -> dict[str, str]:
    meta = resolve_instance_ui_version_meta(settings, inst)
    env = instance_version_path_env()
    env["GRAPES_VERSION"] = meta.grapes_version
    if meta.grapes_version_date:
        env["GRAPES_VERSION_DATE"] = meta.grapes_version_date
    env["GIT_COMMIT"] = meta.git_commit
    return env
