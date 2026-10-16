import tempfile
import unittest
from pathlib import Path

import yaml

from app.config import (
    branch_allowed,
    host_allowed,
    load_policy_from_mapping,
    load_settings,
    resolve_public_host,
)


class ConfigTests(unittest.TestCase):
    def test_branch_patterns(self):
        data = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "config.example.yaml").read_text(encoding="utf-8")
        )
        example_dir = Path(__file__).resolve().parents[1]
        policy = load_policy_from_mapping(data, config_dir=example_dir)
        self.assertTrue(branch_allowed(policy, "main"))
        self.assertTrue(branch_allowed(policy, "feature/foo"))
        self.assertFalse(branch_allowed(policy, "secret/experimental"))

    def test_host_allowlist(self):
        data = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "config.example.yaml").read_text(encoding="utf-8")
        )
        example_dir = Path(__file__).resolve().parents[1]
        policy = load_policy_from_mapping(data, config_dir=example_dir)
        self.assertTrue(host_allowed(policy, "127.0.0.1"))
        self.assertFalse(host_allowed(policy, "8.8.8.8"))

    def test_resolve_public_host_from_request_when_default_loopback(self):
        self.assertEqual(resolve_public_host("127.0.0.1", "192.168.1.10"), "192.168.1.10")
        self.assertEqual(resolve_public_host("127.0.0.1", "grapes.example"), "grapes.example")
        self.assertEqual(resolve_public_host("127.0.0.1", "127.0.0.1"), "127.0.0.1")
        self.assertEqual(resolve_public_host("127.0.0.1", None), "127.0.0.1")
        self.assertEqual(resolve_public_host("10.0.0.5", "192.168.1.10"), "10.0.0.5")

    def test_host_allowlist_disabled_when_cidrs_omitted(self):
        policy = load_policy_from_mapping({"clickhouse": {}})
        self.assertTrue(host_allowed(policy, "8.8.8.8"))
        self.assertTrue(host_allowed(policy, "clickhouse.example.com"))

    def test_reject_duplicate_manager_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.yaml"
            cfg.write_text(
                """
manager:
  bootstrap_admin_password: x
manager:
  data_dir: ./data
""",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                from app.config import load_config_mapping

                load_config_mapping(cfg)

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
