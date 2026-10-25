"""Fixed local projection, unit helpers and planar geometry shared by every owner.

All geometry runs in meters on one fixed tangent plane centred on (34.2, -119.75), including points on
both sides of longitude -120 (no UTM zone switching):

    x = R * radians(lon + 119.75) * cos(radians(34.2)),   y = R * radians(lat - 34.2),   R = 6_371_000 m

Public points are ``{"lat", "lon"}``; GeoJSON ``[lon, lat]`` is handled in ``sources.land`` only.

Numeric tolerance: ``EPS_M`` (1e-6 m) is the single geometric epsilon. A distance <= EPS_M counts as
contact, and a clearance <= ``clearance_m + EPS_M`` is rejected, so boundary cases fail conservatively.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from save_the_whales.shared import contracts as C

R_M = C.EARTH_RADIUS_M
LAT0 = C.PROJECTION_ORIGIN["lat"]
LON0 = C.PROJECTION_ORIGIN["lon"]
_COS0 = math.cos(math.radians(LAT0))
EPS_M = 1e-6

XY = Tuple[float, float]


# =====================================================================================================
# Projection and units
# =====================================================================================================
def project(point: Dict[str, float]) -> XY:
    """``{"lat","lon"}`` -> (x_m, y_m) on the shared tangent plane."""
    return (R_M * math.radians(point["lon"] - LON0) * _COS0, R_M * math.radians(point["lat"] - LAT0))


def unproject(x_m: float, y_m: float) -> Dict[str, float]:
    """(x_m, y_m) -> ``{"lat","lon"}``; exact inverse of ``project``."""
    return {"lat": LAT0 + math.degrees(y_m / R_M), "lon": LON0 + math.degrees(x_m / (R_M * _COS0))}


def knots_to_mps(speed_knots: float) -> float:
    return speed_knots * C.KNOT_MPS


def mps_to_knots(speed_mps: float) -> float:
    return speed_mps / C.KNOT_MPS


def distance_m(a: Dict[str, float], b: Dict[str, float]) -> float:
    """Planar distance between two points in meters."""
    (ax, ay), (bx, by) = project(a), project(b)
    return math.hypot(bx - ax, by - ay)


def path_length_m(path: Sequence[Dict[str, float]]) -> float:
    return sum(distance_m(a, b) for a, b in zip(path, path[1:]))


def ship_clearance_m(length_m: float, beam_m: float, margin_m: float) -> float:
    """Conservative enclosing-circle hull radius plus margin: 0.5*hypot(length, beam) + margin."""
    return 0.5 * math.hypot(length_m, beam_m) + margin_m


def is_finite_point(p: Any) -> bool:
    try:
        return math.isfinite(float(p["lat"])) and math.isfinite(float(p["lon"]))
    except (KeyError, TypeError, ValueError):
        return False


# =====================================================================================================
# Planar primitives (XY tuples, meters)
# =====================================================================================================
def _cross(ox: float, oy: float, ax: float, ay: float, bx: float, by: float) -> float:
    return (ax - ox) * (by - oy) - (ay - oy) * (bx - ox)


def point_segment_distance(p: XY, a: XY, b: XY) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def segments_properly_cross(a: XY, b: XY, c: XY, d: XY) -> bool:
    """True when segments cross at a single interior point of both (touching/collinear excluded)."""
    o1, o2 = _cross(*a, *b, *c), _cross(*a, *b, *d)
    o3, o4 = _cross(*c, *d, *a), _cross(*c, *d, *b)
    return o1 * o2 < 0 and o3 * o4 < 0


def segment_distance(a: XY, b: XY, c: XY, d: XY) -> float:
    """Minimum distance between segments ab and cd (0 when they cross or touch)."""
    if segments_properly_cross(a, b, c, d):
        return 0.0
    return min(point_segment_distance(a, c, d), point_segment_distance(b, c, d),
               point_segment_distance(c, a, b), point_segment_distance(d, a, b))


def normalize_ring_xy(ring: Sequence[XY]) -> List[XY]:
    """Drop consecutive duplicates and the closing vertex."""
    out: List[XY] = []
    for p in ring:
        if not out or math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) > EPS_M:
            out.append((float(p[0]), float(p[1])))
    if len(out) > 1 and math.hypot(out[0][0] - out[-1][0], out[0][1] - out[-1][1]) <= EPS_M:
        out.pop()
    return out


def point_in_ring(p: XY, ring: Sequence[XY]) -> bool:
    """Strict interior test by ray casting (boundary points may go either way; test them separately)."""
    x, y = p
    inside = False
    n = len(ring)
    for i in range(n):
        (x1, y1), (x2, y2) = ring[i], ring[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def point_ring_boundary_distance(p: XY, ring: Sequence[XY]) -> float:
    n = len(ring)
    return min(point_segment_distance(p, ring[i], ring[(i + 1) % n]) for i in range(n))


def point_in_or_on_ring(p: XY, ring: Sequence[XY]) -> bool:
    return point_ring_boundary_distance(p, ring) <= EPS_M or point_in_ring(p, ring)


# =====================================================================================================
# Land clearance (F1/F4)
# =====================================================================================================
_PREPARED: Dict[tuple, Dict[str, Any]] = {}
_PREPARED_MAX = 8


def _land_fingerprint(land: Dict[str, Any]) -> tuple:
    rings = land.get("rings") or []
    head = tuple((len(r), r[0]["lat"], r[0]["lon"], r[-1]["lat"], r[-1]["lon"]) for r in rings if r)
    return (land.get("map_id"), land.get("sha256"), len(rings), head)


def _prepare_land(land: Dict[str, Any]) -> Dict[str, Any]:
    """Project land rings once and build edge arrays; cached by map fingerprint."""
    key = _land_fingerprint(land)
    hit = _PREPARED.get(key)
    if hit is not None:
        return hit
    rings_xy = []
    for ring in land.get("rings") or []:
        xy = normalize_ring_xy([project(p) for p in ring])
        if len(xy) >= 3:
            rings_xy.append(xy)
    starts, ends = [], []
    for ring in rings_xy:
        for i in range(len(ring)):
            starts.append(ring[i])
            ends.append(ring[(i + 1) % len(ring)])
    prepared = {
        "rings": rings_xy,
        "a": np.array(starts, dtype=float).reshape(-1, 2),
        "b": np.array(ends, dtype=float).reshape(-1, 2),
        "ring_bboxes": [(min(x for x, _ in r), min(y for _, y in r), max(x for x, _ in r),
                         max(y for _, y in r)) for r in rings_xy],
    }
    if len(_PREPARED) >= _PREPARED_MAX:
        _PREPARED.pop(next(iter(_PREPARED)))
    _PREPARED[key] = prepared
    return prepared


def _point_to_edges(px: float, py: float, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Distance from one point to each edge a[i]->b[i]."""
    dx, dy = b[:, 0] - a[:, 0], b[:, 1] - a[:, 1]
    l2 = dx * dx + dy * dy
    t = np.clip(((px - a[:, 0]) * dx + (py - a[:, 1]) * dy) / np.where(l2 > 0, l2, 1.0), 0.0, 1.0)
    return np.hypot(px - (a[:, 0] + t * dx), py - (a[:, 1] + t * dy))


def _points_to_segment(pts: np.ndarray, p0: XY, p1: XY) -> np.ndarray:
    """Distance from each point to the single segment p0->p1."""
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    l2 = dx * dx + dy * dy
    t = np.clip(((pts[:, 0] - p0[0]) * dx + (pts[:, 1] - p0[1]) * dy) / (l2 if l2 > 0 else 1.0), 0.0, 1.0)
    return np.hypot(pts[:, 0] - (p0[0] + t * dx), pts[:, 1] - (p0[1] + t * dy))


def _leg_edges(p0: XY, p1: XY, a: np.ndarray, b: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Distances from leg p0->p1 to every land edge (0 where they cross), and the proper-crossing mask."""
    (x0, y0), (x1, y1) = p0, p1
    o1 = (x1 - x0) * (a[:, 1] - y0) - (y1 - y0) * (a[:, 0] - x0)
    o2 = (x1 - x0) * (b[:, 1] - y0) - (y1 - y0) * (b[:, 0] - x0)
    o3 = (b[:, 0] - a[:, 0]) * (y0 - a[:, 1]) - (b[:, 1] - a[:, 1]) * (x0 - a[:, 0])
    o4 = (b[:, 0] - a[:, 0]) * (y1 - a[:, 1]) - (b[:, 1] - a[:, 1]) * (x1 - a[:, 0])
    crossing = (o1 * o2 < 0) & (o3 * o4 < 0)
    d = np.minimum.reduce([_point_to_edges(x0, y0, a, b), _point_to_edges(x1, y1, a, b),
                           _points_to_segment(a, p0, p1), _points_to_segment(b, p0, p1)])
    return np.where(crossing, 0.0, d), crossing


def check_water_path(path: Sequence[Dict[str, float]], land: Dict[str, Any], clearance_m: float) -> Dict[str, Any]:
    """Check a whole path (every vertex and every leg) against land with a hull clearance radius.

    Returns ``{"ok", "min_clearance_m", "reasons", "worst_leg_index"}``. Reasons, in order of severity:
    ``INVALID_MAP`` (map missing, unvalidated or empty), ``INVALID_PATH`` / ``NON_FINITE_COORDINATE``,
    ``POINT_ON_LAND`` (a vertex inside land), ``LAND_CROSSING`` (a leg crosses a land edge),
    ``LAND_CONTACT`` (touching or collinear contact), ``INSUFFICIENT_CLEARANCE`` (closest land edge at
    or within ``clearance_m + EPS_M``). ``min_clearance_m`` is the actual minimum distance from the path
    to land (0.0 when on/through land; None when it can't be evaluated). A one-point path is checked as
    a stationary position.
    """
    if not math.isfinite(clearance_m) or clearance_m < 0:
        raise ValueError("clearance_m must be a finite non-negative number")
    if not land or not land.get("validated") or not land.get("rings"):
        return {"ok": False, "min_clearance_m": None, "reasons": [C.INVALID_MAP], "worst_leg_index": None}
    if not isinstance(path, (list, tuple)) or len(path) == 0:
        return {"ok": False, "min_clearance_m": None, "reasons": [C.INVALID_PATH], "worst_leg_index": None}
    if not all(is_finite_point(p) for p in path):
        return {"ok": False, "min_clearance_m": None, "reasons": [C.NON_FINITE_COORDINATE],
                "worst_leg_index": None}

    prepared = _prepare_land(land)
    if not prepared["rings"]:
        return {"ok": False, "min_clearance_m": None, "reasons": [C.INVALID_MAP], "worst_leg_index": None}
    pts = [project(p) for p in path]
    legs = list(zip(pts, pts[1:])) or [(pts[0], pts[0])]

    reasons = set()
    for idx, p in enumerate(pts):
        for ring, (minx, miny, maxx, maxy) in zip(prepared["rings"], prepared["ring_bboxes"]):
            if minx <= p[0] <= maxx and miny <= p[1] <= maxy and point_in_ring(p, ring) \
                    and point_ring_boundary_distance(p, ring) > EPS_M:   # boundary = contact, not inside
                reasons.add(C.POINT_ON_LAND)

    best, worst_leg = math.inf, None
    for i, (p0, p1) in enumerate(legs):
        d, crossing = _leg_edges(p0, p1, prepared["a"], prepared["b"])
        dmin = float(d.min())
        if crossing.any():
            reasons.add(C.LAND_CROSSING)
        if ((d <= EPS_M) & ~crossing).any():
            reasons.add(C.LAND_CONTACT)
        if dmin > EPS_M and dmin <= clearance_m + EPS_M:
            reasons.add(C.INSUFFICIENT_CLEARANCE)
        if dmin < best:
            best, worst_leg = dmin, i
    if C.POINT_ON_LAND in reasons:
        best = 0.0
        worst_leg = worst_leg if worst_leg is not None else 0
    order = [C.POINT_ON_LAND, C.LAND_CROSSING, C.LAND_CONTACT, C.INSUFFICIENT_CLEARANCE]
    return {
        "ok": not reasons,
        "min_clearance_m": None if best == math.inf else max(0.0, best),
        "reasons": [r for r in order if r in reasons],
        "worst_leg_index": worst_leg,
    }


# =====================================================================================================
# Segment / zone intervals (for Person 3's weather integral)
# =====================================================================================================
def segment_zone_intervals(start: Dict[str, float], end: Dict[str, float],
                           polygon: Sequence[Dict[str, float]]) -> List[Tuple[float, float]]:
    """Fractions [t0, t1] of segment start->end lying inside or on ``polygon`` (all crossings).

    - Intervals are sorted, merged and clipped to [0, 1]; a segment missing the polygon returns [].
    - Boundary counts as inside, so a leg running along an edge is "inside", and a single touching
      point is returned as a zero-length interval (t, t): blocked-zone contact never passes silently,
      while it contributes no exposure time.
    - A zero-length segment returns [(0.0, 1.0)] when inside/on the polygon (a stationary leg still
      accumulates exposure), else [].
    Raises ``ValueError("INVALID_POLYGON")`` for fewer than 3 distinct vertices or non-finite input.
    """
    if not (is_finite_point(start) and is_finite_point(end)) or not all(is_finite_point(p) for p in polygon):
        raise ValueError(C.INVALID_POLYGON)
    ring = normalize_ring_xy([project(p) for p in polygon])
    if len(ring) < 3:
        raise ValueError(C.INVALID_POLYGON)
    a, b = project(start), project(end)
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if length <= EPS_M:
        return [(0.0, 1.0)] if point_in_or_on_ring(a, ring) else []

    teps = EPS_M / length
    ts = {0.0, 1.0}
    n = len(ring)
    for i in range(n):
        p, q = ring[i], ring[(i + 1) % n]
        ex, ey = q[0] - p[0], q[1] - p[1]
        denom = dx * ey - dy * ex
        if abs(denom) > 1e-12 * length * math.hypot(ex, ey):
            t = ((p[0] - a[0]) * ey - (p[1] - a[1]) * ex) / denom
            u = ((p[0] - a[0]) * dy - (p[1] - a[1]) * dx) / denom
            if -teps <= t <= 1 + teps and -1e-12 <= u <= 1 + 1e-12:
                ts.add(min(1.0, max(0.0, t)))
        for v in (p, q):   # polygon vertices on the segment (tangent or collinear contact)
            if point_segment_distance(v, a, b) <= EPS_M:
                ts.add(min(1.0, max(0.0, ((v[0] - a[0]) * dx + (v[1] - a[1]) * dy) / (length * length))))

    at = lambda t: (a[0] + dx * t, a[1] + dy * t)  # noqa: E731
    params = sorted(ts)
    intervals: List[List[float]] = []
    for t0, t1 in zip(params, params[1:]):
        if t1 - t0 > teps and point_in_or_on_ring(at((t0 + t1) / 2), ring):
            intervals.append([t0, t1])
    for t in params:   # isolated boundary contacts
        if not any(lo - teps <= t <= hi + teps for lo, hi in intervals) and \
                point_ring_boundary_distance(at(t), ring) <= EPS_M:
            intervals.append([t, t])
    intervals.sort()
    merged: List[List[float]] = []
    for lo, hi in intervals:
        if merged and lo <= merged[-1][1] + teps:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return [(max(0.0, lo), min(1.0, hi)) for lo, hi in merged]
