import tempfile
import unittest
from pathlib import Path

from app.config import resolve_config_path
from app.services.docker_service import prepare_ui_data_mount


class UiDataMountTests(unittest.TestCase):
    def test_resolve_relative_to_config_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cfg = base / "config.yaml"
            cfg.write_text("x", encoding="utf-8")
            resolved = resolve_config_path(base, "./data/root")
            self.assertEqual(resolved, (base / "data" / "root").resolve())

    def test_prepare_creates_mount_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = prepare_ui_data_mount(Path(tmp) / "inst" / "data")
            self.assertTrue(path.is_dir())


if __name__ == "__main__":
    unittest.main()
