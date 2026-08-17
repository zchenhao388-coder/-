import json
import unittest
from datetime import datetime, timezone

from adapters.eastmoney import EastmoneySnapshotAdapter
from config.thresholds import ThresholdRegistry
from domain.enums import DataQualityState
from market.dqs import DataQualityService
from tests.helpers import ts


class RealAdapterContractTests(unittest.TestCase):
    def test_maps_provider_fields_without_zero_filling_missing_enhanced_data(self):
        body = json.dumps({"rc": 0, "data": {
            "f43": 910, "f47": 436231, "f48": 397586127.0,
            "f57": "600000", "f60": 918, "f86": 1786695115, "f124": 0,
        }}).encode("utf-8")
        adapter = EastmoneySnapshotAdapter(
            opener=lambda request, timeout: body,
            receive_clock=lambda: datetime(2026, 8, 16, tzinfo=timezone.utc),
        )
        tick = adapter.fetch_one("600000.SH")
        self.assertEqual(tick.virtual_price, 9.10)
        self.assertAlmostEqual(tick.gap_pct, (9.10 / 9.18 - 1) * 100)
        self.assertEqual(tick.matched_volume, 43_623_100)
        self.assertEqual(tick.matched_amount, 397_586_127)
        self.assertIsNone(tick.unmatched_volume)
        self.assertIsNone(tick.orderbook)
        self.assertIsNone(tick.exchange_ts)
        self.assertIsNotNone(tick.provider_ts)
        self.assertFalse(adapter.capability.supports_core)
        self.assertFalse(adapter.capability.auction_semantics_verified)
        report = DataQualityService(ThresholdRegistry.load("config/thresholds/execution.yaml")).evaluate(
            [tick], ts("09:25:00"),
        )
        self.assertEqual(report.state, DataQualityState.DEGRADED)


if __name__ == "__main__":
    unittest.main()
