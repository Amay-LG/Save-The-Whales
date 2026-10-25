"""Shared projection, unit conversion, and geometry functions for Person 1 & 2."""
import math
from typing import Tuple, Dict, Any, List

# Common conventions from Tasks.md:
# Fixed origin (34.2, -119.75)
# R = 6_371_000 m
# x = R * radians(lon - LON0) * cos(radians(LAT0))
# y = R * radians(lat - LAT0)

LAT0 = 34.2
LON0 = -119.75
R_M = 6_371_000.0
COS_LAT0 = math.cos(math.radians(LAT0))

NM_TO_M = 1852.0
M_TO_NM = 1.0 / 1852.0
KNOTS_TO_MPS = 1852.0 / 3600.0
MPS_TO_KNOTS = 3600.0 / 1852.0

def project(point: Dict[str, float]) -> Tuple[float, float]:
    """WGS84 point {"lat": float, "lon": float} -> (x, y) in meters."""
    lat = point["lat"]
    lon = point["lon"]
    x = R_M * math.radians(lon - LON0) * COS_LAT0
    y = R_M * math.radians(lat - LAT0)
    return (x, y)

def unproject(x_m: float, y_m: float) -> Dict[str, float]:
    """(x, y) in meters -> WGS84 point {"lat": float, "lon": float}."""
    lat = LAT0 + math.degrees(y_m / R_M)
    lon = LON0 + math.degrees(x_m / (R_M * COS_LAT0))
    return {"lat": lat, "lon": lon}

def distance_m(p1: Tuple[float, float], p2: Tuple[float, float]) -> float:
    """Euclidean distance in meters between two projected points."""
    return math.hypot(p2[0] - p1[0], p2[1] - p1[1])

def ship_clearance_m(length_m: float, beam_m: float, margin_m: float = 100.0) -> float:
    """ship_clearance_m = 0.5 * hypot(length_m, beam_m) + margin_m"""
    return 0.5 * math.hypot(length_m, beam_m) + margin_m

def point_in_polygon(pt: Tuple[float, float], poly: List[Tuple[float, float]]) -> bool:
    """Ray casting point in polygon test."""
    x, y = pt
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            x_int = x1 + (y - y1) * (x2 - x1) / (y2 - y1) if (y2 - y1) != 0 else x1
            if x < x_int:
                inside = not inside
    return inside

def _orient(a: Tuple[float, float], b: Tuple[float, float], c: Tuple[float, float]) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

def segments_intersect(a: Tuple[float, float], b: Tuple[float, float],
                       c: Tuple[float, float], d: Tuple[float, float]) -> bool:
    """True if segment ab intersects segment cd (strict or endpoint)."""
    d1 = _orient(c, d, a)
    d2 = _orient(c, d, b)
    d3 = _orient(a, b, c)
    d4 = _orient(a, b, d)

    if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and \
       ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)):
        return True

    # Collinear / touching check
    def on_segment(p, q, r):
        return min(p[0], r[0]) <= q[0] <= max(p[0], r[0]) and \
               min(p[1], r[1]) <= q[1] <= max(p[1], r[1])

    if d1 == 0 and on_segment(c, a, d): return True
    if d2 == 0 and on_segment(c, b, d): return True
    if d3 == 0 and on_segment(a, c, b): return True
    if d4 == 0 and on_segment(a, d, b): return True

    return False

def dist_point_to_segment_m(p: Tuple[float, float],
                            a: Tuple[float, float],
                            b: Tuple[float, float]) -> float:
    """Perpendicular distance from point p to segment ab in meters."""
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    l2 = dx * dx + dy * dy
    if l2 == 0:
        return distance_m(p, a)
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2))
    proj = (a[0] + t * dx, a[1] + t * dy)
    return distance_m(p, proj)

def segment_to_segment_dist_m(p1: Tuple[float, float], p2: Tuple[float, float],
                             q1: Tuple[float, float], q2: Tuple[float, float]) -> float:
    """Minimum distance between two line segments in meters."""
    if segments_intersect(p1, p2, q1, q2):
        return 0.0
    return min(
        dist_point_to_segment_m(p1, q1, q2),
        dist_point_to_segment_m(p2, q1, q2),
        dist_point_to_segment_m(q1, p1, p2),
        dist_point_to_segment_m(q2, p1, p2),
    )

def check_water_path(path: List[Dict[str, float]],
                     land: Dict[str, Any],
                     clearance_m: float) -> Dict[str, Any]:
    """Check if path is clear of land with clearance_m.
    land is LandMap with 'rings': list of lists of Point.
    """
    if not path or len(path) < 2:
        return {"ok": True, "min_clearance_m": float("inf"), "reasons": []}

    path_pts = [project(p) for p in path]
    rings = land.get("rings", [])
    if not rings:
        return {"ok": True, "min_clearance_m": float("inf"), "reasons": []}

    min_clearance = float("inf")
    reasons = []

    for ring in rings:
        poly_pts = [project(p) for p in ring]
        if len(poly_pts) < 3:
            continue

        # Check waypoint points inside polygon
        for idx, pt in enumerate(path_pts):
            if point_in_polygon(pt, poly_pts):
                min_clearance = 0.0
                reasons.append(f"WAYPOINT_{idx}_INSIDE_LAND")

        # Check path segments vs polygon edges
        m = len(poly_pts)
        for i in range(len(path_pts) - 1):
            s1 = path_pts[i]
            s2 = path_pts[i + 1]
            for j in range(m):
                e1 = poly_pts[j]
                e2 = poly_pts[(j + 1) % m]
                d = segment_to_segment_dist_m(s1, s2, e1, e2)
                if d < min_clearance:
                    min_clearance = d
                if d <= clearance_m:
                    reasons.append(f"SEGMENT_{i}_LAND_CLEARANCE_VIOLATION_{d:.1f}M")

    ok = len(reasons) == 0 and min_clearance > clearance_m
    return {"ok": ok, "min_clearance_m": min_clearance, "reasons": list(set(reasons))}

