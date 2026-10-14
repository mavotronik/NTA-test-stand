import unittest
from pathlib import Path

from app.services.host_metrics import HostMetricsCollector


class HostMetricsTests(unittest.TestCase):
    def test_snapshot_shape(self):
        proc = Path("/proc")
        if not proc.exists():
            self.skipTest("no /proc")
        col = HostMetricsCollector(proc, ["/"])
        snap = col.snapshot()
        self.assertGreaterEqual(snap.cpu_percent, 0.0)
        self.assertLessEqual(snap.cpu_percent, 100.0)
        self.assertGreaterEqual(snap.memory_total_gb, 0)
        self.assertGreaterEqual(snap.load_1, 0.0)
        d = col.as_dict()
        self.assertIn("disks", d)
        self.assertIn("load_1", d)


if __name__ == "__main__":
    unittest.main()
