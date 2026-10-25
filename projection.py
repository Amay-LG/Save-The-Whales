"""WGS84 <-> local tangent-plane projection (meters) for the Santa Barbara Channel.

All internal advisor math uses meters and seconds. This module converts between
WGS84 lat/lon (contract interface) and a local X-east / Y-north coordinate system
centred on the configured projection origin.
"""
import math
import numpy as np

# ── Projection origin: centre of the Santa Barbara Channel ──────────────────
LAT0 = 34.2    # °N
LON0 = -119.75 # °W

R_EARTH = 6_371_000.0  # metres
_COS_LAT0 = math.cos(math.radians(LAT0))

# ── Unit conversions ────────────────────────────────────────────────────────
KN_TO_MS = 0.514444       # 1 knot → m/s
MS_TO_KN = 1.0 / KN_TO_MS
NM_TO_M = 1852.0
M_TO_NM = 1.0 / NM_TO_M
KMH_TO_MS = 1.0 / 3.6


# ── Core projection ────────────────────────────────────────────────────────

def project(lat: float, lon: float) -> tuple[float, float]:
    """WGS84 (lat, lon) → local (x_east_m, y_north_m)."""
    x = R_EARTH * math.radians(lon - LON0) * _COS_LAT0
    y = R_EARTH * math.radians(lat - LAT0)
    return (x, y)


def unproject(x: float, y: float) -> tuple[float, float]:
    """Local (x_m, y_m) → WGS84 (lat, lon)."""
    lat = LAT0 + math.degrees(y / R_EARTH)
    lon = LON0 + math.degrees(x / (R_EARTH * _COS_LAT0))
    return (lat, lon)


def project_array(latlons: list[dict]) -> np.ndarray:
    """List of {"lat": ..., "lon": ...} → (N, 2) array of (x, y) metres."""
    if not latlons:
        return np.empty((0, 2))
    return np.array([project(p["lat"], p["lon"]) for p in latlons])


def unproject_to_latlon(x: float, y: float) -> dict:
    """Local (x, y) → {"lat": ..., "lon": ...} dict for contract output."""
    lat, lon = unproject(x, y)
    return {"lat": round(lat, 6), "lon": round(lon, 6)}


# ── Geometry helpers ────────────────────────────────────────────────────────

def distance_m(p1, p2) -> float:
    """Euclidean distance (m) between two (x, y) points."""
    return math.hypot(p1[0] - p2[0], p1[1] - p2[1])


def heading_rad(p_from, p_to) -> float:
    """Heading in radians, 0 = north, clockwise positive."""
    dx = p_to[0] - p_from[0]
    dy = p_to[1] - p_from[1]
    return math.atan2(dx, dy)


def velocity_from_cog_sog(cog_deg: float, sog_kn: float) -> tuple[float, float]:
    """COG (degrees from north, clockwise) + SOG (knots) → (vx, vy) in m/s."""
    rad = math.radians(cog_deg)
    speed = sog_kn * KN_TO_MS
    return (speed * math.sin(rad), speed * math.cos(rad))


def bearing_deg(p_from, p_to) -> float:
    """Bearing in degrees (0–360, north = 0, clockwise) from p_from to p_to."""
    dx = p_to[0] - p_from[0]
    dy = p_to[1] - p_from[1]
    return math.degrees(math.atan2(dx, dy)) % 360


def path_length_m(waypoints_xy: list[tuple[float, float]]) -> float:
    """Total path length along a waypoint list, in metres."""
    return sum(distance_m(a, b) for a, b in zip(waypoints_xy, waypoints_xy[1:]))


# ── Segment intersection (for land / polygon checks) ───────────────────────

def point_in_polygon(pt: tuple[float, float], poly: list[tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon test."""
    x, y = pt
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            x_int = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < x_int:
                inside = not inside
    return inside


def _orient(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def segments_intersect(a, b, c, d) -> bool:
    """True if line segment ab strictly crosses cd."""
    return (_orient(a, b, c) * _orient(a, b, d) < 0) and \
           (_orient(c, d, a) * _orient(c, d, b) < 0)


def segment_hits_polygon(a, b, poly) -> bool:
    """True if segment ab intersects or is inside polygon."""
    if point_in_polygon(a, poly) or point_in_polygon(b, poly):
        return True
    n = len(poly)
    return any(segments_intersect(a, b, poly[i], poly[(i + 1) % n]) for i in range(n))


def path_hits_land(path_xy: list[tuple[float, float]],
                   land_polys: list[list[tuple[float, float]]]) -> bool:
    """True if any segment of path crosses any land polygon."""
    for a, b in zip(path_xy, path_xy[1:]):
        for poly in land_polys:
            if segment_hits_polygon(a, b, poly):
                return True
    return False


# ── Bounding box ────────────────────────────────────────────────────────────

def point_in_bbox_xy(pt, bbox_xy) -> bool:
    """Check if (x, y) is within the projected bounding box.
    bbox_xy = (x_min, y_min, x_max, y_max)."""
    return bbox_xy[0] <= pt[0] <= bbox_xy[2] and bbox_xy[1] <= pt[1] <= bbox_xy[3]


def project_bbox(bbox_cfg: dict) -> tuple[float, float, float, float]:
    """Config bbox dict → projected (x_min, y_min, x_max, y_max) in metres."""
    x_min, y_min = project(bbox_cfg["lat_min"], bbox_cfg["lon_min"])
    x_max, y_max = project(bbox_cfg["lat_max"], bbox_cfg["lon_max"])
    return (min(x_min, x_max), min(y_min, y_max),
            max(x_min, x_max), max(y_min, y_max))


# ── Land polygon loading ───────────────────────────────────────────────────

def load_land_polygons(geojson: dict) -> list[list[tuple[float, float]]]:
    """GeoJSON FeatureCollection → list of projected polygon rings (metres).
    Skips features with empty or degenerate rings."""
    polys = []
    for feat in geojson.get("features", []):
        ring = feat.get("geometry", {}).get("coordinates", [[]])[0]
        if len(ring) >= 3:
            polys.append([project(lat, lon) for lon, lat in ring])
    return polys


def load_lanes(lanes_json: dict) -> dict:
    """lanes.json → dict of lane_id → {"waypoints_xy": [...], "entry_xy": ..., "exit_xy": ...}."""
    result = {}
    for lane in lanes_json.get("lanes", []):
        wps = [project(wp["lat"], wp["lon"]) for wp in lane.get("waypoints", [])
               if wp.get("lat") and wp.get("lon")]
        entry = lane.get("entry_gate", {})
        exit_ = lane.get("exit_gate", {})
        result[lane["id"]] = {
            "waypoints_xy": wps,
            "entry_xy": project(entry["lat"], entry["lon"]) if entry.get("lat") else None,
            "exit_xy": project(exit_["lat"], exit_["lon"]) if exit_.get("lat") else None,
        }
    return result

