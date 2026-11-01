# Save the Whales: AIS-seeded whale simulation

Real AIS supplies the **baseline route geometry** for the Santa Barbara Channel. The ships that follow those routes, the whales, the sightings and the weather are all **simulated**. An autonomous advisor predicts whale encounters and changes the simulated ships' routes or speeds. No real vessel is controlled. [Architecture.md](Architecture.md) is the behavioral spec and [Tasks.md](Tasks.md) is the team handshake.

## Setup

```bash
uv venv .venv
uv pip install -r requirements.txt       # Python 3.9+; pytest, pandas, streamlit, pydeck, websockets
```

For a live AIS capture, set `AISSTREAM_API_KEY` in your environment. `.env.example` lists the variable names. The key is never written to captures, bundles, run artifacts or logs. Without it, the capture panel shows `AIS_KEY_MISSING` straight away, and runs still work from a cached route bundle.

## Run

| What | Command |
|---|---|
| Dashboard | `.venv/bin/streamlit run app.py` |
| Paired comparison (CLI) | `.venv/bin/python run_comparison.py --bundle data/ais/route_bundle.json --land data/land/santa_barbara.geojson --scenario crossing --seed 0` |
| Same, on the synthetic fixture route | `.venv/bin/python run_comparison.py --bundle tests/fixtures/common/route_bundle.fixture.json --scenario crossing` |
| Dashboard preview with injected **test** services | `.venv/bin/streamlit run tests/ui_preview.py -- --case incomplete` |
| AIS capture to a cache file | `.venv/bin/python -m save_the_whales.sources.ais --seconds 1800 --out data/ais/capture_<date>.jsonl` |
| Routes from captures | `.venv/bin/python -m save_the_whales.sources.routes --capture <jsonl> --land data/land/santa_barbara.geojson --out data/ais/route_bundle.json` |
| Tests | `.venv/bin/python -m pytest` (from the repo root; bare `pytest` can't import the package) |
| Cross-owner integration trace | `.venv/bin/python tests/fixtures/ui/trace_integration.py` |

`--scenario` takes a preset ID from `config/scenarios/` or a path to a settings JSON file. `--voyages`, `--whales` and `--seed` override the preset. Artifacts go to `runs/<run_id>/` (see [runs/README.md](runs/README.md)).

## Demo walkthrough

1. **Data readiness.** The page shows the route bundle's provenance (`AISSTREAM_CAPTURE` vs `SYNTHETIC_FIXTURE`, API-verified, capture time, checksum) and the land map's source and hash. The real demo is gated on genuine API-verified routes **and** validated non-fixture land. A fixture run is labeled `fixture_only` and never counts as acceptance evidence.
2. **Optional capture.** Under **AIS capture**, start a capture, then **Prepare routes from capture**. This freezes a new bundle file. Capture updates never touch a run that is already built.
3. **Scenario.** Pick a preset (table below), adjust the seed, voyage count, designated whale and 1–3 storm zones, then press **Run NO_ACTION vs ADVISOR**. Both policies run on identical frozen inputs.
4. **Read the outcome.** The comparison table shows paired differences. `n/a (incomplete)` means a voyage did not finish; it is never a zero.
   - The map draws, each in its own style:
     - the recorded baseline
     - simulated tracks and current detours
     - simulated storms
     - sighting reports
     - an optional analyst-truth overlay, which is never advisor input
   - The alerts table lists each action with its rejoin index, CPA/time, evidence age and cost breakdown. Below it are rejected-candidate factors and the unmodeled factors F11–F14.

| Preset | What it shows |
|---|---|
| `crossing` | A stationary, surfaced whale halfway along the route. `NO_ACTION` makes contact; the advisor detours and rejoins forward |
| `no_threat` | 5 voyages, with a whale 6 km off the route (beyond the 3 km watch). No action expected |
| `storm_detour` | A severity-0.8 storm straddling the middle third of the route. The advisor detours to cut weather exposure |
| `land_blocked_side` | **Fixture-only.** A toy land rectangle north of the fixture route, so the north dogleg is rejected and the ship passes south |

## Layout and owners

| Path | Owner | Role |
|---|---|---|
| `save_the_whales/shared/`, `save_the_whales/sources/`, `data/` | 1 | Contracts, projection/geometry, AIS capture, route preparation, land map |
| `save_the_whales/simulation/` | 2 | Hidden world, sensors, command execution, truth metrics, `run_scenario` |
| `save_the_whales/advisor/` | 3 | Risk, candidates, weather integral, constraints/validator, planner |
| `save_the_whales/ui/`, `app.py`, `run_comparison.py`, `config/`, `runs/` | 4 | Presets and `build_run_inputs`, map layers, dashboard, production wiring, paired harness |

`app.production_services()` is the only place the four owners' modules are composed. The dashboard and the CLI both consume that mapping. All defaults live in `config/default.json` under `ais`, `simulation`, `sensors`, `response`, `metrics`, `planner` and `scenarios`. The production response lag is 30 s. Acceptance tests use lag 0 with full compliance, plus explicit lag and ignored-command cases.

## Runtime (measured 2026-10-25 on an Apple M2)

| Run | Wall time | Cap |
|---|---|---|
| Default world (5 voyages, 20 whales), advisor | about 0.8 s | 5 s target; stops at `wall_limit_s=30` as `INCOMPLETE` |
| Paired comparison of the default world | about 1.1 s | 30 s |
| Planner | at most 4 geometries × 3 speeds = 12 candidates per threatened ship, at most 8 detour waypoints | |

## Assumptions and limits (research only)

- Ships, whales, sightings and weather are simulated. AIS supplies the baseline geometry only, and later AIS messages never overwrite simulated motion.
- Land is Natural Earth 1:10m generalized coastline, a research map and not a navigation chart. "Passable" means land-free with enclosing-circle hull clearance (`0.5·hypot(L, B) + 100 m`).
- Turns and speed changes are instantaneous. There is no ship–ship separation (F11), no COLREG/TSS or other legality checks (F12), no depth or draft (F13), and no realistic dynamics (F14).
- Whale risk is a deterministic proximity proxy, and `whale_exposure_proxy` is a smooth diagnostic. Neither is a collision probability or a count of deaths avoided. Each case uses one seed: no confidence intervals.
- If a policy sails through an active **blocked** storm zone, the comparison flags it as a hard-constraint violation instead of hiding it.

## Status

- Code for all four owners is integrated and passes the Tasks.md §5 checks on the synthetic fixture route.
- **Not yet met:** a genuine AIS-derived usable route. The captured bundles in `data/ais/` report `AIS_ROUTE_UNAVAILABLE`, so the real-route acceptance item stays open until a longer capture yields a valid segment.
