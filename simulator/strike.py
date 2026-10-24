"""Swept, deduplicated, expected-value strike model (§5.5). Positions in km."""
import math

CLOSE_APPROACH_KM = 0.5


def p_lethal(v_kn):
    """Vanderlaan & Taggart 2007: P(lethal | strike)."""
    return 1 / (1 + math.exp(-(-4.89 + 0.41 * v_kn)))


def _phi(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def p_path(d_km, half_width_km, sigma_km):
    """P(whale inside hull strip) under Gaussian lateral jitter."""
    return _phi((half_width_km - d_km) / sigma_km) - _phi((-half_width_km - d_km) / sigma_km)


def crossing(p0, p1, w0, w1):
    """Return (tau, lateral_miss_km) if the whale passes bow->astern in tau in [0, 1), else None."""
    hx, hy = p1[0] - p0[0], p1[1] - p0[1]
    seg = math.hypot(hx, hy)
    if seg == 0:
        return None
    hx, hy = hx / seg, hy / seg
    r0 = (w0[0] - p0[0], w0[1] - p0[1])
    dr = ((w1[0] - w0[0]) - (p1[0] - p0[0]), (w1[1] - w0[1]) - (p1[1] - p0[1]))
    s0 = r0[0] * hx + r0[1] * hy
    s1 = s0 + dr[0] * hx + dr[1] * hy
    if not (s0 >= 0 > s1):
        return None
    tau = s0 / (s0 - s1)
    return tau, (r0[0] + dr[0] * tau) * -hy + (r0[1] + dr[1] * tau) * hx


def accumulate(ship, seg, whale_w0, whale_w1, availability, ledger, sigma_km, whale_length_km):
    """seg: object with p0, p1, speed_kn. whale_w0/w1: whale XY at the sub-segment's start/end."""
    hit = crossing(seg.p0, seg.p1, whale_w0, whale_w1)
    if hit is None:
        return
    _, d = hit
    pp = p_path(d, ship.beam_m / 2000 + whale_length_km / 2, sigma_km)
    ledger.expected_strikes += availability * pp
    ledger.expected_lethal += availability * pp * p_lethal(seg.speed_kn)
    if abs(d) < CLOSE_APPROACH_KM:
        ledger.close_approaches += 1
