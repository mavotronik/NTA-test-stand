import tempfile
import unittest
from pathlib import Path

from app.services import git_service


class GitWorktreeCleanupTests(unittest.TestCase):
    def test_cleanup_allows_readd_after_orphan_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            import subprocess

            subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
            (repo / "deploy/ui/app").mkdir(parents=True)
            (repo / "deploy/ui/app/Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
            (repo / "deploy/ui/app/package-lock.json").write_text("{}", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True)
            commit = subprocess.check_output(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
            ).strip()
            cache = root / "cache"
            wt = git_service.worktree_path(cache, commit)
            wt.mkdir(parents=True)
            (wt / "orphan.txt").write_text("stale", encoding="utf-8")

            path = git_service.ensure_worktree(
                repo,
                cache,
                commit,
                ui_context="deploy/ui/app",
                ui_dockerfile="deploy/ui/app/Dockerfile",
            )
            self.assertTrue((path / "deploy/ui/app/Dockerfile").is_file())


if __name__ == "__main__":
    unittest.main()
