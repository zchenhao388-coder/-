import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from auction.high_board import HighBoardEcosystemEngine, HighBoardEngine, HighBoardInput, HighBoardPeerGroups
from config.thresholds import ThresholdRegistry
from domain.enums import (
    AuthenticityState,
    BenchmarkStatus,
    CandidateGrade,
    DataQualityState,
    ExecutionState,
    FakeStrongFlag,
    HighBoardEcosystem,
    LeadershipState,
    PermissionState,
    SetupType,
    ThresholdStatus,
    ValidationState,
)
from domain.models import AuctionTick, DataQualityReport, MarketContext, NightPlan
from execution.compiler import CompilerInput, DecisionCompiler
from market.asof import AsOfMarketView, MarketContextRecord
from storage.hard_cancel import SQLiteHardCancelStore
from tests.helpers import make_snapshot, ts


def tick(ticker, clock, gap):
    timestamp = ts(clock)
    return AuctionTick(timestamp, timestamp, ticker, 10.0 * (1 + gap / 100.0), gap, 100_000.0, 1_000_000.0, source="TEST")


class HighBoardMilestoneTests(unittest.TestCase):
    def setUp(self):
        self.high_cfg = ThresholdRegistry.load("config/thresholds/high_board.yaml")
        self.exec_cfg = ThresholdRegistry.load("config/thresholds/execution.yaml")
        self.engine = HighBoardEngine(self.high_cfg)
        self.tempdir = tempfile.TemporaryDirectory()
        self.store_path = Path(self.tempdir.name) / "hard-cancel.sqlite3"

    def tearDown(self):
        self.tempdir.cleanup()

    def market(self, phase="EXPANSION", position=PermissionState.ALLOW):
        return MarketContext(
            "2026-08-14",
            phase,
            "RISK_ON",
            market_permission=PermissionState.ALLOW,
            setup_permissions={SetupType.HIGH_BOARD: PermissionState.ALLOW},
            position_permission=position,
            theme_validation=ValidationState.VALID,
        )

    def view(self, peer_gaps=(5.0, 4.0, 3.0, 2.0), broken_gaps=(0.0,), phase="EXPANSION", as_of="09:25:00"):
        tickers = ["000001.SZ"] + [f"0001{index:02d}.SZ" for index in range(len(peer_gaps))]
        ticks = [tick("000001.SZ", as_of, 6.0)]
        ticks.extend(tick(ticker, as_of, gap) for ticker, gap in zip(tickers[1:], peer_gaps))
        broken_tickers = [f"0002{index:02d}.SZ" for index in range(len(broken_gaps))]
        ticks.extend(tick(ticker, as_of, gap) for ticker, gap in zip(broken_tickers, broken_gaps))
        groups = {
            "high_board": tuple(tickers[:2]),
            "next_height": tuple(tickers[2:3]),
            "middle_board": tuple(tickers[3:]),
            "broken_high_board": tuple(broken_tickers),
        }
        return AsOfMarketView(
            "2026-08-14",
            ts(as_of),
            ticks=ticks,
            peer_groups=groups,
            market_context_records=(MarketContextRecord(self.market(phase), ts("09:14:00")),),
        )

    def snapshot(
        self,
        rank=1.0,
        gap=6.0,
        liquidity=1.0,
        authenticity=AuthenticityState.AUTHENTIC,
        benchmark_status=BenchmarkStatus.SUFFICIENT,
        surprise_status=BenchmarkStatus.SUFFICIENT,
        as_of="09:25:00",
    ):
        return make_snapshot(
            {
                "ActualAuctionGap": gap,
                "NormalizedEG": 2.0,
                "AuctionSurprisePercentile": .95,
                "ThemePeerRank": rank,
                "GlobalPeerRank": rank,
                "HeightPeerRank": rank,
                "HistoricalAmountPercentile": liquidity,
                "HistoricalVolumePercentile": liquidity,
                "PeerAmountPercentile": liquidity,
                "PeerVolumePercentile": liquidity,
                "Post20Delta": 1.0,
            },
            authenticity=authenticity,
            as_of=ts(as_of),
            benchmark_status=benchmark_status,
            surprise_status=surprise_status,
        )

    def evaluate(self, view=None, snapshot=None, **overrides):
        market_view = view or self.view()
        values = dict(
            ticker="000001.SZ",
            market_view=market_view,
            feature_snapshot=snapshot or self.snapshot(as_of=market_view.as_of.time().isoformat()),
            candidate_validation=ValidationState.VALID,
            board_height=5,
            is_highest_board=True,
            leader_identity_strength=.95,
            regulatory_headroom=.9,
            theme_leadership_retained=True,
            board_form="CHANGE_HAND",
        )
        values.update(overrides)
        return self.engine.evaluate(HighBoardInput(**values))

    def compile(self, setup_result, decided_at=None, dqs=DataQualityState.GOOD):
        decided_at = decided_at or ts("09:25:00")
        plan = NightPlan(
            "2026-08-14",
            ("000001.SZ",),
            {"000001.SZ": SetupType.HIGH_BOARD},
            datetime.fromisoformat("2026-08-13T20:00:00+08:00"),
            datetime.fromisoformat("2026-08-13T23:59:59+08:00"),
        )
        report = DataQualityReport(dqs, (), (), 1.0)
        return DecisionCompiler(self.exec_cfg, SQLiteHardCancelStore(self.store_path)).compile(CompilerInput(
            "2026-08-14",
            decided_at,
            plan,
            self.market(),
            setup_result,
            ValidationState.VALID,
            report,
            ValidationState.VALID,
            ValidationState.VALID,
            True,
            .95,
            1,
        ))

    def test_seed_weights_match_locked_high_board_definition(self):
        aqs_keys = tuple(HighBoardEngine.AQS_WEIGHT_KEYS.values())
        hvs_keys = tuple(HighBoardEngine.HVS_WEIGHT_KEYS.values())
        self.assertEqual(sum(self.high_cfg.get(key) for key in aqs_keys), 100.0)
        self.assertEqual(sum(self.high_cfg.get(key) for key in hvs_keys), 100.0)
        self.assertTrue(all(self.high_cfg.metadata(key).status == ThresholdStatus.SEED for key in aqs_keys + hvs_keys))

    def test_dominant_leader_positive_ecosystem_can_arm_through_existing_compiler(self):
        result = self.evaluate()
        self.assertEqual(result.setup_type, SetupType.HIGH_BOARD)
        self.assertEqual(result.leadership_state, LeadershipState.DOMINANT)
        self.assertEqual(result.ecosystem_state, HighBoardEcosystem.POSITIVE.value)
        self.assertEqual(result.grade_recommendation, CandidateGrade.A1)
        decision = self.compile(result)
        self.assertEqual(decision.grade, CandidateGrade.A1)
        self.assertEqual(decision.execution_state, ExecutionState.ARMED)

    def test_leadership_challenged_is_b_even_with_high_scores(self):
        result = self.evaluate(snapshot=self.snapshot(rank=.5), leader_identity_strength=.95)
        self.assertEqual(result.leadership_state, LeadershipState.CHALLENGED)
        self.assertEqual(result.grade_recommendation, CandidateGrade.B_LEADERSHIP_CHALLENGED)
        decision = self.compile(result)
        self.assertNotIn(decision.grade, (CandidateGrade.A1, CandidateGrade.A2))
        self.assertNotEqual(decision.execution_state, ExecutionState.ARMED)

    def test_lost_leadership_is_c_and_cannot_arm(self):
        result = self.evaluate(theme_leadership_retained=False)
        self.assertEqual(result.leadership_state, LeadershipState.LOST)
        self.assertEqual(result.grade_recommendation, CandidateGrade.C_LEADERSHIP_LOST)
        self.assertIn("LEADERSHIP_LOST", result.reason_codes)
        self.assertNotEqual(self.compile(result).execution_state, ExecutionState.ARMED)

    def test_negative_ecosystem_marks_fake_leader_isolation_and_cannot_arm(self):
        view = self.view(peer_gaps=(-3.0, -4.0, -3.0, -2.5), broken_gaps=(-4.0,))
        result = self.evaluate(view=view)
        self.assertIn(FakeStrongFlag.FAKE_LEADER_ISOLATION.value, result.flags)
        self.assertEqual(result.grade_recommendation, CandidateGrade.C_HIGHBOARD_DEGRADED)
        self.assertNotEqual(self.compile(result).execution_state, ExecutionState.ARMED)

    def test_height_premium_without_relative_or_liquidity_support_cannot_a(self):
        result = self.evaluate(snapshot=self.snapshot(rank=.35, liquidity=.3))
        self.assertIn(FakeStrongFlag.FAKE_HEIGHT_PREMIUM.value, result.flags)
        self.assertNotIn(result.grade_recommendation, (CandidateGrade.A1, CandidateGrade.A2))

    def test_one_word_extreme_consensus_in_divergence_is_consensus_risk(self):
        view = self.view(phase="DIVERGENCE")
        result = self.evaluate(view=view, snapshot=self.snapshot(gap=9.5, liquidity=.4), board_form="ONE_WORD")
        self.assertIn(FakeStrongFlag.FAKE_ONE_WORD_CONSENSUS.value, result.flags)
        self.assertEqual(result.grade_recommendation, CandidateGrade.B_CONSENSUS_RISK)
        self.assertNotEqual(self.compile(result).execution_state, ExecutionState.ARMED)

    def test_insufficient_benchmark_cannot_a_or_arm(self):
        result = self.evaluate(snapshot=self.snapshot(benchmark_status=BenchmarkStatus.INSUFFICIENT))
        self.assertEqual(result.grade_recommendation, CandidateGrade.B_ECOSYSTEM_PENDING)
        decision = self.compile(result)
        self.assertNotIn(decision.grade, (CandidateGrade.A1, CandidateGrade.A2))
        self.assertNotEqual(decision.execution_state, ExecutionState.ARMED)

    def test_degraded_and_broken_dqs_keep_existing_caps(self):
        result = self.evaluate()
        degraded = self.compile(result, dqs=DataQualityState.DEGRADED)
        self.assertEqual(degraded.grade, CandidateGrade.B_HIGH_QUALITY)
        self.assertEqual(degraded.execution_state, ExecutionState.WAIT)

        other_store = Path(self.tempdir.name) / "broken.sqlite3"
        plan = NightPlan(
            "2026-08-14", ("000001.SZ",), {"000001.SZ": SetupType.HIGH_BOARD},
            datetime.fromisoformat("2026-08-13T20:00:00+08:00"),
            datetime.fromisoformat("2026-08-13T23:59:59+08:00"),
        )
        broken = DecisionCompiler(self.exec_cfg, SQLiteHardCancelStore(other_store)).compile(CompilerInput(
            "2026-08-14", ts("09:25:00"), plan, self.market(), result,
            ValidationState.VALID, DataQualityReport(DataQualityState.BROKEN, (), ("NO_DATA",), 0.0),
            ValidationState.VALID, ValidationState.VALID, True, .95, 1,
        ))
        self.assertEqual(broken.execution_state, ExecutionState.HARD_CANCELLED)

    def test_pre925_high_board_never_returns_final_a_or_armed(self):
        view = self.view(as_of="09:24:30")
        result = self.evaluate(view=view, snapshot=self.snapshot(as_of="09:24:30"))
        self.assertNotIn(result.grade_recommendation, (CandidateGrade.A1, CandidateGrade.A2))
        decision = self.compile(result, decided_at=ts("09:24:30"))
        self.assertNotIn(decision.grade, (CandidateGrade.A1, CandidateGrade.A2))
        self.assertNotEqual(decision.execution_state, ExecutionState.ARMED)

    def test_ecosystem_engine_cannot_see_future_peer_recovery(self):
        ticks = [
            tick("000001.SZ", "09:21:00", 5.0),
            tick("000010.SZ", "09:21:00", -4.0),
            tick("000011.SZ", "09:21:00", -5.0),
            tick("000010.SZ", "09:25:00", 6.0),
            tick("000011.SZ", "09:25:00", 5.0),
        ]
        view = AsOfMarketView(
            "2026-08-14", ts("09:21:00"), ticks=ticks,
            peer_groups={"high_board": ("000010.SZ",), "next_height": ("000011.SZ",)},
        )
        ecosystem = HighBoardEcosystemEngine(self.high_cfg).evaluate(view, HighBoardPeerGroups())
        self.assertEqual(ecosystem.state, HighBoardEcosystem.NEGATIVE)
        self.assertEqual(ecosystem.observation_count, 2)


if __name__ == "__main__":
    unittest.main()
