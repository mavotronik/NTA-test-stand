import tempfile
import unittest
from pathlib import Path

from app.db import Database, new_id, utc_now
from app.repositories import (
    create_instance,
    delete_db_profile,
    delete_instance,
    fail_orphan_running_jobs,
    list_builds,
    reconcile_stale_builds,
    save_db_profile,
)


class BuildRepositoryTests(unittest.TestCase):
    def _db(self) -> Database:
        path = Path(self._tmp) / "test.sqlite"
        return Database(path)

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = self._tmpdir.__enter__()
        self.db = self._db()
        now = utc_now()
        with self.db.connection() as conn:
            conn.execute(
                "INSERT INTO users(id, username, password_hash, role, active, created_at, updated_at) VALUES (?,?,?,?,1,?,?)",
                (new_id(), "admin", "x", "admin", now, now),
            )

    def tearDown(self):
        self._tmpdir.__exit__(None, None, None)

    def test_reconcile_building_when_job_failed(self):
        bid = new_id()
        iid = new_id()
        jid = new_id()
        pid = new_id()
        now = utc_now()
        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO db_profiles(id, name, host, port, database_name, username, password_enc,
                  tls, writable, comment, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,0,0,'',?,?)
                """,
                (pid, "p", "127.0.0.1", 8123, "default", "u", "enc", now, now),
            )
            conn.execute(
                """
                INSERT INTO instances(id, slug, name, port, state, db_profile_id, git_branch,
                  created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (iid, "demo", "Demo", 3100, "stopped", pid, "main", now, now),
            )
            conn.execute(
                """
                INSERT INTO builds(id, build_key, git_commit, status, image_tag, created_at)
                VALUES (?,?,?,?,?,?)
                """,
                (bid, "key", "abc" * 10 + "abcd", "building", "tag", now),
            )
            conn.execute(
                """
                INSERT INTO deployment_jobs(id, instance_id, job_type, status, stage, build_id, created_at, error)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (jid, iid, "build", "failed", "error", bid, now, "docker failed"),
            )
        reconcile_stale_builds(self.db)
        builds = list_builds(self.db)
        self.assertEqual(builds[0]["status"], "failed")
        self.assertEqual(builds[0]["display_status"], "failed")

    def test_delete_db_profile_blocked_when_in_use(self):
        uid = new_id()
        now = utc_now()
        with self.db.connection() as conn:
            row = conn.execute("SELECT id FROM users LIMIT 1").fetchone()
            uid = row["id"]
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
        create_instance(
            self.db,
            slug="x",
            name="X",
            port=3101,
            db_profile_id=pid,
            git_branch="main",
            user_id=uid,
        )
        with self.assertRaises(ValueError):
            delete_db_profile(self.db, pid)

    def test_delete_instance(self):
        uid = new_id()
        with self.db.connection() as conn:
            uid = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
        pid = save_db_profile(
            self.db,
            profile_id=None,
            name="ch2",
            host="127.0.0.1",
            port=8123,
            database_name="default",
            username="u",
            password_enc="enc",
            tls=False,
            writable=False,
            comment="",
        )
        iid = create_instance(
            self.db,
            slug="y",
            name="Y",
            port=3102,
            db_profile_id=pid,
            git_branch="main",
            user_id=uid,
        )
        delete_instance(self.db, iid)
        with self.db.connection() as conn:
            n = conn.execute("SELECT COUNT(*) AS c FROM instances").fetchone()["c"]
        self.assertEqual(n, 0)

    def test_delete_instance_with_completing_job_clears_jobs(self):
        uid = new_id()
        with self.db.connection() as conn:
            uid = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
        pid = save_db_profile(
            self.db,
            profile_id=None,
            name="ch3",
            host="127.0.0.1",
            port=8123,
            database_name="default",
            username="u",
            password_enc="enc",
            tls=False,
            writable=False,
            comment="",
        )
        iid = create_instance(
            self.db,
            slug="z",
            name="Z",
            port=3103,
            db_profile_id=pid,
            git_branch="main",
            user_id=uid,
        )
        jid = new_id()
        now = utc_now()
        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO deployment_jobs(id, instance_id, job_type, status, stage, created_at)
                VALUES (?,?,?,?,?,?)
                """,
                (jid, iid, "instance_delete", "running", "stop", now),
            )
            conn.execute(
                "INSERT INTO job_logs(job_id, line, created_at) VALUES (?,?,?)",
                (jid, "removing", now),
            )
        delete_instance(self.db, iid, completing_job_id=jid)
        with self.db.connection() as conn:
            inst_n = conn.execute("SELECT COUNT(*) AS c FROM instances").fetchone()["c"]
            jobs_n = conn.execute("SELECT COUNT(*) AS c FROM deployment_jobs").fetchone()["c"]
            logs_n = conn.execute("SELECT COUNT(*) AS c FROM job_logs").fetchone()["c"]
        self.assertEqual(inst_n, 0)
        self.assertEqual(jobs_n, 0)
        self.assertEqual(logs_n, 0)

    def test_fail_orphan_running_jobs(self):
        uid = new_id()
        with self.db.connection() as conn:
            uid = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
        pid = save_db_profile(
            self.db,
            profile_id=None,
            name="ch4",
            host="127.0.0.1",
            port=8123,
            database_name="default",
            username="u",
            password_enc="enc",
            tls=False,
            writable=False,
            comment="",
        )
        iid = create_instance(
            self.db,
            slug="orphan",
            name="Orphan",
            port=3104,
            db_profile_id=pid,
            git_branch="main",
            user_id=uid,
        )
        jid = new_id()
        now = utc_now()
        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO deployment_jobs(id, instance_id, job_type, status, stage, created_at)
                VALUES (?,?,?,?,?,?)
                """,
                (jid, iid, "instance_start", "running", "prepare", now),
            )
        changed = fail_orphan_running_jobs(self.db)
        self.assertEqual(changed, 1)
        with self.db.connection() as conn:
            row = conn.execute("SELECT status, stage FROM deployment_jobs WHERE id=?", (jid,)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["stage"], "orphaned")


if __name__ == "__main__":
    unittest.main()
