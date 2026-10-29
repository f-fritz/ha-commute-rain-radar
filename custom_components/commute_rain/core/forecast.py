# Copied from commute_rain/ by scripts/sync_core.py. Do not edit here.
"""Hourly point forecast (DWD MOSMIX via Bright Sky `/weather`).

All functions here are pure (no I/O).

Field semantics (from https://api.brightsky.dev/openapi.json):
- `precipitation`: total mm during the 60 minutes *before* `timestamp`,
  i.e. also the mean intensity in mm/h over that hour.
- `precipitation_probability`: % chance of > 0.1 mm in that same hour
  (forecasts only).
- A response can start with an observed hour ("current" source); only
  records from "forecast" sources are used.

The forecast is one value per hour for a point, so the whole route gets the
same number; only the ride's timing matters.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np

from .config import CommuteWindow
from .route import Route

HOUR_S = 3600


@dataclass(frozen=True)
class HourlyForecast:
    times: np.ndarray  # int64 epoch s, *end* of each hour
    precipitation_mm: np.ndarray  # mm in the hour = mean mm/h
    probability: np.ndarray  # % (> 0.1 mm in the hour), NaN if missing
    stations: tuple[str, ...]  # forecast station name(s) used


def parse_weather_response(data: dict) -> HourlyForecast:
    sources = {s["id"]: s for s in data.get("sources", [])}
    records = [
        r for r in data["weather"]
        if sources.get(r["source_id"], {}).get("observation_type", "forecast") == "forecast"
    ]
    records.sort(key=lambda r: r["timestamp"])
    used = sorted({sources[r["source_id"]]["station_name"] for r in records if r["source_id"] in sources})

    def num(v):
        return np.nan if v is None else float(v)

    return HourlyForecast(
        times=np.array([int(datetime.fromisoformat(r["timestamp"]).timestamp()) for r in records], dtype=np.int64),
        precipitation_mm=np.array([num(r["precipitation"]) for r in records]),
        probability=np.array([num(r["precipitation_probability"]) for r in records]),
        stations=tuple(used),
    )


def combine_max(forecasts: list[HourlyForecast]) -> HourlyForecast:
    """Hour-by-hour maximum over several points (e.g. route start and end)."""
    times = forecasts[0].times
    for f in forecasts[1:]:
        times = np.intersect1d(times, f.times)

    def stack(attr):
        return np.nanmax([getattr(f, attr)[np.isin(f.times, times)] for f in forecasts], axis=0)

    stations = tuple(sorted({s for f in forecasts for s in f.stations}))
    return HourlyForecast(times, stack("precipitation_mm"), stack("probability"), stations)


def intensity_at(fc: HourlyForecast, t) -> np.ndarray:
    """mm/h of the forecast hour containing each time in `t` (NaN outside)."""
    t = np.asarray(t, dtype=float)
    if len(fc.times) == 0:
        return np.full(t.shape, np.nan)
    i = np.minimum(np.searchsorted(fc.times, t, side="left"), len(fc.times) - 1)  # first hour ending >= t
    inside = (t <= fc.times[i]) & (t > fc.times[i] - HOUR_S)
    return np.where(inside, fc.precipitation_mm[i], np.nan)


# ---------------------------------------------------------------------------
# Rides
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RideForecast:
    departure: datetime
    expected_mm: float  # mm/h of each hour x time spent riding in it
    peak_mm_per_h: float  # wettest hour touched by the ride
    max_probability: float  # % of the most likely-wet hour touched by the ride


def ride_forecast(fc: HourlyForecast, departure: int, duration_s: float, tz: str = "Europe/Berlin") -> RideForecast | None:
    """Expected rain for one ride; None if the ride is not fully covered."""
    start, end = departure, departure + duration_s
    hour_start = fc.times - HOUR_S
    if len(fc.times) == 0 or start < hour_start[0] or end > fc.times[-1]:
        return None
    overlap = np.clip(np.minimum(end, fc.times) - np.maximum(start, hour_start), 0, None)
    touched = overlap > 0
    return RideForecast(
        departure=datetime.fromtimestamp(departure, ZoneInfo(tz)),
        expected_mm=float(np.nansum(fc.precipitation_mm * overlap / HOUR_S)),
        peak_mm_per_h=float(np.nanmax(fc.precipitation_mm[touched])),
        max_probability=float(np.nanmax(fc.probability[touched])) if not np.isnan(fc.probability[touched]).all() else np.nan,
    )


@dataclass(frozen=True)
class WindowForecast:
    """All departures in one commute window on one day."""

    route: str
    day: date
    window: CommuteWindow
    rides: tuple[RideForecast, ...]  # departures covered by the forecast
    best: RideForecast | None  # least rain with peak <= cap; None if none qualifies
    complete: bool  # False if some departures (already past, or beyond 48 h) are missing

    @property
    def min_mm(self) -> float:
        return min(r.expected_mm for r in self.rides)

    @property
    def max_mm(self) -> float:
        return max(r.expected_mm for r in self.rides)

    @property
    def max_probability(self) -> float:
        return max(r.max_probability for r in self.rides)


def window_forecasts(
    fc: HourlyForecast,
    route: Route,
    window: CommuteWindow,
    tz: str = "Europe/Berlin",
    step_min: int = 15,
    max_mm_per_h: float | None = None,
) -> list[WindowForecast]:
    """One WindowForecast per day with at least one departure covered by
    `fc`. The window must be fixed (start and end set)."""
    if window.start is None or window.end is None:
        raise ValueError("forecast windows need a fixed start and end")
    zone = ZoneInfo(tz)
    first = datetime.fromtimestamp(int(fc.times[0]) - HOUR_S, zone).date()
    last = datetime.fromtimestamp(int(fc.times[-1]), zone).date()
    out = []
    day = first
    while day <= last:
        t0 = datetime.combine(day, window.start, zone)
        t1 = datetime.combine(day, window.end, zone)
        deps = range(int(t0.timestamp()), int(t1.timestamp()) + 1, step_min * 60)
        all_rides = [ride_forecast(fc, d, route.total_s, tz) for d in deps]
        rides = [r for r in all_rides if r is not None]
        if rides:
            ok = [r for r in rides if max_mm_per_h is None or r.peak_mm_per_h <= max_mm_per_h]
            best = min(ok, key=lambda r: (r.expected_mm, r.departure)) if ok else None
            out.append(WindowForecast(route.name, day, window, tuple(rides), best, len(rides) == len(all_rides)))
        day += timedelta(days=1)
    return out
