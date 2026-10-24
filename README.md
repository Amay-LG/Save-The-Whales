# Save-The-Whales

## Santa Barbara geographic simulation

Run `python3 -m whaleguard --scenario santa-barbara --port 8766`, then open
http://127.0.0.1:8766. It shows the Santa Barbara coastline, nine archived NOAA AIS
tracks, historical humpback observations, and five simulated ships. WG-01 follows
the recorded VENUS HORIZON passage and automatically replans for staged whale
zones and predicted simulated traffic encounters.

For optional live AISStream reports, run `zsh scripts/run_santa_barbara.zsh`.
This prepares a local virtual environment, prompts for your API key without echo,
and serves the map at http://127.0.0.1:8767. The key stays in process memory and
is sent only by the backend to AISStream; it is never written into a repository
file or returned to the browser. See [the regional setup guide](whaleguard/REGIONAL.md).

## Original WhaleGuard hackathon demonstration

Run `python3 -m whaleguard`, then open http://127.0.0.1:8765. No dependencies
required beyond Python 3.10+. One fictional ship navigates and reroutes
automatically, with a spectator map and optional spoken advisories.
Use `python3 -m whaleguard --live` to adapt to Acartia whale reports. The ship,
land, buffers, speed limits, and navigation outcomes remain simulated; no
real-world safety claims.
See [the demo guide](whaleguard/README.md) for setup, routing details,
verification, and a four-person/two-hour work plan.

## Original simulator scaffold
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
