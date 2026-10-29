"""Radar nowcast: grid projection, response parsing and the rain matrix.

All functions here are pure (no I/O).

Grid facts (from https://api.brightsky.dev/openapi.json, verified against
the API's own `latlon_position` and corner coordinates):
- 1200 rows (N-S) x 1100 columns (W-E), 1 km pixels, polar stereographic:
  +proj=stere +lat_0=90 +lat_ts=60 +lon_0=10 +a=6378137
  +b=6356752.3142451802 +x_0=543196.83521776402 +y_0=3622588.8619310018
- Pixel (row, col) is centred on x = col * 1000 m, y = -row * 1000 m.
- Values are 0.01 mm per 5 minutes, so mm/h = value * 0.12.
- Frame at timestamp T holds the rain of the 5 minutes ending at T.
"""

from __future__ import annotations

import base64
import zlib
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np

from .config import CommuteWindow
from .forecast import HourlyForecast, intensity_at
from .route import Route

GRID_ROWS = 1200
GRID_COLS = 1100
FRAME_S = 300
RAW_TO_MM_PER_H = 0.01 * (3600 / FRAME_S)

# Projection constants (see module docstring).
_A = 6378137.0
_B = 6356752.3142451802
_E = np.sqrt(1 - _B**2 / _A**2)
_X0 = 543196.83521776402
_Y0 = 3622588.8619310018
_LON0 = np.radians(10.0)
_LAT_TS = np.radians(60.0)


def _t(phi):
    es = _E * np.sin(phi)
    return np.tan(np.pi / 4 - phi / 2) / ((1 - es) / (1 + es)) ** (_E / 2)


_RHO_SCALE = _A * (np.cos(_LAT_TS) / np.sqrt(1 - (_E * np.sin(_LAT_TS)) ** 2)) / _t(_LAT_TS)


def latlon_to_grid(lat, lon):
    """Fractional (row, col) on the full radar grid; integers are pixel centres."""
    phi = np.radians(np.asarray(lat, dtype=float))
    dlam = np.radians(np.asarray(lon, dtype=float)) - _LON0
    rho = _RHO_SCALE * _t(phi)
    x = rho * np.sin(dlam) + _X0
    y = -rho * np.cos(dlam) + _Y0
    return -y / 1000.0, x / 1000.0


def latlon_to_pixel(lat, lon):
    """Integer (row, col) of the pixel containing lat/lon."""
    row, col = latlon_to_grid(lat, lon)
    return np.rint(row).astype(int), np.rint(col).astype(int)


PixelBBox = tuple[int, int, int, int]  # (top, left, bottom, right), inclusive


def bbox_for_routes(routes: list[Route], margin_px: int = 5) -> PixelBBox:
    """Combined pixel bounding box of all routes plus a margin."""
    lat = np.concatenate([r.path_lat for r in routes])
    lon = np.concatenate([r.path_lon for r in routes])
    row, col = latlon_to_pixel(lat, lon)
    return (
        max(int(row.min()) - margin_px, 0),
        max(int(col.min()) - margin_px, 0),
        min(int(row.max()) + margin_px, GRID_ROWS - 1),
        min(int(col.max()) + margin_px, GRID_COLS - 1),
    )


# ---------------------------------------------------------------------------
# Radar data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RadarData:
    """Stack of radar frames cut to a pixel bounding box."""

    times: np.ndarray  # int64 epoch seconds (UTC), end of each 5-min interval
    mm_per_h: np.ndarray  # float32 (n_frames, rows, cols)
    bbox: PixelBBox
    observed_until: int  # epoch s of the latest measured (non-forecast) frame

    @property
    def nowcast_end(self) -> int:
        return int(self.times[-1])

    def frame_index(self, t_epoch):
        """Index of the frame whose 5-minute interval contains `t` (or -1)."""
        t = np.asarray(t_epoch, dtype=float)
        idx = np.ceil((t - self.times[0]) / FRAME_S).astype(int)
        valid = (idx >= 0) & (idx < len(self.times))
        return np.where(valid, idx, -1)


def _to_epoch(ts: str) -> int:
    return int(datetime.fromisoformat(ts).timestamp())


def _decode(field, shape: tuple[int, int], fmt: str) -> np.ndarray:
    if fmt == "plain":
        return np.asarray(field, dtype=np.uint16)
    raw = base64.b64decode(field)
    if fmt == "compressed":
        raw = zlib.decompress(raw)
    return np.frombuffer(raw, dtype="<u2").reshape(shape)


def parse_radar_response(data: dict, bbox: PixelBBox, fmt: str = "compressed") -> RadarData:
    """Turn a raw `/radar` JSON response into RadarData (mm/h)."""
    top, left, bottom, right = data.get("bbox") or bbox
    shape = (bottom - top + 1, right - left + 1)
    records = sorted(data["radar"], key=lambda r: r["timestamp"])
    times = np.array([_to_epoch(r["timestamp"]) for r in records], dtype=np.int64)
    frames = np.stack([_decode(r["precipitation_5"], shape, fmt) for r in records])
    # `source` ends with the time of the measurement the frame is based on:
    # equal to the frame timestamp for observations, earlier for nowcasts.
    observed = [_to_epoch(r["timestamp"]) for r in records if r["source"].endswith(r["timestamp"])]
    return RadarData(
        times=times,
        mm_per_h=(frames * RAW_TO_MM_PER_H).astype(np.float32),
        bbox=(top, left, bottom, right),
        observed_until=max(observed) if observed else int(times[0]),
    )


# ---------------------------------------------------------------------------
# Departure x segment matrix
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RainMatrix:
    route: Route
    now: int  # epoch s of the latest observation, reference for rolling windows
    departures: np.ndarray  # int64 epoch seconds
    intensity: np.ndarray  # mm/h, (n_segments, n_departures), NaN = beyond nowcast
    rain_mm: np.ndarray  # mm collected per segment = intensity * time on segment
    total_mm: np.ndarray  # (n_departures,), sum over segments; NaN if incomplete
    partial_mm: np.ndarray  # (n_departures,), sum over the covered segments only
    complete: np.ndarray  # bool (n_departures,): every segment has a value
    # bool like `intensity`: value taken from the hourly forecast, not the radar.
    from_forecast: np.ndarray | None = None

    @property
    def uses_forecast(self) -> np.ndarray:
        """Per departure: at least one segment comes from the forecast."""
        if self.from_forecast is None:
            return np.zeros(len(self.departures), dtype=bool)
        return self.from_forecast.any(axis=0)

    @property
    def peak_mm_per_h(self) -> np.ndarray:
        """Heaviest rain on the (covered part of the) ride, per departure."""
        out = np.full(len(self.departures), np.nan)
        any_data = ~np.isnan(self.intensity).all(axis=0)
        out[any_data] = np.nanmax(self.intensity[:, any_data], axis=0)
        return out

    def in_window(self, window: CommuteWindow | None, tz: str = "Europe/Berlin") -> np.ndarray:
        if window is None:
            return np.ones(len(self.departures), dtype=bool)
        zone = ZoneInfo(tz)
        now = datetime.fromtimestamp(self.now, zone)
        return np.array([window.contains(d, now) for d in self.departure_datetimes(zone)], dtype=bool)

    def departure_datetimes(self, tz: str | ZoneInfo = "Europe/Berlin") -> list[datetime]:
        zone = ZoneInfo(tz) if isinstance(tz, str) else tz
        return [datetime.fromtimestamp(int(t), zone) for t in self.departures]


def departure_times(radar: RadarData, step_min: int = 5, start: int | None = None) -> np.ndarray:
    """Candidate departures every `step_min`, from `start` (default: latest
    observation) to the end of the nowcast."""
    first = radar.observed_until if start is None else start
    step = step_min * 60
    first = -(-first // step) * step  # round up to the step
    return np.arange(first, radar.nowcast_end + 1, step, dtype=np.int64)


def window_departures(window: CommuteWindow, now: datetime, step_min: int = 5) -> np.ndarray:
    """Departures every `step_min` in the next occurrence of a fixed window:
    today's, from `now` on, while it is still open; otherwise tomorrow's."""
    if window.start is None or window.end is None:
        raise ValueError("window_departures needs a fixed start and end")
    step = step_min * 60
    earliest = -(-int(now.timestamp()) // step) * step  # round up to the step
    day = now.date()
    while True:
        start = int(datetime.combine(day, window.start, now.tzinfo).timestamp())
        end = int(datetime.combine(day, window.end, now.tzinfo).timestamp())
        deps = np.arange(max(start, earliest), end + 1, step, dtype=np.int64)
        if len(deps):
            return deps
        day += timedelta(days=1)


def compute_rain_matrix(
    route: Route,
    radar: RadarData | None,
    departures: np.ndarray,
    forecast: HourlyForecast | None = None,
    now: int | None = None,
) -> RainMatrix:
    """Look up, for each departure and segment, the rain at the segment's
    midpoint in the frame covering departure + segment midpoint offset.

    Cells the radar does not cover (beyond the nowcast, or no radar at all)
    are filled from the hourly `forecast` if given: the same value on every
    segment, flagged in `from_forecast`. `now` (reference for rolling
    windows) defaults to the radar's latest observation."""
    if now is None:
        if radar is None:
            raise ValueError("now is required without radar")
        now = radar.observed_until
    mid = np.array([s.mid_offset_s for s in route.segments])
    dur_h = np.array([s.duration_s for s in route.segments]) / 3600
    t = departures[None, :] + mid[:, None]  # (n_seg, n_dep)
    intensity = np.full(t.shape, np.nan, dtype=float)

    if radar is not None:
        lat = np.array([s.lat for s in route.segments])
        lon = np.array([s.lon for s in route.segments])
        row, col = latlon_to_pixel(lat, lon)
        top, left, bottom, right = radar.bbox
        inside = (row >= top) & (row <= bottom) & (col >= left) & (col <= right)
        if not inside.all():
            raise ValueError(f"route {route.name!r} leaves the radar bounding box")
        r, c = row - top, col - left
        fi = radar.frame_index(t)
        valid = fi >= 0
        seg_idx = np.broadcast_to(np.arange(len(lat))[:, None], t.shape)
        intensity[valid] = radar.mm_per_h[fi[valid], r[seg_idx[valid]], c[seg_idx[valid]]]

    from_forecast = None
    if forecast is not None:
        fc = intensity_at(forecast, t)
        from_forecast = np.isnan(intensity) & ~np.isnan(fc)
        intensity[from_forecast] = fc[from_forecast]

    rain_mm = intensity * dur_h[:, None]
    complete = ~np.isnan(intensity).any(axis=0)
    partial = np.nansum(rain_mm, axis=0)
    total = np.where(complete, partial, np.nan)
    return RainMatrix(route, now, departures, intensity, rain_mm, total, partial, complete, from_forecast)


# ---------------------------------------------------------------------------
# Condensed results (future HA sensors)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Departure:
    time: datetime
    total_mm: float
    peak_mm_per_h: float


def _departure(m: RainMatrix, i: int, tz: str) -> Departure:
    return Departure(
        time=datetime.fromtimestamp(int(m.departures[i]), ZoneInfo(tz)),
        total_mm=float(m.total_mm[i]),
        peak_mm_per_h=float(m.peak_mm_per_h[i]),
    )


def rain_if_leaving_now(m: RainMatrix, tz: str = "Europe/Berlin") -> Departure | None:
    """First candidate departure, if the whole ride is inside the nowcast."""
    return _departure(m, 0, tz) if len(m.departures) and m.complete[0] else None


def best_departure(
    m: RainMatrix,
    tz: str = "Europe/Berlin",
    window: CommuteWindow | None = None,
    max_mm_per_h: float | None = None,
) -> Departure | None:
    """Departure with the least total rain (earliest on ties), among rides
    fully covered by the nowcast, inside `window`, and never heavier than
    `max_mm_per_h` on any segment. None if no departure qualifies."""
    ok = m.complete & m.in_window(window, tz)
    if max_mm_per_h is not None:
        ok &= m.peak_mm_per_h <= max_mm_per_h
    if not ok.any():
        return None
    i = int(np.argmin(np.where(ok, m.total_mm, np.inf)))
    return _departure(m, i, tz)


# ---------------------------------------------------------------------------
# Request planning
# ---------------------------------------------------------------------------


def cluster_bboxes(bboxes: list[PixelBBox], max_size_px: int = 150) -> list[tuple[PixelBBox, list[int]]]:
    """Merge bounding boxes into as few requests as possible, as long as each
    merged box stays within `max_size_px` per side. Returns (merged bbox,
    indices of the input boxes it serves)."""
    clusters: list[tuple[PixelBBox, list[int]]] = []
    for i, b in enumerate(bboxes):
        for k, (c, members) in enumerate(clusters):
            m = (min(b[0], c[0]), min(b[1], c[1]), max(b[2], c[2]), max(b[3], c[3]))
            if m[2] - m[0] + 1 <= max_size_px and m[3] - m[1] + 1 <= max_size_px:
                clusters[k] = (m, members + [i])
                break
        else:
            clusters.append((b, [i]))
    return clusters


def radar_needed(windows: list[CommuteWindow], now: datetime, lead_min: int = 120) -> bool:
    """True while any departure window can fall inside the 2 h nowcast: from
    `lead_min` before the window opens until it closes (local time of `now`).
    A window without start counts as open from midnight, without end until
    midnight."""
    day = now.date()
    for w in windows:
        start = datetime.combine(day, w.start or time(0, 0), now.tzinfo) - timedelta(minutes=lead_min)
        end = datetime.combine(day, w.end or time(23, 59, 59), now.tzinfo)
        if start <= now <= end:
            return True
    return False
