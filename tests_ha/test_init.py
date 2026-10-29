from datetime import timedelta
from http import HTTPStatus

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.commute_rain.const import CONF_ALWAYS_POLL, CONF_DEMO_MODE, DEMO_SYNTHETIC

from .conftest import SAMPLE_TIME, commute_data, mock_api, track


@pytest.fixture(autouse=True)
async def berlin(hass):
    await hass.config.async_set_time_zone("Europe/Berlin")


@pytest.mark.freeze_time(SAMPLE_TIME)
async def test_setup_creates_device_and_entities(hass, make_entry, api_mock):
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert [d.name for d in devices] == ["Work"]
    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    # 2 legs x (6 sensors + 1 binary sensor + 1 image)
    assert len(entities) == 16
    assert all(e.config_subentry_id == next(iter(entry.subentries)) for e in entities)

    # One radar and one forecast request for both legs.
    assert api_mock.call_count == 2

    state = hass.states.get("sensor.work_rain_if_leaving_now_outbound")
    assert state is not None, sorted(hass.states.async_entity_ids())
    assert float(state.state) == 0.0  # recorded sample is dry
    assert state.attributes["radar_active"] is True
    assert len(state.attributes["departures"]) > 10

    assert hass.states.get("binary_sensor.work_ride_within_rain_limit_return").state == "on"
    # Outbound window is 07:00-09:30, so at 19:30 local there is no best departure.
    assert hass.states.get("sensor.work_best_departure_outbound").state == "unknown"
    best_return = dt_util.parse_datetime(hass.states.get("sensor.work_best_departure_return").state)
    assert best_return >= dt_util.utcnow()  # never in the past

    fc = hass.states.get("sensor.work_forecast_rain_next_window_outbound")
    assert fc.attributes["day"] == "2026-10-30"  # Friday morning, dry in the sample
    assert fc.attributes["station"] == "KOELN (BOTAN.GART.)"
    assert float(fc.state) == 0.0
    best = dt_util.parse_datetime(hass.states.get("sensor.work_forecast_best_departure_outbound").state)
    assert dt_util.as_local(best).strftime("%Y-%m-%d %H:%M") == "2026-10-30 07:00"


@pytest.mark.freeze_time(SAMPLE_TIME)
async def test_rainy_forecast_window(hass, make_entry, aioclient_mock):
    # Rain over the whole outbound window (07:00-09:30 local = 06:00-08:30 UTC).
    hours = ["2026-10-30T06:00:00+00:00", "2026-10-30T07:00:00+00:00", "2026-10-30T08:00:00+00:00",
             "2026-10-30T09:00:00+00:00"]
    mock_api(aioclient_mock, rain_mm=dict.fromkeys(hours, 2.0))
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    fc = hass.states.get("sensor.work_forecast_rain_next_window_outbound")
    assert fc.attributes["day"] == "2026-10-30"
    assert float(fc.state) > 0.3
    assert hass.states.get("sensor.work_forecast_best_departure_outbound").state == "unknown"


@pytest.mark.freeze_time(SAMPLE_TIME)
async def test_heatmap_image(hass, make_entry, api_mock, hass_client):
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    client = await hass_client()
    resp = await client.get("/api/image_proxy/image.work_rain_heatmap_return")
    assert resp.status == HTTPStatus.OK
    body = await resp.read()
    assert body[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.freeze_time("2026-10-30 02:00:00+00:00")  # 03:00 local, far from any window
async def test_radar_idle_outside_windows(hass, make_entry, api_mock):
    entry = make_entry(options={CONF_ALWAYS_POLL: False})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert [str(call[1]).rsplit("/", 1)[-1].split("?")[0] for call in api_mock.mock_calls] == ["weather"]
    state = hass.states.get("sensor.work_rain_if_leaving_now_outbound")
    assert state.state == "unknown"
    assert state.attributes["radar_active"] is False
    assert hass.states.get("binary_sensor.work_ride_within_rain_limit_outbound").state == "unknown"


@pytest.mark.freeze_time(SAMPLE_TIME)
async def test_two_commutes_share_requests(hass, make_entry, api_mock, work_commute):
    other = commute_data(track("from_work.gpx"), track("to_work.gpx"))
    entry = make_entry(commutes={"Work": work_commute, "Gym": other})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert api_mock.call_count == 2
    assert len(dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)) == 2
    assert hass.states.get("sensor.gym_rain_if_leaving_now_outbound") is not None


async def test_demo_mode_needs_no_radar(hass, make_entry, api_mock, freezer):
    freezer.move_to(SAMPLE_TIME)
    entry = make_entry(options={CONF_DEMO_MODE: DEMO_SYNTHETIC})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert all("radar" not in str(call[1]) for call in api_mock.mock_calls)
    values = set()
    for _ in range(18):  # 90 minutes: one full synthetic cycle
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        values.add(hass.states.get("sensor.work_rain_if_leaving_now_outbound").state)
    assert any(v not in ("unknown", "0.0") for v in values), values


@pytest.mark.freeze_time(SAMPLE_TIME)
async def test_radar_refresh_respects_min_interval(hass, make_entry, api_mock):
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    radar = entry.runtime_data.radar
    await radar.async_refresh()
    await radar.async_refresh()
    assert sum("radar" in str(c[1]) for c in api_mock.mock_calls) == 1


@pytest.mark.freeze_time(SAMPLE_TIME)
async def test_unload(hass, make_entry, api_mock):
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED
