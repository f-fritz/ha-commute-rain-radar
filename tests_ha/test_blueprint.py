import shutil
from datetime import timedelta

import pytest
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service

from .conftest import ROOT

BLUEPRINT = ROOT / "blueprints" / "automation" / "commute_rain" / "commute_rain_notification.yaml"


@pytest.fixture
async def blueprint_dir(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    dst = tmp_path / "blueprints" / "automation" / "commute_rain"
    dst.mkdir(parents=True)
    shutil.copy(BLUEPRINT, dst)


async def test_notification_blueprint(hass, make_entry, api_mock, freezer, blueprint_dir):
    await hass.config.async_set_time_zone("Europe/Berlin")
    freezer.move_to("2026-10-29 18:34:30+00:00")  # 19:34:30 local, near the recorded sample
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    calls = async_mock_service(hass, "notify", "mobile_app_test")

    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "use_blueprint": {
                    "path": "commute_rain/commute_rain_notification.yaml",
                    "input": {
                        "notify_action": "notify.mobile_app_test",
                        "at_time": "19:35:00",
                        "rain_now": "sensor.work_rain_if_leaving_now_return",
                        "best_departure": "sensor.work_best_departure_return",
                        "best_rain": "sensor.work_rain_at_best_departure_return",
                        "forecast_rain": "sensor.work_forecast_rain_next_window_return",
                        "heatmap": "image.work_rain_heatmap_return",
                    },
                }
            }
        },
    )
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert len(calls) == 1
    data = calls[0].data
    assert data["title"].startswith("Leave at ")
    assert "Best departure" in data["message"] and "0.0 mm" in data["message"]
    assert "Leaving now: 0.0 mm" in data["message"]
    assert "Forecast for the window: 0.0 mm" in data["message"]
    assert data["data"]["image"] == "/api/image_proxy/image.work_rain_heatmap_return"
