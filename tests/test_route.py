from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from commute_rain.config import RouteConfig
from commute_rain.route import TrackPoint, build_profile, build_route, parse_gpx, segment_profile

T0 = datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc)
# 0.009 deg latitude is ~1.0 km.
DLAT_KM = 1 / 111.195


def straight_north(km: float, n: int = 101, seconds: float | None = None):
    """Points due north from 50N 7E, evenly spaced; optional timestamps."""
    lats = 50 + np.linspace(0, km * DLAT_KM, n)
    times = [T0 + timedelta(seconds=s) for s in np.linspace(0, seconds, n)] if seconds else [None] * n
    return [TrackPoint(float(la), 7.0, t) for la, t in zip(lats, times)]


def test_parse_gpx_namespaced():
    gpx = """<?xml version="1.0"?>
    <gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1"><trk><trkseg>
      <trkpt lat="50.0" lon="7.0"><time>2026-10-20T07:00:00Z</time></trkpt>
      <trkpt lat="50.01" lon="7.0"><time>2026-10-20T07:02:00.000Z</time></trkpt>
    </trkseg></trk></gpx>"""
    pts = parse_gpx(gpx)
    assert len(pts) == 2
    assert pts[1].lat == 50.01
    assert pts[1].time == datetime(2026, 10, 20, 7, 2, tzinfo=timezone.utc)


def test_distance_segments_constant_speed():
    prof = build_profile(straight_north(10.0), ride_duration_s=40 * 60)
    assert prof.total_m == pytest.approx(10_000, rel=1e-3)
    segs = segment_profile(prof, "distance", 1.0)
    assert len(segs) == 10
    for i, s in enumerate(segs):
        assert s.start_km == pytest.approx(i, abs=1e-6)
        assert s.mid_km == pytest.approx(i + 0.5, abs=1e-3)
        assert s.mid_offset_s == pytest.approx((i + 0.5) * 240, abs=1)
        assert s.lat == pytest.approx(50 + (i + 0.5) * DLAT_KM, abs=1e-5)


def test_remainder_segment_and_sliver_merge():
    prof = build_profile(straight_north(3.5), ride_duration_s=600)
    segs = segment_profile(prof, "distance", 1.0)
    assert [round(s.end_km - s.start_km, 2) for s in segs] == [1, 1, 1, 0.5]
    # 3.05 km: the 50 m sliver is merged into the last full segment.
    prof = build_profile(straight_north(3.05), ride_duration_s=600)
    segs = segment_profile(prof, "distance", 1.0)
    assert len(segs) == 3
    assert segs[-1].end_km == pytest.approx(3.05, abs=1e-3)


def test_gpx_timestamps_speed_profile():
    # First 5 km in 10 min, last 5 km in 30 min.
    fast = straight_north(5.0, n=51, seconds=600)
    slow_lats = 50 + np.linspace(5, 10, 51)[1:] * DLAT_KM
    slow_times = [T0 + timedelta(seconds=600 + s) for s in np.linspace(0, 1800, 51)[1:]]
    pts = fast + [TrackPoint(float(la), 7.0, t) for la, t in zip(slow_lats, slow_times)]
    segs = segment_profile(build_profile(pts), "distance", 1.0)
    assert segs[0].duration_s == pytest.approx(120, abs=1)
    assert segs[-1].duration_s == pytest.approx(360, abs=1)
    assert segs[-1].end_offset_s == pytest.approx(2400, abs=1)


def test_time_segments():
    pts = straight_north(10.0, seconds=38 * 60)
    segs = segment_profile(build_profile(pts), "time", segment_minutes=5)
    assert len(segs) == 8  # 7 x 5 min + 3 min
    assert segs[0].duration_s == pytest.approx(300)
    assert segs[-1].duration_s == pytest.approx(180)
    assert segs[0].end_km == pytest.approx(10 * 5 / 38, abs=1e-3)


def test_reverse_flips_positions_and_speed_profile():
    fast = straight_north(5.0, n=51, seconds=600)
    slow_lats = 50 + np.linspace(5, 10, 51)[1:] * DLAT_KM
    slow_times = [T0 + timedelta(seconds=600 + s) for s in np.linspace(0, 1800, 51)[1:]]
    pts = fast + [TrackPoint(float(la), 7.0, t) for la, t in zip(slow_lats, slow_times)]
    fwd = segment_profile(build_profile(pts), "distance", 1.0)
    rev = segment_profile(build_profile(pts, reverse=True), "distance", 1.0)
    assert rev[0].lat == pytest.approx(fwd[-1].lat, abs=1e-6)
    assert rev[0].duration_s == pytest.approx(fwd[-1].duration_s, abs=1)
    assert rev[-1].end_offset_s == pytest.approx(fwd[-1].end_offset_s, abs=1)


def test_no_timestamps_requires_duration():
    with pytest.raises(ValueError):
        build_profile(straight_north(2.0))


def test_build_route(tmp_path):
    pts = straight_north(2.0, n=3, seconds=480)
    gpx = tmp_path / "r.gpx"
    rows = "".join(f'<trkpt lat="{p.lat}" lon="{p.lon}"><time>{p.time.isoformat()}</time></trkpt>' for p in pts)
    gpx.write_text(f'<gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>{rows}</trkseg></trk></gpx>')
    route = build_route(RouteConfig("r", gpx), parse_gpx(gpx.read_text()))
    assert len(route.segments) == 2
    assert route.total_s == pytest.approx(480)


def test_resample_and_track_roundtrip():
    from commute_rain.route import profile_from_track, profile_to_track, resample_profile, route_from_profile

    pts = straight_north(10.0, n=1001, seconds=40 * 60)
    prof = build_profile(pts)
    small = resample_profile(prof, 50)
    assert len(small.lat) == 201
    back = profile_from_track(profile_to_track(small))
    assert back.total_m == pytest.approx(prof.total_m, rel=1e-4)
    assert back.total_s == pytest.approx(prof.total_s)
    a = segment_profile(prof, "distance", 1.0)
    b = route_from_profile("x", back).segments
    assert [round(s.mid_offset_s) for s in a] == [round(s.mid_offset_s) for s in b]
