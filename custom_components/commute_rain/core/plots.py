# Copied from commute_rain/ by scripts/sync_core.py. Do not edit here.
"""Matplotlib figures. Plotting only; all numbers come from `radar` and `forecast`.

pyplot is only imported when a function has to create its own figure (the
notebook). `render_rain_matrix_png` uses a bare Figure, which is safe to call
from worker threads (Home Assistant).
"""

from __future__ import annotations

import io
from datetime import datetime
from zoneinfo import ZoneInfo

import matplotlib.dates as mdates
import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.collections import PatchCollection
from matplotlib.colors import BoundaryNorm, ListedColormap
from matplotlib.figure import Figure
from matplotlib.patches import Patch, Rectangle

from .config import CommuteWindow
from .forecast import HourlyForecast, WindowForecast
from .radar import RadarData, RainMatrix, best_departure, latlon_to_grid

# Fixed intensity classes (mm/h), roughly following DWD wording, plus the
# acceptable-rain threshold (0.3 mm/h) as its own boundary. The radar's
# smallest non-zero step is 0.12 mm/h, so "dry" means exactly zero.
RAIN_CLASSES = [
    (0.0, 0.1, "dry", "#f0efec"),
    (0.1, 0.3, "acceptable", "#cde2fb"),
    (0.3, 2.5, "light", "#86b6ef"),
    (2.5, 10.0, "moderate", "#3987e5"),
    (10.0, 50.0, "heavy", "#1c5cab"),
    (50.0, 1000.0, "very heavy", "#0d366b"),
]
RAIN_CMAP = ListedColormap([c[3] for c in RAIN_CLASSES])
RAIN_NORM = BoundaryNorm([c[0] for c in RAIN_CLASSES] + [RAIN_CLASSES[-1][1]], RAIN_CMAP.N)


def _class_label(lo: float, hi: float, name: str) -> str:
    if lo == 0:
        return name
    if hi >= 1000:
        return f"{name} (>{lo:g} mm/h)"
    return f"{name} ({lo:g}–{hi:g} mm/h)"


def _short_class_label(lo: float, hi: float, name: str) -> str:
    if lo == 0:
        return f"{name}\n< {hi:g}"
    if hi >= 1000:
        return f"{name}\n> {lo:g}"
    return f"{name}\n{lo:g}–{hi:g}"


NO_DATA = dict(facecolor="white", edgecolor="#b5b3ad", hatch="////", linewidth=0)
INK, MUTED, GRID = "#2b2a27", "#6f6d66", "#d9d7d1"
# Dotted overlay on cells and bars that come from the hourly forecast.
FORECAST = dict(edgecolor=MUTED, hatch="..", linewidth=0)
# Source notice required by the DWD's CC BY 4.0 licence (derived data).
DATA_CREDIT = "Datenbasis: Deutscher Wetterdienst, eigene Bearbeitung (CC BY 4.0) · via Bright Sky"


def _subplots(*args, **kwargs):
    import matplotlib.pyplot as plt

    return plt.subplots(*args, **kwargs)


def _credit(fig, below=None) -> None:
    """Source notice once per figure (several plots may share one): under
    the artist `below` (e.g. a legend under the plot), else under the figure.
    Sits outside the axes; saved with bbox_inches="tight"."""
    if getattr(fig, "_commute_rain_credit", False):
        return
    fig._commute_rain_credit = True
    if below is None:
        fig.text(0.99, 0.0, DATA_CREDIT, ha="right", va="top", fontsize=7, color=MUTED)
    else:
        fig.axes[0].annotate(
            DATA_CREDIT, xy=(1, 0), xycoords=below, xytext=(0, -4), textcoords="offset points",
            ha="right", va="top", fontsize=7, color=MUTED, annotation_clip=False,
        )


def _edges(centres: np.ndarray, step: float) -> np.ndarray:
    return np.append(centres - step / 2, centres[-1] + step / 2)


def _style(ax, fontsize: float = 9):
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=fontsize)
    ax.yaxis.label.set_color(MUTED)
    ax.xaxis.label.set_color(MUTED)


def plot_rain_matrix(
    m: RainMatrix,
    tz: str = "Europe/Berlin",
    window: CommuteWindow | None = None,
    max_mm_per_h: float | None = 0.3,
    ax_heat=None,
    ax_total=None,
    legend: bool = True,
    compact: bool = False,
):
    """Heatmap (departure x segment) plus a strip of total rain per departure.

    Bars are coloured by the heaviest rain on that ride, using the same
    classes as the heatmap. Cells filled from the hourly forecast (see
    `compute_rain_matrix`) are dotted. `compact` is a phone-friendly layout:
    short labels, legend below the plot."""
    zone = ZoneInfo(tz)
    fs = 8 if compact else 9
    if ax_heat is None:
        fig, (ax_heat, ax_total) = _subplots(
            2, 1, figsize=(11, 6.5), sharex=True, gridspec_kw={"height_ratios": [3, 1.3], "hspace": 0.08}
        )
    else:
        fig = ax_heat.figure

    step = float(np.diff(m.departures).min()) if len(m.departures) > 1 else 300.0
    x_edges = mdates.date2num(
        [datetime.fromtimestamp(t, zone) for t in _edges(m.departures.astype(float), step)]
    )
    segs = m.route.segments
    if m.route.segment_mode == "time":
        y_edges = [segs[0].start_offset_s / 60] + [s.end_offset_s / 60 for s in segs]
        y_label = "minutes into ride"
    else:
        y_edges = [segs[0].start_km] + [s.end_km for s in segs]
        y_label = "km from start"

    # Hatched background shows through the NaN (beyond nowcast) cells.
    ax_heat.add_patch(
        Rectangle((x_edges[0], y_edges[0]), x_edges[-1] - x_edges[0], y_edges[-1] - y_edges[0], **NO_DATA, zorder=0)
    )
    ax_heat.pcolormesh(
        x_edges, y_edges, np.ma.masked_invalid(m.intensity), cmap=RAIN_CMAP, norm=RAIN_NORM,
        edgecolors="white", linewidth=0.6, zorder=1,
    )
    from_forecast = m.from_forecast if m.from_forecast is not None else np.zeros(m.intensity.shape, dtype=bool)
    if from_forecast.any():
        cells = [
            Rectangle((x_edges[j], y_edges[i]), x_edges[j + 1] - x_edges[j], y_edges[i + 1] - y_edges[i])
            for i, j in zip(*np.nonzero(from_forecast))
        ]
        ax_heat.add_collection(PatchCollection(cells, facecolor="none", **FORECAST, zorder=1.5))
    has_gaps = bool(np.isnan(m.intensity).any())
    ax_heat.set_ylim(y_edges[0], y_edges[-1])  # start of the ride at the bottom
    ax_heat.set_ylabel(y_label)
    if compact:
        title = f"{m.route.name} · {m.route.total_km:.1f} km · {m.route.total_s / 60:.0f} min"
    else:
        title = (
            f"{m.route.name}: rain along the route by departure time  "
            f"({m.route.total_km:.1f} km, {m.route.total_s / 60:.0f} min)"
        )
    ax_heat.set_title(title, loc="left", color=INK, fontsize=11 if compact else 12)
    _style(ax_heat, fs)
    handles = [Patch(color=c[3], label=_class_label(*c[:3])) for c in RAIN_CLASSES]
    if from_forecast.any():
        handles.append(Patch(facecolor="white", **FORECAST, label="hourly forecast\n(same on whole route)"))
    if has_gaps:
        handles.append(Patch(**NO_DATA, label="beyond nowcast"))
    if legend and not compact:
        ax_heat.legend(handles=handles, loc="upper left", bbox_to_anchor=(1.01, 1), frameon=False, fontsize=9, labelcolor=INK)

    # --- totals strip ---------------------------------------------------
    x = mdates.date2num(m.departure_datetimes(zone))
    width = (x_edges[1] - x_edges[0]) * 0.8
    peak_colours = RAIN_CMAP(RAIN_NORM(np.nan_to_num(m.peak_mm_per_h)))
    for k in np.flatnonzero(m.complete):
        ax_total.bar(
            x[k], m.total_mm[k], width=width, color=peak_colours[k], edgecolor=MUTED, linewidth=0.4,
            hatch=FORECAST["hatch"] if m.uses_forecast[k] else None,
        )
    for k in np.flatnonzero(~m.complete):
        ax_total.bar(
            x[k], m.partial_mm[k], width=width, facecolor="white", edgecolor=peak_colours[k],
            hatch="////", linewidth=0.6,
        )
    ax_total.set_ylabel("rain on ride (mm)")
    ax_total.set_ylim(0, max(0.5, float(np.nanmax(m.partial_mm)) * 1.3))
    _style(ax_total, fs)
    ax_total.grid(axis="y", color=GRID, linewidth=0.6)
    ax_total.set_axisbelow(True)

    best = best_departure(m, tz, window, max_mm_per_h)
    if best is not None:
        bx = mdates.date2num(best.time)
        for ax in (ax_heat, ax_total):
            ax.axvline(bx, color=INK, linewidth=1.2, linestyle=(0, (3, 2)), zorder=3)
        if compact:
            label = f"best {best.time:%H:%M} · {best.total_mm:.2f} mm"
        else:
            label = f"best: {best.time:%H:%M}  ({best.total_mm:.2f} mm, peak {best.peak_mm_per_h:.2f} mm/h)"
        if m.uses_forecast[int(np.searchsorted(m.departures, best.time.timestamp()))]:
            label += " · forecast" if compact else ", from forecast"
    elif not (m.complete & m.in_window(window, tz)).any():
        label = "no complete ride in window" if compact else "no complete ride inside the commute window"
    else:
        label = f"none ≤ {max_mm_per_h:g} mm/h" if compact else f"no departure in window stays ≤ {max_mm_per_h:g} mm/h"
        fallback = best_departure(m, tz, window)
        if fallback is not None:
            bx = mdates.date2num(fallback.time)
            for ax in (ax_heat, ax_total):
                ax.axvline(bx, color=MUTED, linewidth=1.0, linestyle=":", zorder=3)
            if compact:
                label += f"; least {fallback.time:%H:%M} · {fallback.total_mm:.2f} mm"
            else:
                label += f"; least rain: {fallback.time:%H:%M} ({fallback.total_mm:.2f} mm, peak {fallback.peak_mm_per_h:.1f} mm/h)"
    ax_total.text(0.005, 0.97, label, transform=ax_total.transAxes, va="top", fontsize=fs, color=INK)
    if window is not None:
        # Grey out departures outside the commute window.
        for k, inside in enumerate(m.in_window(window, tz)):
            if not inside:
                for ax in (ax_heat, ax_total):
                    ax.axvspan(x_edges[k], x_edges[k + 1], color="#8a8880", alpha=0.25, zorder=2, linewidth=0)

    bottom_legend = None
    if legend and compact:
        # One legend for both panels, below the plot. The mm/h range goes on
        # a second line so all classes fit in one row on a phone.
        classes = [Patch(color=c[3], label=_short_class_label(*c[:3])) for c in RAIN_CLASSES]
        extras = []
        if from_forecast.any():
            extras.append(Patch(facecolor="white", **FORECAST, label="forecast"))
        if has_gaps:
            extras.append(Patch(**NO_DATA, label="no data"))
        if window is not None:
            extras.append(Patch(color="#8a8880", alpha=0.25, label="outside window"))
        # Classes in the first row, extras in the second. The legend fills
        # column by column, so interleave and pad with invisible handles.
        blank = Patch(alpha=0, label="")
        extras += [blank] * (len(classes) - len(extras))
        short = [h for pair in zip(classes, extras) for h in pair] if any(h is not blank for h in extras) else classes
        bottom_legend = ax_total.legend(
            handles=short, loc="upper center", bbox_to_anchor=(0.5, -0.32), ncol=len(classes),
            frameon=False, fontsize=fs, labelcolor=INK, handlelength=1.2, columnspacing=1.0,
            title="rain (mm/h)", title_fontsize=fs,
        )
        bottom_legend.get_title().set_color(MUTED)
    elif legend:
        strip_handles = [
            Patch(facecolor=RAIN_CLASSES[3][3], edgecolor=MUTED, linewidth=0.4, label="whole ride\n(colour = heaviest rain)"),
            Patch(facecolor="white", edgecolor=RAIN_CLASSES[3][3], hatch="////", label="covered part only"),
        ]
        if window is not None:
            strip_handles.append(Patch(color="#8a8880", alpha=0.25, label="outside commute window"))
        ax_total.legend(handles=strip_handles, loc="upper left", bbox_to_anchor=(1.01, 1), frameon=False, fontsize=9, labelcolor=INK)
    long_span = x_edges[-1] - x_edges[0] > 3 / 24
    if compact:
        ticks = [0] if long_span else [0, 30]
    else:
        ticks = [0, 30] if long_span else [0, 15, 30, 45]
    ax_total.xaxis.set_major_locator(mdates.MinuteLocator(byminute=ticks, tz=zone))
    ax_total.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=zone))
    day = f"{datetime.fromtimestamp(int(m.departures[0]), zone):%a %d %b}"
    ax_total.set_xlabel(f"departure time · {day}" if compact else f"departure time ({tz}) · {day}")
    ax_total.set_xlim(x_edges[0], x_edges[-1])
    _credit(fig, below=bottom_legend)
    return fig


def render_rain_matrix_png(
    m: RainMatrix,
    tz: str = "Europe/Berlin",
    window: CommuteWindow | None = None,
    max_mm_per_h: float | None = 0.3,
    compact: bool = True,
    dpi: int = 150,
) -> bytes:
    """PNG bytes of the rain matrix plot, without pyplot (thread-safe)."""
    fig = Figure(figsize=(6.4, 5.6) if compact else (11, 6.5), facecolor="white")
    FigureCanvasAgg(fig)
    ax_heat, ax_total = fig.subplots(
        2, 1, sharex=True, gridspec_kw={"height_ratios": [3, 1.4], "hspace": 0.1}
    )
    plot_rain_matrix(m, tz, window, max_mm_per_h, ax_heat=ax_heat, ax_total=ax_total, compact=compact)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight", facecolor="white")
    return buf.getvalue()


def plot_route_on_radar(routes, radar: RadarData, frame: int = -1, ax=None, tz: str = "Europe/Berlin"):
    """Sanity map: one radar frame in grid pixels with the routes on top."""
    if ax is None:
        _, ax = _subplots(figsize=(6, 6))
    top, left, bottom, right = radar.bbox
    ax.imshow(
        radar.mm_per_h[frame], cmap=RAIN_CMAP, norm=RAIN_NORM, origin="upper",
        extent=(left - 0.5, right + 0.5, bottom + 0.5, top - 0.5),
    )
    for route, style in zip(routes, ["-", "--", ":", "-."]):
        row, col = latlon_to_grid(route.path_lat, route.path_lon)
        ax.plot(col, row, style, color=INK, linewidth=1.5, label=route.name)
        r, c = latlon_to_grid([s.lat for s in route.segments], [s.lon for s in route.segments])
        ax.plot(c, r, "o", color=INK, markersize=3)
    t = datetime.fromtimestamp(int(radar.times[frame]), ZoneInfo(tz))
    ax.set_title(f"radar frame {t:%H:%M} (grid pixels, 1 km)", loc="left", fontsize=11, color=INK)
    ax.set_xlabel("column")
    ax.set_ylabel("row")
    ax.legend(frameon=False, fontsize=9)
    _style(ax)
    _credit(ax.figure)
    return ax.figure


def plot_forecast(
    fc: HourlyForecast,
    windows: list[WindowForecast],
    tz: str = "Europe/Berlin",
    max_mm_per_h: float | None = 0.3,
):
    """48 h timeline: hourly precipitation (top) and probability (bottom),
    with the commute windows shaded and their expected rain written above.

    Two panels instead of one chart with two y-axes, so each measure keeps
    its own honest scale."""
    zone = ZoneInfo(tz)
    fig, (ax_mm, ax_p) = _subplots(
        2, 1, figsize=(13, 6.5), sharex=True, gridspec_kw={"height_ratios": [2, 1], "hspace": 0.1}
    )
    # Each value belongs to the hour *before* its timestamp.
    centres = mdates.date2num([datetime.fromtimestamp(int(t) - 1800, zone) for t in fc.times])
    hour = 1 / 24
    colours = RAIN_CMAP(RAIN_NORM(np.nan_to_num(fc.precipitation_mm)))
    ax_mm.bar(centres, fc.precipitation_mm, width=hour * 0.85, color=colours, edgecolor=MUTED, linewidth=0.3)
    top = max(1.0, float(np.nanmax(fc.precipitation_mm)) * 1.6)
    ax_mm.set_ylim(0, top)
    ax_mm.set_ylabel("precipitation (mm/h)")
    if max_mm_per_h is not None:
        ax_mm.axhline(max_mm_per_h, color=MUTED, linewidth=1, linestyle=(0, (3, 2)))
        ax_mm.text(centres[-1] + hour, max_mm_per_h, f" {max_mm_per_h:g} mm/h", va="center", fontsize=8, color=MUTED)
    ax_mm.set_title(
        "Forecast: precipitation and probability along the commute"
        + (f"  (MOSMIX, {', '.join(fc.stations)})" if fc.stations else ""),
        loc="left", color=INK, fontsize=12,
    )

    ax_p.plot(centres, fc.probability, color="#256abf", linewidth=2)
    ax_p.set_ylim(0, 100)
    ax_p.set_ylabel("chance > 0.1 mm (%)")

    for w in windows:
        w0 = mdates.date2num(datetime.combine(w.day, w.window.start, zone))
        w1 = mdates.date2num(datetime.combine(w.day, w.window.end, zone))
        for ax in (ax_mm, ax_p):
            ax.axvspan(w0, w1, color="#8a8880", alpha=0.18, linewidth=0, zorder=0)
        best = f"best {w.best.departure:%H:%M}" if w.best else f"none ≤ {max_mm_per_h:g}"
        ax_mm.text(
            (w0 + w1) / 2, top * 0.97,
            f"{w.route}\n{w.min_mm:.1f}–{w.max_mm:.1f} mm\n{best}",
            ha="center", va="top", fontsize=8, color=INK, linespacing=1.3,
        )

    for ax in (ax_mm, ax_p):
        _style(ax)
        ax.grid(axis="y", color=GRID, linewidth=0.6)
        ax.set_axisbelow(True)
    ax_p.xaxis.set_major_locator(mdates.HourLocator(byhour=[0, 6, 12, 18], tz=zone))
    ax_p.xaxis.set_major_formatter(mdates.DateFormatter("%a %H:%M", tz=zone))
    ax_p.set_xlim(centres[0] - hour / 2, centres[-1] + hour / 2)
    ax_p.set_xlabel(f"time ({tz})")
    handles = [Patch(color=c[3], label=_class_label(*c[:3])) for c in RAIN_CLASSES[:-1]]
    handles.append(Patch(color="#8a8880", alpha=0.18, label="commute window (departures)"))
    ax_mm.legend(handles=handles, loc="upper left", bbox_to_anchor=(1.01, 1), frameon=False, fontsize=9, labelcolor=INK)
    _credit(fig)
    return fig
