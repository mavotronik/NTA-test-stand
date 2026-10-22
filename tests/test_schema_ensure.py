from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.deps import get_settings
from app.services.schema_ensure import (
    SchemaEnsureError,
    clickhouse_ensure_env,
    repo_root_from_compose_dir,
    run_schema_ensure,
    schema_ensure_available,
)
from tests.test_http import _write_test_config


def _settings_in_tmp(tmp: Path):
    cfg = tmp / "config.yaml"
    _write_test_config(cfg, data_dir=str(tmp))
    os.environ["CONFIG_FILE"] = str(cfg)
    get_settings.cache_clear()
    return get_settings()


class SchemaEnsurePathsTests(unittest.TestCase):
    def test_repo_root_from_compose_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            compose = root / "deploy" / "ui"
            compose.mkdir(parents=True)
            self.assertEqual(repo_root_from_compose_dir(compose), root)

    def test_schema_ensure_available(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            script = root / "deploy" / "schema"
            script.mkdir(parents=True)
            (script / "ensure-live.sh").write_text("#!/bin/bash\n", encoding="utf-8")
            (script / "ensure.list").write_text("# empty\n", encoding="utf-8")
            self.assertTrue(schema_ensure_available(root))
            (script / "ensure.list").unlink()
            self.assertFalse(schema_ensure_available(root))


class ClickHouseEnsureEnvTests(unittest.TestCase):
    def test_env_uses_profile_credentials(self):
        with tempfile.TemporaryDirectory() as td:
            settings = _settings_in_tmp(Path(td))
            from app.crypto_util import encrypt_secret, get_fernet

            fernet = get_fernet(settings.fernet_key, settings.secret_key)
            enc = encrypt_secret(fernet, "s3cret")
            prof = {
                "host": "ch.example",
                "port": 8443,
                "username": "default",
                "password_enc": enc,
                "tls": True,
            }
            env = clickhouse_ensure_env(settings, prof)
            self.assertEqual(env["CH_URL"], "https://ch.example:8443")
            self.assertEqual(env["CH_USER"], "default")
            self.assertEqual(env["CH_PASS"], "s3cret")
            self.assertEqual(env["GRANT_USER"], "default")
            self.assertEqual(env["GRANT_PASS"], "s3cret")
            get_settings.cache_clear()


class RunSchemaEnsureTests(unittest.TestCase):
    def test_skip_when_script_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            settings = _settings_in_tmp(root)
            prof = {"host": "127.0.0.1", "port": 8123, "username": "u", "password_enc": "x", "tls": False}
            with mock.patch("app.services.schema_ensure.decrypt_secret", return_value="p"):
                ok = run_schema_ensure(root, settings, prof)
            self.assertFalse(ok)
            get_settings.cache_clear()

    def test_runs_bash_with_env_not_argv(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            schema_dir = root / "deploy" / "schema"
            schema_dir.mkdir(parents=True)
            script = schema_dir / "ensure-live.sh"
            script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            script.chmod(script.stat().st_mode | stat.S_IXUSR)
            (schema_dir / "ensure.list").write_text("deploy/schema/x.sql\n", encoding="utf-8")

            settings = _settings_in_tmp(root)
            prof = {
                "host": "127.0.0.1",
                "port": 8123,
                "username": "grant_user",
                "password_enc": "enc",
                "tls": False,
            }
            captured: dict = {}

            def fake_popen(cmd, cwd, env, stdout, stderr, text, bufsize):
                captured["cmd"] = cmd
                captured["env"] = env
                captured["cwd"] = cwd

                class Proc:
                    def __init__(self):
                        self.stdout = iter(["ok\n"])

                    def wait(self, timeout=None):
                        return 0

                return Proc()

            with (
                mock.patch("app.services.schema_ensure.decrypt_secret", return_value="pwd"),
                mock.patch("app.services.schema_ensure.subprocess.Popen", side_effect=fake_popen),
            ):
                self.assertTrue(run_schema_ensure(root, settings, prof))

            self.assertEqual(captured["cmd"], ["bash", str(script.resolve())])
            self.assertEqual(captured["cwd"], str(root.resolve()))
            self.assertEqual(captured["env"]["CH_USER"], "grant_user")
            self.assertEqual(captured["env"]["CH_PASS"], "pwd")
            self.assertEqual(captured["env"]["GRANT_USER"], "grant_user")
            self.assertNotIn("pwd", captured["cmd"])
            get_settings.cache_clear()

    def test_nonzero_exit_raises(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            schema_dir = root / "deploy" / "schema"
            schema_dir.mkdir(parents=True)
            script = schema_dir / "ensure-live.sh"
            script.write_text("#!/usr/bin/env bash\nexit 2\n", encoding="utf-8")
            script.chmod(script.stat().st_mode | stat.S_IXUSR)
            (schema_dir / "ensure.list").write_text("x.sql\n", encoding="utf-8")
            settings = _settings_in_tmp(root)
            prof = {
                "host": "127.0.0.1",
                "port": 8123,
                "username": "u",
                "password_enc": "enc",
                "tls": False,
            }

            class Proc:
                def __init__(self):
                    self.stdout = iter(["fail\n"])

                def wait(self, timeout=None):
                    return 2

            with (
                mock.patch("app.services.schema_ensure.decrypt_secret", return_value="p"),
                mock.patch("app.services.schema_ensure.subprocess.Popen", return_value=Proc()),
            ):
                with self.assertRaises(SchemaEnsureError):
                    run_schema_ensure(root, settings, prof)
            get_settings.cache_clear()


class DeploySchemaHookTests(unittest.TestCase):
    def test_deploy_calls_ensure_before_launch(self):
        from app.db import Database, new_id, utc_now
        from app.repositories import create_instance, get_instance, queue_job, save_db_profile
        from app.services.job_worker import JobWorker, _InstanceBuildContext

        tmpdir = tempfile.TemporaryDirectory()
        root = Path(tmpdir.name)
        cfg = root / "config.yaml"
        _write_test_config(cfg, data_dir=str(root))
        os.environ["CONFIG_FILE"] = str(cfg)
        get_settings.cache_clear()
        settings = get_settings()
        db = Database(settings.db_path)
        now = utc_now()
        with db.connection() as conn:
            conn.execute(
                "INSERT INTO users(id, username, password_hash, role, active, created_at, updated_at) VALUES (?,?,?,?,1,?,?)",
                (new_id(), "admin", "x", "admin", now, now),
            )
            uid = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
        pid = save_db_profile(
            db,
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
        iid = create_instance(
            db,
            slug="schema-hook",
            name="Schema hook",
            port=3120,
            db_profile_id=pid,
            git_branch="main",
            user_id=uid,
            git_commit="",
            image_tag="pinned:run",
        )
        jid = queue_job(db, iid, "deploy", uid)
        worker = JobWorker(settings, db)
        wt = root / "wt"
        compose = wt / "deploy" / "ui"
        compose.mkdir(parents=True)
        build_ctx = _InstanceBuildContext(
            build_key="bkey",
            context=wt,
            dockerfile=wt / "Dockerfile",
            compose_dir=compose,
            env_example=wt / "env.example",
            version_id="aaaaaaaaaaaa",
            build_git_commit="a" * 40,
            log_label="test",
            image_tag="grapes-nta-test:aaaaaaaaaaaa",
        )
        call_order: list[str] = []

        def fake_ensure(*_a, **_k):
            call_order.append("ensure")
            return False

        def fake_launch(*_a, **_k):
            call_order.append("launch")
            return ("cid", True)

        with (
            mock.patch.object(worker, "_resolve_instance_build_context", return_value=build_ctx),
            mock.patch("app.services.job_worker.instance_container_env", return_value={}),
            mock.patch("app.services.job_worker.run_schema_ensure", side_effect=fake_ensure),
            mock.patch("app.services.job_worker.schema_ensure_available", return_value=True),
            mock.patch("app.services.job_worker.launch_instance_container", side_effect=fake_launch),
        ):
            worker._run_deploy({"id": jid, "instance_id": iid})

        self.assertEqual(call_order, ["ensure", "launch"])
        inst = get_instance(db, iid)
        self.assertEqual(inst["state"], "running")
        get_settings.cache_clear()
        tmpdir.cleanup()


if __name__ == "__main__":
    unittest.main()
