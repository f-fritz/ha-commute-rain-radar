"""Home Assistant tests. Run with: .venv-ha/bin/pytest tests_ha"""

import json
from pathlib import Path

import pytest
from homeassistant.config_entries import ConfigSubentryData
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.commute_rain.const import CONF_ALWAYS_POLL, DOMAIN, TRACK_STEP_M
from custom_components.commute_rain.core.route import build_profile, load_gpx, profile_to_track, resample_profile

pytest_plugins = "pytest_homeassistant_custom_component"

ROOT = Path(__file__).parent.parent
DATA = ROOT / "tests" / "data"
# Recorded responses for the Cologne sample routes, fetched at 2026-10-29
# 18:20 UTC (19:20 local). Radar dry, forecast dry for the next 48 h.
SAMPLE_TIME = "2026-10-29 18:30:00+00:00"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def track(name: str) -> list:
    return profile_to_track(resample_profile(build_profile(load_gpx(ROOT / "routes" / name)), TRACK_STEP_M))


def commute_data(outbound: list, back: list) -> dict:
    return {
        "segment_mode": "distance",
        "segment_size": 1.0,
        "outbound": {
            "track": outbound,
            "gpx_name": "to_work.gpx",
            "forecast_start": "07:00:00",
            "forecast_end": "09:30:00",
            "radar_start": "07:00:00",
            "radar_end": "09:30:00",
        },
        "return": {
            "track": back,
            "gpx_name": "from_work.gpx",
            "forecast_start": "15:00:00",
            "forecast_end": "18:00:00",
            "radar_end": "20:00:00",
            "radar_max_ahead_min": 120,
        },
    }


@pytest.fixture(scope="session")
def work_commute() -> dict:
    return commute_data(track("to_work.gpx"), track("from_work.gpx"))


@pytest.fixture
def make_entry(work_commute):
    def _make(options=None, commutes=None):
        commutes = {"Work": work_commute} if commutes is None else commutes
        return MockConfigEntry(
            domain=DOMAIN,
            title="Commute Rain",
            data={},
            options=options if options is not None else {CONF_ALWAYS_POLL: True},
            subentries_data=[
                ConfigSubentryData(data=data, subentry_type="commute", title=title, unique_id=None)
                for title, data in commutes.items()
            ],
        )

    return _make


def mock_api(aioclient_mock, rain_mm: dict[str, float] | None = None):
    """Serve the recorded samples. `rain_mm` overrides the forecast
    precipitation for the given hours (ISO timestamps as in the response)."""
    radar = json.loads((DATA / "radar_sample.json").read_text())["response"]
    weather = json.loads((DATA / "weather_sample.json").read_text())["response"]
    for row in weather["weather"]:
        if rain_mm and row["timestamp"] in rain_mm:
            row["precipitation"] = rain_mm[row["timestamp"]]
            row["precipitation_probability"] = 90
    aioclient_mock.get("https://api.brightsky.dev/radar", json=radar)
    aioclient_mock.get("https://api.brightsky.dev/weather", json=weather)
    return aioclient_mock


@pytest.fixture
def api_mock(aioclient_mock):
    return mock_api(aioclient_mock)

