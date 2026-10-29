# Copied from commute_rain/ by scripts/sync_core.py. Do not edit here.
"""Route model: parse a GPX track and split it into timed segments.

Everything except `load_gpx` is pure (no I/O).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from .config import RouteConfig, SegmentMode

EARTH_RADIUS_M = 6_371_000.0


@dataclass(frozen=True)
class TrackPoint:
    lat: float
    lon: float
    time: datetime | None = None


@dataclass(frozen=True)
class Profile:
    """Track resolved into position / distance / time-offset arrays."""

    lat: np.ndarray
    lon: np.ndarray
    dist_m: np.ndarray  # cumulative distance from start
    offset_s: np.ndarray  # seconds after departure

    @property
    def total_m(self) -> float:
        return float(self.dist_m[-1])

    @property
    def total_s(self) -> float:
        return float(self.offset_s[-1])


@dataclass(frozen=True)
class Segment:
    index: int
    lat: float  # midpoint
    lon: float
    start_km: float
    end_km: float
    start_offset_s: float
    end_offset_s: float
    mid_offset_s: float

    @property
    def mid_km(self) -> float:
        return (self.start_km + self.end_km) / 2

    @property
    def duration_s(self) -> float:
        return self.end_offset_s - self.start_offset_s


@dataclass(frozen=True)
class Route:
    name: str
    segment_mode: SegmentMode
    segments: tuple[Segment, ...]
    total_km: float
    total_s: float
    # Full resampled path, handy for maps and bounding boxes.
    path_lat: np.ndarray
    path_lon: np.ndarray

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """(min_lat, min_lon, max_lat, max_lon)"""
        return (
            float(self.path_lat.min()),
            float(self.path_lon.min()),
            float(self.path_lat.max()),
            float(self.path_lon.max()),
        )


# ---------------------------------------------------------------------------
# GPX parsing
# ---------------------------------------------------------------------------


def load_gpx(path: str | Path) -> list[TrackPoint]:
    return parse_gpx(Path(path).read_text(encoding="utf-8"))


def parse_gpx(xml_text: str) -> list[TrackPoint]:
    """Return all <trkpt> points (GPX 1.0/1.1, with or without namespace)."""
    root = ET.fromstring(xml_text)
    ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
    points = []
    for trkpt in root.iter(f"{ns}trkpt"):
        time_el = trkpt.find(f"{ns}time")
        t = None
        if time_el is not None and time_el.text:
            t = datetime.fromisoformat(time_el.text.strip().replace("Z", "+00:00"))
        points.append(TrackPoint(float(trkpt.attrib["lat"]), float(trkpt.attrib["lon"]), t))
    if len(points) < 2:
        raise ValueError("GPX track needs at least two <trkpt> points")
    return points


# ---------------------------------------------------------------------------
# Profile and segmentation
# ---------------------------------------------------------------------------


def haversine_m(lat1, lon1, lat2, lon2):
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp = p2 - p1
    dl = np.radians(np.asarray(lon2) - np.asarray(lon1))
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * np.arcsin(np.sqrt(a))


def build_profile(
    points: list[TrackPoint],
    ride_duration_s: float | None = None,
    reverse: bool = False,
) -> Profile:
    """Distance and time offset for every track point.

    Uses the GPX timestamps if all points have one and no `ride_duration_s` is
    given; otherwise assumes constant speed over `ride_duration_s`.
    With `reverse`, the route is ridden from end to start (the recorded speed
    profile is reversed as well).
    """
    lat = np.array([p.lat for p in points])
    lon = np.array([p.lon for p in points])
    has_times = all(p.time is not None for p in points)

    if ride_duration_s is None:
        if not has_times:
            raise ValueError("GPX has no timestamps; set ride_duration_min")
        t0 = points[0].time
        offset = np.array([(p.time - t0).total_seconds() for p in points])
    else:
        offset = None  # filled after distances are known

    if reverse:
        lat, lon = lat[::-1], lon[::-1]
        if offset is not None:
            offset = offset[-1] - offset[::-1]

    step = haversine_m(lat[:-1], lon[:-1], lat[1:], lon[1:])
    dist = np.concatenate([[0.0], np.cumsum(step)])

    if offset is None:
        offset = dist / dist[-1] * ride_duration_s
    # GPS timestamps can repeat; keep offsets non-decreasing for interpolation.
    offset = np.maximum.accumulate(offset)
    return Profile(lat=lat, lon=lon, dist_m=dist, offset_s=offset)


def _interp_at(profile: Profile, key: np.ndarray, at: np.ndarray) -> dict[str, np.ndarray]:
    return {
        "lat": np.interp(at, key, profile.lat),
        "lon": np.interp(at, key, profile.lon),
        "dist_m": np.interp(at, key, profile.dist_m),
        "offset_s": np.interp(at, key, profile.offset_s),
    }


def segment_profile(
    profile: Profile,
    mode: SegmentMode = "distance",
    segment_length_km: float = 1.0,
    segment_minutes: float = 5.0,
) -> list[Segment]:
    """Split into equal-distance or equal-duration segments.

    The last segment is whatever is left over (shorter). Midpoints are taken
    halfway along the segmenting quantity (distance or time).
    """
    if mode == "distance":
        key, size = profile.dist_m, segment_length_km * 1000
    elif mode == "time":
        key, size = profile.offset_s, segment_minutes * 60
    else:
        raise ValueError(f"unknown segment mode {mode!r}")
    total = float(key[-1])
    edges = np.arange(0.0, total, size)
    edges = np.append(edges, total)
    # Drop a tiny trailing sliver (< 10 % of a segment) by merging it.
    if len(edges) > 2 and edges[-1] - edges[-2] < 0.1 * size:
        edges = np.delete(edges, -2)

    b = _interp_at(profile, key, edges)
    m = _interp_at(profile, key, (edges[:-1] + edges[1:]) / 2)
    return [
        Segment(
            index=i,
            lat=float(m["lat"][i]),
            lon=float(m["lon"][i]),
            start_km=float(b["dist_m"][i] / 1000),
            end_km=float(b["dist_m"][i + 1] / 1000),
            start_offset_s=float(b["offset_s"][i]),
            end_offset_s=float(b["offset_s"][i + 1]),
            mid_offset_s=float(m["offset_s"][i]),
        )
        for i in range(len(edges) - 1)
    ]


def resample_profile(profile: Profile, step_m: float = 50.0) -> Profile:
    """Profile with one point every `step_m` metres (plus the end point).
    Keeps the speed profile; used to store tracks compactly."""
    at = np.append(np.arange(0.0, profile.total_m, step_m), profile.total_m)
    p = _interp_at(profile, profile.dist_m, at)
    return Profile(lat=p["lat"], lon=p["lon"], dist_m=p["dist_m"], offset_s=p["offset_s"])


def profile_to_track(profile: Profile) -> list[list[float]]:
    """JSON-friendly [[lat, lon, offset_s], ...]."""
    return [
        [round(float(a), 6), round(float(b), 6), round(float(t), 1)]
        for a, b, t in zip(profile.lat, profile.lon, profile.offset_s)
    ]


def profile_from_track(track: list[list[float]]) -> Profile:
    arr = np.asarray(track, dtype=float)
    lat, lon, offset = arr[:, 0], arr[:, 1], arr[:, 2]
    step = haversine_m(lat[:-1], lon[:-1], lat[1:], lon[1:])
    return Profile(lat=lat, lon=lon, dist_m=np.concatenate([[0.0], np.cumsum(step)]), offset_s=offset)


def route_from_profile(
    name: str,
    profile: Profile,
    mode: SegmentMode = "distance",
    segment_length_km: float = 1.0,
    segment_minutes: float = 5.0,
) -> Route:
    segments = segment_profile(profile, mode, segment_length_km, segment_minutes)
    return Route(
        name=name,
        segment_mode=mode,
        segments=tuple(segments),
        total_km=profile.total_m / 1000,
        total_s=profile.total_s,
        path_lat=profile.lat,
        path_lon=profile.lon,
    )


def build_route(cfg: RouteConfig, points: list[TrackPoint]) -> Route:
    duration_s = cfg.ride_duration_min * 60 if cfg.ride_duration_min is not None else None
    profile = build_profile(points, duration_s, cfg.reverse)
    return route_from_profile(cfg.name, profile, cfg.segment_mode, cfg.segment_length_km, cfg.segment_minutes)
