import unittest

from auction.features import AuctionFeatureEngine
from config.thresholds import ThresholdRegistry
from domain.enums import AuthenticityState, FakeStrongFlag
from tests.helpers import make_ticks


class FeatureEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = AuctionFeatureEngine(ThresholdRegistry.load("config/thresholds/one_to_two.yaml"))
        self.common = dict(
            ticker="000001.SZ", expected_gap_q50=3.0, expected_gap_q25=2.0, expected_gap_q75=4.0,
            historical_amounts=[100_000, 200_000, 300_000], historical_volumes=[10_000, 20_000, 30_000],
            peer_amounts=[100_000, 200_000, 300_000], peer_volumes=[10_000, 20_000, 30_000],
            theme_peer_gaps=[1.0, 2.0, 3.0], global_peer_gaps=[0.0, 1.0, 2.0], height_peer_gaps=[1.0, 2.0],
        )

    def test_pre20_false_peak_is_mirage(self):
        result = self.engine.compute(ticks=make_ticks([("09:16:00", 2.0), ("09:18:00", 5.5), ("09:20:00", 2.5), ("09:23:00", 2.7), ("09:25:00", 2.8)]), **self.common)
        self.assertIn(FakeStrongFlag.PRE20_MIRAGE, result.fake_strong_flags)
        self.assertEqual(result.authenticity_state, AuthenticityState.SUSPICIOUS)

    def test_post20_continuous_decay_is_fake(self):
        result = self.engine.compute(ticks=make_ticks([("09:19:00", 4.0), ("09:20:00", 5.2), ("09:21:00", 4.8), ("09:22:00", 4.3), ("09:23:00", 3.8), ("09:25:00", 3.2)]), **self.common)
        self.assertIn(FakeStrongFlag.POST20_CONTINUOUS_DECAY, result.fake_strong_flags)
        self.assertEqual(result.authenticity_state, AuthenticityState.FAKE_STRONG)

    def test_drawdown_requires_high_before_low(self):
        result = self.engine.compute(ticks=make_ticks([("09:20:00", 1.0), ("09:22:00", 2.0), ("09:25:00", 4.0)]), **self.common)
        self.assertEqual(result.values["Post20MDD"], 0.0)

    def test_disagreement_then_recovery_is_healthy(self):
        result = self.engine.compute(ticks=make_ticks([("09:19:00", 3.5), ("09:20:00", 3.2), ("09:21:00", 5.0), ("09:22:00", 3.0), ("09:24:00", 4.2), ("09:25:00", 4.8)]), **self.common)
        self.assertEqual(result.authenticity_state, AuthenticityState.HEALTHY_DISAGREEMENT)

    def test_historical_and_peer_liquidity_percentiles_are_distinct(self):
        result = self.engine.compute(ticks=make_ticks([("09:20:00", 2.0), ("09:25:00", 3.0)]), **self.common)
        self.assertIn("HistoricalAmountPercentile", result.values)
        self.assertIn("PeerAmountPercentile", result.values)
        self.assertIn("PeerVolumePercentile", result.values)


if __name__ == "__main__":
    unittest.main()
