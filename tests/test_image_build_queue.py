import tempfile
import unittest
from pathlib import Path

from app.db import Database, new_id, utc_now
from app.repositories import queue_image_build


class ImageBuildQueueTests(unittest.TestCase):
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

    def tearDown(self):
        self._tmpdir.__exit__(None, None, None)

    def test_queue_image_build_creates_job_without_instance(self):
        jid = queue_image_build(
            self.db,
            self.user_id,
            source_mode="repo",
            source_path="",
            git_branch="main",
            git_commit="",
            git_tag="",
        )
        with self.db.connection() as conn:
            job = conn.execute("SELECT * FROM deployment_jobs WHERE id=?", (jid,)).fetchone()
            build = conn.execute("SELECT * FROM builds WHERE id=?", (job["build_id"],)).fetchone()
        self.assertEqual(job["job_type"], "image_build")
        self.assertIsNone(job["instance_id"])
        self.assertEqual(build["status"], "pending")
        self.assertEqual(build["source_mode"], "repo")

    def test_second_image_build_rejected_while_active(self):
        queue_image_build(
            self.db,
            self.user_id,
            source_mode="repo",
            source_path="",
            git_branch="main",
            git_commit="",
            git_tag="",
        )
        with self.assertRaises(ValueError):
            queue_image_build(
                self.db,
                self.user_id,
                source_mode="repo",
                source_path="",
                git_branch="main",
                git_commit="",
                git_tag="",
            )


if __name__ == "__main__":
    unittest.main()
