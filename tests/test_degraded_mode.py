import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml
from fastapi.testclient import TestClient

from app import deps
from app.degraded import clear_degraded, is_degraded
from app.deps import enter_degraded_mode, get_settings


def _write_test_config(path: Path, *, data_dir: str) -> None:
    base = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config.example.yaml").read_text(encoding="utf-8")
    )
    base.setdefault("manager", {})
    base["manager"]["data_dir"] = data_dir
    base["manager"]["bootstrap_admin_password"] = "adminadmin"
    base["manager"]["skip_worker"] = True
    base["manager"]["secret_key"] = "test-secret"
    path.write_text(yaml.safe_dump(base, allow_unicode=True), encoding="utf-8")


class DegradedModeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._cfg = Path(self._tmp.name) / "config.yaml"
        _write_test_config(self._cfg, data_dir=self._tmp.name)
        os.environ["CONFIG_FILE"] = str(self._cfg)
        get_settings.cache_clear()
        deps._metrics = None
        deps._metrics_recorder = None
        deps._worker = None
        clear_degraded()

    def tearDown(self):
        clear_degraded()
        get_settings.cache_clear()
        os.environ.pop("CONFIG_FILE", None)
        self._tmp.cleanup()
        deps._metrics = None
        deps._metrics_recorder = None
        deps._worker = None

    def _client_with_patches(self, **patches):
        ctxs = [mock.patch(k, v) for k, v in patches.items()]
        for c in ctxs:
            c.__enter__()
        try:
            from app.main import app as fastapi_app

            tc = TestClient(fastapi_app)
            tc.__enter__()
            return tc
        except Exception:
            for c in reversed(ctxs):
                c.__exit__(None, None, None)
            raise

    def _close_client(self, tc, ctxs):
        tc.__exit__(None, None, None)
        for c in reversed(ctxs):
            c.__exit__(None, None, None)

    def test_disk_full_skips_database(self):
        patches = {
            "app.main.is_filesystem_full": mock.Mock(return_value=True),
            "app.main.Database": mock.Mock(side_effect=AssertionError("Database must not open")),
        }
        ctxs = [mock.patch(k, v) for k, v in patches.items()]
        for c in ctxs:
            c.__enter__()
        from app.main import app as fastapi_app

        with TestClient(fastapi_app) as client:
            with mock.patch("app.main.attempt_degraded_recovery", return_value=False):
                r = client.get("/degraded")
            self.assertEqual(r.status_code, 200)
            self.assertIn("Ограниченный режим", r.text)
            self.assertIn("нет свободного места", r.text)
            r_health = client.get("/api/health")
            self.assertEqual(r_health.status_code, 503)
            self.assertEqual(r_health.json().get("disk_full"), "true")
            self.assertEqual(client.get("/", follow_redirects=False).status_code, 302)
            self.assertEqual(client.get("/instances", follow_redirects=False).headers.get("location"), "/degraded")
            self.assertIsNone(deps._metrics_recorder)
            self.assertIsNone(deps._worker)
        for c in reversed(ctxs):
            c.__exit__(None, None, None)

    def test_sqlite_disk_full_on_open(self):
        patches = {
            "app.main.is_filesystem_full": mock.Mock(return_value=False),
            "app.main.Database": mock.Mock(
                side_effect=sqlite3.OperationalError("database or disk is full")
            ),
        }
        ctxs = [mock.patch(k, v) for k, v in patches.items()]
        for c in ctxs:
            c.__enter__()
        from app.main import app as fastapi_app

        with TestClient(fastapi_app) as client:
            with mock.patch("app.main.attempt_degraded_recovery", return_value=False):
                r = client.get("/degraded")
            self.assertEqual(r.status_code, 200)
            self.assertIn("нет свободного места", r.text)
            self.assertEqual(client.get("/api/health").json().get("disk_full"), "true")
        for c in reversed(ctxs):
            c.__exit__(None, None, None)

    def test_force_degraded_env(self):
        os.environ["TSM_FORCE_DEGRADED"] = "1"
        db_mock = mock.patch("app.main.Database", side_effect=AssertionError("Database must not open"))
        with db_mock:
            from app.main import app as fastapi_app

            with TestClient(fastapi_app) as client:
                r = client.get("/degraded")
                self.assertEqual(r.status_code, 200)
                self.assertIn("Режим включён вручную", r.text)
                health = client.get("/api/health").json()
                self.assertEqual(health.get("forced"), "true")
                self.assertEqual(health.get("degraded"), "true")
        os.environ.pop("TSM_FORCE_DEGRADED", None)

    def test_degraded_recovers_on_reload(self):
        patches = {
            "app.main.is_filesystem_full": mock.Mock(return_value=True),
        }
        ctxs = [mock.patch(k, v) for k, v in patches.items()]
        for c in ctxs:
            c.__enter__()
        from app.main import app as fastapi_app

        with TestClient(fastapi_app) as client:
            with mock.patch("app.main.attempt_degraded_recovery", return_value=False):
                self.assertEqual(client.get("/degraded").status_code, 200)
            for c in ctxs:
                c.__exit__(None, None, None)
            patches2 = {
                "app.main.is_filesystem_full": mock.Mock(return_value=False),
                "app.deps.is_filesystem_full": mock.Mock(return_value=False),
            }
            ctxs2 = [mock.patch(k, v) for k, v in patches2.items()]
            for c in ctxs2:
                c.__enter__()
            try:
                r = client.get("/degraded", follow_redirects=False)
                self.assertEqual(r.status_code, 302)
                self.assertIn("/login", r.headers.get("location", ""))
                self.assertFalse(is_degraded())
                self.assertEqual(client.get("/api/health").status_code, 200)
            finally:
                for c in reversed(ctxs2):
                    c.__exit__(None, None, None)

    def test_degraded_recovers_to_overview_when_session_valid(self):
        from app.main import app as fastapi_app

        with TestClient(fastapi_app) as client:
            login = client.post(
                "/login",
                data={"username": "admin", "password": "adminadmin"},
                follow_redirects=False,
            )
            self.assertEqual(login.status_code, 302)
            enter_degraded_mode(
                reason="test recovery",
                db_path=get_settings().db_path,
                disk_full=False,
            )
            r = client.get("/degraded", follow_redirects=False)
            self.assertEqual(r.status_code, 302)
            self.assertEqual(r.headers.get("location"), "/")
            self.assertFalse(is_degraded())

    def test_sqlite_io_error_not_disk_full_banner(self):
        patches = {
            "app.main.is_filesystem_full": mock.Mock(return_value=False),
            "app.main.Database": mock.Mock(side_effect=sqlite3.OperationalError("disk I/O error")),
        }
        ctxs = [mock.patch(k, v) for k, v in patches.items()]
        for c in ctxs:
            c.__enter__()
        from app.main import app as fastapi_app

        with TestClient(fastapi_app) as client:
            with mock.patch("app.main.attempt_degraded_recovery", return_value=False):
                r = client.get("/degraded")
            self.assertEqual(r.status_code, 200)
            self.assertIn("База панели не открывается", r.text)
            self.assertEqual(client.get("/api/health").json().get("disk_full"), "false")
        for c in reversed(ctxs):
            c.__exit__(None, None, None)


if __name__ == "__main__":
    unittest.main()