import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from app.pipeline import AuctionDecisionPipeline, TimedDecisionInputs
from auction.features import AuctionFeatureEngine
from auction.one_to_two import OneToTwoEngine
from config.thresholds import ThresholdRegistry
from domain.enums import AuthenticityState, CandidateGrade, DataQualityState, ExecutionState, PermissionState, ReplayMode, SetupType, ValidationState
from domain.models import MarketContext, NightPlan
from expectation.benchmark import BenchmarkKey
from expectation.observations import AuctionGapObservation
from expectation.surprise import PointInTimeAuctionSurprisePercentile
from execution.compiler import DecisionCompiler
from market.asof import MarketContextRecord
from market.dqs import DataQualityService
from replay.clock import FutureDataAccessError, ReplayEngine, VirtualClock
from storage.hard_cancel import SQLiteHardCancelStore
from tests.helpers import make_expectation, make_snapshot, make_ticks, make_view, ts


class EndToEndReplayMatrixTests(unittest.TestCase):
    def setUp(self):
        self.one_cfg = ThresholdRegistry.load("config/thresholds/one_to_two.yaml")
        self.exec_cfg = ThresholdRegistry.load("config/thresholds/execution.yaml")
        self.expect_cfg = ThresholdRegistry.load("config/thresholds/expectation.yaml")
        self.tempdir = tempfile.TemporaryDirectory()
        self.store_path = Path(self.tempdir.name) / "hard-cancel.sqlite3"
        self.key = BenchmarkKey("ONE_TO_TWO", "RISK_ON", "MAIN", "LEADER", "H1", "CHANGE_HAND")
        self.expectation = make_expectation()
        self.context = MarketContext(
            trade_date="2026-08-14", phase="UP", regime="RISK_ON",
            market_permission=PermissionState.ALLOW,
            setup_permissions={SetupType.ONE_TO_TWO: PermissionState.ALLOW},
            position_permission=PermissionState.ALLOW,
            theme_validation=ValidationState.VALID,
        )
        self.context_records = (MarketContextRecord(self.context, ts("09:14:00")),)
        cutoff = datetime.fromisoformat("2026-08-13T23:59:59+08:00")
        self.gap_observations = tuple(
            AuctionGapObservation(f"gap-{index}", f"2026-08-{index + 1:02d}", cutoff, "TEST", "KEY_V1", self.key, gap)
            for index, gap in enumerate((-2.0, -1.0, 0.0, 1.0, 2.0), 1)
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def pipeline(self):
        return AuctionDecisionPipeline(
            AuctionFeatureEngine(self.one_cfg, PointInTimeAuctionSurprisePercentile(self.expect_cfg)),
            OneToTwoEngine(self.one_cfg),
            DataQualityService(self.exec_cfg),
            DecisionCompiler(self.exec_cfg, SQLiteHardCancelStore(self.store_path)),
        )

    def plan(self, in_pool=True):
        pool = ("000001.SZ",) if in_pool else ("000002.SZ",)
        setups = {ticker: SetupType.ONE_TO_TWO for ticker in pool}
        return NightPlan(
            "2026-08-14", pool, setups,
            datetime.fromisoformat("2026-08-13T20:00:00+08:00"),
            datetime.fromisoformat("2026-08-13T23:59:59+08:00"),
        )

    def inputs(self, available_at, **overrides):
        values = dict(
            available_at=available_at,
            regulatory_validation=ValidationState.VALID,
            market_validation=ValidationState.VALID,
            candidate_validation=ValidationState.VALID,
            setup_requirements_met=True,
            cross_setup_rank=1,
            context_strength=1.0,
        )
        values.update(overrides)
        return TimedDecisionInputs(**values)

    def evaluate(self, replay, plan, at, **input_overrides):
        list(replay.run(ReplayMode.CHECKPOINT, checkpoints=(at,)))
        return self.pipeline().evaluate_checkpoint(
            plan,
            replay,
            "000001.SZ",
            self.expectation,
            self.inputs(at, **input_overrides),
            self.context_records,
            auction_gap_observations=self.gap_observations,
            peer_groups={
                "liquidity": ("000001.SZ",), "theme": ("000001.SZ",),
                "global": ("000001.SZ",), "height": ("000001.SZ",),
            },
        )

    def replay(self, points):
        return ReplayEngine(make_ticks(points), VirtualClock(ts("09:14:59")))

    def assert_not_a(self, result):
        self.assertNotIn(result.decision.grade, (CandidateGrade.A1, CandidateGrade.A2))
        self.assertNotEqual(result.decision.execution_state, ExecutionState.ARMED)

    def test_case_1_true_strength_respects_all_four_checkpoints_then_arms(self):
        replay = self.replay([("09:15:00", 2.0), ("09:20:00", 2.5), ("09:24:00", 4.0), ("09:25:00", 4.5)])
        plan = self.plan()
        scouting = self.evaluate(replay, plan, ts("09:15:00"))
        self.assertEqual(scouting.features.authenticity_state, AuthenticityState.UNKNOWN)
        self.assert_not_a(scouting)
        validating = self.evaluate(replay, plan, ts("09:20:00"))
        self.assert_not_a(validating)
        finalizing = self.evaluate(replay, plan, ts("09:24:30"))
        self.assert_not_a(finalizing)
        final = self.evaluate(replay, plan, ts("09:25:00"))
        self.assertIn(final.decision.grade, (CandidateGrade.A1, CandidateGrade.A2))
        self.assertEqual(final.decision.execution_state, ExecutionState.ARMED)

    def test_case_2_pre20_mirage_cannot_a(self):
        replay = self.replay([("09:15:00", 2.0), ("09:18:00", 6.0), ("09:20:00", 3.0), ("09:24:30", 2.5), ("09:25:00", 2.2)])
        result = self.evaluate(replay, self.plan(), ts("09:25:00"))
        self.assertIn("PRE20_MIRAGE", result.setup_result.flags)
        self.assert_not_a(result)

    def test_case_3_post20_continuous_decay_cannot_a(self):
        replay = self.replay([("09:15:00", 3.0), ("09:20:00", 6.0), ("09:22:00", 5.0), ("09:24:30", 4.0), ("09:25:00", 3.0)])
        result = self.evaluate(replay, self.plan(), ts("09:25:00"))
        self.assertIn("POST20_CONTINUOUS_DECAY", result.setup_result.flags)
        self.assert_not_a(result)

    def test_case_4_confirmed_disagreement_is_healthy(self):
        replay = self.replay([("09:15:00", 3.0), ("09:20:00", 3.2), ("09:21:00", 5.0), ("09:22:00", 3.0), ("09:24:00", 4.2), ("09:25:00", 4.8)])
        result = self.evaluate(replay, self.plan(), ts("09:25:00"))
        self.assertEqual(result.features.authenticity_state, AuthenticityState.HEALTHY_DISAGREEMENT)

    def test_case_5_market_falsified_cannot_a(self):
        replay = self.replay([("09:15:00", 2.0), ("09:20:00", 3.0), ("09:25:00", 5.0)])
        result = self.evaluate(replay, self.plan(), ts("09:25:00"), market_validation=ValidationState.FALSIFIED)
        self.assert_not_a(result)
        self.assertEqual(result.decision.execution_state, ExecutionState.HARD_CANCELLED)

    def test_case_6_dqs_broken_cannot_execute(self):
        ticks = make_ticks([("09:15:00", 2.0), ("09:20:00", 3.0), ("09:25:00", 5.0)])
        for field in ("virtual_price", "gap_pct", "matched_volume", "matched_amount"):
            object.__setattr__(ticks[-1], field, None)
        replay = ReplayEngine(ticks, VirtualClock(ts("09:14:59")))
        result = self.evaluate(replay, self.plan(), ts("09:25:00"))
        self.assertEqual(result.data_quality.state, DataQualityState.BROKEN)
        self.assertEqual(result.decision.execution_state, ExecutionState.HARD_CANCELLED)

    def test_case_7_outside_night_pool_is_emergent_c(self):
        replay = self.replay([("09:15:00", 5.0), ("09:20:00", 8.0), ("09:25:00", 10.0)])
        result = self.evaluate(replay, self.plan(in_pool=False), ts("09:25:00"))
        self.assertEqual(result.decision.grade, CandidateGrade.C_AUCTION_EMERGENT)
        self.assertEqual(result.decision.execution_state, ExecutionState.WAIT)

    def test_case_8_low_hvs_cannot_a(self):
        replay = self.replay([("09:15:00", -2.0), ("09:20:00", -1.0), ("09:25:00", 0.0)])
        result = self.evaluate(replay, self.plan(), ts("09:25:00"), context_strength=.2)
        self.assertLess(result.setup_result.hvs, self.exec_cfg.get("compiler_hvs_min"))
        self.assert_not_a(result)

    def test_case_9_missing_liquidity_is_degraded_and_capped_b(self):
        ticks = make_ticks([("09:15:00", 2.0), ("09:20:00", 3.0), ("09:25:00", 5.0)])
        object.__setattr__(ticks[-1], "matched_volume", None)
        object.__setattr__(ticks[-1], "matched_amount", None)
        replay = ReplayEngine(ticks, VirtualClock(ts("09:14:59")))
        result = self.evaluate(replay, self.plan(), ts("09:25:00"))
        self.assertEqual(result.data_quality.state, DataQualityState.DEGRADED)
        self.assertIsNone(result.setup_result.liquidity_score)
        self.assertEqual(result.decision.grade, CandidateGrade.B_HIGH_QUALITY)
        self.assert_not_a(result)

    def test_case_10_hard_cancel_survives_pipeline_restart(self):
        ticks = make_ticks([("09:15:00", 2.0), ("09:20:00", 3.0), ("09:25:00", 5.0)])
        object.__setattr__(ticks[-1], "gap_pct", None)
        object.__setattr__(ticks[-1], "virtual_price", None)
        object.__setattr__(ticks[-1], "matched_volume", None)
        object.__setattr__(ticks[-1], "matched_amount", None)
        cancelled = self.evaluate(ReplayEngine(ticks, VirtualClock(ts("09:14:59"))), self.plan(), ts("09:25:00"))
        self.assertEqual(cancelled.decision.execution_state, ExecutionState.HARD_CANCELLED)
        restarted = self.evaluate(self.replay([("09:15:00", 2.0), ("09:20:00", 3.0), ("09:25:00", 5.0)]), self.plan(), ts("09:25:00"))
        self.assertEqual(restarted.decision.execution_state, ExecutionState.HARD_CANCELLED)

    def test_case_11_future_snapshot_is_rejected_at_0921(self):
        view = make_view(make_ticks([("09:21:00", 2.0), ("09:25:00", 9.0)]), as_of=ts("09:21:00"))
        future_snapshot = make_snapshot({"NormalizedEG": 9.0}, as_of=ts("09:25:00"))
        with self.assertRaises(FutureDataAccessError):
            OneToTwoEngine(self.one_cfg).evaluate(
                __import__("auction.one_to_two", fromlist=["OneToTwoInput"]).OneToTwoInput(
                    "000001.SZ", view, future_snapshot, ValidationState.VALID, 1.0,
                )
            )

    def test_case_12_no_drawdown_has_no_recovery_ratio(self):
        replay = self.replay([("09:15:00", 2.0), ("09:20:00", 3.0), ("09:24:30", 4.0), ("09:25:00", 5.0)])
        result = self.evaluate(replay, self.plan(), ts("09:25:00"))
        self.assertIsNone(result.features.values["RecoveryRatio"])


if __name__ == "__main__":
    unittest.main()
