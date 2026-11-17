import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import yaml

from app import deps
from app.degraded import clear_degraded
from app.deps import (
    attempt_degraded_recovery,
    enter_degraded_mode,
    flush_metrics_buffer,
    get_settings,
)
from app.host_metrics_db import HostMetricsStore
from app.services.host_metrics import HostMetricsSnapshot
from app.services.host_metrics_recorder import HostMetricsRecorder


def _write_test_config(path: Path, *, data_dir: str) -> None:
    base = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config.example.yaml").read_text(encoding="utf-8")
    )
    base.setdefault("manager", {})
    base["manager"]["data_dir"] = data_dir
    base["manager"]["bootstrap_admin_password"] = "adminadmin"
    base["manager"]["skip_worker"] = True
    base["manager"]["secret_key"] = "test-secret"
    path.write_text(yaml.safe_dump(base, allow_unicode=True), encoding="utf-8")


class DegradedMetricsBufferTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._cfg = Path(self._tmp.name) / "config.yaml"
        _write_test_config(self._cfg, data_dir=self._tmp.name)
        os.environ["CONFIG_FILE"] = str(self._cfg)
        get_settings.cache_clear()
        deps._metrics = None
        deps._metrics_recorder = None
        deps._metrics_store = None
        clear_degraded()

    def tearDown(self):
        deps.shutdown_host_metrics_recorder()
        clear_degraded()
        get_settings.cache_clear()
        os.environ.pop("CONFIG_FILE", None)
        self._tmp.cleanup()
        deps._metrics = None
        deps._metrics_store = None
        deps._worker = None

    def test_recorder_memory_mode_appends_buffer(self):
        settings = get_settings()
        collector = mock.Mock()
        collector.snapshot.return_value = HostMetricsSnapshot(
            cpu_percent=10.0,
            cpu_count=4,
            memory_percent=20.0,
            memory_used_gb=1.0,
            memory_total_gb=8.0,
            load_1=0.5,
            load_5=0.4,
            load_15=0.3,
            disks=[{"path": "/", "percent": 50.0, "used_gb": 1.0, "total_gb": 2.0, "free_gb": 1.0, "free_bytes": 1}],
            ts=time.time(),
        )
        rec = HostMetricsRecorder(settings, collector, store=None)
        rec._record_once()
        self.assertEqual(rec.buffered_count(), 1)
        self.assertIsNone(rec._store)

    def test_flush_to_store_writes_sqlite(self):
        settings = get_settings()
        collector = mock.Mock()
        ts = 1700000000.0
        collector.snapshot.return_value = HostMetricsSnapshot(
            cpu_percent=11.0,
            cpu_count=1,
            memory_percent=22.0,
            memory_used_gb=1.0,
            memory_total_gb=8.0,
            load_1=1.1,
            load_5=1.0,
            load_15=0.9,
            disks=[],
            ts=ts,
        )
        store_path = Path(self._tmp.name) / "host_metrics.sqlite"
        store = HostMetricsStore(store_path)
        rec = HostMetricsRecorder(settings, collector, store=None)
        rec._record_once()
        self.assertEqual(rec.flush_to_store(store), 1)
        self.assertEqual(rec.buffered_count(), 0)
        with store.connection() as conn:
            n = conn.execute("SELECT COUNT(*) FROM metric_samples").fetchone()[0]
        self.assertEqual(n, 4)

    def test_recovery_flushes_degraded_buffer(self):
        settings = get_settings()
        enter_degraded_mode(
            reason="test",
            db_path=settings.db_path,
            disk_full=False,
        )
        deps._metrics_recorder._record_once()
        self.assertEqual(deps._metrics_recorder.buffered_count(), 1)
        self.assertTrue(attempt_degraded_recovery(settings))
        store_path = settings.host_metrics_db_path
        self.assertTrue(store_path.exists())
        store = HostMetricsStore(store_path)
        with store.connection() as conn:
            n = conn.execute("SELECT COUNT(*) FROM metric_samples").fetchone()[0]
        self.assertGreaterEqual(n, 4)
        self.assertEqual(deps._metrics_recorder.buffered_count(), 0)

    def test_flush_metrics_buffer_failure_returns_negative(self):
        settings = get_settings()
        deps._metrics_store = None
        enter_degraded_mode(reason="x", db_path=settings.db_path, disk_full=False)
        deps._metrics_recorder.stop()
        deps._metrics_recorder._record_once()
        before = deps._metrics_recorder.buffered_count()
        with mock.patch("app.deps.HostMetricsStore", side_effect=sqlite3.OperationalError("disk full")):
            self.assertEqual(flush_metrics_buffer(settings), -1)
        self.assertEqual(deps._metrics_recorder.buffered_count(), before)


if __name__ == "__main__":
    unittest.main()