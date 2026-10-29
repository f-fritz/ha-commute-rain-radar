import json
from datetime import date, datetime, time, timezone
from pathlib import Path

import numpy as np
import pytest

from commute_rain.config import CommuteWindow
from commute_rain.forecast import HourlyForecast, combine_max, parse_weather_response, ride_forecast, window_forecasts
from commute_rain.route import Route

H = 3600
T0 = int(datetime(2026, 10, 26, 6, 0, tzinfo=timezone.utc).timestamp())  # 07:00 CET


def _fc(mm, prob=None, start=T0):
    """Hourly forecast whose first hour is [start, start + 1h)."""
    n = len(mm)
    return HourlyForecast(
        times=start + H * np.arange(1, n + 1, dtype=np.int64),
        precipitation_mm=np.array(mm, float),
        probability=np.array(prob if prob is not None else [50] * n, float),
        stations=("TEST",),
    )


def _route(duration_s):
    return Route("r", "distance", (), 10.0, duration_s, np.array([50.0]), np.array([7.0]))


def test_parse_recorded_sample_drops_observations():
    stored = json.loads((Path(__file__).parent / "data" / "weather_sample.json").read_text())
    data = stored["response"]
    fc = parse_weather_response(data)
    assert len(fc.times) == 49
    assert np.all(np.diff(fc.times) == H)
    assert fc.stations == ("KOELN (BOTAN.GART.)",)
    assert np.nanmax(fc.probability) <= 100

    # The API puts the current observation first when there is one; it must
    # not end up in the forecast.
    first = data["weather"][0]
    obs_source = {**data["sources"][0], "id": -1, "observation_type": "current", "station_name": "OBS"}
    with_obs = {
        "weather": [{**first, "source_id": -1, "precipitation": 9.9}] + data["weather"],
        "sources": data["sources"] + [obs_source],
    }
    fc_obs = parse_weather_response(with_obs)
    assert np.array_equal(fc_obs.times, fc.times)
    assert np.array_equal(fc_obs.precipitation_mm, fc.precipitation_mm, equal_nan=True)
    assert fc_obs.stations == fc.stations


def test_ride_spanning_two_hours_weights_by_overlap():
    fc = _fc([2.0, 4.0, 0.0])
    # 40-min ride from 07:30: 30 min in hour 1 (2 mm/h), 10 min in hour 2 (4 mm/h).
    r = ride_forecast(fc, T0 + 1800, 2400, "UTC")
    assert r.expected_mm == pytest.approx(2.0 * 0.5 + 4.0 * 10 / 60)
    assert r.peak_mm_per_h == 4.0


def test_ride_outside_forecast_is_none():
    fc = _fc([1.0, 1.0])
    assert ride_forecast(fc, T0 - 60, 600) is None
    assert ride_forecast(fc, T0 + 2 * H - 300, 600) is None


def test_probability_is_max_of_touched_hours():
    fc = _fc([0, 0, 0], prob=[10, 80, 30])
    assert ride_forecast(fc, T0, 1800).max_probability == 10
    assert ride_forecast(fc, T0 + 1800, 3600).max_probability == 80


def test_window_forecasts_best_and_cap():
    # 07:00-09:30 CET window, 30-min ride. Hours 07-08 wet, 08-09 drizzle, 09-11 dry.
    fc = _fc([3.0, 0.2, 0.0, 0.0])
    wf = window_forecasts(fc, _route(1800), CommuteWindow(time(7, 0), time(9, 30)), step_min=30, max_mm_per_h=0.3)
    assert len(wf) == 1 and wf[0].day == date(2026, 10, 26)
    assert len(wf[0].rides) == 6
    assert wf[0].best.departure.strftime("%H:%M") == "09:00"
    assert wf[0].max_mm == pytest.approx(1.5)


def test_partly_covered_window_is_marked_incomplete():
    fc = _fc([0.0, 0.0])  # 07:00-09:00 only
    wf = window_forecasts(fc, _route(1800), CommuteWindow(time(7, 0), time(9, 30)), step_min=30)
    assert len(wf) == 1 and not wf[0].complete
    assert [r.departure.strftime("%H:%M") for r in wf[0].rides] == ["07:00", "07:30", "08:00", "08:30"]
    full = window_forecasts(_fc([0.0] * 3), _route(1800), CommuteWindow(time(7, 0), time(9, 30)))
    assert full[0].complete


def test_window_across_dst_change():
    # 25 Oct 2026: CEST -> CET. 15:00 local is 14:00 UTC that day.
    start = int(datetime(2026, 10, 25, 14, 0, tzinfo=timezone.utc).timestamp())
    fc = _fc([1.0, 0.0, 0.0, 0.0], start=start)
    wf = window_forecasts(fc, _route(1800), CommuteWindow(time(15, 0), time(16, 0)), step_min=60)
    assert [r.departure.strftime("%H:%M") for r in wf[0].rides] == ["15:00", "16:00"]
    assert [r.expected_mm for r in wf[0].rides] == [0.5, 0.0]


def test_rolling_window_rejected():
    with pytest.raises(ValueError):
        window_forecasts(_fc([0.0]), _route(60), CommuteWindow(end=time(20), max_ahead_min=120))


def test_combine_max():
    a = _fc([1.0, 0.0], prob=[10, 90])
    b = _fc([0.5, 2.0], prob=[40, 20])
    c = combine_max([a, b])
    assert c.precipitation_mm.tolist() == [1.0, 2.0]
    assert c.probability.tolist() == [40, 90]


def test_intensity_at_hour_semantics():
    from commute_rain.forecast import intensity_at

    fc = _fc([1.0, 2.0])  # hours (T0, T0+1h] and (T0+1h, T0+2h]
    got = intensity_at(fc, [T0, T0 + 1, T0 + H, T0 + H + 1, T0 + 2 * H, T0 + 2 * H + 1])
    assert np.isnan(got[0]) and np.isnan(got[-1])
    assert got[1:5].tolist() == [1.0, 1.0, 2.0, 2.0]
