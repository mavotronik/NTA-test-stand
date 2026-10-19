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


def _policy(root: Path, *, tag_patterns: list[str] | None = None) -> Policy:
    return Policy(
        git_remote_url=str(root / "repo"),
        allowed_branch_patterns=["main", "ui/*", "feature/*"],
        allowed_tag_patterns=tag_patterns if tag_patterns is not None else ["ui-*"],
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


class GitTagTests(unittest.TestCase):
    def test_list_tags_filters_by_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _init_repo(repo)
            (repo / "app.txt").write_text("v1\n", encoding="utf-8")
            _git(repo, "add", "app.txt")
            _git(repo, "commit", "-m", "c1")
            _git(repo, "tag", "ui-v1.0.0")
            _git(repo, "tag", "detection-v1.0.0")

            tags = git_service.list_tags(repo, _policy(root), fetch=False)
            self.assertEqual(tags, ["ui-v1.0.0"])

    def test_resolve_instance_commit_prefers_git_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _init_repo(repo)
            (repo / "app.txt").write_text("main\n", encoding="utf-8")
            _git(repo, "add", "app.txt")
            _git(repo, "commit", "-m", "main")
            main_sha = _git(repo, "rev-parse", "HEAD")
            _git(repo, "checkout", "-b", "feature/foo")
            (repo / "app.txt").write_text("foo\n", encoding="utf-8")
            _git(repo, "add", "app.txt")
            _git(repo, "commit", "-m", "foo")
            tag_sha = _git(repo, "rev-parse", "HEAD")
            _git(repo, "tag", "ui-v2.0.0")
            _git(repo, "checkout", "main")

            inst = {
                "git_tag": "ui-v2.0.0",
                "git_commit": main_sha,
                "git_branch": "main",
            }
            got = git_service.resolve_instance_commit(repo, inst, _policy(root))
            self.assertEqual(got, tag_sha)
            self.assertNotEqual(got, main_sha)

    def test_resolve_tag_rejects_disallowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _init_repo(repo)
            (repo / "app.txt").write_text("x\n", encoding="utf-8")
            _git(repo, "add", "app.txt")
            _git(repo, "commit", "-m", "c")
            _git(repo, "tag", "secret-v1.0.0")
            with self.assertRaises(git_service.GitError) as ctx:
                git_service.resolve_tag(repo, "secret-v1.0.0", _policy(root), fetch=False)
            self.assertIn("not allowed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
