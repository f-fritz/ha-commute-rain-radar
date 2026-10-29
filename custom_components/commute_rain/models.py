"""Legs (one direction of a commute) built from config subentries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_FORECAST_END,
    CONF_FORECAST_START,
    CONF_RADAR_END,
    CONF_RADAR_MAX_AHEAD,
    CONF_RADAR_START,
    CONF_SEGMENT_MODE,
    CONF_SEGMENT_SIZE,
    CONF_TRACK,
    LEGS,
    SUBENTRY_COMMUTE,
)
from .core.config import CommuteWindow
from .core.route import Route, profile_from_track, route_from_profile


@dataclass(frozen=True)
class Leg:
    commute_id: str  # subentry id
    commute_name: str
    key: str  # "outbound" | "return"
    route: Route
    radar_window: CommuteWindow
    forecast_window: CommuteWindow

    @property
    def id(self) -> str:
        return f"{self.commute_id}_{self.key}"

    @property
    def departure_window(self) -> CommuteWindow:
        """Fixed window covering every departure of this leg. A rolling radar
        window without start begins with the forecast window."""
        return CommuteWindow(
            start=self.radar_window.start or self.forecast_window.start,
            end=self.radar_window.end or self.forecast_window.end,
        )

    @property
    def radar_poll_window(self) -> CommuteWindow:
        """When the radar is worth fetching for this leg."""
        return self.departure_window


def parse_time(value: str | None) -> time | None:
    return time.fromisoformat(value) if value else None


def leg_from_data(commute_id: str, commute_name: str, key: str, commute: dict[str, Any]) -> Leg:
    data = commute[key]
    mode = commute[CONF_SEGMENT_MODE]
    size = float(commute[CONF_SEGMENT_SIZE])
    profile = profile_from_track(data[CONF_TRACK])
    route = route_from_profile(
        f"{commute_name} · {key}",
        profile,
        mode,
        segment_length_km=size if mode == "distance" else 1.0,
        segment_minutes=size if mode == "time" else 5.0,
    )
    return Leg(
        commute_id=commute_id,
        commute_name=commute_name,
        key=key,
        route=route,
        radar_window=CommuteWindow(
            start=parse_time(data.get(CONF_RADAR_START)),
            end=parse_time(data.get(CONF_RADAR_END)),
            max_ahead_min=data.get(CONF_RADAR_MAX_AHEAD),
        ),
        forecast_window=CommuteWindow(
            start=parse_time(data[CONF_FORECAST_START]),
            end=parse_time(data[CONF_FORECAST_END]),
        ),
    )


def legs_from_entry(entry: ConfigEntry) -> list[Leg]:
    return [
        leg_from_data(sub.subentry_id, sub.title, key, dict(sub.data))
        for sub in entry.subentries.values()
        if sub.subentry_type == SUBENTRY_COMMUTE
        for key in LEGS
    ]
