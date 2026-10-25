# tests/fixtures/common/

Shared toy inputs from the `Tasks.md` fixture recipe, published by Person 1. **Fixture-only:** these are not real AIS or verified coastline, and they cannot satisfy the real-data acceptance gates.

| File | Purpose |
|---|---|
| `route_bundle.fixture.json` | `RouteBundle` with one route: 4 waypoints along lat 34.2 from lon −120.1 to −120.025, 18 kn, 300 × 40 m, MMSI `999000001`, `SYNTHETIC_FIXTURE` |
| `land_blocked_side.fixture.geojson` | Toy land rectangle at lat 34.204–34.216, lon −120.075 to −120.055. About 445 m north of the route: the baseline passes, north doglegs hit land. Load with `load_land` |
| `land_far.fixture.geojson` | The same rectangle moved about 11 km north, for tests that need valid but distant land |
| `weather_clear.fixture.json` | `WeatherField` with known coverage `[0, 12600]` s and no zones |
| `weather_storm_south.fixture.json` | One south-side storm zone, severity 0.8, active `[0, 3600)`, `blocked=false`. Clear of the baseline |
| `example_whale_detection.fixture.json` | One whale report near (34.2, −120.065), with an opaque report ID |
