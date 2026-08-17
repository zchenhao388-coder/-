import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from backtest.calibration import (
    CALIBRATION_ROADMAP,
    CalibrationPhase,
    PopulationDriftChecker,
    ThresholdCalibrator,
    ThresholdVersionBuilder,
    ThresholdVersionStore,
)
from backtest.walk_forward import (
    BacktestObservation,
    OutOfSampleMetrics,
    PointInTimeDataset,
    WalkForwardBacktester,
    WalkForwardConfig,
    WalkForwardSplitter,
)
from config.thresholds import ThresholdRegistry
from domain.enums import OutcomeClass, SetupType, ThresholdStatus


SHANGHAI = timezone(timedelta(hours=8))


def observation(identifier, trade_day, eg=1.0, strategy_return=1.0, label_day=None):
    trade = date.fromisoformat(trade_day)
    feature_time = datetime.combine(trade, datetime.min.time(), tzinfo=SHANGHAI) - timedelta(hours=1)
    label_date = date.fromisoformat(label_day) if label_day else trade + timedelta(days=1)
    label_time = datetime.combine(label_date, datetime.min.time(), tzinfo=SHANGHAI)
    return BacktestObservation(
        identifier,
        trade_day,
        "000001.SZ",
        SetupType.ONE_TO_TWO,
        {"EG": eg},
        feature_time,
        label_time,
        strategy_return,
        OutcomeClass.CLEAN_SUCCESS if strategy_return > 0 else OutcomeClass.FAST_FAILURE,
    )


class WalkForwardBacktestTests(unittest.TestCase):
    def test_labels_are_forbidden_from_features(self):
        with self.assertRaises(ValueError):
            BacktestObservation(
                "bad", "2026-01-02", "000001.SZ", SetupType.ONE_TO_TWO,
                {"strategy_return": 10.0},
                datetime(2026, 1, 1, 23, tzinfo=SHANGHAI),
                datetime(2026, 1, 2, 15, tzinfo=SHANGHAI),
                10.0, OutcomeClass.CLEAN_SUCCESS,
            )

    def test_training_excludes_labels_unavailable_at_validation_cutoff(self):
        normal = observation("normal", "2026-03-01", label_day="2026-03-02")
        delayed = observation("delayed", "2026-03-02", label_day="2026-04-15")
        dataset = PointInTimeDataset((normal, delayed))
        selected = dataset.select(date(2026, 1, 1), date(2026, 4, 1), date(2026, 4, 1))
        self.assertEqual([item.observation_id for item in selected], ["normal"])

    def test_walk_forward_is_chronological_and_forward_is_untouched_by_trainer(self):
        observations = tuple(
            observation(f"m{month}", f"2026-{month:02d}-01", eg=1.0 if month % 2 else -1.0, strategy_return=month / 10.0)
            for month in range(1, 9)
        )
        splitter = WalkForwardSplitter(WalkForwardConfig(3, 1, 1, 1))
        windows = splitter.split(observations)
        self.assertTrue(windows)
        self.assertTrue(all(window.train_end == window.validation_start for window in windows))
        self.assertTrue(all(window.validation_end == window.forward_start for window in windows))

        trainer_seen = []

        def trainer(train):
            trainer_seen.append(tuple(item.trade_date for item in train))
            return lambda features: features["EG"] > 0

        results = WalkForwardBacktester(splitter).run(observations, trainer)
        self.assertEqual(len(results), len(windows))
        for seen, result in zip(trainer_seen, results):
            self.assertTrue(all(date.fromisoformat(item) < result.window.validation_start for item in seen))
            self.assertEqual(result.validation.sample_size, 1)
            self.assertEqual(result.forward.sample_size, 1)

    def test_walk_forward_split_is_deterministic_not_random(self):
        observations = tuple(observation(str(month), f"2026-{month:02d}-01") for month in range(1, 9))
        splitter = WalkForwardSplitter(WalkForwardConfig(3, 1, 1, 1))
        self.assertEqual(splitter.split(observations), splitter.split(tuple(reversed(observations))))


class CalibrationMilestoneTests(unittest.TestCase):
    def setUp(self):
        self.cfg = ThresholdRegistry.load("config/thresholds/calibration.yaml")
        self.calibrator = ThresholdCalibrator(self.cfg)
        self.drift_checker = PopulationDriftChecker(self.cfg)
        self.good_metrics = OutOfSampleMetrics(20, 20, .6, 1.0, .1)

    def test_drift_check_detects_distribution_shift(self):
        stable = self.drift_checker.compare("EG", (0.0, 1.0, 2.0), (0.1, 1.1, 2.1))
        shifted = self.drift_checker.compare("EG", (0.0, 1.0, 2.0), (10.0, 11.0, 12.0))
        self.assertFalse(stable.drifted)
        self.assertTrue(shifted.drifted)

    def test_seed_advances_only_to_calibrated_or_retired_after_sufficient_oos(self):
        stable = self.drift_checker.compare("EG", tuple(range(100)), tuple(range(100)))
        calibrated = self.calibrator.evaluate("aqs_a1_min", 80.0, 100, self.good_metrics, self.good_metrics, stable)
        self.assertEqual(calibrated.status, ThresholdStatus.CALIBRATED)

        bad_forward = OutOfSampleMetrics(20, 20, .2, -1.0, .5)
        retired = self.calibrator.evaluate("aqs_a1_min", 80.0, 100, self.good_metrics, bad_forward, stable)
        self.assertEqual(retired.status, ThresholdStatus.RETIRED)

        insufficient = self.calibrator.evaluate("aqs_a1_min", 80.0, 99, self.good_metrics, self.good_metrics, stable)
        self.assertEqual(insufficient.status, ThresholdStatus.SEED)

    def test_threshold_version_is_persisted_with_calibration_metadata(self):
        registry = ThresholdRegistry.load("config/thresholds/one_to_two.yaml")
        stable = self.drift_checker.compare("AQS", tuple(range(100)), tuple(range(100)))
        decision = self.calibrator.evaluate("aqs_a1_min", 80.0, 100, self.good_metrics, self.good_metrics, stable)
        created = datetime(2026, 8, 16, tzinfo=timezone.utc)
        version = ThresholdVersionBuilder().build(registry, (decision,), "parent-v1", created)
        self.assertEqual(version.thresholds["aqs_a1_min"].status, ThresholdStatus.CALIBRATED)
        self.assertEqual(version.thresholds["aqs_a1_min"].value, 80.0)
        self.assertEqual(version.thresholds["aqs_a1_min"].sample_size, 100)

        with tempfile.TemporaryDirectory() as directory:
            store = ThresholdVersionStore(Path(directory))
            store.write(version)
            loaded = store.load(version.version_id)
        self.assertEqual(loaded.version_id, version.version_id)
        self.assertEqual(loaded.thresholds["aqs_a1_min"], version.thresholds["aqs_a1_min"])

    def test_calibration_priority_remains_single_then_interaction_then_weights(self):
        self.assertEqual(
            tuple(CALIBRATION_ROADMAP),
            (CalibrationPhase.SINGLE_FEATURE, CalibrationPhase.INTERACTION, CalibrationPhase.AQS_WEIGHT),
        )
        self.assertIn("EG", CALIBRATION_ROADMAP[CalibrationPhase.SINGLE_FEATURE])
        self.assertEqual(CALIBRATION_ROADMAP[CalibrationPhase.AQS_WEIGHT], ("AQS_WEIGHTS",))


if __name__ == "__main__":
    unittest.main()
