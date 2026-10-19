import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.db import Database, new_id, utc_now
from app.deps import get_settings
from app.repositories import create_instance, get_instance, queue_job, save_db_profile
from app.services.job_worker import JobWorker, _InstanceBuildContext

from tests.test_http import _write_test_config


class InstanceCreatePinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls._cfg = Path(cls._tmp.name) / "config.yaml"
        _write_test_config(cls._cfg, data_dir=cls._tmp.name)
        os.environ["CONFIG_FILE"] = str(cls._cfg)
        get_settings.cache_clear()
        from fastapi.testclient import TestClient
        from app.main import app as fastapi_app

        cls._tc = TestClient(fastapi_app)
        cls._tc.__enter__()
        cls.client = cls._tc

    @classmethod
    def tearDownClass(cls):
        cls._tc.__exit__(None, None, None)
        cls._tmp.cleanup()
        get_settings.cache_clear()

    def _admin_csrf(self) -> str:
        r = self.client.get("/users")
        self.assertEqual(r.status_code, 200)
        m = re.search(r'name="csrf_token" value="([^"]+)"', r.text)
        self.assertIsNotNone(m)
        return m.group(1)

    def test_create_without_pins_stores_empty(self):
        self.client.post("/login", data={"username": "admin", "password": "adminadmin"}, follow_redirects=True)
        csrf = self._admin_csrf()
        settings = get_settings()
        db = Database(settings.db_path)
        profile_id = save_db_profile(
            db,
            profile_id=None,
            name="pins-empty",
            host="127.0.0.1",
            port=8123,
            database_name="default",
            username="u",
            password_enc="enc",
            tls=False,
            writable=False,
            comment="",
        )
        with (
            mock.patch("app.main.git_service.list_branches", return_value=["main"]),
            mock.patch("app.main.git_service.list_tags", return_value=[]),
        ):
            r = self.client.post(
                "/instances/new",
                data={
                    "csrf_token": csrf,
                    "name": "Empty pins",
                    "slug": "empty-pins",
                    "port": "3115",
                    "db_profile_id": profile_id,
                    "git_branch": "main",
                },
                follow_redirects=False,
            )
        self.assertEqual(r.status_code, 302)
        iid = r.headers["location"].rstrip("/").split("/")[-1]
        inst = get_instance(db, iid)
        self.assertIsNotNone(inst)
        self.assertEqual(inst["git_commit"], "")
        self.assertEqual(inst["image_tag"], "")

    def test_create_with_image_tag_persists(self):
        self.client.post("/login", data={"username": "admin", "password": "adminadmin"}, follow_redirects=True)
        csrf = self._admin_csrf()
        settings = get_settings()
        db = Database(settings.db_path)
        profile_id = save_db_profile(
            db,
            profile_id=None,
            name="pins-image",
            host="127.0.0.1",
            port=8123,
            database_name="default",
            username="u",
            password_enc="enc",
            tls=False,
            writable=False,
            comment="",
        )
        with (
            mock.patch("app.main.git_service.list_branches", return_value=["main"]),
            mock.patch("app.main.git_service.list_tags", return_value=[]),
        ):
            r = self.client.post(
                "/instances/new",
                data={
                    "csrf_token": csrf,
                    "name": "With image",
                    "slug": "with-image",
                    "port": "3116",
                    "db_profile_id": profile_id,
                    "git_branch": "main",
                    "image_tag": "custom:tag",
                },
                follow_redirects=False,
            )
        self.assertEqual(r.status_code, 302)
        iid = r.headers["location"].rstrip("/").split("/")[-1]
        inst = get_instance(db, iid)
        self.assertEqual(inst["image_tag"], "custom:tag")
        self.assertEqual(inst["git_commit"], "")

    def test_new_form_defaults_git_branch_to_main(self):
        self.client.post("/login", data={"username": "admin", "password": "adminadmin"}, follow_redirects=True)
        with (
            mock.patch("app.main.git_service.list_branches", return_value=["feature/foo", "main"]),
            mock.patch("app.main.git_service.list_tags", return_value=[]),
            mock.patch("app.main.git_service.pull_repo", return_value="ok"),
        ):
            r = self.client.get("/instances/new")
        self.assertEqual(r.status_code, 200)
        self.assertRegex(r.text, r'<option value="main"\s+selected>')

    def test_create_with_git_tag_stores_main_branch(self):
        self.client.post("/login", data={"username": "admin", "password": "adminadmin"}, follow_redirects=True)
        csrf = self._admin_csrf()
        settings = get_settings()
        db = Database(settings.db_path)
        profile_id = save_db_profile(
            db,
            profile_id=None,
            name="pins-tag",
            host="127.0.0.1",
            port=8123,
            database_name="default",
            username="u",
            password_enc="enc",
            tls=False,
            writable=False,
            comment="",
        )
        with (
            mock.patch("app.main.git_service.list_branches", return_value=["feature/foo", "main"]),
            mock.patch("app.main.git_service.list_tags", return_value=["ui-v1.0.0"]),
            mock.patch("app.main.git_service.resolve_tag", return_value="a" * 40),
        ):
            r = self.client.post(
                "/instances/new",
                data={
                    "csrf_token": csrf,
                    "name": "Tag pin",
                    "slug": "tag-pin",
                    "port": "3119",
                    "db_profile_id": profile_id,
                    "git_branch": "feature/foo",
                    "git_tag": "ui-v1.0.0",
                },
                follow_redirects=False,
            )
        self.assertEqual(r.status_code, 302)
        iid = r.headers["location"].rstrip("/").split("/")[-1]
        inst = get_instance(db, iid)
        self.assertEqual(inst["git_tag"], "ui-v1.0.0")
        self.assertEqual(inst["git_branch"], "main")


class DeployPreservesPinsTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._root = Path(self._tmpdir.name)
        cfg = self._root / "config.yaml"
        _write_test_config(cfg, data_dir=str(self._root))
        os.environ["CONFIG_FILE"] = str(cfg)
        get_settings.cache_clear()
        self.settings = get_settings()
        self.db = Database(self.settings.db_path)
        now = utc_now()
        with self.db.connection() as conn:
            conn.execute(
                "INSERT INTO users(id, username, password_hash, role, active, created_at, updated_at) VALUES (?,?,?,?,1,?,?)",
                (new_id(), "admin", "x", "admin", now, now),
            )
            self.uid = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
        self.pid = save_db_profile(
            self.db,
            profile_id=None,
            name="ch",
            host="127.0.0.1",
            port=8123,
            database_name="default",
            username="u",
            password_enc="enc",
            tls=False,
            writable=False,
            comment="",
        )

    def tearDown(self):
        get_settings.cache_clear()
        self._tmpdir.cleanup()

    def _run_deploy_job(
        self,
        *,
        slug: str,
        port: int,
        git_commit: str,
        git_tag: str = "",
        image_tag: str,
    ) -> None:
        iid = create_instance(
            self.db,
            slug=slug,
            name=f"Deploy {slug}",
            port=port,
            db_profile_id=self.pid,
            git_branch="main",
            user_id=self.uid,
            git_commit=git_commit,
            git_tag=git_tag,
            image_tag=image_tag,
        )
        jid = queue_job(self.db, iid, "deploy", self.uid)
        worker = JobWorker(self.settings, self.db)
        wt = self._root / "wt"
        wt.mkdir()
        commit_full = "a" * 40
        build_ctx = _InstanceBuildContext(
            build_key="bkey",
            context=wt,
            dockerfile=wt / "Dockerfile",
            compose_dir=wt,
            env_example=wt / "env.example",
            version_id=commit_full[:12],
            build_git_commit=commit_full,
            log_label="test",
            image_tag="grapes-nta-test:aaaaaaaaaaaa",
        )
        with (
            mock.patch.object(
                worker,
                "_resolve_instance_build_context",
                return_value=build_ctx,
            ),
            mock.patch(
                "app.services.job_worker.instance_container_env",
                return_value={"PORT": "3100"},
            ),
            mock.patch.object(worker.docker, "run_instance_container", return_value="0123456789ab"),
            mock.patch.object(worker.docker, "wait_health_http", return_value=True),
        ):
            worker._run_deploy({"id": jid, "instance_id": iid})
        inst = get_instance(self.db, iid)
        self.assertIsNotNone(inst)
        self.assertEqual(inst["git_commit"], git_commit)
        self.assertEqual(inst.get("git_tag") or "", git_tag)
        self.assertEqual(inst["image_tag"], image_tag)
        self.assertEqual(inst["state"], "running")

    def test_deploy_keeps_empty_commit_pin(self):
        self._run_deploy_job(slug="deploy-empty-commit", port=3117, git_commit="", image_tag="pinned:run")

    def test_deploy_keeps_user_pins(self):
        self._run_deploy_job(slug="deploy-user-pins", port=3118, git_commit="deadbeef", image_tag="user:pin")

    def test_deploy_keeps_git_tag_pin(self):
        self._run_deploy_job(
            slug="deploy-tag-pin",
            port=3119,
            git_commit="",
            git_tag="ui-v1.0.0",
            image_tag="pinned:tag",
        )


if __name__ == "__main__":
    unittest.main()
