# data/ais/

Generated AIS captures and route bundles live here. Everything except this README is git-ignored, so share a frozen bundle file directly when integrating.

| File | Written by | Format |
|---|---|---|
| `capture_*.jsonl` | `python -m save_the_whales.sources.ais --seconds 1800 --out data/ais/capture_<date>.jsonl` | One normalized `AISRecord` per line (below). No API key, no raw provider payloads |
| `route_bundle*.json` | `python -m save_the_whales.sources.routes --capture <jsonl> [--capture <jsonl> ...] --land data/land/santa_barbara.geojson --out data/ais/route_bundle.json` | `RouteBundle` (contract 2.0, see `save_the_whales/shared/contracts.py`) |

## Credentials

The capture reads `AISSTREAM_API_KEY` from the environment. When it's unset, the capture CLI loads that one variable from the repo's dotenv file (see `.env.example`). The key is never printed, logged, or written to any capture or bundle. A missing key yields capture state `ERROR` with `AIS_KEY_MISSING` immediately; it never hangs.

## Capture record (`AISRecord`)

| Field | Meaning |
|---|---|
| `record_type` | `POSITION` (Class A/B position reports) or `STATIC` (type 5 / type 24 static data) |
| `mmsi` | Source MMSI as a string |
| `observed_at` | UTC `...Z`. Provider metadata time when present, else local receipt time, refined by the AIS UTC-second field when consistent |
| `received_at` | Local receipt time, UTC `...Z` |
| `time_quality` | `PROVIDER_TIME_AIS_SECOND`, `PROVIDER_TIME`, `RECEIPT_TIME_AIS_SECOND` or `RECEIPT_TIME_APPROX`. The AIS UTC-second alone is never treated as a full date-time |
| `lat`, `lon` | WGS84 (None on `STATIC`) |
| `sog_knots`, `cog_deg`, `heading_deg` | None when the AIS "not available" sentinel was sent (102.3, 360, 511) |
| `length_m`, `beam_m`, `ship_type`, `ship_name` | From static messages when known |
| `nav_status`, `message_type`, `provider` (`AISSTREAM`), `adapter`, `receipt_seq` | Provenance and ordering |

## Route preparation settings (`config/default.json` → `ais`)

| Key | Default | Meaning |
|---|---|---|
| `gap_split_s` | 180 | Split tracks at larger gaps |
| `max_implied_speed_knots` | 40 | Split at implausible jumps |
| `min_fixes` / `min_span_s` / `min_travel_m` | 3 / 60 / 250 | Minimum observed route (distinct fixes, seconds, meters) |
| `preferred_clip_s` | 900 | Clip length, so baseline travel is ≤15 min (comparison endpoint = clip exit). `max_clip_s` (1800) is the hard cap |
| `max_route_vertices` / `simplify_tolerance_m` | 32 / 25 | Douglas-Peucker, with every simplified leg rechecked against land. Over budget → trim the clip, don't drop bends |
| `max_routes` | 5 | Routes kept, one per vessel first; 2–5 km and commercial ships preferred |
| `default_length_m` / `default_beam_m` | 300 / 40 | Conservative defaults when static data is missing; listed in `assumed_fields` |
| `clearance_margin_m` / `min_speed_knots` | 100 / 6 | Must equal the planner constraints. `planner.*` wins when present; a mismatch raises |
| `capture_max_s`, `max_records`, `stale_after_s`, `reconnect_*`, `max_consecutive_failures` | 1800, 50000, 120, 1–30 s, 5 | Capture bounds |

A route is usable only if its whole baseline passes `check_water_path` at `ship_clearance_m(length, beam, margin)` and its service speed is at least `min_speed_knots`. `load_route_bundle` repeats those checks against the land map you pass in.
