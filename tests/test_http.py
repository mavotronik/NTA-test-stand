import os
import tempfile
import unittest
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from app.deps import get_settings


def _write_test_config(path: Path, *, data_dir: str, password: str = "adminadmin", skip_worker: bool = True) -> None:
    base = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config.example.yaml").read_text(encoding="utf-8")
    )
    base.setdefault("manager", {})
    base["manager"]["data_dir"] = data_dir
    base["manager"]["bootstrap_admin_password"] = password
    base["manager"]["skip_worker"] = skip_worker
    base["manager"]["secret_key"] = "test-secret"
    path.write_text(yaml.safe_dump(base, allow_unicode=True), encoding="utf-8")


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls._cfg = Path(cls._tmp.name) / "config.yaml"
        _write_test_config(cls._cfg, data_dir=cls._tmp.name)
        os.environ["CONFIG_FILE"] = str(cls._cfg)
        get_settings.cache_clear()
        from app.main import app as fastapi_app

        cls._tc = TestClient(fastapi_app)
        cls._tc.__enter__()
        cls.client = cls._tc
        cls.app = fastapi_app

    @classmethod
    def tearDownClass(cls):
        cls._tc.__exit__(None, None, None)
        cls._tmp.cleanup()
        get_settings.cache_clear()

    def test_health_public(self):
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json().get("ok"), "true")

    def test_root_redirects_to_login(self):
        c = TestClient(self.app)
        c.cookies.clear()
        with c:
            r = c.get("/", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers.get("location", ""))

    def test_host_metrics_requires_auth(self):
        c = TestClient(self.app)
        c.cookies.clear()
        with c:
            r = c.get("/api/host-metrics")
        self.assertEqual(r.status_code, 401)

    def test_login_and_overview(self):
        r = self.client.post("/login", data={"username": "admin", "password": "adminadmin"}, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))
        self.client.cookies.update(r.cookies)
        r2 = self.client.get("/")
        self.assertEqual(r2.status_code, 200)
        self.assertIn("Обзор", r2.text)


if __name__ == "__main__":
    unittest.main()
