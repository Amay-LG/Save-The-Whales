"""At most four route templates and three speeds, with explicit forward rejoin."""
from datetime import timedelta
from copy import deepcopy
from math import hypot
from ._common import distance, length, prefix_to, stamp, utc
from .constraints import choose_rejoin, _prefix_for_delay


def snapshot(ship, observation):
    active = ship.get("active_command")
    lease = {k: active[k] for k in ("command", "valid_until")} if active else None
    return dict(position=dict(lat=ship["lat"], lon=ship["lon"]),
                current_route=deepcopy(ship["current_route"]), sog_knots=ship["sog_knots"],
                sim_time_s=observation["sim_time_s"], decision_id=observation.get("decision_id"),
                now=observation["now"], service_speed_knots=ship["service_speed_knots"],
                active_command=lease,
                baseline_progress_index=ship["baseline_progress_index"])


def action_for(ship, observation, command, speed, points, rejoin, cfg, *, lease_s=None):
    return dict(ship_id=ship["ship_id"], issued_at=observation["now"], command=command,
                target_speed_knots=speed, detour_waypoints=deepcopy(points),
                rejoin_route_index=rejoin, revision=ship["latest_revision"]+1,
                action_id="pending", source_snapshot=snapshot(ship, observation),
                valid_until=stamp(utc(observation["now"])+timedelta(seconds=cfg["lease_s"] if lease_s is None else lease_s)),
                metadata={})


def _at(path, along, geometry):
    for a, b in zip(path, path[1:]):
        d = distance(a, b, geometry)
        if d > 1e-8:
            if along <= d:
                x, y = geometry.project(a)
                u, v = geometry.project(b)
                return x+(u-x)*max(0, along)/d, y+(v-y)*max(0, along)/d, (u-x)/d, (v-y)/d
            along -= d
    x, y = geometry.project(path[-1])
    u, v = geometry.project(path[-2])
    d = max(1e-8, hypot(x-u, y-v))
    return x, y, (x-u)/d, (y-v)/d


def _hazard_along(path, hazard, geometry):
    hx, hy = geometry.project(hazard)
    closest, along, result = float("inf"), 0, 0
    for a, b in zip(path, path[1:]):
        x, y = geometry.project(a)
        u, v = geometry.project(b)
        dx, dy = u-x, v-y
        d = hypot(dx, dy)
        if d < 1e-8:
            continue
        t = max(0, min(1, ((hx-x)*dx+(hy-y)*dy)/(d*d)))
        error = hypot(hx-x-t*dx, hy-y-t*dy)
        if error < closest:
            closest, result = error, along+t*d
        along += d
    return result


def route_templates(ship, observation, cfg, geometry, hazard=None):
    """Current intent, left/right doglegs, then larger dogleg OR baseline return."""
    k = choose_rejoin(ship, cfg, geometry)
    endpoint = ship["original_route"][k]
    prefix_to(ship["current_route"], endpoint)  # Explicitly require a known rejoin.
    templates = [dict(name="current", command="SLOWDOWN", points=[], rejoin=k, change=0)]
    delay = cfg.get("response_lag_s", 0)
    prefix, _, _ = _prefix_for_delay(ship, observation, delay, geometry)
    start = prefix[-1]
    baseline = [start]+ship["original_route"][ship["baseline_progress_index"]+1:k+1]
    total = length(baseline, geometry)
    if total <= 1:
        return templates
    center = total*.5 if hazard is None else _hazard_along(baseline, hazard, geometry)
    center = max(total*.15, min(total*.85, center))
    offsets = cfg["offsets_m"]
    for name, side, offset in (("left", 1, offsets[0]), ("right", -1, offsets[0]),
                               ("larger_left", 1, offsets[min(1, len(offsets)-1)])):
        width = min(total*.2, offset*1.5)
        locations = sorted({max(total*.05, center-width), min(total*.95, center+width)})
        points = []
        for along in locations:
            x, y, dx, dy = _at(baseline, along, geometry)
            points.append(geometry.unproject(x-side*dy*offset, y+side*dx*offset))
        templates.append(dict(name=name, command="DETOUR", points=points, rejoin=k, change=1))
    off_route = ship["current_route"] != ship["original_route"][ship["baseline_progress_index"]+1:]
    if off_route or (ship.get("active_command") or {}).get("command") == "DETOUR":
        templates[-1] = dict(name="baseline_return", command="RESUME", points=[], rejoin=k, change=1)
    seen, unique = set(), []
    for t in templates:
        key = (t["command"], tuple((p["lat"], p["lon"]) for p in t["points"]))
        if key not in seen:
            unique.append(t)
            seen.add(key)
    return unique[:4]


def generate_candidates(ship, observation, cfg, geometry, hazard=None):
    speeds = [ship["service_speed_knots"]]
    # Preserve the actual current speed in the bounded grid when already slowed.
    options = [ship["sog_knots"], 10, 8] if ship["sog_knots"] != speeds[0] else [10, 8]
    for speed in options:
        if cfg["min_speed_knots"] <= speed <= speeds[0] and speed not in speeds:
            speeds.append(speed)
        if len(speeds) == 3:
            break
    result = []
    for template in route_templates(ship, observation, cfg, geometry, hazard):
        for speed in speeds:
            if template["command"] == "RESUME" and speed != ship["service_speed_knots"]:
                continue
            command = template["command"]
            if (template["name"] == "current" and speed == ship["service_speed_knots"]
                and ship["current_route"] == ship["original_route"][ship["baseline_progress_index"]+1:]):
                command = "RESUME"
            action = action_for(ship, observation, command, speed,
                                template["points"], template["rejoin"], cfg)
            result.append(dict(name=f'{template["name"]}@{speed:g}', action=action,
                               change=template["change"] or (speed != ship["sog_knots"])))
    return result[:12]
