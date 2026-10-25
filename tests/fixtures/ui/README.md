# tests/fixtures/ui/

Person 4's **test-only** inputs. Every file here is `SYNTHETIC_FIXTURE` / `fixture_only`: no real AIS, no real coastline, no real run.

| File | Purpose |
|---|---|
| `ui_fixtures.py` | `preview_services(case)` holds a fake `CaptureHandle`, the fixture bundle, a toy `LandMap` and canned run pairs. Its advisor, simulator and weather entries raise, which proves the UI computes nothing itself. Cases: `complete`, `incomplete`, `no_sightings`, `invalid_map`, `key_missing`, `no_routes`, `error` |
| `run_pair_complete.fixture.json` | Two-frame `NO_ACTION`/`ADVISOR` results for the fixture crossing, with one accepted `DETOUR` |
| `run_pair_incomplete.fixture.json` | Derived from the complete pair: the voyage fails with `NO_FEASIBLE_ROUTE`, so delay/distance are null |
| `capture_status.fixture.json` | Canned capture statuses: `key_missing` (`AIS_KEY_MISSING`), `capturing`, `degraded` |
| `make_ui_fixtures.py` | Regenerates the canned JSON with the real modules on the fixture route: `.venv/bin/python tests/fixtures/ui/make_ui_fixtures.py` |
| `trace_integration.py` | Cross-owner trace of Architecture §5, described below. Run it with `.venv/bin/python tests/fixtures/ui/trace_integration.py` |

What `trace_integration.py` does:

1. Feeds AISStream-shaped frames through Person 1's capture handle over a fake websocket.
2. Prepares routes on the real land map.
3. Relabels the in-memory bundle `SYNTHETIC_FIXTURE`.
4. Runs the presets through the real simulator and advisor.
5. Profiles every owner-crossing call and checks each boundary payload.
