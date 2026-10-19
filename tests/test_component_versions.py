import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.config import Settings
from app.services.component_versions import (
    build_instance_version_extra_env,
    format_component_versions_tsv,
    parse_component_versions_tsv,
    resolve_instance_ui_version_meta,
    write_instance_component_versions,
)
from app.services.docker_service import (
    ComponentVersionRow,
    DockerService,
    _parse_container_env_list,
)


class ParseContainerEnvTests(unittest.TestCase):
    def test_parse_env_list(self):
        env = _parse_container_env_list(
            ["GRAPES_VERSION=worker-v1.0.1", "PORT=8080", "NOEQUALS"]
        )
        self.assertEqual(env["GRAPES_VERSION"], "worker-v1.0.1")
        self.assertEqual(env["PORT"], "8080")
        self.assertNotIn("NOEQUALS", env)


class ComponentVersionsTsvTests(unittest.TestCase):
    def test_roundtrip(self):
        rows = {
            "ui": ComponentVersionRow("ui-v1.0.0", "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00Z"),
            "worker": ComponentVersionRow("worker-v1.0.0", "", "2026-01-02T01:00:00Z"),
        }
        text = format_component_versions_tsv(rows)
        parsed = parse_component_versions_tsv(text)
        self.assertEqual(parsed["ui"].version, "ui-v1.0.0")
        self.assertEqual(parsed["worker"].version, "worker-v1.0.0")

    def test_write_merges_host_and_preserves_schema(self):
        docker = mock.Mock(spec=DockerService)
        docker.read_host_component_versions.return_value = {
            "worker": ComponentVersionRow(
                "worker-v1.0.2",
                "2026-02-01T00:00:00+00:00",
                "2026-02-01T12:00:00Z",
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            existing = data_dir / "component-versions.tsv"
            existing.write_text(
                "schema\tschema-v1.0.0\t2026-01-01T00:00:00+00:00\t2026-01-01T00:00:00Z\n",
                encoding="utf-8",
            )
            write_instance_component_versions(
                data_dir,
                docker,
                ui_version="ui-v1.0.3",
                ui_version_date="2026-03-01T00:00:00+00:00",
                deployed_at="2026-03-01T10:00:00Z",
            )
            merged = parse_component_versions_tsv(
                (data_dir / "component-versions.tsv").read_text(encoding="utf-8")
            )
        self.assertIn("schema", merged)
        self.assertEqual(merged["worker"].version, "worker-v1.0.2")
        self.assertEqual(merged["ui"].version, "ui-v1.0.3")
        self.assertNotIn("detection", merged)


class ReadHostComponentVersionsTests(unittest.TestCase):
    def test_skips_missing_and_unknown(self):
        worker = mock.Mock()
        worker.attrs = {
            "Config": {"Env": ["GRAPES_VERSION=worker-v1.0.1", "GRAPES_VERSION_DATE=2026-01-01T00:00:00+00:00"]},
            "State": {"StartedAt": "2026-01-02T10:00:00.123456789Z"},
        }
        worker.reload = mock.Mock()

        detection = mock.Mock()
        detection.attrs = {
            "Config": {"Env": ["GRAPES_VERSION=unknown"]},
            "State": {"StartedAt": ""},
        }
        detection.reload = mock.Mock()

        def get_container(name):
            if name == "grapes-worker":
                return worker
            if name == "grapes-detection":
                return detection
            return None

        svc = DockerService()
        with mock.patch.object(svc, "get_container", side_effect=get_container):
            rows = svc.read_host_component_versions()
        self.assertEqual(set(rows.keys()), {"worker"})
        self.assertEqual(rows["worker"].version, "worker-v1.0.1")


class UiVersionMetaTests(unittest.TestCase):
    def test_git_tag_becomes_grapes_version(self):
        settings = mock.Mock(spec=Settings)
        settings.policy = mock.Mock(
            git_remote_url="/tmp/repo",
            ui_context="deploy/ui/app",
            ui_dockerfile="deploy/ui/app/Dockerfile",
        )
        settings.git_cache_dir = Path("/tmp/cache")
        inst = {
            "git_tag": "ui-v1.0.0",
            "git_branch": "main",
            "git_commit": "",
            "source_mode": "repo",
        }
        with mock.patch(
            "app.services.component_versions.git_service.resolve_instance_commit",
            return_value="a" * 40,
        ), mock.patch(
            "app.services.component_versions.git_service.version_date_for_component_version",
            return_value="2026-10-13T16:35:21+03:00",
        ):
            meta = resolve_instance_ui_version_meta(settings, inst)
            env = build_instance_version_extra_env(settings, inst)
        self.assertEqual(meta.grapes_version, "ui-v1.0.0")
        self.assertEqual(meta.git_commit, "a" * 40)
        self.assertEqual(env["GRAPES_VERSION"], "ui-v1.0.0")
        self.assertEqual(env["GIT_COMMIT"], "a" * 40)
        self.assertEqual(env["UI_DATA_DIR"], "/app/server/data")
        self.assertEqual(env["COMPONENT_VERSIONS_FILE"], "/app/server/data/component-versions.tsv")


class LaunchInstanceVersionEnvTests(unittest.TestCase):
    def test_start_passes_version_env(self):
        from app.db import Database
        from app.services.instance_runtime import launch_instance_container

        settings = mock.Mock()
        settings.instance_ui_data_dir.return_value = Path("/tmp/x/data")
        settings.policy.health_timeout_sec = 1
        settings.policy.health_interval_sec = 1
        inst = {"slug": "demo", "port": 3001, "db_profile_id": "p1"}
        db = mock.Mock(spec=Database)
        docker = mock.Mock(spec=DockerService)
        docker.run_instance_container.return_value = "cid"
        docker.wait_health_http.return_value = True
        docker.read_host_component_versions.return_value = {}

        version_env = {
            "GRAPES_VERSION": "ui-v9.9.9",
            "GRAPES_VERSION_DATE": "2026-01-01T00:00:00+00:00",
            "GIT_COMMIT": "b" * 40,
            "UI_DATA_DIR": "/app/server/data",
            "COMPONENT_VERSIONS_FILE": "/app/server/data/component-versions.tsv",
        }
        with (
            mock.patch(
                "app.services.instance_runtime.get_db_profile",
                return_value={"password_enc": "", "tls": 0, "host": "h", "port": 8123, "username": "u", "database_name": "d"},
            ),
            mock.patch(
                "app.services.instance_runtime.build_instance_version_extra_env",
                return_value=version_env,
            ),
            mock.patch("app.services.instance_runtime.write_instance_component_versions"),
            mock.patch(
                "app.services.instance_runtime.instance_container_env",
                side_effect=lambda *a, **k: dict(k.get("extra_env") or {}),
            ) as env_fn,
        ):
            launch_instance_container(db, settings, docker, inst, "img:tag")
        env_fn.assert_called_once()
        passed = env_fn.call_args.kwargs["extra_env"]
        self.assertEqual(passed["GRAPES_VERSION"], "ui-v9.9.9")


if __name__ == "__main__":
    unittest.main()
