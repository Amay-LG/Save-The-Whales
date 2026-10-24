"""Local tangent-plane projection (km) and pure-Python polygon tests (§4, §5.1)."""
import math

R_KM = 6371.0
KM_PER_NM = 1.852
LAT0, LON0 = 34.2, -119.75
_COS0 = math.cos(math.radians(LAT0))


def project(lat, lon):
    return (R_KM * math.radians(lon - LON0) * _COS0, R_KM * math.radians(lat - LAT0))


def unproject(x, y):
    return (LAT0 + math.degrees(y / R_KM), LON0 + math.degrees(x / (R_KM * _COS0)))


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * R_KM * math.asin(math.sqrt(h))


def point_in_polygon(pt, poly):
    """Ray casting. poly: list of (x, y) vertices, implicitly closed."""
    x, y = pt
    inside = False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def _orient(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def segments_intersect(a, b, c, d):
    return (_orient(a, b, c) * _orient(a, b, d) < 0) and (_orient(c, d, a) * _orient(c, d, b) < 0)


def segment_hits_polygon(a, b, poly):
    if point_in_polygon(a, poly) or point_in_polygon(b, poly):
        return True
    return any(segments_intersect(a, b, p, q) for p, q in zip(poly, poly[1:] + poly[:1]))


def path_hits_land(path, land_polys):
    return any(segment_hits_polygon(a, b, poly) for a, b in zip(path, path[1:]) for poly in land_polys)


def load_land(geojson):
    """GeoJSON FeatureCollection -> list of projected polygons (outer rings only)."""
    polys = []
    for f in geojson["features"]:
        ring = f["geometry"]["coordinates"][0]
        if len(ring) >= 3:
            polys.append([project(lat, lon) for lon, lat in ring])
    return polys
