#!/usr/bin/env python3
"""
Plot a GPX track as an interactive, zoomable chart with per-point timestamps.

- Parses GPX with the standard library only (xml.etree) -- no gpxpy needed.
- Uses plotly for interactivity:
    * scroll wheel / box-select to zoom, drag to pan
    * hover any dot -> tooltip with its timestamp, elevation, distance
    * click a dot -> a persistent selection box stays as reference
- If the track has elevation data, an elevation-over-time profile is
  included underneath, with the same hover timestamps.

Usage:
    python gpx_plot.py track.gpx                  # writes track.html, opens it
    python gpx_plot.py track.gpx -o out.html     # custom output name
    python gpx_plot.py track.gpx --no-browser    # don't auto-open

Only dependency:  pip install plotly
"""

import argparse
import math
import os
import sys
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
except ImportError:
    sys.exit("plotly is required:  pip install plotly")


# ---------------------------------------------------------------------------
# GPX parsing (handles GPX 1.0/1.1 namespace and namespace-less files)
# ---------------------------------------------------------------------------

def parse_gpx(path):
    """Return (points, track_name).

    Each point is a dict: lat, lon, time (datetime|None), ele (float|None).
    """
    root = ET.parse(path).getroot()

    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"  # e.g. {http://www.topografix.com/GPX/1/1}

    def tag(name):
        return ns + name

    name_el = root.find(f"./{tag('trk')}/{tag('name')}")
    if name_el is not None and name_el.text:
        track_name = name_el.text.strip()
    else:
        track_name = os.path.basename(path)

    points = []
    for trkpt in root.iter(tag("trkpt")):
        lat = float(trkpt.attrib["lat"])
        lon = float(trkpt.attrib["lon"])

        time = None
        time_el = trkpt.find(tag("time"))
        if time_el is not None and time_el.text:
            try:
                # ISO 8601; replace Z so fromisoformat works on Python < 3.11 too
                time = datetime.fromisoformat(time_el.text.strip().replace("Z", "+00:00"))
            except ValueError:
                pass

        ele = None
        ele_el = trkpt.find(tag("ele"))
        if ele_el is not None and ele_el.text:
            try:
                ele = float(ele_el.text)
            except ValueError:
                pass

        points.append({"lat": lat, "lon": lon, "time": time, "ele": ele})

    if not points:
        sys.exit(f"No <trkpt> elements found in {path}")
    return points, track_name


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def cumulative_km(points):
    cum = [0.0]
    for a, b in zip(points, points[1:]):
        cum.append(cum[-1] + haversine_km(a["lat"], a["lon"], b["lat"], b["lon"]))
    return cum


def fmt_time(t):
    tz = f" {t.tzname()}" if t.tzname() else ""
    return t.strftime("%Y-%m-%d %H:%M:%S") + tz


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------

def build_figure(points, track_name):
    lats = [p["lat"] for p in points]
    lons = [p["lon"] for p in points]
    times = [p["time"] for p in points]
    eles = [p["ele"] for p in points]
    dists = cumulative_km(points)
    n = len(points)

    time_labels = [fmt_time(t) if t else "no time" for t in times]
    hover_text = [
        f"Point {i + 1}/{n}<br>{time_labels[i]}"
        + (f"<br>Elevation: {e:.0f} m" if e is not None else "")
        + f"<br>Along-track: {d:.2f} km"
        for i, (e, d) in enumerate(zip(eles, dists))
    ]

    has_elevation = any(e is not None for e in eles)
    has_time = times[0] is not None

    fig = make_subplots(
        rows=2 if has_elevation else 1,
        shared_xaxes=False,
        vertical_spacing=0.08,
        subplot_titles=(["Track", "Elevation"] if has_elevation else ["Track"]),
        specs=[[{"type": "xy"}]] * (2 if has_elevation else 1),
    )

    # --- Track (top): line + hoverable dots -------------------------------
    fig.add_trace(
        go.Scatter(
            x=lons, y=lats, mode="lines",
            line=dict(color="royalblue", width=3),
            name="Track", hoverinfo="skip", legendgroup="track",
        ),
        row=1, col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=lons, y=lats, mode="markers",
            marker=dict(size=5, color="royalblue", opacity=0.8),
            name="Points (hover me)", legendgroup="track",
            text=hover_text, hoverinfo="text",
            hoverlabel=dict(font=dict(size=12), bgcolor="white"),
        ),
        row=1, col=1,
    )

    # --- Elevation profile (bottom, only if data exists) --------------------
    if has_elevation:
        x = times if has_time else dists
        x_title = "time" if has_time else "along-track distance (km)"
        fig.add_trace(
            go.Scatter(
                x=x, y=eles, mode="lines+markers",
                line=dict(color="seagreen", width=2),
                marker=dict(size=4),
                name="Elevation",
                text=hover_text, hoverinfo="text",
                hoverlabel=dict(font=dict(size=12), bgcolor="white"),
            ),
            row=2, col=1,
        )
        fig.update_yaxes(title_text="elevation (m)", row=2, col=1)
        fig.update_xaxes(title_text=x_title, row=2, col=1)

    # --- Layout: zoomable, equal-ish aspect for the map ---------------------
    title = f"{track_name} — {n} points"
    if times[0] and times[-1]:
        title += f"<br><sup>{fmt_time(times[0])} → {fmt_time(times[-1])}</sup>"

    fig.update_layout(
        title=title,
        hovermode="closest",       # tooltip snaps to the nearest dot
        dragmode="pan",            # drag = pan; wheel/box = zoom; modebar too
        legend=dict(orientation="h", yanchor="bottom", y=1.05, x=0),
        margin=dict(l=50, r=20, t=90, b=40),
        height=400 + (300 if has_elevation else 0),
    )
    # Scale latitude vs longitude so the map is roughly undistorted.
    # (latitude degrees are ~cos(lat) times shorter on the ground)
    mean_lat = sum(lats) / n
    fig.update_yaxes(
        title_text="latitude", scaleanchor="x",
        scaleratio=1.0 / max(math.cos(math.radians(mean_lat)), 0.1),
        row=1, col=1,
    )
    fig.update_xaxes(title_text="longitude", row=1, col=1)

    return fig


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Plot a GPX track with hoverable timestamps.")
    ap.add_argument("gpx", help="path to the .gpx file")
    ap.add_argument("-o", "--output", help="output HTML file (default: <track>.html)")
    ap.add_argument("--no-browser", action="store_true", help="do not auto-open the result")
    args = ap.parse_args()

    points, track_name = parse_gpx(args.gpx)
    with_time = sum(1 for p in points if p["time"])
    print(f"{track_name}: {len(points)} points, {with_time} with timestamps")

    fig = build_figure(points, track_name)

    out = args.output or os.path.splitext(args.gpx)[0] + ".html"
    fig.write_html(out, include_plotlyjs=True)  # self-contained: works offline
    print(f"Wrote {out}")

    if not args.no_browser:
        webbrowser.open("file://" + os.path.abspath(out))


if __name__ == "__main__":
    main()