"""Shared contracts, types, and constants for Save the Whales (v2.0 contract)."""
from typing import TypedDict, Optional, List, Dict, Any, Tuple

SCHEMA_VERSION = "2.0"

# Commands
SLOWDOWN = "SLOWDOWN"
DETOUR = "DETOUR"
RESUME = "RESUME"
HOLD = "HOLD"  # legacy alias
COMMANDS = (SLOWDOWN, DETOUR, RESUME, HOLD)

# Actions
class Point(TypedDict):
    lat: float
    lon: float

class TimedLeg(TypedDict):
    start: Point
    end: Point
    start_s: float
    end_s: float
    speed_knots: float

class Action(TypedDict, total=False):
    ship_id: str
    action_id: str
    revision: int
    command: str  # SLOWDOWN, DETOUR, RESUME
    target_speed_knots: Optional[float]
    detour_waypoints: Optional[List[Point]]
    rejoin_route_index: Optional[int]
    issued_at: str
    valid_until: str
    metadata: Optional[Dict[str, Any]]

class ObservableShip(TypedDict, total=False):
    ship_id: str
    lat: float
    lon: float
    sog_knots: float
    cog_deg: float
    length_m: float
    beam_m: float
    service_speed_knots: float
    original_route: List[Point]
    current_route: List[Point]
    active_command: Optional[Action]
    baseline_progress_index: int
    latest_revision: int
    last_action_at: Optional[str]
    incident_budget: Optional[Dict[str, Any]]

class WhaleDetection(TypedDict, total=False):
    detection_id: str
    lat: float
    lon: float
    position_error_m: float
    observed_at: str
    reported_at: str
    source: str
    species: Optional[str]

class LandMap(TypedDict, total=False):
    map_id: str
    source: str
    sha256: str
    validated: bool
    fixture_only: bool
    bbox: Dict[str, float]
    rings: List[List[Point]]
    issues: List[str]

class WeatherZone(TypedDict, total=False):
    zone_id: str
    polygon: List[Point]
    severity: float
    active_from_s: float
    active_until_s: float
    blocked: bool

class WeatherField(TypedDict, total=False):
    source: str
    coverage_start_s: float
    coverage_end_s: float
    zones: List[WeatherZone]

class Observation(TypedDict, total=False):
    schema_version: str
    decision_id: str
    now: str
    sim_time_s: float
    decision_interval_min: float
    bbox: Dict[str, float]
    static: Dict[str, Any]  # {"land": LandMap, "weather": WeatherField}
    ships: List[ObservableShip]
    whale_detections: List[WhaleDetection]

class Assessment(TypedDict, total=False):
    ship_id: str
    status: str
    reason_codes: List[str]
    selected_path: List[Point]
    costs: Dict[str, Any]
    rejected_candidates: List[Dict[str, Any]]

class Decision(TypedDict, total=False):
    decision_id: str
    actions: List[Action]
    assessments: List[Assessment]

class ValidationResult(TypedDict, total=False):
    ok: bool
    reasons: List[str]
    validated_action: Optional[Dict[str, Any]]

class Route(TypedDict, total=False):
    route_id: str
    source_mmsi: str
    source_start_at: str
    source_end_at: str
    waypoints: List[Point]
    service_speed_knots: float
    length_m: float
    beam_m: float
    assumed_fields: List[str]
    validation: Dict[str, Any]

class RouteBundle(TypedDict, total=False):
    schema_version: str
    bundle_id: str
    bbox: Dict[str, float]
    source: Dict[str, Any]
    routes: List[Route]
    issues: List[str]

class ScenarioVoyage(TypedDict, total=False):
    voyage_id: str
    route_id: str
    entry_s: float

class ControlledWhale(TypedDict, total=False):
    whale_id: str
    initial_point: Point
    speed_knots: Optional[float]
    heading_deg: Optional[float]
    waypoints: Optional[List[Point]]
    exposure_schedule: Optional[List[Dict[str, Any]]]

class Scenario(TypedDict, total=False):
    scenario_id: str
    seed: int
    start_at: str
    duration_s: float
    voyages: List[ScenarioVoyage]
    whales: List[ControlledWhale]
    weather: Optional[WeatherField]

class RunInputs(TypedDict, total=False):
    run_id: str
    route_bundle: RouteBundle
    land: LandMap
    scenario: Scenario
    config: Dict[str, Any]
    fixture_only: bool

class RunSummary(TypedDict, total=False):
    status: str
    elapsed_wall_s: float
    simulated_duration_s: float
    voyages_completed: int
    voyages_failed: int
    modeled_contacts: int
    close_approaches: int
    whale_exposure_proxy: float
    weather_exposure_s: float
    added_delay_s: Optional[float]
    extra_distance_m: Optional[float]
    alerts: int
    route_changes: int
    rejected_actions: int

class RunResult(TypedDict, total=False):
    manifest: Dict[str, Any]
    summary: RunSummary
    frames: List[Dict[str, Any]]
    events: List[Dict[str, Any]]
    decisions: List[Decision]

