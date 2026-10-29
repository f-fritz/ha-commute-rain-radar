"""Shared update coordinators: one radar and one forecast fetch for all commutes."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import aiohttp
import numpy as np

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import BrightSkyApi
from .const import (
    CONF_ALWAYS_POLL,
    CONF_DEMO_MODE,
    CONF_MAX_RAIN,
    DEFAULT_MAX_RAIN,
    DEMO_SYNTHETIC,
    DOMAIN,
    FORECAST_HOURS,
    FORECAST_INTERVAL,
    FORECAST_MIN_AGE,
    FORECAST_STEP_MIN,
    RADAR_INTERVAL,
    RADAR_MARGIN_KM,
    RADAR_MAX_BBOX_PX,
    RADAR_MIN_AGE,
    RADAR_STEP_MIN,
)
from .core.forecast import HourlyForecast, WindowForecast, parse_weather_response, window_forecasts
from .core.radar import (
    RadarData,
    RainMatrix,
    bbox_for_routes,
    cluster_bboxes,
    compute_rain_matrix,
    departure_times,
    parse_radar_response,
    radar_needed,
)
from .core.synthetic import RainBand, synthetic_radar
from .models import Leg

_LOGGER = logging.getLogger(__name__)

type CommuteRainConfigEntry = ConfigEntry[CommuteRainData]


@dataclass
class CommuteRainData:
    legs: list[Leg]
    radar: RadarCoordinator
    forecast: ForecastCoordinator


def max_rain(entry: ConfigEntry) -> float:
    return float(entry.options.get(CONF_MAX_RAIN, DEFAULT_MAX_RAIN))


# ---------------------------------------------------------------------------
# Radar
# ---------------------------------------------------------------------------


@dataclass
class RadarResult:
    active: bool  # False = idle outside the commute windows, nothing fetched
    matrices: dict[str, RainMatrix] = field(default_factory=dict)  # by leg id
    radars: dict[str, RadarData] = field(default_factory=dict)  # by leg id, shared per cluster
    observed_until: datetime | None = None
    demo: bool = False


def demo_radar(legs: list[Leg], bbox, now: datetime) -> RadarData:
    """Synthetic front + shower crossing the routes, repeating every 90 min."""
    lat = np.concatenate([leg.route.path_lat for leg in legs])
    lon = np.concatenate([leg.route.path_lon for leg in legs])
    observed = int(now.timestamp()) // 300 * 300
    cycle = observed // 5400 * 5400
    centre = dict(lat=float(lat.mean()), lon=float(lon.mean()), heading_deg=60, speed_kmh=20)
    bands = [
        RainBand(**centre, t0=cycle + 2700, width_km=5, peak_mm_per_h=7),
        RainBand(**centre, t0=cycle + 2700 + 3600, width_km=2, peak_mm_per_h=14),
    ]
    return synthetic_radar(bbox, observed, bands)


class RadarCoordinator(DataUpdateCoordinator[RadarResult]):
    """Radar nowcast every 5 min, only around the commute windows."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, legs: list[Leg], api: BrightSkyApi) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} radar", update_interval=RADAR_INTERVAL)
        self.legs = legs
        self.api = api
        self._last_fetch: datetime | None = None
        # One request per cluster of nearby commutes.
        bboxes = [bbox_for_routes([leg.route], RADAR_MARGIN_KM) for leg in legs]
        self.clusters = cluster_bboxes(bboxes, RADAR_MAX_BBOX_PX)

    @property
    def tz(self) -> str:
        return self.hass.config.time_zone

    async def _async_update_data(self) -> RadarResult:
        if not self.legs:
            return RadarResult(active=False)
        now = dt_util.now()
        demo = self.config_entry.options.get(CONF_DEMO_MODE) == DEMO_SYNTHETIC
        always = self.config_entry.options.get(CONF_ALWAYS_POLL, False)
        if not (demo or always or radar_needed([leg.radar_poll_window for leg in self.legs], now)):
            return RadarResult(active=False)
        if not demo and self.data and self.data.active and self._last_fetch and now - self._last_fetch < RADAR_MIN_AGE:
            return self.data  # manual refresh right after a fetch: be polite

        matrices: dict[str, RainMatrix] = {}
        radars: dict[str, RadarData] = {}
        observed = []
        for bbox, members in self.clusters:
            legs = [self.legs[i] for i in members]
            if demo:
                radar = demo_radar(legs, bbox, now)
            else:
                try:
                    raw = await self.api.radar(bbox, now - timedelta(minutes=15), now + timedelta(hours=3))
                except (aiohttp.ClientError, TimeoutError) as err:
                    raise UpdateFailed(f"radar request failed: {err}") from err
                radar = await self.hass.async_add_executor_job(parse_radar_response, raw, bbox)
            # Radar lags real time by a few minutes; never suggest past departures.
            deps = departure_times(radar, RADAR_STEP_MIN, start=max(radar.observed_until, int(now.timestamp())))
            for leg in legs:
                matrices[leg.id] = compute_rain_matrix(leg.route, radar, deps)
                radars[leg.id] = radar
            observed.append(radar.observed_until)
        if not demo:
            self._last_fetch = now
        return RadarResult(
            active=True,
            matrices=matrices,
            radars=radars,
            observed_until=dt_util.utc_from_timestamp(min(observed)),
            demo=demo,
        )


# ---------------------------------------------------------------------------
# Forecast
# ---------------------------------------------------------------------------


@dataclass
class ForecastResult:
    forecasts: dict[str, HourlyForecast]  # by commute id
    next_window: dict[str, WindowForecast | None]  # by leg id
    windows: dict[str, list[WindowForecast]]  # by leg id, all days


def forecast_point(legs: list[Leg]) -> tuple[float, float]:
    """Centre of a commute, rounded so nearby commutes share one request."""
    lat = np.concatenate([leg.route.path_lat for leg in legs])
    lon = np.concatenate([leg.route.path_lon for leg in legs])
    return round(float(lat.min() + lat.max()) / 2 / 0.05) * 0.05, round(float(lon.min() + lon.max()) / 2 / 0.05) * 0.05


def next_window(windows: list[WindowForecast], now: datetime) -> WindowForecast | None:
    for w in windows:
        if datetime.combine(w.day, w.window.end, now.tzinfo) >= now:
            return w
    return None


class ForecastCoordinator(DataUpdateCoordinator[ForecastResult]):
    """Hourly 48 h forecast; one request per distinct (rounded) location."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, legs: list[Leg], api: BrightSkyApi) -> None:
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} forecast", update_interval=FORECAST_INTERVAL
        )
        self.legs = legs
        self.api = api
        self._raw: dict[tuple[float, float], dict] = {}
        self._last_fetch: datetime | None = None

    async def _async_update_data(self) -> ForecastResult:
        now = dt_util.now()
        by_commute: dict[str, list[Leg]] = {}
        for leg in self.legs:
            by_commute.setdefault(leg.commute_id, []).append(leg)
        points = {cid: forecast_point(legs) for cid, legs in by_commute.items()}

        if not (self._last_fetch and now - self._last_fetch < FORECAST_MIN_AGE and set(points.values()) <= set(self._raw)):
            start = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
            raw = {}
            for lat, lon in sorted(set(points.values())):
                try:
                    raw[(lat, lon)] = await self.api.weather(lat, lon, start, start + timedelta(hours=FORECAST_HOURS))
                except (aiohttp.ClientError, TimeoutError) as err:
                    raise UpdateFailed(f"forecast request failed: {err}") from err
            self._raw, self._last_fetch = raw, now

        forecasts = {cid: parse_weather_response(self._raw[p]) for cid, p in points.items()}
        windows = {
            leg.id: window_forecasts(
                forecasts[leg.commute_id], leg.route, leg.forecast_window,
                self.hass.config.time_zone, FORECAST_STEP_MIN, max_rain(self.config_entry),
            )
            for leg in self.legs
        }
        return ForecastResult(
            forecasts=forecasts,
            next_window={lid: next_window(ws, now) for lid, ws in windows.items()},
            windows=windows,
        )
