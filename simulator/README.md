# simulator/
Ground truth (Subteam A). **Advisor code must never import from here.** All geometry works in local XY km (`geo.project`).

| File | Purpose | Status |
|---|---|---|
| `config.py` | Loads `config/default.json` into a `Config` | done |
| `world.py` | Loads the static world (land, lanes, VSR zone, density) | done |
| `geo.py` | Projection, haversine, point-in-polygon, segment–land intersection (§4, §5.1) | done |
| `strike.py` | Swept crossing check, jitter kernel, V&T lethality (§5.5) | done |
| `whales.py` | Whale population: random-walk movement, dive cycle, per-whale random streams, grid lookup (§5.3) | skeleton |
| `ships.py` | Voyage schedule, `Fleet` registry, waypoint kinematics and sub-segments (§5.4) | skeleton |
| `sensors.py` | Visual detections (MVP), acoustic buoy (stretch), counter-based randomness (§5.6) | partial |
| `commands.py` | Action validation, command queue and lifecycle, rejoin (§5.7) | skeleton |
| `metrics.py` | `Ledger` that accumulates the §8.2 metrics, plus the summary | done |
| `baselines.py` | No-Action, Blanket-VSR and Oracle advisors (§8.1) | partial |
| `engine.py` | Main loop (§7) | done (depends on the skeletons) |
