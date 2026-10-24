"""Live inputs are fixtures; tests never depend on network availability."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
import unittest

from whaleguard.live import LiveFeed, parse_reports
from whaleguard.simulation import Simulation, rectangle

NOW = datetime(2026, 10, 24, 21, tzinfo=timezone.utc)


def report(id='a', **updates):
    row = dict(ssemmi_id=id, type='Humpback', latitude=48.0, longitude=-123.0,
               trusted=1, created='2026-10-24 12:00:00',
               ssemmi_date_added=format_datetime(NOW - timedelta(minutes=10)))
    row.update(updates)
    return row


class FeedTests(unittest.TestCase):
    def test_parse_without_fabricating_observation_timezone(self):
        parsed = parse_reports([report()], NOW)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].observed_raw, '2026-10-24 12:00:00')
        self.assertEqual(parsed[0].received, NOW - timedelta(minutes=10))

    def test_acartia_javascript_receipt_format(self):
        rows = [report(ssemmi_date_added='Sat Oct 24 2026 20:49:17 GMT+0000 (Coordinated Universal Time)')]
        parsed = parse_reports(rows, NOW)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].received.isoformat(), '2026-10-24T20:49:17+00:00')

    def test_reject_old_future_untrusted_invalid_nonwhales(self):
        rows = [report('old', ssemmi_date_added=format_datetime(NOW - timedelta(hours=7))),
                report('future', ssemmi_date_added=format_datetime(NOW + timedelta(hours=1))),
                report('untrusted', trusted=0), report('seal', type='Seal'),
                report('bad', latitude=float('nan')), report('bad2', longitude=200),
                report('bad3', ssemmi_date_added='broken'), {}, None]
        self.assertEqual(parse_reports(rows, NOW), [])

    def test_duplicate_latest_wins(self):
        rows = [report(), report(ssemmi_date_added=format_datetime(NOW), latitude=48.1)]
        self.assertEqual(len(parse_reports(rows, NOW)), 1)
        self.assertEqual(parse_reports(rows, NOW)[0].latitude, 48.1)

    def test_frame_fixed_across_updates(self):
        feed = LiveFeed()
        feed.update([report()], NOW)
        self.assertEqual(feed.center, (48, -123))
        feed.update([report('b', latitude=48.01, ssemmi_date_added=format_datetime(NOW))], NOW)
        self.assertEqual(feed.center, (48, -123))
        self.assertEqual(len(feed.zones()), 2)
        self.assertEqual(feed.xy(feed.reports['a']), (15, 9))
        self.assertGreater(feed.xy(feed.reports['b'])[1], 9)

    def test_far_reports_filtered(self):
        feed = LiveFeed()
        feed.update([report()], NOW)
        feed.update([report('far', latitude=34)], NOW)
        self.assertEqual(len(feed.zones()), 1)

    def test_outage_and_empty_response_preserve_until_expiry(self):
        feed = LiveFeed()
        feed.update([report()], NOW)
        success = feed.last_success
        feed.fail()
        self.assertEqual(feed.status, 'unavailable')
        self.assertEqual(feed.last_success, success)
        self.assertEqual(len(feed.zones()), 1)
        feed.update([], NOW)
        self.assertEqual(feed.status, 'connected')
        self.assertEqual(len(feed.zones()), 1)
        feed.expire(NOW + timedelta(hours=7))
        self.assertEqual(feed.zones(), [])

    def test_invalid_top_level_is_not_success(self):
        feed = LiveFeed()
        with self.assertRaises(ValueError):
            feed.update({'error': 'bad'}, NOW)
        self.assertIsNone(feed.last_success)

    def test_snapshot_redacts_private_fields(self):
        feed = LiveFeed()
        feed.update([report(data_source_witness='private name', data_source_comments='private comments')], NOW)
        text = json.dumps(feed.snapshot(NOW), allow_nan=False)
        self.assertNotIn('private', text)
        self.assertNotIn('data_source_witness', text)


class LiveSimulationTests(unittest.TestCase):
    def test_live_disables_timed_fake_event(self):
        sim = Simulation(live=True)
        sim.advance(.3)
        self.assertNotIn(sim.event_zone, sim.zones)
        self.assertEqual(len(sim.advisories), 1)

    def test_repeated_update_deduplicates_and_expiry_replans(self):
        sim = Simulation(live=True)
        sim.advance(.1)
        position = sim.position
        zones = [rectangle('report buffer', 9, 0, 22, 9.5, 4)]
        sim.apply_live_zones(zones)
        self.assertEqual(sim.impact['replan_position'], position)
        count = len(sim.advisories)
        sim.apply_live_zones(zones)
        self.assertEqual(len(sim.advisories), count)
        sim.apply_live_zones([])
        self.assertEqual(len(sim.advisories), count + 1)
        self.assertEqual(len(sim.zones), 2)
        self.assertLess(sim.impact['arrival_change_min'], 0)

    def test_block_then_recover_is_json_serializable(self):
        sim = Simulation(live=True)
        sim.apply_live_zones([rectangle('wall', 10, 0, 11, 18)])
        self.assertEqual(sim.status, 'holding')
        json.dumps(sim.snapshot(), allow_nan=False)
        sim.apply_live_zones([])
        self.assertEqual(sim.status, 'navigating')
        self.assertIsNone(sim.impact['arrival_change_min'])
        json.dumps(sim.snapshot(), allow_nan=False)

    def test_real_feed_shape_drives_simulation(self):
        feed, sim = LiveFeed(), Simulation(live=True)
        feed.update([report()], NOW)
        sim.apply_live_zones(feed.zones())
        self.assertIsNotNone(sim.impact)
        self.assertEqual(sim.position, sim.start)
        self.assertEqual(len(sim.zones), 3)
        sim.advance(10)
        self.assertEqual(sim.status, 'arrived')


if __name__ == '__main__':
    unittest.main()
