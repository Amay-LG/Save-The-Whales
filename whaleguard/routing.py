"""Minimum-time routing on a 1 km, eight-neighbour navigation graph.

Coordinates are fictional kilometres, speed is km/h, time is hours. Segment
costs integrate every polygon crossing; the graph approximates continuous space.
"""
from dataclasses import dataclass
import heapq
import math

from simulator.geo import point_in_polygon  # Reuse the existing ray-casting helper.

EPS = 1e-9
SERVICE_SPEED = 24.0
BOUNDS = (0, 30, 0, 18)


@dataclass(frozen=True)
class Zone:
    name: str
    polygon: tuple
    speed: float | None = None  # None means blocked, including its boundary.

    def __post_init__(self):
        if self.speed is not None and self.speed <= 0:
            raise ValueError("Speed limits must be positive")


def cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def sub(a, b):
    return (a[0] - b[0], a[1] - b[1])


def lerp(a, b, t):
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def on_segment(p, a, b):
    return abs(cross(sub(p, a), sub(b, a))) <= EPS and all(
        min(a[i], b[i]) - EPS <= p[i] <= max(a[i], b[i]) + EPS for i in (0, 1))


def edges(poly):
    return zip(poly, poly[1:] + poly[:1])


def contains(p, poly):
    return any(on_segment(p, a, b) for a, b in edges(poly)) or point_in_polygon(p, poly)


def crossings(a, b, c, d):
    """Segment parameters at intersections, including collinear overlap endpoints."""
    r, s, q = sub(b, a), sub(d, c), sub(c, a)
    den = cross(r, s)
    if abs(den) > EPS:
        t, u = cross(q, s) / den, cross(q, r) / den
        return [max(0.0, min(1.0, t))] if -EPS <= t <= 1 + EPS and -EPS <= u <= 1 + EPS else []
    if abs(cross(q, r)) > EPS:
        return []
    rr = r[0] ** 2 + r[1] ** 2
    if rr <= EPS:
        return [0.0] if on_segment(a, c, d) else []
    ts = [sum((p[i] - a[i]) * r[i] for i in (0, 1)) / rr for p in (c, d)]
    lo, hi = max(0.0, min(ts)), min(1.0, max(ts))
    return [lo, hi] if lo <= hi + EPS else []


def segment_parts(a, b, zones, service_speed=SERVICE_SPEED):
    """Return (start, end, speed) pieces; None for any blocked-area contact."""
    if service_speed <= 0:
        raise ValueError("Service speed must be positive")
    cuts = [0.0, 1.0]
    for z in zones:
        hits = [t for c, d in edges(z.polygon) for t in crossings(a, b, c, d)]
        if z.speed is None:
            if hits or contains(a, z.polygon) or contains(b, z.polygon):
                return None
        else:
            cuts.extend(hits)
    cuts = sorted(set(cuts))
    result = []
    for lo, hi in zip(cuts, cuts[1:]):
        if hi - lo <= EPS:
            continue
        mid = lerp(a, b, (lo + hi) / 2)
        speed = min([service_speed] + [z.speed for z in zones if z.speed is not None and contains(mid, z.polygon)])
        result.append((lerp(a, b, lo), lerp(a, b, hi), speed))
    return result


def segment_time(a, b, zones):
    parts = segment_parts(a, b, zones)
    return math.inf if parts is None else sum(math.dist(p, q) / v for p, q, v in parts)


def route_time(route, zones):
    return sum(segment_time(a, b, zones) for a, b in zip(route, route[1:]))


def shortest_route(start, goal, zones, bounds=BOUNDS):
    """Dijkstra; exact minimum travel time on this graph, not continuous space.

    Off-grid ship positions connect to the nearest 16 visible grid nodes. A
    direct start-to-goal edge is included when clear. No blocked corner cutting.
    """
    start, goal = tuple(start), tuple(goal)
    xmin, xmax, ymin, ymax = bounds
    def valid(p):
        return xmin <= p[0] <= xmax and ymin <= p[1] <= ymax and not any(
            z.speed is None and contains(p, z.polygon) for z in zones)
    if not valid(start) or not valid(goal):
        raise ValueError("Start or destination is blocked or outside the simulation")
    if start == goal:
        return [start]
    nodes = {(x, y) for x in range(xmin, xmax + 1) for y in range(ymin, ymax + 1) if valid((x, y))}
    graph = {p: [] for p in nodes | {start, goal}}
    def connect(a, b):
        cost = segment_time(a, b, zones)
        if math.isfinite(cost):
            graph[a].append((b, cost))
            graph[b].append((a, cost))
    for x, y in sorted(nodes):
        for dx, dy in ((0, 1), (1, -1), (1, 0), (1, 1)):
            q = (x + dx, y + dy)
            if q in nodes:
                connect((x, y), q)
    for p in (start, goal):
        count = 0
        for q in sorted(nodes - {p}, key=lambda q: (math.dist(p, q), q)):
            if math.isfinite(segment_time(p, q, zones)):
                connect(p, q)
                count += 1
                if count == 16:
                    break
    connect(start, goal)
    queue, distance, previous = [(0, start)], {start: 0.0}, {}
    while queue:
        cost, p = heapq.heappop(queue)
        if cost > distance[p]:
            continue
        if p == goal:
            path = [p]
            while p != start:
                p = previous[p]
                path.append(p)
            return path[::-1]
        for q, dt in graph[p]:
            candidate = cost + dt
            if candidate < distance.get(q, math.inf):
                distance[q], previous[q] = candidate, p
                heapq.heappush(queue, (candidate, q))
    raise ValueError("No traversable route")
