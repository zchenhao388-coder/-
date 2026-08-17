import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from adapters.mootdx import MootdxRealtimeAdapter
from adapters.tencent import TencentRealtimeAdapter
from app.probe_sources import capture
from tests.test_mootdx_adapter import FakeMootdxClient, quote_row


SHANGHAI = ZoneInfo("Asia/Shanghai")


def tencent_body() -> bytes:
    fields = [""] * 88
    values = {
        0: "51",
        1: "平安银行",
        2: "000001",
        3: "11.20",
        4: "11.11",
        5: "11.20",
        6: "56685",
        30: datetime.now(SHANGHAI).strftime("%Y%m%d%H%M%S"),
        32: "0.81",
        35: "11.20/56685/63486567",
    }
    for level in range(1, 6):
        values[7 + level * 2] = f"{11.20 - level * 0.01:.2f}"
        values[8 + level * 2] = str(100 * level)
        values[17 + level * 2] = f"{11.19 + level * 0.01:.2f}"
        values[18 + level * 2] = str(200 * level)
    for index, value in values.items():
        fields[index] = value
    return f'v_sz000001="{"~".join(fields)}";\n'.encode("gbk")


class ProbeStabilityTests(unittest.TestCase):
    def test_blocked_mootdx_does_not_stop_tencent_watchdog_or_deadline_exit(self):
        class BlockingMootdxClient(FakeMootdxClient):
            def quotes(self, symbol):
                self.quote_calls.append(tuple(symbol))
                time.sleep(0.6)
                return self.quote_rows

        primary_client = BlockingMootdxClient((quote_row(),))
        primary = MootdxRealtimeAdapter(
            lambda: primary_client,
            timeout_seconds=0.04,
        )
        tencent_calls = []

        def open_tencent(request, timeout):
            tencent_calls.append((request.full_url, timeout))
            return tencent_body()

        secondary = TencentRealtimeAdapter(
            open_tencent,
            timeout_seconds=0.05,
        )
        started_at = datetime.now(SHANGHAI)
        end_time = started_at + timedelta(seconds=0.30)
        wall_started = time.monotonic()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary = capture(
                ("000001.SZ",),
                root,
                None,
                0.02,
                False,
                end_time=end_time,
                primary=primary,
                secondary=secondary,
                stall_seconds=0.015,
                gap_seconds=0.015,
                watchdog_interval_seconds=0.005,
                shutdown_grace_seconds=0.20,
                print_reports=False,
            )
            elapsed = time.monotonic() - wall_started
            trade_date = started_at.date().isoformat()
            tencent_quotes = root / "raw" / secondary.SOURCE / trade_date / "quote" / "000001.SZ.jsonl"
            mootdx_stalls = root / "raw" / primary.SOURCE / trade_date / "stall" / "_SOURCE_.jsonl"
            mootdx_watchdog = root / "raw" / primary.SOURCE / trade_date / "watchdog_stall" / "_SOURCE_.jsonl"
            tencent_heartbeats = root / "raw" / secondary.SOURCE / trade_date / "heartbeat" / "_SOURCE_.jsonl"
            tencent_gaps = root / "raw" / secondary.SOURCE / trade_date / "gap" / "_SOURCE_.jsonl"
            stopped_paths = [
                root / "raw" / source / trade_date / "source_stopped" / "_SOURCE_.jsonl"
                for source in (primary.SOURCE, secondary.SOURCE)
            ]

            quote_rows = tencent_quotes.read_text(encoding="utf-8").splitlines()
            stall_rows = mootdx_stalls.read_text(encoding="utf-8").splitlines()
            watchdog_rows = mootdx_watchdog.read_text(encoding="utf-8").splitlines()
            heartbeat_rows = tencent_heartbeats.read_text(encoding="utf-8").splitlines()
            stall_event = json.loads(stall_rows[0])
            watchdog_event = json.loads(watchdog_rows[0])

            self.assertTrue(summary.deadline_reached)
            self.assertLess(elapsed, 1.0)
            self.assertGreaterEqual(len(quote_rows), 5)
            self.assertGreaterEqual(len(tencent_calls), 5)
            self.assertGreaterEqual(len(stall_rows), 1)
            self.assertGreaterEqual(len(watchdog_rows), 1)
            self.assertGreaterEqual(len(heartbeat_rows), 5)
            self.assertTrue(tencent_gaps.exists())
            self.assertEqual(stall_event["payload"]["error_type"], "MootdxPollTimeout")
            self.assertFalse(stall_event["payload"]["execution_eligible"])
            self.assertEqual(watchdog_event["payload"]["operation"], "POLL")
            self.assertFalse(watchdog_event["payload"]["execution_eligible"])
            self.assertGreater(
                summary.sources[secondary.SOURCE]["successes"],
                summary.sources[primary.SOURCE]["successes"],
            )
            self.assertGreaterEqual(summary.sources[primary.SOURCE]["watchdog_events"], 1)
            self.assertFalse(summary.sources[primary.SOURCE]["thread_alive"])
            self.assertFalse(summary.sources[secondary.SOURCE]["thread_alive"])
            for path in stopped_paths:
                event = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
                self.assertEqual(event["payload"]["termination_reason"], "DEADLINE_REACHED")
            for line in quote_rows:
                event = json.loads(line)
                self.assertIsNone(event["exchange_ts"])
            self.assertFalse(
                (root / "raw" / primary.SOURCE / trade_date / "forced_stop").exists()
            )


if __name__ == "__main__":
    unittest.main()
