"""Base entity: one device per commute, entities per leg."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .const import ATTRIBUTION, DOMAIN
from .models import Leg

LEG_LABELS = {"outbound": "outbound", "return": "return"}


class CommuteRainEntity(CoordinatorEntity):
    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True

    def __init__(self, coordinator: DataUpdateCoordinator, leg: Leg, key: str) -> None:
        super().__init__(coordinator)
        self.leg = leg
        self._attr_translation_key = key
        self._attr_translation_placeholders = {"leg": LEG_LABELS[leg.key]}
        self._attr_unique_id = f"{leg.id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, leg.commute_id)},
            name=leg.commute_name,
            manufacturer="Bright Sky / DWD",
            model="Commute",
            entry_type=DeviceEntryType.SERVICE,
        )
