import unittest

from adapters.mock import MockAdapter
from config.thresholds import ThresholdRegistry
from domain.enums import DataQualityState, ReplayMode
from domain.models import AuctionTick
from market.dqs import DataQualityService
from replay.clock import FutureDataAccessError, ReplayEngine, VirtualClock
from replay.diff import diff_decisions
from tests.helpers import make_ticks, ts


class DomainDataReplayTests(unittest.TestCase):
    def setUp(self):
        self.execution = ThresholdRegistry.load("config/thresholds/execution.yaml")

    def test_missing_is_none_not_zero_and_dqs_broken(self):
        tick = AuctionTick(ts("09:24:00"), ts("09:24:01"), "000001.SZ", None, None, None, None)
        report = DataQualityService(self.execution).evaluate([tick], ts("09:25:00"))
        self.assertEqual(report.state, DataQualityState.BROKEN)
        self.assertIn("matched_amount", report.missing_fields)

    def test_mock_adapter_filters_ticker_and_time(self):
        loaded = list(MockAdapter(make_ticks([("09:19:00", 2.0), ("09:21:00", 3.0)])).load(["000001.SZ"], start=ts("09:20:00")))
        self.assertEqual(len(loaded), 1)

    def test_replay_forbids_future_access(self):
        replay = ReplayEngine(make_ticks([("09:20:00", 2.0), ("09:21:00", 3.0)]), VirtualClock(ts("09:19:00")))
        list(replay.run(ReplayMode.STEP))
        with self.assertRaises(FutureDataAccessError):
            replay.get_ticks(ts("09:21:00"))
        list(replay.run(ReplayMode.STEP))
        self.assertEqual(len(replay.get_ticks(ts("09:21:00"))), 2)

    def test_decision_diff_reports_field_path(self):
        diffs = diff_decisions({"grade": "A1", "score": 90}, {"grade": "A2", "score": 90})
        self.assertEqual(diffs[0].path, "decision.grade")

    def test_checkpoint_includes_tick_at_boundary_and_stops(self):
        replay = ReplayEngine(make_ticks([("09:20:00", 2.0), ("09:21:00", 3.0), ("09:22:00", 4.0)]), VirtualClock(ts("09:19:00")))
        visible = list(replay.run(ReplayMode.CHECKPOINT, checkpoints=[ts("09:21:00")]))
        self.assertEqual(len(visible), 2)
        self.assertEqual(replay.clock.now, ts("09:21:00"))

    def test_virtual_matched_volume_may_fall_without_dqs_penalty(self):
        ticks = make_ticks([("09:24:58", 2.0), ("09:25:00", 2.1)])
        object.__setattr__(ticks[1], "matched_volume", ticks[0].matched_volume - 1)
        report = DataQualityService(self.execution).evaluate(ticks, ts("09:25:00"))
        self.assertEqual(report.state, DataQualityState.GOOD)


if __name__ == "__main__":
    unittest.main()
