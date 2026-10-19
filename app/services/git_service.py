from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

from app.config import Policy, branch_allowed, tag_allowed


class GitError(RuntimeError):
    pass


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if proc.returncode != 0:
        msg = proc.stderr.strip() or proc.stdout.strip() or "git failed"
        raise GitError(msg)
    return proc.stdout.strip()


def _upstream_ref(repo: Path, branch: str) -> str:
    try:
        return _run(
            ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"]
        )
    except GitError:
        candidate = f"origin/{branch}"
        try:
            _run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", candidate])
        except GitError as err:
            raise GitError(f"нет upstream для ветки {branch}") from err
        return candidate


def pull_repo(repo: Path, *, force: bool = False) -> str:
    """Обновить checkout GrapesNTA.

    Без force — ``git pull --ff-only``. С force текущая ветка сбрасывается на upstream:
    локальные коммиты и правки отслеживаемых файлов пропадают.
    """
    repo = repo.resolve()
    if not repo.exists():
        raise GitError(f"repo not found: {repo}")
    branch = _run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"])
    if force or branch == "HEAD":
        _run(["git", "-C", str(repo), "fetch", "--prune"])
        if branch == "HEAD":
            return "fetched (detached HEAD)"
        upstream = _upstream_ref(repo, branch)
        _run(["git", "-C", str(repo), "reset", "--hard", upstream])
        sha = _run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"])
        return f"{branch} сброшена на {upstream} ({sha})"
    out = _run(["git", "-C", str(repo), "pull", "--ff-only", "--prune"])
    sha = _run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"])
    summary = out.splitlines()[-1] if out else "up to date"
    return f"{branch}: {summary} ({sha})"


def list_branches(repo: Path, policy: Policy, *, fetch: bool = True) -> list[str]:
    if not repo.exists():
        raise GitError(f"repo not found: {repo}")
    if fetch:
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


def list_tags(repo: Path, policy: Policy, *, fetch: bool = True) -> list[str]:
    if not repo.exists():
        raise GitError(f"repo not found: {repo}")
    if fetch:
        try:
            _run(["git", "-C", str(repo), "fetch", "--prune", "--tags"])
        except GitError:
            pass
    try:
        out = _run(["git", "-C", str(repo), "tag", "-l", "--sort=-v:refname"])
    except GitError:
        return []
    tags: list[str] = []
    for line in out.splitlines():
        line = line.strip()
        if line and tag_allowed(policy, line):
            tags.append(line)
    return tags


def resolve_tag(repo: Path, tag: str, policy: Policy, *, fetch: bool = True) -> str:
    name = tag.strip()
    if not name:
        raise GitError("empty tag")
    if not tag_allowed(policy, name):
        raise GitError(f"tag not allowed: {name}")
    if fetch:
        try:
            _run(["git", "-C", str(repo), "fetch", "--prune", "--tags"])
        except GitError:
            pass
    return _peel_commit(repo, f"refs/tags/{name}")


def _local_branch_name(branch: str) -> str:
    branch = branch.strip()
    if branch.startswith("origin/"):
        return branch[len("origin/") :]
    return branch


def _peel_commit(repo: Path, ref: str) -> str:
    sha = _run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"]
    )
    if len(sha) != 40:
        raise GitError(f"invalid commit: {sha}")
    return sha


def resolve_instance_commit(repo: Path, inst: dict, policy: Policy) -> str:
    """Commit для сборки/деплоя: git_tag, pin в git_commit или HEAD ветки."""
    tag_pin = (inst.get("git_tag") or "").strip()
    if tag_pin:
        return resolve_tag(repo, tag_pin, policy)
    pin = (inst.get("git_commit") or "").strip()
    if pin:
        try:
            sha = _run(["git", "-C", str(repo), "rev-parse", pin])
        except GitError as err:
            raise GitError(f"pinned commit not found: {pin}") from err
        if len(sha) != 40:
            raise GitError(f"invalid pinned commit: {pin}")
        return sha
    branch = (inst.get("git_branch") or "main").strip()
    return resolve_commit(repo, branch, policy)


def resolve_commit(repo: Path, branch: str, policy: Policy, *, fetch: bool = True) -> str:
    local = _local_branch_name(branch)
    if not branch_allowed(policy, local):
        raise GitError(f"branch not allowed: {branch}")
    if fetch:
        try:
            _run(["git", "-C", str(repo), "fetch", "--prune"])
        except GitError:
            pass
    last_err: GitError | None = None
    for ref in (f"refs/remotes/origin/{local}", f"refs/heads/{local}"):
        try:
            return _peel_commit(repo, ref)
        except GitError as err:
            last_err = err
    raise GitError(f"branch not found: {local}") from last_err


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
