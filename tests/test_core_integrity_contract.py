import inspect
import tempfile
import unittest
from dataclasses import replace
from datetime import timezone
from pathlib import Path

from adapters.eastmoney import EastmoneySnapshotAdapter
from auction.features import AuctionFeatureEngine
from auction.one_to_two import OneToTwoEngine, OneToTwoInput
from auction.phase import auction_phase_at
from config.thresholds import ThresholdRegistry
from domain.enums import AuthenticityState, CandidateGrade, DataQualityState, ExecutionState, PermissionState, SetupType, ValidationState
from domain.models import DataQualityReport, MarketContext
from execution.compiler import CompilerInput, DecisionCompiler
from expectation.observations import PointInTimeObservationBuilder
from expectation.surprise import PointInTimeAuctionSurprisePercentile
from market.asof import AsOfMarketView, PrecomputedFeatureRecord
from storage.hard_cancel import SQLiteHardCancelStore
from tests.helpers import make_expectation, make_night_plan, make_snapshot, make_ticks, make_view, ts


class CoreIntegrityContractTests(unittest.TestCase):
    def setUp(self):
        self.one_cfg = ThresholdRegistry.load("config/thresholds/one_to_two.yaml")
        self.exec_cfg = ThresholdRegistry.load("config/thresholds/execution.yaml")
        self.tempdir = tempfile.TemporaryDirectory()
        self.store_path = Path(self.tempdir.name) / "hard-cancel.sqlite3"

    def tearDown(self):
        self.tempdir.cleanup()

    def market(self):
        return MarketContext(
            trade_date="2026-08-14", phase="UP", regime="RISK_ON",
            market_permission=PermissionState.ALLOW,
            setup_permissions={SetupType.ONE_TO_TWO: PermissionState.ALLOW},
            position_permission=PermissionState.ALLOW,
            theme_validation=ValidationState.VALID,
        )

    def strong_result(self):
        view = make_view(make_ticks([("09:25:00", 5.0)]))
        snapshot = make_snapshot({
            "NormalizedEG": 2.0, "AuctionSurprisePercentile": .95,
            "ThemePeerRank": 1.0, "GlobalPeerRank": 1.0, "HeightPeerRank": 1.0,
            "HistoricalAmountPercentile": 1.0, "HistoricalVolumePercentile": 1.0,
            "PeerAmountPercentile": 1.0, "PeerVolumePercentile": 1.0,
        })
        return OneToTwoEngine(self.one_cfg).evaluate(
            OneToTwoInput("000001.SZ", view, snapshot, ValidationState.VALID, 1.0)
        )

    def compiler(self):
        return DecisionCompiler(self.exec_cfg, SQLiteHardCancelStore(self.store_path))

    def compiler_input(self, result, decided_at, **overrides):
        values = dict(
            trade_date="2026-08-14", decided_at=decided_at, night_plan=make_night_plan(),
            market_context=self.market(), setup_result=result,
            regulatory_validation=ValidationState.VALID,
            data_quality=DataQualityReport(DataQualityState.GOOD, (), (), 1.0),
            market_validation=ValidationState.VALID, candidate_validation=ValidationState.VALID,
            setup_requirements_met=True, auction_percentile=.95, cross_setup_rank=1,
        )
        values.update(overrides)
        return CompilerInput(**values)

    def test_structural_auction_phase_boundaries_exist(self):
        self.assertEqual(auction_phase_at(ts("09:15:00")).value, "SCOUTING")
        self.assertEqual(auction_phase_at(ts("09:20:00")).value, "VALIDATING")
        self.assertEqual(auction_phase_at(ts("09:24:30")).value, "FINALIZING")
        self.assertEqual(auction_phase_at(ts("09:25:00")).value, "FINAL")
        self.assertEqual(auction_phase_at(ts("09:25:00").astimezone(timezone.utc)).value, "FINAL")

    def test_pre20_and_pre25_cannot_arm_or_keep_a_grade(self):
        result = self.strong_result()
        for clock in ("09:15:00", "09:20:00", "09:24:30", "09:24:59"):
            point_in_time_result = replace(result, computed_as_of=ts(clock))
            decision = self.compiler().compile(self.compiler_input(point_in_time_result, ts(clock)))
            self.assertNotIn(decision.grade, (CandidateGrade.A1, CandidateGrade.A2), clock)
            self.assertNotEqual(decision.execution_state, ExecutionState.ARMED, clock)

    def test_pre20_feature_authenticity_is_unknown(self):
        ticks = make_ticks([("09:15:00", 5.0), ("09:19:59", 5.5)])
        snapshot = AuctionFeatureEngine(self.one_cfg).compute(make_view(ticks), "000001.SZ", make_expectation())
        self.assertEqual(snapshot.authenticity_state, AuthenticityState.UNKNOWN)

    def test_feature_engine_requires_asof_view_not_arbitrary_ticks(self):
        parameters = inspect.signature(AuctionFeatureEngine.compute).parameters
        self.assertIn("market_view", parameters)
        self.assertNotIn("ticks", parameters)

    def test_asof_view_filters_future_ticks_and_precomputed_values(self):
        view = AsOfMarketView(
            trade_date="2026-08-14", as_of=ts("09:21:00"),
            ticks=make_ticks([("09:21:00", 2.0), ("09:25:00", 9.0)]),
            precomputed_features=(PrecomputedFeatureRecord("000001.SZ", "FutureFeature", 99.0, ts("09:25:00")),),
        )
        self.assertEqual(len(view.get_ticks("000001.SZ")), 1)
        self.assertIsNone(view.get_precomputed_feature("000001.SZ", "FutureFeature"))

    def test_a_grade_iff_armed_including_failed_score_gate(self):
        decision = self.compiler().compile(
            self.compiler_input(self.strong_result(), ts("09:25:00"), auction_percentile=.2)
        )
        is_a = decision.grade in (CandidateGrade.A1, CandidateGrade.A2)
        self.assertEqual(is_a, decision.execution_state == ExecutionState.ARMED)
        self.assertFalse(is_a)

    def test_hard_cancel_survives_compiler_restart(self):
        broken = DataQualityReport(DataQualityState.BROKEN, (), ("NO_DATA",), 0.0)
        request = self.compiler_input(self.strong_result(), ts("09:25:00"), data_quality=broken)
        first = self.compiler().compile(request)
        second = self.compiler().compile(self.compiler_input(self.strong_result(), ts("09:25:01")))
        self.assertEqual(first.execution_state, ExecutionState.HARD_CANCELLED)
        self.assertEqual(second.execution_state, ExecutionState.HARD_CANCELLED)

    def test_compiler_rejects_inconsistent_trade_dates(self):
        request = self.compiler_input(self.strong_result(), ts("09:25:00"), trade_date="2026-08-13")
        with self.assertRaises(ValueError):
            self.compiler().compile(request)

    def test_missing_liquidity_is_none_and_weights_are_renormalized(self):
        view = make_view(make_ticks([("09:25:00", 5.0)]), include_history=False, include_peers=False)
        snapshot = make_snapshot({
            "NormalizedEG": 2.0, "AuctionSurprisePercentile": .95,
            "ThemePeerRank": 1.0, "GlobalPeerRank": 1.0, "HeightPeerRank": 1.0,
        })
        result = OneToTwoEngine(self.one_cfg).evaluate(
            OneToTwoInput("000001.SZ", view, snapshot, ValidationState.VALID, 1.0)
        )
        self.assertIsNone(result.liquidity_score)
        self.assertAlmostEqual(result.effective_weights["expectation"], 25.0)
        self.assertAlmostEqual(result.effective_weights["authenticity"], 31.25)
        self.assertAlmostEqual(result.effective_weights["relative"], 25.0)
        self.assertAlmostEqual(result.effective_weights["context"], 18.75)

    def test_dqs_degraded_caps_grade_at_b(self):
        degraded = DataQualityReport(DataQualityState.DEGRADED, ("matched_amount",), ("MISSING",), .8)
        decision = self.compiler().compile(
            self.compiler_input(self.strong_result(), ts("09:25:00"), data_quality=degraded)
        )
        self.assertNotIn(decision.grade, (CandidateGrade.A1, CandidateGrade.A2))
        self.assertNotEqual(decision.execution_state, ExecutionState.ARMED)

    def test_eg_is_actual_minus_conditional_expected(self):
        ticks = make_ticks([("09:25:00", 5.0)])
        snapshot = AuctionFeatureEngine(self.one_cfg).compute(make_view(ticks), "000001.SZ", make_expectation())
        self.assertEqual(snapshot.values["EG"], 2.0)
        self.assertEqual(snapshot.values["NormalizedEG"], 1.0)

    def test_recovery_and_close_location_are_none_without_drawdown_or_range(self):
        engine = AuctionFeatureEngine(self.one_cfg)
        self.assertIsNone(engine._recovery_ratio([2.0, 3.0, 4.0]))
        self.assertIsNone(engine._recovery_ratio([4.0, 4.0]))
        self.assertIsNone(engine._close_location([4.0, 4.0]))

    def test_healthy_disagreement_needs_liquidity_late_slope_and_relative_rank(self):
        ticks = make_ticks([("09:20:00", 5.0), ("09:23:00", 3.0), ("09:24:30", 4.8), ("09:25:00", 4.9)])
        view = make_view(ticks, include_history=False, include_peers=False)
        snapshot = AuctionFeatureEngine(self.one_cfg).compute(view, "000001.SZ", make_expectation())
        self.assertEqual(snapshot.authenticity_state, AuthenticityState.HEALTHY_DISAGREEMENT_PENDING)

    def test_point_in_time_surprise_and_observation_builder_exist(self):
        self.assertTrue(PointInTimeAuctionSurprisePercentile)
        self.assertTrue(PointInTimeObservationBuilder)

    def test_expectation_thresholds_include_minimum_local_sample(self):
        registry = ThresholdRegistry.load("config/thresholds/expectation.yaml")
        self.assertGreater(registry.get("minimum_local_sample_size"), 1)

    def test_eastmoney_provider_time_is_not_exchange_time(self):
        payload = b'{"rc":0,"data":{"f43":1050,"f47":10,"f48":10500,"f57":"000001","f60":1000,"f86":1786670700}}'
        adapter = EastmoneySnapshotAdapter(opener=lambda request, timeout: payload, receive_clock=lambda: ts("09:25:01"))
        tick = adapter.fetch_one("000001.SZ")
        self.assertIsNone(tick.exchange_ts)
        self.assertIsNotNone(tick.provider_ts)
        self.assertFalse(adapter.capability.core_fields["exchange_ts"])
        self.assertEqual(set(adapter.capability.allowed_uses), {"FIELD_PROBE", "RAW_CAPTURE", "SHADOW_RESEARCH"})


if __name__ == "__main__":
    unittest.main()
