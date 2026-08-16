import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterator, List, Optional, Sequence

from domain.enums import ReplayMode
from domain.models import AuctionTick


class FutureDataAccessError(RuntimeError):
    pass


@dataclass
class VirtualClock:
    now: datetime

    def advance_to(self, target: datetime) -> None:
        if target < self.now:
            raise ValueError("virtual clock cannot move backwards")
        self.now = target

    def assert_visible(self, exchange_ts: Optional[datetime]) -> None:
        if exchange_ts is None or exchange_ts > self.now:
            raise FutureDataAccessError(f"tick at {exchange_ts!r} is not visible at {self.now!r}")


class ReplayEngine:
    def __init__(self, ticks: Sequence[AuctionTick], clock: VirtualClock):
        self._ticks = tuple(sorted(ticks, key=lambda t: t.exchange_ts.timestamp() if t.exchange_ts else float("inf")))
        self.clock = clock
        self._cursor = 0
        self._visible: List[AuctionTick] = []

    @property
    def visible_ticks(self) -> Sequence[AuctionTick]:
        for tick in self._visible:
            self.clock.assert_visible(tick.exchange_ts)
        return tuple(self._visible)

    def get_ticks(self, as_of: datetime) -> Sequence[AuctionTick]:
        if as_of > self.clock.now:
            raise FutureDataAccessError(f"requested {as_of!r}, clock is {self.clock.now!r}")
        return tuple(t for t in self._visible if t.exchange_ts is not None and t.exchange_ts <= as_of)

    def step(self) -> Optional[AuctionTick]:
        if self._cursor >= len(self._ticks):
            return None
        tick = self._ticks[self._cursor]
        if tick.exchange_ts is None:
            raise ValueError("replay requires exchange_ts")
        self.clock.advance_to(tick.exchange_ts)
        self._visible.append(tick)
        self._cursor += 1
        return tick

    def run(
        self,
        mode: ReplayMode = ReplayMode.FULL_SPEED,
        checkpoints: Sequence[datetime] = (),
        on_tick: Optional[Callable[[AuctionTick, "ReplayEngine"], None]] = None,
    ) -> Iterator[AuctionTick]:
        checkpoint_target = None
        if mode == ReplayMode.CHECKPOINT:
            eligible = sorted(checkpoint for checkpoint in checkpoints if checkpoint >= self.clock.now)
            if not eligible:
                return
            checkpoint_target = eligible[0]
        previous_ts = self.clock.now
        while self._cursor < len(self._ticks):
            next_tick = self._ticks[self._cursor]
            if next_tick.exchange_ts is None:
                raise ValueError("replay requires exchange_ts")
            if checkpoint_target is not None and next_tick.exchange_ts > checkpoint_target:
                self.clock.advance_to(checkpoint_target)
                return
            if mode == ReplayMode.REAL_TIME:
                time.sleep(max(0.0, (next_tick.exchange_ts - previous_ts).total_seconds()))
            tick = self.step()
            assert tick is not None
            previous_ts = tick.exchange_ts  # type: ignore[assignment]
            if on_tick is not None:
                on_tick(tick, self)
            yield tick
            if mode == ReplayMode.STEP:
                return
            if checkpoint_target is not None:
                next_ts = self._ticks[self._cursor].exchange_ts if self._cursor < len(self._ticks) else None
                if next_ts is None or next_ts > checkpoint_target:
                    self.clock.advance_to(checkpoint_target)
                    return
