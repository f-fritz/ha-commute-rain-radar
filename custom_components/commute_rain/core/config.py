# Copied from commute_rain/ by scripts/sync_core.py. Do not edit here.
"""All settings in one place: a list of routes plus shared options."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Literal

SegmentMode = Literal["distance", "time"]


@dataclass(frozen=True)
class CommuteWindow:
    """Acceptable departure times, in local time.

    Fixed: CommuteWindow(time(7, 0), time(9, 30)).
    Rolling: CommuteWindow(end=time(20, 0), max_ahead_min=120), i.e. from
    now up to two hours ahead, but no later than 20:00.
    """

    start: time | None = None
    end: time | None = None
    max_ahead_min: int | None = None

    def contains(self, departure: datetime, now: datetime) -> bool:
        t = departure.timetz().replace(tzinfo=None)
        if self.start is not None and t < self.start:
            return False
        if self.end is not None and t > self.end:
            return False
        if self.max_ahead_min is not None and departure > now + timedelta(minutes=self.max_ahead_min):
            return False
        return True


@dataclass(frozen=True)
class RouteConfig:
    name: str
    gpx_path: Path
    # "distance": equal-length segments of `segment_length_km`.
    # "time": equal-duration segments of `segment_minutes` of riding.
    segment_mode: SegmentMode = "distance"
    segment_length_km: float = 1.0
    segment_minutes: float = 5.0
    # None = use the speed profile from the GPX timestamps.
    ride_duration_min: float | None = None
    reverse: bool = False
    # Departure window for the 2 h radar nowcast (may be rolling) and for the
    # 48 h forecast (must be fixed: start and end set).
    radar_window: CommuteWindow | None = None
    forecast_window: CommuteWindow | None = None


@dataclass(frozen=True)
class Config:
    routes: tuple[RouteConfig, ...]
    timezone: str = "Europe/Berlin"
    cache_dir: Path = Path("data/cache")
    # Extra radar pixels (= km) fetched around the combined route bounding box.
    radar_margin_km: int = 5
    departure_step_min: int = 5
    # A departure only counts as "best" if the rain never exceeds this on any
    # segment. The radar comes in steps of 0.12 mm/h, so 0.3 allows 0.12 and 0.24.
    max_rain_mm_per_h: float | None = 0.3
    forecast_hours: int = 48
    forecast_step_min: int = 15
    # "center": one forecast for the centre of all routes. "route_points":
    # start, middle and end of every route, combined by taking the maximum.
    forecast_points: Literal["center", "route_points"] = "center"
    user_agent: str = "commute-rain-radar/0.1 (personal project; github.com/f-fritz)"
