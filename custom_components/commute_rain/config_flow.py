"""Config flow: one hub entry (shared backend) with one subentry per commute."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from .const import (
    CONF_ALWAYS_POLL,
    CONF_DEMO_MODE,
    CONF_FORECAST_END,
    CONF_FORECAST_START,
    CONF_GPX_FILE,
    CONF_GPX_NAME,
    CONF_MAX_RAIN,
    CONF_RADAR_END,
    CONF_RADAR_MAX_AHEAD,
    CONF_RADAR_START,
    CONF_REVERSE_OUTBOUND,
    CONF_RIDE_DURATION,
    CONF_SEGMENT_MODE,
    CONF_SEGMENT_SIZE,
    CONF_TRACK,
    DEFAULT_MAX_RAIN,
    DEMO_OFF,
    DEMO_SYNTHETIC,
    DOMAIN,
    SUBENTRY_COMMUTE,
    TRACK_STEP_M,
)
from .core.route import Profile, build_profile, parse_gpx, profile_from_track, profile_to_track, resample_profile

LEG_DEFAULTS = {
    "outbound": {
        CONF_FORECAST_START: "07:00:00",
        CONF_FORECAST_END: "09:30:00",
        CONF_RADAR_START: "07:00:00",
        CONF_RADAR_END: "09:30:00",
    },
    "return": {
        CONF_FORECAST_START: "15:00:00",
        CONF_FORECAST_END: "18:00:00",
        CONF_RADAR_END: "20:00:00",
        CONF_RADAR_MAX_AHEAD: 120,
    },
}
LEG_FIELDS = (CONF_FORECAST_START, CONF_FORECAST_END, CONF_RADAR_START, CONF_RADAR_END, CONF_RADAR_MAX_AHEAD)


class GpxError(Exception):
    """Error key for the form."""


# ---------------------------------------------------------------------------
# Track handling (runs in the executor)
# ---------------------------------------------------------------------------


def _with_duration(profile: Profile, duration_min: float | None) -> Profile:
    if not duration_min:
        return profile
    offset = profile.dist_m / profile.total_m * duration_min * 60
    return Profile(profile.lat, profile.lon, profile.dist_m, offset)


def track_from_upload(hass: HomeAssistant, file_id: str, duration_min: float | None) -> tuple[list, str]:
    with process_uploaded_file(hass, file_id) as path:
        text, name = Path(path).read_text(encoding="utf-8"), Path(path).name
    try:
        points = parse_gpx(text)
    except (ET.ParseError, ValueError, KeyError) as err:
        raise GpxError("invalid_gpx") from err
    try:
        profile = build_profile(points, duration_min * 60 if duration_min else None)
    except ValueError as err:
        raise GpxError("no_timestamps") from err
    return profile_to_track(resample_profile(profile, TRACK_STEP_M)), name


def reversed_track(track: list, duration_min: float | None) -> list:
    p = profile_from_track(track)
    rev = Profile(
        lat=p.lat[::-1],
        lon=p.lon[::-1],
        dist_m=p.total_m - p.dist_m[::-1],
        offset_s=p.total_s - p.offset_s[::-1],
    )
    return profile_to_track(_with_duration(rev, duration_min))


# ---------------------------------------------------------------------------
# Hub
# ---------------------------------------------------------------------------


class CommuteRainConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="Commute Rain", data={})
        return self.async_show_form(step_id="user", data_schema=vol.Schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return CommuteRainOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_COMMUTE: CommuteSubentryFlow}


class CommuteRainOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        opts = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(CONF_MAX_RAIN, default=opts.get(CONF_MAX_RAIN, DEFAULT_MAX_RAIN)): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0, max=10, step=0.01, unit_of_measurement="mm/h", mode="box")
                ),
                vol.Required(CONF_ALWAYS_POLL, default=opts.get(CONF_ALWAYS_POLL, False)): selector.BooleanSelector(),
                vol.Required(CONF_DEMO_MODE, default=opts.get(CONF_DEMO_MODE, DEMO_OFF)): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=[DEMO_OFF, DEMO_SYNTHETIC], translation_key=CONF_DEMO_MODE)
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)


# ---------------------------------------------------------------------------
# Commute (subentry): name -> outbound leg -> return leg
# ---------------------------------------------------------------------------


def _optional(key: str, suggested: Any) -> vol.Optional:
    return vol.Optional(key, description={"suggested_value": suggested} if suggested is not None else None)


TIME = selector.TimeSelector()
MINUTES = selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=600, step=1, unit_of_measurement="min", mode="box"))


class CommuteSubentryFlow(ConfigSubentryFlow):
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._title = ""

    @property
    def _reconfiguring(self) -> bool:
        return self.source == "reconfigure"

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        sub = self._get_reconfigure_subentry()
        self._data = {k: (dict(v) if isinstance(v, dict) else v) for k, v in sub.data.items()}
        self._title = sub.title
        return await self.async_step_user()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        if user_input is not None:
            self._title = user_input["name"]
            self._data[CONF_SEGMENT_MODE] = user_input[CONF_SEGMENT_MODE]
            self._data[CONF_SEGMENT_SIZE] = user_input[CONF_SEGMENT_SIZE]
            return await self.async_step_outbound()
        schema = vol.Schema(
            {
                vol.Required("name", default=self._title or "Work"): selector.TextSelector(),
                vol.Required(CONF_SEGMENT_MODE, default=self._data.get(CONF_SEGMENT_MODE, "distance")): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=["distance", "time"], translation_key=CONF_SEGMENT_MODE)
                ),
                vol.Required(CONF_SEGMENT_SIZE, default=self._data.get(CONF_SEGMENT_SIZE, 1.0)): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0.2, max=30, step=0.1, mode="box")
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_outbound(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return await self._leg_step("outbound", user_input)

    async def async_step_return(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return await self._leg_step("return", user_input)

    async def _leg_step(self, key: str, user_input: dict[str, Any] | None) -> SubentryFlowResult:
        current = self._data.get(key, {})
        errors: dict[str, str] = {}
        if user_input is not None:
            leg, error = await self._build_leg(key, user_input, current)
            if error:
                errors["base"] = error
            else:
                self._data[key] = leg
                if key == "outbound":
                    return await self.async_step_return()
                return self._finish()

        values = {**LEG_DEFAULTS[key], **current} if not user_input else user_input
        fields: dict[Any, Any] = {}
        need_file = not current and key == "outbound"
        fields[(vol.Required if need_file else vol.Optional)(CONF_GPX_FILE)] = selector.FileSelector(
            selector.FileSelectorConfig(accept=".gpx")
        )
        if key == "return":
            fields[vol.Optional(CONF_REVERSE_OUTBOUND, default=bool(values.get(CONF_REVERSE_OUTBOUND, False)))] = (
                selector.BooleanSelector()
            )
        fields[_optional(CONF_RIDE_DURATION, values.get(CONF_RIDE_DURATION))] = MINUTES
        fields[vol.Required(CONF_FORECAST_START, default=values[CONF_FORECAST_START])] = TIME
        fields[vol.Required(CONF_FORECAST_END, default=values[CONF_FORECAST_END])] = TIME
        fields[_optional(CONF_RADAR_START, values.get(CONF_RADAR_START))] = TIME
        fields[_optional(CONF_RADAR_END, values.get(CONF_RADAR_END))] = TIME
        fields[_optional(CONF_RADAR_MAX_AHEAD, values.get(CONF_RADAR_MAX_AHEAD))] = MINUTES
        return self.async_show_form(
            step_id=key,
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={"current_gpx": current.get(CONF_GPX_NAME, "none")},
        )

    async def _build_leg(self, key: str, user_input: dict[str, Any], current: dict[str, Any]) -> tuple[dict, str | None]:
        if user_input[CONF_FORECAST_START] >= user_input[CONF_FORECAST_END]:
            return {}, "forecast_window_order"
        duration = user_input.get(CONF_RIDE_DURATION)
        leg = {k: user_input.get(k) for k in LEG_FIELDS}
        leg[CONF_RIDE_DURATION] = duration
        try:
            if user_input.get(CONF_GPX_FILE):
                leg[CONF_TRACK], leg[CONF_GPX_NAME] = await self.hass.async_add_executor_job(
                    track_from_upload, self.hass, user_input[CONF_GPX_FILE], duration
                )
            elif key == "return" and user_input.get(CONF_REVERSE_OUTBOUND):
                leg[CONF_TRACK] = await self.hass.async_add_executor_job(
                    reversed_track, self._data["outbound"][CONF_TRACK], duration
                )
                leg[CONF_GPX_NAME] = f"{self._data['outbound'].get(CONF_GPX_NAME, 'outbound')} (reversed)"
                leg[CONF_REVERSE_OUTBOUND] = True
            elif current.get(CONF_TRACK):
                leg[CONF_TRACK], leg[CONF_GPX_NAME] = current[CONF_TRACK], current.get(CONF_GPX_NAME)
            else:
                return {}, "gpx_required"
        except GpxError as err:
            return {}, str(err)
        return leg, None

    def _finish(self) -> SubentryFlowResult:
        if self._reconfiguring:
            return self.async_update_and_abort(
                self._get_entry(), self._get_reconfigure_subentry(), title=self._title, data=self._data
            )
        return self.async_create_entry(title=self._title, data=self._data)
