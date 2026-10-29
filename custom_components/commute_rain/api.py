"""Async Bright Sky client (aiohttp). Same requests as core.brightsky_client."""

from __future__ import annotations

from datetime import datetime

import aiohttp

from .const import USER_AGENT
from .core.brightsky_client import BASE_URL, radar_params, weather_params
from .core.radar import PixelBBox


class BrightSkyApi:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def radar(self, bbox: PixelBBox, date: datetime, last_date: datetime) -> dict:
        return await self._get("radar", radar_params(bbox, date, last_date))

    async def weather(self, lat: float, lon: float, date: datetime, last_date: datetime) -> dict:
        return await self._get("weather", weather_params(lat, lon, date, last_date))

    async def _get(self, endpoint: str, params: dict) -> dict:
        async with self._session.get(
            f"{BASE_URL}/{endpoint}",
            params=params,
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            resp.raise_for_status()
            return await resp.json()
