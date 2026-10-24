"""Run without dependencies: python3 -m unittest tests.test_whaleguard -v."""
import math
import unittest

from whaleguard.routing import Zone, segment_time, segment_parts, shortest_route, route_time
from whaleguard.simulation import Simulation, rectangle


class GeometryTests(unittest.TestCase):
    def test_crossing_with_both_endpoints_outside(self):
        zone = rectangle('slow', 4, -1, 6, 1, 6)
        self.assertAlmostEqual(segment_time((0, 0), (10, 0), [zone]), 8 / 24 + 2 / 6)

    def test_overlaps_choose_lowest_speed(self):
        zones = [rectangle('A', 2, -1, 8, 1, 12), rectangle('B', 4, -1, 6, 1, 4)]
        self.assertAlmostEqual(segment_time((0, 0), (10, 0), zones), 4 / 24 + 4 / 12 + 2 / 4)

    def test_thin_zone_not_missed(self):
        zone = rectangle('thin', 4.1234, -1, 4.1235, 1, 1)
        self.assertAlmostEqual(segment_time((0, 0), (10, 0), [zone]), 9.9999 / 24 + .0001)

    def test_blocked_interior_edge_and_corner_contact(self):
        zone = rectangle('land', 1, 1, 2, 2)
        for a, b in [((0, 1.5), (3, 1.5)), ((0, 1), (3, 1)), ((0, 2), (2, 0)),
                     ((1.5, 1.5), (1.6, 1.6)), ((1, 1), (0, 0))]:
            with self.subTest(a=a, b=b):
                self.assertTrue(math.isinf(segment_time(a, b, [zone])))

    def test_slow_boundary_is_speed_limited(self):
        z = rectangle('slow', 1, 1, 2, 2, 6)
        self.assertAlmostEqual(segment_time((0, 1), (3, 1), [z]), 2 / 24 + 1 / 6)

    def test_non_rectangular_zone(self):
        z = Zone('triangle', ((2, -2), (6, 0), (2, 2)), 6)
        self.assertAlmostEqual(segment_time((0, 0), (8, 0), [z]), 4 / 24 + 4 / 6)


class RoutingTests(unittest.TestCase):
    def test_mild_slowdown_beats_detour(self):
        z = rectangle('mild', 4, 3, 6, 7, 20)
        route = shortest_route((1, 5), (9, 5), [z], (0, 10, 0, 10))
        self.assertAlmostEqual(route_time(route, [z]), 6 / 24 + 2 / 20)

    def test_severe_slowdown_makes_detour_faster(self):
        z = rectangle('severe', 4, 3, 6, 7, 1)
        route = shortest_route((1, 5), (9, 5), [z], (0, 10, 0, 10))
        self.assertLess(route_time(route, [z]), segment_time((1, 5), (9, 5), [z]))
        self.assertGreater(sum(math.dist(a, b) for a, b in zip(route, route[1:])), 8)

    def test_land_avoidance_and_off_grid_start(self):
        z = rectangle('land', 4, 3, 6, 7)
        route = shortest_route((1.23, 5.12), (9, 5), [z], (0, 10, 0, 10))
        self.assertEqual(route[0], (1.23, 5.12))
        self.assertTrue(all(segment_parts(a, b, [z]) is not None for a, b in zip(route, route[1:])))

    def test_unreachable_and_blocked_start(self):
        z = rectangle('wall', 4, 0, 6, 10)
        with self.assertRaises(ValueError):
            shortest_route((1, 5), (9, 5), [z], (0, 10, 0, 10))
        with self.assertRaises(ValueError):
            shortest_route((5, 5), (9, 5), [z], (0, 10, 0, 10))

    def test_same_start_destination(self):
        self.assertEqual(shortest_route((1, 1), (1, 1), []), [(1, 1)])


class SimulationTests(unittest.TestCase):
    def test_movement_splits_at_zone_boundaries(self):
        s = Simulation()
        s.position, s.route = (0, 0), [(0, 0), (10, 0)]
        s.zones = [rectangle('slow', 4, -1, 6, 1, 6)]
        s.move(.25)  # 10 minutes to x=4, then 5 minutes at 6 km/h.
        self.assertAlmostEqual(s.position[0], 4.5)
        s.move(.25)
        self.assertAlmostEqual(s.position[0], 6)
        s.move(1 / 6)
        self.assertEqual(s.position, (10, 0))

    def test_replan_position_and_eta_accounting(self):
        s = Simulation()
        s.advance(s.event_at)
        position = s.position
        s.advance(1e-6)
        self.assertEqual(s.impact['replan_position'], position)
        self.assertEqual(s.old_route[0], position)
        self.assertNotEqual(s.old_route[1:], s.route[1:])
        impact = s.impact
        self.assertAlmostEqual(impact['arrival_change_min'], impact['new_remaining_min'] - impact['previous_remaining_min'])
        self.assertAlmostEqual(impact['saved_vs_old_route_min'], impact['old_route_new_limits_min'] - impact['new_remaining_min'])
        self.assertGreater(impact['saved_vs_old_route_min'], 0)
        self.assertTrue(all(segment_parts(a, b, s.zones) is not None for a, b in zip(s.route, s.route[1:])))
        s.advance(10)
        self.assertEqual(s.status, 'arrived')
        self.assertEqual(s.position, s.destination)
        self.assertAlmostEqual(s.elapsed * 60, s.baseline * 60 + impact['arrival_change_min'])
        self.assertEqual(len(s.advisories), 3)

    def test_tick_size_does_not_change_outcome(self):
        large, small = Simulation(), Simulation()
        large.advance(10)
        for _ in range(1500):
            small.advance(.001)
        self.assertEqual(large.position, small.position)
        self.assertAlmostEqual(large.elapsed, small.elapsed)
        self.assertAlmostEqual(large.impact['arrival_change_min'], small.impact['arrival_change_min'])

    def test_unreachable_replan_holds(self):
        s = Simulation()
        s.event_zone = rectangle('new exclusion', 10, 0, 11, 18)
        s.advance(1)
        self.assertEqual(s.status, 'holding')
        held = s.position
        s.advance(1)
        self.assertEqual(s.position, held)
        self.assertIsNone(s.snapshot()['remaining_min'])


if __name__ == '__main__':
    unittest.main()
