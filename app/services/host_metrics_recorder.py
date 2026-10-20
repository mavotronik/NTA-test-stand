from __future__ import annotations

import threading
import time

from app.config import Settings
from app.host_metrics_db import HostMetricsStore
from app.services.host_metrics import HostMetricsCollector


class HostMetricsRecorder:
    def __init__(
        self,
        settings: Settings,
        store: HostMetricsStore,
        collector: HostMetricsCollector,
    ) -> None:
        self._settings = settings
        self._store = store
        self._collector = collector
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_prune_at = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="host-metrics", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _maybe_prune(self, now: float) -> None:
        if now - self._last_prune_at < 300.0:
            return
        retention_h = self._settings.policy.host_metrics_history_retention_hours
        cutoff = now - retention_h * 3600.0
        self._store.prune_older_than(cutoff)
        self._last_prune_at = now

    def _record_once(self) -> None:
        snap = self._collector.snapshot()
        disk_pct = snap.disks[0]["percent"] if snap.disks else 0.0
        self._store.insert_samples(
            snap.ts,
            {
                "cpu": snap.cpu_percent,
                "memory": snap.memory_percent,
                "load": snap.load_1,
                "disk": disk_pct,
            },
        )
        self._maybe_prune(snap.ts)

    def _loop(self) -> None:
        interval = max(1.0, float(self._settings.policy.host_metrics_refresh_sec))
        while not self._stop.is_set():
            try:
                self._record_once()
            except Exception:
                pass
            self._stop.wait(interval)
