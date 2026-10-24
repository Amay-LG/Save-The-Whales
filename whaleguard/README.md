# WhaleGuard — two-hour hackathon demo

Ships, land obstacles, speed limits, buffers, and navigation outcomes are simulated.
Default demo mode is entirely fictional; optional live mode reads whale reports
from Acartia. No LLM routing or real-world safety claims. Python 3.10+; no package
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


## Live whale sightings mode

```sh
python3 -m whaleguard --live
```

The read-only [Acartia recent-sightings endpoint](https://acartia.io/api/v1/sightings/current)
is polled every 30 seconds. This endpoint is documented for experimentation without
registration in [Acartia's contribution guide](https://github.com/Typehuman/SSEMMI/blob/main/CONTRIBUTING.md).
Data attribution: **Acartia and its data contributors**. The demo exposes only
species, report ID, position, receipt time, and the source's observation-time
text; it does not send witness names or comments to the browser.

- The ship waits for an eligible report before departing. The first successful
  set of eligible reports establishes a fixed 30 × 18 km frame centered on its
  newest report. Later reports do not shift that frame or teleport the ship.
- Only records marked trusted by the provider and identified as whale species
  are accepted. Coordinates and timestamps are validated, IDs deduplicated, and
  future or old receipt timestamps rejected. The default receipt-age window is
  six hours, measured in actual wall time, not accelerated simulation time.
- **Receipt age is not observation age.** Acartia's `created` field has no explicit
  timezone. It is displayed unchanged; no timezone or current whale position is
  inferred. The explicit UTC offset in `ssemmi_date_added` governs receipt-age
  filtering. Recently received reports can describe older observations.
- Each nearby report produces a 12-sided, 2 km simulated buffer with an assumed
  6 km/h speed limit. These are demo parameters, not legal restrictions or
  validated biological models. The island and zone A remain fictional.
- The map projects actual report coordinates into the local frame but contains
  **no real coastline or navigation chart**. Never use it to operate a vessel.
- New/changed/expired buffers automatically replan from the simulated ship's
  current position and produce an advisory. Identical polls are quiet. No
  hard-coded timed zone is injected in live mode.
- Network calls have a timeout and size limit and run outside the simulation
  lock. A failed fetch marks the feed unavailable and retains accepted reports
  until their configured expiry. Absence from a subsequent snapshot does not
  prematurely erase a report. Expiry removes a demo buffer; it does not assert
  that an actual whale has left.
- Polling does not guarantee that new reports will arrive during a presentation.
  Use default fictional mode for the deterministic 49-second demonstration.
- Live mode runs at 10 simulated seconds per actual second (roughly seven minutes
  per voyage). `--time-scale 90` speeds it up; `--poll-seconds 15` polls faster
  (minimum 10); `--report-age-hours 2` changes the receipt-age window.
- Last successful fetch, feed availability, nearby receipt ages, observation-time
  text, and attribution are visible beside the map. Restart to choose a new frame
  or begin another voyage. Each server instance has one shared simulated ship.

Run both offline suites:

```sh
python3 -m unittest tests.test_whaleguard tests.test_whaleguard_live -v
```

Live tests include the provider's actual JavaScript date format, future/old
reports, malformed coordinates, duplicate IDs, fixed projection, outages,
reconnects, expiry, redacted metadata, repeated replanning, and recovery from hold.
