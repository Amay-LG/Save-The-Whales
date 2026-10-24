# Save-The-Whales
We're gonna save the whales.

A whale-ship strike simulator and testbed for real-time alert and detour advisors in the Santa Barbara Channel. The design is in `my-artifacts/simulation_implementation_plan_v2.md`; "§" references in the code point to that plan.

| Path | Purpose |
|---|---|
| `config/` | Tunable parameters (time step, whale counts, sensor settings, seeds) |
| `data/` | Geometry: land, lanes, speed-reduction zone, whale density |
| `contract/` | Shared input/output schema between the simulator and the advisor. **Frozen at hour 2** |
| `simulator/` | Ground-truth world: whales, ships, sensors, strikes, metrics (Subteam A) |
| `your_project/` | The alert and detour advisor (Subteam B). May import only from `contract/` |
| `tests/` | pytest suite (`pip install -r requirements.txt && pytest`) |
| `runs/` | Run outputs: results and trajectory logs. Git-ignored |
| `my-artifacts/` | Planning docs. Git-ignored |
| `run_comparison.py` | Runs every scenario on every seed and prints a table with confidence intervals. Add `--quick` for a short smoke run |
| `app.py` | Demo: replays one run and shows the scoreboard |
| `requirements.txt` | Python dependencies |
