import unittest
from datetime import datetime, timedelta, timezone

from config.thresholds import ThresholdRegistry
from expectation.benchmark import BenchmarkKey, BenchmarkObservation, FeaturesAsOf, HierarchicalConditionalBenchmark
from market.asof import AsOfMarketView


class ExpectationTests(unittest.TestCase):
    def setUp(self):
        self.thresholds = ThresholdRegistry.load("config/thresholds/expectation.yaml")
        self.tz = timezone(timedelta(hours=8))
        self.cutoff = datetime(2026, 8, 14, 8, 0, tzinfo=self.tz)

    def observation(self, index, key, gap, available, trade_date="2026-08-13"):
        return BenchmarkObservation(
            f"obs-{index}",
            trade_date,
            f"0000{index:02d}.SZ",
            FeaturesAsOf(key.feature_mapping(), datetime(2026, 8, 12, 15, 0, tzinfo=self.tz), "FEATURES_V1"),
            datetime(2026, 8, 12, 23, 0, tzinfo=self.tz),
            key,
            "TEST_KEY_V1",
            gap,
            1_000_000 + gap * 10_000,
            100_000 + gap * 1_000,
            available,
            "TEST_SOURCE_V1",
        )

    def view(self, observations):
        return AsOfMarketView("2026-08-14", self.cutoff, expectation_observations=observations)

    def test_hierarchical_benchmark_returns_shrunk_quantiles_and_metadata(self):
        leaf_key = BenchmarkKey("ONE_TO_TWO", "RISK_ON", "MAIN", "LEADER", "H1", "CHANGE_HAND")
        sibling_key = BenchmarkKey("ONE_TO_TWO", "RISK_ON", "MAIN", "LEADER", "H1", "T_BOARD")
        available = datetime(2026, 8, 13, 15, 0, tzinfo=self.tz)
        observations = [self.observation(index, leaf_key, gap, available) for index, gap in enumerate((2, 3, 4, 5, 6), 1)]
        observations += [self.observation(index + 10, sibling_key, gap, available) for index, gap in enumerate((-1, 0, 1, 2), 1)]
        result = HierarchicalConditionalBenchmark(self.thresholds).estimate(
            self.view(observations), leaf_key, "2026-08-14", self.cutoff,
        )
        self.assertEqual(result.sample_size, 5)
        self.assertEqual(result.parent_sample_size, 9)
        self.assertAlmostEqual(result.shrinkage_weight, 5 / 15)
        self.assertLessEqual(result.expected_gap_q10, result.expected_gap_q50)
        self.assertLessEqual(result.expected_gap_q50, result.expected_gap_q90)

    def test_future_and_current_trade_date_observations_are_excluded(self):
        key = BenchmarkKey("ONE_TO_TWO", "RISK_ON", "MAIN", "LEADER", "H1", "CHANGE_HAND")
        before = datetime(2026, 8, 13, 15, 0, tzinfo=self.tz)
        future = datetime(2026, 8, 14, 9, 26, tzinfo=self.tz)
        observations = (
            self.observation(1, key, 2.0, before, "2026-08-13"),
            self.observation(2, key, 99.0, future, "2026-08-14"),
        )
        result = HierarchicalConditionalBenchmark(self.thresholds).estimate(
            self.view(observations), key, "2026-08-14", self.cutoff,
        )
        self.assertEqual(result.sample_size, 1)
        self.assertEqual(result.expected_gap_q50, 2.0)

    def test_small_leaf_falls_back_to_parent_reaching_minimum(self):
        leaf = BenchmarkKey("ONE_TO_TWO", "RISK_ON", "MAIN", "LEADER", "H1", "CHANGE_HAND")
        sibling = BenchmarkKey("ONE_TO_TWO", "RISK_ON", "MAIN", "LEADER", "H1", "T_BOARD")
        available = datetime(2026, 8, 13, 15, 0, tzinfo=self.tz)
        observations = [self.observation(1, leaf, 8.0, available)]
        observations += [self.observation(index, sibling, float(index), available) for index in range(2, 7)]
        result = HierarchicalConditionalBenchmark(self.thresholds).estimate(
            self.view(observations), leaf, "2026-08-14", self.cutoff,
        )
        self.assertEqual(result.sample_size, 6)
        self.assertLess(result.peer_similarity, 1.0)


if __name__ == "__main__":
    unittest.main()
