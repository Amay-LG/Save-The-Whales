"""Person 1: AIS normalization/capture, route preparation, bundles and land maps (no network)."""
import asyncio
import copy
import json
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from save_the_whales.shared import contracts as C
from save_the_whales.shared import geo
from save_the_whales.sources import ais, land as landmod, routes

ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "tests" / "fixtures" / "common"
SOURCES = ROOT / "tests" / "fixtures" / "sources"
CONFIG = json.loads((ROOT / "config" / "default.json").read_text())
P = lambda lat, lon: {"lat": lat, "lon": lon}  # noqa: E731
T0 = datetime(2026, 7, 1, 11, 0, 0, tzinfo=timezone.utc)
STATUS_KEYS = {"state", "reason_codes", "records_received", "records_valid", "vessels_tracked",
               "last_received_at", "api_verified"}


def cfg(**ais_overrides):
    out = copy.deepcopy(CONFIG)
    out["ais"].update(ais_overrides)
    return out


def toy_land(name="land_far.fixture.geojson"):
    return landmod.load_land(COMMON / name, CONFIG)


def track(xy_points, *, dt_s=10.0, sog=12.0, mmsi="999000010", t0=T0, provider="SYNTHETIC_FIXTURE",
          seq0=0, fixed_times=None):
    """Fixes every ``dt_s`` at constant speed along a polyline given in projected meters."""
    speed = geo.knots_to_mps(sog)
    legs = list(zip(xy_points, xy_points[1:]))
    total = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in legs)
    out, s, i = [], 0.0, 0
    while s <= total + 1e-9:
        rem, pos = s, xy_points[-1]
        for a, b in legs:
            seg = math.hypot(b[0] - a[0], b[1] - a[1])
            if rem <= seg:
                f = rem / seg if seg else 0.0
                pos = (a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1]))
                break
            rem -= seg
        t = t0 + timedelta(seconds=(fixed_times[i] if fixed_times else dt_s * i))
        p = geo.unproject(*pos)
        out.append({"record_type": "POSITION", "mmsi": mmsi, "observed_at": ais.utc_iso(t),
                    "received_at": ais.utc_iso(t + timedelta(seconds=1)), "time_quality": C.TIME_PROVIDER,
                    "provider": provider, "adapter": ais.ADAPTER_ID if provider == "AISSTREAM" else "fixture",
                    "receipt_seq": seq0 + i, "lat": p["lat"], "lon": p["lon"], "sog_knots": sog, "cog_deg": None})
        s += speed * dt_s
        i += 1
        if fixed_times and i >= len(fixed_times):
            break
    return out


def static(mmsi="999000010", length=200.0, beam=30.0, ship_type=70):
    return {"record_type": "STATIC", "mmsi": mmsi, "observed_at": ais.utc_iso(T0), "received_at": ais.utc_iso(T0),
            "time_quality": C.TIME_RECEIPT_APPROX, "provider": "SYNTHETIC_FIXTURE", "receipt_seq": 0,
            "lat": None, "lon": None, "length_m": length, "beam_m": beam, "ship_type": ship_type}


# Water well south of the toy land fixtures; x in meters east of the origin (around -120.06 lon).
SOUTH_Y = geo.project(P(34.15, -120.0))[1]
X0 = geo.project(P(34.15, -120.10))[0]


# =====================================================================================================
# Normalization
# =====================================================================================================
def _fixture_messages():
    return {json.loads(l)["case"]: json.loads(l)["message"]
            for l in (SOURCES / "aisstream_messages.fixture.jsonl").read_text().splitlines() if l.strip()}


MSG = _fixture_messages()
NOW = datetime(2026, 7, 1, 12, 0, 7, tzinfo=timezone.utc)


def test_normalize_class_a_reconciles_utc_second():
    r = ais.normalize_message(json.dumps(MSG["class_a_valid"]).encode(), NOW, 1, C.REGION_BBOX)
    assert r["record_type"] == "POSITION" and r["mmsi"] == "999000001"
    assert r["observed_at"] == "2026-07-01T12:00:03.000Z" and r["time_quality"] == C.TIME_PROVIDER_AIS_SECOND
    assert r["received_at"] == "2026-07-01T12:00:07.000Z"
    assert (r["lat"], r["lon"], r["sog_knots"], r["cog_deg"], r["heading_deg"]) == (34.2, -120.1, 18.0, 90.0, None)


def test_normalize_sentinels_become_none():
    r = ais.normalize_message(MSG["class_a_sentinels"], NOW, 2, C.REGION_BBOX)
    assert r["sog_knots"] is None and r["cog_deg"] is None and r["heading_deg"] is None
    assert r["time_quality"] == C.TIME_PROVIDER and r["observed_at"] == "2026-07-01T12:00:06.000Z"


@pytest.mark.parametrize("case", ["class_a_invalid_flag", "class_a_position_unavailable", "class_a_outside_bbox",
                                  "static_no_usable_fields", "provider_error", "unsupported_type"])
def test_normalize_rejects(case):
    assert ais.normalize_message(MSG[case], NOW, 3, C.REGION_BBOX) is None


def test_normalize_class_b_and_static():
    b = ais.normalize_message(MSG["class_b_valid"], NOW, 4, C.REGION_BBOX)
    assert b["message_type"] == "StandardClassBPositionReport" and b["heading_deg"] == 268
    s5 = ais.normalize_message(MSG["static_type5"], NOW, 5, C.REGION_BBOX)
    assert (s5["record_type"], s5["length_m"], s5["beam_m"], s5["ship_type"]) == ("STATIC", 300, 40, 70)
    s24 = ais.normalize_message(MSG["static_type24"], NOW, 6, C.REGION_BBOX)
    assert (s24["length_m"], s24["beam_m"], s24["ship_type"], s24["ship_name"]) == (12, 4, 37, "FIXTURE 006")


def test_receipt_time_used_and_labelled_without_provider_time():
    msg = copy.deepcopy(MSG["class_a_valid"])
    del msg["MetaData"]["time_utc"]
    r = ais.normalize_message(msg, datetime(2026, 7, 1, 12, 0, 20, tzinfo=timezone.utc), 7, C.REGION_BBOX)
    assert r["time_quality"] == C.TIME_RECEIPT_AIS_SECOND and r["observed_at"] == "2026-07-01T12:00:03.000Z"
    msg["Message"]["PositionReport"]["Timestamp"] = 60
    r = ais.normalize_message(msg, NOW, 8, C.REGION_BBOX)
    assert r["time_quality"] == C.TIME_RECEIPT_APPROX and r["observed_at"] == ais.utc_iso(NOW)


def test_parse_utc_formats():
    assert ais.parse_utc("2026-07-01 12:00:05.123456789 +0000 UTC") == datetime(2026, 7, 1, 12, 0, 5, 123456,
                                                                                 tzinfo=timezone.utc)
    assert ais.parse_utc("2026-07-01T05:00:00-07:00") == datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
    assert ais.parse_utc("not a time") is None


# =====================================================================================================
# Capture handle (fake websocket; never touches the network)
# =====================================================================================================
class FakeWS:
    def __init__(self, frames, close_after=False):
        self.frames = list(frames)
        self.sent = []
        self.close_after = close_after

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, text):
        self.sent.append(json.loads(text))

    async def recv(self):
        if self.frames:
            return self.frames.pop(0)
        if self.close_after:
            raise ConnectionError("closed")
        await asyncio.sleep(30)


def wait_for(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_missing_key_is_immediate_error(monkeypatch):
    monkeypatch.delenv(ais.KEY_ENV, raising=False)
    t = time.monotonic()
    h = ais.start_capture(CONFIG)
    st = h.status()
    assert time.monotonic() - t < 0.5
    assert set(st) == STATUS_KEYS and st["state"] == C.CAPTURE_ERROR and st["reason_codes"] == [C.AIS_KEY_MISSING]
    h.stop()


def test_capture_stream_status_snapshot_and_stop():
    frames = [json.dumps(MSG[c]).encode() for c in ("class_a_valid", "static_type5", "class_b_valid",
                                                     "class_a_sentinels", "class_a_invalid_flag", "provider_error")]
    ws = FakeWS(frames)
    h = ais.AISCaptureHandle(CONFIG, "dummy-key", connect=lambda url: ws).start()
    assert wait_for(lambda: h.status()["records_received"] == 6)
    st = h.status()
    assert set(st) == STATUS_KEYS and st["state"] == C.CAPTURE_CAPTURING
    assert st["records_valid"] == 4 and st["vessels_tracked"] == 3 and st["api_verified"]
    assert C.AIS_API_ERROR in st["reason_codes"]
    sub = ws.sent[0]
    assert sub["BoundingBoxes"] == [[[33.8, -120.5], [34.6, -119.0]]]   # [lat, lon] corners
    snap = h.snapshot_records()
    snap[0]["lat"] = 0.0
    assert h.snapshot_records()[0]["lat"] == 34.2                      # copies, not worker state
    assert "dummy-key" not in json.dumps(snap)
    h.stop()
    assert h.status()["state"] == C.CAPTURE_STOPPED and not h._thread.is_alive()


def test_capture_retention_is_bounded():
    frames = [json.dumps(MSG["class_a_valid"]).encode()] * 5
    h = ais.AISCaptureHandle(cfg(max_records=3), "k", connect=lambda url: FakeWS(frames)).start()
    assert wait_for(lambda: h.status()["records_valid"] == 5)
    assert len(h.snapshot_records()) == 3 and C.AIS_RETENTION_CAP_REACHED in h.status()["reason_codes"]
    h.stop()


def test_capture_gives_up_after_repeated_failures():
    def broken(url):
        raise OSError("unreachable")
    h = ais.AISCaptureHandle(cfg(reconnect_initial_s=0.01, reconnect_max_s=0.02, max_consecutive_failures=3),
                             "k", connect=broken).start()
    assert wait_for(lambda: h.status()["state"] == C.CAPTURE_ERROR)
    assert h.status()["reason_codes"] == [C.AIS_CONNECTION_FAILED]
    h.stop()


def test_capture_stops_at_its_time_bound():
    h = ais.AISCaptureHandle(cfg(capture_max_s=0.3), "k", connect=lambda url: FakeWS([])).start()
    assert wait_for(lambda: h.status()["state"] == C.CAPTURE_STOPPED, timeout=4)
    assert C.AIS_CAPTURE_LIMIT_REACHED in h.status()["reason_codes"]
    h.stop()


def test_capture_file_round_trip(tmp_path):
    recs = [ais.normalize_message(MSG["class_a_valid"], NOW, 1, C.REGION_BBOX)]
    ais.save_capture(recs, tmp_path / "c.jsonl")
    assert ais.load_capture(tmp_path / "c.jsonl") == recs


def test_dotenv_loader_reads_only_the_key(tmp_path, monkeypatch):
    monkeypatch.delenv(ais.KEY_ENV, raising=False)
    env = tmp_path / "dotenv"
    env.write_text('OTHER=1\nexport AISSTREAM_API_KEY="abc123"\n')
    assert ais.load_key_from_dotenv(env) and ais.os.environ[ais.KEY_ENV] == "abc123"
    monkeypatch.setenv(ais.KEY_ENV, "kept")
    assert ais.load_key_from_dotenv(env) and ais.os.environ[ais.KEY_ENV] == "kept"
    monkeypatch.delenv(ais.KEY_ENV)
    assert not ais.load_key_from_dotenv(tmp_path / "missing")


# =====================================================================================================
# Route preparation
# =====================================================================================================
def straight(x_len=4000.0, y=SOUTH_Y):
    return [(X0, y), (X0 + x_len, y)]


def test_fixture_capture_becomes_one_route():
    recs = ais.load_capture(SOURCES / "capture.fixture.jsonl")
    b = routes.prepare_routes(recs, toy_land(), CONFIG)
    assert b["schema_version"] == "2.0" and b["issues"] == [C.VALIDATED_AGAINST_FIXTURE_MAP]
    assert b["source"]["kind"] == C.SYNTHETIC_FIXTURE and b["source"]["fixture_only"]
    assert not b["source"]["api_verified"]
    (r,) = b["routes"]
    assert r["validation"] == {"ok": True, "reasons": []} and r["assumed_fields"] == []
    assert (r["length_m"], r["beam_m"], r["service_speed_knots"]) == (300.0, 40.0, 18.0)
    assert r["waypoints"][0] == P(recs[0]["lat"], recs[0]["lon"])
    assert r["waypoints"][-1] == P(recs[-2]["lat"], recs[-2]["lon"])
    assert len(r["waypoints"]) == 2   # straight track simplifies to its endpoints


def test_sorting_and_duplicates_are_order_independent():
    recs = track(straight()) + [static()]
    dup = dict(recs[5], receipt_seq=999, lat=recs[5]["lat"] + 0.001)   # same observation time, later receipt
    shuffled = list(reversed(recs)) + [dup]
    a = routes.prepare_routes(recs, toy_land(), CONFIG)
    b = routes.prepare_routes(shuffled, toy_land(), CONFIG)
    assert a["routes"][0]["waypoints"] == b["routes"][0]["waypoints"]
    assert b["preparation_stats"]["duplicate_fixes"] == 1


def test_gap_and_jump_splitting():
    times = [10 * i for i in range(30)] + [10 * i + 400 for i in range(30, 60)]   # one 400 s gap
    b = routes.prepare_routes(track(straight(8000), fixed_times=times) + [static()], toy_land(), CONFIG)
    assert b["preparation_stats"]["gap_splits"] == 1 and len(b["routes"]) == 2
    jumpy = track(straight())
    jumpy[10] = dict(jumpy[10], lat=jumpy[10]["lat"] + 0.05)                    # ~5.5 km in 10 s
    b = routes.prepare_routes(jumpy + [static()], toy_land(), CONFIG)
    assert b["preparation_stats"]["jump_splits"] >= 1
    for r in b["routes"]:
        assert all(abs(p["lat"] - jumpy[0]["lat"]) < 0.01 for p in r["waypoints"])


def test_bends_are_preserved():
    corner = (X0 + 2000, SOUTH_Y)
    b = routes.prepare_routes(track([(X0, SOUTH_Y), corner, (X0 + 2000, SOUTH_Y - 2000)]) + [static()],
                              toy_land(), CONFIG)
    (r,) = b["routes"]
    xy = [geo.project(p) for p in r["waypoints"]]
    spacing = geo.knots_to_mps(12.0) * 10   # fixes are ~62 m apart, so the nearest fix is within half of that
    assert len(xy) == 3 and min(math.hypot(x - corner[0], y - corner[1]) for x, y in xy) < spacing / 2


@pytest.mark.parametrize("recs,why", [
    (lambda: track(straight())[:1], C.TOO_FEW_FIXES),                                   # a single fix
    (lambda: track(straight(), dt_s=10)[:3], C.TOO_SHORT_DURATION),                     # 20 s
    (lambda: track(straight(200), dt_s=30, sog=6.5), C.TOO_SHORT_DISTANCE),             # < 250 m
])
def test_unusable_clips_are_rejected(recs, why):
    b = routes.prepare_routes(recs() + [static()], toy_land(), CONFIG)
    assert b["routes"] == [] and C.AIS_ROUTE_UNAVAILABLE in b["issues"]
    assert b["preparation_stats"]["clips_rejected"].get(why, 0) >= 1


def test_land_crossing_chord_is_split_and_routes_clear_land():
    blocked = toy_land("land_blocked_side.fixture.geojson")
    y = geo.project(P(34.21, -120.0))[1]   # passes through the toy rectangle
    x_w, x_e = geo.project(P(34.21, -120.095))[0], geo.project(P(34.21, -120.035))[0]
    west = track([(x_w - 3000, y), (x_w, y)], mmsi="999000011")
    # West ends at ~480 s; east starts 300 s later, so the ~5.5 km chord is plausible (< 40 kn) but crosses land.
    east = track([(x_e, y), (x_e + 3000, y)], mmsi="999000011", t0=T0 + timedelta(seconds=780), seq0=500)
    b = routes.prepare_routes(west + east + [static("999000011")], blocked, cfg(gap_split_s=600))
    assert b["preparation_stats"]["land_chord_splits"] == 1
    clearance = geo.ship_clearance_m(200, 30, 100)
    assert b["routes"] and all(geo.check_water_path(r["waypoints"], blocked, clearance)["ok"] for r in b["routes"])


def test_hull_clearance_trims_close_passes():
    blocked = toy_land("land_blocked_side.fixture.geojson")
    y = geo.project(P(34.2031, -120.0))[1]   # ~100 m south of the toy coast for part of the track
    b = routes.prepare_routes(track([(X0 - 2000, y), (X0 + 9000, y)]) + [static()], blocked, CONFIG)
    assert b["preparation_stats"]["clearance_trims"] >= 1
    clearance = geo.ship_clearance_m(200, 30, 100)
    for r in b["routes"]:
        assert geo.check_water_path(r["waypoints"], blocked, clearance)["ok"]


def test_defaults_and_service_speed_are_flagged():
    b = routes.prepare_routes(track(straight(), sog=11.0), toy_land(), CONFIG)
    (r,) = b["routes"]
    assert set(r["assumed_fields"]) == {"length_m", "beam_m"} and (r["length_m"], r["beam_m"]) == (300.0, 40.0)
    assert r["service_speed_knots"] == 11.0
    slow = routes.prepare_routes(track(straight(), sog=4.0) + [static()], toy_land(), CONFIG)
    assert slow["routes"] == [] and slow["preparation_stats"]["clips_rejected"][C.SERVICE_SPEED_BELOW_MIN] >= 1


def test_invalid_map_blocks_preparation():
    bad = landmod.load_land(ROOT / "missing.geojson", CONFIG)
    b = routes.prepare_routes(track(straight()) + [static()], bad, CONFIG)
    assert b["routes"] == [] and {C.INVALID_MAP, C.AIS_ROUTE_UNAVAILABLE} <= set(b["issues"])


def test_aisstream_records_mark_api_provenance():
    b = routes.prepare_routes(track(straight(), provider="AISSTREAM"), toy_land(), CONFIG)
    assert b["source"]["kind"] == C.AISSTREAM_CAPTURE and b["source"]["api_verified"]
    assert not b["source"]["fixture_only"] and b["source"]["capture_sha256"]


def test_clip_duration_vertex_budget_and_route_cap():
    zig = [(X0 + 300 * i, SOUTH_Y + (300 if i % 2 else 0)) for i in range(60)]     # 59 bends, ~25 km
    b = routes.prepare_routes(track(zig, sog=14) + [static()], toy_land(), CONFIG)
    assert b["routes"]
    for r in b["routes"]:
        assert len(r["waypoints"]) <= CONFIG["ais"]["max_route_vertices"]
        assert r["source_duration_s"] <= CONFIG["ais"]["preferred_clip_s"]
    many = []
    for k in range(7):
        many += track(straight(4000, SOUTH_Y - 3000 * k), mmsi=f"99900002{k}", seq0=1000 * k) + [static(f"99900002{k}")]
    assert len(routes.prepare_routes(many, toy_land(), CONFIG)["routes"]) == CONFIG["ais"]["max_routes"]


def test_conflicting_planner_margin_is_rejected():
    bad = cfg()
    bad["planner"] = {"clearance_margin_m": 50, "min_speed_knots": 6}
    with pytest.raises(ValueError):
        routes.prepare_routes([], toy_land(), bad)


# =====================================================================================================
# Bundle save/load
# =====================================================================================================
def test_bundle_round_trip_and_recheck(tmp_path):
    b = routes.prepare_routes(track(straight(), provider="AISSTREAM") + [static()], toy_land(), CONFIG)
    routes.save_route_bundle(b, tmp_path / "b.json")
    loaded = routes.load_route_bundle(tmp_path / "b.json", toy_land(), CONFIG)
    assert loaded["routes"] == b["routes"] and loaded["source"] == b["source"]
    assert loaded["issues"] == [C.VALIDATED_AGAINST_FIXTURE_MAP]

    tampered = json.loads((tmp_path / "b.json").read_text())
    tampered["routes"][0]["source_track"][3]["lat"] += 0.01
    (tmp_path / "t.json").write_text(json.dumps(tampered))
    r = routes.load_route_bundle(tmp_path / "t.json", toy_land(), CONFIG)["routes"][0]
    assert not r["validation"]["ok"] and C.SOURCE_TRACK_CHECKSUM_MISMATCH in r["validation"]["reasons"]


def test_loaded_route_rechecked_against_new_land():
    bundle = routes.load_route_bundle(COMMON / "route_bundle.fixture.json", toy_land(), CONFIG)
    assert bundle["routes"][0]["validation"]["ok"]
    crossing = toy_land("land_blocked_side.fixture.geojson")
    crossing["rings"] = [[P(34.195, -120.07), P(34.195, -120.06), P(34.205, -120.06), P(34.205, -120.07)]]
    crossing["map_id"] = "crossing"
    again = routes.load_route_bundle(COMMON / "route_bundle.fixture.json", crossing, CONFIG)
    assert not again["routes"][0]["validation"]["ok"] and C.AIS_ROUTE_UNAVAILABLE in again["issues"]
    assert C.INVALID_BASELINE_ROUTE in again["routes"][0]["validation"]["reasons"]


def test_unreadable_bundle_is_explicit(tmp_path):
    (tmp_path / "bad.json").write_text("{not json")
    for p in (tmp_path / "bad.json", tmp_path / "missing.json"):
        b = routes.load_route_bundle(p, toy_land(), CONFIG)
        assert b["routes"] == [] and {C.INVALID_BUNDLE, C.AIS_ROUTE_UNAVAILABLE} <= set(b["issues"])
    wrong = json.loads((COMMON / "route_bundle.fixture.json").read_text())
    wrong["schema_version"] = "3.0"
    (tmp_path / "v3.json").write_text(json.dumps(wrong))
    b = routes.load_route_bundle(tmp_path / "v3.json", toy_land(), CONFIG)
    assert C.SCHEMA_VERSION_MISMATCH in b["issues"] and not b["routes"][0]["validation"]["ok"]


# =====================================================================================================
# Land maps
# =====================================================================================================
def test_real_regional_land_map_is_usable():
    m = landmod.load_land(ROOT / "data" / "land" / "santa_barbara.geojson", CONFIG)
    assert m["validated"] and not m["fixture_only"] and m["issues"] == []
    assert len(m["rings"]) >= 5 and m["sha256"] == m["provenance"]["sha256"]
    lons = [p["lon"] for r in m["rings"] for p in r]
    assert min(lons) <= -120.4 and max(lons) >= -119.4   # San Miguel through Anacapa + mainland
    # Santa Cruz Island interior is land; mid-channel is clear water.
    assert not geo.check_water_path([P(34.0, -119.75)], m, 0)["ok"]
    assert geo.check_water_path([P(34.2, -119.9)], m, 251)["ok"]


@pytest.mark.parametrize("name", ["land_far.fixture.geojson", "land_blocked_side.fixture.geojson"])
def test_fixture_maps_are_labelled(name):
    m = toy_land(name)
    assert m["validated"] and m["fixture_only"] and len(m["rings"]) == 1


def _write_map(tmp_path, gj, prov=None):
    p = tmp_path / "m.geojson"
    p.write_text(json.dumps(gj))
    if prov is not None:
        (tmp_path / "provenance.json").write_text(json.dumps(dict(prov, file="m.geojson")))
    return p


PROV = {"source": "test", "fixture_only": True, "bbox": C.REGION_BBOX}
SQUARE = [[-120.0, 34.0], [-119.9, 34.0], [-119.9, 34.1], [-120.0, 34.1], [-120.0, 34.0]]


@pytest.mark.parametrize("gj,code", [
    ("", C.MAP_EMPTY),
    ({"type": "FeatureCollection", "features": [{"type": "Feature", "geometry":
                                                 {"type": "Polygon", "coordinates": [[]]}}]}, C.INVALID_RING),
    ({"type": "Polygon", "coordinates": [[[-120.0, 34.0], [-119.9, 34.1], [-119.9, 34.0], [-120.0, 34.1]]]},
     C.INVALID_RING),   # bow-tie
])
def test_invalid_maps(tmp_path, gj, code):
    p = tmp_path / "m.geojson"
    p.write_text(gj if isinstance(gj, str) else json.dumps(dict(gj, provenance=PROV)))
    m = landmod.load_land(p, CONFIG)
    assert not m["validated"] and C.INVALID_MAP in m["issues"] and code in m["issues"]


def test_missing_file_and_missing_provenance(tmp_path):
    m = landmod.load_land(tmp_path / "nope.geojson", CONFIG)
    assert not m["validated"] and {C.INVALID_MAP, C.MAP_FILE_MISSING} <= set(m["issues"])
    m = landmod.load_land(_write_map(tmp_path, {"type": "Polygon", "coordinates": [SQUARE]}), CONFIG)
    assert not m["validated"] and C.MAP_PROVENANCE_MISSING in m["issues"]


def test_sidecar_hash_mismatch_and_multipolygon_holes(tmp_path):
    hole = [[-119.98, 34.02], [-119.92, 34.02], [-119.92, 34.08], [-119.98, 34.08], [-119.98, 34.02]]
    other = [[-119.5, 34.0], [-119.4, 34.0], [-119.4, 34.1], [-119.5, 34.0]]
    gj = {"type": "MultiPolygon", "coordinates": [[SQUARE, hole], [other]]}
    p = _write_map(tmp_path, gj, dict(PROV, sha256="0" * 64))
    m = landmod.load_land(p, CONFIG)
    assert not m["validated"] and C.MAP_HASH_MISMATCH in m["issues"]
    (tmp_path / "provenance.json").write_text(json.dumps(dict(PROV, file="m.geojson", sha256=landmod.sha256_file(p))))
    m = landmod.load_land(p, CONFIG)
    assert m["validated"] and len(m["rings"]) == 2 and C.HOLES_FILLED_AS_LAND in m["simplifications"]
    assert not geo.check_water_path([P(34.05, -119.95)], m, 0)["ok"]   # the filled lake counts as land


def test_land_subset_keeps_enclosing_and_crossing_polygons():
    region = C.REGION_BBOX
    enclosing = {"type": "Polygon", "coordinates": [[[-121, 33.5], [-118, 33.5], [-118, 34.4], [-121, 34.4], [-121, 33.5]]]}
    crossing = {"type": "Polygon", "coordinates": [[[-120.6, 34.5], [-120.4, 34.5], [-120.4, 34.7], [-120.6, 34.7], [-120.6, 34.5]]]}
    outside = {"type": "Polygon", "coordinates": [[[-118, 30], [-117, 30], [-117, 31], [-118, 30]]]}
    fc, stats = landmod.prepare_land_subset([(enclosing, "a"), (crossing, "b"), (outside, "c"), (crossing, "d")], region)
    assert stats["polygons_by_source"] == {"a": 1, "b": 1}   # enclosing kept, outside dropped, duplicate deduped
    for f in fc["features"]:
        for lon, lat in f["geometry"]["coordinates"][0]:
            assert region["lon_min"] <= lon <= region["lon_max"] and region["lat_min"] <= lat <= region["lat_max"]
