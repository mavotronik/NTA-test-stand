import tempfile
import unittest
from pathlib import Path

from app.auth import authenticate, bootstrap_admin, create_session, get_session_user
from app.crypto_util import decrypt_secret, encrypt_secret, get_fernet, hash_password, verify_password
from app.db import Database


class AuthCryptoTests(unittest.TestCase):
    def test_password_roundtrip(self):
        h = hash_password("secret123")
        self.assertTrue(verify_password(h, "secret123"))
        self.assertFalse(verify_password(h, "wrong"))

    def test_fernet_secrets(self):
        f = get_fernet(None, "test-secret-key")
        enc = encrypt_secret(f, "db-password")
        self.assertEqual(decrypt_secret(f, enc), "db-password")
        self.assertNotIn("db-password", enc)

    def test_session_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "t.sqlite")
            bootstrap_admin(db, "admin", "adminpass")
            user = authenticate(db, "admin", "adminpass")
            self.assertIsNotNone(user)
            sid, _ = create_session(db, user["id"], 1)
            loaded = get_session_user(db, sid)
            self.assertEqual(loaded["username"], "admin")


if __name__ == "__main__":
    unittest.main()
