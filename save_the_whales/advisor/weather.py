"""Exact maximum-severity integration on piecewise linear timed paths."""
from ._common import finite, point


def _geometry(geometry):
    if geometry is None:
        from save_the_whales.shared import geo
        geometry = geo
    if not callable(getattr(geometry, "segment_zone_intervals", None)):
        raise RuntimeError("Shared geometry.segment_zone_intervals is not implemented")
    return geometry


def weather_details(legs, weather, *, geometry=None):
    """Return (severity_seconds, blocked); unknown/invalid fields fail closed."""
    geometry = _geometry(geometry)
    if not isinstance(weather, dict) or weather.get("source") != "SIMULATED":
        raise ValueError("UNKNOWN_WEATHER")
    start, end = weather.get("coverage_start_s"), weather.get("coverage_end_s")
    if not finite(start) or not finite(end) or end < start or not isinstance(weather.get("zones"), list):
        raise ValueError("UNKNOWN_WEATHER")
    zones = weather["zones"]
    if len(zones) > 32:
        raise ValueError("UNKNOWN_WEATHER")
    for z in zones:
        if (not isinstance(z, dict) or not isinstance(z.get("polygon"), list)
            or not 3 <= len(z["polygon"]) <= 256 or not all(point(p) for p in z["polygon"])
            or not finite(z.get("severity")) or not 0 <= z["severity"] <= 1
            or not finite(z.get("active_from_s")) or not finite(z.get("active_until_s"))
            or z["active_until_s"] < z["active_from_s"] or not isinstance(z.get("blocked"), bool)):
            raise ValueError("UNKNOWN_WEATHER")
        ring = [geometry.project(p) for p in z["polygon"]]
        area = sum(x*v-u*y for (x, y), (u, v) in zip(ring, ring[1:]+ring[:1]))
        if abs(area) <= 1e-6:
            raise ValueError("UNKNOWN_WEATHER")
    exposure, blocked = 0.0, False
    for leg in legs:
        a, b = leg.get("start_s"), leg.get("end_s")
        if (not finite(a) or not finite(b) or b < a or not point(leg.get("start"))
            or not point(leg.get("end")) or a < start or b > end):
            raise ValueError("UNKNOWN_WEATHER")
        spans = []
        for z in zones:
            for lo, hi in geometry.segment_zone_intervals(leg["start"], leg["end"], z["polygon"]):
                if not finite(lo) or not finite(hi) or not 0 <= lo <= hi <= 1:
                    raise ValueError("UNKNOWN_WEATHER")
                lo = max(a+(b-a)*lo, z["active_from_s"])
                hi = min(a+(b-a)*hi, z["active_until_s"])
                # Hard blocks include boundary touches during an active interval.
                if lo <= hi and a <= hi and lo <= b and lo < z["active_until_s"] and hi >= z["active_from_s"]:
                    blocked |= z["blocked"]
                    if hi > lo:
                        spans.append((lo, hi, z["severity"]))
        cuts = sorted({v for lo, hi, _ in spans for v in (lo, hi)})
        for lo, hi in zip(cuts, cuts[1:]):
            middle = (lo+hi)/2
            exposure += (hi-lo)*max((s for l, h, s in spans if l <= middle < h), default=0)
    return exposure, blocked


def weather_exposure(legs, weather, *, geometry=None):
    """Severity-seconds, not a probability. Outside coverage raises UNKNOWN_WEATHER.

    The public two-argument call uses Person 1's shared geometry. Tests and the
    planner can inject the same facade via the optional keyword-only argument.
    Hard-block rejection is separate from this diagnostic integral.
    """
    return weather_details(legs, weather, geometry=geometry)[0]
