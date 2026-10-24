# tests/
Run with `pytest` from the repo root. Each test maps to the plan's §9.

| File | Purpose |
|---|---|
| `test_geo.py` | Projection vs haversine (<0.5%), point-in-polygon, segment intersection |
| `test_strike.py` | Lethality values, tunnelling regression, boundary dedup, every pass counted exactly once |
| `test_isolation.py` | `your_project/` never imports `simulator`; `NoAction` returns `[]` |
| *(TODO)* `test_engine.py` | Determinism, whale paths identical across scenarios, every voyage exits, No-Action has zero delay, waypoint carry-over, command expiry |
| *(TODO)* `test_commands.py` | Unknown ship rejected, speed clamped, detour across an island rejected |
