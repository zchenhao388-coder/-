import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

from adapters.mootdx import MootdxRealtimeAdapter
from adapters.production import (
    DataSourceAcceptanceReport,
    DataSourceNotCertified,
    ExecutionDataSourceGuard,
    VendorContract,
)
from adapters.tencent import TencentRealtimeAdapter, TencentResponseError
from config.thresholds import ThresholdRegistry
from domain.enums import DataQualityState, SourceAgreementState
from domain.models import AuctionTick
from domain.reason_codes import ReasonCode
from market.asof import AsOfMarketView
from market.dqs import DataQualityService
from market.source_validation import CrossSourceValidator
from monitoring.feed import FeedHealthMonitor
from storage.jsonl import RawPayloadJsonlStore
from tests.helpers import ts


def tencent_body(changes=None):
    fields = [""] * 88
    values = {
        0: "51",
        1: "平安银行",
        2: "000001",
        3: "11.20",
        4: "11.11",
        5: "11.20",
        6: "56685",
        30: "20260814093045",
        32: "0.81",
        35: "11.20/56685/63486567",
    }
    for level in range(1, 6):
        values[7 + level * 2] = f"{11.20 - level * 0.01:.2f}"
        values[8 + level * 2] = str(100 * level)
        values[17 + level * 2] = f"{11.19 + level * 0.01:.2f}"
        values[18 + level * 2] = str(200 * level)
    values.update(changes or {})
    for index, value in values.items():
        fields[index] = "" if value is None else str(value)
    return f'v_sz000001="{"~".join(fields)}";\n'.encode("gbk")


def tencent_adapter(body=None):
    calls = []

    def open_response(request, timeout):
        calls.append((request.full_url, timeout))
        return body if body is not None else tencent_body()

    result = TencentRealtimeAdapter(open_response, receive_clock=lambda: ts("09:30:47"))
    result.connect()
    result.subscribe(("000001.SZ",))
    return result, calls


def probe_tick(source, price, gap, receive="09:25:00"):
    return AuctionTick(
        exchange_ts=None,
        receive_ts=ts(receive),
        provider_ts=ts(receive),
        ticker="000001.SZ",
        virtual_price=price,
        gap_pct=gap,
        matched_volume=None,
        matched_amount=None,
        source=source,
    )


class TencentAdapterTests(unittest.TestCase):
    def test_capability_is_secondary_probe_and_guard_rejects_it(self):
        capability = TencentRealtimeAdapter().capability
        report = DataSourceAcceptanceReport(
            adapter_name=capability.adapter_name,
            contract=VendorContract("TENCENT_PUBLIC", "", "", date(2027, 1, 1)),
            fields=(),
            auction_semantics_verified=False,
            exchange_timestamp_verified=False,
            tested_trade_dates=(),
            evidence_version="",
        )

        with self.assertRaisesRegex(DataSourceNotCertified, "EXPERIMENTAL_SOURCE_FORBIDDEN"):
            ExecutionDataSourceGuard().authorize(capability, report, ts("09:25:00"))
        self.assertFalse(capability.execution_enabled)
        self.assertFalse(capability.core_fields["exchange_ts"])
        self.assertEqual(capability.allowed_uses, ("FIELD_PROBE", "RAW_CAPTURE", "SHADOW_RESEARCH"))

    def test_maps_actual_response_shape_without_promoting_provider_time(self):
        source, calls = tencent_adapter()

        tick = source.poll_once()[0]

        self.assertIn("q=sz000001", calls[0][0])
        self.assertIsNone(tick.exchange_ts)
        self.assertEqual(tick.receive_ts, ts("09:30:47"))
        self.assertEqual(tick.provider_ts, ts("09:30:45"))
        self.assertEqual(tick.virtual_price, 11.20)
        self.assertEqual(tick.gap_pct, 0.81)
        self.assertEqual(tick.matched_volume, 5_668_500.0)
        self.assertEqual(tick.matched_amount, 63_486_567.0)
        self.assertEqual(tick.bid, 11.19)
        self.assertEqual(tick.ask, 11.20)
        self.assertEqual(tick.orderbook["asks"][0]["volume"], 20_000.0)
        self.assertIsNone(tick.unmatched_side)
        self.assertIsNone(tick.unmatched_volume)
        self.assertFalse(tick.raw["exchange_timestamp_verified"])

    def test_missing_fields_remain_none_and_raw_payload_is_append_only(self):
        source, _ = tencent_adapter(tencent_body({3: None, 4: None, 6: None, 9: None, 19: None, 32: None, 35: None}))
        tick = source.poll_once()[0]

        self.assertIsNone(tick.virtual_price)
        self.assertIsNone(tick.gap_pct)
        self.assertIsNone(tick.matched_volume)
        self.assertIsNone(tick.matched_amount)
        self.assertIsNone(tick.bid)
        self.assertIsNone(tick.ask)
        with tempfile.TemporaryDirectory() as directory:
            event = source.raw_quote_payloads((tick,))[0]
            path = RawPayloadJsonlStore(directory).append(event)
            self.assertIn("tencent_realtime_probe/2026-08-14/quote", path.as_posix())
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["source"], TencentRealtimeAdapter.SOURCE)
            self.assertIn("raw_text", stored["payload"])

    def test_malformed_response_fails_loudly(self):
        source, _ = tencent_adapter(b'v_sz000001="bad";')
        with self.assertRaises(TencentResponseError):
            source.poll_once()


class CrossSourceValidationTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).parents[1]
        self.validator = CrossSourceValidator(
            ThresholdRegistry.load(root / "config/thresholds/source_validation.yaml")
        )
        self.dqs = DataQualityService(
            ThresholdRegistry.load(root / "config/thresholds/execution.yaml")
        )
        self.feed = FeedHealthMonitor(
            ThresholdRegistry.load(root / "config/thresholds/monitoring.yaml")
        )

    @staticmethod
    def view(primary, secondary, as_of="09:25:00", extra=()):
        return AsOfMarketView(
            "2026-08-14",
            ts(as_of),
            ticks=(primary, secondary, *extra),
        )

    def report(self, primary, secondary, as_of="09:25:00", extra=()):
        return self.validator.evaluate(
            self.view(primary, secondary, as_of, extra),
            "000001.SZ",
            MootdxRealtimeAdapter.SOURCE,
            TencentRealtimeAdapter.SOURCE,
        )

    def test_matching_prices_are_agreed_but_never_execution_eligible(self):
        report = self.report(
            probe_tick(MootdxRealtimeAdapter.SOURCE, 11.20, 0.81),
            probe_tick(TencentRealtimeAdapter.SOURCE, 11.20, 0.81),
        )

        self.assertEqual(report.state, SourceAgreementState.AGREED)
        self.assertEqual(report.dqs_state, DataQualityState.GOOD)
        self.assertFalse(report.execution_eligible)
        self.assertEqual(report.reason_codes, ())

    def test_material_price_conflict_is_structured_disagreement_and_degrades_dqs(self):
        report = self.report(
            probe_tick(MootdxRealtimeAdapter.SOURCE, 11.20, 0.81),
            probe_tick(TencentRealtimeAdapter.SOURCE, 11.25, 0.90),
        )
        verified = replace(
            probe_tick("LICENSED", 11.20, 0.81),
            exchange_ts=ts("09:25:00"),
            matched_volume=100_000,
            matched_amount=1_120_000,
        )
        merged = self.dqs.evaluate((verified,), ts("09:25:00"), report)

        self.assertEqual(report.state, SourceAgreementState.DISAGREEMENT)
        self.assertEqual(report.reason_codes, (ReasonCode.SOURCE_DISAGREEMENT.value,))
        self.assertEqual(merged.state, DataQualityState.DEGRADED)
        self.assertIn(ReasonCode.SOURCE_DISAGREEMENT.value, merged.reasons)

    def test_critical_conflict_blocks_dqs_and_feed_health(self):
        primary = probe_tick(MootdxRealtimeAdapter.SOURCE, 11.20, 0.81)
        secondary = probe_tick(TencentRealtimeAdapter.SOURCE, 11.50, 3.50)
        verified = replace(
            primary,
            source="LICENSED",
            exchange_ts=ts("09:25:00"),
            matched_volume=100_000,
            matched_amount=1_120_000,
        )
        view = self.view(primary, secondary, extra=(verified,))
        report = self.validator.evaluate(
            view, "000001.SZ", MootdxRealtimeAdapter.SOURCE, TencentRealtimeAdapter.SOURCE
        )

        self.assertEqual(report.state, SourceAgreementState.CRITICAL)
        self.assertEqual(self.dqs.evaluate((verified,), ts("09:25:00"), report).state, DataQualityState.BROKEN)
        health = self.feed.evaluate(view, "000001.SZ", report)
        self.assertEqual(health.state, DataQualityState.BROKEN)
        self.assertIn(ReasonCode.SOURCE_DISAGREEMENT.value, health.reasons)

    def test_future_secondary_data_is_invisible_and_crosscheck_is_insufficient(self):
        primary = probe_tick(MootdxRealtimeAdapter.SOURCE, 11.20, 0.81)
        future = probe_tick(TencentRealtimeAdapter.SOURCE, 11.20, 0.81, receive="09:25:01")

        report = self.report(primary, future)

        self.assertEqual(report.state, SourceAgreementState.INSUFFICIENT)
        self.assertEqual(report.dqs_state, DataQualityState.DEGRADED)
        self.assertEqual(report.reason_codes, (ReasonCode.SOURCE_CROSSCHECK_UNAVAILABLE.value,))

    def test_large_receive_time_skew_is_critical(self):
        report = self.report(
            probe_tick(MootdxRealtimeAdapter.SOURCE, 11.20, 0.81, receive="09:24:40"),
            probe_tick(TencentRealtimeAdapter.SOURCE, 11.20, 0.81, receive="09:25:00"),
        )
        self.assertEqual(report.state, SourceAgreementState.CRITICAL)
        self.assertEqual(report.reason_codes, (ReasonCode.SOURCE_TIME_SKEW.value,))

    def test_provider_time_skew_cannot_hide_behind_similar_receive_times(self):
        primary = replace(
            probe_tick(MootdxRealtimeAdapter.SOURCE, 11.20, 0.81),
            provider_ts=ts("09:24:30"),
        )
        secondary = replace(
            probe_tick(TencentRealtimeAdapter.SOURCE, 11.20, 0.81),
            provider_ts=ts("09:24:55"),
        )

        report = self.report(primary, secondary)

        self.assertEqual(report.receive_time_skew_seconds, 0.0)
        self.assertEqual(report.provider_time_skew_seconds, 25.0)
        self.assertEqual(report.state, SourceAgreementState.CRITICAL)
        self.assertEqual(report.dqs_state, DataQualityState.BROKEN)
        self.assertEqual(report.reason_codes, (ReasonCode.SOURCE_TIME_SKEW.value,))

    def test_repository_config_keeps_both_free_sources_non_execution(self):
        root = Path(__file__).parents[1]
        config = json.loads((root / "config/data_sources.yaml").read_text(encoding="utf-8"))
        mootdx = config["sources"]["mootdx_realtime"]
        tencent = config["sources"]["tencent_realtime"]
        self.assertEqual(mootdx["role"], "PRIMARY_PROBE")
        self.assertEqual(tencent["role"], "SECONDARY_PROBE")
        self.assertFalse(mootdx["execution_enabled"])
        self.assertFalse(tencent["execution_enabled"])


if __name__ == "__main__":
    unittest.main()
