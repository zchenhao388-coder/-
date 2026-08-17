import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from config.thresholds import ThresholdRegistry
from domain.context import deserialize_night_plan, serialize_night_plan
from domain.enums import (
    AccountAuctionState,
    AuctionOpenConsistency,
    AuthenticityState,
    BenchmarkStatus,
    CandidateGrade,
    DataQualityState,
    ExecutionMode,
    ExecutionState,
    IdentityRecoveryState,
    LeadershipState,
    OpenPhase,
    SetupType,
    SignalTradability,
    ValidationState,
)
from domain.models import DataQualityReport, DecisionTrace, NightPlan, OpenTick, SetupResult
from execution.cross_setup import CrossSetupResolver
from execution.open_execution import (
    OpenExecutionEngine,
    OpenExecutionPlanFactory,
    OpenValidationInput,
    open_phase_at,
)
from market.asof import AsOfMarketView
from storage.hard_cancel import SQLiteHardCancelStore
from tests.helpers import ts


def setup_result(ticker, setup_type, grade, aqs=90.0, hvs=90.0, **overrides):
    values = dict(
        ticker=ticker,
        setup_type=setup_type,
        expectation_score=90.0,
        authenticity_score=100.0,
        relative_strength_score=90.0,
        liquidity_score=90.0,
        context_score=90.0,
        hvs=hvs,
        aqs=aqs,
        validation_state=ValidationState.VALID,
        authenticity_state=AuthenticityState.AUTHENTIC,
        flags=(),
        grade_recommendation=grade,
        computed_as_of=ts("09:25:00"),
        benchmark_status=BenchmarkStatus.SUFFICIENT,
        surprise_status=BenchmarkStatus.SUFFICIENT,
    )
    values.update(overrides)
    return SetupResult(**values)


def open_tick(ticker, clock, price, open_price=10.0, amount_multiplier=100.0):
    timestamp = ts(clock)
    return OpenTick(
        timestamp,
        timestamp,
        ticker,
        price,
        100.0,
        price * amount_multiplier,
        open_price,
        max(open_price, price),
        min(open_price, price),
        source="TEST",
    )


class CrossSetupResolverTests(unittest.TestCase):
    def plan(self):
        return NightPlan(
            "2026-08-14",
            ("000001.SZ", "000002.SZ"),
            {"000001.SZ": SetupType.ONE_TO_TWO, "000002.SZ": SetupType.HIGH_BOARD},
            datetime.fromisoformat("2026-08-13T20:00:00+08:00"),
            datetime.fromisoformat("2026-08-13T23:59:59+08:00"),
        )

    def test_primary_setup_wins_and_secondary_is_evidence_only(self):
        primary = setup_result("000001.SZ", SetupType.ONE_TO_TWO, CandidateGrade.A2, reason_codes=("PRIMARY",))
        secondary = setup_result(
            "000001.SZ", SetupType.WEAK_TO_STRONG, CandidateGrade.A1,
            reason_codes=("IDENTITY_RECOVERED",), flags=("REPAIR_SUPPORT",),
        )
        other = setup_result("000002.SZ", SetupType.HIGH_BOARD, CandidateGrade.B_HIGH_QUALITY)
        resolved = CrossSetupResolver().resolve(self.plan(), (secondary, other, primary))
        ticker_one = next(item for item in resolved if item.ticker == "000001.SZ")
        self.assertEqual(ticker_one.primary_result.setup_type, SetupType.ONE_TO_TWO)
        self.assertEqual(len(ticker_one.secondary_results), 1)
        self.assertIn("IDENTITY_RECOVERED", ticker_one.supporting_evidence)
        self.assertEqual(len({item.ticker for item in resolved}), len(resolved))

    def test_cross_setup_rank_is_unique_and_quality_ordered(self):
        first = setup_result("000001.SZ", SetupType.ONE_TO_TWO, CandidateGrade.A2, aqs=80, hvs=80)
        second = setup_result("000002.SZ", SetupType.HIGH_BOARD, CandidateGrade.B_HIGH_QUALITY, aqs=99, hvs=99)
        resolved = CrossSetupResolver().resolve(self.plan(), (second, first))
        self.assertEqual([item.ticker for item in resolved], ["000001.SZ", "000002.SZ"])
        self.assertEqual([item.cross_setup_rank for item in resolved], [1, 2])
        self.assertEqual([item.setup_quality_percentile for item in resolved], [1.0, .5])

    def test_missing_primary_setup_result_is_rejected(self):
        secondary_only = setup_result("000001.SZ", SetupType.WEAK_TO_STRONG, CandidateGrade.A1)
        with self.assertRaises(ValueError):
            CrossSetupResolver().resolve(self.plan(), (secondary_only,))

    def test_account_state_uses_compiled_decisions_not_setup_recommendations(self):
        resolver = CrossSetupResolver()
        armed = DecisionTrace("000001.SZ", ts("09:25:00"), CandidateGrade.A1, ExecutionState.ARMED)
        waiting = DecisionTrace("000001.SZ", ts("09:25:00"), CandidateGrade.B_CONFIRMATION, ExecutionState.WAIT)
        cash = DecisionTrace("000001.SZ", ts("09:25:00"), CandidateGrade.DROP, ExecutionState.HARD_CANCELLED)
        self.assertEqual(resolver.account_state((armed,)), AccountAuctionState.EXECUTION_READY)
        self.assertEqual(resolver.account_state((waiting,)), AccountAuctionState.WAIT_OPEN)
        self.assertEqual(resolver.account_state((cash,)), AccountAuctionState.CASH)


class OpenExecutionMilestoneTests(unittest.TestCase):
    def setUp(self):
        self.cfg = ThresholdRegistry.load("config/thresholds/execution.yaml")
        self.tempdir = tempfile.TemporaryDirectory()
        self.store_path = Path(self.tempdir.name) / "hard-cancel.sqlite3"
        self.plan_factory = OpenExecutionPlanFactory()

    def tearDown(self):
        self.tempdir.cleanup()

    def night_plan(self, setup=SetupType.ONE_TO_TWO):
        return NightPlan(
            "2026-08-14", ("000001.SZ",), {"000001.SZ": setup},
            datetime.fromisoformat("2026-08-13T20:00:00+08:00"),
            datetime.fromisoformat("2026-08-13T23:59:59+08:00"),
        )

    def open_plan(self, setup=SetupType.ONE_TO_TWO, grade=CandidateGrade.A1, state=ExecutionState.ARMED, **setup_overrides):
        night = self.night_plan(setup)
        result = setup_result("000001.SZ", setup, grade, **setup_overrides)
        decision = DecisionTrace("000001.SZ", ts("09:25:00"), grade, state)
        return night, self.plan_factory.create(night, decision, result, 10.0, .8)

    def view(self, points, as_of=None, extra=()):
        ticks = [open_tick("000001.SZ", clock, price) for clock, price in points]
        ticks.extend(extra)
        boundary = ts(as_of or points[-1][0])
        return AsOfMarketView(
            "2026-08-14", boundary, open_ticks=ticks,
            peer_groups={"theme_open": ("000001.SZ",)},
        )

    def validation(self, plan, night, view, dqs=DataQualityState.GOOD, **overrides):
        values = dict(
            plan=plan,
            night_plan=night,
            market_view=view,
            data_quality=DataQualityReport(dqs, (), (), 1.0),
            regulatory_validation=ValidationState.VALID,
            sector_validation=ValidationState.VALID,
        )
        values.update(overrides)
        return OpenValidationInput(**values)

    def engine(self):
        return OpenExecutionEngine(self.cfg, SQLiteHardCancelStore(self.store_path))

    def test_structural_open_phase_boundaries(self):
        self.assertEqual(open_phase_at(ts("09:29:59")), OpenPhase.PRE_OPEN)
        self.assertEqual(open_phase_at(ts("09:30:00")), OpenPhase.OPEN_SHOCK)
        self.assertEqual(open_phase_at(ts("09:30:15")), OpenPhase.OPEN_CONFIRM)
        self.assertEqual(open_phase_at(ts("09:31:00")), OpenPhase.FOLLOW_THROUGH)
        self.assertEqual(open_phase_at(ts("09:35:00")), OpenPhase.EXPIRED)

    def test_a_is_armed_plan_not_unconditional_buy_then_executes_on_confirmation(self):
        night, plan = self.open_plan()
        self.assertEqual(plan.state, ExecutionState.ARMED)
        early = self.engine().evaluate(self.validation(plan, night, self.view((("09:30:00", 10.0), ("09:30:05", 10.02)))))
        self.assertEqual(early.state, ExecutionState.WAIT)
        self.assertEqual(early.consistency, AuctionOpenConsistency.PARTIAL)

        confirmed_view = self.view((("09:30:00", 10.0), ("09:30:15", 10.1)))
        confirmed = self.engine().evaluate(self.validation(plan, night, confirmed_view))
        self.assertEqual(confirmed.state, ExecutionState.EXECUTE)
        self.assertEqual(confirmed.tradability, SignalTradability.VALID_AND_TRADABLE)
        self.assertEqual(confirmed.trigger_price, 10.1)

    def test_extended_signal_is_no_chase_wait(self):
        night, plan = self.open_plan()
        view = self.view((("09:30:00", 10.0), ("09:30:15", 10.4)))
        decision = self.engine().evaluate(self.validation(plan, night, view))
        self.assertEqual(decision.state, ExecutionState.WAIT)
        self.assertEqual(decision.tradability, SignalTradability.VALID_BUT_EXTENDED)
        self.assertIn("NO_CHASE", decision.reason_codes)

    def test_b_waiting_requires_upgrade_before_execute(self):
        night, plan = self.open_plan(grade=CandidateGrade.B_HIGH_QUALITY, state=ExecutionState.WAIT)
        self.assertEqual(plan.state, ExecutionState.WAITING)
        view = self.view((("09:30:00", 10.0), ("09:30:15", 10.1)))
        pending = self.engine().evaluate(self.validation(plan, night, view, upgrade_requirements_met=False))
        upgraded = self.engine().evaluate(self.validation(plan, night, view, upgrade_requirements_met=True))
        self.assertEqual(pending.state, ExecutionState.WAIT)
        self.assertIn("UPGRADE_REQUIREMENT_PENDING", pending.reason_codes)
        self.assertEqual(upgraded.state, ExecutionState.EXECUTE)

    def test_dqs_degraded_waits_and_broken_cancel_is_sticky_across_rebuild(self):
        night, plan = self.open_plan()
        view = self.view((("09:30:00", 10.0), ("09:30:15", 10.1)))
        degraded = self.engine().evaluate(self.validation(plan, night, view, dqs=DataQualityState.DEGRADED))
        self.assertEqual(degraded.state, ExecutionState.WAIT)

        cancelled = self.engine().evaluate(self.validation(plan, night, view, dqs=DataQualityState.BROKEN))
        self.assertEqual(cancelled.state, ExecutionState.CANCELLED)
        reconstructed = deserialize_night_plan(serialize_night_plan(night))
        sticky = OpenExecutionEngine(self.cfg, SQLiteHardCancelStore(self.store_path)).evaluate(
            self.validation(plan, reconstructed, view, dqs=DataQualityState.GOOD)
        )
        self.assertEqual(sticky.state, ExecutionState.CANCELLED)
        self.assertIn("HARD_CANCEL_STICKY", sticky.reason_codes)

    def test_setup_specific_hard_cancels(self):
        high_night, high_plan = self.open_plan(SetupType.HIGH_BOARD, leadership_state=LeadershipState.DOMINANT)
        view = self.view((("09:30:00", 10.0), ("09:30:15", 10.1)))
        high_cancel = self.engine().evaluate(self.validation(
            high_plan, high_night, view, leadership_state=LeadershipState.LOST,
        ))
        self.assertEqual(high_cancel.state, ExecutionState.CANCELLED)
        self.assertIn("LEADERSHIP_LOST", high_cancel.reason_codes)

        weak_store = Path(self.tempdir.name) / "weak.sqlite3"
        weak_night, weak_plan = self.open_plan(
            SetupType.WEAK_TO_STRONG,
            identity_recovery=IdentityRecoveryState.TRUE,
        )
        weak_engine = OpenExecutionEngine(self.cfg, SQLiteHardCancelStore(weak_store))
        weak_cancel = weak_engine.evaluate(self.validation(
            weak_plan, weak_night, view, weakness_reappeared=True,
        ))
        self.assertEqual(weak_cancel.state, ExecutionState.CANCELLED)
        self.assertIn("WEAKNESS_REAPPEARS", weak_cancel.reason_codes)

    def test_untriggered_signal_expires_at_0935(self):
        night, plan = self.open_plan()
        view = self.view((("09:30:00", 10.0),), as_of="09:35:00")
        decision = self.engine().evaluate(self.validation(plan, night, view))
        self.assertEqual(decision.phase, OpenPhase.EXPIRED)
        self.assertEqual(decision.state, ExecutionState.EXPIRED)

    def test_open_feature_engine_cannot_see_future_tick(self):
        night, plan = self.open_plan()
        future = open_tick("000001.SZ", "09:30:15", 10.5)
        view = self.view((("09:30:00", 10.0), ("09:30:05", 10.02)), as_of="09:30:05", extra=(future,))
        decision = self.engine().evaluate(self.validation(plan, night, view))
        self.assertEqual(decision.features.values["LastPrice"], 10.02)
        self.assertIsNone(decision.features.values["Return15s"])
        self.assertEqual(decision.state, ExecutionState.WAIT)

    def test_c_and_drop_do_not_receive_open_plan(self):
        night = self.night_plan()
        for grade, state in (
            (CandidateGrade.C_NIGHT_DEGRADED, ExecutionState.WAIT),
            (CandidateGrade.DROP, ExecutionState.HARD_CANCELLED),
        ):
            with self.subTest(grade=grade.value):
                result = setup_result("000001.SZ", SetupType.ONE_TO_TWO, grade)
                decision = DecisionTrace("000001.SZ", ts("09:25:00"), grade, state)
                with self.assertRaises(ValueError):
                    self.plan_factory.create(night, decision, result, 10.0, .8)


if __name__ == "__main__":
    unittest.main()
