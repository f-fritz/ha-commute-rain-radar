import base64
import json
import zlib
from pathlib import Path

import numpy as np
import pytest

from commute_rain.radar import (
    FRAME_S,
    RadarData,
    best_departure,
    compute_rain_matrix,
    departure_times,
    latlon_to_grid,
    latlon_to_pixel,
    parse_radar_response,
)
from commute_rain.route import Route, Segment
from commute_rain.synthetic import RainBand, synthetic_radar

# ---------------------------------------------------------------------------
# lat/lon -> grid
# ---------------------------------------------------------------------------

# Corner coordinates from the API docs; they are the *outer edges* of the
# corner pixels, i.e. +-0.5 px from the pixel centres.
CORNERS = {
    "NW": (55.86208711, 1.463301510, -0.5, -0.5),
    "NE": (55.84543856, 18.73161645, -0.5, 1099.5),
    "SE": (45.68460578, 16.58086935, 1199.5, 1099.5),
    "SW": (45.69642538, 3.566994635, 1199.5, -0.5),
}


@pytest.mark.parametrize("name", CORNERS)
def test_grid_corners(name):
    lat, lon, row, col = CORNERS[name]
    r, c = latlon_to_grid(lat, lon)
    assert r == pytest.approx(row, abs=1e-3)
    assert c == pytest.approx(col, abs=1e-3)


def test_grid_matches_api_latlon_position():
    # Live response for lat=50.9413 lon=6.9583 (Cologne Cathedral) distance=15000
    # returned bbox=[586, 304, 616, 334] and latlon_position={x: 14.763, y: 15.042}.
    r, c = latlon_to_grid(50.9413, 6.9583)
    assert r == pytest.approx(586 + 15.042, abs=1e-3)
    assert c == pytest.approx(304 + 14.763, abs=1e-3)
    assert latlon_to_pixel(50.9413, 6.9583) == (601, 319)


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _record(ts, source_ts, arr):
    raw = base64.b64encode(zlib.compress(arr.astype("<u2").tobytes())).decode()
    return {"timestamp": ts, "source": f"RADARCOMP::RV::{source_ts}", "precipitation_5": raw}


def test_parse_compressed_response():
    a = np.arange(6, dtype=np.uint16).reshape(2, 3)
    data = {
        "radar": [
            _record("2026-10-24T11:25:00+00:00", "2026-10-24T11:20:00+00:00", a * 2),
            _record("2026-10-24T11:20:00+00:00", "2026-10-24T11:20:00+00:00", a),
        ]
    }
    rd = parse_radar_response(data, bbox=(10, 20, 11, 22))
    assert rd.mm_per_h.shape == (2, 2, 3)
    assert rd.mm_per_h[0, 1, 2] == pytest.approx(5 * 0.12)  # sorted by time
    assert rd.mm_per_h[1, 1, 2] == pytest.approx(10 * 0.12)
    assert rd.observed_until == rd.times[0]


def test_parse_recorded_sample():
    sample = Path(__file__).parent / "data" / "radar_sample.json"
    stored = json.loads(sample.read_text())
    # Requested by bbox (the sample routes plus margin), so the response has no bbox.
    bbox = tuple(int(v) for v in stored["params"]["bbox"].split(","))
    rd = parse_radar_response(stored["response"], bbox=bbox)
    assert rd.bbox == (596, 312, 608, 325)
    assert rd.mm_per_h.shape[1:] == (13, 14)
    assert rd.nowcast_end - rd.observed_until == 2 * 3600


# ---------------------------------------------------------------------------
# Departure / segment / frame lookup
# ---------------------------------------------------------------------------

T0 = 1_792_000_000 - (1_792_000_000 % FRAME_S)  # some 5-minute boundary


def _route(n_seg=4, seg_min=5.0):
    """Segments along one grid row, each `seg_min` long, at pixels (650, 320+i)."""
    segs = []
    for i in range(n_seg):
        # Find the lat/lon of pixel centre (650, 320 + i) numerically.
        lat, lon = _pixel_to_latlon(650, 320 + i)
        segs.append(Segment(i, lat, lon, i, i + 1, i * seg_min * 60, (i + 1) * seg_min * 60, (i + 0.5) * seg_min * 60))
    lat = np.array([s.lat for s in segs])
    lon = np.array([s.lon for s in segs])
    return Route("test", "distance", tuple(segs), n_seg, n_seg * seg_min * 60, lat, lon)


def _pixel_to_latlon(row, col):
    lat, lon = 50.5, 7.0
    for _ in range(50):  # Newton-ish fixed point, the grid is ~1 km / 0.009 deg
        r, c = latlon_to_grid(lat, lon)
        lat -= (row - r) * 0.009
        lon += (col - c) * 0.0141
    return float(lat), float(lon)


def _encoded_radar(n_frames=12):
    """value = frame index * 1000 + local column, so the lookup is decodable."""
    bbox = (645, 315, 655, 330)
    rows, cols = 655 - 645 + 1, 330 - 315 + 1
    f = np.arange(n_frames)[:, None, None] * 1000 + np.arange(cols)[None, None, :] + np.zeros((1, rows, 1))
    times = T0 + FRAME_S * np.arange(n_frames, dtype=np.int64)
    return RadarData(times, f.astype(np.float32), bbox, observed_until=int(times[0]))


def test_frame_index_interval_semantics():
    rd = _encoded_radar()
    # Frame k covers (T_k - 5 min, T_k].
    assert rd.frame_index(T0) == 0
    assert rd.frame_index(T0 + 1) == 1
    assert rd.frame_index(T0 + 300) == 1
    assert rd.frame_index(T0 - 299) == 0
    assert rd.frame_index(T0 - 300) == -1
    assert rd.frame_index(rd.times[-1] + 1) == -1


def test_matrix_lookup_picks_right_frame_and_pixel():
    rd = _encoded_radar(n_frames=12)
    route = _route()
    deps = np.array([T0, T0 + 600], dtype=np.int64)
    m = compute_rain_matrix(route, rd, deps)
    for j, dep in enumerate(deps):
        for i, s in enumerate(route.segments):
            frame = int(np.ceil((dep + s.mid_offset_s - T0) / FRAME_S))
            col = 320 + i - 315
            assert m.intensity[i, j] == frame * 1000 + col


def test_matrix_masks_beyond_nowcast():
    rd = _encoded_radar(n_frames=6)  # nowcast ends at T0 + 25 min
    route = _route(n_seg=4)  # 20-min ride, midpoints at 2.5, 7.5, 12.5, 17.5 min
    deps = departure_times(rd, step_min=5)
    m = compute_rain_matrix(route, rd, deps)
    assert list(deps - T0) == [0, 300, 600, 900, 1200, 1500]
    assert m.complete.tolist() == [True, True, False, False, False, False]
    # dep +10 min: segments at +12.5, +17.5 inside; +22.5, +27.5 -> frame 5 ok, frame 6 missing
    assert np.isnan(m.intensity[:, 2]).tolist() == [False, False, False, True]
    assert np.isnan(m.total_mm[2])


def test_rain_mm_is_intensity_times_duration():
    rd = _encoded_radar()
    rd.mm_per_h[:] = 6.0  # 6 mm/h everywhere
    m = compute_rain_matrix(_route(n_seg=4, seg_min=5), rd, np.array([T0]))
    assert m.total_mm[0] == pytest.approx(6.0 * 20 / 60)


def test_synthetic_band_known_answer():
    """A narrow band parked over segment 2 from T0 + 15 min onward: leaving at
    T0 is dry, leaving later gets wet exactly on segment 2."""
    route = _route(n_seg=4)
    s2 = route.segments[2]
    band = RainBand(s2.lat, s2.lon, t0=T0, heading_deg=90, speed_kmh=0, width_km=0.6, peak_mm_per_h=10)
    rd = synthetic_radar((645, 315, 655, 330), observed_until=T0, bands=[band])
    rd.mm_per_h[rd.times <= T0 + 15 * 60] = 0  # band appears after T0 + 15 min
    m = compute_rain_matrix(route, rd, departure_times(rd))
    wet = m.intensity > 1
    assert not wet[:, 0].any()  # segment 2 reached at +12.5 min: still dry
    assert wet[2, 1:][m.complete[1:]].all()  # every later full ride gets wet on segment 2
    assert not wet[[0, 1, 3]].any()
    best = best_departure(m, tz="UTC")
    assert best is not None and best.total_mm == 0
    assert int(best.time.timestamp()) == T0


def _matrix_with_totals(peaks):
    """One-segment, 10-min route; departure k has constant rain peaks[k]."""
    rd = _encoded_radar(n_frames=len(peaks) + 4)
    route = _route(n_seg=1, seg_min=10)
    deps = T0 + 300 * np.arange(len(peaks), dtype=np.int64)
    rd.mm_per_h[:] = 0
    for k, p in enumerate(peaks):
        # segment midpoint at +5 min -> frame k + 1
        rd.mm_per_h[k + 1] = p
    return compute_rain_matrix(route, rd, deps)


def test_best_departure_respects_peak_cap():
    # Totals rank: 0.12 < 0.24 < 0.36 < 2.0 ; cap 0.3 excludes 0.36 and 2.0.
    m = _matrix_with_totals([2.0, 0.36, 0.24, 0.12])
    assert best_departure(m, "UTC").peak_mm_per_h == pytest.approx(0.12)
    m = _matrix_with_totals([2.0, 0.36, 0.48])
    assert best_departure(m, "UTC", max_mm_per_h=0.3) is None
    assert best_departure(m, "UTC").peak_mm_per_h == pytest.approx(0.36)
    m = _matrix_with_totals([0.36, 0.24, 2.0])
    best = best_departure(m, "UTC", max_mm_per_h=0.3)
    assert int(best.time.timestamp()) == T0 + 300


def test_rolling_window():
    from datetime import datetime, time, timedelta, timezone

    from commute_rain.config import CommuteWindow

    w = CommuteWindow(end=time(20, 0), max_ahead_min=120)
    now = datetime(2026, 10, 26, 17, 0, tzinfo=timezone.utc)
    assert w.contains(now, now)
    assert w.contains(now + timedelta(minutes=120), now)
    assert not w.contains(now + timedelta(minutes=125), now)
    late = datetime(2026, 10, 26, 19, 0, tzinfo=timezone.utc)
    assert w.contains(late + timedelta(minutes=60), late)
    assert not w.contains(late + timedelta(minutes=65), late)  # after 20:00
    fixed = CommuteWindow(time(7, 0), time(9, 30))
    assert fixed.contains(datetime(2026, 10, 26, 9, 30, tzinfo=timezone.utc), now)
    assert not fixed.contains(datetime(2026, 10, 26, 9, 35, tzinfo=timezone.utc), now)


def test_best_departure_window_uses_matrix_now():
    from datetime import datetime, timezone

    from commute_rain.config import CommuteWindow

    m = _matrix_with_totals([0.24, 0.24, 0.12, 0.0])  # cheapest is the last
    start = datetime.fromtimestamp(T0, timezone.utc).time()
    w = CommuteWindow(start=start, max_ahead_min=5)  # only first two departures
    best = best_departure(m, "UTC", window=w)
    assert int(best.time.timestamp()) == T0


def test_cluster_bboxes():
    from commute_rain.radar import cluster_bboxes

    near = [(630, 320, 650, 335), (640, 330, 660, 345)]
    far = (100, 900, 120, 920)
    clusters = cluster_bboxes(near + [far], max_size_px=150)
    assert clusters == [((630, 320, 660, 345), [0, 1]), (far, [2])]


def test_radar_needed():
    from datetime import datetime, time
    from zoneinfo import ZoneInfo

    from commute_rain.config import CommuteWindow
    from commute_rain.radar import radar_needed

    tz = ZoneInfo("Europe/Berlin")
    w = [CommuteWindow(time(7, 0), time(9, 30)), CommuteWindow(time(15, 0), time(20, 0), max_ahead_min=120)]
    at = lambda h, m=0: datetime(2026, 10, 26, h, m, tzinfo=tz)  # noqa: E731
    assert not radar_needed(w, at(4, 59))
    assert radar_needed(w, at(5, 0))
    assert radar_needed(w, at(9, 30))
    assert not radar_needed(w, at(10, 0))
    assert radar_needed(w, at(13, 0))
    assert not radar_needed(w, at(20, 1))


def test_forecast_fills_cells_beyond_nowcast():
    from commute_rain.forecast import HourlyForecast

    rd = _encoded_radar(n_frames=6)  # nowcast ends at T0 + 25 min
    route = _route(n_seg=4)
    deps = T0 + 300 * np.arange(6, dtype=np.int64)
    fc = HourlyForecast(np.array([T0 + 3600 * k for k in range(1, 4)]), np.array([0.5, 0.5, 0.5]), np.full(3, 50.0), ())
    m = compute_rain_matrix(route, rd, deps, forecast=fc)
    radar_only = compute_rain_matrix(route, rd, deps)
    assert m.complete.all()
    assert (m.from_forecast == np.isnan(radar_only.intensity)).all()
    assert (m.intensity[m.from_forecast] == 0.5).all()
    assert m.uses_forecast.tolist() == [False, False, True, True, True, True]
    # Without radar, everything comes from the forecast.
    fc_only = compute_rain_matrix(route, None, deps, forecast=fc, now=T0)
    assert fc_only.from_forecast.all() and fc_only.now == T0


def test_window_departures():
    from datetime import datetime, time
    from zoneinfo import ZoneInfo

    from commute_rain.config import CommuteWindow
    from commute_rain.radar import window_departures

    tz = ZoneInfo("Europe/Berlin")
    w = CommuteWindow(time(7, 0), time(9, 30))
    at = lambda d, h, m=0: datetime(2026, 10, d, h, m, tzinfo=tz)  # noqa: E731
    local = lambda deps: [datetime.fromtimestamp(int(t), tz) for t in deps]  # noqa: E731
    before = local(window_departures(w, at(26, 5, 0)))
    assert (before[0], before[-1], len(before)) == (at(26, 7), at(26, 9, 30), 31)
    during = local(window_departures(w, at(26, 8, 2)))
    assert during[0] == at(26, 8, 5)
    after = local(window_departures(w, at(26, 9, 31)))
    assert (after[0], after[-1]) == (at(27, 7), at(27, 9, 30))
