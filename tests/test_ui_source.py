import tempfile
import unittest
from pathlib import Path

from app.services.ui_source import UiSourceError, find_ui_layout, normalize_source_path


def _make_ui_tree(base: Path) -> None:
    ui = base / "deploy" / "ui"
    (ui / "app").mkdir(parents=True)
    (ui / "app" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (ui / "app" / "package-lock.json").write_text("{}", encoding="utf-8")


class UiSourceFindTests(unittest.TestCase):
    def test_finds_deploy_ui(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "GrapesNTA"
            _make_ui_tree(root)
            layout = find_ui_layout(root)
            self.assertTrue((layout.ui_dir / "docker-compose.yml").parent == layout.ui_dir)
            self.assertTrue(layout.dockerfile.is_file())
            self.assertEqual(layout.context.name, "app")

    def test_finds_nested_ui(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "work"
            nested = root / "vendor" / "GrapesNTA" / "deploy" / "ui"
            (nested / "app").mkdir(parents=True)
            (nested / "app" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
            layout = find_ui_layout(root)
            self.assertEqual(layout.ui_dir, nested.resolve())

    def test_missing_ui_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "empty"
            root.mkdir()
            with self.assertRaises(UiSourceError):
                find_ui_layout(root)

    def test_normalize_requires_absolute(self):
        with self.assertRaises(UiSourceError):
            normalize_source_path("relative/path")


if __name__ == "__main__":
    unittest.main()
