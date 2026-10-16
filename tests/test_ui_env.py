import tempfile
import unittest
from pathlib import Path

from app.crypto_util import encrypt_secret, get_fernet
from app.services.instance_runtime import instance_container_env, parse_env_file, ui_mapping_env


class _Settings:
    secret_key = "test-secret-key"
    fernet_key = None


_EXAMPLE = """
# comment
PORT=3000
CLICKHOUSE_URL=http://localhost:8123
CLICKHOUSE_USER=default
CLICKHOUSE_PASSWORD=
CLICKHOUSE_DATABASE=default
CLICKHOUSE_FLOWS_RAW_TABLE=flows_raw
CH_COL_TIME=time_received_ns
CH_COL_SRC_ASN=src_asn
CH_COL_DST_ASN=dst_asn
CH_MAC_STORAGE=fixedstring
export CH_COL_BYTES=bytes
"""


class UiEnvExampleTests(unittest.TestCase):
    def test_parse_skips_comments_and_quotes(self):
        parsed = parse_env_file("CH_COL_SRC_ASN=src_asn\n# CH_COL_DST_ASN=nope\nCH_COL_TIME='time_received_ns'\n")
        self.assertEqual(parsed["CH_COL_SRC_ASN"], "src_asn")
        self.assertEqual(parsed["CH_COL_TIME"], "time_received_ns")
        self.assertNotIn("CH_COL_DST_ASN", parsed)

    def test_profile_overrides_connection_mappings_stay(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "env.example"
            path.write_text(_EXAMPLE, encoding="utf-8")
            fernet = get_fernet(None, _Settings.secret_key)
            prof = {
                "tls": 0,
                "host": "10.0.0.5",
                "port": 8123,
                "username": "stand",
                "password_enc": encrypt_secret(fernet, "secret-from-profile"),
                "database_name": "nta",
            }
            env = instance_container_env(
                _Settings(),
                {"slug": "demo"},
                prof,
                example_path=path,
                extra_env={"GIT_COMMIT": "abc"},
            )
        self.assertEqual(env["CH_COL_SRC_ASN"], "src_asn")
        self.assertEqual(env["CH_COL_DST_ASN"], "dst_asn")
        self.assertEqual(env["CH_COL_TIME"], "time_received_ns")
        self.assertEqual(env["CH_COL_BYTES"], "bytes")
        self.assertEqual(env["CH_MAC_STORAGE"], "fixedstring")
        self.assertEqual(env["CLICKHOUSE_FLOWS_RAW_TABLE"], "flows_raw")
        self.assertEqual(env["CLICKHOUSE_URL"], "http://10.0.0.5:8123")
        self.assertEqual(env["CLICKHOUSE_USER"], "stand")
        self.assertEqual(env["CLICKHOUSE_PASSWORD"], "secret-from-profile")
        self.assertEqual(env["CLICKHOUSE_DATABASE"], "nta")
        self.assertEqual(env["SESSION_COOKIE_SECURE"], "false")
        self.assertNotIn("PORT", env)
        self.assertEqual(env["GIT_COMMIT"], "abc")

    def test_missing_example_keeps_profile_only(self):
        loaded = ui_mapping_env(Path("/no/such/env.example"))
        self.assertEqual(loaded, {})

    def test_shipped_grapes_example_maps_src_asn(self):
        example = Path("/home/mavotronik/work/GrapesNTA/deploy/ui/env.example")
        if not example.is_file():
            self.skipTest("GrapesNTA env.example is not on this machine")
        loaded = ui_mapping_env(example)
        self.assertEqual(loaded["CH_COL_SRC_ASN"], "src_asn")
        self.assertEqual(loaded["CH_COL_TIME"], "time_received_ns")
        self.assertNotIn("CLICKHOUSE_PASSWORD", loaded)
        self.assertNotIn("CLICKHOUSE_URL", loaded)


if __name__ == "__main__":
    unittest.main()
