import tempfile
import unittest
from pathlib import Path

from app.db import Database, new_id, utc_now
from app.repositories import create_instance, next_free_port, port_owner, save_db_profile


class PortAllocationTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmpdir.name) / "test.sqlite")
        now = utc_now()
        self.user_id = new_id()
        with self.db.connection() as conn:
            conn.execute(
                "INSERT INTO users(id, username, password_hash, role, active, created_at, updated_at) VALUES (?,?,?,?,1,?,?)",
                (self.user_id, "admin", "x", "admin", now, now),
            )
        self.profile_id = save_db_profile(
            self.db,
            profile_id=None,
            name="db",
            host="127.0.0.1",
            port=8123,
            database_name="default",
            username="u",
            password_enc="enc",
            tls=False,
            writable=False,
            comment="",
        )

    def tearDown(self):
        self._tmpdir.cleanup()

    def _instance(self, slug: str, port: int) -> str:
        return create_instance(
            self.db,
            slug=slug,
            name=slug,
            port=port,
            db_profile_id=self.profile_id,
            git_branch="main",
            user_id=self.user_id,
        )

    def test_next_free_port_fills_gaps_and_stops_at_range(self):
        self.assertEqual(next_free_port(self.db, 3100, 3102), 3100)
        first = self._instance("a", 3100)
        self._instance("c", 3102)
        self.assertEqual(next_free_port(self.db, 3100, 3102), 3101)
        self.assertEqual(next_free_port(self.db, 3100, 3102, exclude_id=first), 3100)
        self._instance("b", 3101)
        self.assertIsNone(next_free_port(self.db, 3100, 3102))
        owner = port_owner(self.db, 3100)
        self.assertIsNotNone(owner)
        self.assertEqual(owner["slug"], "a")
        self.assertIsNone(port_owner(self.db, 3100, exclude_id=first))
