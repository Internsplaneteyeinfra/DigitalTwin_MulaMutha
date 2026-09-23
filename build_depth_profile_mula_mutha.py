"""
Interpolate the REAL water-depth survey grid (data/mula_mutha_water_depth.xlsx,
11,580 lat/lon/depth points) onto the KML-derived chainage stations
(naditwin/chainage_profile.json, 1,698 stations @ 10 m) so every station
along the real centerline gets a REAL measured depth value -- not a
synthetic formula.

Method: for each chainage station (lon, lat), take the inverse-distance-
weighted (IDW) average of the k nearest depth-survey points (k=8,
power=2), using a local equirectangular metres-projection so distances
are physically meaningful (not raw degrees). If the nearest survey
point is implausibly far away (> `MAX_SNAP_M`), the station is flagged
so you can sanity-check whether the survey grid actually covers that
stretch of the reach.

Output: naditwin/depth_profile.json -- one entry per chainage station:
    {"chainage_m": ..., "lon": ..., "lat": ...,
     "depth_m": <IDW depth>, "nearest_m": <distance to nearest survey point>}

This is the ONE genuinely-measured vertical layer in this build. Bed
elevation (absolute, geodetic) still needs a real DEM (Copernicus
GLO-30 or similar) to convert this relative depth into an absolute bed
level -- see README.md.
"""
import json
import math
import os

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

HERE = os.path.dirname(os.path.abspath(__file__))
DEPTH_XLSX = os.path.join(HERE, "data", "mula_mutha_water_depth.xlsx")
PROFILE_PATH = os.path.join(HERE, "naditwin", "rivers", "mula_mutha", "chainage_profile.json")
OUT_PATH = os.path.join(HERE, "naditwin", "rivers", "mula_mutha", "depth_profile.json")

K_NEIGHBORS = 8
IDW_POWER = 2.0
MAX_SNAP_M = 60.0   # flag stations whose nearest survey point is farther than this

# rough local metres-per-degree at this latitude (~18.53N), good enough
# for nearest-neighbour interpolation over a few hundred metres
LAT0 = 18.53
M_PER_DEG_LAT = 111320.0
M_PER_DEG_LON = 111320.0 * math.cos(math.radians(LAT0))


def to_local_xy(lon, lat):
    return lon * M_PER_DEG_LON, lat * M_PER_DEG_LAT


def main():
    df = pd.read_excel(DEPTH_XLSX, sheet_name="Water Depth Points")
    df = df.rename(columns={"Latitude": "lat", "Longitude": "lon", "Depth (m)": "depth_m"})
    print(f"Loaded {len(df)} real depth-survey points "
          f"(depth range {df['depth_m'].min():.2f}-{df['depth_m'].max():.2f} m)")

    xs, ys = to_local_xy(df["lon"].values, df["lat"].values)
    survey_xy = np.column_stack([xs, ys])
    tree = cKDTree(survey_xy)

    with open(PROFILE_PATH) as f:
        stations = json.load(f)

    out = []
    n_flagged = 0
    for s in stations:
        sx, sy = to_local_xy(s["lon"], s["lat"])
        dists, idxs = tree.query([sx, sy], k=K_NEIGHBORS)
        dists = np.atleast_1d(dists)
        idxs = np.atleast_1d(idxs)
        nearest_m = float(dists[0])

        if dists[0] < 1e-6:
            depth = float(df["depth_m"].values[idxs[0]])
        else:
            w = 1.0 / (dists ** IDW_POWER)
            depth = float(np.sum(w * df["depth_m"].values[idxs]) / np.sum(w))

        flagged = nearest_m > MAX_SNAP_M
        if flagged:
            n_flagged += 1

        out.append({
            "chainage_m": s["chainage_m"],
            "lon": s["lon"],
            "lat": s["lat"],
            "depth_m": round(depth, 3),
            "nearest_survey_m": round(nearest_m, 1),
            "flagged": flagged,
        })

    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=2)

    depths = [o["depth_m"] for o in out]
    print(f"Wrote {len(out)} station depths -> {OUT_PATH}")
    print(f"Depth range across stations: {min(depths):.2f}-{max(depths):.2f} m, "
          f"avg {sum(depths)/len(depths):.2f} m")
    print(f"Stations flagged (nearest survey point > {MAX_SNAP_M} m away): {n_flagged} / {len(out)}")


if __name__ == "__main__":
    main()
