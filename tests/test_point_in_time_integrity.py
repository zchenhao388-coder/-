import unittest
from dataclasses import replace
from datetime import datetime

from auction.features import AuctionFeatureEngine
from auction.one_to_two import OneToTwoEngine, OneToTwoInput
from app.pipeline import AuctionDecisionPipeline
from config.thresholds import ThresholdRegistry
from domain.enums import SetupType, ValidationState
from domain.models import NightPlan
from expectation.benchmark import BenchmarkKey, FeaturesAsOf
from expectation.observations import AuctionGapObservation, PointInTimeObservationBuilder
from expectation.surprise import PointInTimeAuctionSurprisePercentile
from market.asof import AsOfMarketView, PrecomputedFeatureRecord
from market.peer import PeerEngine
from replay.clock import FutureDataAccessError
from tests.helpers import make_expectation, make_snapshot, make_ticks, make_view, ts


class PointInTimeIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.one_cfg = ThresholdRegistry.load("config/thresholds/one_to_two.yaml")
        self.key = BenchmarkKey("ONE_TO_TWO", "RISK_ON", "MAIN", "LEADER", "H1", "CHANGE_HAND")

    def test_feature_and_peer_view_cannot_see_future_tick(self):
        ticks = make_ticks([("09:21:00", 2.0), ("09:25:00", 9.0)])
        view = AsOfMarketView(
            "2026-08-14", ts("09:21:00"), ticks=ticks, peer_groups={"theme": ("000001.SZ",)},
        )
        snapshot = AuctionFeatureEngine(self.one_cfg).compute(view, "000001.SZ", make_expectation())
        self.assertEqual(snapshot.values["ActualAuctionGap"], 2.0)
        self.assertEqual(PeerEngine.latest_values(view, "theme", "gap_pct"), (2.0,))

    def test_setup_engine_rejects_future_precomputed_snapshot(self):
        view = make_view(make_ticks([("09:21:00", 2.0)]), as_of=ts("09:21:00"))
        future = make_snapshot({"NormalizedEG": 9.0}, as_of=ts("09:25:00"))
        with self.assertRaises(FutureDataAccessError):
            OneToTwoEngine(self.one_cfg).evaluate(
                OneToTwoInput("000001.SZ", view, future, ValidationState.VALID, 1.0)
            )

    def test_future_precomputed_feature_is_not_visible(self):
        view = AsOfMarketView(
            "2026-08-14", ts("09:21:00"),
            precomputed_features=(PrecomputedFeatureRecord("000001.SZ", "Rank", 1.0, ts("09:25:00")),),
        )
        self.assertIsNone(view.get_precomputed_feature("000001.SZ", "Rank"))

    def test_observation_builder_rejects_feature_and_label_leakage(self):
        plan = NightPlan(
            "2026-08-14", ("000001.SZ",), {"000001.SZ": SetupType.ONE_TO_TWO},
            datetime.fromisoformat("2026-08-13T20:00:00+08:00"),
            datetime.fromisoformat("2026-08-13T23:00:00+08:00"),
        )
        builder = PointInTimeObservationBuilder()
        args = dict(
            night_plan=plan, observation_id="obs-1", auction_trade_date="2026-08-14",
            ticker="000001.SZ", features_as_of=FeaturesAsOf(
                self.key.feature_mapping(),
                datetime.fromisoformat("2026-08-13T21:00:00+08:00"), "FEATURES_V1",
            ),
            benchmark_key=self.key, benchmark_key_source="KEY_V1", actual_next_auction_gap=5.0,
            auction_amount=1_000_000.0, auction_volume=100_000.0,
            label_available_at=datetime.fromisoformat("2026-08-14T09:25:00+08:00"), source_version="SOURCE_V1",
        )
        observation = builder.build_benchmark_observation(**args)
        self.assertEqual(observation.actual_next_auction_gap, 5.0)
        with self.assertRaises(ValueError):
            builder.build_benchmark_observation(**{**args, "features_as_of": FeaturesAsOf(
                self.key.feature_mapping(), datetime.fromisoformat("2026-08-14T09:25:00+08:00"), "FEATURES_V1",
            )})
        with self.assertRaises(ValueError):
            builder.build_benchmark_observation(**{**args, "label_available_at": datetime.fromisoformat("2026-08-13T22:00:00+08:00")})
        with self.assertRaises(ValueError):
            builder.build_benchmark_observation(**{**args, "features_as_of": FeaturesAsOf(
                {**self.key.feature_mapping(), "actual_next_auction_gap": 5.0}, datetime.fromisoformat("2026-08-13T21:00:00+08:00"), "FEATURES_V1",
            )})
        with self.assertRaises(ValueError):
            builder.build_benchmark_observation(**{**args, "features_as_of": FeaturesAsOf(
                {**self.key.feature_mapping(), "regime": "RISK_OFF"},
                datetime.fromisoformat("2026-08-13T21:00:00+08:00"), "FEATURES_V1",
            )})

    def test_surprise_excludes_current_and_future_trade_dates(self):
        cutoff = datetime.fromisoformat("2026-08-13T23:59:59+08:00")
        observations = (
            AuctionGapObservation("old-1", "2026-08-12", cutoff, "SRC", "KEY", self.key, 1.0),
            AuctionGapObservation("old-2", "2026-08-13", cutoff, "SRC", "KEY", self.key, 3.0),
            AuctionGapObservation("current", "2026-08-14", ts("09:25:00"), "SRC", "KEY", self.key, 99.0),
        )
        view = AsOfMarketView("2026-08-14", ts("09:25:00"), auction_gap_observations=observations)
        percentile = PointInTimeAuctionSurprisePercentile(
            ThresholdRegistry.load("config/thresholds/expectation.yaml")
        ).percentile(view, self.key, 2.0, cutoff)
        self.assertEqual(percentile.percentile, .5)
        self.assertEqual(percentile.status.value, "INSUFFICIENT")

    def test_view_rejects_request_for_future_cutoff(self):
        view = AsOfMarketView("2026-08-14", ts("09:21:00"))
        with self.assertRaises(FutureDataAccessError):
            view.get_expectation_observations(ts("09:25:00"))

    def test_pipeline_rejects_expectation_after_night_plan_cutoff(self):
        expectation = make_expectation()
        invalid = replace(
            expectation,
            information_available_at=datetime.fromisoformat("2026-08-14T09:25:00+08:00"),
            generated_at=datetime.fromisoformat("2026-08-14T09:25:00+08:00"),
        )
        plan = NightPlan(
            "2026-08-14", ("000001.SZ",), {"000001.SZ": SetupType.ONE_TO_TWO},
            datetime.fromisoformat("2026-08-13T20:00:00+08:00"),
            datetime.fromisoformat("2026-08-13T23:59:59+08:00"),
        )
        with self.assertRaises(ValueError):
            AuctionDecisionPipeline._validate_night_expectation(plan, invalid)


if __name__ == "__main__":
    unittest.main()
