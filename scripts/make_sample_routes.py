"""Generate the made-up sample routes in routes/ (deterministic).

Köln Süd station -> Messe Köln and back, by bike. Waypoints are placed by
hand along real streets (to work over the Severinsbrücke, home over the
Hohenzollernbrücke), then ridden at 1 Hz with a realistic speed profile:
accelerating, cruising, a few traffic-light stops, slower on bridges,
plus a little GPS noise. Not a recording; for demos and tests only.
"""

import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (lat, lon, stop_s, bridge): stop_s > 0 = wait at a traffic light there;
# bridge = the leg ending here is on a bridge (raised, a little slower).
TO_WORK = [
    (50.92388, 6.94003, 0, 0),   # Köln Süd, Luxemburger Straße exit
    (50.92481, 6.94051, 0, 0),   # Luxemburger Straße
    (50.92584, 6.94115, 0, 0),
    (50.92718, 6.94212, 0, 0),
    (50.92831, 6.94340, 25, 0),  # Barbarossaplatz
    (50.92646, 6.94726, 0, 0),   # Sachsenring
    (50.92522, 6.95080, 15, 0),
    (50.92358, 6.95433, 0, 0),
    (50.92180, 6.95819, 0, 0),
    (50.92111, 6.96060, 20, 0),  # Chlodwigplatz
    (50.92183, 6.95971, 0, 0),   # Severinstraße
    (50.92311, 6.95900, 0, 0),
    (50.92440, 6.95866, 0, 0),
    (50.92595, 6.95829, 0, 0),
    (50.92723, 6.95789, 0, 0),
    (50.92852, 6.95758, 0, 0),
    (50.92929, 6.95738, 15, 0),
    (50.92958, 6.95890, 0, 0),   # onto the Severinsbrücke
    (50.92987, 6.96092, 0, 1),
    (50.93019, 6.96295, 0, 1),
    (50.93083, 6.97010, 0, 1),
    (50.93150, 6.97300, 20, 1),  # Deutz, end of bridge
    (50.93250, 6.97167, 0, 0),   # north along the Deutz riverside road
    (50.93385, 6.97117, 0, 0),
    (50.93533, 6.97101, 0, 0),
    (50.93681, 6.97101, 0, 0),
    (50.93829, 6.97128, 0, 0),
    (50.93964, 6.97206, 0, 0),
    (50.94013, 6.97459, 15, 0),  # Ottoplatz, Bf Köln Messe/Deutz
    (50.94124, 6.97362, 0, 0),   # through the station
    (50.94247, 6.97420, 0, 0),
    (50.94247, 6.97693, 0, 0),   # Messe Köln
]

FROM_WORK = [
    (50.94247, 6.97693, 0, 0),   # Messe Köln
    (50.94247, 6.97420, 0, 0),
    (50.94124, 6.97362, 0, 0),   # through the station
    (50.94013, 6.97459, 20, 0),  # Ottoplatz
    (50.94030, 6.97250, 0, 0),
    (50.94045, 6.97089, 0, 0),   # Hohenzollernbrücke, Deutz side
    (50.94119, 6.96642, 0, 1),
    (50.94193, 6.96195, 0, 1),   # cathedral side
    (50.94161, 6.96136, 0, 0),
    (50.94087, 6.95942, 0, 0),   # past the cathedral
    (50.94026, 6.95805, 15, 0),
    (50.93971, 6.95553, 0, 0),
    (50.93971, 6.95327, 20, 0),  # Tunisstraße / Nord-Süd-Fahrt
    (50.93804, 6.95311, 0, 0),
    (50.93607, 6.95304, 25, 0),
    (50.93410, 6.95319, 0, 0),
    (50.93237, 6.95358, 0, 0),
    (50.93040, 6.95397, 20, 0),  # Blaubach
    (50.93026, 6.95224, 0, 0),   # Rothgerberbach
    (50.93037, 6.94983, 0, 0),
    (50.93012, 6.94758, 0, 0),
    (50.92965, 6.94597, 0, 0),
    (50.92903, 6.94469, 0, 0),
    (50.92831, 6.94340, 25, 0),  # Barbarossaplatz
    (50.92718, 6.94212, 0, 0),   # Luxemburger Straße
    (50.92584, 6.94115, 0, 0),
    (50.92481, 6.94051, 0, 0),
    (50.92388, 6.94003, 0, 0),   # Köln Süd
]

# Departure times (UTC; 07:42 and 17:05 Cologne summer time).
START = {"to_work": datetime(2026, 10, 20, 5, 42, 10, tzinfo=timezone.utc),
         "from_work": datetime(2026, 10, 20, 15, 5, 40, tzinfo=timezone.utc)}
CRUISE_MS = 5.0  # 18 km/h
EARTH_R = 6_371_000.0


def _dist(a, b) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(h))


def ride(waypoints, rng: random.Random):
    """1 Hz samples (lat, lon, ele, dt_s) along the waypoints."""
    pts, t, speed = [], 0, 0.0
    # Deck height 8 m: up from the first bridge waypoint, down to the last.
    flags = [w[3] for w in waypoints] + [0]
    height = [8.0 if flags[i] and flags[i + 1] else 0.0 for i in range(len(waypoints))]
    lat, lon = waypoints[0][:2]
    pts.append((lat, lon, 52.0, 0))
    for i in range(1, len(waypoints)):
        a, b = waypoints[i - 1], waypoints[i]
        seg = _dist(a, b)
        on_bridge = bool(b[3])
        target = CRUISE_MS * (0.85 if on_bridge else 1.0)
        done = 0.0
        while done < seg:
            remaining = seg - done
            stop_ahead = b[2] > 0 and remaining < 25
            want = min(target, 1.5 + remaining * 0.15) if stop_ahead else target
            want *= 1 + rng.uniform(-0.06, 0.06)
            speed += max(-1.2, min(0.6, want - speed))  # gentle accel, firmer braking
            speed = max(speed, 0.8)
            done = min(seg, done + speed)
            f = done / seg
            lat, lon = a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f
            ele = 52.0 + height[i - 1] + (height[i] - height[i - 1]) * f
            t += 1
            pts.append((lat, lon, ele, t))
        if b[2]:
            wait = b[2] + rng.randint(-8, 8)
            for _ in range(wait):
                t += 1
                pts.append((lat, lon, 52.0, t))
            speed = 0.0
    # GPS noise: about 2 m, correlated between samples like a real receiver.
    out, nlat, nlon = [], 0.0, 0.0
    for lat, lon, ele, dt in pts:
        nlat = 0.8 * nlat + rng.gauss(0, 1.2)
        nlon = 0.8 * nlon + rng.gauss(0, 1.2)
        out.append((lat + nlat / 111_320, lon + nlon / (111_320 * math.cos(math.radians(lat))),
                    ele + rng.gauss(0, 0.4), dt))
    return out


def to_gpx(name: str, start: datetime, pts) -> str:
    rows = "\n".join(
        f'      <trkpt lat="{lat:.7f}" lon="{lon:.7f}"><ele>{ele:.1f}</ele>'
        f"<time>{(start + timedelta(seconds=dt)).strftime('%Y-%m-%dT%H:%M:%SZ')}</time></trkpt>"
        for lat, lon, ele, dt in pts
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="commute-rain-radar scripts/make_sample_routes.py" xmlns="http://www.topografix.com/GPX/1/1">
  <metadata><desc>Made-up sample ride, Cologne. Not a recording.</desc></metadata>
  <trk>
    <name>{name}</name>
    <type>cycling</type>
    <trkseg>
{rows}
    </trkseg>
  </trk>
</gpx>
"""


def main() -> None:
    for key, wps, name in (("to_work", TO_WORK, "Köln Süd → Messe"), ("from_work", FROM_WORK, "Messe → Köln Süd")):
        pts = ride(wps, random.Random(key))
        path = ROOT / "routes" / f"{key}.gpx"
        path.write_text(to_gpx(name, START[key], pts))
        km = sum(_dist(wps[i - 1], wps[i]) for i in range(1, len(wps))) / 1000
        print(f"{path.relative_to(ROOT)}: {km:.1f} km, {pts[-1][3] / 60:.1f} min, {len(pts)} points")


if __name__ == "__main__":
    main()
