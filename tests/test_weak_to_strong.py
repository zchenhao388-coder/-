import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from auction.weak_to_strong import WeakToStrongEngine, WeakToStrongInput
from config.thresholds import ThresholdRegistry
from domain.enums import (
    AuthenticityState,
    BenchmarkStatus,
    CandidateGrade,
    DataQualityState,
    ExecutionState,
    FakeStrongFlag,
    IdentityRecoveryState,
    PermissionState,
    SetupType,
    ThresholdStatus,
    ValidationState,
    WeaknessResolution,
    WeaknessSource,
    WeakToStrongType,
)
from domain.models import AuctionTick, DataQualityReport, MarketContext, NightPlan, WeaknessState
from execution.compiler import CompilerInput, DecisionCompiler
from market.asof import AsOfMarketView, MarketContextRecord
from storage.hard_cancel import SQLiteHardCancelStore
from tests.helpers import make_snapshot, ts


def tick(ticker, clock, gap):
    timestamp = ts(clock)
    return AuctionTick(timestamp, timestamp, ticker, 10.0 * (1 + gap / 100.0), gap, 100_000.0, 1_000_000.0, source="TEST")


class WeakToStrongMilestoneTests(unittest.TestCase):
    def setUp(self):
        self.weak_cfg = ThresholdRegistry.load("config/thresholds/weak_to_strong.yaml")
        self.exec_cfg = ThresholdRegistry.load("config/thresholds/execution.yaml")
        self.engine = WeakToStrongEngine(self.weak_cfg)
        self.tempdir = tempfile.TemporaryDirectory()
        self.store_path = Path(self.tempdir.name) / "hard-cancel.sqlite3"
        self.weakness = WeaknessState(
            WeakToStrongType.WEAK_BOARD_TO_STRONG,
            .6,
            WeaknessSource.BOARD_STRUCTURE,
            True,
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def market(self):
        return MarketContext(
            "2026-08-14", "REPAIR", "RISK_ON",
            market_permission=PermissionState.ALLOW,
            setup_permissions={SetupType.WEAK_TO_STRONG: PermissionState.ALLOW},
            position_permission=PermissionState.ALLOW,
            theme_validation=ValidationState.VALID,
        )

    def view(self, competitor_gaps=(1.0, 2.0), as_of="09:25:00", extra_ticks=()):
        competitor_tickers = tuple(f"0003{index:02d}.SZ" for index in range(len(competitor_gaps)))
        ticks = [tick("000001.SZ", as_of, 5.0)]
        ticks.extend(tick(ticker, as_of, gap) for ticker, gap in zip(competitor_tickers, competitor_gaps))
        ticks.extend(extra_ticks)
        return AsOfMarketView(
            "2026-08-14",
            ts(as_of),
            ticks=ticks,
            peer_groups={"direct_competitors": competitor_tickers},
            market_context_records=(MarketContextRecord(self.market(), ts("09:14:00")),),
        )

    def snapshot(
        self,
        theme_rank=.95,
        gap=5.0,
        post20=1.0,
        funding=1.0,
        liquidity=1.0,
        authenticity=AuthenticityState.AUTHENTIC,
        benchmark=BenchmarkStatus.SUFFICIENT,
        surprise=BenchmarkStatus.SUFFICIENT,
        as_of="09:25:00",
    ):
        return make_snapshot(
            {
                "ActualAuctionGap": gap,
                "NormalizedEG": 2.0,
                "AuctionSurprisePercentile": .95,
                "ThemePeerRank": theme_rank,
                "GlobalPeerRank": theme_rank,
                "HeightPeerRank": theme_rank,
                "HistoricalAmountPercentile": liquidity,
                "HistoricalVolumePercentile": liquidity,
                "PeerAmountPercentile": liquidity,
                "PeerVolumePercentile": liquidity,
                "Post20Delta": post20,
                "FundingConfirmed": funding,
            },
            authenticity=authenticity,
            as_of=ts(as_of),
            benchmark_status=benchmark,
            surprise_status=surprise,
        )

    def evaluate(self, view=None, snapshot=None, **overrides):
        market_view = view or self.view()
        values = dict(
            ticker="000001.SZ",
            market_view=market_view,
            feature_snapshot=snapshot or self.snapshot(as_of=market_view.as_of.time().isoformat()),
            candidate_validation=ValidationState.VALID,
            weakness_state=self.weakness,
            previous_theme_rank=.4,
            theme_repair_confirmed=True,
            leadership_transfer_confirmed=False,
            context_strength=.9,
        )
        values.update(overrides)
        return self.engine.evaluate(WeakToStrongInput(**values))

    def compile(self, setup_result, dqs=DataQualityState.GOOD, decided_at=None):
        decided_at = decided_at or ts("09:25:00")
        plan = NightPlan(
            "2026-08-14", ("000001.SZ",), {"000001.SZ": SetupType.WEAK_TO_STRONG},
            datetime.fromisoformat("2026-08-13T20:00:00+08:00"),
            datetime.fromisoformat("2026-08-13T23:59:59+08:00"),
        )
        return DecisionCompiler(self.exec_cfg, SQLiteHardCancelStore(self.store_path)).compile(CompilerInput(
            "2026-08-14", decided_at, plan, self.market(), setup_result,
            ValidationState.VALID, DataQualityReport(dqs, (), (), 1.0),
            ValidationState.VALID, ValidationState.VALID, True, .95, 1,
        ))

    def test_seed_weights_match_locked_weak_to_strong_definition(self):
        keys = tuple(WeakToStrongEngine.WEIGHT_KEYS.values())
        self.assertEqual(sum(self.weak_cfg.get(key) for key in keys), 100.0)
        self.assertTrue(all(self.weak_cfg.metadata(key).status == ThresholdStatus.SEED for key in keys))

    def test_all_three_locked_weakness_types_are_supported(self):
        self.assertEqual(
            {item.value for item in WeakToStrongType},
            {"WEAK_BOARD_TO_STRONG", "BROKEN_BOARD_TO_STRONG", "CORE_NEGATIVE_FEEDBACK_REPAIR"},
        )

    def test_resolved_weakness_and_recovered_identity_can_arm_via_compiler(self):
        result = self.evaluate()
        self.assertEqual(result.weakness_resolved, WeaknessResolution.TRUE)
        self.assertEqual(result.identity_recovery, IdentityRecoveryState.TRUE)
        self.assertEqual(result.grade_recommendation, CandidateGrade.A1)
        decision = self.compile(result)
        self.assertEqual(decision.grade, CandidateGrade.A1)
        self.assertEqual(decision.execution_state, ExecutionState.ARMED)

    def test_not_repairable_is_drop_even_with_extreme_scores(self):
        weakness = WeaknessState(
            WeakToStrongType.CORE_NEGATIVE_FEEDBACK_REPAIR, 1.0, WeaknessSource.EVENT, False,
        )
        result = self.evaluate(weakness_state=weakness)
        self.assertEqual(result.weakness_resolved, WeaknessResolution.FALSE)
        self.assertEqual(result.grade_recommendation, CandidateGrade.DROP)
        self.assertNotEqual(self.compile(result).execution_state, ExecutionState.ARMED)

    def test_partial_repair_is_confirmation_only(self):
        result = self.evaluate(snapshot=self.snapshot(funding=0.0))
        self.assertEqual(result.weakness_resolved, WeaknessResolution.PARTIAL)
        self.assertNotIn(result.grade_recommendation, (CandidateGrade.A1, CandidateGrade.A2))

    def test_no_identity_recovery_cannot_a(self):
        view = self.view(competitor_gaps=(6.0, 7.0))
        result = self.evaluate(view=view, snapshot=self.snapshot(theme_rank=.2), previous_theme_rank=.3)
        self.assertEqual(result.identity_recovery, IdentityRecoveryState.FALSE)
        self.assertIn(FakeStrongFlag.NO_IDENTITY_RECOVERY.value, result.flags)
        self.assertNotIn(result.grade_recommendation, (CandidateGrade.A1, CandidateGrade.A2))

    def test_open_only_isolated_liquidity_and_fade_flags_each_block_a(self):
        cases = (
            (dict(snapshot=self.snapshot(post20=0.0)), FakeStrongFlag.OPEN_ONLY_REPAIR),
            (dict(theme_repair_confirmed=False), FakeStrongFlag.ISOLATED_REPAIR),
            (dict(snapshot=self.snapshot(liquidity=.3)), FakeStrongFlag.LIQUIDITY_FAKE_REPAIR),
            (dict(snapshot=self.snapshot(post20=-1.5)), FakeStrongFlag.REPAIR_AND_FADE),
        )
        for overrides, expected_flag in cases:
            with self.subTest(flag=expected_flag.value):
                result = self.evaluate(**overrides)
                self.assertIn(expected_flag.value, result.flags)
                self.assertNotIn(result.grade_recommendation, (CandidateGrade.A1, CandidateGrade.A2))

    def test_minimum_sample_contract_still_blocks_a_and_armed(self):
        result = self.evaluate(snapshot=self.snapshot(surprise=BenchmarkStatus.INSUFFICIENT))
        self.assertNotIn(result.grade_recommendation, (CandidateGrade.A1, CandidateGrade.A2))
        decision = self.compile(result)
        self.assertNotIn(decision.grade, (CandidateGrade.A1, CandidateGrade.A2))
        self.assertNotEqual(decision.execution_state, ExecutionState.ARMED)

    def test_dqs_degraded_caps_and_broken_hard_cancels(self):
        result = self.evaluate()
        degraded = self.compile(result, dqs=DataQualityState.DEGRADED)
        self.assertEqual(degraded.grade, CandidateGrade.B_HIGH_QUALITY)
        self.assertEqual(degraded.execution_state, ExecutionState.WAIT)

        alternate_store = Path(self.tempdir.name) / "broken.sqlite3"
        self.store_path = alternate_store
        broken = self.compile(result, dqs=DataQualityState.BROKEN)
        self.assertEqual(broken.execution_state, ExecutionState.HARD_CANCELLED)

    def test_pre925_weak_to_strong_never_returns_a_or_armed(self):
        view = self.view(as_of="09:24:30")
        result = self.evaluate(view=view, snapshot=self.snapshot(as_of="09:24:30"))
        self.assertNotIn(result.grade_recommendation, (CandidateGrade.A1, CandidateGrade.A2))
        decision = self.compile(result, decided_at=ts("09:24:30"))
        self.assertNotIn(decision.grade, (CandidateGrade.A1, CandidateGrade.A2))
        self.assertNotEqual(decision.execution_state, ExecutionState.ARMED)

    def test_identity_recovery_cannot_see_future_competitor_weakness(self):
        all_ticks = (
            tick("000001.SZ", "09:21:00", 5.0),
            tick("000300.SZ", "09:21:00", 6.0),
            tick("000300.SZ", "09:25:00", 1.0),
        )
        early_view = AsOfMarketView(
            "2026-08-14", ts("09:21:00"), ticks=all_ticks,
            peer_groups={"direct_competitors": ("000300.SZ",)},
            market_context_records=(MarketContextRecord(self.market(), ts("09:14:00")),),
        )
        result = self.evaluate(
            view=early_view,
            snapshot=self.snapshot(as_of="09:21:00"),
            previous_theme_rank=.95,
        )
        self.assertEqual(result.identity_recovery, IdentityRecoveryState.PARTIAL)


if __name__ == "__main__":
    unittest.main()
