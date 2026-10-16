import subprocess
import tempfile
import unittest
from pathlib import Path

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


class GitPullTests(unittest.TestCase):
    def test_pull_fast_forwards_and_force_resets_local_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin = root / "origin"
            clone = root / "clone"
            _init_repo(origin)
            (origin / "app.txt").write_text("v1\n", encoding="utf-8")
            _git(origin, "add", "app.txt")
            _git(origin, "commit", "-m", "v1")
            subprocess.run(["git", "clone", str(origin), str(clone)], check=True, capture_output=True)
            _git(clone, "config", "user.email", "t@example.com")
            _git(clone, "config", "user.name", "t")

            (origin / "app.txt").write_text("v2\n", encoding="utf-8")
            _git(origin, "add", "app.txt")
            _git(origin, "commit", "-m", "v2")

            status = git_service.pull_repo(clone)
            self.assertIn("main:", status)
            self.assertEqual((clone / "app.txt").read_text(encoding="utf-8"), "v2\n")

            (origin / "app.txt").write_text("v3\n", encoding="utf-8")
            _git(origin, "add", "app.txt")
            _git(origin, "commit", "-m", "v3")
            (clone / "app.txt").write_text("local\n", encoding="utf-8")
            with self.assertRaises(git_service.GitError):
                git_service.pull_repo(clone)
            self.assertEqual((clone / "app.txt").read_text(encoding="utf-8"), "local\n")

            forced = git_service.pull_repo(clone, force=True)
            self.assertIn("сброшена на origin/main", forced)
            self.assertEqual((clone / "app.txt").read_text(encoding="utf-8"), "v3\n")

    def test_pull_without_upstream_reports_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo(repo)
            (repo / "f.txt").write_text("x\n", encoding="utf-8")
            _git(repo, "add", "f.txt")
            _git(repo, "commit", "-m", "c")
            with self.assertRaises(git_service.GitError):
                git_service.pull_repo(repo)


if __name__ == "__main__":
    unittest.main()
