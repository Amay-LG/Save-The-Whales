"""Action validation and rejoin computation (§5.7). Invalid -> rejected, never crash."""
from contract.schema import COMMANDS, HOLD, RESUME


class Rejected(Exception):
    pass


def validate(action: dict, world, fleet, t, cfg) -> dict:
    """Return a normalised command dict or raise Rejected(reason).
    Checks: active ship_id, command type, speed clamp [min, service], 1..max_waypoints in bbox
    and off land, full path (ship -> detour -> rejoin) land-free, length <= max_path_ratio x original,
    valid_until default/max."""
    if action.get("ship_id") not in fleet.active:
        raise Rejected("unknown ship_id")
    if action.get("command") not in COMMANDS:
        raise Rejected("unknown command")
    cmd = dict(action)
    if cmd["command"] == HOLD:
        cmd["command"] = RESUME
    raise NotImplementedError("speed clamp, waypoint/land/rejoin/deviation checks, TTL")


def compute_rejoin(last_xy, original_route, land):
    """First original waypoint ahead whose leg from last_xy is land-free, else None."""
    raise NotImplementedError
