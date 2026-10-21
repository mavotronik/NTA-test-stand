import tempfile
import unittest
from pathlib import Path

from app.db import Database, new_id, utc_now
from app.repositories import create_instance, save_db_profile, unlink_image_tags_from_instances


class ImageUnlinkTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = self._tmpdir.__enter__()
        self.db = Database(Path(self._tmp) / "test.sqlite")
        now = utc_now()
        self.user_id = new_id()
        with self.db.connection() as conn:
            conn.execute(
                "INSERT INTO users(id, username, password_hash, role, active, created_at, updated_at) VALUES (?,?,?,?,1,?,?)",
                (self.user_id, "admin", "x", "admin", now, now),
            )
        pid = save_db_profile(
            self.db,
            profile_id=None,
            name="ch",
            host="127.0.0.1",
            port=8123,
            database_name="default",
            username="u",
            password_enc="enc",
            tls=False,
            writable=False,
            comment="",
        )
        self.iid = create_instance(
            self.db,
            slug="demo",
            name="Demo",
            port=3100,
            db_profile_id=pid,
            git_branch="main",
            user_id=self.user_id,
            image_tag="grapes-nta-test:abc123456789",
        )
        with self.db.connection() as conn:
            conn.execute(
                "UPDATE instances SET rollback_image_tag=? WHERE id=?",
                ("grapes-nta-test:old0000000001", self.iid),
            )

    def tearDown(self):
        self._tmpdir.__exit__(None, None, None)

    def test_unlink_clears_matching_tags(self):
        affected = unlink_image_tags_from_instances(
            self.db,
            ["grapes-nta-test:abc123456789"],
            user_id=self.user_id,
        )
        self.assertEqual(len(affected), 1)
        with self.db.connection() as conn:
            row = conn.execute("SELECT image_tag, rollback_image_tag FROM instances WHERE id=?", (self.iid,)).fetchone()
        self.assertEqual(row["image_tag"], "")
        self.assertEqual(row["rollback_image_tag"], "grapes-nta-test:old0000000001")


if __name__ == "__main__":
    unittest.main()
