from datetime import datetime, timedelta, timezone
import json
import threading
import unittest

from whaleguard.aisstream import AisStream

NOW=datetime(2026,10,24,22,tzinfo=timezone.utc)


def message(**updates):
    msg={'Valid':True,'UserID':123456789,'Latitude':34.2,'Longitude':-119.8,'Sog':12,'Cog':270}
    msg.update(updates)
    return {'MessageType':'PositionReport','MetaData':{'MMSI':123456789,'ShipName':'TEST SHIP','time_utc':'2026-10-24 22:00:00 UTC'},'Message':{'PositionReport':msg}}


class AisTests(unittest.TestCase):
    def test_binary_report_and_redacted_snapshot(self):
        feed=AisStream(True,'test-private-key')
        feed.ingest(json.dumps(message()).encode(),NOW)
        snapshot=feed.snapshot(NOW)
        self.assertEqual(len(snapshot['vessels']),1)
        self.assertEqual(snapshot['vessels'][0]['speed_kn'],12)
        self.assertNotIn('test-private-key',json.dumps(snapshot))
        self.assertEqual(feed.subscription()['BoundingBoxes'][0][0],[33.85,-120.7])

    def test_confirmation_has_no_metadata(self):
        feed=AisStream(True,'key')
        feed.ingest({'MessageType':'SubscriptionConfirmation','Message':{'CompressionEnabled':True}},NOW)
        self.assertEqual(feed.status,'connected');self.assertTrue(feed.compression)

    def test_invalid_or_out_of_region_ignored(self):
        feed=AisStream(True,'key')
        for row in [message(Latitude=90),message(Longitude=-70),message(Valid=False),message(Latitude=float('nan')),None,b'bad json']:
            feed.ingest(row,NOW)
        self.assertEqual(feed.snapshot(NOW)['vessels'],[])

    def test_source_age_and_out_of_order(self):
        feed=AisStream(True,'key')
        feed.ingest(message(),NOW)
        old=message(Latitude=34.3);old['MetaData']['time_utc']='2026-10-24 21:59:00 UTC'
        feed.ingest(old,NOW)
        self.assertEqual(feed.snapshot(NOW)['vessels'][0]['latitude'],34.2)
        stale=message();stale['MetaData']['time_utc']='2026-10-24 20:00:00 UTC'
        feed.ingest(stale,NOW)
        self.assertEqual(feed.received_count,1)

    def test_stale_then_expired(self):
        feed=AisStream(True,'key');feed.ingest(message(),NOW)
        self.assertTrue(feed.snapshot(NOW+timedelta(seconds=121))['vessels'][0]['stale'])
        self.assertEqual(feed.snapshot(NOW+timedelta(seconds=301))['vessels'],[])

    def test_ais_unavailable_speed_and_course_sentinels(self):
        feed=AisStream(True,'key');feed.ingest(message(Sog=102.3,Cog=360),NOW)
        vessel=feed.snapshot(NOW)['vessels'][0]
        self.assertIsNone(vessel['speed_kn']);self.assertIsNone(vessel['course_deg'])

    def test_missing_key_does_not_open_connection(self):
        feed=AisStream(True)
        feed.run(threading.Event(),connector=lambda *a,**kw:self.fail('must not connect'))
        self.assertEqual(feed.status,'not_configured')

    def test_complete_subscription_sent_immediately_and_errors_redacted(self):
        stop=threading.Event();sent=[]
        class Socket:
            def __enter__(self):return self
            def __exit__(self,*_):pass
            def send(self,data):sent.append(json.loads(data))
            def recv(self,timeout):
                stop.set();return json.dumps({'error':'bad key: secret'})
        feed=AisStream(True,'secret')
        feed.run(stop,connector=lambda *a,**kw:Socket())
        self.assertEqual(sent[0]['APIKey'],'secret')
        self.assertEqual(feed.status,'subscription_error')
        self.assertNotIn('secret',json.dumps(feed.snapshot(NOW)))


if __name__=='__main__':unittest.main()
