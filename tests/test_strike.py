import random

from simulator import strike

STEP = 15 * 1.852 / 12  # km per 5-min tick at 15 kn
W, SIG = 0.032, 0.3


def test_lethality():
    for v, p in [(10, 0.312), (15, 0.779), (20, 0.965)]:
        assert abs(strike.p_lethal(v) - p) < 1e-3


def test_tunnelling_regression():
    tau, d = strike.crossing((0, 0), (STEP, 0), (STEP / 2, 0), (STEP / 2, 0))
    assert abs(tau - 0.5) < 1e-9 and d == 0
    assert strike.p_path(d, W, SIG) > 0.08


def test_boundary_counted_once():
    hits = [strike.crossing((i * STEP, 0), ((i + 1) * STEP, 0), (STEP, 0.01), (STEP, 0.01)) for i in range(2)]
    assert sum(h is not None for h in hits) == 1


def test_each_pass_counted_once():
    rng = random.Random(0)
    for _ in range(2000):
        wx, wy, vx, vy = rng.uniform(1, 20), rng.uniform(-1, 1), rng.uniform(-.1, .1), rng.uniform(-.1, .1)
        n = sum(strike.crossing((i * STEP, 0), ((i + 1) * STEP, 0),
                                (wx + vx * i, wy + vy * i), (wx + vx * (i + 1), wy + vy * (i + 1))) is not None
                for i in range(40))
        assert n == 1
