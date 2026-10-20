import tempfile
import unittest
from pathlib import Path

from app.host_metrics_db import HostMetricsStore, _downsample_points


class HostMetricsHistoryTests(unittest.TestCase):
    def test_insert_query_and_prune(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HostMetricsStore(Path(tmp) / "host_metrics.sqlite")
            store.insert_samples(1000.0, {"cpu": 10.0, "memory": 20.0})
            store.insert_samples(1005.0, {"cpu": 30.0, "load": 1.2})
            rows = store.query_series("cpu", 999.0, 1010.0, max_points=400)
            self.assertEqual(len(rows), 2)
            self.assertAlmostEqual(rows[0]["value"], 10.0)
            self.assertAlmostEqual(rows[1]["value"], 30.0)
            deleted = store.prune_older_than(1003.0)
            self.assertEqual(deleted, 2)
            rows_after = store.query_series("cpu", 0, 2000, max_points=400)
            self.assertEqual(len(rows_after), 1)
            self.assertAlmostEqual(rows_after[0]["value"], 30.0)

    def test_downsample(self):
        rows = [(float(i), float(i)) for i in range(100)]
        out = _downsample_points(rows, 10)
        self.assertEqual(len(out), 10)
        self.assertGreater(out[-1][0], out[0][0])
        self.assertAlmostEqual(out[-1][1], 94.5)

    def test_unknown_metric_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HostMetricsStore(Path(tmp) / "host_metrics.sqlite")
            self.assertEqual(store.query_series("unknown", 0, 1), [])


if __name__ == "__main__":
    unittest.main()
