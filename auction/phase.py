from datetime import datetime, time
from zoneinfo import ZoneInfo

from domain.enums import AuctionPhase


def auction_phase_at(moment: datetime) -> AuctionPhase:
    """Return the structural call-auction phase for the local exchange time."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("auction phase requires a timezone-aware timestamp")
    clock = moment.astimezone(ZoneInfo("Asia/Shanghai")).timetz().replace(tzinfo=None)
    if clock < time(9, 15):
        return AuctionPhase.PRE_AUCTION
    if clock < time(9, 20):
        return AuctionPhase.SCOUTING
    if clock < time(9, 24, 30):
        return AuctionPhase.VALIDATING
    if clock < time(9, 25):
        return AuctionPhase.FINALIZING
    if clock < time(9, 30):
        return AuctionPhase.FINAL
    return AuctionPhase.OPEN_EXECUTION
