import unittest
from dataclasses import replace

from auction.features import AuctionFeatureEngine
from auction.one_to_two import OneToTwoEngine, OneToTwoInput
from config.thresholds import ThresholdRegistry
from domain.enums import AuthenticityState, CandidateGrade, DataQualityState, ExecutionState, PermissionState, SetupType, ValidationState
from domain.models import DataQualityReport, MarketContext
from execution.compiler import CompilerInput, DecisionCompiler
from tests.helpers import make_ticks, ts


class OneToTwoCompilerAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.one_cfg = ThresholdRegistry.load("config/thresholds/one_to_two.yaml")
        self.exec_cfg = ThresholdRegistry.load("config/thresholds/execution.yaml")
        self.feature_engine = AuctionFeatureEngine(self.one_cfg)
        self.setup_engine = OneToTwoEngine(self.one_cfg)
        self.compiler = DecisionCompiler(self.exec_cfg)
        self.good_dqs = DataQualityReport(DataQualityState.GOOD, (), (), 1.0)

    def market(self):
        return MarketContext(
            trade_date="2026-08-14", phase="UP", regime="RISK_ON",
            market_permission=PermissionState.ALLOW,
            setup_permissions={SetupType.ONE_TO_TWO: PermissionState.ALLOW},
            position_permission=PermissionState.ALLOW,
            theme_validation=ValidationState.VALID,
        )

    def strong_result(self, authenticity=AuthenticityState.AUTHENTIC):
        features = {
            "NormalizedEG": 2.0, "AuctionSurprisePercentile": 0.95,
            "ThemePeerRank": 1.0, "GlobalPeerRank": 1.0, "HeightPeerRank": 1.0,
            "HistoricalAmountPercentile": 1.0, "HistoricalVolumePercentile": 1.0,
            "PeerAmountPercentile": 1.0, "PeerVolumePercentile": 1.0,
        }
        return self.setup_engine.evaluate(OneToTwoInput("000001.SZ", features, authenticity, (), ValidationState.VALID, 1.0))

    def compile(self, result, **overrides):
        values = dict(
            trade_date="2026-08-14", decided_at=ts("09:25:00"), in_night_pool=True,
            market_context=self.market(), setup_result=result,
            regulatory_validation=ValidationState.VALID, data_quality=self.good_dqs,
            market_validation=ValidationState.VALID, candidate_validation=ValidationState.VALID,
            setup_requirements_met=True, auction_percentile=.95, cross_setup_rank=1,
        )
        values.update(overrides)
        return self.compiler.compile(CompilerInput(**values))

    def test_natural_strengthening_arms_a(self):
        ticks = make_ticks([("09:19:00", 2.0), ("09:20:00", 2.5), ("09:22:00", 3.2), ("09:24:00", 4.0), ("09:25:00", 4.5)])
        snapshot = self.feature_engine.compute(
            "000001.SZ", ticks, 3.0, 2.0, 4.0,
            historical_amounts=[10_000, 20_000], historical_volumes=[1_000, 2_000],
            theme_peer_gaps=[1, 2, 3], global_peer_gaps=[0, 1, 2], height_peer_gaps=[1, 2],
        )
        result = self.setup_engine.evaluate(OneToTwoInput("000001.SZ", snapshot.values, snapshot.authenticity_state, [flag.value for flag in snapshot.fake_strong_flags], ValidationState.VALID, 1.0))
        decision = self.compile(result)
        self.assertIn(decision.grade, (CandidateGrade.A1, CandidateGrade.A2))
        self.assertEqual(decision.execution_state, ExecutionState.ARMED)

    def test_post20_decay_cannot_be_a(self):
        decision = self.compile(self.strong_result(AuthenticityState.FAKE_STRONG))
        self.assertEqual(decision.grade, CandidateGrade.DROP)
        self.assertEqual(decision.execution_state, ExecutionState.HARD_CANCELLED)

    def test_theme_invalidation_overrides_single_stock_score(self):
        decision = self.compile(self.strong_result(), market_validation=ValidationState.INVALID)
        self.assertEqual(decision.execution_state, ExecutionState.HARD_CANCELLED)

    def test_dqs_broken_blocks_execution(self):
        broken = DataQualityReport(DataQualityState.BROKEN, ("matched_amount",), ("NO_DATA",), .4)
        self.assertEqual(self.compile(self.strong_result(), data_quality=broken).execution_state, ExecutionState.HARD_CANCELLED)

    def test_percentile_gate_is_independent_from_high_aqs(self):
        decision = self.compile(self.strong_result(), auction_percentile=.2)
        self.assertEqual(decision.execution_state, ExecutionState.WAIT)
        self.assertFalse(next(step for step in decision.steps if step.gate == "AQS/HVS/Percentile").passed)

    def test_outside_night_pool_is_capped_at_emergent(self):
        decision = self.compile(replace(self.strong_result(), flags=("AUCTION_LIMIT_UP",)), in_night_pool=False)
        self.assertEqual(decision.grade, CandidateGrade.C_AUCTION_EMERGENT)
        self.assertEqual(decision.execution_state, ExecutionState.WAIT)

    def test_hard_cancel_is_not_recoverable_same_day(self):
        result = self.strong_result()
        first = self.compile(result, data_quality=DataQualityReport(DataQualityState.BROKEN, (), (), .2))
        second = self.compile(result)
        self.assertEqual(first.execution_state, ExecutionState.HARD_CANCELLED)
        self.assertEqual(second.steps[0].gate, "HardCancelSticky")


if __name__ == "__main__":
    unittest.main()
