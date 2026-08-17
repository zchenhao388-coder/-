import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from auction.one_to_two import OneToTwoEngine, OneToTwoInput
from config.thresholds import ThresholdRegistry
from domain.enums import AuthenticityState, CandidateGrade, DataQualityState, ExecutionState, PermissionState, SetupType, ValidationState
from domain.models import DataQualityReport, MarketContext
from execution.compiler import CompilerInput, DecisionCompiler
from storage.hard_cancel import SQLiteHardCancelStore
from tests.helpers import make_night_plan, make_snapshot, make_ticks, make_view, ts


class CompilerGateCoverageTests(unittest.TestCase):
    def setUp(self):
        self.one_cfg = ThresholdRegistry.load("config/thresholds/one_to_two.yaml")
        self.exec_cfg = ThresholdRegistry.load("config/thresholds/execution.yaml")
        self.tempdir = tempfile.TemporaryDirectory()
        self.compiler = DecisionCompiler(
            self.exec_cfg,
            SQLiteHardCancelStore(Path(self.tempdir.name) / "hard-cancel.sqlite3"),
        )
        self.good_dqs = DataQualityReport(DataQualityState.GOOD, (), (), 1.0)
        view = make_view(make_ticks([("09:25:00", 5.0)]))
        snapshot = make_snapshot({
            "NormalizedEG": 2.0, "AuctionSurprisePercentile": .99,
            "ThemePeerRank": 1.0, "GlobalPeerRank": 1.0, "HeightPeerRank": 1.0,
            "HistoricalAmountPercentile": 1.0, "HistoricalVolumePercentile": 1.0,
            "PeerAmountPercentile": 1.0, "PeerVolumePercentile": 1.0,
        })
        self.strong = OneToTwoEngine(self.one_cfg).evaluate(
            OneToTwoInput("000001.SZ", view, snapshot, ValidationState.VALID, 1.0)
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def market(self, market_permission=PermissionState.ALLOW, setup_permission=PermissionState.ALLOW, position_permission=PermissionState.ALLOW):
        return MarketContext(
            trade_date="2026-08-14", phase="UP", regime="RISK_ON",
            market_permission=market_permission,
            setup_permissions={SetupType.ONE_TO_TWO: setup_permission},
            position_permission=position_permission,
            theme_validation=ValidationState.VALID,
        )

    def request(self, case, **overrides):
        values = dict(
            trade_date="2026-08-14", decided_at=ts("09:25:00"), night_plan=make_night_plan(),
            market_context=self.market(), setup_result=self.strong,
            regulatory_validation=ValidationState.VALID, data_quality=self.good_dqs,
            market_validation=ValidationState.VALID, candidate_validation=ValidationState.VALID,
            setup_requirements_met=True, auction_percentile=.99, cross_setup_rank=1,
        )
        values.update(overrides)
        return CompilerInput(**values)

    def assert_blocked(self, decision, failed_gate, hard=True):
        self.assertNotIn(decision.grade, (CandidateGrade.A1, CandidateGrade.A2))
        self.assertNotEqual(decision.execution_state, ExecutionState.ARMED)
        step = next(step for step in decision.steps if step.gate == failed_gate)
        self.assertFalse(step.passed)
        if hard:
            self.assertEqual(decision.execution_state, ExecutionState.HARD_CANCELLED)

    def test_market_permission_deny(self):
        decision = self.compiler.compile(self.request("market-deny", market_context=self.market(market_permission=PermissionState.DENY)))
        self.assert_blocked(decision, "MarketPermission")
        self.assertNotIn("AQS/HVS/Percentile", [step.gate for step in decision.steps])

    def test_setup_permission_deny(self):
        decision = self.compiler.compile(self.request("setup-deny", market_context=self.market(setup_permission=PermissionState.DENY)))
        self.assert_blocked(decision, "SetupPermission")

    def test_regulatory_hard_invalid(self):
        decision = self.compiler.compile(self.request("reg-hard", regulatory_validation=ValidationState.HARD_INVALID))
        self.assert_blocked(decision, "Regulatory")

    def test_dqs_degraded_caps_at_b(self):
        degraded = DataQualityReport(DataQualityState.DEGRADED, ("matched_amount",), ("MISSING",), .8)
        decision = self.compiler.compile(self.request("dqs-degraded", data_quality=degraded))
        self.assertEqual(decision.grade, CandidateGrade.B_HIGH_QUALITY)
        self.assertEqual(decision.execution_state, ExecutionState.WAIT)

    def test_dqs_broken(self):
        broken = DataQualityReport(DataQualityState.BROKEN, (), ("NO_DATA",), 0.0)
        decision = self.compiler.compile(self.request("dqs-broken", data_quality=broken))
        self.assert_blocked(decision, "DQS")

    def test_market_validation_falsified(self):
        decision = self.compiler.compile(self.request("market-false", market_validation=ValidationState.FALSIFIED))
        self.assert_blocked(decision, "MarketValidation")

    def test_candidate_validation_hard_invalid(self):
        decision = self.compiler.compile(self.request("candidate-hard", candidate_validation=ValidationState.HARD_INVALID))
        self.assert_blocked(decision, "CandidateValidation")

    def test_candidate_validation_falsified(self):
        decision = self.compiler.compile(self.request("candidate-false", candidate_validation=ValidationState.FALSIFIED))
        self.assert_blocked(decision, "CandidateValidation")

    def test_authenticity_failed(self):
        result = replace(self.strong, authenticity_state=AuthenticityState.FAKE_STRONG)
        decision = self.compiler.compile(self.request("auth-fail", setup_result=result))
        self.assert_blocked(decision, "Authenticity")

    def test_setup_requirements_failed(self):
        decision = self.compiler.compile(self.request("setup-req", setup_requirements_met=False))
        self.assert_blocked(decision, "SetupRequirements")

    def test_aqs_failed(self):
        decision = self.compiler.compile(self.request("aqs-fail", setup_result=replace(self.strong, aqs=1.0)))
        self.assert_blocked(decision, "AQS/HVS/Percentile", hard=False)

    def test_hvs_failed(self):
        decision = self.compiler.compile(self.request("hvs-fail", setup_result=replace(self.strong, hvs=1.0)))
        self.assert_blocked(decision, "AQS/HVS/Percentile", hard=False)

    def test_percentile_failed(self):
        decision = self.compiler.compile(self.request("pct-fail", auction_percentile=.1))
        self.assert_blocked(decision, "AQS/HVS/Percentile", hard=False)

    def test_cross_setup_rank_failed(self):
        decision = self.compiler.compile(self.request("rank-fail", cross_setup_rank=99))
        self.assert_blocked(decision, "CrossSetupRank", hard=False)

    def test_position_permission_deny(self):
        decision = self.compiler.compile(self.request("position-deny", market_context=self.market(position_permission=PermissionState.DENY)))
        self.assert_blocked(decision, "PositionPermission", hard=False)


if __name__ == "__main__":
    unittest.main()
