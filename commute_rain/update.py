"""One update cycle for all routes: fetch once, compute per route.

This is the shape of the later Home Assistant update coordinator.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .brightsky_client import BrightSkyClient
from .config import Config
from .forecast import HourlyForecast, combine_max, parse_weather_response
from .radar import RadarData, RainMatrix, bbox_for_routes, compute_rain_matrix, departure_times, parse_radar_response
from .route import Route, build_route, load_gpx


def load_routes(cfg: Config) -> list[Route]:
    return [build_route(rc, load_gpx(rc.gpx_path)) for rc in cfg.routes]


def fetch_radar(client: BrightSkyClient, routes: list[Route], cfg: Config, now: datetime | None = None) -> RadarData:
    """Radar for the combined bounding box of all routes, from shortly before
    now to the end of the nowcast."""
    now = now or datetime.now(timezone.utc)
    bbox = bbox_for_routes(routes, cfg.radar_margin_km)
    raw = client.get_radar(bbox, now - timedelta(minutes=15), now + timedelta(hours=3))
    return parse_radar_response(raw, bbox)


def radar_matrices(routes: list[Route], radar: RadarData, cfg: Config) -> dict[str, RainMatrix]:
    deps = departure_times(radar, cfg.departure_step_min)
    return {r.name: compute_rain_matrix(r, radar, deps) for r in routes}


def forecast_points(routes: list[Route], mode: str = "center") -> list[tuple[float, float]]:
    """Points to fetch the forecast for, rounded so nearby points dedupe."""
    if mode == "center":
        lat = [x for r in routes for x in (r.bounds[0], r.bounds[2])]
        lon = [x for r in routes for x in (r.bounds[1], r.bounds[3])]
        pts = [((min(lat) + max(lat)) / 2, (min(lon) + max(lon)) / 2)]
    else:
        pts = []
        for r in routes:
            mid = r.segments[len(r.segments) // 2]
            pts += [(r.path_lat[0], r.path_lon[0]), (mid.lat, mid.lon), (r.path_lat[-1], r.path_lon[-1])]
    return sorted({(round(float(a), 2), round(float(b), 2)) for a, b in pts})


def fetch_forecast(client: BrightSkyClient, routes: list[Route], cfg: Config, now: datetime | None = None) -> HourlyForecast:
    """Hourly forecast from the current hour for `cfg.forecast_hours`."""
    now = (now or datetime.now(timezone.utc)).replace(minute=0, second=0, microsecond=0)
    last = now + timedelta(hours=cfg.forecast_hours)
    fcs = [
        parse_weather_response(client.get_weather(lat, lon, now, last))
        for lat, lon in forecast_points(routes, cfg.forecast_points)
    ]
    return fcs[0] if len(fcs) == 1 else combine_max(fcs)
