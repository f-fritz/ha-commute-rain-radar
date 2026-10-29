import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.commute_rain.const import CONF_ALWAYS_POLL, CONF_DEMO_MODE, CONF_MAX_RAIN, DOMAIN

from .conftest import ROOT, SAMPLE_TIME

# Uploaded files are referenced by UUID.
THERE, BACK, BAD, NO_TIME = (str(uuid.UUID(int=i)) for i in range(1, 5))
FILES: dict[str, Path] = {THERE: ROOT / "routes" / "to_work.gpx", BACK: ROOT / "routes" / "from_work.gpx"}


@contextmanager
def _fake_upload(hass, file_id):
    yield FILES[file_id]


@pytest.fixture(autouse=True)
def uploads():
    with patch("custom_components.commute_rain.config_flow.process_uploaded_file", _fake_upload):
        yield


@pytest.fixture(autouse=True)
def no_setup():
    with patch("custom_components.commute_rain.async_setup_entry", return_value=True):
        yield


async def test_hub_flow_and_single_instance(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    again = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert again["type"] is FlowResultType.ABORT
    assert again["reason"] == "single_instance_allowed"


async def test_options_flow(hass, make_entry):
    entry = make_entry(options={})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MAX_RAIN: 0.5, CONF_ALWAYS_POLL: True, CONF_DEMO_MODE: "synthetic"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_MAX_RAIN: 0.5, CONF_ALWAYS_POLL: True, CONF_DEMO_MODE: "synthetic"}


LEG_OUT = {"forecast_start": "07:00:00", "forecast_end": "09:30:00", "radar_start": "07:00:00", "radar_end": "09:30:00"}
LEG_BACK = {"forecast_start": "15:00:00", "forecast_end": "18:00:00", "radar_end": "20:00:00", "radar_max_ahead_min": 120}


async def _start_commute(hass, entry):
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "commute"), context={"source": config_entries.SOURCE_USER}
    )
    assert result["step_id"] == "user"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Office", "segment_mode": "time", "segment_size": 5}
    )
    assert result["step_id"] == "outbound"
    return result


async def test_add_commute_with_two_tracks(hass, make_entry):
    entry = make_entry(commutes={})
    entry.add_to_hass(hass)
    result = await _start_commute(hass, entry)
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"gpx_file": THERE, **LEG_OUT})
    assert result["step_id"] == "return"
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"gpx_file": BACK, **LEG_BACK})
    assert result["type"] is FlowResultType.CREATE_ENTRY

    sub = next(iter(entry.subentries.values()))
    assert sub.title == "Office"
    out, back = sub.data["outbound"], sub.data["return"]
    assert out["gpx_name"] == "to_work.gpx" and back["gpx_name"] == "from_work.gpx"
    # 50 m resampling: ~6 km -> ~120 points, 22 min sample ride.
    assert 110 < len(out["track"]) < 135
    assert out["track"][-1][2] == pytest.approx(22.2 * 60, abs=30)
    assert back["radar_max_ahead_min"] == 120 and back.get("radar_start") is None


async def test_return_leg_reversed_outbound(hass, make_entry):
    entry = make_entry(commutes={})
    entry.add_to_hass(hass)
    result = await _start_commute(hass, entry)
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"gpx_file": THERE, **LEG_OUT})
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"reverse_outbound": True, "ride_duration_min": 45, **LEG_BACK}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    sub = next(iter(entry.subentries.values()))
    out, back = sub.data["outbound"]["track"], sub.data["return"]["track"]
    assert back[0][:2] == out[-1][:2] and back[-1][:2] == out[0][:2]
    assert back[-1][2] == pytest.approx(45 * 60)


async def test_return_leg_needs_a_track(hass, make_entry):
    entry = make_entry(commutes={})
    entry.add_to_hass(hass)
    result = await _start_commute(hass, entry)
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"gpx_file": THERE, **LEG_OUT})
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], dict(LEG_BACK))
    assert result["errors"] == {"base": "gpx_required"}


async def test_invalid_gpx_and_missing_timestamps(hass, make_entry, tmp_path):
    bad = tmp_path / "bad.gpx"
    bad.write_text("not xml")
    no_time = tmp_path / "notime.gpx"
    no_time.write_text(
        '<gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>'
        '<trkpt lat="50.0" lon="7.0"/><trkpt lat="50.01" lon="7.0"/></trkseg></trk></gpx>'
    )
    FILES[BAD], FILES[NO_TIME] = bad, no_time
    entry = make_entry(commutes={})
    entry.add_to_hass(hass)
    result = await _start_commute(hass, entry)
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"gpx_file": BAD, **LEG_OUT})
    assert result["errors"] == {"base": "invalid_gpx"}
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"gpx_file": NO_TIME, **LEG_OUT})
    assert result["errors"] == {"base": "no_timestamps"}
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"gpx_file": NO_TIME, "ride_duration_min": 5, **LEG_OUT}
    )
    assert result["step_id"] == "return"


async def test_forecast_window_order(hass, make_entry):
    entry = make_entry(commutes={})
    entry.add_to_hass(hass)
    result = await _start_commute(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"gpx_file": THERE, **LEG_OUT, "forecast_start": "10:00:00"}
    )
    assert result["errors"] == {"base": "forecast_window_order"}


@pytest.mark.freeze_time(SAMPLE_TIME)
async def test_reconfigure_keeps_tracks(hass, make_entry):
    entry = make_entry()
    entry.add_to_hass(hass)
    sub = next(iter(entry.subentries.values()))
    before = sub.data["return"]["track"]
    result = await entry.start_subentry_reconfigure_flow(hass, sub.subentry_id)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Work", "segment_mode": "distance", "segment_size": 0.5}
    )
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], dict(LEG_OUT))
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**LEG_BACK, "forecast_start": "16:00:00"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    sub = entry.subentries[sub.subentry_id]
    assert sub.data["segment_size"] == 0.5
    assert sub.data["return"]["forecast_start"] == "16:00:00"
    assert sub.data["return"]["track"] == before
