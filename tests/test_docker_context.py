import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from app.services.docker_service import _tar_build_context, compose_build_apk_dns_retryable


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


if __name__ == "__main__":
    unittest.main()
