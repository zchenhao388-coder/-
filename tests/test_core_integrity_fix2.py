import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.pipeline import AuctionDecisionPipeline, TimedDecisionInputs
from auction.features import AuctionFeatureEngine
from auction.one_to_two import OneToTwoEngine, OneToTwoInput
from config.thresholds import ThresholdRegistry
from domain.context import deserialize_night_plan, night_plan_context_id, serialize_night_plan
from domain.enums import CandidateGrade, DataQualityState, ExecutionState, PermissionState, ReplayMode, SetupType, ValidationState
from domain.models import DataQualityReport, MarketContext, NightPlan
from execution.compiler import CompilerInput, DecisionCompiler
from expectation.benchmark import BenchmarkKey, BenchmarkObservation, FeaturesAsOf, HierarchicalConditionalBenchmark
from expectation.observations import AuctionGapObservation
from expectation.surprise import PointInTimeAuctionSurprisePercentile
from market.asof import AsOfMarketView, MarketContextRecord
from market.dqs import DataQualityService
from replay.clock import ReplayEngine, VirtualClock
from storage.hard_cancel import SQLiteHardCancelStore
from tests.helpers import make_snapshot, make_ticks, make_view, ts


class CoreIntegrityFix2Tests(unittest.TestCase):
    def setUp(self):
        self.one_cfg = ThresholdRegistry.load("config/thresholds/one_to_two.yaml")
        self.exec_cfg = ThresholdRegistry.load("config/thresholds/execution.yaml")
        self.expect_cfg = ThresholdRegistry.load("config/thresholds/expectation.yaml")
        self.key = BenchmarkKey("ONE_TO_TWO", "RISK_ON", "MAIN", "LEADER", "H1", "CHANGE_HAND")
        self.cutoff = datetime.fromisoformat("2026-08-13T23:59:59+08:00")
        self.tempdir = tempfile.TemporaryDirectory()
        self.store_path = Path(self.tempdir.name) / "hard-cancel.sqlite3"

    def tearDown(self):
        self.tempdir.cleanup()

    def plan(self, generated_at=None, cutoff=None, pool=("000001.SZ", "000002.SZ")):
        return NightPlan(
            "2026-08-14",
            pool,
            {ticker: SetupType.ONE_TO_TWO for ticker in reversed(pool)},
            generated_at or datetime.fromisoformat("2026-08-13T20:00:00+08:00"),
            cutoff or self.cutoff,
        )

    def market(self):
        return MarketContext(
            trade_date="2026-08-14", phase="UP", regime="RISK_ON",
            market_permission=PermissionState.ALLOW,
            setup_permissions={SetupType.ONE_TO_TWO: PermissionState.ALLOW},
            position_permission=PermissionState.ALLOW,
            theme_validation=ValidationState.VALID,
        )

    def benchmark_observation(self, index, gap):
        return BenchmarkObservation(
            f"bench-{index}",
            f"2026-08-{index + 1:02d}",
            f"0000{index:02d}.SZ",
            FeaturesAsOf(self.key.feature_mapping(), datetime.fromisoformat("2026-08-01T15:00:00+08:00"), "FEATURES_V1"),
            datetime.fromisoformat("2026-08-01T23:00:00+08:00"),
            self.key,
            "KEY_V1",
            gap,
            1_000_000.0,
            100_000.0,
            self.cutoff,
            "SOURCE_V1",
        )

    def expectation_with_count(self, count):
        observations = tuple(self.benchmark_observation(index, float(index)) for index in range(1, count + 1))
        view = AsOfMarketView(
            "2026-08-14", ts("09:25:00"), expectation_observations=observations,
        )
        expectation = HierarchicalConditionalBenchmark(self.expect_cfg).estimate_for_night_plan(
            view,
            "000001.SZ",
            self.key,
            datetime.fromisoformat("2026-08-13T20:00:00+08:00"),
            self.cutoff,
        )
        return expectation, observations

    def evaluate_strong_candidate(self, expectation, surprise_sample_count):
        gap_observations = tuple(
            AuctionGapObservation(f"gap-{index}", f"2026-08-{index + 1:02d}", self.cutoff, "SRC", "KEY", self.key, float(index))
            for index in range(1, surprise_sample_count + 1)
        )
        replay = ReplayEngine(
            make_ticks([("09:15:00", 5.0), ("09:20:00", 8.0), ("09:25:00", 10.0)]),
            VirtualClock(ts("09:14:59")),
        )
        list(replay.run(ReplayMode.CHECKPOINT, checkpoints=(ts("09:25:00"),)))
        pipeline = AuctionDecisionPipeline(
            AuctionFeatureEngine(self.one_cfg, PointInTimeAuctionSurprisePercentile(self.expect_cfg)),
            OneToTwoEngine(self.one_cfg),
            DataQualityService(self.exec_cfg),
            DecisionCompiler(self.exec_cfg, SQLiteHardCancelStore(self.store_path)),
        )
        return pipeline.evaluate_checkpoint(
            self.plan(), replay, "000001.SZ", expectation,
            TimedDecisionInputs(ts("09:25:00"), ValidationState.VALID, ValidationState.VALID, ValidationState.VALID, True, 1, 1.0),
            (MarketContextRecord(self.market(), ts("09:14:00")),),
            auction_gap_observations=gap_observations,
            peer_groups={name: ("000001.SZ",) for name in ("liquidity", "theme", "global", "height")},
        )

    def assert_minimum_sample_gate_blocks_strong_candidate(self, result):
        self.assertGreaterEqual(result.setup_result.aqs, self.exec_cfg.get("compiler_aqs_min"))
        self.assertGreaterEqual(result.setup_result.hvs, self.exec_cfg.get("compiler_hvs_min"))
        self.assertEqual(result.data_quality.state, DataQualityState.GOOD)
        gate = next(step for step in result.decision.steps if step.gate == "BenchmarkMinimumSample")
        self.assertFalse(gate.passed)
        self.assertEqual(gate.reason, "BENCHMARK_MINIMUM_SAMPLE_FAILED")
        self.assertNotIn(result.decision.grade, (CandidateGrade.A1, CandidateGrade.A2))
        self.assertNotEqual(result.decision.execution_state, ExecutionState.ARMED)

    def test_broadest_fallback_below_minimum_is_explicitly_insufficient(self):
        expectation, _ = self.expectation_with_count(4)
        self.assertEqual(expectation.distribution.sample_size, 4)
        self.assertEqual(expectation.distribution.status.value, "INSUFFICIENT")
        self.assertEqual(expectation.distribution.effective_sample_size, 4)
        self.assertEqual(expectation.distribution.minimum_sample_size, 5)
        self.assertEqual(expectation.distribution.peer_similarity, 0.0)

    def test_extreme_gap_cannot_bypass_insufficient_benchmark_samples(self):
        expectation, _ = self.expectation_with_count(4)
        result = self.evaluate_strong_candidate(expectation, surprise_sample_count=5)
        self.assertEqual(result.features.values["AuctionSurprisePercentile"], 1.0)
        self.assertEqual(result.features.benchmark_status.value, "INSUFFICIENT")
        self.assertEqual(result.features.surprise_status.value, "SUFFICIENT")
        self.assert_minimum_sample_gate_blocks_strong_candidate(result)

    def test_extreme_gap_cannot_bypass_insufficient_surprise_samples(self):
        expectation, _ = self.expectation_with_count(5)
        result = self.evaluate_strong_candidate(expectation, surprise_sample_count=4)
        self.assertEqual(result.features.values["AuctionSurprisePercentile"], 1.0)
        self.assertEqual(result.features.benchmark_status.value, "SUFFICIENT")
        self.assertEqual(result.features.surprise_status.value, "INSUFFICIENT")
        self.assert_minimum_sample_gate_blocks_strong_candidate(result)

    def test_logically_identical_reconstructed_plan_has_same_context_identity(self):
        original = self.plan()
        serialized = serialize_night_plan(original)
        reconstructed = deserialize_night_plan(serialized)
        self.assertEqual(night_plan_context_id(original), night_plan_context_id(reconstructed))

        equivalent = self.plan(
            generated_at=datetime.fromisoformat("2026-08-13T20:01:00+08:00"),
            cutoff=original.information_available_at.astimezone(timezone.utc),
            pool=tuple(reversed(original.candidate_pool)),
        )
        self.assertEqual(night_plan_context_id(original), night_plan_context_id(equivalent))

    def test_hard_cancel_survives_logically_equivalent_plan_reconstruction(self):
        original = self.plan()
        reconstructed = deserialize_night_plan(serialize_night_plan(original))
        view = make_view(make_ticks([("09:25:00", 5.0)]))
        snapshot = make_snapshot({
            "NormalizedEG": 2.0, "AuctionSurprisePercentile": .99,
            "ThemePeerRank": 1.0, "GlobalPeerRank": 1.0, "HeightPeerRank": 1.0,
            "HistoricalAmountPercentile": 1.0, "HistoricalVolumePercentile": 1.0,
            "PeerAmountPercentile": 1.0, "PeerVolumePercentile": 1.0,
        })
        setup = OneToTwoEngine(self.one_cfg).evaluate(
            OneToTwoInput("000001.SZ", view, snapshot, ValidationState.VALID, 1.0)
        )
        base = dict(
            trade_date="2026-08-14", decided_at=ts("09:25:00"), market_context=self.market(), setup_result=setup,
            regulatory_validation=ValidationState.VALID, market_validation=ValidationState.VALID,
            candidate_validation=ValidationState.VALID, setup_requirements_met=True,
            auction_percentile=.99, cross_setup_rank=1,
        )
        first = DecisionCompiler(self.exec_cfg, SQLiteHardCancelStore(self.store_path)).compile(CompilerInput(
            night_plan=original,
            data_quality=DataQualityReport(DataQualityState.BROKEN, (), ("NO_DATA",), 0.0),
            **base,
        ))
        second = DecisionCompiler(self.exec_cfg, SQLiteHardCancelStore(self.store_path)).compile(CompilerInput(
            night_plan=reconstructed,
            data_quality=DataQualityReport(DataQualityState.GOOD, (), (), 1.0),
            **base,
        ))
        self.assertEqual(first.execution_state, ExecutionState.HARD_CANCELLED)
        self.assertEqual(second.execution_state, ExecutionState.HARD_CANCELLED)

    def test_truly_different_plan_has_different_context_identity(self):
        original = self.plan()
        changed = self.plan(pool=("000001.SZ",))
        self.assertNotEqual(night_plan_context_id(original), night_plan_context_id(changed))


if __name__ == "__main__":
    unittest.main()
