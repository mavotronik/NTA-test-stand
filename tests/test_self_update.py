import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from app.config import load_settings
from app.services.self_update import SelfUpdateConfig, SelfUpdateError, SelfUpdateService


def _run_git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


class SelfUpdateTests(unittest.TestCase):
    def _init_repo(self, tmp: Path) -> Path:
        repo = tmp / "stand"
        repo.mkdir()
        _run_git(repo, "init", "-b", "main")
        _run_git(repo, "config", "user.email", "t@test.local")
        _run_git(repo, "config", "user.name", "Test")
        (repo / "f.txt").write_text("1", encoding="utf-8")
        _run_git(repo, "add", "f.txt")
        _run_git(repo, "commit", "-m", "initial")
        return repo

    def test_config_self_update_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.yaml"
            cfg.write_text(
                """
manager:
  data_dir: {data}
  bootstrap_admin_password: x
git:
  remote_url: /tmp/repo
self_update:
  enabled: false
  check_interval_sec: 120
  branch: develop
  systemd_unit: custom.service
""".format(data=tmp),
                encoding="utf-8",
            )
            settings = load_settings(cfg)
            self.assertFalse(settings.self_update.enabled)
            self.assertEqual(settings.self_update.check_interval_sec, 120.0)
            self.assertEqual(settings.self_update.branch, "develop")
            self.assertEqual(settings.self_update.systemd_unit, "custom.service")

    def test_no_updates_when_at_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._init_repo(Path(tmp))
            head = _run_git(repo, "rev-parse", "HEAD")
            cfg = SelfUpdateConfig(True, 600, "main", "nta-test-stand.service")
            svc = SelfUpdateService(cfg, repo_root=str(repo))

            def fake_git(repo_path, *args, **kwargs):
                if args[:3] == ("fetch", "--prune", "origin"):
                    return ""
                if args[:2] == ("rev-parse", "origin/main"):
                    return head
                raise AssertionError(args)

            with mock.patch("app.services.self_update.panel_build_info") as pbi:
                pbi.return_value = mock.Mock(commit=head)
                with mock.patch("app.services.self_update._git", side_effect=fake_git):
                    snap = svc.check_now()
            self.assertFalse(snap.update_available)
            self.assertEqual(snap.phase, "idle")

    def test_lists_commits_ahead(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._init_repo(Path(tmp))
            base = _run_git(repo, "rev-parse", "HEAD")
            (repo / "f.txt").write_text("2", encoding="utf-8")
            _run_git(repo, "commit", "-am", "second change")
            _run_git(repo, "branch", "-f", "origin-main", "HEAD")
            _run_git(repo, "reset", "--hard", base)
            cfg = SelfUpdateConfig(True, 600, "main", "nta-test-stand.service")

            def fake_git(repo_path, *args, **kwargs):
                if args[:3] == ("fetch", "--prune", "origin"):
                    return ""
                if args[:2] == ("rev-parse", "origin/main"):
                    return _run_git(repo, "rev-parse", "origin-main")
                if args[:2] == ("merge-base", "--is-ancestor"):
                    return ""
                if args[0] == "log":
                    return _run_git(
                        repo,
                        "log",
                        f"{base}..origin-main",
                        "--format=%H%x1f%s%x1f%b%x1e",
                    )
                raise AssertionError(args)

            svc = SelfUpdateService(cfg, repo_root=str(repo))
            with mock.patch("app.services.self_update.panel_build_info") as pbi:
                pbi.return_value = mock.Mock(commit=base)
                with mock.patch("app.services.self_update._git", side_effect=fake_git):
                    snap = svc.check_now()
            self.assertTrue(snap.update_available)
            self.assertEqual(len(snap.commits), 1)
            self.assertIn("second", snap.commits[0].subject)

    def test_apply_ff_only_failure_no_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._init_repo(Path(tmp))
            base = _run_git(repo, "rev-parse", "HEAD")
            (repo / "f.txt").write_text("2", encoding="utf-8")
            _run_git(repo, "commit", "-am", "remote change")
            remote_head = _run_git(repo, "rev-parse", "HEAD")
            _run_git(repo, "reset", "--hard", base)
            cfg = SelfUpdateConfig(True, 600, "main", "nta-test-stand.service")
            restarted: list[str] = []

            def fake_git(repo_path, *args, **kwargs):
                if args[:3] == ("fetch", "--prune", "origin"):
                    return ""
                if args[:2] == ("rev-parse", "origin/main"):
                    return remote_head
                if args[:2] == ("merge-base", "--is-ancestor"):
                    return ""
                if args[0] == "log":
                    return _run_git(
                        repo,
                        "log",
                        f"{base}..{remote_head}",
                        "--format=%H%x1f%s%x1f%b%x1e",
                    )
                if args[:4] == ("pull", "--ff-only", "origin", "main"):
                    raise SelfUpdateError("not fast-forward")
                raise AssertionError(args)

            svc = SelfUpdateService(
                cfg,
                repo_root=str(repo),
                restart_fn=restarted.append,
            )
            with mock.patch("app.services.self_update.panel_build_info") as pbi:
                pbi.return_value = mock.Mock(commit=base)
                with mock.patch("app.services.self_update._git", side_effect=fake_git):
                    svc.check_now()
                    snap = svc.apply_update(username="admin")
            self.assertEqual(restarted, [])
            self.assertEqual(snap.phase, "failed")
            self.assertIn("not fast-forward", snap.pull_error)


if __name__ == "__main__":
    unittest.main()
