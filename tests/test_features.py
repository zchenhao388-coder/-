import unittest

from auction.features import AuctionFeatureEngine
from config.thresholds import ThresholdRegistry
from domain.enums import AuthenticityState, FakeStrongFlag
from tests.helpers import make_expectation, make_ticks, make_view


class FeatureEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = AuctionFeatureEngine(ThresholdRegistry.load("config/thresholds/one_to_two.yaml"))

    def compute(self, points, include_history=True, include_peers=True):
        ticks = make_ticks(points)
        view = make_view(ticks, include_history=include_history, include_peers=include_peers)
        return self.engine.compute(
            view,
            "000001.SZ",
            make_expectation(),
            liquidity_peer_group="liquidity" if include_peers else None,
            theme_peer_group="theme" if include_peers else None,
            global_peer_group="global" if include_peers else None,
            height_peer_group="height" if include_peers else None,
        )

    def test_pre20_false_peak_is_mirage(self):
        result = self.compute([("09:16:00", 2.0), ("09:18:00", 5.5), ("09:20:00", 2.5), ("09:23:00", 2.7), ("09:25:00", 2.8)])
        self.assertIn(FakeStrongFlag.PRE20_MIRAGE, result.fake_strong_flags)
        self.assertEqual(result.authenticity_state, AuthenticityState.SUSPICIOUS)

    def test_post20_continuous_decay_is_fake(self):
        result = self.compute([("09:19:00", 4.0), ("09:20:00", 5.2), ("09:21:00", 4.8), ("09:22:00", 4.3), ("09:23:00", 3.8), ("09:25:00", 3.2)])
        self.assertIn(FakeStrongFlag.POST20_CONTINUOUS_DECAY, result.fake_strong_flags)
        self.assertEqual(result.authenticity_state, AuthenticityState.FAKE_STRONG)

    def test_drawdown_requires_high_before_low(self):
        result = self.compute([("09:20:00", 1.0), ("09:22:00", 2.0), ("09:25:00", 4.0)])
        self.assertEqual(result.values["Post20MDD"], 0.0)

    def test_disagreement_then_recovery_is_healthy_with_all_confirmations(self):
        result = self.compute([("09:19:00", 3.5), ("09:20:00", 3.2), ("09:21:00", 5.0), ("09:22:00", 3.0), ("09:24:00", 4.2), ("09:25:00", 4.8)])
        self.assertEqual(result.authenticity_state, AuthenticityState.HEALTHY_DISAGREEMENT)
        self.assertGreater(result.values["Late30Slope"], 0)
        self.assertEqual(result.values["FundingConfirmed"], 1.0)

    def test_disagreement_without_peer_confirmation_is_pending(self):
        result = self.compute([("09:20:00", 5.0), ("09:23:00", 3.0), ("09:24:00", 4.2), ("09:25:00", 4.8)], include_peers=False)
        self.assertEqual(result.authenticity_state, AuthenticityState.HEALTHY_DISAGREEMENT_PENDING)

    def test_historical_and_peer_liquidity_percentiles_are_distinct(self):
        result = self.compute([("09:20:00", 2.0), ("09:25:00", 3.0)])
        self.assertIn("HistoricalAmountPercentile", result.values)
        self.assertIn("PeerAmountPercentile", result.values)
        self.assertIn("PeerVolumePercentile", result.values)

    def test_eg_is_actual_gap_minus_expected_median(self):
        result = self.compute([("09:25:00", 5.0)])
        self.assertEqual(result.values["ActualAuctionGap"], 5.0)
        self.assertEqual(result.values["EG"], 2.0)
        self.assertEqual(result.values["NormalizedEG"], 1.0)

    def test_no_drawdown_has_no_recovery_and_flat_range_has_no_location(self):
        self.assertIsNone(self.engine._recovery_ratio([2.0, 3.0, 4.0]))
        self.assertIsNone(self.engine._close_location([4.0, 4.0]))


if __name__ == "__main__":
    unittest.main()
