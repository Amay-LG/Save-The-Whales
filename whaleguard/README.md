# WhaleGuard — two-hour hackathon demo

All ships, waters, zones, advisories, and outcomes are fictional simulations.
No live APIs, LLM routing, or real-world safety claims. Python 3.10+; no package
installation required. Run from the repository root:

```sh
python3 -m whaleguard
```

Open http://127.0.0.1:8765 immediately and select **Enable spoken advisories**.
This only enables browser speech; the simulation already runs autonomously on
the server, even without an open browser. Use a browser with an installed English
speech voice. Speech uses only voices marked local by the browser; unavailable
or blocked audio falls back to the visible transcript. Rehearse audio on the
presentation machine; browser speech support varies.

One wall-clock second represents 1.5 simulated minutes. The new zone appears at
8 wall-clock seconds (simulated minute 12). The voyage ends after approximately
49 wall-clock seconds. Ctrl-C and rerun the command to replay; reload the page
if desired. Reloading alone does not restart the server's voyage. Use `--port
8766` if the default port is occupied. The server binds only to localhost.

## What to demonstrate

1. WG-01 moves automatically from its start toward its destination, avoiding the
   fictional island and accounting for an existing whale zone's speed limit.
2. A new fictional zone appears without a click. The old route becomes dashed;
   Dijkstra replans from the actual current ship position.
3. The advisory explains the new limit and two distinct comparisons:
   - arrival changes from the prior prediction by **+4.1 simulated minutes**;
   - the new route saves **168.7 simulated minutes** versus continuing along the
     old route while obeying the newly imposed limits.
4. The simulated ship arrives in **72.8 simulated minutes**. The original
   prediction was **68.7 minutes**. The map is a spectator view throughout.

Pitch: “The interface is the advisory. The system monitors a fictional zone
change, calculates the fastest permitted route, and navigates automatically.”

## Four-person ownership and remaining two hours

| Owner | Files / responsibility | Next useful work |
|---|---|---|
| Routing | `whaleguard/routing.py`, routing/geometry tests | Review graph resolution and adversarial geometry |
| Simulation | `whaleguard/simulation.py`, simulation tests | Tune scenario timing; retain deterministic movement and ETA math |
| Experience | `whaleguard/static/index.html` | Rehearse voice; polish spectator map and accessibility |
| Integration/demo (suggested user role) | `whaleguard/__main__.py`, docs | Keep build green, integrate teammates, rehearse story |

Agree owners in the first 10 minutes. Use the next 60 minutes for focused work,
20 minutes for integration, and the last 30 minutes for audio checks and demo
rehearsals. Coordinate edits to the shared test file. Avoid expanding into live
feeds or completing the larger original research simulator during this window.

## Routing and movement contract

- Coordinates are local fictional kilometres; speeds are km/h; core time is hours.
- The graph is an eight-neighbour 1 km grid. Start and goal also connect to their
  16 nearest visible grid nodes and each other when traversable.
- Dijkstra minimizes integrated travel time **on this graph**. It is an
  approximation of the continuous-space routing problem; finer grids can find
  faster routes. No claim of continuous global optimality is made.
- Every edge is split at all polygon boundary intersections. Each interval uses
  the minimum of cruise speed (24 km/h) and overlapping speed limits.
- Blocked interior, edge overlap, and corner contact all reject an edge. Slow
  zone boundaries inherit the speed limit. No endpoint-only collision checks.
- Movement uses the same split segments as routing and carries time across
  waypoints. Large updates split at the timed event, so replanning occurs at
  precisely the simulated event position, without teleporting.
- No feasible route means hold position and announce it. The ship is a point;
  hull clearance, currents, uncertainty, live navigation, and geographic fidelity
  are out of scope.
- `GET /api/state` is read-only. A server timer advances the simulation, not
  browser polling. All viewers observe the same voyage.

The new package reuses `simulator.geo.point_in_polygon`. It adds boundary-aware
segment handling without changing that existing helper or the frozen advisor
contract. Original `app.py`, `simulator/`, and `your_project/` remain intact;
the original large simulator still contains stubs.

## Verification

```sh
python3 -m unittest tests.test_whaleguard -v
# If existing development dependencies are installed:
python3 -m pytest -q
```

Tests cover outside-to-outside crossings, thin and overlapping zones, a triangle,
blocked tangencies/edges, speed-limited boundaries, slowing versus detouring,
blocked/unreachable destinations, off-grid starts, movement through boundaries,
current-position replanning, ETA comparisons, tick-size independence, arrival,
and holding when no route exists.
