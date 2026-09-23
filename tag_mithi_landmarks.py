"""
Tag real Mithi River (Mumbai) localities onto the KML-derived chainage
profile, so the reach reads the way people actually think of it, upstream
to downstream:

    Kurla (near Vidyanagari/Kalina end, upstream)
      -> Kalina -> Vakola -> BKC -> Dharavi -> N. Mahim
      -> Mahim Creek mouth (downstream, meets the Arabian Sea)

Why: extract_centerline.py's Voronoi-skeleton walk has no idea which end
of the traced polygon is "upstream" -- it just picks one leaf node as
station 0. For this Mithi River KML, chainage 0 already landed at the
Kurla/Vidyanagari (upstream, NE) end and the max chainage at the Mahim
Creek (downstream, SW) end -- confirmed by inspecting the first/last
station lon/lat below -- so, unlike the Mula-Mutha build, NO reversal is
needed here. This script only does the locality-snapping step:

  1. Loads naditwin/chainage_profile.json (already in upstream->downstream
     order for this KML).
  2. Snaps each named locality (given as an approximate real-world
     lon/lat) to its nearest station on that chainage and records the
     offset -> naditwin/landmarks.json.

Run once: `python3 tag_mithi_landmarks.py`
Needs only the standard library.
"""
import json
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
NADITWIN = os.path.join(HERE, "naditwin", "rivers", "mithi")
PROFILE_PATH = os.path.join(NADITWIN, "chainage_profile.json")
LANDMARKS_OUT = os.path.join(NADITWIN, "landmarks.json")

# Approximate real-world lon/lat for each named locality (public
# reference coordinates, e.g. OSM -- not surveyed). These are only used
# to find the *nearest station on the traced centerline*; the station's
# own lon/lat (from your KML) is what actually gets used downstream, so
# a locality being a bit off the exact bank just means a larger snap
# distance, reported below so you can sanity-check it and nudge the
# lon/lat here if a particular snap looks off.
LOCALITIES = [
    ("Kurla",              72.8791, 19.0728),  # LBS Marg / Kurla-Kalina bridge, upstream reach
    ("Kalina",             72.8474, 19.0730),  # Vidyanagari / Mumbai University Kalina campus
    ("Vakola",             72.8523, 19.0762),  # Vakola bridge / nala junction, Santacruz (E)
    ("BKC",                72.8677, 19.0660),  # Bandra Kurla Complex, river's southern edge
    ("Dharavi",            72.8547, 19.0430),  # Dharavi, 90 Feet Rd stretch along the Mithi
    ("N–Mahim",            72.8407, 19.0453),  # North Mahim, near Mahim Nature Park
    ("Mahim Creek mouth",  72.8378, 19.0385),  # confluence with Mahim Bay / Arabian Sea
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
    print(f"  chainage 0   -> lon {stations[0]['lon']}, lat {stations[0]['lat']}  (Kurla/upstream end)")
    print(f"  chainage max -> lon {stations[-1]['lon']}, lat {stations[-1]['lat']}  (Mahim Creek mouth end)")

    landmarks = []
    for name, lon, lat in LOCALITIES:
        best_i, best_d = 0, float("inf")
        for i, s in enumerate(stations):
            d = haversine_m(lon, lat, s["lon"], s["lat"])
            if d < best_d:
                best_d, best_i = d, i
        st = stations[best_i]
        landmarks.append({
            "name": name,
            "chainage_m": st["chainage_m"],
            "chainage_km": round(st["chainage_m"] / 1000.0, 3),
            "lon": st["lon"],
            "lat": st["lat"],
            "snap_distance_m": round(best_d, 1),
        })

    # report in real upstream->downstream order (by chainage) regardless
    # of the order LOCALITIES was listed in above
    landmarks.sort(key=lambda lm: lm["chainage_m"])

    with open(LANDMARKS_OUT, "w") as f:
        json.dump(landmarks, f, indent=2)

    print(f"\nWrote {len(landmarks)} landmarks -> {LANDMARKS_OUT}")
    print(f"{'Locality':<20}{'Chainage (km)':>14}{'Snap dist (m)':>16}")
    for lm in landmarks:
        flag = "  <- off riverbank, check" if lm["snap_distance_m"] > 800 else ""
        print(f"{lm['name']:<20}{lm['chainage_km']:>14.3f}{lm['snap_distance_m']:>16.1f}{flag}")


if __name__ == "__main__":
    main()
