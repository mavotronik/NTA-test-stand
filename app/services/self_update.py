from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from app.build_info import REPO_ROOT, panel_build_info
from app.config import SelfUpdateConfig


class SelfUpdateError(RuntimeError):
    pass


@dataclass
class PendingCommit:
    sha: str
    sha_short: str
    subject: str
    body: str


@dataclass
class SelfUpdateSnapshot:
    phase: str
    running_commit: str
    running_commit_short: str
    remote_commit: str
    remote_commit_short: str
    branch: str
    update_available: bool
    commits: list[PendingCommit]
    check_error: str
    pull_log: str
    pull_error: str
    initiated_by: str
    last_check_at: str
    restart_pending: bool


def _utc_now_iso() -> str:
    from app.db import utc_now

    return utc_now()


def _git(repo: str, *args: str, timeout: float = 120) -> str:
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    proc = subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", repo, *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=timeout,
    )
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "git failed").strip()
        raise SelfUpdateError(msg)
    return (proc.stdout or "").strip()


def _parse_log_commits(raw: str) -> list[PendingCommit]:
    if not raw.strip():
        return []
    out: list[PendingCommit] = []
    for block in raw.split("\x1e"):
        block = block.strip("\n")
        if not block.strip():
            continue
        lines = block.split("\n")
        header = lines[0]
        parts = header.split("\x1f", 2)
        if len(parts) < 2:
            continue
        sha, subject = parts[0], parts[1]
        body = parts[2] if len(parts) > 2 else ""
        if len(lines) > 1:
            extra = "\n".join(lines[1:]).strip()
            body = (body + "\n" + extra).strip() if body else extra
        out.append(
            PendingCommit(
                sha=sha,
                sha_short=sha[:12],
                subject=subject.strip(),
                body=body.strip(),
            )
        )
    return out


AuditFn = Callable[..., None]
RestartFn = Callable[[str], None]


def _default_restart(unit: str) -> None:
    subprocess.run(
        [
            "systemd-run",
            "--on-active=2",
            "systemctl",
            "restart",
            unit,
        ],
        capture_output=True,
        check=False,
        timeout=10,
    )


class SelfUpdateService:
    """Фоновая проверка origin и применение обновления checkout стенда."""

    def __init__(
        self,
        config: SelfUpdateConfig,
        *,
        repo_root: str | None = None,
        restart_fn: RestartFn | None = None,
    ) -> None:
        self._config = config
        self._repo = str(repo_root or REPO_ROOT)
        self._restart_fn = restart_fn or _default_restart
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._audit_fn: AuditFn | None = None
        self._audit_ctx: dict[str, Any] = {}

        self._phase = "idle"
        self._running_commit = ""
        self._remote_commit = ""
        self._commits: list[PendingCommit] = []
        self._check_error = ""
        self._pull_log = ""
        self._pull_error = ""
        self._initiated_by = ""
        self._last_check_at = ""
        self._restart_pending = False

    def set_audit_callback(self, fn: AuditFn | None, **ctx: Any) -> None:
        self._audit_fn = fn
        self._audit_ctx = ctx

    def start(self) -> None:
        if not self._config.enabled:
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="self-update", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        interval = max(30.0, float(self._config.check_interval_sec))
        while not self._stop.is_set():
            try:
                with self._lock:
                    if self._phase in ("idle", "update_available", "failed"):
                        self._check_locked(force_fetch=True)
            except Exception:
                pass
            self._stop.wait(interval)

    def check_now(self) -> SelfUpdateSnapshot:
        with self._lock:
            return self._check_locked(force_fetch=True)

    def snapshot(self) -> SelfUpdateSnapshot:
        with self._lock:
            return self._build_snapshot()

    def apply_update(
        self,
        *,
        username: str,
        user_id: str | None = None,
        role: str | None = None,
        ip: str | None = None,
    ) -> SelfUpdateSnapshot:
        with self._lock:
            if self._phase in ("pulling", "restarting"):
                return self._build_snapshot()
            if not self._commits and self._phase != "update_available":
                self._check_locked(force_fetch=True)
            if not self._commits:
                raise SelfUpdateError("Нет доступных обновлений")
            self._phase = "pulling"
            self._pull_log = ""
            self._pull_error = ""
            self._initiated_by = username
            from_commit = self._running_commit
            branch = self._config.branch
            try:
                out = _git(self._repo, "pull", "--ff-only", "origin", branch, timeout=180)
                self._pull_log = out or "Already up to date."
                new_head = _git(self._repo, "rev-parse", "HEAD")
                self._phase = "restarting"
                self._restart_pending = True
                if self._audit_fn:
                    self._audit_fn(
                        user_id=user_id,
                        username=username,
                        role=role,
                        action="self_update",
                        target_type="stand",
                        target_id=None,
                        detail={
                            "from_commit": from_commit,
                            "to_commit": new_head,
                            "branch": branch,
                            "commits": [c.sha for c in self._commits],
                        },
                        ip=ip,
                        ok=True,
                    )
                self._restart_fn(self._config.systemd_unit)
            except SelfUpdateError as err:
                self._pull_error = str(err)
                self._pull_log = self._pull_log or str(err)
                self._phase = "failed"
                if self._audit_fn:
                    self._audit_fn(
                        user_id=user_id,
                        username=username,
                        role=role,
                        action="self_update",
                        target_type="stand",
                        target_id=None,
                        detail={"from_commit": from_commit, "branch": branch, "error": str(err)},
                        ip=ip,
                        ok=False,
                        error=str(err),
                    )
            return self._build_snapshot()

    def _check_locked(self, *, force_fetch: bool) -> SelfUpdateSnapshot:
        self._last_check_at = _utc_now_iso()
        running = panel_build_info().commit
        self._running_commit = running
        branch = self._config.branch
        remote_ref = f"origin/{branch}"
        try:
            if force_fetch:
                _git(self._repo, "fetch", "--prune", "origin", branch, timeout=180)
            remote_sha = _git(self._repo, "rev-parse", remote_ref)
            self._remote_commit = remote_sha
            self._check_error = ""
            if not running:
                running = _git(self._repo, "rev-parse", "HEAD")
                self._running_commit = running
            if running == remote_sha:
                self._commits = []
                if self._phase not in ("pulling", "restarting"):
                    self._phase = "idle"
            else:
                try:
                    _git(self._repo, "merge-base", "--is-ancestor", running, remote_sha)
                    raw = _git(
                        self._repo,
                        "log",
                        f"{running}..{remote_ref}",
                        "--format=%H%x1f%s%x1f%b%x1e",
                    )
                    self._commits = _parse_log_commits(raw)
                    if self._phase not in ("pulling", "restarting"):
                        self._phase = "update_available"
                except SelfUpdateError:
                    self._commits = []
                    self._check_error = "Локальная версия не является предком origin; требуется ручное вмешательство."
                    if self._phase not in ("pulling", "restarting"):
                        self._phase = "failed"
        except SelfUpdateError as err:
            self._check_error = str(err)
            self._commits = []
            if self._phase not in ("pulling", "restarting"):
                self._phase = "failed"
        return self._build_snapshot()

    def _build_snapshot(self) -> SelfUpdateSnapshot:
        running = self._running_commit or panel_build_info().commit
        remote = self._remote_commit
        return SelfUpdateSnapshot(
            phase=self._phase,
            running_commit=running,
            running_commit_short=running[:12] if running else "—",
            remote_commit=remote,
            remote_commit_short=remote[:12] if remote else "—",
            branch=self._config.branch,
            update_available=bool(self._commits) and self._phase == "update_available",
            commits=list(self._commits),
            check_error=self._check_error,
            pull_log=self._pull_log,
            pull_error=self._pull_error,
            initiated_by=self._initiated_by,
            last_check_at=self._last_check_at,
            restart_pending=self._restart_pending,
        )


def snapshot_to_dict(s: SelfUpdateSnapshot) -> dict[str, Any]:
    return {
        "phase": s.phase,
        "running_commit": s.running_commit,
        "running_commit_short": s.running_commit_short,
        "remote_commit": s.remote_commit,
        "remote_commit_short": s.remote_commit_short,
        "branch": s.branch,
        "update_available": s.update_available,
        "commits": [
            {"sha": c.sha, "sha_short": c.sha_short, "subject": c.subject, "body": c.body}
            for c in s.commits
        ],
        "check_error": s.check_error,
        "pull_log": s.pull_log,
        "pull_error": s.pull_error,
        "initiated_by": s.initiated_by,
        "last_check_at": s.last_check_at,
        "restart_pending": s.restart_pending,
    }
