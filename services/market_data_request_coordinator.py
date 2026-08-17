from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
from typing import Any, Callable, Hashable


PRIORITY_FORMAL_DECISION = 0
PRIORITY_MANUAL = 10
PRIORITY_OVERVIEW = 20


@dataclass
class _Flight:
    key: Hashable
    priority: int
    sequence: int
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: BaseException | None = None


class MarketDataRequestCoordinator:
    """Coordinate market-data work across UI, overview, and formal decisions.

    Exact keys share one in-flight result. Formal work may use the reserved
    second slot while a manual or overview request is running, preventing
    background work from consuming the whole process-level request lane.
    """

    def __init__(self, *, max_active: int = 2, max_background_active: int = 1):
        self.max_active = max(1, int(max_active))
        self.max_background_active = max(1, int(max_background_active))
        self._condition = threading.Condition(threading.RLock())
        self._sequence = 0
        self._pending: list[_Flight] = []
        self._in_flight: dict[Hashable, _Flight] = {}
        self._active: list[_Flight] = []
        self._recent: dict[Hashable, tuple[float, Any]] = {}

    def run(
        self,
        key: Hashable,
        *,
        priority: int,
        callback: Callable[[], Any],
        reuse_seconds: float = 0.0,
    ) -> Any:
        follower: _Flight | None = None
        with self._condition:
            recent = self._recent.get(key)
            if (
                recent is not None
                and reuse_seconds > 0
                and time.monotonic() - recent[0] <= float(reuse_seconds)
            ):
                return recent[1]
            follower = self._in_flight.get(key)
            if follower is None:
                self._sequence += 1
                flight = _Flight(
                    key=key,
                    priority=int(priority),
                    sequence=self._sequence,
                )
                self._in_flight[key] = flight
                self._pending.append(flight)
                while not self._can_start(flight):
                    self._condition.wait()
                self._pending.remove(flight)
                self._active.append(flight)
            else:
                flight = follower

        if follower is not None:
            flight.event.wait()
            if flight.error is not None:
                raise flight.error
            return flight.result

        try:
            result = callback()
        except BaseException as exc:
            with self._condition:
                flight.error = exc
            raise
        else:
            with self._condition:
                flight.result = result
                if reuse_seconds > 0:
                    self._recent[key] = (time.monotonic(), result)
            return result
        finally:
            with self._condition:
                if flight in self._active:
                    self._active.remove(flight)
                self._in_flight.pop(key, None)
                flight.event.set()
                self._prune_recent()
                self._condition.notify_all()

    def _can_start(self, flight: _Flight) -> bool:
        if len(self._active) >= self.max_active:
            return False
        if flight.priority > PRIORITY_FORMAL_DECISION:
            background_active = sum(
                1 for item in self._active
                if item.priority > PRIORITY_FORMAL_DECISION
            )
            if background_active >= self.max_background_active:
                return False
        first = min(
            self._pending,
            key=lambda item: (item.priority, item.sequence),
            default=flight,
        )
        return first is flight

    def _prune_recent(self) -> None:
        cutoff = time.monotonic() - 300.0
        for key, (completed_at, _result) in list(self._recent.items()):
            if completed_at < cutoff:
                self._recent.pop(key, None)

    def snapshot(self) -> dict:
        with self._condition:
            return {
                "active": len(self._active),
                "pending": len(self._pending),
                "active_keys": [str(item.key) for item in self._active],
            }


market_data_request_coordinator = MarketDataRequestCoordinator()
