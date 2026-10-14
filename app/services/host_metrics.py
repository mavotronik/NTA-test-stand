from __future__ import annotations

import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import psutil


@dataclass
class HostMetricsSnapshot:
    cpu_percent: float
    memory_percent: float
    memory_used_gb: float
    memory_total_gb: float
    load_1: float
    load_5: float
    load_15: float
    disks: list[dict[str, Any]]
    ts: float


def _read_loadavg(host_proc: Path) -> tuple[float, float, float]:
    path = host_proc / "loadavg"
    if path.exists():
        parts = path.read_text(encoding="utf-8").split()
        return float(parts[0]), float(parts[1]), float(parts[2])
    la = os.getloadavg()
    return la[0], la[1], la[2]


def _read_meminfo(host_proc: Path) -> tuple[float, float, float]:
    mem_path = host_proc / "meminfo"
    if mem_path.exists():
        info: dict[str, int] = {}
        for line in mem_path.read_text(encoding="utf-8").splitlines():
            key, _, rest = line.partition(":")
            if not rest:
                continue
            val = rest.strip().split()[0]
            info[key] = int(val) * 1024
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        used = max(total - avail, 0)
        pct = (used / total * 100) if total else 0.0
        return pct, used / (1024**3), total / (1024**3)
    vm = psutil.virtual_memory()
    return vm.percent, vm.used / (1024**3), vm.total / (1024**3)


def _read_cpu_percent(host_proc: Path, last_idle: float | None, last_total: float | None) -> tuple[float, float, float]:
    stat_path = host_proc / "stat"
    if stat_path.exists():
        line = stat_path.read_text(encoding="utf-8").splitlines()[0]
        parts = line.split()
        # cpu user nice system idle iowait irq softirq steal guest guest_nice
        nums = [float(x) for x in parts[1:]]
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        total = sum(nums)
        if last_idle is not None and last_total is not None:
            dt = total - last_total
            didle = idle - last_idle
            usage = (1.0 - didle / dt) * 100 if dt > 0 else 0.0
        else:
            usage = 0.0
        return max(0.0, min(100.0, usage)), idle, total
    return psutil.cpu_percent(interval=0.2), 0.0, 0.0


def _disk_usage_paths(paths: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in paths:
        try:
            p = str(Path(raw).resolve())
        except OSError:
            continue
        if p in seen:
            continue
        seen.add(p)
        try:
            du = psutil.disk_usage(p)
        except (OSError, PermissionError):
            continue
        out.append({
            "path": p,
            "percent": round(du.percent, 1),
            "used_gb": round(du.used / (1024**3), 2),
            "total_gb": round(du.total / (1024**3), 2),
        })
    return out


class HostMetricsCollector:
    def __init__(self, host_proc: Path, disk_paths: list[str]) -> None:
        self.host_proc = host_proc if (host_proc / "stat").exists() else Path("/proc")
        self.disk_paths = disk_paths
        self._last_idle: float | None = None
        self._last_total: float | None = None

    def snapshot(self) -> HostMetricsSnapshot:
        load_1, load_5, load_15 = _read_loadavg(self.host_proc)
        mem_pct, mem_used, mem_total = _read_meminfo(self.host_proc)
        cpu, idle, total = _read_cpu_percent(self.host_proc, self._last_idle, self._last_total)
        self._last_idle, self._last_total = idle, total
        return HostMetricsSnapshot(
            cpu_percent=round(cpu, 1),
            memory_percent=round(mem_pct, 1),
            memory_used_gb=round(mem_used, 2),
            memory_total_gb=round(mem_total, 2),
            load_1=round(load_1, 2),
            load_5=round(load_5, 2),
            load_15=round(load_15, 2),
            disks=_disk_usage_paths(self.disk_paths),
            ts=time.time(),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self.snapshot())
