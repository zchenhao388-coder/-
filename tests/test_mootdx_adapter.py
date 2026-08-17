import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

from adapters.mootdx import MootdxRealtimeAdapter
from adapters.production import (
    DataSourceAcceptanceReport,
    DataSourceNotCertified,
    ExecutionDataSourceGuard,
    VendorContract,
)
from config.thresholds import ThresholdRegistry
from domain.enums import DataQualityState
from market.asof import AsOfMarketView
from market.dqs import DataQualityService
from monitoring.feed import FeedHealthMonitor
from storage.jsonl import RawPayloadJsonlStore, RawTickJsonlStore
from tests.helpers import ts


class FakeMootdxClient:
    def __init__(self, quotes, transactions=()):
        self.quote_rows = quotes
        self.transaction_rows = transactions
        self.quote_calls = []
        self.transaction_calls = []
        self.closed = False

    def quotes(self, symbol):
        self.quote_calls.append(tuple(symbol))
        return self.quote_rows

    def transaction(self, symbol, date):
        self.transaction_calls.append((symbol, date))
        return self.transaction_rows

    def close(self):
        self.closed = True


def quote_row(**changes):
    row = {
        "market": 0,
        "code": "000001",
        "price": 11.20,
        "last_close": 11.11,
        "servertime": "9:24:53.286",
        "vol": 21048,
        "amount": 23_573_760,
    }
    for level in range(1, 6):
        row[f"bid{level}"] = 11.20 - level * 0.01
        row[f"ask{level}"] = 11.19 + level * 0.01
        row[f"bid_vol{level}"] = 100 * level
        row[f"ask_vol{level}"] = 200 * level
    row.update(changes)
    return row


def adapter(rows=None, transactions=()):
    client = FakeMootdxClient(rows if rows is not None else (quote_row(),), transactions)
    result = MootdxRealtimeAdapter(lambda: client, lambda: ts("09:25:19"))
    result.connect()
    result.subscribe(("000001.SZ",))
    return result, client


class MootdxAdapterTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).parents[1]
        self.execution = ThresholdRegistry.load(root / "config/thresholds/execution.yaml")
        self.monitoring = ThresholdRegistry.load(root / "config/thresholds/monitoring.yaml")

    def test_capability_is_probe_only_and_production_guard_rejects_it(self):
        capability = MootdxRealtimeAdapter(lambda: object()).capability
        report = DataSourceAcceptanceReport(
            adapter_name=capability.adapter_name,
            contract=VendorContract("MOOTDX_PUBLIC", "", "", date(2027, 1, 1)),
            fields=(),
            auction_semantics_verified=False,
            exchange_timestamp_verified=False,
            tested_trade_dates=(),
            evidence_version="",
        )

        with self.assertRaisesRegex(DataSourceNotCertified, "EXPERIMENTAL_SOURCE_FORBIDDEN"):
            ExecutionDataSourceGuard().authorize(capability, report, ts("09:25:00"))
        self.assertEqual(capability.allowed_uses, ("FIELD_PROBE", "RAW_CAPTURE", "SHADOW_RESEARCH"))
        self.assertFalse(capability.execution_enabled)
        self.assertFalse(capability.core_fields["exchange_ts"])

    def test_quote_maps_provisional_fields_without_promoting_provider_time(self):
        source, client = adapter()

        tick = source.poll_once()[0]

        self.assertEqual(client.quote_calls, [("000001",)])
        self.assertIsNone(tick.exchange_ts)
        self.assertEqual(tick.receive_ts, ts("09:25:19"))
        self.assertEqual(tick.provider_ts, ts("09:24:53.286000"))
        self.assertEqual(tick.ticker, "000001.SZ")
        self.assertEqual(tick.virtual_price, 11.20)
        self.assertAlmostEqual(tick.gap_pct, (11.20 / 11.11 - 1) * 100)
        self.assertEqual(tick.matched_volume, 2_104_800.0)
        self.assertEqual(tick.matched_amount, 23_573_760.0)
        self.assertEqual(tick.bid, 11.19)
        self.assertEqual(tick.ask, 11.20)
        self.assertEqual(tick.orderbook["bids"][0]["volume"], 10_000.0)
        self.assertIsNone(tick.unmatched_side)
        self.assertIsNone(tick.unmatched_volume)
        self.assertFalse(tick.raw["auction_semantics_verified"])

    def test_missing_values_stay_none_instead_of_becoming_zero(self):
        source, _ = adapter((quote_row(price=None, last_close=None, vol=None, amount=None, bid1=None, ask1=None),))

        tick = source.poll_once()[0]

        self.assertIsNone(tick.virtual_price)
        self.assertIsNone(tick.gap_pct)
        self.assertIsNone(tick.matched_volume)
        self.assertIsNone(tick.matched_amount)
        self.assertIsNone(tick.bid)
        self.assertIsNone(tick.ask)

    def test_unknown_exchange_time_is_broken_and_probe_view_is_receive_bounded(self):
        source, _ = adapter()
        current = source.poll_once()[0]
        future = replace(current, receive_ts=ts("09:25:30"), provider_ts=ts("09:25:20"))
        view = AsOfMarketView("2026-08-14", ts("09:25:19"), ticks=(current, future))

        self.assertEqual(view.get_ticks("000001.SZ"), ())
        self.assertEqual(view.get_probe_ticks("000001.SZ"), (current,))
        dqs = DataQualityService(self.execution).evaluate((current,), ts("09:25:19"))
        health = FeedHealthMonitor(self.monitoring).evaluate(view, "000001.SZ")
        self.assertEqual(dqs.state, DataQualityState.DEGRADED)
        self.assertIn("MISSING_RELIABLE_EXCHANGE_TS", dqs.reasons)
        self.assertEqual(health.state, DataQualityState.BROKEN)

    def test_repeated_and_out_of_order_provider_updates_are_preserved(self):
        rows = (quote_row(servertime="9:24:57.500"), quote_row(servertime="9:24:56.000", price=11.19))
        source, _ = adapter(rows)

        ticks = source.poll_once()

        self.assertEqual(len(ticks), 2)
        self.assertGreater(ticks[0].provider_ts, ticks[1].provider_ts)
        self.assertIs(source.get_latest("000001.SZ"), ticks[1])

    def test_transaction_payload_remains_raw_and_exchange_time_unknown(self):
        rows = ({"time": "09:15", "price": 11.2, "vol": 100, "num": 5, "buyorsell": 0},)
        source, client = adapter(transactions=rows)

        events = source.capture_transactions("000001.SZ", "2026-08-14")

        self.assertEqual(client.transaction_calls, [("000001", "20260814")])
        self.assertEqual(events[0].payload, rows[0])
        self.assertEqual(events[0].provider_ts, ts("09:15:00"))
        self.assertIsNone(events[0].exchange_ts)

    def test_raw_payload_and_canonical_tick_are_append_only_and_date_partitioned(self):
        source, _ = adapter()
        tick = source.poll_once()[0]
        event = source.raw_quote_payloads((tick,))[0]
        with tempfile.TemporaryDirectory() as directory:
            raw_path = RawPayloadJsonlStore(directory).append(event)
            RawPayloadJsonlStore(directory).append(event)
            tick_path = RawTickJsonlStore(directory).append(tick)

            self.assertIn("mootdx_realtime_probe/2026-08-14/quote", raw_path.as_posix())
            self.assertIn("2026-08-14", tick_path.as_posix())
            lines = raw_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            stored = json.loads(lines[0])
            self.assertEqual(stored["source"], MootdxRealtimeAdapter.SOURCE)
            self.assertEqual(stored["payload"]["servertime"], "9:24:53.286")


if __name__ == "__main__":
    unittest.main()
