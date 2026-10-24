"""Shared simulator <-> advisor contract (plan §6). Plain JSON-compatible dicts only."""
from typing import List, Literal, Optional, TypedDict

SCHEMA_VERSION = "2.0"
SLOWDOWN, DETOUR, RESUME = "SLOWDOWN", "DETOUR", "RESUME"
HOLD = "HOLD"  # deprecated alias of RESUME
COMMANDS = (SLOWDOWN, DETOUR, RESUME, HOLD)


class LatLon(TypedDict):
    lat: float
    lon: float


class ShipObs(TypedDict):
    ship_id: str
    lat: float
    lon: float
    sog_knots: float
    cog_deg: float
    length_m: float
    beam_m: float
    service_speed_knots: float
    original_route: List[LatLon]
    current_route: List[LatLon]
    active_command: Optional[dict]


class WhaleDetection(TypedDict, total=False):
    detection_id: str
    species: str
    lat: float
    lon: float
    position_error_m: float
    observed_at: str
    reported_at: str
    source: str        # "visual:<ship_id>" | "acoustic:<buoy_id>" | "oracle"
    dive_state: str    # oracle only


class Observation(TypedDict):
    schema_version: str
    now: str
    decision_interval_min: int
    bbox: dict
    static: dict       # full on first call, references afterwards
    ships: List[ShipObs]
    whale_detections: List[WhaleDetection]


class Action(TypedDict, total=False):
    ship_id: str
    issued_at: str
    command: Literal["SLOWDOWN", "DETOUR", "RESUME", "HOLD"]
    target_speed_knots: float
    detour_waypoints: List[LatLon]
    valid_until: str
    metadata: dict     # logged, never interpreted
