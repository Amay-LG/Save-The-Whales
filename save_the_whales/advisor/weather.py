"""Simulated-weather exposure along timed legs and blocked-zone checks (F3).

``weather_exposure(legs, weather)`` integrates the **maximum** active severity over time, in
severity-seconds. Each leg is intersected with each zone polygon (``segment_zone_intervals``), mapped
to absolute virtual times, intersected with the zone's half-open active window, and overlaps are
partitioned so overlapping zones contribute max(severity), never a sum. Clear weather contributes zero;
any leg outside the field's coverage raises ``ValueError("UNKNOWN_WEATHER")`` instead of returning 0.

A blocked zone is a hard rejection, independent of its soft score: ``blocked_hits`` reports any leg
that touches or enters an active blocked zone, including a single boundary contact.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from save_the_whales.shared import contracts as C

INVALID_LEG = "INVALID_LEG"
INVALID_WEATHER = "INVALID_WEATHER"
WEATHER_BLOCKED = "WEATHER_BLOCKED"


def _geometry(geometry):
    if geometry is None:
        from save_the_whales.shared import geo
        return geo
    return geometry


def _check_field(weather: Any) -> None:
    if not isinstance(weather, dict) or not isinstance(weather.get("zones"), list):
        raise ValueError(C.UNKNOWN_WEATHER)
    for k in ("coverage_start_s", "coverage_end_s"):
        v = weather.get(k)
        if not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError(C.UNKNOWN_WEATHER)
    for z in weather["zones"]:
        sev = z.get("severity")
        if not isinstance(sev, (int, float)) or not 0.0 <= sev <= 1.0:
            raise ValueError(INVALID_WEATHER)


def _leg_times(leg: Dict[str, Any]) -> Tuple[float, float]:
    s0, s1 = leg.get("start_s"), leg.get("end_s")
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (s0, s1)) or s1 < s0:
        raise ValueError(INVALID_LEG)
    return float(s0), float(s1)


def zone_time_intervals(legs: Sequence[Dict[str, Any]], weather: Dict[str, Any], geometry=None
                        ) -> List[Tuple[float, float, Dict[str, Any], int, float, float]]:
    """(t0, t1, zone, leg_index, f0, f1) for every leg/zone overlap (closed intervals, before the
    active-window cut). Raises UNKNOWN_WEATHER for legs outside coverage."""
    geo = _geometry(geometry)
    _check_field(weather)
    out = []
    for i, leg in enumerate(legs):
        s0, s1 = _leg_times(leg)
        if s0 < weather["coverage_start_s"] or s1 > weather["coverage_end_s"]:
            raise ValueError(C.UNKNOWN_WEATHER)
        for z in weather["zones"]:
            for f0, f1 in geo.segment_zone_intervals(leg["start"], leg["end"], z["polygon"]):
                out.append((s0 + f0 * (s1 - s0), s0 + f1 * (s1 - s0), z, i, f0, f1))
    return out


def max_severity_integral(pieces: Sequence[Tuple[float, float, float]]) -> float:
    """Integral of max(severity) over the union of (t0, t1, severity) pieces."""
    cuts = sorted({p[0] for p in pieces} | {p[1] for p in pieces})
    total = 0.0
    for a, b in zip(cuts, cuts[1:]):
        if b > a:
            total += (b - a) * max((sev for lo, hi, sev in pieces if lo <= a and hi >= b), default=0.0)
    return total


def weather_exposure(legs: Sequence[Dict[str, Any]], weather: Dict[str, Any], *, geometry=None) -> float:
    """Integral of max active severity dt along timed legs (severity-seconds)."""
    pieces = []
    for t0, t1, z, _, _, _ in zone_time_intervals(legs, weather, geometry):
        a, b = max(t0, float(z["active_from_s"])), min(t1, float(z["active_until_s"]))
        if b > a:
            pieces.append((a, b, float(z["severity"])))
    return max_severity_integral(pieces)


def blocked_hits(legs: Sequence[Dict[str, Any]], weather: Dict[str, Any], *, geometry=None) -> List[str]:
    """Zone IDs of active blocked zones that any leg touches or enters while active."""
    hits = []
    for t0, t1, z, _, _, _ in zone_time_intervals(legs, weather, geometry):
        if z.get("blocked") and t0 < float(z["active_until_s"]) and t1 >= float(z["active_from_s"]):
            if z["zone_id"] not in hits:
                hits.append(z["zone_id"])
    return hits


def exposure_span(legs: Sequence[Dict[str, Any]], weather: Dict[str, Any], *, geometry=None
                  ) -> Optional[Tuple[Tuple[int, float], Tuple[int, float], Dict[str, Any]]]:
    """First and last (leg_index, fraction) inside an active zone, plus the most severe zone; None if
    the legs never enter active weather. Used to place weather doglegs."""
    first = last = worst = None
    for t0, t1, z, i, f0, f1 in zone_time_intervals(legs, weather, geometry):
        a, b = max(t0, float(z["active_from_s"])), min(t1, float(z["active_until_s"]))
        if b < a or (b == a and not z.get("blocked")):
            continue
        s0, s1 = float(legs[i]["start_s"]), float(legs[i]["end_s"])
        span = s1 - s0
        fa = f0 if span <= 0 else (a - s0) / span
        fb = f1 if span <= 0 else (b - s0) / span
        if first is None or (i, fa) < first:
            first = (i, fa)
        if last is None or (i, fb) > last:
            last = (i, fb)
        if worst is None or float(z["severity"]) > float(worst["severity"]):
            worst = z
    return None if first is None else (first, last, worst)
