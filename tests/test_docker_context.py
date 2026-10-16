import io
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

from app.services.docker_service import (
    _stream_command_output,
    _tar_build_context,
    compose_build_apk_dns_retryable,
    ui_image_build_command,
)


class DockerContextTarTests(unittest.TestCase):
    def test_tar_includes_dockerfile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
            (root / "app.txt").write_text("ok", encoding="utf-8")
            buf = _tar_build_context(root)
            with tarfile.open(fileobj=buf, mode="r:") as tar:
                names = tar.getnames()
            self.assertIn("Dockerfile", names)
            self.assertIn("app.txt", names)


class ComposeBuildRetryTests(unittest.TestCase):
    def test_detects_dns_transient(self):
        sample = (
            "WARNING: fetching https://dl-cdn.alpinelinux.org/...: DNS: transient error "
            "(try again later)\n"
        )
        self.assertTrue(compose_build_apk_dns_retryable(sample))

    def test_detects_apk_index_failure(self):
        sample = (
            "ERROR: unable to select packages:\n"
            "  libcap (no such package):\n"
        )
        self.assertTrue(compose_build_apk_dns_retryable(sample))

    def test_other_failures_not_retryable(self):
        self.assertFalse(compose_build_apk_dns_retryable("COPY failed: file not found"))


class UiImageBuildCommandTests(unittest.TestCase):
    def test_host_network_uses_buildx_allow(self):
        with tempfile.TemporaryDirectory() as tmp:
            ui = Path(tmp)
            (ui / "app").mkdir()
            (ui / "app" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
            (ui / "docker-compose.yml").write_text(
                "services:\n"
                "  grapes-nta:\n"
                "    image: grapes-nta:latest\n"
                "    build:\n"
                "      context: ./app\n"
                "      dockerfile: Dockerfile\n"
                "      network: host\n",
                encoding="utf-8",
            )
            cmd = ui_image_build_command(ui, build_date="2026-10-16T00:00:00Z")
        self.assertEqual(cmd[0:3], ["docker", "buildx", "build"])
        self.assertIn("--allow=network.host", cmd)
        self.assertIn("--network=host", cmd)
        self.assertIn("--progress=plain", cmd)
        self.assertIn("--load", cmd)
        self.assertIn("BUILD_DATE=2026-10-16T00:00:00Z", cmd)
        self.assertEqual(cmd[cmd.index("-t") + 1], "grapes-nta:latest")
        self.assertTrue(cmd[-1].endswith("/app"))

    def test_default_network_stays_on_compose(self):
        with tempfile.TemporaryDirectory() as tmp:
            ui = Path(tmp)
            (ui / "docker-compose.yml").write_text(
                "services:\n"
                "  grapes-nta:\n"
                "    build:\n"
                "      context: ./app\n"
                "      dockerfile: Dockerfile\n",
                encoding="utf-8",
            )
            cmd = ui_image_build_command(ui, build_date="2026-10-16T00:00:00Z")
        self.assertEqual(cmd[0:3], ["docker", "compose", "-f"])
        self.assertIn("build", cmd)
        self.assertNotIn("--allow=network.host", cmd)


class StreamCommandOutputTests(unittest.TestCase):
    def test_emits_each_line_before_the_process_exits(self):
        seen: list[tuple[str, float]] = []

        def on_line(line: str) -> None:
            seen.append((line, time.monotonic()))

        rc, text = _stream_command_output(
            [
                "python3",
                "-c",
                "import time; print('one', flush=True); time.sleep(0.4); print('two', flush=True)",
            ],
            cwd="/tmp",
            log_callback=on_line,
        )
        self.assertEqual(rc, 0)
        self.assertEqual([line for line, _ in seen], ["one", "two"])
        self.assertGreaterEqual(seen[1][1] - seen[0][1], 0.3)
        self.assertIn("one", text)
        self.assertIn("two", text)


if __name__ == "__main__":
    unittest.main()
