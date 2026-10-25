# Save the Whales

A Santa Barbara ocean lab for comparing automatic whale-aware navigation in a
simulated world. Real AIS observations supply recorded route geometry. Ships,
whales, sightings, and storm fields are simulated. No real vessels are controlled.

## Dashboard quick start

Use Python 3.10+ and the team's shared requirements when populated. While the
shared dependency file is being prepared, the dashboard needs:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install 'streamlit>=1.50,<2' 'pandas>=2' 'pydeck>=0.9' pytest
streamlit run app.py
```

The production entry point binds the real source, geometry, simulation, and
advisor modules. Missing owner functions are listed explicitly; the Run button
stays disabled. It never silently imports test services or makes up a route.

For the independently testable dashboard preview:

```sh
streamlit run tests/ui_preview.py
```

This is prominently marked **FIXTURE PREVIEW**. Routes, land, decisions, and
comparison numbers are canned synthetic UI data, not measured planner results.
It proves presentation and callable wiring only. The fixture validator rejects
production use; it is not an always-accept substitute.

## Real-data walkthrough

1. Set `AISSTREAM_API_KEY` in your shell as required by the source owner's adapter.
   Never put it in a dashboard field, artifact, or committed file.
2. Load validated regional land and a genuine AIS route bundle under
   **Routes & land**, or start capture and freeze its copied records into a bundle.
   A background capture handle survives Streamlit reruns. UI status polls never
   do network reads, and capture changes apply only to future runs.
3. Select route IDs, seed, ship/whale counts, and a preset: crossing, no nearby
   threat, storm tradeoff, or coast-side constraint. Add one simulated storm with
   editable center, size, severity, and optional impassable setting.
4. Run the paired comparison once. The harness deep-copies the same frozen inputs
   into separate `NO_ACTION` and `ADVISOR` worlds. UI refresh never re-runs policy.
5. Inspect recorded/current/assessed routes, simulated sightings, active weather,
   action revisions/rejoins, predicted CPA and rejected candidates. Hidden truth
   is an explicitly opt-in analyst display, never an advisor input.
6. Review the paired outcomes and download JSON. Incomplete/failed policies stay
   visible and have no computed paired differences. Real runs save under unique
   `runs/<timestamp>-<id>/` folders.

A coast-side preset does not fabricate land or guarantee a rejected north-side
path. Use a suitable real coastal clip or the explicit common test fixture to
exercise that acceptance case. All presets are scenario construction, not a
second routing algorithm.

## Headless comparison

```sh
python run_comparison.py --bundle data/ais/routes.json \
  --land data/land/santa_barbara.geojson --scenario crossing --seed 0
```

Requires production owner modules and genuine validated data. Missing interfaces,
fixture provenance, invalid land, and unavailable routes produce clear nonzero
exit status. It shares the dashboard's composition root and orchestration.

## Verification

```sh
python -m pytest tests/test_ui_contract.py tests/test_end_to_end.py -q
```

The owner-module end-to-end test skips explicitly until all interfaces and real
artifacts are supplied. To enable it, set `WHALE_ROUTE_BUNDLE` and
`WHALE_LAND_FILE`. A skipped real-data test is an unmet integration gate, not a
successful full-demo claim. The preview and UI contract tests run independently.

The simulator must enforce the wall limit it receives. Each policy receives an
identical maximum 14.5-second budget in a paired run. The harness measures total
runtime and marks a pair incomplete if it exceeds 30 seconds; it does not create
an uncancellable thread or conceal a runner that fails to enforce its budget.

See [DASHBOARD_HANDOFF.md](DASHBOARD_HANDOFF.md) for exact integration boundaries.
Architecture.md and Tasks.md remain the behavioral and shared-contract reference.

## Assumptions

One AIS-seeded simulation mode; no training or LLM. Reported whale evidence is
synthetic and distinct from hidden simulator truth. Geography is research-grade,
not a navigation chart. No ship–ship coordination, COLREGS/TSS legality, depth
clearance, or realistic ship manoeuvring is claimed. Outcome scores are model
proxies, never real deaths avoided or calibrated collision probabilities.
