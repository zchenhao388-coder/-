import unittest
from datetime import datetime, timedelta, timezone

from expectation.benchmark import BenchmarkKey, BenchmarkObservation, HierarchicalConditionalBenchmark


class ExpectationTests(unittest.TestCase):
    def test_hierarchical_benchmark_returns_shrunk_quantiles_and_metadata(self):
        leaf_key = BenchmarkKey("ONE_TO_TWO", "RISK_ON", "MAIN", "LEADER", "H1", "CHANGE_HAND")
        sibling_key = BenchmarkKey("ONE_TO_TWO", "RISK_ON", "MAIN", "LEADER", "H1", "T_BOARD")
        available = datetime(2026, 8, 13, 15, 0, tzinfo=timezone(timedelta(hours=8)))
        observations = [
            BenchmarkObservation(leaf_key, gap, 1_000_000 + gap * 10_000, 100_000 + gap * 1_000, available)
            for gap in (2.0, 4.0, 6.0)
        ] + [BenchmarkObservation(sibling_key, gap, 500_000, 50_000, available) for gap in (-1.0, 0.0, 1.0, 2.0)]
        result = HierarchicalConditionalBenchmark(observations, prior_strength=3).estimate(leaf_key, datetime(2026, 8, 14, 8, 0, tzinfo=available.tzinfo))
        self.assertEqual(result.sample_size, 3)
        self.assertEqual(result.parent_sample_size, 7)
        self.assertAlmostEqual(result.shrinkage_weight, .5)
        self.assertLessEqual(result.expected_gap_q10, result.expected_gap_q50)
        self.assertLessEqual(result.expected_gap_q50, result.expected_gap_q90)

    def test_future_observation_is_excluded(self):
        key = BenchmarkKey("ONE_TO_TWO", "RISK_ON", "MAIN", "LEADER", "H1", "CHANGE_HAND")
        tz = timezone(timedelta(hours=8))
        before = datetime(2026, 8, 13, 15, 0, tzinfo=tz)
        future = datetime(2026, 8, 14, 9, 26, tzinfo=tz)
        model = HierarchicalConditionalBenchmark([
            BenchmarkObservation(key, 2.0, 100.0, 10.0, before),
            BenchmarkObservation(key, 99.0, 999.0, 99.0, future),
        ], prior_strength=1)
        result = model.estimate(key, datetime(2026, 8, 14, 9, 0, tzinfo=tz))
        self.assertEqual(result.sample_size, 1)
        self.assertEqual(result.expected_gap_q50, 2.0)


if __name__ == "__main__":
    unittest.main()
