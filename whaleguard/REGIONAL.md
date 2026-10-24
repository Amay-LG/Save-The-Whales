# Santa Barbara Channel pivot

This demo combines a real geographic map and historical ship/whale records with
automatic **simulated** navigation. Its illustrative decisions are not intended
for operating real vessels.

## Run with your AISStream key

From the repository root:

```zsh
zsh scripts/run_santa_barbara.zsh
```

The launcher installs the optional `websockets` dependency in a local `.venv`,
asks for your key at a hidden terminal prompt if `AISSTREAM_API_KEY` is not already
set, and starts the regional map on **http://127.0.0.1:8767**. Input is not echoed
or written to a file or shell history. The environment key is transmitted by the
server over WSS to AISStream, and never serialized into browser state or logs.
Do not paste keys into chat or commit them to the repository. Ctrl-C stops the
server. Pass a different free port with `zsh scripts/run_santa_barbara.zsh 8768`.

If you already have an environment and the dependency installed:

```zsh
python3 -m whaleguard --scenario santa-barbara --aisstream --port 8767
```

Use the same terminal in which you exported `AISSTREAM_API_KEY`. A shell variable
set in another terminal is not automatically available here. To enter it locally:

```zsh
read -s "AISSTREAM_API_KEY?AISStream key: "
export AISSTREAM_API_KEY
printf '\n'
```

With no key, package, or internet connection, the archive simulation still runs.
The AIS card explicitly shows `not_configured`, `dependency_missing`, or
`reconnecting`. It does not fabricate live traffic.

## Run without live AIS

```sh
python3 -m whaleguard --scenario santa-barbara --port 8766
```

No additional Python packages or internet connection are required. This remains
a complete demonstration: four background ships and WG-01 move automatically,
a predicted overtaking encounter triggers a passing-route calculation, and a
historical whale location becomes a staged speed zone at simulated minute 24.
With default timing, that whale event occurs after 16 actual seconds. Enable
spoken advisories immediately after loading. Restart the command to replay.
Reloading the browser does not reset the shared server simulation.

## What the map shows

- A locally rendered, zoomable/pannable geographic map of the mainland and four
  Channel Islands, with place labels and a kilometre scale. No external tile keys.
- Nine NOAA/USCG archived ship tracks from January 1, 2024. The highlighted
  recorded passage is VENUS HORIZON, with 267 original reports over 5.5 hours.
  A thin dashed blue line marks that source passage; mint marks the computed
  simulation course. Generated detours are not actual recorded shipping routes.
- 182 historical humpback observations from a regional CC0 sample of Happywhale /
  OBIS-SEAMAP records via GBIF. These sampled locations are context, not a current
  density estimate. Dates, uncertainty values, licenses, and source URLs remain
  in the bundled data. Tooltip dates distinguish them from current observations.
- WG-01 plus four purple simulated traffic instances placed at fictional offsets
  along the archived route. Their names and positions do not imply that the
  original ships were sailing together.
- Optional cyan AISStream vessel reports, named with MMSI and receipt age. Trails
  show only recently received reports. Real ships are never moved by this app.

Shipping-lane polygons are drawn as reference only. The simplified planner does
not implement lane-direction rules, bathymetry, vessel dimensions, manoeuvring
constraints, or COLREGS compliance. Historical whale and AIS records have different
dates. The staged whale event never claims to be a new real detection.

## Routing and traffic behaviour

The planner reuses the existing exact segment/zone integration. Dijkstra searches
a forward station-and-offset graph within approximately +/-4 km of the archived
passage. It minimizes permitted-speed travel time on that finite graph and rejects
land intersections, including boundary contact. This is not global continuous
route optimality, and the corridor is not an officially approved detour area.

A staged 3 km zone uses an assumed 6 km/h limit. At each update the simulator
projects its own and simulated traffic motion over eight simulated minutes.
Predicted encounters can trigger a route around temporary traffic buffers.
Movement is committed only after checking the closest separation during each
simultaneous step, including between endpoints. Conflicts cause yielding and
resumption. The default separation is a **simulated 650 m**; it is not validated
as a real navigation clearance. Every simulated tick is at most five seconds.

If enabled, recent AIS positions add stationary constraints in this accelerated
world. Their coordinates change only when a new report is received; actual ships
are not extrapolated along the accelerated simulation clock. The simulation may
route around or yield to these reported positions. This mixes a simulated voyage
with wall-time observations intentionally for demonstration. Reports become stale
after two minutes, cease influencing planning at that point, and are removed
after five minutes. No data means unknown coverage, not empty water.

Arrival impacts are model estimates. Traffic holds can change realized arrival.
The displayed ETA change refers to the latest replan. A run identifier resets
transcript and speech tracking after server restarts.

## AISStream transport

Based on https://aisstream.io/documentation:

- One backend WSS connection to `wss://stream.aisstream.io/v0/stream`.
- A complete subscription is sent immediately after opening. Bounding-box corners
  use **latitude, longitude**, covering 33.85–34.60 N and 120.70–119.00 W.
- Subscribes to class A and class B position-report types. The client enables
  permessage-deflate and exposes subscription compression confirmation.
- UTF-8 JSON binary frames, subscription confirmations, invalid positions, AIS
  sentinel speeds/courses, out-of-order timestamps, and provider errors are handled.
- Exponential backoff with jitter on disconnect, keepalives, bounded message size,
  bounded vessel/trail caches, and no provider raw-error logging that could echo
  a credential. The browser receives only filtered vessel fields.
- The provider needs an account key. Authenticated delivery and actual reception
  in the region cannot be verified without a configured key and available traffic.
  An open socket does not guarantee positions; the UI distinguishes subscription
  status from received reports and shows the last message time.

## Verify

```sh
python3 -m unittest tests.test_whaleguard tests.test_whaleguard_live tests.test_regional tests.test_aisstream -v
```

All test traffic is synthetic. Tests do not contact AISStream or need a real key.
They cover binary decoding, complete subscription, safe snapshots, invalid and
out-of-region reports, time ordering, report staleness/expiry, projected geography,
archived route chronology/land checks, swept traffic conflicts, live-position
holds, retry after traffic clears, and full-voyage completion.

Data sources and processing limitations: [data/README.md](data/README.md).
