import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.config import load_settings


class MainEntryTests(unittest.TestCase):
    def test_load_settings_port_from_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.yaml"
            cfg.write_text(
                """
manager:
  host: 0.0.0.0
  port: 8090
  bootstrap_admin_password: x
git:
  remote_url: /tmp/repo
  allowed_branch_patterns: [main]
storage_root: /tmp/data
""",
                encoding="utf-8",
            )
            settings = load_settings(cfg)
            self.assertEqual(settings.port, 8090)
            self.assertEqual(settings.host, "0.0.0.0")

    def test_main_passes_host_port_to_uvicorn(self):
        from app import __main__ as entry

        fake = mock.Mock(port=8090, host="127.0.0.1")
        with mock.patch.object(entry, "load_settings", return_value=fake):
            with mock.patch.object(entry, "uvicorn") as uv:
                entry.main()
        uv.run.assert_called_once_with("app.main:app", host="127.0.0.1", port=8090)


if __name__ == "__main__":
    unittest.main()
