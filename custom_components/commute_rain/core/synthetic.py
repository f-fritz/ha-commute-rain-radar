# Copied from commute_rain/ by scripts/sync_core.py. Do not edit here.
"""Synthetic radar: a straight rain band moving across the grid.

Gives a known answer for checking the heatmap on dry days. Works in grid
pixel coordinates, where 1 pixel = 1 km.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .radar import FRAME_S, RAW_TO_MM_PER_H, PixelBBox, RadarData, latlon_to_grid


@dataclass(frozen=True)
class RainBand:
    # Where the band's centre line passes at `t0` (epoch s).
    lat: float
    lon: float
    t0: int
    heading_deg: float = 90.0  # direction of motion, 0 = north, 90 = east
    speed_kmh: float = 30.0
    width_km: float = 4.0  # full width at half maximum
    peak_mm_per_h: float = 8.0


def band_intensity(band: RainBand, row, col, t) -> np.ndarray:
    """mm/h of `band` at fractional grid position (row, col) and epoch time t."""
    r0, c0 = latlon_to_grid(band.lat, band.lon)
    h = np.radians(band.heading_deg)
    # Unit vector of motion in (row, col): north = -row, east = +col.
    dr, dc = -np.cos(h), np.sin(h)
    travelled = band.speed_kmh * (np.asarray(t) - band.t0) / 3600
    along = (np.asarray(row) - r0) * dr + (np.asarray(col) - c0) * dc - travelled
    sigma = band.width_km / 2.355
    return band.peak_mm_per_h * np.exp(-0.5 * (along / sigma) ** 2)


def synthetic_radar(
    bbox: PixelBBox,
    observed_until: int,
    bands: list[RainBand],
    history_frames: int = 12,
    forecast_frames: int = 24,
) -> RadarData:
    """Frames from `history_frames` before to `forecast_frames` after
    `observed_until`, quantised to the real 0.01 mm / 5 min steps."""
    top, left, bottom, right = bbox
    rows, cols = np.mgrid[top : bottom + 1, left : right + 1]
    times = observed_until + FRAME_S * np.arange(-history_frames, forecast_frames + 1, dtype=np.int64)
    # Use the middle of each 5-minute interval as representative time.
    frames = np.stack(
        [sum(band_intensity(b, rows, cols, t - FRAME_S / 2) for b in bands) for t in times]
    )
    frames = np.round(frames / RAW_TO_MM_PER_H) * RAW_TO_MM_PER_H
    return RadarData(times=times, mm_per_h=frames.astype(np.float32), bbox=bbox, observed_until=observed_until)
