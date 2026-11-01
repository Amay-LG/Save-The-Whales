---
name: integration-check
description: Check the integrated system against Tasks.md §5 "Required integrated acceptance": full test suite, integration traces, timed paired comparisons for each scenario preset, and real-data readiness. Use when asked whether the project is demo-ready, integrated, or passes final acceptance.
---

Report where the integrated system stands against `Tasks.md` §5. Don't change any files. Never read `.env`.

1. **Re-read** "## 5. Integration and final acceptance" in `Tasks.md`, especially "### Required integrated acceptance". Don't rely on memory.
2. **Suite.** Run `.venv/bin/python -m pytest -q` and record the counts and failing test names.
3. **Traces.** Run `.venv/bin/python tests/fixtures/ui/trace_integration.py` and `.venv/bin/python tests/fixtures/simulation/trace_p1.py`, and note any failure.
4. **Timed comparisons.** For each preset in `config/scenarios/`, run `.venv/bin/python run_comparison.py --bundle tests/fixtures/common/route_bundle.fixture.json --scenario <name> --no-save` and measure the wall time. The targets are under 5 s for a default run and under 30 s for the pair. Measure the times rather than asserting them.
   - If `data/ais/route_bundle.json` exists, also run with `--bundle data/ais/route_bundle.json --land data/land/santa_barbara.geojson`.
5. **Real-data readiness.**
   - Route bundle: `source.kind` must be `AISSTREAM_CAPTURE` with `api_verified=true`, and at least one route must have `validation.ok=true`. `AIS_ROUTE_UNAVAILABLE` or a missing bundle means NOT MET.
   - Land: `data/land/provenance.json` must have `fixture_only=false`, a hash, and no `PENDING` visual checks.
   - Fixture runs prove wiring only. They never satisfy a real-data acceptance item.
6. **Report** one table with one row per "Required integrated acceptance" item: `item | status (MET / NOT MET / UNVERIFIED) | evidence`. Follow it with the timing numbers, then at most 3 bullets naming the blocking items and the owning Person, using the coverage table in §5. Keep it terse.
