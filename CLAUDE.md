# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`Architecture.md` controls product behaviour. `Tasks.md` holds the team split, the shared data shapes, the defaults and the failure codes. Read the relevant section before changing a boundary.

## Commands
- Setup: `uv venv .venv && uv pip install -r requirements.txt`
- Tests: `.venv/bin/python -m pytest [tests/test_x.py]`. Never use bare `pytest`: there's no conftest, so `save_the_whales` isn't importable for single-file runs.
- Lint: `.venv/bin/ruff check <paths>`
- Dashboard: `.venv/bin/streamlit run app.py`. Fixture-service preview: `.venv/bin/streamlit run tests/ui_preview.py -- --case incomplete`
- Paired run on the fixture route: `.venv/bin/python run_comparison.py --bundle tests/fixtures/common/route_bundle.fixture.json --scenario crossing --no-save`
- Integration traces: `.venv/bin/python tests/fixtures/ui/trace_integration.py`, `.venv/bin/python tests/fixtures/simulation/trace_p1.py`

## Python
- Python 3.9 is the floor. Add `from __future__ import annotations` wherever hints use `X | Y` or `list[...]`, follow the existing `typing.Dict/List/Optional` style, and don't use `match`.
- Lint only (ruff). Don't reformat code you aren't changing.

## Ownership and contracts
- Every path has an owner (Person 1–4, `Tasks.md` §1–4). Each developer's `CLAUDE.local.md` says which Person they are. Ask before editing another owner's files.
- Shared shapes are defined in `save_the_whales/shared/contracts.py` (Person 1): plain dicts and TypedDicts, contract `"2.0"`. Don't use Pydantic, competing schemas or silent field aliases. On a mismatch, the owner fixes their own boundary and its test.
- Points are `{"lat", "lon"}`; only GeoJSON uses `[lon, lat]`. Contract speeds are in knots and internal velocity in m/s. UTC strings end in `Z`.
- All defaults live in `config/default.json`. Pass config explicitly or through a closure, never as module constants.
- Use the fixed failure-code strings from `Tasks.md` (`AIS_KEY_MISSING`, `INVALID_MAP`, `NO_FEASIBLE_ROUTE`, …). Return structured issues, not empty success-shaped objects.
- `tests/test_end_to_end.py::test_static_imports_respect_owner_boundaries` enforces the import boundaries:
  - `shared` imports no sibling package.
  - `advisor` receives geometry by injection and imports no `shared.geo`, simulation, sources, ui or network modules.
  - `simulation` imports no sources, advisor, ui or streamlit.
  - `ui` imports no sources or advisor.
  - Only `app.production_services()` composes the four.

## Data and secrets
- Never read, print or write `.env`. `AISSTREAM_API_KEY` comes from the environment at runtime, and `.env.example` holds names only. A hook enforces this.
- Label fixture data (`source.kind=SYNTHETIC_FIXTURE`, `fixture_only=true`, fake MMSIs `999…`). Never present it as real AIS or verified coastline.
- `data/land/santa_barbara.geojson` is hash-checked against `data/land/provenance.json`, so editing it breaks loading (`MAP_HASH_MISMATCH`).
- `data/ais/*` is git-ignored, so a fresh clone has no route bundle. Use the fixture bundle.
- Tests never touch the network: they use a fake websocket and patched sockets.

## Git
- Start commit subjects with the owner (`Person N: …` or `Persons 2-4: …`). Put a bulleted per-path summary in the body.
