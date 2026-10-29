"""Bright Sky HTTP client with cache and record/replay.

Every live response is stored on disk as
`<cache_dir>/<endpoint>/<key>/<fetched_at>.json` = {"params", "fetched_at", "response"}.
That single store serves three purposes:
- cache: a live request younger than the endpoint's minimum interval
  (radar 5 min, weather 1 h) is answered from disk;
- record: the files are kept, so any earlier run can be replayed;
- replay: `mode="replay"` never touches the network and returns the newest
  stored response (or the one given by `replay_at`).

Request building (`*_params`) is separate from the transport so an aiohttp
client can reuse it later.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import requests

from .radar import PixelBBox

BASE_URL = "https://api.brightsky.dev"
MIN_INTERVAL_S = {"radar": 5 * 60, "weather": 60 * 60}

Mode = Literal["live", "replay"]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def radar_params(bbox: PixelBBox, date: datetime | None = None, last_date: datetime | None = None) -> dict:
    """Query for `/radar`. Default: whole available nowcast (API keeps 6 h of
    history and forecasts 2 h ahead of the latest measurement)."""
    params = {"bbox": ",".join(map(str, bbox)), "format": "compressed"}
    if date is not None:
        params["date"] = _iso(date)
    if last_date is not None:
        params["last_date"] = _iso(last_date)
    return params


def weather_params(lat: float, lon: float, date: datetime, last_date: datetime) -> dict:
    return {"lat": f"{lat:.4f}", "lon": f"{lon:.4f}", "date": _iso(date), "last_date": _iso(last_date)}


def _cache_key(params: dict) -> str:
    # The time window changes on every call; the cache is keyed by location only.
    stable = {k: v for k, v in sorted(params.items()) if k not in ("date", "last_date")}
    return hashlib.sha1(json.dumps(stable).encode()).hexdigest()[:12]


class BrightSkyClient:
    def __init__(
        self,
        cache_dir: str | Path,
        user_agent: str,
        mode: Mode = "live",
        replay_at: str | None = None,
        session: requests.Session | None = None,
    ):
        self.cache_dir = Path(cache_dir)
        self.mode = mode
        self.replay_at = replay_at
        self.session = session or requests.Session()
        # requests advertises gzip by default; the radar endpoint requires it.
        self.session.headers["User-Agent"] = user_agent

    def get_radar(self, bbox: PixelBBox, date: datetime | None = None, last_date: datetime | None = None) -> dict:
        return self._get("radar", radar_params(bbox, date, last_date))

    def get_weather(self, lat: float, lon: float, date: datetime, last_date: datetime) -> dict:
        return self._get("weather", weather_params(lat, lon, date, last_date))

    # -- internals ---------------------------------------------------------

    def _dir(self, endpoint: str, params: dict) -> Path:
        return self.cache_dir / endpoint / _cache_key(params)

    def _stored(self, endpoint: str, params: dict) -> list[Path]:
        d = self._dir(endpoint, params)
        return sorted(d.glob("*.json")) if d.exists() else []

    def _get(self, endpoint: str, params: dict) -> dict:
        stored = self._stored(endpoint, params)
        if self.mode == "replay":
            if self.replay_at:
                stored = [p for p in stored if p.stem == self.replay_at]
            if not stored:
                raise FileNotFoundError(f"no recorded {endpoint} response in {self._dir(endpoint, params)}")
            return json.loads(stored[-1].read_text())["response"]

        if stored and time.time() - int(stored[-1].stem) < MIN_INTERVAL_S[endpoint]:
            return json.loads(stored[-1].read_text())["response"]

        resp = self.session.get(f"{BASE_URL}/{endpoint}", params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        fetched_at = int(time.time())
        path = self._dir(endpoint, params) / f"{fetched_at}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"params": params, "fetched_at": fetched_at, "response": data}))
        return data
