import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from app.services.docker_service import _tar_build_context


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


if __name__ == "__main__":
    unittest.main()
