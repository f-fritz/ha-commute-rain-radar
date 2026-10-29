"""Generate demo.ipynb (without outputs)."""

import nbformat as nbf

md, code = nbf.v4.new_markdown_cell, nbf.v4.new_code_cell
cells = [
    md("""# Commute rain radar: demo

For each possible departure time, how much rain would I ride through?

1. Load the routes from GPX and split them into segments.
2. **Live radar:** fetch the radar nowcast (about 2 h ahead) once for all routes and plot the departure × segment matrix.
3. **Synthetic radar:** the same plots against a known rain front, for dry days.
4. **Forecast:** a 48 h hourly point forecast with the expected rain for each commute window.

Colour classes are fixed, so plots are comparable between days. Hatched cells are beyond the end of the nowcast (no data, *not* dry)."""),
    code("""from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib.pyplot as plt

from commute_rain.brightsky_client import BrightSkyClient
from commute_rain.config import CommuteWindow, Config, RouteConfig
from commute_rain.forecast import window_forecasts
from commute_rain.plots import plot_forecast, plot_rain_matrix, plot_route_on_radar
from commute_rain.radar import best_departure, bbox_for_routes, compute_rain_matrix, departure_times, rain_if_leaving_now
from commute_rain.synthetic import RainBand, synthetic_radar
from commute_rain.update import fetch_forecast, fetch_radar, load_routes, radar_matrices

%matplotlib inline
plt.rcParams["figure.dpi"] = 100"""),
    md("""## Config

Two routes, each with its own GPX and speed profile, and two departure windows: one for the radar nowcast and one for the 48 h forecast.

- To work: 07:00-09:30 for both.
- Home: radar = rolling, from now up to 2 h ahead but no later than 20:00; forecast = 15:00-18:00.
- "Best departure" = least total rain among rides that never exceed `max_rain_mm_per_h` (0.3 mm/h) on any segment."""),
    code("""MORNING = CommuteWindow(time(7, 0), time(9, 30))
EVENING_RADAR = CommuteWindow(end=time(20, 0), max_ahead_min=120)
EVENING_FORECAST = CommuteWindow(time(15, 0), time(18, 0))

cfg = Config(
    routes=(
        RouteConfig("To work", Path("routes/to_work.gpx"), segment_mode="distance", segment_length_km=1.0,
                    radar_window=MORNING, forecast_window=MORNING),
        RouteConfig("Home", Path("routes/from_work.gpx"), segment_mode="time", segment_minutes=5,
                    radar_window=EVENING_RADAR, forecast_window=EVENING_FORECAST),
    ),
    cache_dir=Path("data/cache"),
)
TZ = cfg.timezone
routes = load_routes(cfg)
for r in routes:
    print(f"{r.name:8s} {r.total_km:5.1f} km  {r.total_s / 60:4.0f} min  {len(r.segments)} segments ({r.segment_mode})")"""),
    md("## Live radar\n\nOne request for the combined bounding box of all routes. Responses are stored in `data/cache/` and reused for 5 minutes. Set `MODE = \"replay\"` to rerun from the last recorded response offline."),
    code("""MODE = "live"  # or "replay"
client = BrightSkyClient(cfg.cache_dir, cfg.user_agent, mode=MODE)
radar = fetch_radar(client, routes, cfg)
local = lambda t: datetime.fromtimestamp(int(t), ZoneInfo(TZ)).strftime("%H:%M")
print(f"bbox {radar.bbox}, frames {local(radar.times[0])}-{local(radar.nowcast_end)}, "
      f"observed until {local(radar.observed_until)}, max {radar.mm_per_h.max():.2f} mm/h")"""),
    code("""live = radar_matrices(routes, radar, cfg)
for rc, r in zip(cfg.routes, routes):
    m, window = live[r.name], rc.radar_window
    now, best = rain_if_leaving_now(m, TZ), best_departure(m, TZ, window, cfg.max_rain_mm_per_h)
    print(f"{r.name}: leaving now -> " + (f"{now.total_mm:.2f} mm" if now else "ride not covered by nowcast")
          + "; best in window -> " + (f"{best.time:%H:%M} ({best.total_mm:.2f} mm)" if best else "none"))
    plot_rain_matrix(m, TZ, window, cfg.max_rain_mm_per_h)
    plt.show()"""),
    md("""## Synthetic rain front (known answer)

A 5 km wide front moving north-east at 20 km/h, followed by a narrow, heavy shower. "Now" is 07:00 on a Monday.

What to check:
- The front reaches the end of the route (the north) *earlier* in departure time than the start. A rider who leaves at time *d* reaches km *x* only at *d* + offset(*x*), so the bands run diagonally.
- The gap between the two bands is the best departure."""),
    code("""now = int(datetime(2026, 10, 26, 7, 0, tzinfo=ZoneInfo(TZ)).timestamp())
mid = routes[0].segments[len(routes[0].segments) // 2]
bands = [
    RainBand(mid.lat, mid.lon, t0=now + 25 * 60, heading_deg=60, speed_kmh=20, width_km=5, peak_mm_per_h=7),
    RainBand(mid.lat, mid.lon, t0=now + 85 * 60, heading_deg=60, speed_kmh=20, width_km=2, peak_mm_per_h=14),
]
synth = synthetic_radar(bbox_for_routes(routes, cfg.radar_margin_km), observed_until=now, bands=bands)
plot_route_on_radar(routes, synth, frame=synth.frame_index(now + 25 * 60))
plt.show()"""),
    code("""fig, axes = plt.subplots(2, 2, figsize=(17, 7), sharex=True, gridspec_kw={"height_ratios": [3, 1.3], "hspace": 0.08, "wspace": 0.12})
deps = departure_times(synth, cfg.departure_step_min)
for col, r in enumerate(routes):
    m = compute_rain_matrix(r, synth, deps)
    # Both routes against the same synthetic morning, each with its own window.
    plot_rain_matrix(m, TZ, cfg.routes[col].radar_window, cfg.max_rain_mm_per_h,
                     ax_heat=axes[0, col], ax_total=axes[1, col], legend=col == 1)
plt.show()"""),
    md("## Condensed results\n\nThese are the values that will later become Home Assistant sensors. They need no plotting."),
    code("""m = compute_rain_matrix(routes[0], synth, deps)
best = best_departure(m, TZ, MORNING, cfg.max_rain_mm_per_h)
least = best_departure(m, TZ, MORNING)
now_dep = rain_if_leaving_now(m, TZ)
print(f"leaving now ({now_dep.time:%H:%M}): {now_dep.total_mm:.2f} mm, peak {now_dep.peak_mm_per_h:.2f} mm/h")
print(f"best (peak <= {cfg.max_rain_mm_per_h} mm/h): "
      + (f"{best.time:%H:%M}: {best.total_mm:.2f} mm, peak {best.peak_mm_per_h:.2f} mm/h" if best else "none"))
print(f"least rain, ignoring the cap: {least.time:%H:%M}: {least.total_mm:.2f} mm, peak {least.peak_mm_per_h:.2f} mm/h")
print()
print("departure  total mm  peak mm/h  (distribution over departure times)")
for t, total, peak, ok in zip(m.departure_datetimes(TZ), m.total_mm, m.peak_mm_per_h, m.complete):
    if ok:
        flag = " " if peak <= cfg.max_rain_mm_per_h else "x"
        print(f"  {t:%H:%M}    {total:5.2f}    {peak:5.2f} {flag}  {'#' * int(round(total * 10))}")"""),
]
cells += [
    md("""## Task 2: 48 h forecast

Hourly MOSMIX point forecast for the centre of all routes (one request, cached for 1 h). It has no per-segment resolution, so only the ride's timing matters. For each departure in a window (every 15 min), expected rain = sum over hours of (mm/h × time riding in that hour). "Best" uses the same 0.3 mm/h limit, applied to the wettest hour the ride touches.

Precipitation and probability are drawn as two panels on one time axis rather than as bars and a line with two y-axes."""),
    code("""fc = fetch_forecast(client, routes, cfg)
windows = [
    w
    for rc, r in zip(cfg.routes, routes)
    for w in window_forecasts(fc, r, rc.forecast_window, TZ, cfg.forecast_step_min, cfg.max_rain_mm_per_h)
]
windows.sort(key=lambda w: (w.day, w.window.start))
plot_forecast(fc, windows, TZ, cfg.max_rain_mm_per_h)
plt.show()

for w in windows:
    best = (f"best {w.best.departure:%H:%M} ({w.best.expected_mm:.2f} mm)" if w.best
            else f"no departure stays ≤ {cfg.max_rain_mm_per_h} mm/h")
    print(f"{w.day:%a %d.%m}  {w.route:8s} {w.window.start:%H:%M}-{w.window.end:%H:%M}: "
          f"{w.min_mm:.2f}-{w.max_mm:.2f} mm, up to {w.max_probability:.0f}% chance; {best}")"""),
]

nb = nbf.v4.new_notebook(cells=cells, metadata={"kernelspec": {"name": "commute-rain", "display_name": "commute-rain (.venv)", "language": "python"}})
nbf.write(nb, "demo.ipynb")
