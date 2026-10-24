"""Run every scenario on every seed; print paired mean ± 95% CI vs Blanket-VSR (§8)."""
import argparse
import json
import statistics as st
from pathlib import Path

from simulator.baselines import BlanketVSR, NoAction, Oracle
from simulator.config import Config
from simulator.engine import run
from simulator.world import World
from your_project.advisor import Advisor


def scenarios(world):
    return {"no_action": NoAction, "blanket_vsr": lambda: BlanketVSR(world.vsr_zone),
            "advisor": Advisor, "oracle": lambda: Oracle(Advisor())}


def paired_ci(a, b):
    diffs = [x - y for x, y in zip(a, b)]
    m = st.mean(diffs)
    half = 1.96 * st.stdev(diffs) / len(diffs) ** 0.5 if len(diffs) > 1 else float("nan")  # TODO: t-quantile
    return m, half


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="1 week, 1 seed")
    ap.add_argument("--dev", action="store_true", help="use dev seeds instead of held-out eval seeds")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    cfg = Config.load(args.config) if args.config else Config.load()
    seeds = cfg["dev_seeds"] if args.dev else cfg["eval_seeds"]
    if args.quick:
        cfg.raw["months"], seeds = 7 / 30.4375, seeds[:1]
    world = World.load()

    results = {name: [run(make(), cfg, world, s) for s in seeds] for name, make in scenarios(world).items()}
    Path("runs").mkdir(exist_ok=True)
    Path("runs/results.json").write_text(json.dumps(results, indent=2))

    base = [r["expected_lethal"] for r in results["blanket_vsr"]]
    for name, rs in results.items():
        m, h = paired_ci([r["expected_lethal"] for r in rs], base)
        print(f"{name:12s} lethal={st.mean(r['expected_lethal'] for r in rs):.3f} "
              f"delay_h={st.mean(r['delay_h'] for r in rs):.1f}  Δlethal vs VSR={m:+.3f} ± {h:.3f}")


if __name__ == "__main__":
    main()
