# tests/fixtures/simulation/

Person 2's private, **test-only** inputs and helpers. Nothing here is real AIS, verified coastline, or a production advisor/validator.

| File | Purpose |
|---|---|
| `config.fixture.json` | Proposed `simulation`, `sensors`, `response`, `metrics` sections (Tasks.md defaults), plus a minimal `planner` subset used only by the fixture validator. `fixture_config()` merges it over `config/default.json` |
| `scenario_crossing.fixture.json` | Recipe scenario: one voyage on `fixture-route-001` at entry 0, one stationary, always-surfaced whale at (34.2, −120.065), clear weather `[0, 12600]` s |
| `sim_fixtures.py` | Private projection copy, `make_inputs`, `no_action`, scripted deciders and action builders, the strict `make_fixture_validator`, and the fixture weather integrals `zero_weather` / `make_fixture_weather_exposure` |
| `trace_p1.py` | Integration trace against Person 1's real loaders and `shared.geo`: profiles cross-module calls, checks boundary payloads against `shared.contracts`, and re-checks simulated motion. Run with `.venv/bin/python tests/fixtures/simulation/trace_p1.py` |

Handshake shapes the simulator expects from other owners (also in `shared/contracts.py`):

- `ValidationResult.validated_action` (Person 3) is a `ValidatedAction`: `{action, remaining_waypoints, rejoin_route_index, effective_target_speed_knots, incident_budget}`.
  - `action` must be the submitted action, unmodified.
  - `remaining_waypoints` excludes the current position and ends with `original_route[rejoin:]`.
- `Scenario.whales` (Person 4) are `ScenarioWhale` specs; see `save_the_whales/simulation/whales.py`.
