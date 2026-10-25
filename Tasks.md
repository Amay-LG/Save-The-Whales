# Save the Whales — Four-Person Implementation Plan

```text
Save-The-Whales/
├── Architecture.md                         # Behavioral specification; read first
├── Tasks.md                                # This plan and the shared implementation handshake
├── README.md                               # [4] Setup, launch, assumptions, demo walkthrough
├── requirements.txt                        # [1] Shared dependency list
├── .env.example                            # [1] Variable names only; never real credentials
├── .gitignore                              # [1] Secrets, captures, caches, runs, Python artifacts
├── app.py                                  # [4] Streamlit entry point and production wiring
├── run_comparison.py                       # [4] Small paired-comparison CLI
├── save_the_whales/
│   ├── __init__.py                         # [1] Minimal package initialization
│   ├── shared/                             # [1] Owned by Person 1; used by everyone
│   │   ├── __init__.py
│   │   ├── contracts.py                   # TypedDicts/constants; JSON-compatible values
│   │   └── geo.py                         # Projection, distances, segment/polygon operations
│   ├── sources/                            # [1] Real AIS and static land preparation
│   │   ├── __init__.py
│   │   ├── ais.py                         # Bounded background AIS capture
│   │   ├── routes.py                      # Track cleaning, route bundles, provenance
│   │   └── land.py                        # Normalize and validate the regional land subset
│   ├── simulation/                         # [2] Hidden world and simulated execution
│   │   ├── __init__.py
│   │   ├── world.py                       # Scenario initialization and virtual clock
│   │   ├── ships.py                       # Waypoint motion, route progress, simulated IDs
│   │   ├── whales.py                      # Seeded movement and exposure states
│   │   ├── sensors.py                     # Noisy/delayed reports; observable snapshots
│   │   ├── commands.py                    # Queues, compliance, revisions, expiry, rejoin
│   │   ├── metrics.py                     # Truth-based encounter/exposure/delay metrics
│   │   └── engine.py                      # Bounded run_scenario callback boundary
│   ├── advisor/                            # [3] No network or simulator imports
│   │   ├── __init__.py
│   │   ├── risk.py                        # Timed-leg CPA and uncertainty-aware risk
│   │   ├── candidates.py                  # At most four route geometries, three speeds
│   │   ├── weather.py                     # Active-zone exposure and blocked-zone checks
│   │   ├── constraints.py                 # Shared final action/path validation
│   │   └── planner.py                     # Scoring, decisions, explanations
│   └── ui/                                 # [4] Presentation and scenario assembly
│       ├── __init__.py
│       ├── scenarios.py                   # Resolve route IDs into scenario presets
│       ├── plots.py                       # Map layers and comparison table formatting
│       └── dashboard.py                   # UI accepting real or explicitly injected test services
├── config/
│   ├── default.json                       # [4] Agreed defaults from the handshake below
│   └── scenarios/                          # [4] Crossing, no-threat, storm, land-blocked-side
│       ├── crossing.json
│       ├── no_threat.json
│       ├── storm_detour.json
│       └── land_blocked_side.json
├── data/
│   ├── land/
│   │   ├── santa_barbara.geojson           # [1] Actual nonempty research geometry
│   │   └── provenance.json                # [1] Source/version, extent, resolution, hash
│   └── ais/
│       └── README.md                      # [1] Cache format; generated captures/bundles ignored
├── tests/
│   ├── fixtures/
│   │   ├── common/                        # [1] Publish shared toy inputs; explicitly fixture-only
│   │   ├── sources/                       # [1] Sanitized protocol/track fixtures
│   │   ├── simulation/                    # [2] Scripted decisions/validators and small worlds
│   │   ├── advisor/                       # [3] Fixed observations and test geometry facade
│   │   └── ui/                            # [4] Canned capture status, frames, decisions, results
│   ├── test_sources.py                    # [1]
│   ├── test_geo.py                        # [1]
│   ├── test_simulation.py                 # [2]
│   ├── test_advisor.py                    # [3]
│   ├── test_ui_contract.py                # [4]
│   ├── test_end_to_end.py                 # [4] Real modules after integration
│   └── ui_preview.py                      # [4] Explicit test-service preview, never a real-data claim
└── runs/
    └── README.md                          # [4] Generated manifests/results/frames; artifacts ignored
```

The tree is the **target structure**, independent of the placeholder repository. The numbered owners are the four parallel assignments below. This task creates the plan, not those implementation files. [Architecture.md](Architecture.md) controls product behavior; this requested layout replaces its temporary advice to retain placeholder paths. Reuse useful code without preserving unfinished scaffolding.

**Delivery constraint:** approximately 90 minutes of parallel implementation, then up to 30 minutes for integration and fixes. No training, whale API, live weather API, ship–ship avoidance, maritime-law checks, database, backend server, or general route-search framework. Real AIS supplies recorded baseline geometry; ships, whales, sightings, and weather remain simulated. Never let later AIS messages overwrite simulated movement.

**Shared handshake — part of every person's assignment.** Use native contract version `"2.0"` with plain dictionaries, small TypedDict definitions, lists, numbers, and UTC strings. No teammate must wait for another module to exist: implement against the exact shapes/functions below using private fixtures and injected callbacks. Person 1 publishes the shared definitions, but dictionary construction and tests need no runtime schema import. Do not build competing schemas or silently accept a different interpretation.

**Common conventions and defaults**

| Item | Agreement |
|---|---|
| Coordinates | Public points are `{"lat": float, "lon": float}`. GeoJSON alone uses `[lon, lat]`. Geometry helpers return meters, with fixed origin `(34.2, -119.75)`. |
| Projection/conversion | Use `R=6_371_000 m`, `x=R*radians(lon+119.75)*cos(radians(34.2))`, `y=R*radians(lat-34.2)`. Knots to m/s is `speed_knots*1852/3600`. Tests may copy these tiny formulas while waiting for the real helper. |
| Speed/time | Contract speed fields are knots. Internal velocity is m/s. UTC strings end in `Z`; virtual elapsed time is seconds from `scenario.start_at`. Source AIS times never drive the virtual clock. |
| Region | `{"lat_min": 33.8, "lat_max": 34.6, "lon_min": -120.5, "lon_max": -119.0}`. |
| Default world | Five simulated voyages, twenty whales, `dt_s=30`, `decision_dt_s=60`, `duration_s=7200`, `drain_max_s=1800`, `wall_limit_s=30`. Finish early when no pending/active voyages remain. |
| Planning | At most four geometries × three speeds; up to eight detour waypoints; `lookahead_s=900`, `max_rejoin_s=3600`, offsets `[500, 1000]` m. |
| Constraints | `clearance_margin_m=100`, `min_speed_knots=6`, `max_path_ratio=1.5`, `max_added_delay_s=900`. Actual source service speed bounds valid candidate speeds; defaults are modeled assumptions. |
| Score defaults | `w_whale=5`, `w_weather=2`, `w_delay=1`, `w_distance=0.5`, `w_change=0.1`; `min_objective_improvement=0.01`, `min_whale_improvement=0.05`, `whale_risk_tolerance=0.01`, `cooldown_s=60`. These are research starting values, not learned/calibrated probabilities. |
| Evidence | Retain at most 50 recent reports; `max_detection_age_s=1800`, drift allowance initially `1.5 m/s`. Missing fresh evidence is not proof of whale absence. |
| Responses | For deterministic initial acceptance tests, `response_lag_s=0`, `compliance=1`. Also test one nonzero-lag/ignored-command case; production scenario values are explicit. |
| Encounter episodes | Enter at body clearance ≤500 m; at most one contact per episode; exit after clearance >750 m continuously for 120 s. |
| Truth proxy | `metrics.sigma_eval_m=300`, independent of sensor error; integrate Gaussian proximity × relative travel/scale × true exposure × illustrative speed weight. This is a smooth diagnostic, not expected deaths. |
| Weather | Configured/simulated only; severity `[0,1]`; overlapping zones use max severity. Unknown coverage is not clear weather. |
| Failure codes | Use machine-readable strings, including `AIS_KEY_MISSING`, `AIS_ROUTE_UNAVAILABLE`, `INVALID_MAP`, `INVALID_BASELINE_ROUTE`, `INVALID_ACTION`, `STALE_ACTION`, `SUPERSEDED`, `UNKNOWN_WEATHER`, `INSUFFICIENT_FRESH_EVIDENCE`, `NO_FEASIBLE_ROUTE`, `NOT_EVALUATED_BUDGET`. UI text may explain them. |

Store these under corresponding `simulation`, `planner`, `sensors`, `response`, and `ais` keys in `config/default.json`; do not scatter different defaults across modules. Functions receive their configuration explicitly or through a closure.

**Data shapes shared across all four assignments**

| Object | Required fields and meanings |
|---|---|
| `RouteBundle` | `schema_version`, `bundle_id`, `bbox`, `source`, `routes`, `issues: list[str]`. `source` has `kind` (`AISSTREAM_CAPTURE` or `SYNTHETIC_FIXTURE`), `api_verified`, `captured_at`, `capture_sha256`, `fixture_only`. No secrets/raw subscription payloads. No usable routes means `routes=[]` plus `AIS_ROUTE_UNAVAILABLE` in issues. |
| Route | `route_id`, `source_mmsi`, `source_start_at`, `source_end_at`, `waypoints: list[Point]`, `service_speed_knots`, `length_m`, `beam_m`, `assumed_fields: list[str]`, `validation: {ok: bool, reasons: list[str]}`. Waypoints include the initial position at index 0 and final endpoint; indices never change. |
| `LandMap` | `map_id`, `source`, `sha256`, `validated`, `fixture_only`, `bbox`, `rings: list[list[Point]]`, `issues: list[str]`. Rings describe land; normalize MultiPolygons into rings. For this conservative MVP, holes may be filled as land if documented. Preserve source GeoJSON for display. Missing/invalid input returns `validated=false` and `INVALID_MAP` in issues; an empty map never passes readiness. |
| `WeatherField` | `source="SIMULATED"`, `coverage_start_s`, `coverage_end_s`, `zones`. A zone has `zone_id`, `polygon: list[Point]`, `severity`, `active_from_s`, `active_until_s`, `blocked: bool`. Use half-open active intervals; outside field coverage is unknown. |
| `Scenario` | `scenario_id`, `seed`, `start_at`, `duration_s`, `voyages`, `whales`, `weather`. A voyage has `voyage_id`, `route_id`, `entry_s`. A controlled whale has a private local ID, initial point, speed/heading or waypoints, and exposure schedule. Private whale objects never enter advisor payloads. |
| `RunInputs` | `run_id`, `route_bundle`, `land`, `scenario`, `config`, `fixture_only`. Treat as immutable; clone any mutable world state. `fixture_only` is true if any dependency is a test fixture. |
| `Observation` | Existing v2 keys: `schema_version`, `now`, `decision_interval_min`, `bbox`, `static`, `ships`, `whale_detections`. Add `decision_id` and `sim_time_s`. `static={land: LandMap, weather: WeatherField}`. No true whale state/identity, future detections, or hidden world handle. |
| Observable ship | Existing `ship_id`, `lat`, `lon`, `sog_knots`, `cog_deg`, `length_m`, `beam_m`, `service_speed_knots`, `original_route`, `current_route`, `active_command`; plus `baseline_progress_index`, `latest_revision`, `last_action_at`, and optional `incident_budget`. `current_route` is future waypoints excluding the current position; `original_route` includes its immutable index-0 start. |
| Whale detection | `detection_id`, `lat`, `lon`, `position_error_m`, `observed_at`, `reported_at`, `source`, optional species. IDs identify reports, not animals. Duplicated IDs are the same report; observation time does not advance on redisplay. |
| `TimedLeg` | `start: Point`, `end: Point`, `start_s`, `end_s`, `speed_knots`. Times are absolute virtual elapsed seconds, not seconds relative to each leg. Zero-length legs are handled explicitly. |
| `Action` | Existing `ship_id`, `issued_at`, `command`, `target_speed_knots`, `detour_waypoints`, `valid_until`, `metadata`; plus `action_id`, integer `revision`, and `rejoin_route_index` when applicable. New output never uses `HOLD`. |
| Detour convention | `detour_waypoints` excludes the current position **and** the baseline rejoin. `rejoin_route_index` points into `original_route`. Resolved remaining path is detour points + baseline rejoin once + `original_route[index+1:]`. A resume during a detour also names the forward rejoin. |
| `Decision` | `decision_id`, `actions: list[Action]`, `assessments: list[Assessment]`. Assessment has `ship_id`, `status`, `reason_codes`, `selected_path`, `costs`, `rejected_candidates`. `selected_path` includes current position and all remaining points through baseline completion. `NO_FEASIBLE_ROUTE` is explicit, not an empty action list interpreted as safe. |
| Cost breakdown | `whale_risk`, `weather_exposure_s`, `weather_cost_normalized`, `added_delay_s`, `extra_distance_m`, `objective`; include reference endpoint/budget and named rejection codes. Scores are model proxies, not probabilities. |
| `ValidationResult` | `ok: bool`, `reasons: list[str]`, `validated_action: dict or null`. The validated object includes the original action, resolved `remaining_waypoints`, rejoin index, effective target speed, and incident budget. Never silently clamp/rewrite a scored action. |
| Incident budget | Core execution state: `incident_id`, `rejoin_route_index`, `reference_distance_m`, `reference_eta_s`, `max_distance_m`, `max_added_delay_s`, accumulated incident time/distance. Retain across replans; do not multiply detour budgets repeatedly. Do not hide this control state in explanatory metadata. |
| `RunResult` | `manifest`, `summary`, `frames`, `events`, `decisions`. Manifest records source/map/config hashes, seed, weather, `fixture_only`, mode, and status. Frames/events/decisions are capped/downsampled if necessary. |
| Summary | `status` (`COMPLETE`, `INCOMPLETE`, `ERROR`), `elapsed_wall_s`, `simulated_duration_s`, `voyages_completed`, `voyages_failed`, `modeled_contacts`, `close_approaches`, `whale_exposure_proxy`, `weather_exposure_s`, `added_delay_s`, `extra_distance_m`, `alerts`, `route_changes`, `rejected_actions`. Delay/distance deltas are null if voyages are incomplete; keep partial per-voyage diagnostics separately. |
| Frame/event | Frame: `sim_time_s`, `now`, observable `ships`, `whale_detections`, current alerts, optional separately labeled `analyst_truth`. Event: `sim_time_s`, `type`, optional `ship_id`, `details`. Analyst truth goes only to results/UI, never to the advisor callback. |

**Callable boundary everyone can stub immediately**

```python
# Person 1
start_capture(config) -> CaptureHandle
# handle.status() -> dict; handle.snapshot_records() -> copied list[dict]; handle.stop() -> None
prepare_routes(records, land, config) -> RouteBundle
save_route_bundle(bundle, path) -> None
load_route_bundle(path, land, config) -> RouteBundle
load_land(path, config) -> LandMap
project(point) -> tuple[float, float]              # meters; fixed shared origin
unproject(x_m, y_m) -> Point
ship_clearance_m(length_m, beam_m, margin_m) -> float
check_water_path(path, land, clearance_m) -> dict # {ok, min_clearance_m, reasons}
segment_zone_intervals(start, end, polygon) -> list[tuple[float, float]]
# Each interval is a fraction in [0,1] of the geometric segment, including all crossings.

# Person 3: use a module-like geometry argument; bind config once with a closure.
make_decider(config, geometry) -> decide
# decide(observation) -> Decision
make_validator(config, geometry) -> validate_action
# validate_action(action, observation, *, purpose="ISSUE") -> ValidationResult
# purpose is ISSUE, APPLY, or EXPIRY_FALLBACK; only the simulator calls the latter two.
weather_exposure(legs, weather) -> float          # integral max(severity) dt, in severity-seconds

# Person 2
run_scenario(inputs, decide, validate_action, weather_exposure,
             *, project, on_frame=None) -> RunResult

# Person 4
build_run_inputs(bundle, land, scenario_settings, config) -> RunInputs
compare_runs(baseline_result, advisor_result) -> dict
render_dashboard(services) -> None
```

Callbacks are ordinary functions, not a framework. `decide` reads only its observation. `validate_action` reads only action, permitted observable state, static inputs, and configuration. For v2 callers expecting `advise(obs) -> list[Action]`, Person 3 may expose a thin wrapper over the same decision implementation; do not calculate a decision twice.

Capture status has fixed keys: `state` (`IDLE`, `CONNECTING`, `CAPTURING`, `DEGRADED`, `STOPPED`, `ERROR`), `reason_codes`, `records_received`, `records_valid`, `vessels_tracked`, `last_received_at`, and `api_verified`. Expected input-readiness failures return structured status/issues, not inconsistent success-shaped empty objects. `weather_exposure` returns a float only for known coverage; otherwise raise `ValueError("UNKNOWN_WEATHER")`. Person 3 converts that error into assessment/validation reasons, and Person 2 marks unmeasurable execution incomplete rather than recording zero storm exposure.

Revision rules are purpose-specific: `ISSUE` requires a revision newer than `ship.latest_revision`; the executor deduplicates previously accepted `action_id`s before that call. `APPLY` requires equality with the latest accepted revision and an unexpired lease. `EXPIRY_FALLBACK` uses the current **unpassed** route and stored rejoin, not the original detour points; it may bypass the old lease's expiry check but never physical path/weather checks or newer-revision protection.

**Common fixture recipe — available from this document, without waiting for Person 1**

- Construct a fixture-only route with points `(lat,lon)` `[(34.2,-120.1), (34.2,-120.075), (34.2,-120.05), (34.2,-120.025)]`, 18-knot service speed, length 300 m, beam 40 m. Use `source.kind=SYNTHETIC_FIXTURE`, `api_verified=false`, `fixture_only=true` and a fake MMSI such as `999000001`.
- Use `start_at="2026-07-01T12:00:00Z"`, one voyage entering at 0, and a surfaced test whale near `(34.2,-120.065)`. Tests that need evidence inject an ordinary report with its own opaque ID and an observation timestamp; no real whale data is implied.
- For land-blocked-side tests, use a **toy**, explicitly fixture-only land rectangle bounded by latitudes `[34.204,34.216]`, longitudes `[-120.075,-120.055]`. For other tests move the toy obstacle far from the path while retaining valid nonempty geometry.
- Clear weather has `zones=[]` with known coverage `[0,12600]` seconds. A storm fixture can use a south-side rectangle, severity `0.8`, active `[0,3600)`, `blocked=false`. Change only weather between equal-path scoring tests.
- Each person can create private copies under their own fixture directory. These are unit/development inputs, **not real AIS or verified coastal data**, and cannot satisfy the integrated real-route/land acceptance gates.

## 1. Person 1 — Shared contracts, real AIS routes, and land geometry

**Own:** `shared/`, `sources/`, real data/cache formats, root package initialization, `requirements.txt`, `.env.example`, `.gitignore`, and the Person 1 tests/fixtures. Read Architecture §§3–7, F1/F4/F8, and the source-readiness requirements.

**Deliver:** a usable genuine AIS-derived route bundle, usable regional land map, and small geometry functions used by the other owners.

### Tasks

- [ ] Publish `shared/contracts.py` and the common fixture shapes first. Use lightweight TypedDicts/constants; no Pydantic or schema migration. Publish callable signatures even if the network worker is unfinished. Keep package initialization free of network/file side effects.
- [ ] Provide the fixed projection and unit helpers. All other owners use `project(Point)` in meters; handle source GeoJSON `[lon,lat]` separately. Match the declared origin and exercise a round trip across longitude −120°.
- [ ] Implement geometry primitives and `check_water_path`. Check endpoints, entire segment intersections, tangent/collinear contact, and minimum segment-to-land-edge clearance. Return named rejection codes and actual minimum clearance. `ship_clearance_m = 0.5*hypot(length_m, beam_m) + margin_m`; conservatively reject clearance at or below that radius (with one documented numeric epsilon).
- [ ] Implement `segment_zone_intervals` once for the weather owner. Return all inside intervals along a segment, clipped to `[0,1]`; a polygon outside the segment returns `[]`. A zero-length segment inside/on the polygon returns `[(0,1)]`, so a positive-duration stationary leg still accumulates exposure; outside returns `[]`. Handle starts-inside and boundary contact conservatively so blocked-zone crossings do not pass silently.
- [ ] Prepare a nonempty regional land GeoJSON/provenance file covering mainland and relevant Channel Islands. Normalize Polygon/MultiPolygon inputs; validate rings/bounds and retain source/version/resolution/hash. Do not accept empty placeholders, and do not discard an enclosing/crossing polygon merely because none of its vertices lies inside the region.
- [ ] Implement `load_land` returning the shared `LandMap` plus clear `INVALID_MAP` failures. Any conservative simplification/hole filling must be labeled. Visually overlay routes and coast before marking the real subset ready; this remains research geometry, not a navigation chart.
- [ ] Start bounded AIS capture early, in the background, while building the rest. Read `AISSTREAM_API_KEY` from the environment, subscribe to the Santa Barbara box, normalize position/static messages, and keep one connection. Follow the provider protocol linked in Architecture §4; no key in files/logs.
- [ ] Make capture status explicit: state, received/valid records, tracked vessels, last receipt, and error code. `snapshot_records()` returns copied normalized records, never the worker's mutable dictionary. Stop/reconnect cleanly and cap queues/retention. Missing credentials or no coverage must not hang the UI.
- [ ] Normalize records to `mmsi`, `observed_at`, `received_at`, `lat`, `lon`, `sog_knots`, `cog_deg`, optional dimensions, and `time_quality`. If a complete observation timestamp is unavailable, label receipt-time approximation; an AIS UTC-second field is not a full date-time.
- [ ] Implement `prepare_routes`: group/sort/deduplicate; reject invalid values; split gaps over about 180 s and implausible jumps; require at least three distinct fixes, ≥60 s span, and initially ≥250 m travel. Keep bends, reject land-crossing gap chords, and use short clips (≤30 minutes).
- [ ] For the first demonstration prefer segments with baseline travel time ≤15 minutes, so the comparison endpoint is the segment's exit. Preserve generic rejoin indices for longer inputs. If a route cannot be simplified to the vertex budget without invalidating geometry, trim/select a shorter source segment instead of losing bends.
- [ ] Capture at least one segment long enough for the detour demonstration, preferably roughly 2–5 km with room around the hazard. Passing the 250-meter minimum source-quality gate does not guarantee that a 500-meter dogleg fits the 1.5× distance budget. Keep capture running while teammates work; do not relax constraints or invent route extensions to force a detour.
- [ ] Derive source service speed from valid SOG and preserve dimensions when available. Flag every assumed field. Validate the entire baseline at the modeled hull clearance before returning `route.validation.ok=true`.
- [ ] Save/load 1–5 selected observed segments through the agreed JSON bundle functions, preserving source times/checksum/metadata and rechecking geometry/shape on load. API capture and file loading produce the same bundle shape; a single fix plus an inferred straight line is not an observed route.
- [ ] Add only the agreed dependencies: existing pytest/pandas/Streamlit/pydeck and one lightweight WebSocket client; optional existing plotting tools are fine. Ignore credentials and generated source/run artifacts.

### Dependencies and independent development

This owner has no dependency on the simulator, planner, or UI. `prepare_routes` uses the same owned geometry functions that Person 3 later calls. Use sanitized AIS-shaped fixtures and toy polygons to finish parsing, cleaning, and geometry while the real stream accumulates observations.

Publish a fixture bundle/map immediately, then replace it with the real-data artifact when ready; **do not change field shapes**. Other owners need no network credentials. They may copy the recipe above into private fixtures until the shared files exist.

### Completion checks

- [ ] Track sorting, duplicate fixes, gap splitting, bend preservation, and rejection of one-fix routes work without network.
- [ ] Land checks reject crossing, touching, collinear overlap, and insufficient clearance even when all waypoints are in water; valid distant paths pass.
- [ ] Weather interval primitives handle outside/inside/crossing cases.
- [ ] At least one genuine AIS-derived usable segment and a real, nonempty regional land artifact are delivered, with provenance and `fixture_only=false`.
- [ ] If access/coverage/land preparation blocks the real artifacts, deliver working code plus exact status/fixtures immediately. This unblocks teammates but leaves the real-data acceptance item incomplete.

## 2. Person 2 — Simulator, sightings, command execution, and truth metrics

**Own:** `simulation/` and Person 2 tests/fixtures. Read Architecture §§3, 7, 9–12, F7/F9/F10, and the shared shapes above.

**Deliver:** `run_scenario` that can execute with a scripted advisor today and the real Person 3 advisor later, without changing simulator internals.

### Tasks

- [ ] Initialize an independent world from a frozen `RunInputs`. Deep-copy mutable ship/whale state. Keep source route geometry immutable and separate from current simulated paths. Source capture does not appear in this module.
- [ ] Spawn `sim:<source_mmsi>:<voyage_id>` vessels at configured virtual entry times. Preserve source identity separately. Several voyages may use one source route; record reuse instead of pretending to have observed several real vessels.
- [ ] Implement piecewise-linear waypoint motion with instantaneous modeled turns/speed changes, carry leftover distance across waypoints, and emit timed movement segments. Stop at the last source waypoint; never synthesize a source-route extension. Maintain baseline progress and active-detour/rejoin state monotonically.
- [ ] Implement seeded whales with simple controlled initial state, movement/waypoints, scheduled turns, and surface/deep exposure. Use named per-whale streams and physical-time schedules; whales never read vessel positions. Keep private IDs/dive state hidden.
- [ ] Implement noisy, range/probability-limited sightings and a due-report queue. Create opaque report IDs, record observation and availability times, and release only due reports. Keep observation noise keyed by sensor/sample/entity so a changed policy does not shift unrelated random draws.
- [ ] Build copied/serialized v2 observations with observable ship state/intent, reported detections, land/weather, decision ID, and virtual time. Cap/age evidence and publish counters. Do not pass a World, true-whale list, analytic exposure flags, or future reports to `decide`.
- [ ] Implement the main loop: spawn/apply due state changes; release sensor reports; decide when due; validate/queue actions; move the world with segment subdivisions; evaluate truth; record bounded frames/events. The default decision interval remains independent of render frequency.
- [ ] Consume `Decision.actions` and assessments. `NO_FEASIBLE_ROUTE` pauses/fails the affected voyage and marks the run incomplete; an ordinary empty action list retains the current valid plan. `INSUFFICIENT_FRESH_EVIDENCE` is not a declaration of no whales.
- [ ] Call the injected validator with `purpose="ISSUE"` before accepting, then `purpose="APPLY"` after any response lag. Ignore obsolete revisions/expired commands; repeated delivery of the same action is idempotent. Keep one seeded compliance outcome per voyage/incident.
- [ ] Implement `SLOWDOWN`, `DETOUR`, and `RESUME` exactly as the shared convention defines. Follow resolved future waypoints; at the explicit rejoin continue `original_route[index+1:]`. Never reset to the route start or read later source fixes.
- [ ] Keep core incident budget/revision state in the ship snapshot so the pure planner can respect cooldown and cumulative detour limits. Store control fields from the validated object, not explanatory metadata.
- [ ] Schedule finite expiry independently of the advisor. Use `purpose="EXPIRY_FALLBACK"` validation for an internal fallback: restore declared speed while following the remaining validated detour to its forward rejoin. No instantaneous shortcut. A failed remaining-path check produces an incomplete result and bounded diagnostic stop.
- [ ] Implement continuous swept relative-motion contacts and fixed encounter episodes. Use the declared conservative enclosing-circle model. Split intervals at waypoints, exposure changes, and commands; count boundary contacts once and track exit dwell in physical seconds.
- [ ] Accumulate truth-based smooth whale exposure separately from geometric contacts. Use a deterministic Gaussian segment integral or a small refinement-checked quadrature; sensor uncertainty must not redefine true positions. Clearly label the result a proxy.
- [ ] Accumulate weather exposure by passing actual simulated movement `TimedLeg`s to the injected Person 3 weather function. Compute added delay/distance against matched original-route voyages; use null deltas and visible censored diagnostics when incomplete.
- [ ] Return the agreed `RunResult`, include source/map/config hashes and `fixture_only`, and cap frames/events. Enforce the 30-second wall stop, bounded drain, and early completion when no scheduled voyages remain. A timeout is incomplete, not a successful fallback policy.

### Dependencies and independent development

| Expected dependency | Contract | Develop without waiting |
|---|---|---|
| Person 1 data/geometry | `RouteBundle`, `LandMap`, `project(Point)->(x_m,y_m)` | Use the common fixture recipe and a private pure projection helper with the identical origin/units. Keep it test-only and replace it with injected `shared.geo.project` at integration. |
| Person 3 decisions | `decide(obs)->Decision` | Write a scripted callback returning no action, then one valid detour at a fixed decision time, then resume. Test execution independently of optimization. |
| Person 3 validation | `validate_action(action,obs,purpose=...)->ValidationResult` | A private strict fixture validator resolves known paths and rejects selected malformed/stale cases. It must never become an always-accept production fallback. |
| Person 3 weather integration | `weather_exposure(legs,field)->severity-seconds` | For clear-weather unit tests return zero; for a synthetic storm use a known deterministic callback. Production wiring supplies the real function. |
| Person 4 UI | Optional `on_frame(frame)` | Run headlessly and collect result dictionaries; no Streamlit import in the simulator. |

### Completion checks

- [ ] A seeded fixture run completes without Person 1's API worker or Person 3's real planner.
- [ ] A scripted detour changes motion, preserves original route bytes/checksum, rejoins forward, and expires without teleporting or indefinitely slowing a ship.
- [ ] Lag/ignored/superseded/duplicate commands behave deterministically.
- [ ] Swept contact, episode exit dwell, hidden-truth isolation, and seed reproducibility pass focused tests.
- [ ] A route-cache run continues offline; changing a capture file after initialization cannot affect its world.

## 3. Person 3 — Whale prediction, route/weather scoring, and final validation

**Own:** `advisor/` and Person 3 tests/fixtures. Read Architecture §§7–9, the complete F1–F14 list, and the shared input/action conventions. This is the owner of all decision policy and final constraint rules; geometry primitives remain Person 1's responsibility.

**Deliver:** the pure decider and validator callbacks plus the weather integral. They must work against fixed JSON observations, without a running simulator or AIS connection.

### Tasks

- [ ] Implement `make_decider(config, geometry)` and `make_validator(config, geometry)` as ordinary closures. No network, simulator imports, hidden state lookup, or model training. Decision/revision identities derive from observable IDs/state, not random calls or current wall time.
- [ ] Filter/deduplicate report IDs and compute age using virtual time. Bound retained evidence, handle unknown motion with a stationary mean, and grow uncertainty with lookahead/age. A useful starting scale is `max(50 m, hypot(position_error_m, drift_mps*(age_s+t_s)))`; record assumptions.
- [ ] Implement relative CPA for each candidate leg, advancing the whale mean and uncertainty to that leg's actual start time. Handle zero relative velocity and finite intervals. The maximum risk across evidence/legs avoids duplicate-report confidence inflation.
- [ ] Screen the current path for whale watches, material storm exposure/blocks, active-plan changes, and expiry. Do not search all candidates on every UI update or when nothing relevant changed.
- [ ] Choose a common forward baseline rejoin, initially within fifteen minutes or the source clip's exit. `rejoin_route_index` must exceed `baseline_progress_index`; preserve remaining original-route intent. On off-route replans, compare incremental costs with the remaining current validated path and retain the original incident caps.
- [ ] Generate the current path plus at most three bounded doglegs around the threat interval: left, right, and one larger-offset alternative. Use approach/bypass/departure geometry, not an arbitrary heading. Reserve a slot for a validated baseline-return alternative when a detour is already active.
- [ ] Combine with at most three speeds (service, valid 10/8-knot choices), deduplicate, and stop at twelve candidates. Bound waypoints, offsets, time-to-rejoin, length, and delay. Cache geometry checks across speed variants. No A* or unbounded sampling.
- [ ] Construct complete future paths: current position → intermediate points → explicit rejoin → remaining baseline. Assign arrival/departure times using candidate speed, response delay, any lease fallback, and baseline continuation. Never evaluate every leg as starting at zero.
- [ ] Apply the hard checks before scoring. Reuse Person 1's water/clearance function and the same ship radius formula; check all legs, touching edges, and return paths. Validate bounds, speed, map readiness, input finiteness, forward progress, incident caps, and weather coverage/blocks.
- [ ] Implement `weather_exposure`. Intersect each timed leg with zone intervals, intersect with active time windows, partition overlaps, and integrate **maximum** severity. Return severity-seconds. Clear contributes zero; unknown time coverage returns an explicit failure rather than zero. A blocked-zone hit is a path rejection, independent of its soft score.
- [ ] Normalize weather by reference travel time, and delay/distance against the same reference endpoint. Implement Architecture §8.4's objective with the agreed starting weights and expose each contribution. Positive weather weight must rank worse weather lower when other factors match.
- [ ] Require minimum objective benefit and no material whale-risk increase. For a whale-triggered maneuver also require the configured whale-risk reduction. Favor stable current plans when changes are not worthwhile. Do not invent calibrated collision/death probabilities.
- [ ] Emit `SLOWDOWN`, `DETOUR`, or a freshly assessed `RESUME`; populate validity, IDs/revision, explicit rejoin, reasons, costs, and assessed/unmodeled factors. A new revision can renew a lease; repeating an action cannot.
- [ ] Return per-vessel assessments and rejected candidates even when there is no action. If baseline and all alternatives violate hard constraints, return `NO_FEASIBLE_ROUTE` for that vessel. Missing evidence or exhausted compute must have distinct status codes.
- [ ] Implement final action validation independent of candidate generation. `ISSUE` checks schema/revision/validity/path. `APPLY` rechecks against current observable ship position after lag and rejects materially stale geometry. `EXPIRY_FALLBACK` is an internal simulator purpose: check recorded remaining path/rejoin/weather without pretending an expired action is newly valid advice. All three still enforce physical constraints.
- [ ] Resolve the exact future route and incident budget into `ValidationResult.validated_action`. Do not let the executor independently guess a rejoin. Never silently repair/clamp a scored route or speed.
- [ ] Explicitly exclude F11–F14: no ship–ship checks, COLREG/TSS/international/local legality, bathymetric clearance, or realistic dynamics. Displayed lanes are contextual; crossing a lane is not a rejection reason in this version.

### Dependencies and independent development

| Expected dependency | Contract | Develop without waiting |
|---|---|---|
| Person 2 observations | `Observation` and observable ship progress/command state | Construct observations from the common fixture with an injected report at a chosen virtual time. No World or simulator import is needed. |
| Person 1 geometry | `project`, `ship_clearance_m`, `check_water_path`, `segment_zone_intervals` | Inject a private test geometry facade implementing simple rectangle/segment cases. It can return canned known failures for selected fixture paths; mark it test-only. Replace with the real module and rerun contact/boundary tests at integration. |
| Person 1 route/land inputs | The canonical fixture shapes | Use synthetic fixture provenance honestly. The algorithm's tests do not require a real stream or actual Santa Barbara polygons. |
| Person 4 presentation | `Decision.assessments`, costs, rejection codes | Return plain JSON data with fixed names; no UI imports or rendering in planner code. |

### Completion checks

- [ ] Known CPA, zero-relative-speed, aged report, and later-leg arrival-time cases pass.
- [ ] A known fixture yields an accepted detour and a valid explicit forward rejoin; another demonstrates slowdown without requiring a route change.
- [ ] Land-blocked-side and invalid-return cases reject correctly. Equal geometry/time/risk prefers clear weather, and blocked/timed weather is enforced.
- [ ] Counts never exceed twelve candidates/eight detour points; the action list never contains conflicting commands for a ship.
- [ ] Identical observation/configuration yields identical decisions, irrespective of hidden simulator truth or unrelated source capture updates.
- [ ] Final validator rejects malformed, stale, expired, superseded, out-of-bounds, and no-feasible cases; no `HOLD` alias is emitted.

## 4. Person 4 — Dashboard, scenario presets, comparison harness, and integration wiring

**Own:** `ui/`, `app.py`, `run_comparison.py`, `config/`, README/run documentation, and Person 4 tests/fixtures. Read Architecture §§1, 3, 10–14, 16, and the shared result/callback shapes.

**Deliver:** a working UI against injected test services first, then one production composition path connecting the other three owners. Do not reimplement their algorithms inside the UI.

### Tasks

- [ ] Write `config/default.json` using the agreed defaults and small scenario presets. Core modules receive those dictionaries; they never import Streamlit/session state to discover settings.
- [ ] Implement `build_run_inputs` to resolve selected route IDs, spawn virtual voyages, create controlled whale cases, and construct known simulated-weather coverage. Coverage should span the scenario, bounded drain, and local forecast. Edit presets before Run; no drag-and-drop editor is required.
- [ ] Build one Streamlit page with route capture/cache controls, selected source route IDs, seed/count inputs, whale-crossing/no-threat settings, and clear/storm/land-blocked-side cases. Provide one to three simple rectangular storm-zone inputs or presets.
- [ ] Make `render_dashboard(services)` accept a small mapping of ordinary callables: capture start/load/prepare/status, input builder, scenario runner, decider/validator factories, geometry, and weather integral. Bind production services only in `app.py`; private preview services remain under tests.
- [ ] During independent work, use a fake CaptureHandle, fixture RouteBundle/LandMap, canned Decision, and two-frame RunResult. Explicitly label the preview `fixture_only`; do not add a second production execution mode or a silent fallback import that masks missing modules.
- [ ] Keep one real capture handle across Streamlit reruns. UI polls copied status/records from Person 1; rendering never performs a blocking network read. Capture updates do not mutate an already built RunInputs/result.
- [ ] Render recorded AIS baseline, current simulated paths/detours, land/clearance context, storm zones, and reported whale sightings distinctly. Analyst-only truth overlay is optional and never used to build advisor inputs. Include source capture dates and provenance/assumed fields.
- [ ] Show per-vessel action, forward rejoin, predicted CPA/window, weather/delay/distance/risk cost breakdown, rejected factors, and unmodeled F11–F14. Simulated weather and ship positions must be labeled honestly.
- [ ] Gate the real demonstration on genuine AIS provenance and nonfixture usable land. A synthetic preview can demonstrate UI behavior, but cannot mark the integrated build complete. Missing key, route cache, map, or feasible plan has an explicit screen state.
- [ ] Implement a small `NO_ACTION` versus `ADVISOR` harness using identical frozen inputs and separate world instances. `NO_ACTION` returns `Decision(actions=[], assessments=[])`; never bypass the simulator's outcome accounting. A failed voyage/run stays in the comparison.
- [ ] Display/serialize summaries and paired differences. Do not subtract null/incomplete delay values or invent confidence intervals. Save manifest/results/optional frames under a unique run directory; no database.
- [ ] Wire Person 3's factories and Person 1's geometry into Person 2's runner, including the shared weather integral. Core wiring happens once; UI refresh does not trigger new decisions or recalculate a run.
- [ ] Add a simple batch entry point, for example `python run_comparison.py --bundle <file> --land <file> --scenario crossing --seed 0`; launch UI with `streamlit run app.py`. Document credential setup, real-data prerequisites, fixtures, runtime caps, and research-only assumptions.
- [ ] Own final end-to-end tests and timing, while each owner retains/fixes failures in their modules. Cut animation, extra seeds, and charts first if time runs short.

### Dependencies and independent development

| Expected dependency | Contract | Develop without waiting |
|---|---|---|
| Person 1 capture/data | CaptureHandle plus bundle/map shapes | Canned statuses (`AIS_KEY_MISSING`, capturing, usable cache), the common fixture, and a fake load/prepare function. |
| Person 2 engine/results | `run_scenario(...)->RunResult` and optional frame callback | Return two canned frames and a summary with COMPLETE/INCOMPLETE variants. Verify the map/table/empty/error states now. |
| Person 3 planner | Decider/validator factories and Decision diagnostics | Canned detour/rejection/cost records; render them without performing calculations. |
| Shared contracts | The handshake in this file | Plain dict fixtures work before Person 1 publishes Python types. Do not implement another schema or expose raw World state. |

### Completion checks

- [ ] UI and result formatting run with explicitly injected fixture services before other production modules are ready.
- [ ] Missing data, no sightings, `INVALID_MAP`, `NO_FEASIBLE_ROUTE`, capture failures, and incomplete/null metrics render correctly.
- [ ] Source lines, simulated paths, storm polygons, and reported/true whale layers cannot be confused.
- [ ] Full wiring later runs an accepted route change, source-state separation, weather preference, and paired comparison using the real module boundaries.

## 5. Integration and final acceptance — all four people, Person 4 coordinates

This is the fifth section after the four independent assignments. It is integration work, not a fifth implementation owner. Reserve approximately the last thirty minutes; merge ready interfaces continuously instead of waiting until the final minute.

### Join-up order

1. **Handshake review:** confirm the exact dictionary fields, point order, units, future-waypoint/rejoin convention, callback signatures, statuses, and configuration keys. Person 1 owns shared changes; update affected fixtures together rather than creating aliases silently.
2. **Core fixture wiring:** connect real shared geometry + real planner/validator/weather + real simulator on an explicitly synthetic fixture. No AIS dependency is needed to find type/clock/route mismatches. This proves integration, not real-data provenance.
3. **Real data substitution:** load Person 1's genuine route bundle and nonfixture land subset using the same shapes. Freeze checksums before running. Do not change algorithms to accommodate fake source flags or empty maps.
4. **Capture/source separation:** keep capture running or append records after initialization; prove that an accepted simulated detour and original route checksum are unaffected. Source status changes may update the capture panel only.
5. **Scenario acceptance:** run the required cases below with matched baseline/advisor inputs; inspect path/cost diagnostics and incomplete states.
6. **Timing and handoff:** measure default run/comparison duration, record assumptions/configuration/source hashes, fix critical mismatches, and write the shortest reliable demo walkthrough. Stretch work starts only after required checks pass.

### Required integrated acceptance

- [ ] At least one valid route is derived from actually received AIS records with API/provenance verification, not from fixture coordinates, COG extrapolation, or the placeholder lane file.
- [ ] The selected map is nonempty, spatially usable research land geometry with source/version/hash. Missing/invalid map blocks detours.
- [ ] A controlled whale encounter produces sightings, a decision, an accepted **route change**, simulated motion along that route, and a forward rejoin. A slowdown-only outcome does not satisfy the route-change case.
- [ ] A candidate with water-only waypoints but a land-crossing/touching/insufficient-clearance leg or return is rejected.
- [ ] Otherwise equal clear/stormy alternatives rank the clear one better; active blocked weather rejects a path and weather is evaluated at actual arrival times.
- [ ] All policies share the route bundle, whales, weather, schedules, response draws, and sensor randomness; no policy gets better compliance merely by sending extra messages.
- [ ] Expired/superseded/ignored commands do not cause teleporting, baseline rewinds, unsafe unchecked shortcuts, or indefinite slowdown. Fallback failures and no-feasible voyages remain incomplete.
- [ ] The advisor cannot access hidden whale state. No real vessel is controlled, and no legality/traffic/depth/realistic-dynamics support is implied.
- [ ] Default run target is under five seconds and the small paired comparison under thirty seconds on the available machine, **measured rather than asserted**. Exceeding the wall cap yields incomplete diagnostics, not a hung UI.
- [ ] After acquisition/static preparation, a frozen-cache comparison runs offline with the same seed/configuration. Missing actual AIS credentials or real land still leaves the corresponding full-demo acceptance item unmet.

### Coverage and conflict ownership

| Architecture concern | Implementation owner | Consumer / integration check |
|---|---|---|
| Real AIS provenance, baseline integrity | 1 | 2 freezes it; 4 displays/readiness-checks it |
| F1 land and F4 hull clearance primitives | 1 | 3 enforces them in every candidate/action; 2 revalidates through callback |
| F2 whale risk and F3 weather | 3 | 2 provides reports and truth metrics; 4 exposes scores/assumptions |
| F5 delay, F6 distance, F7 forward rejoin | 3 plans/validates; 2 executes/measures | 4 compares matched outcomes |
| F8 quality/uncertainty | 1 source/map; 2 sensor boundary; 3 decision gates | 4 labels limitations and blocks false readiness |
| F9 revisions/stability and finite commands | 3 proposes; 2 owns queued/applied state | 4 shows revisions, refusals, and expiry |
| F10 bounded computation | 3 candidate caps; 2 wall cap; 1 bounded capture | 4 measures and displays timing/deferred work |
| F11–F14 exclusions | All preserve | 4 labels them; no teammate adds those subsystems |

If integration reveals an API mismatch, the owning person fixes their boundary and the affected focused test. Do not write a second planner in the UI, a second simulator in the advisor, or a permissive validator to make the demo appear complete. When access/data readiness is blocked, hand over tested code, explicit blockers, and labeled fixtures; never hide the incomplete acceptance item.
