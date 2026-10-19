import subprocess
import tempfile
import unittest
from pathlib import Path

from app.config import Policy
from app.services import git_service


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")


def _policy(root: Path) -> Policy:
    return Policy(
        git_remote_url=str(root / "repo"),
        allowed_branch_patterns=["main", "ui/*", "feature/*"],
        allowed_tag_patterns=[],
        port_min=3100,
        port_max=3199,
        storage_root=str(root / "data"),
        public_host="127.0.0.1",
        clickhouse_cidrs=[],
        ch_connect_timeout_sec=5,
        build_max_parallel=1,
        ui_dockerfile="Dockerfile",
        ui_context=".",
        deploy_max_parallel=1,
        health_timeout_sec=60,
        health_interval_sec=3,
        host_metrics_refresh_sec=5,
        host_disk_paths=["/"],
    )


class GitResolveCommitTests(unittest.TestCase):
    def test_namespaced_remote_branch_without_local_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin = root / "origin"
            clone = root / "clone"
            _init_repo(origin)
            (origin / "app.txt").write_text("main\n", encoding="utf-8")
            _git(origin, "add", "app.txt")
            _git(origin, "commit", "-m", "main")
            _git(origin, "checkout", "-b", "ui/clients-translate")
            (origin / "app.txt").write_text("clients\n", encoding="utf-8")
            _git(origin, "add", "app.txt")
            _git(origin, "commit", "-m", "clients")
            want = _git(origin, "rev-parse", "HEAD")
            _git(origin, "checkout", "main")
            subprocess.run(
                ["git", "clone", str(origin), str(clone)],
                check=True,
                capture_output=True,
            )
            self.assertNotIn("ui/clients-translate", _git(clone, "branch", "--format=%(refname:short)"))

            got = git_service.resolve_commit(clone, "ui/clients-translate", _policy(root))
            self.assertEqual(got, want)

            via_origin = git_service.resolve_commit(
                clone, "origin/ui/clients-translate", _policy(root), fetch=False
            )
            self.assertEqual(via_origin, want)

    def test_local_only_namespaced_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _init_repo(repo)
            (repo / "app.txt").write_text("main\n", encoding="utf-8")
            _git(repo, "add", "app.txt")
            _git(repo, "commit", "-m", "main")
            _git(repo, "checkout", "-b", "feature/foo")
            (repo / "app.txt").write_text("foo\n", encoding="utf-8")
            _git(repo, "add", "app.txt")
            _git(repo, "commit", "-m", "foo")
            want = _git(repo, "rev-parse", "HEAD")
            _git(repo, "checkout", "main")

            got = git_service.resolve_commit(repo, "feature/foo", _policy(root), fetch=False)
            self.assertEqual(got, want)

    def test_missing_branch_has_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _init_repo(repo)
            (repo / "app.txt").write_text("main\n", encoding="utf-8")
            _git(repo, "add", "app.txt")
            _git(repo, "commit", "-m", "main")
            with self.assertRaises(git_service.GitError) as ctx:
                git_service.resolve_commit(repo, "ui/missing", _policy(root), fetch=False)
            self.assertIn("branch not found: ui/missing", str(ctx.exception))
            self.assertNotIn("ambiguous argument", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
