# tests/fixtures/advisor/

Person 3's private, **test-only** inputs. Observations are hand-built v2 dicts on the Tasks.md recipe route with toy land and synthetic reports. They are not real AIS, coastline or whale data.

| File | Purpose |
|---|---|
| `config.fixture.json` | Proposed `planner` section (Tasks.md defaults plus the planner's template sizes, triggers and lease margin) and `response` |
| `adv_fixtures.py` | Builders: `observation`, `ship_on_route`, `report`, `storm`, `load_land` (via Person 1's `load_land` on the common toy maps). Running it rewrites `obs_crossing.fixture.json` |
| `obs_crossing.fixture.json` | Frozen observation at t = 60 s: ship on `fixture-route-001` at 18 kn, one surfaced-whale report at (34.2, −120.065) observed at t = 0, clear weather, `land_far` toy map |
