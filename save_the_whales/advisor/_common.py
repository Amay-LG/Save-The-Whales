"""Private numerical conventions; no simulator, network, or UI dependencies."""
from datetime import datetime, timezone, timedelta
from math import isfinite, hypot

KNOT = 1852 / 3600
DEFAULTS = dict(lookahead_s=900, max_rejoin_s=3600, offsets_m=[500, 1000],
    clearance_margin_m=100, min_speed_knots=6, max_path_ratio=1.5,
    max_added_delay_s=900, w_whale=5, w_weather=2, w_delay=1,
    w_distance=.5, w_change=.1, min_objective_improvement=.01,
    min_whale_improvement=.05, whale_risk_tolerance=.01, cooldown_s=60,
    max_detection_age_s=1800, max_reports=50, drift_mps=1.5,
    watch_distance_m=500, watch_risk=.15, weather_trigger_s=1,
    lease_s=900, stale_position_tolerance_m=100, max_route_points=256)
UNMODELED = ["F11_SHIP_SHIP", "F12_MARITIME_LEGALITY", "F13_DEPTH", "F14_DYNAMICS"]


def settings(config):
    out = dict(DEFAULTS)
    out.update(config.get("sensors", {}))
    out.update(config.get("planner", {}))
    try:
        out["max_reports"] = min(50, int(out["max_reports"]))
        out["max_route_points"] = min(256, int(out["max_route_points"]))
    except (ValueError, TypeError, OverflowError):
        raise ValueError("INVALID_CONFIG") from None
    for key in DEFAULTS:
        if key == "offsets_m":
            if not out[key] or any(not finite(x) or not 0 < x <= 5000 for x in out[key]):
                raise ValueError("INVALID_CONFIG")
        elif not finite(out[key]) or not 0 <= out[key] <= 1e9:
            raise ValueError("INVALID_CONFIG")
    if out["min_speed_knots"] <= 0 or out["max_reports"] < 1 or out["max_path_ratio"] < 1:
        raise ValueError("INVALID_CONFIG")
    if out["max_route_points"] < 2 or out["lease_s"] <= 0:
        raise ValueError("INVALID_CONFIG")
    if not 0 < out["lookahead_s"] <= out["max_rejoin_s"] <= 3600 or out["lease_s"] > 3600:
        raise ValueError("INVALID_CONFIG")
    if out["drift_mps"] > 50 or out["max_detection_age_s"] > 86400:
        raise ValueError("INVALID_CONFIG")
    return out


def finite(x):
    return isinstance(x, (float, int)) and not isinstance(x, bool) and isfinite(x)


def point(p):
    return (isinstance(p, dict) and finite(p.get("lat")) and finite(p.get("lon"))
            and -90 <= p["lat"] <= 90 and -180 <= p["lon"] <= 180)


def utc(value):
    if not isinstance(value, str):
        raise ValueError("INVALID_TIME")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None or dt.utcoffset() != timedelta(0):
        raise ValueError("INVALID_TIME")
    return dt.astimezone(timezone.utc)


def stamp(dt):
    return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")


def distance(a, b, geometry):
    x, y = geometry.project(a)
    u, v = geometry.project(b)
    return hypot(u-x, v-y)


def length(path, geometry):
    return sum(distance(a, b, geometry) for a, b in zip(path, path[1:]))


def same(a, b):
    return abs(a["lat"]-b["lat"]) < 1e-9 and abs(a["lon"]-b["lon"]) < 1e-9


def prefix_to(path, endpoint):
    for i, p in enumerate(path):
        if same(p, endpoint):
            return path[:i+1]
    raise ValueError("MISSING_REJOIN")


def advance(position, remaining, speed, seconds, geometry):
    """Traverse existing intent; carry travel through any number of waypoints."""
    pos, pending, travelled = dict(position), list(remaining), 0.0
    left = speed*KNOT*seconds
    while pending:
        d = distance(pos, pending[0], geometry)
        if d <= left + 1e-8:
            pos = dict(pending.pop(0))
            travelled += d
            left = max(0, left-d)
        else:
            x, y = geometry.project(pos)
            u, v = geometry.project(pending[0])
            pos = geometry.unproject(x+(u-x)*left/d, y+(v-y)*left/d)
            travelled += left
            break
    return pos, pending, travelled


def timed_path(path, start_s, target_speed, service_speed, lease_s, geometry,
               *, initial_speed=None, delay_s=0):
    """Split at response and lease boundaries, including stationary waits.

    Path already contains the response prefix. During it the old speed applies;
    after the lease every remaining leg uses service speed (same geometry).
    """
    legs, now, elapsed = [], start_s, 0.0
    if len(path) < 2:
        return legs
    for a, b in zip(path, path[1:]):
        pos = a
        remaining = distance(pos, b, geometry)
        while remaining > 1e-7:
            if elapsed < delay_s-1e-8:
                speed, boundary = initial_speed, delay_s
            elif elapsed < lease_s-1e-8:
                speed, boundary = target_speed, lease_s
            else:
                speed, boundary = service_speed, float("inf")
            if speed is None or speed < 0:
                raise ValueError("INVALID_SPEED")
            if speed == 0:
                if not isfinite(boundary):
                    raise ValueError("INVALID_SPEED")
                dt, end = boundary-elapsed, pos
            else:
                dt = min(remaining/(speed*KNOT), boundary-elapsed)
                fraction = min(1, dt*speed*KNOT/remaining)
                x, y = geometry.project(pos)
                u, v = geometry.project(b)
                end = b if fraction >= 1-1e-10 else geometry.unproject(x+(u-x)*fraction, y+(v-y)*fraction)
            legs.append(dict(start=dict(pos), end=dict(end), start_s=now,
                             end_s=now+dt, speed_knots=speed))
            remaining = distance(end, b, geometry)
            now += dt
            elapsed += dt
            pos = end
    return legs
