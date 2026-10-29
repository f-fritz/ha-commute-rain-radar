"""Binary sensor per leg: is there a departure that stays within the rain limit?"""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import CommuteRainConfigEntry, RadarCoordinator, max_rain
from .core.radar import best_departure
from .entity import CommuteRainEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: CommuteRainConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    data = entry.runtime_data
    for commute_id in entry.subentries:
        async_add_entities(
            [RideWithinLimit(data.radar, leg, "ride_within_limit") for leg in data.legs if leg.commute_id == commute_id],
            config_subentry_id=commute_id,
        )


class RideWithinLimit(CommuteRainEntity, BinarySensorEntity):
    """On if the radar finds a departure in the window that never exceeds the
    rain limit; unknown while the radar is idle."""

    coordinator: RadarCoordinator

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        m = data.matrices.get(self.leg.id) if data and data.active else None
        if m is None:
            return None
        cap = max_rain(self.coordinator.config_entry)
        return best_departure(m, self.coordinator.tz, self.leg.radar_window, cap) is not None
