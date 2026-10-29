"""Sensors per commute leg: radar nowcast (2 h) and forecast (48 h)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, UnitOfPrecipitationDepth
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import CommuteRainConfigEntry, ForecastCoordinator, RadarCoordinator, max_rain
from .core.radar import Departure, RainMatrix, best_departure, rain_if_leaving_now
from .entity import CommuteRainEntity
from .models import Leg

RADAR_KEYS = ("rain_now", "best_departure", "best_departure_rain")
FORECAST_KEYS = ("forecast_rain", "forecast_best_departure", "forecast_probability")


async def async_setup_entry(
    hass: HomeAssistant, entry: CommuteRainConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    data = entry.runtime_data
    for commute_id in entry.subentries:
        legs = [leg for leg in data.legs if leg.commute_id == commute_id]
        entities: list[SensorEntity] = []
        for leg in legs:
            entities += [RadarSensor(data.radar, leg, key) for key in RADAR_KEYS]
            entities += [ForecastSensor(data.forecast, leg, key) for key in FORECAST_KEYS]
        async_add_entities(entities, config_subentry_id=commute_id)


def _mm(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def _pct(value: float) -> int | None:
    return None if value != value else round(value)  # NaN -> None


def _departure_attrs(d: Departure | None) -> dict[str, Any]:
    if d is None:
        return {}
    return {"departure": d.time.isoformat(), "total_mm": _mm(d.total_mm), "peak_mm_per_h": _mm(d.peak_mm_per_h)}


class RadarSensor(CommuteRainEntity, SensorEntity):
    coordinator: RadarCoordinator
    _unrecorded_attributes = frozenset({"departures"})

    def __init__(self, coordinator: RadarCoordinator, leg: Leg, key: str) -> None:
        super().__init__(coordinator, leg, key)
        if key == "best_departure":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
        else:
            self._attr_device_class = SensorDeviceClass.PRECIPITATION
            self._attr_native_unit_of_measurement = UnitOfPrecipitationDepth.MILLIMETERS
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_suggested_display_precision = 2

    @property
    def _matrix(self) -> RainMatrix | None:
        data = self.coordinator.data
        return data.matrices.get(self.leg.id) if data and data.active else None

    def _best(self, m: RainMatrix, capped: bool = True) -> Departure | None:
        cap = max_rain(self.coordinator.config_entry) if capped else None
        return best_departure(m, self.coordinator.tz, self.leg.radar_window, cap)

    @property
    def native_value(self):
        m = self._matrix
        if m is None:
            return None
        key = self._attr_translation_key
        if key == "rain_now":
            now = rain_if_leaving_now(m, self.coordinator.tz)
            return _mm(now.total_mm) if now else None
        best = self._best(m)
        if best is None:
            return None
        return best.time if key == "best_departure" else _mm(best.total_mm)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        attrs: dict[str, Any] = {
            "radar_active": bool(data and data.active),
            "demo": bool(data and data.demo),
            "observed_until": data.observed_until.isoformat() if data and data.observed_until else None,
            "max_rain_mm_per_h": max_rain(self.coordinator.config_entry),
        }
        m = self._matrix
        if m is None:
            return attrs
        key = self._attr_translation_key
        if key == "rain_now":
            attrs |= _departure_attrs(rain_if_leaving_now(m, self.coordinator.tz))
            attrs["departures"] = [
                {
                    "time": t.isoformat(),
                    "total_mm": _mm(float(m.total_mm[i])) if m.complete[i] else None,
                    "partial_mm": _mm(float(m.partial_mm[i])),
                    "peak_mm_per_h": _mm(float(m.peak_mm_per_h[i])),
                    "complete": bool(m.complete[i]),
                }
                for i, t in enumerate(m.departure_datetimes(self.coordinator.tz))
            ]
        else:
            attrs |= _departure_attrs(self._best(m))
            least = self._best(m, capped=False)
            attrs["least_rain"] = _departure_attrs(least) or None
        return attrs


class ForecastSensor(CommuteRainEntity, SensorEntity):
    coordinator: ForecastCoordinator
    _unrecorded_attributes = frozenset({"rides"})

    def __init__(self, coordinator: ForecastCoordinator, leg: Leg, key: str) -> None:
        super().__init__(coordinator, leg, key)
        if key == "forecast_best_departure":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
        elif key == "forecast_probability":
            self._attr_native_unit_of_measurement = PERCENTAGE
            self._attr_state_class = SensorStateClass.MEASUREMENT
        else:
            self._attr_device_class = SensorDeviceClass.PRECIPITATION
            self._attr_native_unit_of_measurement = UnitOfPrecipitationDepth.MILLIMETERS
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_suggested_display_precision = 2

    @property
    def _window(self):
        data = self.coordinator.data
        return data.next_window.get(self.leg.id) if data else None

    @property
    def native_value(self):
        w = self._window
        if w is None:
            return None
        key = self._attr_translation_key
        if key == "forecast_probability":
            return _pct(w.max_probability)
        if key == "forecast_best_departure":
            return w.best.departure if w.best else None
        # Rain at the best departure; without a qualifying one, the least rain.
        return _mm(w.best.expected_mm if w.best else w.min_mm)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        w = self._window
        if w is None:
            return {}
        fc = self.coordinator.data.forecasts[self.leg.commute_id]
        attrs: dict[str, Any] = {
            "day": w.day.isoformat(),
            "window_start": w.window.start.isoformat(),
            "window_end": w.window.end.isoformat(),
            "complete": w.complete,
            "min_mm": _mm(w.min_mm),
            "max_mm": _mm(w.max_mm),
            "max_probability": _pct(w.max_probability),
            "best_departure": w.best.departure.isoformat() if w.best else None,
            "station": ", ".join(fc.stations),
        }
        if self._attr_translation_key == "forecast_rain":
            attrs["rides"] = [
                {
                    "time": r.departure.isoformat(),
                    "expected_mm": _mm(r.expected_mm),
                    "peak_mm_per_h": _mm(r.peak_mm_per_h),
                    "probability": _pct(r.max_probability),
                }
                for r in w.rides
            ]
        return attrs
