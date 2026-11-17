from __future__ import annotations

import threading

from app.config import Settings
from app.host_metrics_db import HostMetricsStore
from app.services.host_metrics import HostMetricsCollector


class HostMetricsRecorder:
    def __init__(
        self,
        settings: Settings,
        collector: HostMetricsCollector,
        store: HostMetricsStore | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._collector = collector
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_prune_at = 0.0
        self._degraded = False
        self._buffer: list[tuple[float, dict[str, float]]] = []
        self._buffer_lock = threading.Lock()

    def set_store(self, store: HostMetricsStore | None) -> None:
        self._store = store

    def buffered_count(self) -> int:
        with self._buffer_lock:
            return len(self._buffer)

    def start(self, *, degraded: bool = False) -> None:
        self._degraded = degraded
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="host-metrics", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self._thread = None

    def _interval_sec(self) -> float:
        policy = self._settings.policy
        if self._degraded:
            return max(1.0, float(policy.host_metrics_degraded_refresh_sec))
        return max(1.0, float(policy.host_metrics_refresh_sec))

    def _retention_cutoff(self, now: float) -> float:
        retention_h = self._settings.policy.host_metrics_history_retention_hours
        return now - retention_h * 3600.0

    def _snapshot_values(self) -> tuple[float, dict[str, float]]:
        snap = self._collector.snapshot()
        disk_pct = snap.disks[0]["percent"] if snap.disks else 0.0
        return snap.ts, {
            "cpu": snap.cpu_percent,
            "memory": snap.memory_percent,
            "load": snap.load_1,
            "disk": disk_pct,
        }

    def _prune_buffer(self, now: float) -> None:
        cutoff = self._retention_cutoff(now)
        with self._buffer_lock:
            self._buffer = [(ts, vals) for ts, vals in self._buffer if ts >= cutoff]

    def _append_buffer(self, ts: float, values: dict[str, float]) -> None:
        with self._buffer_lock:
            self._buffer.append((ts, values))
        self._prune_buffer(ts)

    def _maybe_prune_store(self, now: float) -> None:
        if self._store is None:
            return
        if now - self._last_prune_at < 300.0:
            return
        cutoff = self._retention_cutoff(now)
        self._store.prune_older_than(cutoff)
        self._last_prune_at = now

    def _record_once(self) -> None:
        ts, values = self._snapshot_values()
        if self._degraded or self._store is None:
            self._append_buffer(ts, values)
            return
        self._store.insert_samples(ts, values)
        self._maybe_prune_store(ts)

    def flush_to_store(self, store: HostMetricsStore) -> int:
        with self._buffer_lock:
            pending = list(self._buffer)
        if not pending:
            return 0
        for ts, values in pending:
            store.insert_samples(ts, values)
        with self._buffer_lock:
            self._buffer.clear()
        if pending:
            now = pending[-1][0]
            if now - self._last_prune_at >= 300.0:
                store.prune_older_than(self._retention_cutoff(now))
                self._last_prune_at = now
        return len(pending)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._record_once()
            except Exception:
                pass
            self._stop.wait(self._interval_sec())