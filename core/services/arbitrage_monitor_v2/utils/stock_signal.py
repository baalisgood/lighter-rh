"""In-memory stock decision signals. Prices and FIFO costs remain raw."""
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time
from math import exp, isfinite
from zoneinfo import ZoneInfo


@dataclass
class BasisState:
    window: float = 3600.0
    warmup: float = 3600.0
    drift_window: float = 3600.0
    drift_limit: float = 0.10
    max_gap: float = 3.0
    recovery_seconds: float = 60.0
    reset_after: float = 600.0
    ema: float | None = None
    started: float | None = None
    last: float | None = None
    valid_seconds: float = 0.0
    recovery_started: float | None = None
    recovering: bool = False
    history: deque = field(default_factory=deque)
    ready: bool = False
    stable: bool = False

    def reset(self):
        self.ema = self.started = self.last = self.recovery_started = None
        self.valid_seconds = 0.0
        self.history.clear()
        self.ready = self.stable = self.recovering = False

    def observe(self, now, value):
        if not isfinite(now) or (self.last is not None and now < self.last):
            self.reset()
            return
        gap = None if self.last is None else now - self.last
        if gap is not None and gap > self.reset_after:
            self.reset()
            gap = None
        if value is None or not isfinite(value):
            self.ready = self.stable = False
            self.recovering = True
            self.recovery_started = None
            return
        if gap is not None and gap > self.max_gap:
            self.recovering = True
            self.recovery_started = None
        if gap is not None and gap < 1.0:
            return
        if self.last is None:
            self.started, self.ema = now, value
        elif gap <= self.max_gap and (not self.recovering or self.recovery_started is not None):
            self.valid_seconds += gap
            self.ema += (1 - exp(-gap / self.window)) * (value - self.ema)
        # The first sample after an outage must not weight the offline interval.
        if self.recovering and self.recovery_started is None:
            self.recovery_started = now
        self.last = now
        self.history.append((now, self.ema))
        cutoff = now - self.drift_window
        while len(self.history) > 1 and self.history[1][0] <= cutoff:
            self.history.popleft()
        mature = self.valid_seconds >= max(self.warmup, self.drift_window)
        recovered = not self.recovering or now - self.recovery_started >= self.recovery_seconds
        self.ready = mature and recovered
        self.stable = self.ready and abs(self.ema - self.history[0][1]) <= self.drift_limit


def cash_threshold(grid, now=None):
    schedule = grid.stock_session
    # 24x7 markets (crypto) opt out with stock_session.enabled: false.
    if not schedule or schedule.get("enabled", True) is False:
        return grid.initial_spread_threshold
    local = (now or datetime.now(ZoneInfo("America/New_York"))).astimezone(ZoneInfo("America/New_York"))
    date = local.date().isoformat()
    known = local.year in schedule.get("calendar_valid_years", [])
    closed = set(schedule.get("closed_dates", [])) | set(schedule.get("closed_overrides", []))
    end = time.fromisoformat(schedule.get("early_closes", {}).get(date, "16:00"))
    regular = known and local.weekday() < 5 and date not in closed and time(9, 30) <= local.time().replace(tzinfo=None) < end
    return float(grid.initial_spread_threshold if regular else schedule.get("offhours_threshold_pct", grid.initial_spread_threshold + .06))
