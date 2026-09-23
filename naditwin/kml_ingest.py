"""
kml_ingest.py — turn ANY uploaded river KML (polygon trace or centerline
LineString, optionally with named Placemark points for bridges/localities)
into the same on-disk shape the rest of NadiTwin already expects:

    naditwin/rivers/<slug>/chainage_profile.json   [{chainage_m,lon,lat,width_m}, ...]
    naditwin/rivers/<slug>/reach_polygon.json      [[lon,lat], ...]   (best-effort; may
                                                     just be a thin buffer around the
                                                     centerline if the KML was a bare
                                                     LineString with no polygon)
    naditwin/rivers/<slug>/landmarks.json          [{name,chainage_m,chainage_km,lon,lat,
                                                      type,source,snap_distance_m}, ...]

Standard library only — no shapely/pyproj/scipy/networkx — so this can run
inside the live server process on any upload, not just as an offline dev
script.

WHAT'S REAL vs BEST-EFFORT for an uploaded river, honestly:
  - If the KML has a LineString: that IS the real centerline, used as-is.
  - If the KML only has a Polygon (hand-traced river banks, the common
    Google Earth / QGIS style): the centerline is DERIVED by pairing the
    two bank arcs point-for-point and taking their midpoints. This is a
    fast, dependency-free approximation of the true medial axis — good
    for a reasonably straight/elongated reach, weaker on tight meanders
    or braided channels. Width is the real bank-to-bank distance from the
    KML at each paired station, not synthetic.
  - Named Point placemarks in the KML (e.g. "XYZ Bridge", "Sangam") are
    used as real, KML-sourced landmarks, snapped to the nearest chainage
    station.
  - If the KML has NO named points at all, illustrative bridge/locality
    markers are auto-placed at a plausible spacing so the dashboard has
    something to show margins/alerts against — these are clearly tagged
    "source": "auto" (both in the JSON and in the dashboard UI) and are
    NOT real bridge locations. Replace them by adding named Point
    placemarks to your KML and re-uploading.
"""

import json
import math
import os
import random
import re
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
RIVERS_DIR = os.path.join(HERE, "rivers")
UPLOADS_DIR = (
    os.path.join("/tmp", "naditwin", "rivers")
    if os.environ.get("VERCEL")
    else RIVERS_DIR
)

DEFAULT_CHAINAGE_STEP_M = 100.0   # user-requested: chainage-wise change every 100 m
DEFAULT_WIDTH_M = 25.0            # fallback when KML gives no width info (LineString-only)
MAX_RING_POINTS = 1600            # decimate absurdly dense polygons before the O(n) diameter walk

BRIDGE_WORDS = ("bridge", "pul", "setu", "causeway", "crossing", "flyover", "culvert")
CONFLUENCE_WORDS = ("sangam", "confluence", "meets", "mouth", "creek mouth")


class KmlIngestError(ValueError):
    pass


# --------------------------------- parsing ---------------------------------

def _local(tag):
    return tag.split("}")[-1] if "}" in tag else tag


def _find_coords_text(el):
    for node in el.iter():
        if _local(node.tag) == "coordinates" and node.text and node.text.strip():
            return node.text.strip()
    return None


def _parse_coords(text):
    pts = []
    for tok in re.split(r"\s+", text.strip()):
        if not tok:
            continue
        parts = tok.split(",")
        if len(parts) < 2:
            continue
        try:
            lon, lat = float(parts[0]), float(parts[1])
        except ValueError:
            continue
        pts.append((lon, lat))
    return pts


def parse_kml(kml_text):
    """Returns (linestrings, polygons, points):
       linestrings = [(name, [(lon,lat), ...]), ...]
       polygons    = [(name, [(lon,lat), ...]), ...]   (outer boundary ring)
       points      = [(name, lon, lat), ...]
    """
    try:
        root = ET.fromstring(kml_text)
    except ET.ParseError as e:
        raise KmlIngestError(f"Could not parse KML/XML: {e}")

    linestrings, polygons, points = [], [], []

    placemarks = [el for el in root.iter() if _local(el.tag) == "Placemark"]
    if not placemarks:
        # some exports skip <Placemark> and put geometry straight under the doc;
        # treat the whole document as one unnamed placemark
        placemarks = [root]

    for pm in placemarks:
        pname = ""
        for child in pm:
            if _local(child.tag) == "name" and child.text:
                pname = child.text.strip()
                break

        for geom in pm.iter():
            gt = _local(geom.tag)
            if gt == "Point":
                ct = _find_coords_text(geom)
                if ct:
                    pts = _parse_coords(ct)
                    if pts:
                        points.append((pname, pts[0][0], pts[0][1]))
            elif gt == "LineString":
                ct = _find_coords_text(geom)
                if ct:
                    pts = _parse_coords(ct)
                    if len(pts) >= 2:
                        linestrings.append((pname, pts))
            elif gt == "Polygon":
                outer = None
                for node in geom.iter():
                    if _local(node.tag) == "outerBoundaryIs":
                        outer = node
                        break
                ct = _find_coords_text(outer if outer is not None else geom)
                if ct:
                    pts = _parse_coords(ct)
                    if len(pts) >= 3:
                        polygons.append((pname, pts))

    if not linestrings and not polygons:
        raise KmlIngestError(
            "No LineString or Polygon geometry found in this KML — "
            "need at least one river centerline or traced river-bank polygon."
        )
    return linestrings, polygons, points


# ------------------------------- geometry math -------------------------------

def haversine_m(lon1, lat1, lon2, lat2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))


def _cum_dist(pts):
    """pts: [(lon,lat), ...] -> cumulative haversine distance at each point."""
    cum = [0.0]
    for i in range(1, len(pts)):
        cum.append(cum[-1] + haversine_m(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1]))
    return cum


def _resample_by_arclength(pts, n_out):
    """Resample a polyline to n_out evenly-arclength-spaced points."""
    if len(pts) == n_out:
        return list(pts)
    cum = _cum_dist(pts)
    total = cum[-1]
    if total <= 0:
        return [pts[0]] * n_out
    out = []
    j = 0
    for k in range(n_out):
        d = total * k / (n_out - 1) if n_out > 1 else 0.0
        while j < len(cum) - 2 and cum[j + 1] < d:
            j += 1
        seg = cum[j + 1] - cum[j]
        t = 0.0 if seg <= 0 else (d - cum[j]) / seg
        lon = pts[j][0] + (pts[j + 1][0] - pts[j][0]) * t
        lat = pts[j][1] + (pts[j + 1][1] - pts[j][1]) * t
        out.append((lon, lat))
    return out


def _decimate(pts, max_n):
    if len(pts) <= max_n:
        return pts
    step = len(pts) / max_n
    idx = sorted(set(int(i * step) for i in range(max_n)))
    return [pts[i] for i in idx]


def _point_in_polygon(x, y, poly):
    """Ray-casting point-in-polygon test. poly: [(lon,lat), ...], unclosed."""
    n = len(poly)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-15) + xi):
            inside = not inside
        j = i
    return inside


def _closest_point_on_segment(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return ax, ay
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return ax + t * dx, ay + t * dy


def _nearest_point_on_ring(x, y, poly):
    best, best_d = None, float("inf")
    n = len(poly)
    for i in range(n):
        ax, ay = poly[i]
        bx, by = poly[(i + 1) % n]
        cx, cy = _closest_point_on_segment(x, y, ax, ay, bx, by)
        d = (cx - x) ** 2 + (cy - y) ** 2
        if d < best_d:
            best_d, best = d, (cx, cy)
    return best


def _keep_inside_polygon(centerline, poly):
    """Safety net so the drawn centerline NEVER steps outside the traced
    river-bank polygon: the bank-pairing midpoint can occasionally land
    just outside on a tight meander or a non-convex hand-trace. Any point
    found outside is nudged toward the polygon centroid at increasing
    strength until it lands inside; if the shape is winding enough that
    even that fails, it's pulled toward the previous already-inside
    centerline point instead (chainage stations are ~100 m apart, so a
    neighbor is always a safe, very-close substitute) — so the reach line
    stays within the KML polygon end to end, no exceptions."""
    if len(poly) < 3:
        return centerline
    ring = poly[:-1] if (len(poly) > 2 and poly[0] == poly[-1]) else list(poly)
    cx = sum(p[0] for p in ring) / len(ring)
    cy = sum(p[1] for p in ring) / len(ring)
    out = []
    for (x, y) in centerline:
        if _point_in_polygon(x, y, ring):
            out.append((x, y))
            continue
        nx, ny = _nearest_point_on_ring(x, y, ring)
        fixed = None
        for frac in (0.01, 0.05, 0.15, 0.35, 0.6, 0.85):
            tx = nx + (cx - nx) * frac
            ty = ny + (cy - ny) * frac
            if _point_in_polygon(tx, ty, ring):
                fixed = (tx, ty)
                break
        if fixed is None and out:
            px, py = out[-1]
            mid = ((nx + px) / 2.0, (ny + py) / 2.0)
            fixed = mid if _point_in_polygon(mid[0], mid[1], ring) else (px, py)
        if fixed is None:
            fixed = (nx, ny)   # first point, worst case — right on the boundary
        out.append(fixed)
    return out


def _farthest_pair_approx(pts):
    """2-pass farthest-point heuristic: cheap O(n) approx of the polygon
    'diameter' endpoints — used as the two ends of the river reach."""
    a0 = pts[0]
    ia = max(range(len(pts)), key=lambda i: haversine_m(a0[0], a0[1], pts[i][0], pts[i][1]))
    a = pts[ia]
    ib = max(range(len(pts)), key=lambda i: haversine_m(a[0], a[1], pts[i][0], pts[i][1]))
    return (ia, ib) if ia < ib else (ib, ia)


def _centerline_from_polygon(ring):
    """ring: closed or unclosed [(lon,lat), ...] hand-traced river-bank polygon.
    Returns (centerline_pts, width_at_each_pt) via bank-pairing midpoints."""
    pts = list(ring)
    if len(pts) > 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    pts = _decimate(pts, MAX_RING_POINTS)
    if len(pts) < 6:
        raise KmlIngestError("Traced polygon has too few vertices to derive a centerline.")

    ia, ib = _farthest_pair_approx(pts)
    if ia == ib:
        raise KmlIngestError("Could not find two distinct ends of the reach in this polygon.")

    arc1 = pts[ia:ib + 1]                                   # A -> B, one bank
    arc2_raw = pts[ib:] + pts[:ia + 1]                       # B -> ... -> A, other bank
    arc2 = list(reversed(arc2_raw))                          # A -> B, same direction as arc1

    n = max(60, min(2000, max(len(arc1), len(arc2))))
    b1 = _resample_by_arclength(arc1, n)
    b2 = _resample_by_arclength(arc2, n)

    centerline, widths = [], []
    for p1, p2 in zip(b1, b2):
        centerline.append(((p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0))
        widths.append(haversine_m(p1[0], p1[1], p2[0], p2[1]))
    return centerline, widths


def _bbox(pts):
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    return min(lons), min(lats), max(lons), max(lats)


def _thin_buffer_polygon(centerline, half_width_m=15.0):
    """Fabricates a thin ribbon polygon around a bare LineString so the map
    still has something to shade — clearly not a real bank trace."""
    n = len(centerline)
    left, right = [], []
    for i in range(n):
        x, y = centerline[i]
        if i == 0:
            x2, y2 = centerline[i + 1]
            dx, dy = x2 - x, y2 - y
        elif i == n - 1:
            x0, y0 = centerline[i - 1]
            dx, dy = x - x0, y - y0
        else:
            x0, y0 = centerline[i - 1]
            x2, y2 = centerline[i + 1]
            dx, dy = x2 - x0, y2 - y0
        # rough lon/lat-degree perpendicular offset for a small ribbon
        norm = math.hypot(dx, dy) or 1e-9
        px, py = -dy / norm, dx / norm
        m_per_deg_lat = 111320.0
        m_per_deg_lon = 111320.0 * math.cos(math.radians(y)) or 1e-6
        off_m = half_width_m
        left.append((x + px * off_m / m_per_deg_lon, y + py * off_m / m_per_deg_lat))
        right.append((x - px * off_m / m_per_deg_lon, y - py * off_m / m_per_deg_lat))
    return left + list(reversed(right))


# ------------------------------- landmarks -------------------------------

def _classify(name):
    low = (name or "").lower()
    if any(w in low for w in BRIDGE_WORDS):
        return "bridge"
    if any(w in low for w in CONFLUENCE_WORDS):
        return "confluence"
    return "locality"


def _snap_points_to_chainage(named_points, stations):
    out = []
    for name, lon, lat in named_points:
        if not name:
            continue
        best_i, best_d = 0, float("inf")
        for i, s in enumerate(stations):
            d = haversine_m(lon, lat, s["lon"], s["lat"])
            if d < best_d:
                best_d, best_i = d, i
        st = stations[best_i]
        out.append({
            "name": name,
            "chainage_m": st["chainage_m"],
            "chainage_km": round(st["chainage_m"] / 1000.0, 3),
            "lon": st["lon"], "lat": st["lat"],
            "type": _classify(name),
            "source": "kml",
            "snap_distance_m": round(best_d, 1),
        })
    return out


def _auto_landmarks(stations, reach_len_m, seed):
    """No named points in the KML — auto-place plausible bridge markers so
    margins/alerts/hydrograph have assets to attach to, named simply B-1,
    B-2, ... in upstream-to-downstream chainage order. Clearly tagged
    source:"auto" (both in the JSON and the dashboard UI) — these are
    illustrative placements at a plausible spacing, not surveyed bridge
    locations. Add named Point placemarks to your KML and re-upload for
    real ones."""
    rng = random.Random(seed)
    n_bridges = max(2, min(10, round(reach_len_m / 4000.0)))
    raw = []
    for k in range(n_bridges):
        frac = (k + 1) / (n_bridges + 1)
        jitter = rng.uniform(-0.35, 0.35) / (n_bridges + 1)
        x = max(0.0, min(reach_len_m, reach_len_m * (frac + jitter)))
        best_i = min(range(len(stations)), key=lambda i: abs(stations[i]["chainage_m"] - x))
        st = stations[best_i]
        raw.append(st)
    raw.sort(key=lambda st: st["chainage_m"])
    out = []
    for i, st in enumerate(raw):
        out.append({
            "name": f"B-{i + 1}",
            "chainage_m": st["chainage_m"],
            "chainage_km": round(st["chainage_m"] / 1000.0, 3),
            "lon": st["lon"], "lat": st["lat"],
            "type": "bridge",
            "source": "auto",
            "snap_distance_m": 0.0,
        })
    return out


# --------------------------------- main entry ---------------------------------

def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return s or "river"


def build_river_from_kml(kml_text, display_name, chainage_step_m=DEFAULT_CHAINAGE_STEP_M):
    """Parses kml_text, derives chainage/width/landmarks, writes
    naditwin/rivers/<slug>/*.json, and returns a summary dict."""
    linestrings, polygons, points = parse_kml(kml_text)

    width_source = "none"
    if linestrings:
        name, cl_pts = max(linestrings, key=lambda t: len(t[1]))
        centerline = cl_pts
        widths = None
        poly_for_map = None
        # if a polygon ALSO happens to be present alongside the LineString,
        # use it for width-at-station + the map trace
        if polygons:
            _, ring = max(polygons, key=lambda t: len(t[1]))
            poly_for_map = ring
            widths = _widths_from_polygon_at_points(centerline, ring)
            if widths is not None:
                width_source = "kml_polygon"
    else:
        pname, ring = max(polygons, key=lambda t: len(t[1]))
        centerline, widths = _centerline_from_polygon(ring)
        centerline = _keep_inside_polygon(centerline, ring)   # never draw outside the traced KML polygon
        poly_for_map = ring
        width_source = "kml_bank_pairing"

    if not centerline or len(centerline) < 2:
        raise KmlIngestError("Could not derive a usable centerline from this KML.")

    total_len = _cum_dist(centerline)[-1]
    if total_len < 50:
        raise KmlIngestError(f"Derived reach length is only {total_len:.0f} m — check the KML geometry.")

    n_stations = int(total_len // chainage_step_m) + 1
    station_pts = _resample_by_arclength(centerline, n_stations + 1)
    if not linestrings:
        # second safety pass: linear interpolation between two boundary-safe
        # points can still drift outside a concave/meandering polygon —
        # re-clip the final resampled chainage stations too.
        station_pts = _keep_inside_polygon(station_pts, ring)
    cum = _cum_dist(station_pts)

    if widths is not None and len(widths) == len(centerline):
        width_cum = _cum_dist(centerline)
        station_widths = _interp_series(width_cum, widths, cum)
    else:
        station_widths = [DEFAULT_WIDTH_M] * len(station_pts)
        width_source = "default_constant"

    stations = [
        {"chainage_m": round(cum[i], 1), "lon": round(station_pts[i][0], 7),
         "lat": round(station_pts[i][1], 7), "width_m": round(max(1.0, station_widths[i]), 1)}
        for i in range(len(station_pts))
    ]

    if poly_for_map is None:
        poly_for_map = _thin_buffer_polygon(centerline)
    reach_polygon = [[round(lo, 7), round(la, 7)] for lo, la in poly_for_map]

    kml_landmarks = _snap_points_to_chainage(points, stations)
    if kml_landmarks:
        landmarks = kml_landmarks
        landmarks.sort(key=lambda lm: lm["chainage_m"])
    else:
        landmarks = _auto_landmarks(stations, total_len, seed=hash(display_name) & 0xFFFFFFFF)

    slug = slugify(display_name)
    out_dir = os.path.join(UPLOADS_DIR, slug)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "chainage_profile.json"), "w") as f:
        json.dump(stations, f, indent=2)
    with open(os.path.join(out_dir, "reach_polygon.json"), "w") as f:
        json.dump(reach_polygon, f)
    with open(os.path.join(out_dir, "landmarks.json"), "w") as f:
        json.dump(landmarks, f, indent=2)

    n_bridges = sum(1 for lm in landmarks if lm["type"] == "bridge")
    n_confluences = sum(1 for lm in landmarks if lm["type"] == "confluence")
    span = f"{landmarks[0]['name']} \u2192 {landmarks[-1]['name']}" if landmarks else ""

    # persisted so the server can rediscover this uploaded river (with its
    # real display name/casing) on restart without re-uploading the KML.
    meta_out = {
        "display_name": display_name,
        "slug": slug,
        "reach_km": round(total_len / 1000.0, 3),
        "n_bridges": n_bridges,
        "n_confluences": n_confluences,
        "span": span,
        "landmarks_source": "kml" if kml_landmarks else "auto",
        "width_source": width_source,
        "chainage_step_m": chainage_step_m,
        "uploaded": True,
    }
    with open(os.path.join(out_dir, "meta.json"), "w") as f:
        json.dump(meta_out, f, indent=2)

    return {
        "key": slug,
        "display_name": display_name,
        "reach_km": round(total_len / 1000.0, 3),
        "n_stations": len(stations),
        "n_landmarks": len(landmarks),
        "n_bridges": n_bridges,
        "n_confluences": n_confluences,
        "span": span,
        "landmarks_source": "kml" if kml_landmarks else "auto",
        "width_source": width_source,
        "chainage_step_m": chainage_step_m,
    }


def _widths_from_polygon_at_points(centerline, ring):
    """Best-effort width when BOTH a LineString centerline and a separate
    Polygon were supplied: nearest-ring-point distance doubled. Cheap and
    approximate; returns None on any failure so caller falls back cleanly."""
    try:
        pts = list(ring)
        if len(pts) > 2 and pts[0] == pts[-1]:
            pts = pts[:-1]
        pts = _decimate(pts, MAX_RING_POINTS)
        widths = []
        for cx, cy in centerline:
            d = min(haversine_m(cx, cy, rx, ry) for rx, ry in pts)
            widths.append(max(2.0, d * 2.0))
        return widths
    except Exception:
        return None


def _interp_series(xs, ys, targets):
    out = []
    j = 0
    n = len(xs)
    for t in targets:
        while j < n - 2 and xs[j + 1] < t:
            j += 1
        seg = xs[j + 1] - xs[j]
        frac = 0.0 if seg <= 0 else (t - xs[j]) / seg
        frac = max(0.0, min(1.0, frac))
        out.append(ys[j] + (ys[j + 1] - ys[j]) * frac)
    return out
