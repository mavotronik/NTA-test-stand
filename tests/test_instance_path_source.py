import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.db import Database, new_id, utc_now
from app.deps import get_settings
from app.repositories import create_instance, get_instance, queue_job, save_db_profile
from app.services.job_worker import JobWorker

from tests.test_http import _write_test_config


def _ui_tree(root: Path) -> None:
    ui = root / "deploy" / "ui"
    (ui / "app").mkdir(parents=True)
    (ui / "app" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (ui / "app" / "package-lock.json").write_text("{}", encoding="utf-8")
    (ui / "env.example").write_text("# test\nCH_COL_FOO=bar\n", encoding="utf-8")


class InstancePathSourceHttpTests(unittest.TestCase):
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
        cls.grapes_root = Path(cls._tmp.name) / "GrapesNTA"
        _ui_tree(cls.grapes_root)

    @classmethod
    def tearDownClass(cls):
        cls._tc.__exit__(None, None, None)
        cls._tmp.cleanup()
        get_settings.cache_clear()

    def setUp(self):
        self.client.post("/login", data={"username": "admin", "password": "adminadmin"}, follow_redirects=True)
        settings = get_settings()
        self.db = Database(settings.db_path)
        self.profile_id = save_db_profile(
            self.db,
            profile_id=None,
            name=f"path-prof-{new_id()[:8]}",
            host="127.0.0.1",
            port=8123,
            database_name="default",
            username="u",
            password_enc="enc",
            tls=False,
            writable=False,
            comment="",
        )

    def _csrf_from_new_form(self) -> str:
        with mock.patch("app.main.git_service.pull_repo", return_value="ok"), mock.patch(
            "app.main.git_service.list_branches", return_value=["main"]
        ):
            r = self.client.get("/instances/new")
        self.assertEqual(r.status_code, 200)
        m = re.search(r'name="csrf_token" value="([^"]+)"', r.text)
        self.assertIsNotNone(m)
        return m.group(1)

    def test_create_path_instance_persists(self):
        csrf = self._csrf_from_new_form()
        slug = f"path-{new_id()[:8]}"
        r = self.client.post(
            "/instances/new",
            data={
                "csrf_token": csrf,
                "name": "Path inst",
                "slug": slug,
                "port": 3199,
                "db_profile_id": self.profile_id,
                "source_mode": "path",
                "source_path": str(self.grapes_root),
            },
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 302)
        iid = r.headers["location"].split("/")[-1]
        inst = get_instance(self.db, iid)
        self.assertEqual(inst["source_mode"], "path")
        self.assertEqual(inst["source_path"], str(self.grapes_root.resolve()))
        self.assertEqual(inst["git_branch"], "")

    def test_create_path_invalid_path_shows_error(self):
        csrf = self._csrf_from_new_form()
        r = self.client.post(
            "/instances/new",
            data={
                "csrf_token": csrf,
                "name": "Bad path",
                "slug": f"bad-{new_id()[:8]}",
                "port": 3198,
                "db_profile_id": self.profile_id,
                "source_mode": "path",
                "source_path": "/no/such/grapes",
            },
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("не найден", r.text.lower())


class PathBuildWorkerTests(unittest.TestCase):
    def test_build_path_instance_without_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.yaml"
            _write_test_config(cfg, data_dir=tmp)
            os.environ["CONFIG_FILE"] = str(cfg)
            get_settings.cache_clear()
            settings = get_settings()
            db = Database(settings.db_path)
            grapes = Path(tmp) / "GrapesNTA"
            _ui_tree(grapes)
            uid = new_id()
            profile_id = save_db_profile(
                db,
                profile_id=None,
                name="p",
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
                slug="path-build",
                name="Path build",
                port=3200,
                db_profile_id=profile_id,
                git_branch="",
                user_id=uid,
                source_mode="path",
                source_path=str(grapes),
            )
            jid = queue_job(db, iid, "build", uid)
            worker = JobWorker(settings, db)
            with mock.patch.object(
                worker.docker,
                "can_compose_ui_build",
                return_value=False,
            ), mock.patch.object(
                worker.docker,
                "build_image",
                return_value=("grapes-nta-test:path-deadbeef", "imgid"),
            ) as build_image, mock.patch(
                "app.services.job_worker.git_service.resolve_instance_commit",
                side_effect=AssertionError("git resolve must not run for path source"),
            ), mock.patch(
                "app.services.job_worker.git_service.ensure_worktree",
                side_effect=AssertionError("worktree must not run for path source"),
            ):
                worker._run_build({"id": jid, "instance_id": iid, "build_id": None})
            build_image.assert_called_once()
            inst = get_instance(db, iid)
            self.assertIsNotNone(inst)
            get_settings.cache_clear()

    def test_path_source_rebuilds_on_repeat_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.yaml"
            _write_test_config(cfg, data_dir=tmp)
            os.environ["CONFIG_FILE"] = str(cfg)
            get_settings.cache_clear()
            settings = get_settings()
            db = Database(settings.db_path)
            grapes = Path(tmp) / "GrapesNTA"
            _ui_tree(grapes)
            uid = new_id()
            profile_id = save_db_profile(
                db,
                profile_id=None,
                name="p2",
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
                slug="path-rebuild",
                name="Path rebuild",
                port=3201,
                db_profile_id=profile_id,
                git_branch="",
                user_id=uid,
                source_mode="path",
                source_path=str(grapes),
            )
            worker = JobWorker(settings, db)
            patches = (
                mock.patch.object(worker.docker, "can_compose_ui_build", return_value=False),
                mock.patch.object(
                    worker.docker,
                    "build_image",
                    return_value=("grapes-nta-test:path-deadbeef", "imgid"),
                ),
                mock.patch(
                    "app.services.job_worker.git_service.resolve_instance_commit",
                    side_effect=AssertionError("git resolve must not run for path source"),
                ),
                mock.patch(
                    "app.services.job_worker.git_service.ensure_worktree",
                    side_effect=AssertionError("worktree must not run for path source"),
                ),
            )
            with patches[0], patches[1] as build_image, patches[2], patches[3]:
                jid1 = queue_job(db, iid, "build", uid)
                worker._run_build({"id": jid1, "instance_id": iid, "build_id": None})
                jid2 = queue_job(db, iid, "build", uid)
                worker._run_build({"id": jid2, "instance_id": iid, "build_id": None})
            self.assertEqual(build_image.call_count, 2)
            with db.connection() as conn:
                rows = conn.execute(
                    "SELECT status FROM builds WHERE build_key != '' ORDER BY created_at"
                ).fetchall()
            statuses = [r["status"] for r in rows]
            self.assertEqual(statuses.count("ready"), 1)
            self.assertEqual(statuses.count("removed"), 1)
            get_settings.cache_clear()


if __name__ == "__main__":
    unittest.main()
