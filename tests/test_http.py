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
            self.assertIn("page-modal", empty.text)
            self.assertIn('id="instanceFormModal"', empty.text)
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
            self.assertIn('data-close-href="/instances"', edit.text)
            self.assertIn("js-form-modal-dialog", edit.text)
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
            saved = self.client.post(
                f"/instances/{first_id}/edit",
                data={
                    "csrf_token": csrf,
                    "name": "Порт A",
                    "port": "3100",
                    "db_profile_id": profile_id,
                    "git_branch": "main",
                    "return_to": "instances",
                },
                follow_redirects=False,
            )
            self.assertEqual(saved.status_code, 302)
            self.assertTrue(
                saved.headers.get("location", "").startswith("/instances?"),
                saved.headers.get("location", ""),
            )
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
        self.assertIn(f"/instances/{iid}/start", stopped.text)
        self.assertNotIn(f"/instances/{iid}/stop", stopped.text)

        with db.connection() as conn:
            conn.execute("UPDATE instances SET state='running' WHERE id=?", (iid,))
        running = self.client.get("/instances")
        self.assertEqual(running.status_code, 200)
        self.assertIn(f"/instances/{iid}/stop", running.text)
        self.assertNotIn(f"/instances/{iid}/start", running.text)

    def test_instance_lifecycle_queues_jobs_without_blocking(self):
        self.client.post("/login", data={"username": "admin", "password": "adminadmin"}, follow_redirects=True)
        from app.db import Database
        from app.repositories import create_instance, list_users, save_db_profile

        settings = get_settings()
        db = Database(settings.db_path)
        admin_id = next(user["id"] for user in list_users(db) if user["username"] == "admin")
        profile_id = save_db_profile(
            db,
            profile_id=None,
            name="queue-profile",
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
            slug="queue-test",
            name="Queue",
            port=3201,
            db_profile_id=profile_id,
            git_branch="main",
            user_id=admin_id,
        )
        with db.connection() as conn:
            conn.execute(
                "UPDATE instances SET state='stopped', image_tag='demo:latest' WHERE id=?",
                (iid,),
            )
        detail = self.client.get(f"/instances/{iid}")
        self.assertEqual(detail.status_code, 200)
        m = re.search(r'name="csrf_token" value="([^"]+)"', detail.text)
        self.assertIsNotNone(m)
        csrf = m.group(1)
        r_start = self.client.post(
            f"/instances/{iid}/start",
            data={"csrf_token": csrf, "return_to": "instances"},
            follow_redirects=False,
        )
        self.assertEqual(r_start.status_code, 302)
        with db.connection() as conn:
            row = conn.execute(
                "SELECT job_type, status FROM deployment_jobs WHERE instance_id=? ORDER BY created_at DESC LIMIT 1",
                (iid,),
            ).fetchone()
        self.assertEqual(row["job_type"], "instance_start")
        self.assertEqual(row["status"], "queued")

    def test_instances_bulk_actions(self):
        self.client.post("/login", data={"username": "admin", "password": "adminadmin"}, follow_redirects=True)
        from app.db import Database
        from app.repositories import create_instance, list_users, save_db_profile

        settings = get_settings()
        db = Database(settings.db_path)
        admin_id = next(user["id"] for user in list_users(db) if user["username"] == "admin")
        profile_id = save_db_profile(
            db,
            profile_id=None,
            name="bulk-profile",
            host="127.0.0.1",
            port=8123,
            database_name="default",
            username="u",
            password_enc="enc",
            tls=False,
            writable=False,
            comment="",
        )
        i1 = create_instance(
            db,
            slug="bulk-a",
            name="Bulk A",
            port=3210,
            db_profile_id=profile_id,
            git_branch="main",
            user_id=admin_id,
        )
        i2 = create_instance(
            db,
            slug="bulk-b",
            name="Bulk B",
            port=3211,
            db_profile_id=profile_id,
            git_branch="main",
            user_id=admin_id,
        )
        with db.connection() as conn:
            conn.execute(
                "UPDATE instances SET state='stopped', image_tag='demo:latest' WHERE id IN (?,?)",
                (i1, i2),
            )
        listing = self.client.get("/instances")
        self.assertEqual(listing.status_code, 200)
        self.assertIn('id="instances-bulk-form"', listing.text)
        self.assertIn('name="instance_ids"', listing.text)
        self.assertIn('id="confirmModal"', listing.text)
        self.assertIn('hx-target="#formModalInner"', listing.text)
        self.assertIn('data-instances-bulk="start"', listing.text)
        self.assertIn('data-instance-name="Bulk A"', listing.text)

        csrf = self._admin_csrf()
        empty = self.client.post(
            "/instances/bulk",
            data={"csrf_token": csrf, "action": "start"},
            follow_redirects=False,
        )
        self.assertEqual(empty.status_code, 302)
        self.assertIn("error=", empty.headers.get("location", ""))

        csrf = self._admin_csrf()
        r = self.client.post(
            "/instances/bulk",
            data={
                "csrf_token": csrf,
                "action": "start",
                "instance_ids": [i1, i2],
            },
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 302)
        self.assertIn("message=", r.headers.get("location", ""))
        with db.connection() as conn:
            rows = conn.execute(
                """
                SELECT instance_id, job_type FROM deployment_jobs
                WHERE instance_id IN (?,?) AND job_type='instance_start'
                """,
                (i1, i2),
            ).fetchall()
        self.assertEqual(len(rows), 2)

        with db.connection() as conn:
            conn.execute(
                "UPDATE deployment_jobs SET status='done' WHERE instance_id IN (?,?) AND status IN ('queued','running')",
                (i1, i2),
            )

        csrf = self._admin_csrf()
        r_rebuild = self.client.post(
            "/instances/bulk",
            data={
                "csrf_token": csrf,
                "action": "rebuild",
                "instance_ids": [i1],
            },
            follow_redirects=False,
        )
        self.assertEqual(r_rebuild.status_code, 302)
        with db.connection() as conn:
            row = conn.execute(
                "SELECT job_type FROM deployment_jobs WHERE instance_id=? AND job_type='build_run' ORDER BY created_at DESC LIMIT 1",
                (i1,),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["job_type"], "build_run")

        csrf = self._admin_csrf()
        r_stop = self.client.post(
            "/instances/bulk",
            data={"csrf_token": csrf, "action": "stop", "instance_ids": [i1, i2]},
            follow_redirects=False,
        )
        self.assertEqual(r_stop.status_code, 302)
        with db.connection() as conn:
            stop_rows = conn.execute(
                "SELECT COUNT(*) AS c FROM deployment_jobs WHERE instance_id IN (?,?) AND job_type='instance_stop'",
                (i1, i2),
            ).fetchone()
        self.assertEqual(stop_rows["c"], 1)

    def test_confirm_modal_markup(self):
        self.client.post("/login", data={"username": "admin", "password": "adminadmin"}, follow_redirects=True)
        for path in ("/", "/instances", "/databases", "/users"):
            page = self.client.get(path)
            self.assertEqual(page.status_code, 200, path)
            self.assertIn('id="confirmModal"', page.text)

        csrf = self._admin_csrf()
        self.client.post(
            "/users",
            data={
                "csrf_token": csrf,
                "username": "modaluser",
                "password": "temppass1",
                "role": "viewer",
            },
            follow_redirects=False,
        )
        users_page = self.client.get("/users")
        self.assertIn("data-confirm", users_page.text)
        self.assertIn("Удалить пользователя?", users_page.text)
        self.assertIn('id="userCreateModal"', users_page.text)
        self.assertIn('data-open-modal="userCreateModal"', users_page.text)
        self.assertNotIn("onsubmit=", users_page.text)

        from app.db import Database
        from app.repositories import create_instance, list_users, save_db_profile

        settings = get_settings()
        db = Database(settings.db_path)
        admin_id = next(user["id"] for user in list_users(db) if user["username"] == "admin")
        profile_id = save_db_profile(
            db,
            profile_id=None,
            name="modal-profile",
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
            slug="modal-inst",
            name="Modal Inst",
            port=3212,
            db_profile_id=profile_id,
            git_branch="main",
            user_id=admin_id,
        )
        db_page = self.client.get("/databases")
        self.assertIn("data-confirm", db_page.text)
        self.assertIn("Удалить профиль БД?", db_page.text)
        self.assertIn('hx-target="#formModalInner"', db_page.text)
        self.assertIn("/databases/new", db_page.text)
        self.assertNotIn("onsubmit=", db_page.text)
        new_db = self.client.get("/databases/new")
        self.assertEqual(new_db.status_code, 200)
        self.assertIn("page-modal", new_db.text)
        detail = self.client.get(f"/instances/{iid}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("data-confirm", detail.text)
        self.assertIn("Удалить инстанс?", detail.text)
        self.assertIn('data-confirm-ok="Да"', detail.text)
        self.assertIn('data-confirm-cancel="Нет"', detail.text)
        self.assertIn('data-confirm-ok-hover="Да, мне мама разрешила"', detail.text)
        self.assertNotIn("onsubmit=", detail.text)

    def test_build_run_htmx_opens_watch_and_live_partials(self):
        self.client.post("/login", data={"username": "admin", "password": "adminadmin"}, follow_redirects=True)
        from app.db import Database
        from app.repositories import create_instance, list_users, save_db_profile

        settings = get_settings()
        db = Database(settings.db_path)
        admin_id = next(user["id"] for user in list_users(db) if user["username"] == "admin")
        profile_id = save_db_profile(
            db,
            profile_id=None,
            name="watch-profile",
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
            slug="watch-inst",
            name="Watch Inst",
            port=3220,
            db_profile_id=profile_id,
            git_branch="main",
            user_id=admin_id,
        )
        with db.connection() as conn:
            conn.execute(
                "UPDATE instances SET git_commit='abc1234', image_tag='old:tag', container_id='0123456789ab' WHERE id=?",
                (iid,),
            )
        detail = self.client.get(f"/instances/{iid}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn('id="instance-config"', detail.text)
        self.assertIn('id="instance-actions"', detail.text)
        self.assertIn('hx-post="/instances/%s/build-run"' % iid, detail.text)
        self.assertIn("abc1234", detail.text)
        self.assertIn("old:tag", detail.text)
        self.assertIn("0123456789ab"[:12], detail.text)
        m = re.search(r'name="csrf_token" value="([^"]+)"', detail.text)
        self.assertIsNotNone(m)
        csrf = m.group(1)

        redirected = self.client.post(
            f"/instances/{iid}/build-run",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        self.assertEqual(redirected.status_code, 302)
        self.assertIn(f"/instances/{iid}?job=", redirected.headers.get("location", ""))

        with db.connection() as conn:
            conn.execute(
                "UPDATE deployment_jobs SET status='done' WHERE instance_id=? AND status IN ('queued','running')",
                (iid,),
            )

        live = self.client.post(
            f"/instances/{iid}/build-run",
            data={"csrf_token": csrf},
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )
        self.assertEqual(live.status_code, 200)
        self.assertIn("js-log-modal-dialog", live.text)
        self.assertIn("job-log-pre", live.text)
        self.assertIn("js:{after_id: jobLogPollVals().after_id}", live.text)
        self.assertIn('id="instance-job-watch"', live.text)
        self.assertIn("hx-swap-oob", live.text)
        self.assertIn('id="instance-config"', live.text)
        self.assertIn('id="instance-actions"', live.text)
        self.assertRegex(live.text, r'type="submit" disabled')
        self.assertEqual(live.headers.get("HX-Trigger"), "instance-live-refresh")

        with db.connection() as conn:
            job = conn.execute(
                "SELECT id FROM deployment_jobs WHERE instance_id=? AND job_type='build_run' ORDER BY created_at DESC LIMIT 1",
                (iid,),
            ).fetchone()
        self.assertIsNotNone(job)
        watch = self.client.get(f"/instances/{iid}/jobs/{job['id']}/watch")
        self.assertEqual(watch.status_code, 200)
        self.assertIn("job-log-pre", watch.text)

        conflict = self.client.post(
            f"/instances/{iid}/build-run",
            data={"csrf_token": csrf},
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )
        self.assertEqual(conflict.status_code, 200)
        self.assertEqual(conflict.headers.get("HX-Retarget"), "#instance-flash")
        self.assertIn("conflicting job already active", conflict.text)

        with db.connection() as conn:
            conn.execute(
                "UPDATE instances SET git_commit='def5678', image_tag='new:tag', container_id='fedcba987654' WHERE id=?",
                (iid,),
            )
            conn.execute(
                "UPDATE deployment_jobs SET status='done' WHERE instance_id=? AND status IN ('queued','running')",
                (iid,),
            )
        task = self.client.get(f"/instances/{iid}/current-task")
        self.assertEqual(task.status_code, 200)
        self.assertIn("hx-swap-oob", task.text)
        self.assertIn("def5678", task.text)
        self.assertIn("new:tag", task.text)
        self.assertIn("fedcba987654"[:12], task.text)
        actions_html = task.text.split('id="instance-actions"', 1)[-1].split('id="instance-jobs-wrap"', 1)[0]
        self.assertNotRegex(actions_html, r'type="submit" disabled')


if __name__ == "__main__":
    unittest.main()
