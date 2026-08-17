from __future__ import annotations

from datetime import datetime, timezone
import inspect
import threading
from typing import Callable

from database import repository
from services.market_data_service import sync_market_overview_daily_prices


class MarketOverviewRefreshCoordinator:
    """The sole background owner of market-overview refreshes.

    Realtime decision events deliberately do not use this coordinator: their
    event-time IEX snapshots are an independent, auditable data path.
    """

    SETTING_KEY = "market_overview_auto_refresh_enabled"

    def __init__(
        self,
        *,
        interval_seconds: float = 300.0,
        sync_callback: Callable[[], dict] = sync_market_overview_daily_prices,
    ):
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.sync_callback = sync_callback
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._auto_enabled = True
        self._last_result: dict | None = None
        self._last_error: str | None = None
        self._updated_at: str | None = None
        self._reason: str | None = None

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            try:
                self._auto_enabled = bool(
                    repository.get_system_setting(self.SETTING_KEY, True)
                )
            except Exception:
                self._auto_enabled = True
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="market-overview-coordinator",
                daemon=True,
            )
            self._thread.start()
        # Opening the application always performs one refresh. The switch
        # controls subsequent five-minute refreshes only.
        self.trigger(reason="startup")

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            if self.auto_enabled:
                self.trigger(reason="periodic")

    @property
    def auto_enabled(self) -> bool:
        with self._lock:
            return self._auto_enabled

    def set_auto_enabled(self, enabled: bool) -> dict:
        value = bool(enabled)
        repository.set_system_setting(self.SETTING_KEY, value)
        with self._lock:
            changed_to_enabled = value and not self._auto_enabled
            self._auto_enabled = value
            self._updated_at = self._now()
        if changed_to_enabled:
            self.trigger(reason="enabled")
        return self.snapshot()

    def trigger(self, *, reason: str = "manual") -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._last_error = None
            self._updated_at = self._now()
            self._reason = str(reason)
        threading.Thread(
            target=self._run_once,
            args=(str(reason),),
            name="market-overview-refresh",
            daemon=True,
        ).start()
        return True

    def _run_once(self, reason: str) -> None:
        try:
            parameters = inspect.signature(self.sync_callback).parameters
            result = (
                self.sync_callback(reason=reason)
                if "reason" in parameters
                else self.sync_callback()
            )
            with self._lock:
                self._last_result = result
                self._last_error = None
                self._updated_at = self._now()
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)
                self._updated_at = self._now()
        finally:
            with self._lock:
                self._running = False

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "running": self._running,
                "auto_enabled": self._auto_enabled,
                "interval_seconds": int(self.interval_seconds),
                "last_result": self._last_result,
                "last_error": self._last_error,
                "updated_at": self._updated_at,
                "reason": self._reason,
            }


market_overview_coordinator = MarketOverviewRefreshCoordinator()
