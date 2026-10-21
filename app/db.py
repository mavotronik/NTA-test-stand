from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 3

MIGRATION_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('admin','operator','viewer')),
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  csrf_token TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS db_profiles (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  host TEXT NOT NULL,
  port INTEGER NOT NULL DEFAULT 8123,
  database_name TEXT NOT NULL DEFAULT 'default',
  username TEXT NOT NULL,
  password_enc TEXT NOT NULL,
  tls INTEGER NOT NULL DEFAULT 0,
  writable INTEGER NOT NULL DEFAULT 0,
  comment TEXT NOT NULL DEFAULT '',
  disabled INTEGER NOT NULL DEFAULT 0,
  last_check_ok INTEGER,
  last_check_at TEXT,
  last_check_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instances (
  id TEXT PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  port INTEGER NOT NULL UNIQUE,
  state TEXT NOT NULL,
  db_profile_id TEXT NOT NULL REFERENCES db_profiles(id),
  git_branch TEXT NOT NULL,
  git_commit TEXT NOT NULL DEFAULT '',
  git_tag TEXT NOT NULL DEFAULT '',
  source_mode TEXT NOT NULL DEFAULT 'repo',
  source_path TEXT NOT NULL DEFAULT '',
  image_tag TEXT NOT NULL DEFAULT '',
  container_id TEXT NOT NULL DEFAULT '',
  config_version INTEGER NOT NULL DEFAULT 1,
  rollback_image_tag TEXT NOT NULL DEFAULT '',
  rollback_config_json TEXT NOT NULL DEFAULT '{}',
  created_by TEXT REFERENCES users(id),
  updated_by TEXT REFERENCES users(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instance_configs (
  id TEXT PRIMARY KEY,
  instance_id TEXT NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  config_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  created_by TEXT REFERENCES users(id),
  UNIQUE(instance_id, version)
);

CREATE TABLE IF NOT EXISTS builds (
  id TEXT PRIMARY KEY,
  build_key TEXT NOT NULL,
  git_commit TEXT NOT NULL,
  status TEXT NOT NULL,
  image_tag TEXT NOT NULL DEFAULT '',
  image_id TEXT NOT NULL DEFAULT '',
  log_path TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  source_mode TEXT NOT NULL DEFAULT 'repo',
  source_path TEXT NOT NULL DEFAULT '',
  git_branch TEXT NOT NULL DEFAULT 'main',
  git_tag TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_builds_key ON builds(build_key);
CREATE INDEX IF NOT EXISTS idx_builds_status ON builds(status);

CREATE TABLE IF NOT EXISTS deployment_jobs (
  id TEXT PRIMARY KEY,
  instance_id TEXT REFERENCES instances(id) ON DELETE CASCADE,
  job_type TEXT NOT NULL,
  status TEXT NOT NULL,
  stage TEXT NOT NULL DEFAULT '',
  build_id TEXT REFERENCES builds(id),
  requested_by TEXT REFERENCES users(id),
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_instance ON deployment_jobs(instance_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON deployment_jobs(status);

CREATE TABLE IF NOT EXISTS job_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL REFERENCES deployment_jobs(id) ON DELETE CASCADE,
  line TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_logs_job ON job_logs(job_id);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT,
  username TEXT,
  role TEXT,
  action TEXT NOT NULL,
  target_type TEXT,
  target_id TEXT,
  detail_json TEXT NOT NULL DEFAULT '{}',
  ip TEXT,
  ok INTEGER NOT NULL DEFAULT 1,
  error TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._schema_lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(instances)").fetchall()}
        if "source_mode" not in cols:
            conn.execute(
                "ALTER TABLE instances ADD COLUMN source_mode TEXT NOT NULL DEFAULT 'repo'"
            )
        if "source_path" not in cols:
            conn.execute(
                "ALTER TABLE instances ADD COLUMN source_path TEXT NOT NULL DEFAULT ''"
            )
        if "git_tag" not in cols:
            conn.execute(
                "ALTER TABLE instances ADD COLUMN git_tag TEXT NOT NULL DEFAULT ''"
            )

        build_cols = {row[1] for row in conn.execute("PRAGMA table_info(builds)").fetchall()}
        if "source_mode" not in build_cols:
            conn.execute(
                "ALTER TABLE builds ADD COLUMN source_mode TEXT NOT NULL DEFAULT 'repo'"
            )
        if "source_path" not in build_cols:
            conn.execute(
                "ALTER TABLE builds ADD COLUMN source_path TEXT NOT NULL DEFAULT ''"
            )
        if "git_branch" not in build_cols:
            conn.execute(
                "ALTER TABLE builds ADD COLUMN git_branch TEXT NOT NULL DEFAULT 'main'"
            )
        if "git_tag" not in build_cols:
            conn.execute(
                "ALTER TABLE builds ADD COLUMN git_tag TEXT NOT NULL DEFAULT ''"
            )

        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        job_cols = {}
        if "deployment_jobs" in tables:
            job_cols = {
                row[1]: row
                for row in conn.execute("PRAGMA table_info(deployment_jobs)").fetchall()
            }
        needs_nullable = bool(
            job_cols and job_cols.get("instance_id", (None, None, None, 1))[3] == 1
        )
        if "deployment_jobs_v3" in tables or needs_nullable:
            self._migrate_deployment_jobs_nullable_instance(conn)

    @staticmethod
    def _migrate_deployment_jobs_nullable_instance(conn: sqlite3.Connection) -> None:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        has_old = "deployment_jobs" in tables
        has_v3 = "deployment_jobs_v3" in tables

        if has_v3 and has_old:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("DROP TABLE deployment_jobs")
            conn.execute("ALTER TABLE deployment_jobs_v3 RENAME TO deployment_jobs")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_instance ON deployment_jobs(instance_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status ON deployment_jobs(status)"
            )
            conn.execute("PRAGMA foreign_keys=ON")
            return

        if has_v3 and not has_old:
            conn.execute("ALTER TABLE deployment_jobs_v3 RENAME TO deployment_jobs")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_instance ON deployment_jobs(instance_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status ON deployment_jobs(status)"
            )
            return

        if not has_old:
            return

        conn.executescript(
            """
            PRAGMA foreign_keys=OFF;
            CREATE TABLE deployment_jobs_v3 (
              id TEXT PRIMARY KEY,
              instance_id TEXT REFERENCES instances(id) ON DELETE CASCADE,
              job_type TEXT NOT NULL,
              status TEXT NOT NULL,
              stage TEXT NOT NULL DEFAULT '',
              build_id TEXT REFERENCES builds(id),
              requested_by TEXT REFERENCES users(id),
              error TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              started_at TEXT,
              finished_at TEXT
            );
            INSERT INTO deployment_jobs_v3(
              id, instance_id, job_type, status, stage, build_id, requested_by,
              error, created_at, started_at, finished_at
            )
            SELECT
              id, instance_id, job_type, status, stage, build_id, requested_by,
              error, created_at, started_at, finished_at
            FROM deployment_jobs;
            DROP TABLE deployment_jobs;
            ALTER TABLE deployment_jobs_v3 RENAME TO deployment_jobs;
            CREATE INDEX IF NOT EXISTS idx_jobs_instance ON deployment_jobs(instance_id);
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON deployment_jobs(status);
            PRAGMA foreign_keys=ON;
            """
        )

    def _init_schema(self) -> None:
        with self._schema_lock:
            with self.connection() as conn:
                conn.executescript(MIGRATION_SQL)
                self._migrate_schema(conn)
                row = conn.execute(
                    "SELECT value FROM schema_meta WHERE key='version'"
                ).fetchone()
                if not row:
                    conn.execute(
                        "INSERT INTO schema_meta(key,value) VALUES('version',?)",
                        (str(SCHEMA_VERSION),),
                    )
                else:
                    conn.execute(
                        "UPDATE schema_meta SET value=? WHERE key='version'",
                        (str(SCHEMA_VERSION),),
                    )



def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
