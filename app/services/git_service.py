from __future__ import annotations

import fnmatch
import hashlib
import shutil
import subprocess
from pathlib import Path

from app.config import Policy, branch_allowed


class GitError(RuntimeError):
    pass


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        msg = proc.stderr.strip() or proc.stdout.strip() or "git failed"
        raise GitError(msg)
    return proc.stdout.strip()


def list_branches(repo: Path, policy: Policy) -> list[str]:
    if not repo.exists():
        raise GitError(f"repo not found: {repo}")
    try:
        _run(["git", "-C", str(repo), "fetch", "--prune"], cwd=None)
    except GitError:
        pass
    branches: list[str] = []
    for fmt_cmd in (
        ["git", "-C", str(repo), "branch", "-r", "--format=%(refname:short)"],
        ["git", "-C", str(repo), "branch", "--format=%(refname:short)"],
    ):
        try:
            out = _run(fmt_cmd)
        except GitError:
            continue
        for line in out.splitlines():
            line = line.strip()
            if not line or line.endswith("/HEAD"):
                continue
            if line.startswith("origin/"):
                line = line[len("origin/") :]
            if branch_allowed(policy, line):
                branches.append(line)
    return sorted(set(branches))


def resolve_commit(repo: Path, branch: str, policy: Policy) -> str:
    if not branch_allowed(policy, branch):
        raise GitError(f"branch not allowed: {branch}")
    ref = branch if "/" in branch else f"origin/{branch}"
    try:
        sha = _run(["git", "-C", str(repo), "rev-parse", ref])
    except GitError:
        sha = _run(["git", "-C", str(repo), "rev-parse", branch])
    if len(sha) != 40:
        raise GitError(f"invalid commit: {sha}")
    return sha


def _same_commit(repo: Path, left: str, right: str) -> bool:
    try:
        a = _run(["git", "-C", str(repo), "rev-parse", left])
        b = _run(["git", "-C", str(repo), "rev-parse", right])
        return a == b
    except GitError:
        return False


def worktree_path(cache_dir: Path, commit: str) -> Path:
    return (cache_dir / "worktrees" / commit[:12]).resolve()


def _worktree_head(repo: Path, wt: Path) -> str | None:
    wt = wt.resolve()
    try:
        out = _run(["git", "-C", str(repo), "worktree", "list", "--porcelain"])
    except GitError:
        return None
    current_path: str | None = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            current_path = Path(line.split(" ", 1)[1]).resolve().as_posix()
        elif line.startswith("HEAD ") and current_path == wt.as_posix():
            return line.split(" ", 1)[1]
    return None


def _cleanup_stale_worktree(repo: Path, wt: Path) -> None:
    wt = wt.resolve()
    try:
        _run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)])
    except GitError:
        pass
    if wt.exists():
        shutil.rmtree(wt, ignore_errors=False)
    try:
        _run(["git", "-C", str(repo), "worktree", "prune"])
    except GitError:
        pass


def ensure_worktree(
    repo: Path,
    cache_dir: Path,
    commit: str,
    *,
    ui_context: str = "deploy/ui/app",
    ui_dockerfile: str = "deploy/ui/app/Dockerfile",
) -> Path:
    repo = repo.resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    wt = worktree_path(cache_dir, commit)
    marker = wt / ui_dockerfile
    ctx_dir = wt / ui_context

    if marker.is_file() and ctx_dir.is_dir():
        return wt

    registered_head = _worktree_head(repo, wt)
    if registered_head and _same_commit(repo, registered_head, commit) and marker.is_file():
        return wt

    if wt.exists() or registered_head:
        _cleanup_stale_worktree(repo, wt)

    wt.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "-C", str(repo), "worktree", "add", "--detach", str(wt), commit])

    if not marker.is_file():
        _cleanup_stale_worktree(repo, wt)
        raise GitError(f"worktree at {wt} has no {ui_dockerfile} at commit {commit[:12]}")
    if not ctx_dir.is_dir():
        _cleanup_stale_worktree(repo, wt)
        raise GitError(f"worktree at {wt} has no {ui_context} at commit {commit[:12]}")

    return wt


def build_key_for_commit(
    commit: str,
    dockerfile: Path,
    context_dir: Path,
    extra: str = "",
) -> str:
    parts = [commit]
    if dockerfile.exists():
        parts.append(hashlib.sha256(dockerfile.read_bytes()).hexdigest()[:16])
    lock = context_dir / "package-lock.json"
    if lock.exists():
        parts.append(hashlib.sha256(lock.read_bytes()).hexdigest()[:16])
    parts.append(extra)
    return hashlib.sha256("|".join(parts).encode()).hexdigest()
