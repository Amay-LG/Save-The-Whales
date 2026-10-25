"""PRIVATE TEST facade. Synthetic polygons, never a real coastal-data claim."""
from math import radians, degrees, cos, hypot


def project(p):
    return (6371000*radians(p["lon"]+119.75)*cos(radians(34.2)),
            6371000*radians(p["lat"]-34.2))


def unproject(x, y):
    return dict(lat=34.2+degrees(y/6371000), lon=-119.75+degrees(x/(6371000*cos(radians(34.2)))))


def ship_clearance_m(length_m, beam_m, margin_m):
    return .5*hypot(length_m, beam_m)+margin_m


def _cross(a, b):
    return a[0]*b[1]-a[1]*b[0]


def _sub(a, b):
    return a[0]-b[0], a[1]-b[1]


def _point_segment(p, a, b):
    d = _sub(b, a)
    size = d[0]*d[0]+d[1]*d[1]
    t = 0 if not size else max(0, min(1, ((p[0]-a[0])*d[0]+(p[1]-a[1])*d[1])/size))
    return hypot(p[0]-a[0]-t*d[0], p[1]-a[1]-t*d[1])


def _inside(p, ring):
    inside = False
    for a, b in zip(ring, ring[1:]+ring[:1]):
        if _point_segment(p, a, b) <= 1e-7:
            return True
        if (a[1] > p[1]) != (b[1] > p[1]):
            if p[0] < (b[0]-a[0])*(p[1]-a[1])/(b[1]-a[1])+a[0]:
                inside = not inside
    return inside


def segment_zone_intervals(start, end, polygon):
    a, b = project(start), project(end)
    ring, cuts = [project(p) for p in polygon], {0.0, 1.0}
    d = _sub(b, a)
    if hypot(*d) < 1e-9:
        return [(0., 1.)] if _inside(a, ring) else []
    for p, q in zip(ring, ring[1:]+ring[:1]):
        e, offset = _sub(q, p), _sub(p, a)
        denominator = _cross(d, e)
        if abs(denominator) > 1e-8:
            t, u = _cross(offset, e)/denominator, _cross(offset, d)/denominator
            if -1e-9 <= t <= 1+1e-9 and -1e-9 <= u <= 1+1e-9:
                cuts.add(max(0, min(1, t)))
        elif abs(_cross(offset, d)) < 1e-7:
            size = d[0]*d[0]+d[1]*d[1]
            for vertex in (p, q):
                t = ((vertex[0]-a[0])*d[0]+(vertex[1]-a[1])*d[1])/size
                if 0 <= t <= 1:
                    cuts.add(t)
    cuts = sorted(cuts)
    intervals = [(lo, hi) for lo, hi in zip(cuts, cuts[1:])
                 if _inside((a[0]+d[0]*(lo+hi)/2, a[1]+d[1]*(lo+hi)/2), ring)]
    intervals += [(t, t) for t in cuts if _inside((a[0]+d[0]*t, a[1]+d[1]*t), ring)]
    merged = []
    for lo, hi in sorted(intervals):
        if merged and lo <= merged[-1][1]+1e-10:
            merged[-1] = merged[-1][0], max(hi, merged[-1][1])
        else:
            merged.append((lo, hi))
    return merged


def check_water_path(path, land, clearance_m):
    if not land.get("validated") or not land.get("rings"):
        return dict(ok=False, min_clearance_m=0, reasons=["INVALID_MAP"])
    minimum = float("inf")
    for a, b in zip(path, path[1:]):
        x, y = project(a), project(b)
        for ring in land["rings"]:
            if segment_zone_intervals(a, b, ring):
                minimum = 0
            xy = [project(p) for p in ring]
            for p, q in zip(xy, xy[1:]+xy[:1]):
                minimum = min(minimum, _point_segment(x, p, q), _point_segment(y, p, q),
                              _point_segment(p, x, y), _point_segment(q, x, y))
    return dict(ok=minimum > clearance_m+1e-6, min_clearance_m=minimum,
                reasons=[] if minimum > clearance_m+1e-6 else ["LAND_CLEARANCE"])
