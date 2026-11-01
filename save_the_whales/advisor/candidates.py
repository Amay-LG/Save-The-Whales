"""Bounded route geometries: the current plan, a baseline return, and dogleg templates (F7, F10).

At most four geometries per threatened ship:

- ``CURRENT``: the remaining current validated path (no geometry change).
- ``RETURN``: only while off-route, a direct forward connection to the common rejoin (the slot
  reserved for a baseline-return alternative).
- Doglegs around the hazard interval [s_begin, s_end] (arc length along the baseline):
  an approach/bypass waypoint at ``s_begin - half_length`` and a departure waypoint at
  ``s_end + half_length``, each offset perpendicular to the local baseline direction, then the explicit
  rejoin. On-route: left and right at the first offset plus one larger-offset fallback; off-route: the
  first and larger offsets on the preferred side.

Speeds: service plus the configured lower speeds that lie within [min_speed, service], deduplicated, at
most three; the current plan also keeps its present speed. That caps evaluation at 12 candidates.
"""
from __future__ import annotations

import math
from bisect import bisect_right
from typing import Any, Dict, List, Optional, Sequence, Tuple

XY = Tuple[float, float]
CURRENT, RETURN = "CURRENT", "RETURN"
DOGLEG_LEFT, DOGLEG_RIGHT, DOGLEG_WIDE = "DOGLEG_LEFT", "DOGLEG_RIGHT", "DOGLEG_WIDE"


class Polyline:
    """A baseline in projected meters with cumulative arc length."""

    def __init__(self, xy: Sequence[XY]):
        self.xy = list(xy)
        self.arcs = [0.0]
        for a, b in zip(self.xy, self.xy[1:]):
            self.arcs.append(self.arcs[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))

    @property
    def length(self) -> float:
        return self.arcs[-1]

    def project(self, p: XY, from_index: int = 0) -> Tuple[float, float]:
        """(arc, distance) of the closest point on segments starting at ``from_index``."""
        best = (self.arcs[min(from_index, len(self.xy) - 1)], math.inf)
        for i in range(max(0, from_index), len(self.xy) - 1):
            a, b = self.xy[i], self.xy[i + 1]
            ex, ey = b[0] - a[0], b[1] - a[1]
            L2 = ex * ex + ey * ey
            s = 0.0 if L2 <= 0 else max(0.0, min(1.0, ((p[0] - a[0]) * ex + (p[1] - a[1]) * ey) / L2))
            d = math.hypot(a[0] + ex * s - p[0], a[1] + ey * s - p[1])
            if d < best[1] - 1e-9:
                best = (self.arcs[i] + s * math.sqrt(L2), d)
        return best

    def at(self, s: float) -> Tuple[XY, XY]:
        """Point and unit tangent at arc ``s`` (clamped to the polyline)."""
        s = max(0.0, min(self.length, s))
        i = min(max(0, bisect_right(self.arcs, s) - 1), len(self.xy) - 2)
        while i < len(self.xy) - 2 and self.arcs[i + 1] - self.arcs[i] <= 0:
            i += 1
        a, b = self.xy[i], self.xy[i + 1]
        seg = self.arcs[i + 1] - self.arcs[i]
        f = 0.0 if seg <= 0 else (s - self.arcs[i]) / seg
        t = (0.0, 0.0) if seg <= 0 else ((b[0] - a[0]) / seg, (b[1] - a[1]) / seg)
        return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f), t

    def side_of(self, s: float, p: XY) -> float:
        """Signed lateral offset of ``p`` at arc ``s``: positive = left of travel direction."""
        (x, y), (tx, ty) = self.at(s)
        return tx * (p[1] - y) - ty * (p[0] - x)


def offset_point(line: Polyline, s: float, offset_m: float) -> XY:
    (x, y), (tx, ty) = line.at(s)
    return (x - ty * offset_m, y + tx * offset_m)     # left normal = (-ty, tx)


def dogleg(line: Polyline, s_begin: float, s_end: float, offset_m: float, half_length_m: float,
           s_min: float, s_max: float) -> Optional[List[XY]]:
    """[approach/bypass, departure] waypoints, or None when the hazard leaves no room."""
    a = max(s_begin - half_length_m, s_min)
    b = min(s_end + half_length_m, s_max)
    if b - a < 1.0:
        return None
    return [offset_point(line, a, offset_m), offset_point(line, b, offset_m)]


def speed_set(service: float, current: float, cfg: Dict[str, Any]) -> Tuple[List[float], List[float]]:
    """(speeds for new geometries, speeds for the current geometry), each at most three, deduplicated."""
    lo = float(cfg["min_speed_knots"])
    base = [service] + [float(s) for s in cfg["alt_speeds_knots"] if lo <= float(s) < service]
    base = list(dict.fromkeys(round(s, 6) for s in base))[: int(cfg["max_speeds"])]
    cur = list(dict.fromkeys([round(current, 6)] + base))[: int(cfg["max_speeds"])]
    return base, cur
