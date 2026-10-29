"""Heatmap image per leg (departure x segment), rendered with matplotlib.

The x-axis is the leg's departure window (today's while it is open, else
tomorrow's). Cells come from the radar where the nowcast reaches and from the
hourly forecast beyond it, so the radar fills in from the left as the window
approaches.
"""

from __future__ import annotations

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import RADAR_STEP_MIN
from .coordinator import CommuteRainConfigEntry, ForecastCoordinator, RadarCoordinator, max_rain
from .core.plots import render_rain_matrix_png
from .core.radar import compute_rain_matrix, window_departures
from .entity import CommuteRainEntity
from .models import Leg


async def async_setup_entry(
    hass: HomeAssistant, entry: CommuteRainConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    data = entry.runtime_data
    for commute_id in entry.subentries:
        async_add_entities(
            [RainHeatmap(hass, data.radar, data.forecast, leg) for leg in data.legs if leg.commute_id == commute_id],
            config_subentry_id=commute_id,
        )


class RainHeatmap(CommuteRainEntity, ImageEntity):
    """Rendered lazily: only when someone looks at it, once per update."""

    coordinator: RadarCoordinator
    _attr_content_type = "image/png"

    def __init__(
        self, hass: HomeAssistant, coordinator: RadarCoordinator, forecast: ForecastCoordinator, leg: Leg
    ) -> None:
        CommuteRainEntity.__init__(self, coordinator, leg, "heatmap")
        ImageEntity.__init__(self, hass)
        self.forecast = forecast
        self._png: bytes | None = None
        self._attr_image_last_updated = dt_util.utcnow()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.forecast.async_add_listener(self._handle_coordinator_update))

    @callback
    def _handle_coordinator_update(self) -> None:
        self._png = None
        self._attr_image_last_updated = dt_util.utcnow()
        super()._handle_coordinator_update()

    def _render(self) -> bytes | None:
        rd, fd = self.coordinator.data, self.forecast.data
        radar = rd.radars.get(self.leg.id) if rd and rd.active else None
        fc = fd.forecasts.get(self.leg.commute_id) if fd else None
        if radar is None and fc is None:
            return None
        now = dt_util.now()
        deps = window_departures(self.leg.departure_window, now, RADAR_STEP_MIN)
        m = compute_rain_matrix(self.leg.route, radar, deps, forecast=fc, now=int(now.timestamp()))
        # The x-axis already is the window, so no greying; best = least rain on it.
        return render_rain_matrix_png(m, self.coordinator.tz, None, max_rain(self.coordinator.config_entry))

    async def async_image(self) -> bytes | None:
        if self._png is None:
            self._png = await self.hass.async_add_executor_job(self._render)
        return self._png
