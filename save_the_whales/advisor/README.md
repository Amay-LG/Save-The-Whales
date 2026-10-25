# Task 3: prediction and route advice

Implemented against [Architecture.md](../../Architecture.md) §§7–9 and the
Person 3 assignment in [Tasks.md](../../Tasks.md). This package uses the standard
library only. It does not import the simulator, AIS adapter, UI, or hidden whale
state. It issues commands for simulated ships only.

## Integration

```python
from save_the_whales.shared import geo
from save_the_whales.advisor import make_decider, make_validator, weather_exposure

decide = make_decider(config, geo)
validate_action = make_validator(config, geo)
decision = decide(observation)
# Person 2 calls validate_action for ISSUE, APPLY, and EXPIRY_FALLBACK.
# Person 4 passes these callbacks and weather_exposure to run_scenario.
```

The geometry facade must implement `project`, `unproject`, `ship_clearance_m`,
`check_water_path`, and `segment_zone_intervals` exactly as specified in Tasks.md.
`check_water_path.min_clearance_m` is the minimum distance to land **before**
subtracting the requested clearance radius. Boundary touches are rejected.
`segment_zone_intervals` includes tangent/boundary contacts and returns `(0,1)`
for a stationary point inside/on a zone.

The standalone, two-argument `weather_exposure(legs, weather)` lazily uses
`shared.geo`. It also accepts `geometry=facade` for isolated tests. No permissive
production geometry fallback exists: an unimplemented shared helper raises an
explicit integration error. Unknown weather raises `ValueError("UNKNOWN_WEATHER")`.

Inputs/outputs are JSON dictionaries in contract 2.0. Factories copy configuration;
decisions do not mutate observations or retain policy state between calls.
`Decision.assessments` provides status, costs and their weighted contributions,
complete selected path, candidate count/rejections, and CPA information. Risk is a
deterministic proximity/severity proxy, never a collision/death probability.

## Command handoff details for Person 2

1. For each action, call `validate_action(action, observation, purpose="ISSUE")`.
   Persist the returned `incident_budget` and latest accepted revision when queuing
   the action. Account for actual distance/time during response lag in that same
   budget. Deduplicate previously accepted action IDs before this call.
2. At execution time call the validator again with `purpose="APPLY"`. ISSUE needs
   `revision > latest_revision`; APPLY needs equality. Do not recompute a new budget
   from a moved ship or discard the initial incident reference.
3. Follow `validated_action.remaining_waypoints`, starting from the actual current
   position at application. These omit the current position and include the rejoin
   exactly once plus the original continuation. Respect the resolved target speed.
4. Keep `incident_budget.accumulated_distance_m` and `accumulated_time_s` updated.
   Clear the completed budget at its explicit rejoin, after preserving monotonic
   baseline progress. A replan cannot select a new endpoint/reset these caps.
5. At lease expiry pass the original action with `purpose="EXPIRY_FALLBACK"` and a
   fresh observation. The validator uses **unpassed current route**, stored rejoin,
   and service speed; it does not replay the original detour points. It still
   rejects newer revisions, bad land/geometry, exhausted budgets, or blocked/unknown
   weather. Failure must make the voyage incomplete, not trigger an unchecked
   shortcut. Speed remains leased until expiry (or a new command); reaching a
   rejoin does not alone extend or renew it.

`validated_action` contains `action` (the unchanged original),
`remaining_waypoints`, `rejoin_route_index`, `target_speed_knots`, and
`incident_budget`. Additional diagnostics include `selected_path`, `timed_legs`,
`local_distance_m`, `local_eta_s`, `weather_exposure_s`, and `clearance_radius_m`.
For ISSUE, diagnostic paths/times include the old-path response prefix; execution
uses the APPLY resolution instead of executing that prefix twice.

Generated actions add a small **core** `source_snapshot` dictionary, containing
`position`, `current_route`, `sog_knots`, `service_speed_knots`, `now`, `sim_time_s`,
`baseline_progress_index`, `decision_id`, and `active_command`. Preserve it through
queuing. APPLY uses its old intent/lease to detect motion inconsistent with the
issued snapshot; a detour without this core snapshot is stale at APPLY. This data
is deliberately outside explanatory `metadata`. Snapshot motion comparison allows
100 m by default. All paths are physically rechecked after lag regardless.

`RESUME` preserves every unpassed baseline waypoint when already on baseline.
During a detour it proposes a new, checked connection to the explicit forward
rejoin. A speed change while preserving an active detour uses `SLOWDOWN` even when
restoring service speed. No command is renamed or clamped after scoring.

## Policy and bounded work

- Filter virtual-time reports, deterministically deduplicate IDs, retain at most
  50, and expand uncertainty with report age and each actual leg's arrival time.
  Mean movement is stationary unless an optional explicit projected
  `velocity_mps={x,y}` is reported; reports never identify true animals.
- Screen the current plan, then evaluate current/left/right/larger-offset
  geometry at up to three speeds (service, valid 10/8 knots). Reserve a return
  template for active detours. At most 12 candidates/eight detour points; share
  land checks across speed variants. Clear speed-only lease reassessments avoid
  unnecessary dogleg searches.
- Validate the current-position connection, response path, detour, rejoin, and
  full remaining baseline. Forecast restoration of service speed at lease expiry
  on the same geometry. Reject departure from bounds, land contact/clearance,
  backtracking, excessive distance/delay/time, invalid inputs and blocked weather.
- Integrate maximum severity over geometric crossings and half-open active times;
  severity zero does not exempt a blocked polygon. Unknown coverage is never clear.
- Normalize costs at a common forward endpoint. Initial references use baseline;
  replans compare incremental remaining current intent. Signed delay/distance
  deltas credit a shorter/faster return; cumulative incident caps stay fixed.
- Require objective improvement, no material whale-risk increase, and a whale
  benefit for whale-triggered maneuvers. Expiring slowdown renewal uses a fresh
  fallback assessment. Missing fresh reports cannot alone justify resuming or
  speeding up; expiry still follows the simulator's finite, checked fallback.

The planner exposes `NO_FEASIBLE_ROUTE`, `UNKNOWN_WEATHER`, `INVALID_MAP`,
`INSUFFICIENT_FRESH_EVIDENCE`, `NOT_EVALUATED_BUDGET`, and ordinary keep/cooldown
states separately. A missing/shared integration implementation is not a safe
empty result. Baseline AIS provenance/readiness remains Persons 1/2/4's upstream
responsibility: the observable ship contract supplies route intent, not a route
bundle. Synthetic test data never establishes real-data readiness.

Configuration comes from `planner`, `sensors`, and `response` dictionaries.
Architecture/Tasks defaults are in `_common.DEFAULTS`, used only when absent.
Additional defaults: `lease_s=900`, `watch_distance_m=500`, `watch_risk=0.15`,
`weather_trigger_s=1`, `stale_position_tolerance_m=100`, `max_route_points=256`.
Missing dimensions use 400 m/60 m with an extra 100 m margin and a visible
assumption. Normal supplied dimensions use `hypot(length,beam)/2 + margin`.
Unsupported nonfinite/extreme settings fail before planning. The local rejoin
horizon is capped at 3600 s. Weather fields allow up to 32 zones/256 vertices each.

F11–F14 are deliberately unmodeled: ship–ship interactions, maritime legality,
depth/draft, and realistic turning/stopping/current/wave physics. There is no
training, optimization service, route-search framework, or new dependency.

## Independent verification

```bash
python3 -m unittest discover -s tests -p test_advisor.py -v
```

Tests use the explicit **synthetic**, private geometry/input fixtures in
`tests/fixtures/advisor/`. They cover CPA/relative rest/later legs, evidence age and
duplicates, accepted detour and slowdown, land/continuation/rejoin rejection,
weather maxima/timing/blocks, revision and lease semantics, source staleness,
response lag across an old lease, renewal, preserved baseline bends, no-feasible
and missing-data cases, deterministic decisions, and a manual execution handoff.
No pytest installation is needed (pytest can also collect the unittest suite).

The clear detour fixture explicitly uses `planner.drift_mps=0`; another fixture
uses the default drift allowance and a tight distance cap to demonstrate slowdown.
These settings are test scenarios, not fitted biological models.

An isolated benchmark on this machine evaluated 120 decision calls with five
synthetic ships and twenty reports in approximately 1.84 s using the test geometry.
This is not a full-simulator or real-land performance claim. Rerun the focused
suite with Person 1's geometry, then the integrated acceptance/timing checks once
the other owners' modules are implemented.
