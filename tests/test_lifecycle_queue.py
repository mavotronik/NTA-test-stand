import tempfile
import unittest
from pathlib import Path

from app.db import Database, new_id, utc_now
from app.repositories import (
    create_instance,
    queue_job,
    save_db_profile,
    INSTANCE_ACTIVE_JOB_TYPES,
)


class LifecycleQueueTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = self._tmpdir.__enter__()
        self.db = Database(Path(self._tmp) / "test.sqlite")
        now = utc_now()
        with self.db.connection() as conn:
            conn.execute(
                "INSERT INTO users(id, username, password_hash, role, active, created_at, updated_at) VALUES (?,?,?,?,1,?,?)",
                (new_id(), "admin", "x", "admin", now, now),
            )
            self.uid = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
        self.pid = save_db_profile(
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
            slug="life",
            name="Life",
            port=3100,
            db_profile_id=self.pid,
            git_branch="main",
            user_id=self.uid,
        )

    def tearDown(self):
        self._tmpdir.__exit__(None, None, None)

    def test_queue_lifecycle_job_types(self):
        for jt in ("instance_stop", "instance_start", "instance_delete", "instance_rollback"):
            with self.db.connection() as conn:
                conn.execute("DELETE FROM deployment_jobs")
            jid = queue_job(self.db, self.iid, jt, self.uid)
            self.assertTrue(jid)
            with self.db.connection() as conn:
                row = conn.execute(
                    "SELECT job_type, status FROM deployment_jobs WHERE id=?", (jid,)
                ).fetchone()
            self.assertEqual(row["job_type"], jt)
            self.assertEqual(row["status"], "queued")

    def test_conflicting_active_job(self):
        queue_job(self.db, self.iid, "instance_stop", self.uid)
        with self.assertRaises(ValueError):
            queue_job(self.db, self.iid, "instance_start", self.uid)

    def test_active_types_include_lifecycle(self):
        self.assertIn("instance_delete", INSTANCE_ACTIVE_JOB_TYPES)


if __name__ == "__main__":
    unittest.main()
