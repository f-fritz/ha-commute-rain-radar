"""Constants for Commute Rain."""

from datetime import timedelta

DOMAIN = "commute_rain"
SUBENTRY_COMMUTE = "commute"

LEGS = ("outbound", "return")

USER_AGENT = "commute-rain-radar/0.1 (Home Assistant custom integration; github.com/f-fritz)"
# Source notice required by the DWD's CC BY 4.0 licence (derived data).
ATTRIBUTION = "Datenbasis: Deutscher Wetterdienst, eigene Bearbeitung (CC BY 4.0), via Bright Sky"

RADAR_INTERVAL = timedelta(minutes=5)
FORECAST_INTERVAL = timedelta(hours=1)
# Never hit the API more often than this, even on manual refresh.
RADAR_MIN_AGE = timedelta(minutes=5) - timedelta(seconds=10)
FORECAST_MIN_AGE = timedelta(hours=1) - timedelta(minutes=1)

FORECAST_HOURS = 48
FORECAST_STEP_MIN = 15
RADAR_STEP_MIN = 5
RADAR_MARGIN_KM = 5
# Larger combined areas are split into several radar requests.
RADAR_MAX_BBOX_PX = 150
TRACK_STEP_M = 50.0

# Options (hub)
CONF_MAX_RAIN = "max_rain_mm_per_h"
CONF_ALWAYS_POLL = "always_poll_radar"
CONF_DEMO_MODE = "demo_mode"
DEFAULT_MAX_RAIN = 0.3
DEMO_OFF = "off"
DEMO_SYNTHETIC = "synthetic"

# Commute subentry data
CONF_SEGMENT_MODE = "segment_mode"
CONF_SEGMENT_SIZE = "segment_size"
CONF_TRACK = "track"
CONF_GPX_NAME = "gpx_name"
CONF_GPX_FILE = "gpx_file"
CONF_RIDE_DURATION = "ride_duration_min"
CONF_REVERSE_OUTBOUND = "reverse_outbound"
CONF_FORECAST_START = "forecast_start"
CONF_FORECAST_END = "forecast_end"
CONF_RADAR_START = "radar_start"
CONF_RADAR_END = "radar_end"
CONF_RADAR_MAX_AHEAD = "radar_max_ahead_min"
