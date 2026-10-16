from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_STAMP_COMMIT = REPO_ROOT / ".panel_commit"
_STAMP_BUILT = REPO_ROOT / ".panel_build_time"
_STAMP_REPO = REPO_ROOT / ".panel_repo_url"


@dataclass(frozen=True)
class PanelBuild:
    commit: str
    commit_short: str
    built_at: str
    built_at_iso: str
    repo_url: str
    repo_name: str


def format_built_at(raw: str) -> str:
    text = (raw or "").strip()
    if not text or text == "unknown":
        return "—"
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.astimezone().strftime("%d.%m.%Y %H:%M")


def browse_repo_url(remote: str) -> str:
    """Turn a git remote into an https page, or "" when it is not a web URL."""
    text = (remote or "").strip()
    if not text or text == "unknown":
        return ""
    if text.startswith("git@"):
        host, _, path = text[4:].partition(":")
        if not host or not path:
            return ""
        text = f"https://{host}/{path}"
    elif text.startswith("ssh://"):
        rest = text[len("ssh://"):]
        if rest.startswith("git@"):
            rest = rest[4:]
        text = "https://" + rest
    elif not text.startswith(("http://", "https://")):
        return ""
    if text.endswith(".git"):
        text = text[:-4]
    return text.rstrip("/")


def repo_name_from_url(url: str) -> str:
    name = url.rstrip("/").rsplit("/", 1)[-1]
    return name or url


def _read_stamp(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _git(*args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-c", "safe.directory=*", "-C", str(REPO_ROOT), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


@lru_cache(maxsize=1)
def panel_build_info() -> PanelBuild:
    """Commit and build time of this panel process.

    Docker images stamp both at image build. A checkout started with
    ``python -m app`` falls back to ``HEAD`` and that commit's time.
    """
    commit = (
        os.environ.get("PANEL_GIT_COMMIT", "").strip()
        or _read_stamp(_STAMP_COMMIT)
        or _git("rev-parse", "HEAD")
    )
    if commit == "unknown":
        commit = ""
    built_raw = (
        os.environ.get("PANEL_BUILD_TIME", "").strip()
        or _read_stamp(_STAMP_BUILT)
        or _git("log", "-1", "--format=%cI")
    )
    repo_url = browse_repo_url(
        os.environ.get("PANEL_REPO_URL", "").strip()
        or _read_stamp(_STAMP_REPO)
        or _git("remote", "get-url", "origin")
    )
    return PanelBuild(
        commit=commit,
        commit_short=commit[:12] if commit else "—",
        built_at=format_built_at(built_raw),
        built_at_iso=built_raw if built_raw and built_raw != "unknown" else "",
        repo_url=repo_url,
        repo_name=repo_name_from_url(repo_url) if repo_url else "",
    )
