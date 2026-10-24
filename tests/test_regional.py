import json
import math
import unittest
from datetime import datetime

from whaleguard.regional import RegionalSimulation, project, unproject, swept_distance, move_route


class RegionalTests(unittest.TestCase):
    def test_projection_roundtrip(self):
        p=(-119.6982,34.4208)
        back=unproject(project(p))
        self.assertAlmostEqual(p[0],back[0]);self.assertAlmostEqual(p[1],back[1])

    def test_recorded_route_is_ordered_and_clear_of_land(self):
        s=RegionalSimulation()
        timestamps=[datetime.fromisoformat(p['time'].replace('Z','+00:00')) for p in s.track['points']]
        self.assertTrue(all(a<b for a,b in zip(timestamps,timestamps[1:])))
        self.assertTrue(all(s.clear(a,b) for a,b in zip(s.original,s.original[1:])))
        self.assertGreater(len(s.data['tracks']),1)
        self.assertTrue(all(r['license']=='CC0' for r in s.data['whale_sightings']))

    def test_swept_crossing_catches_collision_between_endpoints(self):
        self.assertAlmostEqual(swept_distance((0,0),(2,0),(1,-1),(1,1)),0)
        self.assertAlmostEqual(swept_distance((0,0),(2,0),(0,2),(2,2)),2)

    def test_conflict_controller_yields_without_colliding(self):
        s=RegionalSimulation()
        s.route=[(0,0),(2,0)];s.position=(0,0)
        s.traffic=[{'id':'crossing','route':[(1,-1),(1,1)],'position':(1,-1),'speed':24,'active':True}]
        s.last_traffic_plan=s.elapsed
        s.step(1/12)
        self.assertEqual(s.position,(0,0))
        self.assertGreater(s.hold_hours,0)
        self.assertGreaterEqual(s.min_separation,s.snapshot()['separation_km'])

    def test_complete_voyage_and_boundary_safe_replans(self):
        s=RegionalSimulation()
        s.advance(30/60)
        self.assertTrue(s.event_done)
        self.assertTrue(any('traffic encounter' in a['text'] for a in s.advisories))
        self.assertTrue(all(s.clear(a,b) for a,b in zip(s.planned_route,s.planned_route[1:])))
        self.assertGreater(s.impact['saved_min'],0)
        whale_impact=s.impact['arrival_change_min']
        self.assertGreater(whale_impact,0)
        s.advance(6)
        self.assertEqual(s.status,'arrived')
        self.assertEqual(s.position,s.destination)
        self.assertGreaterEqual(s.min_separation,s.snapshot()['separation_km'])
        json.dumps(s.snapshot(),allow_nan=False)

    def test_unique_run_id(self):
        self.assertNotEqual(RegionalSimulation().run_id,RegionalSimulation().run_id)

    def test_live_position_snapshot_causes_hold(self):
        s=RegionalSimulation()
        s.last_traffic_plan=0
        s.live_vessels=[{'position':s.position}]
        before=s.position
        s.step(5/3600)
        self.assertEqual(s.position,before)
        self.assertGreater(s.hold_hours,0)

    def test_holding_retries_when_live_report_clears(self):
        s=RegionalSimulation();s.holding=True;s.last_traffic_plan=0
        s.elapsed=2/60
        s.step(5/3600)
        self.assertFalse(s.holding)


if __name__=='__main__':
    unittest.main()
