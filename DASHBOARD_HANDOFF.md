# Dashboard integration handoff

Branch: `arnavsatish/dashboard`. This branch changes only the dashboard-owned
UI, configs, composition root, comparison CLI, tests/preview, and run docs.
It does not replace sources, shared geometry/contracts, simulation, or advisor.

## Production imports

`app.production_services()` binds these exact interfaces from Tasks.md:

| Module | Names |
|---|---|
| `sources.ais` | `start_capture(config)` |
| `sources.routes` | `prepare_routes(records,land,config)`, `load_route_bundle(path,land,config)`, `save_route_bundle(bundle,path)` |
| `sources.land` | `load_land(path,config)` |
| `shared.geo` | `project`, `unproject`, `ship_clearance_m`, `check_water_path`, `segment_zone_intervals` |
| `simulation.engine` | `run_scenario(inputs,decide,validate,weather_exposure,*,project,on_frame=None)` |
| `advisor.planner` | `make_decider(config,geometry)` |
| `advisor.constraints` | `make_validator(config,geometry)` |
| `advisor.weather` | `weather_exposure(legs,field)` |

All paths above are under `save_the_whales`. Missing callables are displayed as
integration pending; no silent import aliases or fixture fallbacks exist.

## Source/geometry owner

Publish the agreed RouteBundle and LandMap dictionaries. For the production Run
button, source must have `kind=AISSTREAM_CAPTURE`, `api_verified=true`,
`fixture_only=false`; land must be `validated=true`, `fixture_only=false`, with
nonempty rings. Each route requires `validation.ok=true`. Shared version is 2.0.
The UI starts capture once, retains the handle, polls copied status, and prepares
copied records. It never accesses the key or injects AIS positions into a run.

Add shared dependencies to your owned requirements.txt: Streamlit >=1.50,
pandas >=2, pydeck >=0.9, pytest, plus your selected WebSocket client. The dashboard
adds no dependency on a backend server or other routing library.

## Simulation owner

The harness calls your runner twice with deep-copied identical inputs and the same
randomness configuration. Per-policy wall budget is at most 14.5 seconds, so a
small pair fits 30 seconds. Honour that budget and return incomplete diagnostics.

Frame ship paths follow the handshake: current_route excludes current position;
original_route includes index-0 start. Frames carry `sim_time_s`, `now`, ships,
whale_detections, alerts, optional analyst_truth. Return dictionaries containing
manifest, summary, frames, events, decisions. Summary failure/null semantics are
preserved; neither policy disappears from the outcome table.

Nested whale initialization fields were underspecified in the shared table. The
scenario builder currently emits:
`whale_id`, `initial_position={lat,lon}`, `speed_mps`, `heading_deg`, `waypoints`,
`exposure_schedule=[{start_s,end_s,exposed}]`. Accept or explicitly agree a change
with the dashboard owner before wiring. build_run_inputs also accepts explicit
`scenario_settings.whales` and `.weather`, preserving those dictionaries.
Default whales use northward crossing motion; non-threat whales are offset away.
No private whale IDs are ever used by the UI to construct advisor observations.

## Advisor owner

Factories receive config and a module-like geometry object. The same validator
is supplied for baseline and advisor worlds; NO_ACTION returns an ordinary empty
Decision and still goes through simulator accounting. Assessed paths, CPA fields,
cost breakdowns, rejections, revision IDs, and explicit rejoin indices appear in
the journal/full diagnostics without recalculating policy in presentation code.

Use `metadata.reason` for a short journal explanation. The full metadata and
assessment remain visible. Decisions may carry optional `sim_time_s` to support
accurate saved-frame inspection; without it the UI shows available run decisions.

## Integration acceptance

Run dashboard contracts first, then supply real paths through environment:

```sh
python -m pytest tests/test_ui_contract.py -q
WHALE_ROUTE_BUNDLE=data/ais/routes.json \
WHALE_LAND_FILE=data/land/santa_barbara.geojson \
python -m pytest tests/test_end_to_end.py -q
```

The real gate requires complete outcomes, at least one actual accepted route
change, real provenance, nonfixture land, and a measured pair under 30 seconds.
Land-clearance, weather ranking, command/rejoin/expiry, and sensor isolation tests
remain with their module owners. Dashboard fixture results cannot satisfy them.

At delivery against skeleton `d5f90a4`, owner implementations and genuine inputs
were still absent. The preview is runnable, but the integrated real-data demo is
explicitly pending those modules/artifacts.
