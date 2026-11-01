"""Shared contract 2.0: constants and TypedDict shapes from the Tasks.md handshake.

Everything crossing a module boundary is a plain JSON-compatible dict, list, number, bool or string.
These TypedDicts document the agreed shapes; importing them is optional and nothing here validates at
runtime. Conventions:

- Points are ``{"lat": float, "lon": float}``. Only GeoJSON uses ``[lon, lat]``.
- Contract speeds are knots; internal velocity is m/s (``shared.geo.knots_to_mps``).
- Timestamps are UTC ISO-8601 strings ending in ``Z``. Virtual time is seconds from ``scenario.start_at``.
- Source AIS times describe the recorded track only; they never drive the virtual clock.
- MMSIs are strings of digits (identifiers, not numbers).
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Literal, Optional, Protocol, Tuple, TypedDict

SCHEMA_VERSION = "2.0"

# --- Region, projection and units -------------------------------------------------------------------
REGION_BBOX = {"lat_min": 33.8, "lat_max": 34.6, "lon_min": -120.5, "lon_max": -119.0}
PROJECTION_ORIGIN = {"lat": 34.2, "lon": -119.75}
EARTH_RADIUS_M = 6_371_000.0
METERS_PER_NM = 1852.0
KNOT_MPS = METERS_PER_NM / 3600.0

# --- Commands and validation purposes ---------------------------------------------------------------
SLOWDOWN, DETOUR, RESUME = "SLOWDOWN", "DETOUR", "RESUME"
COMMANDS = (SLOWDOWN, DETOUR, RESUME)
UNSUPPORTED_COMMANDS = ("HOLD",)  # rejected visibly; never emitted

PURPOSE_ISSUE, PURPOSE_APPLY, PURPOSE_EXPIRY_FALLBACK = "ISSUE", "APPLY", "EXPIRY_FALLBACK"
VALIDATION_PURPOSES = (PURPOSE_ISSUE, PURPOSE_APPLY, PURPOSE_EXPIRY_FALLBACK)

# --- Shared failure codes (Tasks.md conventions table) ----------------------------------------------
AIS_KEY_MISSING = "AIS_KEY_MISSING"
AIS_ROUTE_UNAVAILABLE = "AIS_ROUTE_UNAVAILABLE"
INVALID_MAP = "INVALID_MAP"
INVALID_BASELINE_ROUTE = "INVALID_BASELINE_ROUTE"
INVALID_ACTION = "INVALID_ACTION"
STALE_ACTION = "STALE_ACTION"
SUPERSEDED = "SUPERSEDED"
UNKNOWN_WEATHER = "UNKNOWN_WEATHER"
INSUFFICIENT_FRESH_EVIDENCE = "INSUFFICIENT_FRESH_EVIDENCE"
NO_FEASIBLE_ROUTE = "NO_FEASIBLE_ROUTE"
NOT_EVALUATED_BUDGET = "NOT_EVALUATED_BUDGET"
FAILURE_CODES = (
    AIS_KEY_MISSING, AIS_ROUTE_UNAVAILABLE, INVALID_MAP, INVALID_BASELINE_ROUTE, INVALID_ACTION,
    STALE_ACTION, SUPERSEDED, UNKNOWN_WEATHER, INSUFFICIENT_FRESH_EVIDENCE, NO_FEASIBLE_ROUTE,
    NOT_EVALUATED_BUDGET,
)

# --- Source kinds -----------------------------------------------------------------------------------
AISSTREAM_CAPTURE = "AISSTREAM_CAPTURE"
SYNTHETIC_FIXTURE = "SYNTHETIC_FIXTURE"
SOURCE_KINDS = (AISSTREAM_CAPTURE, SYNTHETIC_FIXTURE)
PROVIDER_AISSTREAM = "AISSTREAM"
WEATHER_SIMULATED = "SIMULATED"

# --- AIS capture status (fixed keys: state, reason_codes, records_received, records_valid,
#     vessels_tracked, last_received_at, api_verified) -------------------------------------------------
CAPTURE_IDLE, CAPTURE_CONNECTING, CAPTURE_CAPTURING = "IDLE", "CONNECTING", "CAPTURING"
CAPTURE_DEGRADED, CAPTURE_STOPPED, CAPTURE_ERROR = "DEGRADED", "STOPPED", "ERROR"
CAPTURE_STATES = (CAPTURE_IDLE, CAPTURE_CONNECTING, CAPTURE_CAPTURING, CAPTURE_DEGRADED,
                  CAPTURE_STOPPED, CAPTURE_ERROR)
AIS_CONNECTION_FAILED = "AIS_CONNECTION_FAILED"        # repeated connect/stream failures; gave up
AIS_RECONNECTING = "AIS_RECONNECTING"                  # transient failure; backing off
AIS_API_ERROR = "AIS_API_ERROR"                        # provider sent an error message
AIS_NO_RECENT_MESSAGES = "AIS_NO_RECENT_MESSAGES"      # connected but silent (coverage or outage)
AIS_CAPTURE_LIMIT_REACHED = "AIS_CAPTURE_LIMIT_REACHED"
AIS_RETENTION_CAP_REACHED = "AIS_RETENTION_CAP_REACHED"  # oldest records dropped
AIS_WEBSOCKET_CLIENT_MISSING = "AIS_WEBSOCKET_CLIENT_MISSING"

# Normalized-record time quality. AISStream metadata time is provider-stamped (close to reception);
# the AIS "Timestamp" field is only the UTC second, so it is reconciled against a full date-time.
TIME_PROVIDER_AIS_SECOND = "PROVIDER_TIME_AIS_SECOND"
TIME_PROVIDER = "PROVIDER_TIME"
TIME_RECEIPT_AIS_SECOND = "RECEIPT_TIME_AIS_SECOND"
TIME_RECEIPT_APPROX = "RECEIPT_TIME_APPROX"

# --- Geometry rejection codes (shared.geo.check_water_path) -----------------------------------------
INVALID_PATH = "INVALID_PATH"
NON_FINITE_COORDINATE = "NON_FINITE_COORDINATE"
POINT_ON_LAND = "POINT_ON_LAND"
LAND_CROSSING = "LAND_CROSSING"
LAND_CONTACT = "LAND_CONTACT"                      # touching or collinear contact with a land edge
INSUFFICIENT_CLEARANCE = "INSUFFICIENT_CLEARANCE"  # distance to land <= hull clearance radius
INVALID_POLYGON = "INVALID_POLYGON"

# --- Route preparation / bundle issue codes ---------------------------------------------------------
TOO_FEW_FIXES = "TOO_FEW_FIXES"
TOO_SHORT_DURATION = "TOO_SHORT_DURATION"
TOO_SHORT_DISTANCE = "TOO_SHORT_DISTANCE"
SERVICE_SPEED_UNKNOWN = "SERVICE_SPEED_UNKNOWN"
SERVICE_SPEED_BELOW_MIN = "SERVICE_SPEED_BELOW_MIN"
VERTEX_BUDGET_EXCEEDED = "VERTEX_BUDGET_EXCEEDED"
VALIDATED_AGAINST_FIXTURE_MAP = "VALIDATED_AGAINST_FIXTURE_MAP"
INVALID_BUNDLE = "INVALID_BUNDLE"
SCHEMA_VERSION_MISMATCH = "SCHEMA_VERSION_MISMATCH"
SOURCE_TRACK_CHECKSUM_MISMATCH = "SOURCE_TRACK_CHECKSUM_MISMATCH"

# --- Land map issue / simplification codes ----------------------------------------------------------
MAP_FILE_MISSING = "MAP_FILE_MISSING"
MAP_EMPTY = "MAP_EMPTY"
INVALID_RING = "INVALID_RING"
MAP_PROVENANCE_MISSING = "MAP_PROVENANCE_MISSING"
MAP_HASH_MISMATCH = "MAP_HASH_MISMATCH"
HOLES_FILLED_AS_LAND = "HOLES_FILLED_AS_LAND"
CLIPPED_TO_REGION_BBOX = "CLIPPED_TO_REGION_BBOX"

# --- Run status --------------------------------------------------------------------------------------
RUN_COMPLETE, RUN_INCOMPLETE, RUN_ERROR = "COMPLETE", "INCOMPLETE", "ERROR"


# =====================================================================================================
# Shapes
# =====================================================================================================
class Point(TypedDict):
    lat: float
    lon: float


class BBox(TypedDict):
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float


# --- Person 1: AIS records, routes, land -------------------------------------------------------------
class _AISRecordRequired(TypedDict):
    record_type: Literal["POSITION", "STATIC"]
    mmsi: str
    observed_at: str
    received_at: str
    time_quality: str
    provider: str          # "AISSTREAM" for captured records; "SYNTHETIC_FIXTURE" for fixtures
    receipt_seq: int       # local receipt order, used as a tie-breaker


class AISRecord(_AISRecordRequired, total=False):
    lat: Optional[float]   # None on STATIC records without a position
    lon: Optional[float]
    sog_knots: Optional[float]
    cog_deg: Optional[float]
    heading_deg: Optional[float]
    nav_status: Optional[int]
    length_m: Optional[float]
    beam_m: Optional[float]
    ship_type: Optional[int]
    ship_name: Optional[str]
    message_type: str
    adapter: str


class CaptureStatus(TypedDict):
    state: str
    reason_codes: List[str]
    records_received: int
    records_valid: int
    vessels_tracked: int
    last_received_at: Optional[str]
    api_verified: bool


class CaptureHandle(Protocol):
    def status(self) -> CaptureStatus: ...
    def snapshot_records(self) -> List[AISRecord]: ...
    def stop(self) -> None: ...


class RouteSource(TypedDict):
    kind: str              # AISSTREAM_CAPTURE | SYNTHETIC_FIXTURE
    api_verified: bool
    captured_at: Optional[str]
    capture_sha256: Optional[str]
    fixture_only: bool


class RouteValidation(TypedDict):
    ok: bool
    reasons: List[str]


class _RouteRequired(TypedDict):
    route_id: str
    source_mmsi: str
    source_start_at: str
    source_end_at: str
    waypoints: List[Point]   # index 0 = initial position, last = final endpoint; indices never change
    service_speed_knots: float
    length_m: float
    beam_m: float
    assumed_fields: List[str]
    validation: RouteValidation


class Route(_RouteRequired, total=False):
    # Small optional provenance fields (Architecture §4.3 step 4): the unsimplified clip and its hash.
    source_track: List[Dict[str, Any]]   # [{lat, lon, observed_at, sog_knots}]
    source_track_sha256: str
    simplification_tolerance_m: float
    baseline_length_m: float
    baseline_duration_s: float


class RouteBundle(TypedDict):
    schema_version: str
    bundle_id: str
    bbox: BBox
    source: RouteSource
    routes: List[Route]      # [] plus AIS_ROUTE_UNAVAILABLE in issues when nothing is usable
    issues: List[str]


class _LandMapRequired(TypedDict):
    map_id: str
    source: str
    sha256: Optional[str]
    validated: bool
    fixture_only: bool
    bbox: BBox
    rings: List[List[Point]]   # land rings; MultiPolygons normalized into rings
    issues: List[str]


class LandMap(_LandMapRequired, total=False):
    simplifications: List[str]   # labelled conservative steps, e.g. HOLES_FILLED_AS_LAND
    provenance: Dict[str, Any]
    geojson: Dict[str, Any]      # source GeoJSON preserved for display


class WaterPathResult(TypedDict, total=False):
    ok: bool
    min_clearance_m: Optional[float]
    reasons: List[str]
    worst_leg_index: Optional[int]


# --- Weather and scenarios (Persons 3/4) ------------------------------------------------------------
class WeatherZone(TypedDict):
    zone_id: str
    polygon: List[Point]
    severity: float          # [0, 1]
    active_from_s: float     # half-open [active_from_s, active_until_s)
    active_until_s: float
    blocked: bool


class WeatherField(TypedDict):
    source: str              # "SIMULATED"
    coverage_start_s: float
    coverage_end_s: float
    zones: List[WeatherZone]


class Voyage(TypedDict):
    voyage_id: str
    route_id: str
    entry_s: float


class _ScenarioWhaleRequired(TypedDict):
    whale_id: str            # private local ID; never enters advisor payloads
    start: Point


class ScenarioWhale(_ScenarioWhaleRequired, total=False):
    # Movement (one of): waypoints + speed_mps; heading_deg + speed_mps (+ turns); random_walk (+ area).
    species: str
    speed_mps: float
    heading_deg: float                   # compass, 0 = north
    turns: List[Dict[str, float]]        # [{"at_s", "heading_deg", "speed_mps"?}]
    waypoints: List[Point]
    random_walk: bool
    area: BBox                           # reflection box for random walks
    # Exposure (one of): explicit schedule (gaps are not exposed) or a surface/deep cycle.
    exposure: List[Dict[str, Any]]       # [{"from_s", "until_s", "exposed": bool}]
    surface_s: float
    deep_s: float
    phase_s: float


class Scenario(TypedDict):
    scenario_id: str
    seed: int
    start_at: str
    duration_s: float
    voyages: List[Voyage]
    whales: List[ScenarioWhale]    # private controlled whales; never enter advisor payloads
    weather: WeatherField


class RunInputs(TypedDict):
    run_id: str
    route_bundle: RouteBundle
    land: LandMap
    scenario: Scenario
    config: Dict[str, Any]
    fixture_only: bool


# --- Observation (Person 2 -> Person 3) -------------------------------------------------------------
class _ObservableShipRequired(TypedDict):
    ship_id: str
    lat: float
    lon: float
    sog_knots: float
    cog_deg: float
    length_m: float
    beam_m: float
    service_speed_knots: float
    original_route: List[Point]    # immutable, includes index-0 start
    current_route: List[Point]     # future waypoints, excluding the current position
    active_command: Optional[Dict[str, Any]]
    baseline_progress_index: int
    latest_revision: int
    last_action_at: Optional[str]


class ObservableShip(_ObservableShipRequired, total=False):
    incident_budget: Optional["IncidentBudget"]


class _WhaleDetectionRequired(TypedDict):
    detection_id: str        # identifies a report, not an animal
    lat: float
    lon: float
    position_error_m: float
    observed_at: str
    reported_at: str
    source: str


class WhaleDetection(_WhaleDetectionRequired, total=False):
    species: str


class Observation(TypedDict):
    schema_version: str
    now: str
    decision_interval_min: float
    bbox: BBox
    static: Dict[str, Any]   # {"land": LandMap, "weather": WeatherField}
    ships: List[ObservableShip]
    whale_detections: List[WhaleDetection]
    decision_id: str
    sim_time_s: float


class TimedLeg(TypedDict):
    start: Point
    end: Point
    start_s: float           # absolute virtual elapsed seconds
    end_s: float
    speed_knots: float


# --- Actions, decisions, validation (Person 3 <-> Person 2) -----------------------------------------
class _ActionRequired(TypedDict):
    ship_id: str
    issued_at: str
    command: str             # SLOWDOWN | DETOUR | RESUME
    target_speed_knots: Optional[float]
    detour_waypoints: List[Point]   # excludes current position and the baseline rejoin
    valid_until: str
    metadata: Dict[str, Any]       # explanations/costs only; execution never reads it
    action_id: str
    revision: int


class Action(_ActionRequired, total=False):
    rejoin_route_index: int        # index into original_route; required for new DETOUR outputs


class IncidentBudget(TypedDict):
    incident_id: str
    rejoin_route_index: int
    reference_distance_m: float
    reference_eta_s: float
    max_distance_m: float
    max_added_delay_s: float
    accumulated_time_s: float
    accumulated_distance_m: float


class ValidatedAction(TypedDict):
    action: Action                          # the submitted action, unmodified (resolve, never rewrite)
    remaining_waypoints: List[Point]        # future path, excluding the current position, through the
                                            # route end: detour points + original_route[rejoin:]
    rejoin_route_index: Optional[int]       # None for SLOWDOWN (path unchanged)
    effective_target_speed_knots: Optional[float]
    incident_budget: Optional[IncidentBudget]


class ValidationResult(TypedDict):
    ok: bool
    reasons: List[str]
    validated_action: Optional[ValidatedAction]


class CostBreakdown(TypedDict, total=False):
    whale_risk: float
    weather_exposure_s: float
    weather_cost_normalized: float
    added_delay_s: float
    extra_distance_m: float
    objective: float


class Assessment(TypedDict):
    ship_id: str
    status: str
    reason_codes: List[str]
    selected_path: List[Point]     # current position through baseline completion
    costs: CostBreakdown
    rejected_candidates: List[Dict[str, Any]]


class Decision(TypedDict):
    decision_id: str
    actions: List[Action]
    assessments: List[Assessment]


# --- Results (Person 2 -> Person 4) -----------------------------------------------------------------
class RunSummary(TypedDict):
    status: str                    # COMPLETE | INCOMPLETE | ERROR
    elapsed_wall_s: float
    simulated_duration_s: float
    voyages_completed: int
    voyages_failed: int
    modeled_contacts: int
    close_approaches: int
    whale_exposure_proxy: float
    weather_exposure_s: Optional[float]
    added_delay_s: Optional[float]   # null when voyages are incomplete
    extra_distance_m: Optional[float]
    alerts: int
    route_changes: int
    rejected_actions: int


class RunResult(TypedDict):
    manifest: Dict[str, Any]
    summary: RunSummary
    frames: List[Dict[str, Any]]
    events: List[Dict[str, Any]]
    decisions: List[Decision]


# --- Callable boundary (Tasks.md "Callable boundary everyone can stub immediately") -----------------
ProjectFn = Callable[[Point], Tuple[float, float]]
DecideFn = Callable[[Observation], Decision]
ValidateActionFn = Callable[..., ValidationResult]   # (action, observation, *, purpose="ISSUE")
WeatherExposureFn = Callable[[List[TimedLeg], WeatherField], float]
