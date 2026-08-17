from typing import Mapping, Optional

from market.asof import AsOfMarketView


class PeerEngine:
    """Point-in-time peer access; it never accepts raw peer arrays."""

    @staticmethod
    def latest_values(market_view: AsOfMarketView, group: str, field: str):
        return market_view.get_peer_latest_values(group, field)

    @staticmethod
    def percentile(market_view: AsOfMarketView, group: str, field: str, value: Optional[float]) -> Optional[float]:
        population = market_view.get_peer_latest_values(group, field)
        if value is None or not population:
            return None
        return sum(1 for item in population if item <= value) / len(population)
