import html
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def _admin_csrf(self) -> str:
        r = self.client.get("/users")
        self.assertEqual(r.status_code, 200)
        m = re.search(r'name="csrf_token" value="([^"]+)"', r.text)
        self.assertIsNotNone(m)
        return m.group(1)

    def test_user_password_and_delete(self):
        self.client.post("/login", data={"username": "admin", "password": "adminadmin"}, follow_redirects=True)
        csrf = self._admin_csrf()
        r = self.client.post(
            "/users",
            data={
                "csrf_token": csrf,
                "username": "tempuser",
                "password": "temppass1",
                "role": "viewer",
            },
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 302)
        csrf = self._admin_csrf()
        from app.deps import get_settings
        from app.db import Database
        from app.repositories import list_users

        db = Database(get_settings().db_path)
        users = list_users(db)
        admin_id = next(u["id"] for u in users if u["username"] == "admin")
        uid = next(u["id"] for u in users if u["username"] == "tempuser")
        r_self = self.client.post(
            f"/users/{admin_id}/delete",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        self.assertEqual(r_self.status_code, 400)
        csrf = self._admin_csrf()
        r_pwd = self.client.post(
            f"/users/{uid}/password",
            data={"csrf_token": csrf, "password": "newpass99"},
            follow_redirects=False,
        )
        self.assertEqual(r_pwd.status_code, 302)
        c2 = TestClient(self.app)
        with c2:
            r_login = c2.post(
                "/login",
                data={"username": "tempuser", "password": "newpass99"},
                follow_redirects=False,
            )
            self.assertIn(r_login.status_code, (302, 303))
            csrf = self._admin_csrf()
            r_del = self.client.post(
                f"/users/{uid}/delete",
                data={"csrf_token": csrf},
                follow_redirects=False,
            )
            self.assertEqual(r_del.status_code, 302)

    def test_instance_port_autofill_and_conflict(self):
        self.client.post("/login", data={"username": "admin", "password": "adminadmin"}, follow_redirects=True)
        csrf = self._admin_csrf()
        from app.db import Database
        from app.repositories import create_instance, list_users, save_db_profile

        settings = get_settings()
        db = Database(settings.db_path)
        admin_id = next(user["id"] for user in list_users(db) if user["username"] == "admin")
        profile_id = save_db_profile(
            db,
            profile_id=None,
            name="port-autofill",
            host="127.0.0.1",
            port=8123,
            database_name="default",
            username="u",
            password_enc="enc",
            tls=False,
            writable=False,
            comment="",
        )
        with mock.patch("app.main.git_service.list_branches", return_value=["main"]):
            empty = self.client.get("/instances/new")
            self.assertEqual(empty.status_code, 200)
            self.assertIn('id="instance-port"', empty.text)
            self.assertIn('value="3100"', empty.text)
            self.assertIn("Подставлен первый свободный порт", empty.text)

            first_id = create_instance(
                db,
                slug="port-a",
                name="Порт A",
                port=3100,
                db_profile_id=profile_id,
                git_branch="main",
                user_id=admin_id,
            )
            create_instance(
                db,
                slug="port-c",
                name="Порт C",
                port=3102,
                db_profile_id=profile_id,
                git_branch="main",
                user_id=admin_id,
            )
            suggested = self.client.get("/instances/new")
            self.assertEqual(suggested.status_code, 200)
            self.assertIn('value="3101"', suggested.text)
            self.assertIn("Порт A", suggested.text)
            self.assertIn("Порт C", suggested.text)

            conflict = self.client.post(
                "/instances/new",
                data={
                    "csrf_token": csrf,
                    "name": "Новый",
                    "slug": "port-b",
                    "port": "3100",
                    "db_profile_id": profile_id,
                    "git_branch": "main",
                    "git_commit": "abc",
                    "image_tag": "img:1",
                },
                follow_redirects=False,
            )
            self.assertEqual(conflict.status_code, 409)
            self.assertEqual(self._port_input_class(conflict.text), "input input-invalid")
            self.assertIn("уже занят инстансом «Порт A»", conflict.text)
            self.assertIn('value="3100"', conflict.text)
            self.assertIn('value="abc"', conflict.text)

            created = self.client.post(
                "/instances/new",
                data={
                    "csrf_token": csrf,
                    "name": "Новый",
                    "slug": "port-b",
                    "port": "3101",
                    "db_profile_id": profile_id,
                    "git_branch": "main",
                },
                follow_redirects=False,
            )
            self.assertEqual(created.status_code, 302)

            edit = self.client.get(f"/instances/{first_id}/edit")
            self.assertEqual(edit.status_code, 200)
            self.assertIn('value="3100"', edit.text)
            self.assertEqual(self._port_input_class(edit.text), "input")
            occupied = html.unescape(edit.text)
            self.assertNotIn('"slug": "port-a"', occupied)
            self.assertIn('"slug": "port-b"', occupied)

            edit_conflict = self.client.post(
                f"/instances/{first_id}/edit",
                data={
                    "csrf_token": csrf,
                    "name": "Порт A",
                    "port": "3101",
                    "db_profile_id": profile_id,
                    "git_branch": "main",
                },
                follow_redirects=False,
            )
            self.assertEqual(edit_conflict.status_code, 409)
            self.assertEqual(self._port_input_class(edit_conflict.text), "input input-invalid")
            self.assertIn("уже занят инстансом «Новый»", edit_conflict.text)

    def _port_input_class(self, html: str) -> str:
        match = re.search(r'<input class="([^"]*)" id="instance-port"', html)
        self.assertIsNotNone(match)
        return match.group(1)

    def test_instances_start_stop_buttons(self):
        self.client.post("/login", data={"username": "admin", "password": "adminadmin"}, follow_redirects=True)
        from app.db import Database
        from app.repositories import create_instance, list_users, save_db_profile

        settings = get_settings()
        db = Database(settings.db_path)
        admin_id = next(user["id"] for user in list_users(db) if user["username"] == "admin")
        profile_id = save_db_profile(
            db,
            profile_id=None,
            name="btn-profile",
            host="127.0.0.1",
            port=8123,
            database_name="default",
            username="u",
            password_enc="enc",
            tls=False,
            writable=False,
            comment="",
        )
        iid = create_instance(
            db,
            slug="btn-test",
            name="Кнопки",
            port=3200,
            db_profile_id=profile_id,
            git_branch="main",
            user_id=admin_id,
        )
        with db.connection() as conn:
            conn.execute(
                "UPDATE instances SET state='stopped', image_tag='demo:latest' WHERE id=?",
                (iid,),
            )
        stopped = self.client.get("/instances")
        self.assertEqual(stopped.status_code, 200)
        self.assertIn("mdi-play", stopped.text)
        self.assertIn("Запустить", stopped.text)
        self.assertIn("button success", stopped.text)
        self.assertNotIn("mdi-stop", stopped.text)

        with db.connection() as conn:
            conn.execute("UPDATE instances SET state='running' WHERE id=?", (iid,))
        running = self.client.get("/instances")
        self.assertEqual(running.status_code, 200)
        self.assertIn("mdi-stop", running.text)
        self.assertIn("Остановить", running.text)
        self.assertIn("button danger", running.text)
        self.assertNotIn("mdi-play", running.text)


if __name__ == "__main__":
    unittest.main()
