"""SYNTHETIC AIS fixture for unit tests only. These are NOT real observations.

AGENTS.md rule 4: never use this in a benchmark or report. Every vessel id starts with
"999" (no real MMSI uses MID 999), and the CSV file name says SYNTHETIC.

Vessels (all on 2023-06-01 UTC, NOAA MarineCadastre CSV layout):
  999000001  moving   — straight line, 12 kn, COG 45°, report every 60 s for 2 h
  999000002  turning  — 10 kn, constant turn of 1.5°/min (a 180° turn over 2 h)
  999000003  stopping — 8 kn decelerating to 0 over 40 min, then moored with GPS jitter
  999000004  gaps     — 10 kn east; reports missing for 45 min (gap) and 3 h (gap)
  999000005  antimeridian — 14 kn heading 090° from 179.95°E, crossing to 179.xx°W
  999000006  dirty    — a duplicate, an out-of-order row, SOG 102.3, heading 511,
                        a lat 91 row, and a 1-report position spike

Regenerate the committed CSV with:
    cd backend && uv run python tests/fixtures/synthetic.py
Tests import it as `fixtures.synthetic` (pytest pythonpath includes tests/).
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from maritime_forecast.geo.geodesic import direct, knots_to_mps, wrap_bearing

T0 = datetime(2023, 6, 1, tzinfo=UTC)
CSV_PATH = Path(__file__).with_name("SYNTHETIC_tracks.csv")
HEADER = ["MMSI", "BaseDateTime", "LAT", "LON", "SOG", "COG", "Heading", "VesselType",
          "Status", "Length", "Width", "Draft", "TransceiverClass"]  # fmt: skip

MOVING, TURNING, STOPPING, GAPS, ANTIMERIDIAN, DIRTY = (f"99900000{i}" for i in range(1, 7))
GAP_MINUTES = (45, 180)  # the two gaps in the GAPS vessel


def _dead_reckon(lat, lon, sogs, cogs, dt_s):
    """Integrate positions from per-step SOG (kn) and COG (deg) on the WGS84 geodesic."""
    lats, lons = [lat], [lon]
    for s, c in zip(sogs[:-1], cogs[:-1], strict=True):
        la, lo = direct(lats[-1], lons[-1], c, knots_to_mps(s) * dt_s)
        lats.append(float(la))
        lons.append(float(lo))
    return lats, lons


def rows() -> list[dict]:
    out: list[dict] = []

    def add(mmsi, t, lat, lon, sog, cog, heading=511, vtype=70, status=0):
        out.append({"MMSI": mmsi, "BaseDateTime": t.strftime("%Y-%m-%dT%H:%M:%S"),
                    "LAT": f"{lat:.6f}", "LON": f"{lon:.6f}", "SOG": f"{sog:.1f}",
                    "COG": f"{cog:.1f}", "Heading": f"{heading:.0f}", "VesselType": vtype,
                    "Status": status, "Length": 180, "Width": 30, "Draft": 9.5,
                    "TransceiverClass": "A"})  # fmt: skip

    n = 121  # 2 h at 60 s
    times = [T0 + timedelta(minutes=k) for k in range(n)]

    # moving
    lats, lons = _dead_reckon(29.0, -94.0, [12.0] * n, [45.0] * n, 60)
    for t, la, lo in zip(times, lats, lons, strict=True):
        add(MOVING, t, la, lo, 12.0, 45.0, 45)

    # turning: COG 0 -> 180 at 1.5 deg/min
    cogs = [float(wrap_bearing(1.5 * k)) for k in range(n)]
    lats, lons = _dead_reckon(28.5, -94.5, [10.0] * n, cogs, 60)
    for t, la, lo, c in zip(times, lats, lons, cogs, strict=True):
        add(TURNING, t, la, lo, 10.0, c, c, vtype=60)

    # stopping: decelerate 8 -> 0 kn over 40 min, then moored with ~5 m jitter
    sogs = [max(0.0, 8.0 * (1 - k / 40)) for k in range(n)]
    lats, lons = _dead_reckon(29.3, -94.8, sogs, [270.0] * n, 60)
    rng = np.random.default_rng(7)
    for k, (t, la, lo, s) in enumerate(zip(times, lats, lons, sogs, strict=True)):
        if k > 40:
            la, lo = lats[40] + rng.normal(0, 4.5e-5), lons[40] + rng.normal(0, 5e-5)
        add(STOPPING, t, la, lo, s, 270.0, 270, vtype=80, status=5 if k > 45 else 0)

    # gaps: 10 kn east; drop minutes [20, 65) (45 min) and [80, 260) (3 h); 5 h total
    m = 300
    lats, lons = _dead_reckon(28.8, -95.2, [10.0] * (m + 1), [90.0] * (m + 1), 60)
    for k in range(m + 1):
        if 20 <= k < 20 + GAP_MINUTES[0] or 80 <= k < 80 + GAP_MINUTES[1]:
            continue
        add(GAPS, T0 + timedelta(minutes=k), lats[k], lons[k], 10.0, 90.0, 90, vtype=31)

    # antimeridian: 14 kn east from 179.95E at 10 S; crosses within ~12 min
    lats, lons = _dead_reckon(-10.0, 179.95, [14.0] * 61, [90.0] * 61, 60)
    for k in range(61):
        add(ANTIMERIDIAN, T0 + timedelta(minutes=k), lats[k], lons[k], 14.0, 90.0, 90)

    # dirty
    lats, lons = _dead_reckon(29.1, -94.6, [6.0] * 20, [180.0] * 20, 60)
    for k in range(20):
        add(DIRTY, T0 + timedelta(minutes=k), lats[k], lons[k], 6.0, 180.0, 180)
    d = [r for r in out if r["MMSI"] == DIRTY]
    out.append(dict(d[5]))  # exact duplicate
    d[8]["SOG"] = "102.3"  # SOG not available
    d[9]["LAT"] = "91.000000"  # position not available -> dropped
    d[12]["LAT"] = f"{float(d[12]['LAT']) + 0.5:.6f}"  # ~55 km spike for one report
    out.insert(0, out.pop(out.index(d[15])))  # out of order in the file
    return out


def csv_text() -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=HEADER, lineterminator="\n")
    w.writeheader()
    w.writerows(rows())
    return buf.getvalue()


if __name__ == "__main__":
    CSV_PATH.write_text(csv_text())
    print(f"wrote {CSV_PATH} ({len(rows())} rows)")
