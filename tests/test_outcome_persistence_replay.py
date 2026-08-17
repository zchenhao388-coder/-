import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from backtest.outcome import OutcomeEngine, OutcomeInput, SetupOutcomeEvidence, diagnose_outcome
from config.thresholds import ThresholdRegistry
from domain.enums import (
    AuctionOpenConsistency,
    CandidateGrade,
    ExecutionState,
    OpenPhase,
    OutcomeClass,
    OutcomeDiagnostic,
    SetupType,
    SignalTradability,
    StorageLayer,
)
from domain.models import (
    DecisionTrace,
    NightPlan,
    OpenExecutionDecision,
    OpenFeatureSnapshot,
    OpenTick,
)
from market.asof import AsOfMarketView, PrecomputedFeatureRecord
from replay.clock import VirtualClock
from replay.diff import ReplayDecisionSnapshot, diff_replay_snapshots
from replay.manifest import (
    ReplayIntegrityError,
    ReplayManifestBuilder,
    ReplayManifestStore,
    ReplayManifestVerifier,
    VerifiedReplayFactory,
)
from storage.jsonl import LayeredResearchStore
from tests.helpers import make_ticks, ts


def open_tick(clock, price):
    timestamp = ts(clock)
    return OpenTick(
        timestamp, timestamp, "000001.SZ", price, 100.0, price * 100.0,
        10.0, max(10.0, price), min(10.0, price), source="TEST",
    )


def night_plan():
    return NightPlan(
        "2026-08-14", ("000001.SZ",), {"000001.SZ": SetupType.ONE_TO_TWO},
        datetime.fromisoformat("2026-08-13T20:00:00+08:00"),
        datetime.fromisoformat("2026-08-13T23:59:59+08:00"),
    )


def open_decision(state=ExecutionState.EXECUTE, price=10.0):
    snapshot = OpenFeatureSnapshot("000001.SZ", "2026-08-14", ts("09:30:00"), {"LastPrice": price})
    return OpenExecutionDecision(
        "000001.SZ", ts("09:30:00"), OpenPhase.OPEN_SHOCK, state,
        AuctionOpenConsistency.CONFIRMED, SignalTradability.VALID_AND_TRADABLE,
        snapshot, (), price if state == ExecutionState.EXECUTE else None,
    )


class LayeredPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_raw_derived_decision_outcome_are_separate_append_only_layers(self):
        store = LayeredResearchStore(self.root / "records")
        records = {
            StorageLayer.RAW: make_ticks((("09:25:00", 5.0),))[0],
            StorageLayer.DERIVED: {"EG": 2.0, "MissingLiquidity": None},
            StorageLayer.DECISION: DecisionTrace("000001.SZ", ts("09:25:00"), CandidateGrade.A1, ExecutionState.ARMED),
            StorageLayer.OUTCOME: {"OutcomeClass": "CLEAN_SUCCESS"},
        }
        for layer, record in records.items():
            store.append(layer, "2026-08-14", "000001.SZ", record, "fingerprint-v1")
        store.append(StorageLayer.RAW, "2026-08-14", "000001.SZ", records[StorageLayer.RAW], "fingerprint-v1")

        for layer in StorageLayer:
            loaded = store.read(layer, "2026-08-14", "000001.SZ")
            self.assertTrue(loaded)
            self.assertEqual(loaded[0]["layer"], layer.value)
            self.assertEqual(loaded[0]["version_fingerprint"], "fingerprint-v1")
        self.assertEqual(len(store.read(StorageLayer.RAW, "2026-08-14", "000001.SZ")), 2)
        self.assertIsNone(store.read(StorageLayer.DERIVED, "2026-08-14", "000001.SZ")[0]["payload"]["MissingLiquidity"])


class ReplayManifestAndDiffTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.raw = self.root / "000001.SZ.jsonl"
        self.config = self.root / "execution.yaml"
        self.raw.write_text('{"tick":1}\n', encoding="utf-8")
        self.config.write_text('{"threshold":1}\n', encoding="utf-8")

    def tearDown(self):
        self.tempdir.cleanup()

    def build_manifest(self, created_at):
        return ReplayManifestBuilder().build(
            night_plan(),
            (self.raw,),
            (self.config,),
            {"auction": "V1", "compiler": "V1"},
            "commit-abc",
            created_at=created_at,
        )

    def test_manifest_identity_is_deterministic_and_contains_no_absolute_paths(self):
        first = self.build_manifest(datetime(2026, 8, 14, tzinfo=timezone.utc))
        second = self.build_manifest(datetime(2026, 8, 15, tzinfo=timezone.utc))
        self.assertEqual(first.manifest_id, second.manifest_id)
        self.assertTrue(all(not item.logical_name.startswith("/") for item in (*first.raw_artifacts, *first.config_artifacts)))
        store = ReplayManifestStore(self.root / "manifests")
        self.assertEqual(store.write(first), store.write(second))

    def test_verified_replay_refuses_changed_raw_or_config_artifacts(self):
        manifest = self.build_manifest(datetime(2026, 8, 14, tzinfo=timezone.utc))
        ReplayManifestVerifier().verify(manifest, (self.raw,), (self.config,))
        engine = VerifiedReplayFactory().create(
            manifest,
            (self.raw,),
            (self.config,),
            make_ticks((("09:15:00", 1.0),)),
            VirtualClock(ts("09:14:59")),
        )
        self.assertIsNotNone(engine.step())

        self.config.write_text('{"threshold":2}\n', encoding="utf-8")
        with self.assertRaises(ReplayIntegrityError):
            VerifiedReplayFactory().create(
                manifest,
                (self.raw,),
                (self.config,),
                make_ticks((("09:15:00", 1.0),)),
                VirtualClock(ts("09:14:59")),
            )

    def test_end_to_end_decision_diff_reports_grade_cancel_and_trigger_changes(self):
        left_final = DecisionTrace("000001.SZ", ts("09:25:00"), CandidateGrade.A1, ExecutionState.ARMED)
        right_final = DecisionTrace("000001.SZ", ts("09:25:00"), CandidateGrade.B_CONFIRMATION, ExecutionState.WAIT)
        left_open = open_decision(ExecutionState.EXECUTE, 10.1)
        right_open = open_decision(ExecutionState.CANCELLED, 10.0)
        left = ReplayDecisionSnapshot({"000001.SZ": left_final}, {"000001.SZ": left_open})
        right = ReplayDecisionSnapshot({"000001.SZ": right_final}, {"000001.SZ": right_open})
        diff = diff_replay_snapshots(left, right)
        self.assertEqual(diff.grade_changes, ("000001.SZ",))
        self.assertEqual(diff.added_cancels, ("000001.SZ",))
        self.assertEqual(diff.trigger_changes, ("000001.SZ",))
        self.assertTrue(diff.details)


class OutcomeMilestoneTests(unittest.TestCase):
    def setUp(self):
        self.cfg = ThresholdRegistry.load("config/thresholds/outcome.yaml")
        self.engine = OutcomeEngine(self.cfg)

    def test_outcome_engine_cannot_see_future_tick(self):
        ticks = (
            open_tick("09:30:00", 10.0),
            open_tick("09:31:00", 10.1),
            open_tick("09:35:00", 10.2),
            open_tick("15:00:00", 9.0),
        )
        view = AsOfMarketView("2026-08-14", ts("09:35:00"), open_ticks=ticks)
        outcome = self.engine.compute(OutcomeInput(
            "000001.SZ", SetupType.ONE_TO_TWO, CandidateGrade.A1,
            view, open_decision(), 11.0,
        ))
        self.assertAlmostEqual(outcome.return_close, 2.0)
        self.assertIsNone(outcome.return_30m)

    def test_complete_outcome_and_d2_label_use_point_in_time_views(self):
        ticks = (
            open_tick("09:30:00", 10.0),
            open_tick("09:31:00", 10.2),
            open_tick("09:35:00", 10.5),
            open_tick("10:00:00", 11.1),
            open_tick("15:00:00", 11.0),
        )
        trade_view = AsOfMarketView("2026-08-14", ts("15:00:00"), open_ticks=ticks)
        label_time = datetime.fromisoformat("2026-08-15T09:30:00+08:00")
        label_view = AsOfMarketView(
            "2026-08-15", label_time,
            precomputed_features=(PrecomputedFeatureRecord("000001.SZ", "D2OpenGap", -8.0, label_time),),
        )
        evidence = SetupOutcomeEvidence(leadership_retained_close=True, high_board_blowup=False)
        outcome = self.engine.compute(OutcomeInput(
            "000001.SZ", SetupType.HIGH_BOARD, CandidateGrade.A1,
            trade_view, open_decision(), 11.0, label_view, evidence,
        ))
        self.assertTrue(outcome.touched_limit_up)
        self.assertTrue(outcome.closed_limit_up)
        self.assertEqual(outcome.outcome_class, OutcomeClass.CLEAN_SUCCESS)
        self.assertAlmostEqual(outcome.return_5m, 5.0)
        self.assertAlmostEqual(outcome.return_30m, 11.0)
        self.assertEqual(outcome.d2_open_gap, -8.0)
        self.assertTrue(outcome.next_day_nuclear_open)
        self.assertTrue(outcome.leadership_retained_close)
        self.assertIsNone(outcome.weakness_resolved_at_925)

    def test_false_positive_and_false_negative_diagnostics_are_structured(self):
        failure_ticks = (
            open_tick("09:30:00", 10.0),
            open_tick("09:35:00", 9.5),
            open_tick("15:00:00", 9.0),
        )
        failure_view = AsOfMarketView("2026-08-14", ts("15:00:00"), open_ticks=failure_ticks)
        failure = self.engine.compute(OutcomeInput(
            "000001.SZ", SetupType.ONE_TO_TWO, CandidateGrade.A1,
            failure_view, open_decision(), 11.0,
        ))
        diagnostics = diagnose_outcome(failure, SetupType.ONE_TO_TWO, ExecutionState.EXECUTE, ("POSITIVE_SURPRISE",))
        self.assertIn(OutcomeDiagnostic.FP_EXECUTION, diagnostics)
        self.assertIn(OutcomeDiagnostic.FP_EXPECTATION, diagnostics)


if __name__ == "__main__":
    unittest.main()
