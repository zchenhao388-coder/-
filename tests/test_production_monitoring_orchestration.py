import json
import unittest
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

from adapters.base import DataCapability
from adapters.eastmoney import EastmoneySnapshotAdapter
from adapters.production import (
    CanonicalAuctionMapper,
    CanonicalFieldMap,
    CertifiedRealtimeAdapter,
    DataSourceAcceptanceReport,
    DataSourceNotCertified,
    ExecutionDataSourceGuard,
    FieldSemanticAcceptance,
    VendorContract,
)
from app.orchestrator import ApplicationOrchestrator, RuntimeMode
from config.thresholds import ThresholdRegistry
from domain.enums import (
    CircuitBreakerState,
    DailyStage,
    DataQualityState,
    FieldAcceptanceStatus,
)
from domain.models import AuctionTick
from market.asof import AsOfMarketView
from monitoring.feed import DataCircuitBreaker, FeedHealthMonitor, FeedHealthReport
from tests.helpers import ts


CORE_FIELDS = (
    "exchange_ts",
    "receive_ts",
    "ticker",
    "virtual_price",
    "gap_pct",
    "matched_volume",
    "matched_amount",
)


class FakeRealtimeTransport:
    def __init__(self, events=()):
        self.events = tuple(events)
        self.connected = False
        self.subscriptions = set()
        self.closed = False

    def connect(self):
        self.connected = True

    def subscribe(self, tickers):
        self.subscriptions.update(tickers)

    def unsubscribe(self, tickers):
        self.subscriptions.difference_update(tickers)

    def stream(self):
        yield from self.events

    def health(self):
        return {"connected": self.connected, "closed": self.closed}

    def close(self):
        self.closed = True
        self.connected = False


def certified_capability():
    return DataCapability(
        adapter_name="licensed_vendor_realtime",
        core_fields={field: True for field in CORE_FIELDS},
        field_provenance={field: f"contract:{field}" for field in CORE_FIELDS},
        timestamp_semantics="contractual exchange event timestamp",
        auction_semantics_verified=True,
        allowed_uses=("EXECUTION", "SHADOW_RESEARCH"),
        execution_enabled=True,
        license_verified=True,
        sla_verified=True,
        semantic_evidence_version="SEMANTICS_V1",
    )


def certified_report():
    verified_at = datetime.fromisoformat("2026-08-01T12:00:00+08:00")
    return DataSourceAcceptanceReport(
        adapter_name="licensed_vendor_realtime",
        contract=VendorContract(
            vendor_name="LICENSED_VENDOR",
            license_reference="LICENSE-2026-001",
            sla_reference="SLA-2026-001",
            valid_until=date(2027, 8, 1),
        ),
        fields=tuple(
            FieldSemanticAcceptance(
                canonical_field=field,
                status=FieldAcceptanceStatus.VERIFIED,
                evidence_reference=f"evidence:{field}",
                sample_size=100,
                verified_at=verified_at,
            )
            for field in CORE_FIELDS
        ),
        auction_semantics_verified=True,
        exchange_timestamp_verified=True,
        tested_trade_dates=("2026-07-30", "2026-07-31"),
        evidence_version="SEMANTICS_V1",
    )


def monitoring_thresholds():
    return ThresholdRegistry.load(Path(__file__).parents[1] / "config/thresholds/monitoring.yaml")


def healthy_report(as_of=None):
    boundary = as_of or ts("09:20:00")
    return FeedHealthReport(
        ticker="000001.SZ",
        as_of=boundary,
        state=DataQualityState.GOOD,
        latest_exchange_age_seconds=0.0,
        latest_receive_lag_seconds=0.0,
        missing_fraction=0.0,
        reasons=(),
    )


class ProductionAdapterAcceptanceTests(unittest.TestCase):
    def test_eastmoney_can_never_be_authorized_for_execution(self):
        capability = EastmoneySnapshotAdapter().capability
        report = replace(certified_report(), adapter_name=capability.adapter_name)

        with self.assertRaisesRegex(DataSourceNotCertified, "EXPERIMENTAL_SOURCE_FORBIDDEN"):
            ExecutionDataSourceGuard().authorize(capability, report, ts("09:20:00"))
        self.assertFalse(capability.execution_enabled)
        self.assertNotIn("EXECUTION", capability.allowed_uses)

    def test_pending_field_semantics_blocks_certification(self):
        report = certified_report()
        pending = replace(report.fields[-1], status=FieldAcceptanceStatus.PENDING)
        report = replace(report, fields=(*report.fields[:-1], pending))

        with self.assertRaisesRegex(DataSourceNotCertified, "FIELD_UNVERIFIED:matched_amount"):
            ExecutionDataSourceGuard().authorize(certified_capability(), report, ts("09:20:00"))

    def test_future_dated_evidence_blocks_certification(self):
        report = certified_report()
        future = replace(report.fields[0], verified_at=ts("09:21:00"))
        report = replace(report, fields=(future, *report.fields[1:]))

        with self.assertRaisesRegex(DataSourceNotCertified, "FIELD_UNVERIFIED:exchange_ts"):
            ExecutionDataSourceGuard().authorize(certified_capability(), report, ts("09:20:00"))

    def test_certified_adapter_maps_core_and_keeps_missing_enhanced_as_none(self):
        event = {
            "ex": ts("09:20:00"),
            "recv": ts("09:20:00"),
            "symbol": "000001.SZ",
            "price": 10.5,
            "gap": 5.0,
            "volume": 200_000,
            "amount": 2_100_000,
        }
        transport = FakeRealtimeTransport((event,))
        mapper = CanonicalAuctionMapper(
            CanonicalFieldMap("ex", "recv", "symbol", "price", "gap", "volume", "amount"),
            source="licensed_vendor_realtime",
        )
        adapter = CertifiedRealtimeAdapter(
            certified_capability(), certified_report(), transport, mapper, lambda: ts("09:20:00")
        )

        adapter.connect()
        adapter.subscribe(("000001.SZ",))
        ticks = tuple(adapter.stream())

        self.assertTrue(transport.connected)
        self.assertEqual(transport.subscriptions, {"000001.SZ"})
        self.assertEqual(ticks[0].matched_amount, 2_100_000.0)
        self.assertIsNone(ticks[0].unmatched_side)
        self.assertIsNone(ticks[0].unmatched_volume)
        self.assertIsNone(ticks[0].bid)
        self.assertIsNone(ticks[0].ask)
        self.assertIsNone(ticks[0].orderbook)
        self.assertIs(adapter.get_latest("000001.SZ"), ticks[0])

    def test_uncertified_adapter_does_not_touch_transport(self):
        transport = FakeRealtimeTransport()
        adapter = CertifiedRealtimeAdapter(
            replace(certified_capability(), execution_enabled=False),
            certified_report(),
            transport,
            CanonicalAuctionMapper(
                CanonicalFieldMap("ex", "recv", "symbol", "price", "gap", "volume", "amount"),
                source="licensed_vendor_realtime",
            ),
            lambda: ts("09:20:00"),
        )

        with self.assertRaises(DataSourceNotCertified):
            adapter.connect()
        self.assertFalse(transport.connected)


class FeedMonitoringTests(unittest.TestCase):
    def setUp(self):
        self.thresholds = monitoring_thresholds()
        self.monitor = FeedHealthMonitor(self.thresholds)

    @staticmethod
    def view(tick, as_of=None, extra_ticks=()):
        return AsOfMarketView(
            "2026-08-14",
            as_of or ts("09:20:00"),
            ticks=(tick, *extra_ticks),
        )

    @staticmethod
    def tick(clock="09:20:00", **changes):
        values = {
            "exchange_ts": ts(clock),
            "receive_ts": ts(clock),
            "ticker": "000001.SZ",
            "virtual_price": 10.5,
            "gap_pct": 5.0,
            "matched_volume": 200_000.0,
            "matched_amount": 2_100_000.0,
            "source": "CERTIFIED_TEST",
        }
        values.update(changes)
        return AuctionTick(**values)

    def test_health_classifies_good_degraded_and_broken(self):
        good = self.monitor.evaluate(self.view(self.tick()), "000001.SZ")
        degraded = self.monitor.evaluate(
            self.view(self.tick(matched_amount=None)), "000001.SZ"
        )
        broken = self.monitor.evaluate(
            self.view(self.tick("09:19:50")), "000001.SZ"
        )

        self.assertEqual(good.state, DataQualityState.GOOD)
        self.assertEqual(degraded.state, DataQualityState.DEGRADED)
        self.assertEqual(broken.state, DataQualityState.BROKEN)

    def test_health_monitor_cannot_see_future_tick(self):
        visible = self.tick("09:19:58")
        future = self.tick("09:25:00", matched_amount=None)

        report = self.monitor.evaluate(
            self.view(visible, as_of=ts("09:20:00"), extra_ticks=(future,)), "000001.SZ"
        )

        self.assertEqual(report.state, DataQualityState.GOOD)
        self.assertEqual(report.latest_exchange_age_seconds, 2.0)

    def test_open_breaker_requires_good_samples_and_certified_reset(self):
        breaker = DataCircuitBreaker(self.thresholds)
        broken = replace(healthy_report(), state=DataQualityState.BROKEN, reasons=("TEST",))
        self.assertEqual(breaker.observe(broken), CircuitBreakerState.OPEN)

        breaker.observe(healthy_report())
        breaker.observe(healthy_report())
        with self.assertRaisesRegex(RuntimeError, "recovery sample"):
            breaker.reset(certified_capability(), certified_report(), ts("09:20:00"))
        self.assertEqual(breaker.state, CircuitBreakerState.OPEN)

        breaker.observe(healthy_report())
        breaker.reset(certified_capability(), certified_report(), ts("09:20:00"))
        self.assertEqual(breaker.state, CircuitBreakerState.CLOSED)


class OrchestrationTests(unittest.TestCase):
    def test_automatic_order_placement_is_structurally_forbidden(self):
        with self.assertRaisesRegex(ValueError, "does not permit automatic order"):
            RuntimeMode(auto_order=True)

    def test_uncertified_source_halts_before_handler(self):
        calls = []
        orchestrator = ApplicationOrchestrator(
            replace(certified_capability(), execution_enabled=False),
            certified_report(),
            DataCircuitBreaker(monitoring_thresholds()),
            {"AUTHENTICITY_0920": calls.append},
        )

        event = orchestrator.run_checkpoint(ts("09:20:00"), healthy_report())

        self.assertEqual(event.stage, DailyStage.HALTED)
        self.assertFalse(event.executed)
        self.assertEqual(calls, [])

    def test_stale_health_report_cannot_authorize_current_checkpoint(self):
        calls = []
        orchestrator = ApplicationOrchestrator(
            certified_capability(),
            certified_report(),
            DataCircuitBreaker(monitoring_thresholds()),
            {"FINAL_DECISION_0925": calls.append},
        )

        event = orchestrator.run_checkpoint(ts("09:25:00"), healthy_report(ts("09:20:00")))

        self.assertEqual(event.stage, DailyStage.HALTED)
        self.assertEqual(event.detail, "HEALTH_REPORT_AS_OF_MISMATCH")
        self.assertEqual(calls, [])

    def test_daily_sequence_is_checkpointed_and_idempotent(self):
        calls = []
        checkpoints = (
            "PRE_FLIGHT",
            "AUCTION_START_0915",
            "AUTHENTICITY_0920",
            "FINAL_WINDOW_092430",
            "FINAL_DECISION_0925",
            "OPEN_EXECUTION_0930_0935",
            "INTRADAY_IDLE",
            "AFTER_CLOSE_OUTCOME_NIGHT_PLAN",
        )
        handlers = {checkpoint: (lambda current, name=checkpoint: calls.append((name, current))) for checkpoint in checkpoints}
        orchestrator = ApplicationOrchestrator(
            certified_capability(),
            certified_report(),
            DataCircuitBreaker(monitoring_thresholds()),
            handlers,
        )
        times = ("09:14:00", "09:15:00", "09:20:00", "09:24:30", "09:25:00", "09:30:00", "10:00:00", "15:00:00")

        events = tuple(
            orchestrator.run_checkpoint(ts(clock), healthy_report(ts(clock))) for clock in times
        )
        duplicate = orchestrator.run_checkpoint(ts("09:20:30"), healthy_report(ts("09:20:30")))

        self.assertTrue(all(event.executed for event in events))
        self.assertEqual([name for name, _ in calls], list(checkpoints))
        self.assertFalse(duplicate.executed)
        self.assertEqual(duplicate.detail, "IDEMPOTENT_ALREADY_COMPLETED")

    def test_open_circuit_halts_checkpoint(self):
        calls = []
        breaker = DataCircuitBreaker(monitoring_thresholds())
        orchestrator = ApplicationOrchestrator(
            certified_capability(),
            certified_report(),
            breaker,
            {"FINAL_DECISION_0925": calls.append},
        )
        broken = replace(
            healthy_report(ts("09:25:00")),
            state=DataQualityState.BROKEN,
            reasons=("NO_VISIBLE_TICKS",),
        )

        event = orchestrator.run_checkpoint(ts("09:25:00"), broken)

        self.assertEqual(event.stage, DailyStage.HALTED)
        self.assertEqual(event.detail, "DATA_CIRCUIT_OPEN")
        self.assertEqual(calls, [])

    def test_repository_config_keeps_real_sources_disabled_and_shadow_only(self):
        root = Path(__file__).parents[1]
        data_sources = json.loads((root / "config/data_sources.yaml").read_text(encoding="utf-8"))
        system = json.loads((root / "config/system.yaml").read_text(encoding="utf-8"))

        eastmoney = data_sources["sources"]["eastmoney_snapshot"]
        production = data_sources["sources"]["licensed_realtime_template"]
        self.assertFalse(eastmoney["execution_enabled"])
        self.assertFalse(production["configured"])
        self.assertFalse(production["execution_enabled"])
        self.assertTrue(system["shadow_mode"])
        self.assertFalse(system["auto_order"])


if __name__ == "__main__":
    unittest.main()
