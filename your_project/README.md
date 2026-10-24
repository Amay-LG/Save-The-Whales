# your_project/
The alert and detour advisor (Subteam B). **Import only from `contract/` and this package.** A test enforces this.

| File | Purpose | Status |
|---|---|---|
| `advisor.py` | Entry point: `Advisor.advise(observation) -> list[Action]`. Target ≤20 ms per call | stub (returns `[]`) |
| `detector.py` | Merges duplicate detections, projects each ship's path ahead, computes time and distance of closest approach (TCPA/CPA) vs whales | skeleton |
| `router.py` | Decides SLOWDOWN vs DETOUR; generates land-safe waypoints | skeleton |
