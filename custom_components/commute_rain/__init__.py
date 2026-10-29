"""Commute Rain: rain along your cycling commute per departure time."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import BrightSkyApi
from .coordinator import CommuteRainConfigEntry, CommuteRainData, ForecastCoordinator, RadarCoordinator
from .models import legs_from_entry

PLATFORMS = [Platform.BINARY_SENSOR, Platform.IMAGE, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: CommuteRainConfigEntry) -> bool:
    legs = await hass.async_add_executor_job(legs_from_entry, entry)
    api = BrightSkyApi(async_get_clientsession(hass))
    radar = RadarCoordinator(hass, entry, legs, api)
    forecast = ForecastCoordinator(hass, entry, legs, api)
    await radar.async_config_entry_first_refresh()
    await forecast.async_config_entry_first_refresh()
    entry.runtime_data = CommuteRainData(legs, radar, forecast)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Adding/removing a commute or changing options -> rebuild everything.
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: CommuteRainConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: CommuteRainConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
