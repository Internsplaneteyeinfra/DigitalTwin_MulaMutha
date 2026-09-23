"""
Reverse the extracted chainage direction and tag real Pune localities
onto it, so the reach reads the way people actually think of it:

    Sangam (confluence, upstream end, chainage 0)
      -> Bund Garden -> Dhanori -> Wadgaon Sheri -> Kharadi
      -> Mundhwa -> Hadapsar -> Manjari (downstream end)

Why: extract_centerline.py's Voronoi-skeleton walk has no idea which
end of the traced polygon is "upstream" -- it just picked one leaf
node as station 0. In this KML that happened to put chainage 0 at the
Manjari/Hadapsar (downstream) end and the max chainage at Sangam
(upstream, the real confluence). This script:

  1. Reverses naditwin/chainage_profile.json so chainage 0 m is at the
     Sangam end and chainage increases downstream (matches the real
     flow direction of the combined Mula-Mutha).
  2. Snaps each named locality (given as an approximate real-world
     lon/lat) to its nearest station on the *new* chainage and records
     the offset -> naditwin/landmarks.json.
  3. Leaves reach_polygon.json untouched (it's just the traced outline,
     order-independent for rendering).

The original (un-reversed) profile is kept as
naditwin/chainage_profile_original.json so this is easy to undo.

Run once: `python3 reverse_and_tag_landmarks.py`
Needs only the standard library.
"""
import json
import math
import os
import shutil

HERE = os.path.dirname(__file__)
NADITWIN = os.path.join(HERE, "naditwin", "rivers", "mula_mutha")
PROFILE_PATH = os.path.join(NADITWIN, "chainage_profile.json")
BACKUP_PATH = os.path.join(NADITWIN, "chainage_profile_original.json")
LANDMARKS_OUT = os.path.join(NADITWIN, "landmarks.json")

# Approximate real-world lon/lat for each named locality (public
# reference coordinates, e.g. OSM / Wikipedia -- not surveyed). These
# are only used to find the *nearest station on the traced centerline*;
# the station's own lon/lat (from your KML) is what actually gets used
# downstream, so a locality being a bit off the exact bank (e.g.
# Dhanori, which sits a few km north of the river) just means a larger
# snap distance, reported below so you can sanity-check it.
LOCALITIES = [
    ("Sangam",         73.8598, 18.5316),  # Mula-Mutha confluence / Sangamwadi
    ("Bund Garden",    73.8834, 18.5406),
    ("Dhanori",        73.9105, 18.5869),
    ("Wadgaon Sheri",  73.9231, 18.5533),
    ("Kharadi",        73.9436, 18.5522),
    ("Mundhwa",        73.9439, 18.5322),
    ("Hadapsar",       73.9280, 18.5050),  # river-adjacent Hadapsar (Mundhwa-Hadapsar rd)
    ("Manjari",        73.9718, 18.5124),
]


def haversine_m(lon1, lat1, lon2, lat2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))


def main():
    with open(PROFILE_PATH) as f:
        stations = json.load(f)

    total_len = stations[-1]["chainage_m"]
    print(f"Loaded {len(stations)} stations, total length {total_len:.1f} m")
    print(f"  old chainage 0   -> lon {stations[0]['lon']}, lat {stations[0]['lat']}  (Manjari/Hadapsar end)")
    print(f"  old chainage max -> lon {stations[-1]['lon']}, lat {stations[-1]['lat']}  (Sangam end)")

    if not os.path.exists(BACKUP_PATH):
        shutil.copyfile(PROFILE_PATH, BACKUP_PATH)
        print(f"Backed up original (un-reversed) profile -> {BACKUP_PATH}")
    else:
        print(f"Backup already exists at {BACKUP_PATH}, not overwriting.")

    # Reverse order + recompute chainage from the Sangam end.
    reversed_stations = []
    for s in reversed(stations):
        new_chainage = round(total_len - s["chainage_m"], 1)
        reversed_stations.append({
            "chainage_m": new_chainage,
            "lon": s["lon"],
            "lat": s["lat"],
            "width_m": s["width_m"],
        })
    # first must be exactly 0.0, last exactly total_len
    reversed_stations[0]["chainage_m"] = 0.0
    reversed_stations[-1]["chainage_m"] = round(total_len, 1)

    with open(PROFILE_PATH, "w") as f:
        json.dump(reversed_stations, f, indent=2)
    print(f"Wrote reversed profile -> {PROFILE_PATH}")
    print(f"  new chainage 0   -> lon {reversed_stations[0]['lon']}, lat {reversed_stations[0]['lat']}  (Sangam)")
    print(f"  new chainage max -> lon {reversed_stations[-1]['lon']}, lat {reversed_stations[-1]['lat']}  (Manjari)")

    # Snap each named locality to its nearest station on the NEW chainage.
    landmarks = []
    for name, lon, lat in LOCALITIES:
        best_i, best_d = 0, float("inf")
        for i, s in enumerate(reversed_stations):
            d = haversine_m(lon, lat, s["lon"], s["lat"])
            if d < best_d:
                best_d, best_i = d, i
        st = reversed_stations[best_i]
        landmarks.append({
            "name": name,
            "chainage_m": st["chainage_m"],
            "chainage_km": round(st["chainage_m"] / 1000.0, 3),
            "lon": st["lon"],
            "lat": st["lat"],
            "snap_distance_m": round(best_d, 1),
        })

    with open(LANDMARKS_OUT, "w") as f:
        json.dump(landmarks, f, indent=2)

    print(f"\nWrote {len(landmarks)} landmarks -> {LANDMARKS_OUT}")
    print(f"{'Locality':<16}{'Chainage (km)':>14}{'Snap dist (m)':>16}")
    for lm in landmarks:
        flag = "  <- off riverbank, check" if lm["snap_distance_m"] > 800 else ""
        print(f"{lm['name']:<16}{lm['chainage_km']:>14.3f}{lm['snap_distance_m']:>16.1f}{flag}")


if __name__ == "__main__":
    main()
