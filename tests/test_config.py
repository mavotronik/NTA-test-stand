import tempfile
import unittest
from pathlib import Path

import yaml

from app.config import branch_allowed, host_allowed, load_policy_from_mapping, load_settings


class ConfigTests(unittest.TestCase):
    def test_branch_patterns(self):
        data = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "config.example.yaml").read_text(encoding="utf-8")
        )
        policy = load_policy_from_mapping(data)
        self.assertTrue(branch_allowed(policy, "main"))
        self.assertTrue(branch_allowed(policy, "feature/foo"))
        self.assertFalse(branch_allowed(policy, "secret/experimental"))

    def test_host_allowlist(self):
        data = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "config.example.yaml").read_text(encoding="utf-8")
        )
        policy = load_policy_from_mapping(data)
        self.assertTrue(host_allowed(policy, "127.0.0.1"))
        self.assertFalse(host_allowed(policy, "8.8.8.8"))

    def test_load_custom_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.yaml"
            cfg.write_text(
                """
manager:
  data_dir: {data}
  bootstrap_admin_password: x
git:
  remote_url: /tmp/repo
  allowed_branch_patterns: [main]
ports: {{min: 3100, max: 3101}}
storage_root: /tmp/data
""".format(data=tmp),
                encoding="utf-8",
            )
            settings = load_settings(cfg)
            self.assertEqual(settings.policy.port_min, 3100)
            self.assertEqual(settings.data_dir, Path(tmp))


if __name__ == "__main__":
    unittest.main()
