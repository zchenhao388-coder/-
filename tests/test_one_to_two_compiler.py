import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from auction.features import AuctionFeatureEngine
from auction.one_to_two import OneToTwoEngine, OneToTwoInput
from config.thresholds import ThresholdRegistry
from domain.enums import AuthenticityState, CandidateGrade, DataQualityState, ExecutionState, PermissionState, SetupType, ValidationState
from domain.models import DataQualityReport, MarketContext
from execution.compiler import CompilerInput, DecisionCompiler
from storage.hard_cancel import SQLiteHardCancelStore
from expectation.surprise import AuctionSurpriseResult
from domain.enums import BenchmarkStatus
from tests.helpers import make_expectation, make_night_plan, make_snapshot, make_ticks, make_view, ts


class OneToTwoCompilerAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.one_cfg = ThresholdRegistry.load("config/thresholds/one_to_two.yaml")
        self.exec_cfg = ThresholdRegistry.load("config/thresholds/execution.yaml")
        class SufficientSurprise:
            @staticmethod
            def percentile(*args, **kwargs):
                return AuctionSurpriseResult(.95, 5, 5, BenchmarkStatus.SUFFICIENT)

        self.feature_engine = AuctionFeatureEngine(self.one_cfg, SufficientSurprise())
        self.setup_engine = OneToTwoEngine(self.one_cfg)
        self.tempdir = tempfile.TemporaryDirectory()
        self.store_path = Path(self.tempdir.name) / "hard-cancel.sqlite3"
        self.compiler = DecisionCompiler(self.exec_cfg, SQLiteHardCancelStore(self.store_path))
        self.good_dqs = DataQualityReport(DataQualityState.GOOD, (), (), 1.0)

    def tearDown(self):
        self.tempdir.cleanup()

    def market(self, **overrides):
        values = dict(
            trade_date="2026-08-14", phase="UP", regime="RISK_ON",
            market_permission=PermissionState.ALLOW,
            setup_permissions={SetupType.ONE_TO_TWO: PermissionState.ALLOW},
            position_permission=PermissionState.ALLOW,
            theme_validation=ValidationState.VALID,
        )
        values.update(overrides)
        return MarketContext(**values)

    def strong_result(self, authenticity=AuthenticityState.AUTHENTIC):
        values = {
            "NormalizedEG": 2.0, "AuctionSurprisePercentile": .95,
            "ThemePeerRank": 1.0, "GlobalPeerRank": 1.0, "HeightPeerRank": 1.0,
            "HistoricalAmountPercentile": 1.0, "HistoricalVolumePercentile": 1.0,
            "PeerAmountPercentile": 1.0, "PeerVolumePercentile": 1.0,
        }
        view = make_view(make_ticks([("09:25:00", 5.0)]))
        snapshot = make_snapshot(values, authenticity)
        return self.setup_engine.evaluate(OneToTwoInput("000001.SZ", view, snapshot, ValidationState.VALID, 1.0))

    def compile(self, result, **overrides):
        values = dict(
            trade_date="2026-08-14", decided_at=ts("09:25:00"), night_plan=make_night_plan(),
            market_context=self.market(), setup_result=result,
            regulatory_validation=ValidationState.VALID, data_quality=self.good_dqs,
            market_validation=ValidationState.VALID, candidate_validation=ValidationState.VALID,
            setup_requirements_met=True, auction_percentile=.95, cross_setup_rank=1,
        )
        values.update(overrides)
        return self.compiler.compile(CompilerInput(**values))

    def test_natural_strengthening_arms_a(self):
        ticks = make_ticks([("09:19:00", 2.0), ("09:20:00", 2.5), ("09:22:00", 3.2), ("09:24:00", 4.0), ("09:25:00", 4.5)])
        view = make_view(ticks)
        snapshot = self.feature_engine.compute(
            view, "000001.SZ", make_expectation(),
            liquidity_peer_group="liquidity", theme_peer_group="theme",
            global_peer_group="global", height_peer_group="height",
        )
        result = self.setup_engine.evaluate(OneToTwoInput("000001.SZ", view, snapshot, ValidationState.VALID, 1.0))
        decision = self.compile(result)
        self.assertIn(decision.grade, (CandidateGrade.A1, CandidateGrade.A2))
        self.assertEqual(decision.execution_state, ExecutionState.ARMED)

    def test_post20_decay_cannot_be_a(self):
        decision = self.compile(self.strong_result(AuthenticityState.FAKE_STRONG))
        self.assertEqual(decision.grade, CandidateGrade.DROP)
        self.assertEqual(decision.execution_state, ExecutionState.HARD_CANCELLED)

    def test_theme_invalidation_overrides_single_stock_score(self):
        decision = self.compile(self.strong_result(), market_validation=ValidationState.FALSIFIED)
        self.assertEqual(decision.execution_state, ExecutionState.HARD_CANCELLED)

    def test_dqs_broken_blocks_execution(self):
        broken = DataQualityReport(DataQualityState.BROKEN, ("matched_amount",), ("NO_DATA",), .4)
        self.assertEqual(self.compile(self.strong_result(), data_quality=broken).execution_state, ExecutionState.HARD_CANCELLED)

    def test_dqs_degraded_caps_at_b(self):
        degraded = DataQualityReport(DataQualityState.DEGRADED, ("matched_amount",), ("MISSING",), .8)
        decision = self.compile(self.strong_result(), data_quality=degraded)
        self.assertEqual(decision.grade, CandidateGrade.B_HIGH_QUALITY)
        self.assertEqual(decision.execution_state, ExecutionState.WAIT)

    def test_percentile_gate_failure_removes_a_grade(self):
        decision = self.compile(self.strong_result(), auction_percentile=.2)
        self.assertEqual(decision.execution_state, ExecutionState.WAIT)
        self.assertNotIn(decision.grade, (CandidateGrade.A1, CandidateGrade.A2))
        self.assertFalse(next(step for step in decision.steps if step.gate == "AQS/HVS/Percentile").passed)

    def test_outside_night_pool_is_capped_at_emergent(self):
        decision = self.compile(self.strong_result(), night_plan=make_night_plan(in_pool=False))
        self.assertEqual(decision.grade, CandidateGrade.C_AUCTION_EMERGENT)
        self.assertEqual(decision.execution_state, ExecutionState.WAIT)

    def test_hard_cancel_is_not_recoverable_after_compiler_restart(self):
        result = self.strong_result()
        first = self.compile(result, data_quality=DataQualityReport(DataQualityState.BROKEN, (), (), .2))
        restarted = DecisionCompiler(self.exec_cfg, SQLiteHardCancelStore(self.store_path))
        second = restarted.compile(CompilerInput(
            trade_date="2026-08-14", decided_at=ts("09:25:01"), night_plan=make_night_plan(),
            market_context=self.market(), setup_result=result,
            regulatory_validation=ValidationState.VALID, data_quality=self.good_dqs,
            market_validation=ValidationState.VALID, candidate_validation=ValidationState.VALID,
            setup_requirements_met=True, auction_percentile=.95, cross_setup_rank=1,
        ))
        self.assertEqual(first.execution_state, ExecutionState.HARD_CANCELLED)
        self.assertEqual(second.steps[0].gate, "HardCancelSticky")

    def test_before_0925_cannot_produce_final_a(self):
        result = self.strong_result()
        for clock in ("09:15:00", "09:20:00", "09:24:30", "09:24:59"):
            point_in_time_result = replace(result, computed_as_of=ts(clock))
            decision = self.compile(point_in_time_result, decided_at=ts(clock))
            self.assertNotIn(decision.grade, (CandidateGrade.A1, CandidateGrade.A2))
            self.assertNotEqual(decision.execution_state, ExecutionState.ARMED)


if __name__ == "__main__":
    unittest.main()
