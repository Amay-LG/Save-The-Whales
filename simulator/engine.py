"""Main simulation loop (§7)."""
import copy
import json
import time

from simulator import strike
from simulator.commands import Rejected, validate
from simulator.metrics import Ledger
from simulator.sensors import SensorSuite
from simulator.ships import Fleet, build_schedule
from simulator.whales import WhalePopulation


def json_roundtrip(obj):
    return json.loads(json.dumps(obj, default=str))


def timed_call(fn, obs, ledger):
    t0 = time.perf_counter()
    try:
        out = fn(obs) or []
    except Exception as e:  # advisor bugs must not kill the run
        ledger.log.append(("advisor_error", repr(e)))
        out = []
    ledger.advisor_ms.append((time.perf_counter() - t0) * 1000)
    return out


def build_observation(t, world, fleet, detections, oracle=None) -> dict:
    """Assemble contract.schema.Observation (ships AIS-like; whales = detections or oracle)."""
    raise NotImplementedError


def run(advisor, cfg, world, seed) -> dict:
    whales = WhalePopulation(cfg, seed, world.density, world.land)
    voyages = build_schedule(cfg, seed, world.lanes)
    sensors = SensorSuite(cfg, seed)
    fleet, ledger = Fleet(world), Ledger()
    wlen_km = cfg["whales"]["length_m"] / 1000

    t, end, tick = cfg.start, cfg.start + cfg.duration, 0
    while t < end or fleet.active:                      # drain period (H2)
        if t < end:
            fleet.spawn_due(voyages, t)
        sensors.observe(t, tick, whales, fleet)

        if tick % cfg.ticks_per_decision == 0 and fleet.active:
            obs = build_observation(t, world, fleet, sensors.reported_by(t),
                                    oracle=whales if advisor.oracle else None)
            for a in timed_call(advisor.advise, json_roundtrip(obs), ledger):
                try:
                    fleet.submit(validate(a, world, fleet, t, cfg), t, ledger)
                except Rejected as e:
                    ledger.invalid_commands += 1
                    ledger.log.append(("rejected", str(e), copy.deepcopy(a)))

        fleet.apply_due_commands(t)
        w0 = whales.positions()
        avail = {w.whale_id: whales.availability(w) for w in whales.whales}
        whales.step(cfg["dt_min"])
        w1 = whales.positions()
        grid = whales.grid()
        for ship, seg in fleet.step(cfg["dt_min"]):
            xs, ys = (seg.p0[0], seg.p1[0]), (seg.p0[1], seg.p1[1])
            m = cfg.margin_km
            for wid in grid.query((min(xs) - m, min(ys) - m, max(xs) + m, max(ys) + m)):
                a, b = w0[wid], w1[wid]                 # interpolate to sub-segment span (I3.5)
                lerp = lambda f: (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
                strike.accumulate(ship, seg, lerp(seg.t0_frac), lerp(seg.t1_frac),
                                  avail[wid], ledger, cfg.sigma_km, wlen_km)
        t += cfg.dt
        tick += 1
        fleet.despawn_arrived(t, ledger)
    return ledger.summary()
