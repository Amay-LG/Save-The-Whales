"""Cross-owner integration trace: does data flow along exactly the arrows of Architecture §5?

    AIS API -> capture -> route cache -> simulated fleet/whales -> sensors -> snapshot (+ land, weather)
    -> advisor -> full-path checks -> validated commands -> simulated world -> metrics -> UI

The trace feeds sanitized AISStream-shaped frames (built from ``tests/fixtures/sources/capture.fixture.jsonl``)
through Person 1's real capture handle over a fake websocket, prepares routes against the real research land
map, relabels the in-memory bundle ``SYNTHETIC_FIXTURE`` (a fake socket is not real AIS), then runs Person 4's
scenario builder and paired harness over Person 2's simulator and Person 3's advisor. A ``sys.setprofile``
tracer records every call that crosses an owner boundary (caller module -> callee module.function) and
checks the payloads at each boundary against the shared handshake. Network connects are blocked while it runs.

    .venv/bin/python tests/fixtures/ui/trace_integration.py      # prints the report; exit 1 on failure
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import socket
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for p in (str(ROOT), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from app import load_config, production_services  # noqa: E402
from save_the_whales.shared import contracts as C  # noqa: E402
from save_the_whales.shared import geo  # noqa: E402
from save_the_whales.simulation.world import canonical_sha256  # noqa: E402
from save_the_whales.sources import ais, routes  # noqa: E402
from save_the_whales.ui import plots  # noqa: E402

SOURCES = ROOT / "tests" / "fixtures" / "sources"
REAL_LAND = ROOT / "data" / "land" / "santa_barbara.geojson"
OWNERS = (("save_the_whales.shared", "shared"), ("save_the_whales.sources", "sources"),
          ("save_the_whales.simulation", "simulation"), ("save_the_whales.advisor", "advisor"),
          ("save_the_whales.ui", "ui"))
EXACT = {"app": "app", "run_comparison": "harness"}
# Cross-owner calls the architecture permits. ``None`` = any function of the callee owner.
ALLOWED: Dict[Tuple[str, str], Optional[set]] = {
    ("sources", "shared"): None, ("simulation", "shared"): None, ("advisor", "shared"): None,
    ("ui", "shared"): None, ("harness", "shared"): None, ("app", "shared"): None,
    ("simulation", "advisor"): {"decide", "validate_action", "weather_exposure"},   # injected callbacks only
    ("simulation", "harness"): {"no_action", "observed"},                         # NO_ACTION, weather wrapper
    ("harness", "simulation"): {"run_scenario"},
    ("harness", "advisor"): {"make_decider", "make_validator", "weather_exposure", "blocked_hits"},
    ("harness", "ui"): None, ("harness", "sources"): None, ("harness", "app"): None,
    ("ui", "simulation"): {"make_background_whales"},                             # seeded whale specs
}
OBS_KEYS = {"schema_version", "now", "decision_interval_min", "bbox", "static", "ships", "whale_detections",
            "decision_id", "sim_time_s"}
SHIP_KEYS = {"ship_id", "lat", "lon", "sog_knots", "cog_deg", "length_m", "beam_m", "service_speed_knots",
             "original_route", "current_route", "active_command", "baseline_progress_index", "latest_revision",
             "last_action_at"}
DET_KEYS = {"detection_id", "lat", "lon", "position_error_m", "observed_at", "reported_at", "source"}
ACTION_KEYS = {"ship_id", "issued_at", "command", "target_speed_knots", "detour_waypoints", "valid_until",
               "metadata", "action_id", "revision"}
VALIDATED_KEYS = {"action", "remaining_waypoints", "rejoin_route_index", "effective_target_speed_knots",
                  "incident_budget"}
LEG_KEYS = {"start", "end", "start_s", "end_s", "speed_knots"}
HIDDEN_TOKENS = ("analyst_truth", '"exposed"', "random_walk", '"whale_id"')


def owner(mod: str) -> Optional[str]:
    if mod in EXACT:
        return EXACT[mod]
    for prefix, o in OWNERS:
        if mod == prefix or mod.startswith(prefix + "."):
            return o
    return None


def sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# --- profiler ------------------------------------------------------------------------------------
class Tracer:
    """Records owner-crossing calls and runs payload checks on the watched boundary functions."""

    def __init__(self):
        self.edges: Counter = Counter()             # (caller owner, callee owner, "module.function")
        self.problems: List[str] = []
        self.counts: Counter = Counter()
        self.clearances: Counter = Counter()
        self.purposes: Counter = Counter()
        self.inputs_seen: List[Dict[str, Any]] = []
        self.hidden_tokens: List[str] = []
        self._pending: Dict[int, Any] = {}
        self._watch: Dict[Tuple[str, str], Callable] = {
            ("save_the_whales.advisor.planner", "decide"): self._decide,
            ("save_the_whales.advisor.constraints", "validate_action"): self._validate,
            ("save_the_whales.advisor.weather", "weather_exposure"): self._weather,
            ("save_the_whales.shared.geo", "check_water_path"): self._water,
            ("save_the_whales.simulation.engine", "run_scenario"): self._run_scenario,
        }

    def __call__(self, frame, event, arg):
        if event not in ("call", "return"):
            return
        mod = frame.f_globals.get("__name__", "")
        callee = owner(mod)
        if callee is None:
            return
        key = (mod, frame.f_code.co_name)
        if event == "call":
            back = frame.f_back
            while back is not None and owner(back.f_globals.get("__name__", "")) is None:
                back = back.f_back
            caller = owner(back.f_globals.get("__name__", "")) if back is not None else None
            if caller is not None and caller != callee:
                self.edges[(caller, callee, f"{mod}.{frame.f_code.co_name}")] += 1
            if key in self._watch:
                self._watch[key]("call", frame, caller, None)
        elif key in self._watch:
            self._watch[key]("return", frame, None, arg)

    def _bad(self, msg: str) -> None:
        if len(self.problems) < 50:
            self.problems.append(msg)

    # boundary checks
    def _decide(self, event, frame, caller, ret):
        if event == "call":
            obs = frame.f_locals.get("observation")
            self._pending[id(frame)] = obs
            self.counts["decide"] += 1
            self._check_observation(obs)
            return
        obs = self._pending.pop(id(frame), None) or {}
        if not isinstance(ret, dict) or set(ret) != {"decision_id", "actions", "assessments"}:
            return self._bad(f"decision shape {sorted(ret) if isinstance(ret, dict) else type(ret)}")
        if ret["decision_id"] != obs.get("decision_id"):
            self._bad("decision_id does not echo the observation")
        ships = {s["ship_id"]: s for s in obs.get("ships") or []}
        per_ship = Counter(a.get("ship_id") for a in ret["actions"])
        if any(n > 1 for n in per_ship.values()):
            self._bad("more than one action for a ship")
        if {a["ship_id"] for a in ret["assessments"]} != set(ships):
            self._bad("assessments do not cover exactly the observed ships")
        for a in ret["actions"]:
            self.counts[f"action:{a.get('command')}"] += 1
            if not ACTION_KEYS <= set(a) or a["command"] not in C.COMMANDS:
                self._bad(f"action shape/command {a.get('command')}")
            if len(a.get("detour_waypoints") or []) > 8:
                self._bad("more than 8 detour waypoints")
            if a["command"] == C.DETOUR and not (a.get("rejoin_route_index", -1) >
                                                 ships[a["ship_id"]]["baseline_progress_index"]):
                self._bad("DETOUR rejoin is not ahead of baseline progress")
        for s in ret["assessments"]:
            n = (s.get("costs") or {}).get("candidates_evaluated")
            if n is not None and n > 12:
                self._bad(f"{n} candidates evaluated (> 12)")

    def _check_observation(self, obs):
        if not isinstance(obs, dict) or set(obs) != OBS_KEYS:
            return self._bad(f"observation keys {sorted(obs) if isinstance(obs, dict) else type(obs)}")
        if set(obs["static"]) != {"land", "weather"}:
            self._bad("static is not {land, weather}")
        for s in obs["ships"]:
            if not SHIP_KEYS <= set(s) or not set(s) <= SHIP_KEYS | {"incident_budget"}:
                self._bad(f"observable ship keys {sorted(set(s) ^ SHIP_KEYS)}")
        for d in obs["whale_detections"]:
            if not DET_KEYS <= set(d) or not set(d) <= DET_KEYS | {"species"}:
                self._bad(f"detection keys {sorted(d)}")
            if not str(d["source"]).startswith("SIMULATED"):
                self._bad("detection not labeled simulated")
        self.counts["detections_seen"] += len(obs["whale_detections"])
        text = json.dumps({k: v for k, v in obs.items() if k != "static"})
        self.hidden_tokens += [t for t in HIDDEN_TOKENS if t in text]
        self.counts["observation_static_sha:" + sha(obs["static"])[:12]] += 1

    def _validate(self, event, frame, caller, ret):
        if event == "call":
            self.purposes[frame.f_locals.get("purpose")] += 1
            self._pending[id(frame)] = copy.deepcopy(frame.f_locals.get("action"))
            return
        action = self._pending.pop(id(frame), None)
        if not isinstance(ret, dict) or set(ret) != {"ok", "reasons", "validated_action"}:
            return self._bad("ValidationResult shape")
        if ret["ok"]:
            va = ret["validated_action"] or {}
            if set(va) != VALIDATED_KEYS:
                self._bad(f"validated_action keys {sorted(va)}")
            elif va["action"] != action:
                self._bad("validated_action.action differs from the submitted action")
        self.counts["validation_ok" if ret["ok"] else "validation_rejected"] += 1

    def _weather(self, event, frame, caller, ret):
        if event == "call":
            for leg in frame.f_locals.get("legs") or []:
                if set(leg) != LEG_KEYS or not (0 <= leg["start_s"] <= leg["end_s"]) or \
                        not all(math.isfinite(leg[k]) for k in ("start_s", "end_s", "speed_knots")):
                    self._bad(f"TimedLeg {sorted(leg)} {leg.get('start_s')}..{leg.get('end_s')}")
            self.counts["weather_calls"] += 1
        elif not (isinstance(ret, float) and math.isfinite(ret) and ret >= 0):
            self._bad(f"weather_exposure returned {ret!r}")

    def _water(self, event, frame, caller, ret):
        if event == "call":
            self.clearances[(caller, round(float(frame.f_locals.get("clearance_m", -1)), 3))] += 1

    def _run_scenario(self, event, frame, caller, ret):
        if event == "call":
            self.inputs_seen.append(copy.deepcopy(frame.f_locals.get("inputs")))


# --- fake AISStream source -----------------------------------------------------------------------
class FakeWS:
    """Async websocket stand-in: replays frames, then idles until the capture is stopped."""

    def __init__(self, frames):
        self.frames, self.sent = list(frames), []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, text):
        self.sent.append(json.loads(text))

    async def recv(self):
        if self.frames:
            return self.frames.pop(0)
        await asyncio.sleep(30)


def aisstream_frames() -> List[bytes]:
    """AISStream-shaped frames for the synthetic fixture track (fake MMSI 999000001)."""
    tmpl = {r["case"]: r["message"] for r in map(json.loads, (SOURCES / "aisstream_messages.fixture.jsonl")
                                                  .read_text().splitlines()) if r}
    recs = [json.loads(l) for l in (SOURCES / "capture.fixture.jsonl").read_text().splitlines() if l.strip()]
    frames = [json.dumps(tmpl["static_type5"]).encode()]
    for r in recs:
        if r["record_type"] != "POSITION":
            continue
        t = r["observed_at"]
        m = copy.deepcopy(tmpl["class_a_valid"])
        m["MetaData"].update(MMSI=int(r["mmsi"]), latitude=r["lat"], longitude=r["lon"],
                             time_utc=f"{t[:10]} {t[11:19]} +0000 UTC")
        m["Message"]["PositionReport"].update(Latitude=r["lat"], Longitude=r["lon"], Sog=r["sog_knots"],
                                              Cog=r["cog_deg"], Timestamp=int(t[17:19]), UserID=int(r["mmsi"]))
        frames.append(json.dumps(m).encode())
    return frames


class _NoNetwork:
    def __init__(self):
        self.attempts = 0

    def __enter__(self):
        self._orig = socket.socket.connect

        def refuse(sock, *a, **k):
            self.attempts += 1
            raise OSError("network blocked by integration trace")
        socket.socket.connect = refuse
        return self

    def __exit__(self, *exc):
        socket.socket.connect = self._orig
        return False


# --- the trace -----------------------------------------------------------------------------------
def run_trace(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = copy.deepcopy(cfg or load_config())
    checks: List[Tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    services = production_services(cfg)
    tracer = Tracer()
    frames_sent = aisstream_frames()
    ws = FakeWS(frames_sent)
    with _NoNetwork() as net:
        threading.setprofile(tracer)
        sys.setprofile(tracer)
        try:
            # 1. AIS API -> capture (real adapter, fake socket)
            handle = ais.AISCaptureHandle(cfg, "trace-dummy-key", connect=lambda url: ws).start()
            end = time.monotonic() + 5
            while handle.status()["records_received"] < len(frames_sent) and time.monotonic() < end:
                time.sleep(0.02)
            status = handle.status()
            records = handle.snapshot_records()
            handle.stop()
            check("AIS API -> capture: subscription, normalized records, copied snapshot",
                  status["state"] == C.CAPTURE_CAPTURING and status["records_valid"] == len(frames_sent)
                  and ws.sent and ws.sent[0]["BoundingBoxes"] == [[[33.8, -120.5], [34.6, -119.0]]]
                  and all({"mmsi", "observed_at", "received_at", "time_quality"} <= set(r) for r in records)
                  and "trace-dummy-key" not in json.dumps(records),
                  f"{status['state']} {status['records_valid']}/{len(frames_sent)} records")

            # 2. capture -> immutable route cache (real land), frozen through save/load
            land = services["load_land"](REAL_LAND, cfg)
            bundle = services["prepare_routes"](records, land, cfg)
            usable = [r for r in bundle["routes"] if r["validation"]["ok"]]
            check("capture -> route cache: usable observed route with capture checksum",
                  usable and bundle["source"]["capture_sha256"] == routes.capture_sha256(records)
                  and land["validated"] and not land["fixture_only"],
                  f"{len(usable)} usable route(s), {len(usable[0]['waypoints']) if usable else 0} waypoints")
            bundle["source"].update(kind=C.SYNTHETIC_FIXTURE, api_verified=False, fixture_only=True)
            bundle["issues"] = list(bundle["issues"]) + ["SYNTHETIC_FIXTURE_VIA_FAKE_WEBSOCKET"]
            with tempfile.TemporaryDirectory() as tmp:
                services["save_route_bundle"](bundle, Path(tmp) / "bundle.json")
                frozen = services["load_route_bundle"](Path(tmp) / "bundle.json", land, cfg)
            check("route cache round-trips unchanged",
                  [r["waypoints"] for r in frozen["routes"]] == [r["waypoints"] for r in bundle["routes"]])
            route = copy.deepcopy(frozen["routes"][0])     # snapshot; the late-update check mutates frozen

            # 3-11. scenario -> paired runs through the real simulator and advisor
            presets = services["load_presets"](cfg)
            pairs = {}
            for pid in ("crossing", "storm_detour"):
                inputs = services["build_run_inputs"](frozen, land, presets[pid], cfg)
                before = sha(inputs)
                pairs[pid] = (inputs, services["run_pair"](inputs, on_frame=None))
                check(f"{pid}: inputs unchanged by the run", sha(inputs) == before)
            # A capture/source update during a run must not leak into it: every frame mutates the
            # bundle the inputs were built from and appends a fresh capture record.
            inputs = services["build_run_inputs"](frozen, land, presets["crossing"], cfg)

            def mutate(_frame):
                frozen["routes"][0]["waypoints"].append({"lat": 0.0, "lon": 0.0})
                records.append(dict(records[-1], receipt_seq=records[-1]["receipt_seq"] + 1))
            late = services["run_pair"](inputs, on_frame=mutate)
            check("capture/source update mid-run cannot alter the frozen world",
                  late["advisor"]["summary"] == {**pairs["crossing"][1]["advisor"]["summary"],
                                                 "elapsed_wall_s": late["advisor"]["summary"]["elapsed_wall_s"]}
                  and late["advisor"]["manifest"]["route_bundle"]["sha256"] ==
                  pairs["crossing"][1]["advisor"]["manifest"]["route_bundle"]["sha256"])
        finally:
            sys.setprofile(None)
            threading.setprofile(None)

    inputs, pair = pairs["crossing"]
    adv, base = pair["advisor"], pair["baseline"]
    rid = route["route_id"]
    wsha = adv["manifest"]["route_bundle"]["routes_used"][rid]["waypoints_sha256"]
    check("route cache -> simulated fleet: frozen bundle and route bytes reach the world",
          all(s["route_bundle"] == inputs["route_bundle"] and s["land"] == inputs["land"]
              and s["scenario"] == inputs["scenario"] for s in tracer.inputs_seen[:2])
          and wsha == canonical_sha256(route["waypoints"])
          and adv["manifest"]["voyages"][0]["ship_id"] == f"sim:{route['source_mmsi']}:v1",
          f"routes_used[{rid}].waypoints_sha256={wsha[:12]}")
    statics = [k for k in tracer.counts if k.startswith("observation_static_sha:")]
    want = {sha({"land": i["land"], "weather": i["scenario"]["weather"]})[:12] for i, _ in pairs.values()}
    check("land + simulated weather -> snapshot (static is exactly the frozen inputs)",
          {k.split(":")[1] for k in statics} <= want and statics, f"{len(statics)} distinct static payloads")
    check("world -> sensors -> snapshot: simulated sightings, no hidden whale truth",
          tracer.counts["detections_seen"] > 0 and not tracer.hidden_tokens,
          f"{tracer.counts['detections_seen']} detections seen; hidden tokens {sorted(set(tracer.hidden_tokens))}")
    check("snapshot -> advisor: decide() called with observations only, Decision shape held",
          tracer.counts["decide"] > 0 and not tracer.problems, f"{tracer.counts['decide']} decisions")
    hull = geo.ship_clearance_m(route["length_m"], route["beam_m"], cfg["planner"]["clearance_margin_m"])
    adv_clear = {c for (o, c), n in tracer.clearances.items() if o == "advisor"}
    check("advisor -> full-path checks: Person 1 land check at the hull clearance radius",
          adv_clear == {round(hull, 3)} and any(e[2].endswith("segment_zone_intervals") and e[0] == "advisor"
                                                for e in tracer.edges),
          f"advisor clearance radii {sorted(adv_clear)} (hull {hull:.1f} m)")
    applied = [e for e in adv["events"] if e["type"] == "COMMAND_APPLIED"]
    check("checks -> validated commands: ISSUE and APPLY validation, each accepted action applied",
          tracer.purposes[C.PURPOSE_ISSUE] > 0 and tracer.purposes[C.PURPOSE_APPLY] > 0 and applied
          and len(applied) == sum(1 for e in adv["events"] if e["type"] == "ACTION_ACCEPTED"),
          f"purposes {dict(tracer.purposes)}; applied {len(applied)}")
    lats = [s["lat"] for f in adv["frames"] for s in f["ships"]]
    origins = {json.dumps(s["original_route"]) for f in adv["frames"] for s in f["ships"]}
    progress = [s["baseline_progress_index"] for f in adv["frames"] for s in f["ships"]]
    check("commands -> world: route change, forward rejoin, immutable baseline",
          applied[0]["details"]["command"] == C.DETOUR and max(abs(l - 34.2) for l in lats) > 0.004
          and any(e["type"] == "REJOINED" for e in adv["events"]) and origins == {json.dumps(route["waypoints"])}
          and progress == sorted(progress), f"max lateral {max(abs(l - 34.2) for l in lats) * 111e3:.0f} m")
    alerts = plots.alert_rows(adv)
    check("advisor -> UI: every action reaches the alert table",
          len(alerts) == sum(len(d["actions"]) for d in adv["decisions"]) > 0, f"{len(alerts)} alert rows")
    rows = plots.comparison_rows(pair["comparison"])
    check("world -> independent metrics -> UI comparison",
          any(e[0] == "simulation" for e in tracer.edges) and pair["comparison"]["paired"]
          and base["summary"]["modeled_contacts"] > adv["summary"]["modeled_contacts"] and rows,
          f"contacts NO_ACTION {base['summary']['modeled_contacts']} vs ADVISOR {adv['summary']['modeled_contacts']}")
    storm = pairs["storm_detour"][1]
    check("storm case: weather exposure measured on actual legs and reduced by the detour",
          storm["baseline"]["summary"]["weather_exposure_s"] > storm["advisor"]["summary"]["weather_exposure_s"]
          and storm["advisor"]["summary"]["route_changes"] >= 1,
          f"{storm['baseline']['summary']['weather_exposure_s']:.0f} -> "
          f"{storm['advisor']['summary']['weather_exposure_s']:.0f} severity-s")

    violations = forbidden_edges(tracer.edges)
    check("no call crosses an owner boundary the architecture forbids", not violations, "; ".join(violations[:8]))
    check("advisor and simulator ran offline", net.attempts == 0, f"{net.attempts} connect attempts")
    return {"checks": checks, "edges": tracer.edges, "problems": tracer.problems, "violations": violations,
            "ok": all(ok for _, ok, _ in checks)}


def forbidden_edges(edges: Counter) -> List[str]:
    """Owner-crossing calls not permitted by ``ALLOWED`` (e.g. advisor -> simulation, sources -> advisor)."""
    out = []
    for (caller, callee, fn), n in sorted(edges.items()):
        allowed = ALLOWED.get((caller, callee), set())
        if allowed is not None and fn.rsplit(".", 1)[1] not in allowed:
            out.append(f"{caller} -> {fn} x{n}")
    return out


def edge_summary(edges: Counter) -> List[str]:
    by_owner: Dict[Tuple[str, str], Counter] = {}
    for (caller, callee, fn), n in edges.items():
        by_owner.setdefault((caller, callee), Counter())[fn.split("save_the_whales.")[-1]] += n
    out = []
    for (caller, callee), fns in sorted(by_owner.items()):
        top = ", ".join(f"{f}x{n}" for f, n in fns.most_common(4))
        out.append(f"{caller:>10} -> {callee:<10} {sum(fns.values()):>7} calls  {top}")
    return out


def main() -> int:
    rep = run_trace()
    print("Owner-crossing calls (caller -> callee):")
    for line in edge_summary(rep["edges"]):
        print("  " + line)
    print("\nArchitecture §5 checks:")
    for name, ok, detail in rep["checks"]:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    for p in rep["problems"]:
        print("  payload problem:", p)
    print(f"\n{sum(ok for _, ok, _ in rep['checks'])}/{len(rep['checks'])} checks passed")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
