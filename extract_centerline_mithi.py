"""
Extract a river centerline + chainage + width profile from a hand-drawn
polygon KML (Google Earth 'measurement' polygon tracing the river banks).

Method: Voronoi-skeleton centerline extraction.
  1. Parse polygon coords (lon/lat) from KML.
  2. Project to local UTM (metres) via pyproj.
  3. Densify the boundary ring.
  4. Build a Voronoi diagram of the densified boundary points.
  5. Keep only Voronoi edges whose midpoint falls INSIDE the polygon
     (these approximate the medial axis / centerline).
  6. Build a graph of surviving edges, take the longest shortest-path
     between two far-apart leaf/near-leaf nodes -> ordered centerline.
  7. Resample centerline at fixed chainage step (e.g. 10 m).
  8. At each chainage station, cast a perpendicular line and measure
     where it crosses the polygon boundary -> local river width.
"""
import json
import math
import xml.etree.ElementTree as ET

import numpy as np
from pyproj import Transformer
from scipy.spatial import Voronoi
from shapely.geometry import Polygon, LineString, Point
import networkx as nx

import os
HERE = os.path.dirname(os.path.abspath(__file__))
KML_PATH = os.path.join(HERE, "data", "mithi_river.kml")
CHAINAGE_STEP_M = 10.0   # spacing between chainage stations


def parse_polygon(path):
    ns = {"kml": "http://www.opengis.net/kml/2.2"}
    root = ET.parse(path).getroot()
    coords_text = root.find(".//kml:Polygon//kml:coordinates", ns).text.strip()
    pts = []
    for tok in coords_text.split():
        parts = tok.split(",")
        lon, lat = parts[0], parts[1]
        pts.append((float(lon), float(lat)))
    return pts


def densify_ring(coords_xy, spacing=2.0):
    """coords_xy: list of (x,y) in metres, closed ring. Insert points so
    consecutive spacing <= `spacing` metres."""
    out = []
    n = len(coords_xy)
    for i in range(n - 1):
        x0, y0 = coords_xy[i]
        x1, y1 = coords_xy[i + 1]
        seg_len = math.hypot(x1 - x0, y1 - y0)
        steps = max(1, int(seg_len // spacing))
        for s in range(steps):
            t = s / steps
            out.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
    out.append(coords_xy[-1])
    return out


def main():
    lonlat = parse_polygon(KML_PATH)
    if lonlat[0] != lonlat[-1]:
        lonlat.append(lonlat[0])

    # local UTM zone for Pune (~lon 73.86E) is UTM zone 43N
    to_utm = Transformer.from_crs("EPSG:4326", "EPSG:32643", always_xy=True)
    to_wgs = Transformer.from_crs("EPSG:32643", "EPSG:4326", always_xy=True)

    xy = [to_utm.transform(lon, lat) for lon, lat in lonlat]
    poly = Polygon(xy)
    if not poly.is_valid:
        poly = poly.buffer(0)

    dense = densify_ring(xy, spacing=3.0)
    dense_arr = np.array(dense)

    vor = Voronoi(dense_arr)

    # keep voronoi edges fully inside polygon
    G = nx.Graph()
    for (i, j) in vor.ridge_vertices:
        if i == -1 or j == -1:
            continue
        p1 = vor.vertices[i]
        p2 = vor.vertices[j]
        mid = Point((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2)
        if poly.contains(mid) and poly.contains(Point(p1)) and poly.contains(Point(p2)):
            d = math.hypot(p1[0] - p2[0], p1[1] - p2[1])
            G.add_edge(i, j, weight=d, p1=tuple(p1), p2=tuple(p2))

    # largest connected component = the skeleton
    comp = max(nx.connected_components(G), key=len)
    sub = G.subgraph(comp)

    # find graph diameter path (longest shortest path) -> river centerline
    # (approx: BFS/Dijkstra from an arbitrary node to find one endpoint,
    #  then again from that endpoint to find the true far endpoint)
    nodes = list(sub.nodes())
    start = nodes[0]
    lengths = nx.single_source_dijkstra_path_length(sub, start, weight="weight")
    end_a = max(lengths, key=lengths.get)
    lengths2 = nx.single_source_dijkstra_path_length(sub, end_a, weight="weight")
    end_b = max(lengths2, key=lengths2.get)
    path_nodes = nx.dijkstra_path(sub, end_a, end_b, weight="weight")

    centerline_xy = [tuple(vor.vertices[n]) for n in path_nodes]
    line = LineString(centerline_xy)
    total_len = line.length
    print(f"Skeleton nodes on centerline: {len(centerline_xy)}")
    print(f"Centerline length: {total_len:.1f} m")

    # resample at fixed chainage step
    n_stations = int(total_len // CHAINAGE_STEP_M) + 1
    stations = []
    for k in range(n_stations + 1):
        d = min(k * CHAINAGE_STEP_M, total_len)
        pt = line.interpolate(d)
        stations.append((d, pt.x, pt.y))
    if stations[-1][0] < total_len - 1e-6:
        pt = line.interpolate(total_len)
        stations.append((total_len, pt.x, pt.y))

    # local direction + perpendicular width at each station
    result = []
    for idx, (d, x, y) in enumerate(stations):
        # tangent direction via neighbouring stations
        if idx == 0:
            x2, y2 = stations[idx + 1][1], stations[idx + 1][2]
            dx, dy = x2 - x, y2 - y
        elif idx == len(stations) - 1:
            x0, y0 = stations[idx - 1][1], stations[idx - 1][2]
            dx, dy = x - x0, y - y0
        else:
            x0, y0 = stations[idx - 1][1], stations[idx - 1][2]
            x2, y2 = stations[idx + 1][1], stations[idx + 1][2]
            dx, dy = x2 - x0, y2 - y0
        norm = math.hypot(dx, dy) or 1.0
        dx, dy = dx / norm, dy / norm
        # perpendicular
        px, py = -dy, dx
        # cast a long perpendicular segment and clip to polygon
        L = 200.0
        cross = LineString([(x - px * L, y - py * L), (x + px * L, y + py * L)])
        inter = cross.intersection(poly)
        if inter.is_empty:
            width = 0.0
        elif inter.geom_type == "LineString":
            width = inter.length
        elif inter.geom_type == "MultiLineString":
            width = max(g.length for g in inter.geoms)
        else:
            width = 0.0

        lon, lat = to_wgs.transform(x, y)
        result.append({
            "chainage_m": round(d, 1),
            "lon": round(lon, 7),
            "lat": round(lat, 7),
            "width_m": round(width, 1),
        })

    out_profile = os.path.join(HERE, "naditwin", "rivers", "mithi", "chainage_profile.json")
    with open(out_profile, "w") as f:
        json.dump(result, f, indent=2)

    print(f"Wrote {len(result)} chainage stations to {out_profile}")
    print("Sample:", result[0], "...", result[-1])
    widths = [r["width_m"] for r in result]
    print(f"Width range: {min(widths):.1f} - {max(widths):.1f} m, avg {sum(widths)/len(widths):.1f} m")

    # also dump polygon in wgs84 for the dashboard basemap overlay
    poly_wgs = [to_wgs.transform(x, y) for x, y in xy]
    out_poly = os.path.join(HERE, "naditwin", "rivers", "mithi", "reach_polygon.json")
    with open(out_poly, "w") as f:
        json.dump([[round(lo, 7), round(la, 7)] for lo, la in poly_wgs], f)
    print(f"Wrote reach polygon to {out_poly}")


if __name__ == "__main__":
    main()
