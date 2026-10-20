from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

METRIC_NAMES = frozenset({"cpu", "memory", "disk", "load"})

_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS metric_samples (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  metric TEXT NOT NULL,
  value REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_metric_samples_metric_ts ON metric_samples(metric, ts);
CREATE INDEX IF NOT EXISTS idx_metric_samples_ts ON metric_samples(ts);
"""


def _downsample_points(
    rows: list[tuple[float, float]],
    max_points: int,
) -> list[tuple[float, float]]:
    if len(rows) <= max_points or max_points < 1:
        return rows
    if max_points == 1:
        return [rows[-1]]
    n = len(rows)
    out: list[tuple[float, float]] = []
    for i in range(max_points):
        start = int(i * n / max_points)
        end = int((i + 1) * n / max_points)
        if end <= start:
            end = min(start + 1, n)
        bucket = rows[start:end]
        if not bucket:
            continue
        avg_ts = sum(p[0] for p in bucket) / len(bucket)
        avg_val = sum(p[1] for p in bucket) / len(bucket)
        out.append((avg_ts, avg_val))
    return out


class HostMetricsStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30.0)
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

    def _init_schema(self) -> None:
        with self._lock:
            with self.connection() as conn:
                conn.executescript(_SCHEMA)

    def insert_samples(self, ts: float, values: dict[str, float]) -> None:
        rows = [(ts, name, float(val)) for name, val in values.items() if name in METRIC_NAMES]
        if not rows:
            return
        with self._lock:
            with self.connection() as conn:
                conn.executemany(
                    "INSERT INTO metric_samples(ts, metric, value) VALUES (?, ?, ?)",
                    rows,
                )

    def prune_older_than(self, cutoff_ts: float) -> int:
        with self._lock:
            with self.connection() as conn:
                cur = conn.execute(
                    "DELETE FROM metric_samples WHERE ts < ?",
                    (cutoff_ts,),
                )
                return cur.rowcount

    def query_series(
        self,
        metric: str,
        since_ts: float,
        until_ts: float,
        *,
        max_points: int = 400,
    ) -> list[dict[str, float]]:
        if metric not in METRIC_NAMES:
            return []
        with self._lock:
            with self.connection() as conn:
                cur = conn.execute(
                    """
                    SELECT ts, value FROM metric_samples
                    WHERE metric = ? AND ts >= ? AND ts <= ?
                    ORDER BY ts ASC
                    """,
                    (metric, since_ts, until_ts),
                )
                raw = [(float(r[0]), float(r[1])) for r in cur.fetchall()]
        down = _downsample_points(raw, max_points)
        return [{"ts": ts, "value": round(val, 4)} for ts, val in down]
