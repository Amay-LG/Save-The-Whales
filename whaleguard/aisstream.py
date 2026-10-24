"""Optional server-side AISStream transport; credentials never enter snapshots."""
from datetime import datetime, timezone
import json
import math
import random
import threading

from .regional import project

ENDPOINT = 'wss://stream.aisstream.io/v0/stream'
# API corners are LATITUDE, LONGITUDE (opposite the GeoJSON order).
BOUNDING_BOXES = [[[33.85, -120.7], [34.6, -119.0]]]
MESSAGE_TYPES = ['PositionReport', 'StandardClassBPositionReport', 'ExtendedClassBPositionReport']


class AisStream:
    def __init__(self, enabled=False, api_key=None):
        self._api_key=api_key
        self.enabled=enabled
        self.status='disabled' if not enabled else ('connecting' if api_key else 'not_configured')
        self.detail='Optional live AIS overlay is off.' if not enabled else ('Connecting to AISStream.' if api_key else 'Set AISSTREAM_API_KEY in the launch terminal.')
        self.vessels={}
        self.lock=threading.Lock()
        self.compression=None
        self.last_message=None
        self.received_count=0

    def subscription(self):
        # This method is for the transport only. Never serialize it to the UI/logs.
        return {'APIKey':self._api_key,'BoundingBoxes':BOUNDING_BOXES,'FilterMessageTypes':MESSAGE_TYPES}

    def ingest(self, raw, now=None):
        now=now or datetime.now(timezone.utc)
        try:
            if isinstance(raw,bytes):
                raw=raw.decode('utf-8')
            data=json.loads(raw) if isinstance(raw,str) else raw
            if not isinstance(data,dict):
                return
            if 'error' in data or 'Error' in data:
                with self.lock:
                    self.status='subscription_error'
                    self.detail='AISStream rejected the subscription. Check the key and account connection limits.'
                return  # Do not echo provider error text; it may contain secrets.
            kind=data.get('MessageType')
            if kind=='SubscriptionConfirmation':
                with self.lock:
                    self.status='connected'
                    self.detail='Subscribed to Santa Barbara. Waiting for position reports.'
                    self.compression=data.get('Message',{}).get('CompressionEnabled')
                return
            if kind not in MESSAGE_TYPES:
                return
            msg=data['Message'][kind]
            meta=data.get('MetaData',{})
            if msg.get('Valid') is False:
                return
            mmsi=str(meta.get('MMSI',msg.get('UserID','')))
            if len(mmsi)!=9 or not mmsi.isdigit():
                return
            lat=float(msg.get('Latitude',meta.get('Latitude')))
            lon=float(msg.get('Longitude',meta.get('Longitude')))
            if not math.isfinite(lat+lon) or not (33.85<=lat<=34.6 and -120.7<=lon<=-119.0):
                return
            source_time=None
            stamp=meta.get('time_utc')
            if isinstance(stamp,str):
                try:
                    source_time=datetime.fromisoformat(stamp.replace(' UTC','+00:00').replace('Z','+00:00'))
                    if source_time.tzinfo is None:
                        source_time=None
                except ValueError:
                    pass
            if source_time is not None and not -30<=(now-source_time).total_seconds()<=300:
                return
            def number(field,upper):
                try:
                    v=float(msg[field])
                    return v if math.isfinite(v) and 0<=v<upper else None
                except (KeyError,TypeError,ValueError):
                    return None
            speed=number('Sog',102.2)
            course=number('Cog',360)
            point=project((lon,lat))
            vessel={'mmsi':mmsi,'name':str(meta.get('ShipName','')).strip()[:80] or mmsi,
                    'position':point,'latitude':lat,'longitude':lon,
                    'speed_kn':speed,'course_deg':course,
                    'received_utc':now.isoformat(),
                    'source_time_utc':source_time.isoformat() if source_time else None,
                    '_received':now}
            with self.lock:
                previous=self.vessels.get(mmsi)
                if previous and source_time and previous.get('source_time_utc') and source_time.isoformat()<previous['source_time_utc']:
                    return
                trail=list(previous.get('trail',[])) if previous else []
                if not trail or math.dist(trail[-1],point)>.03:
                    trail.append(point)
                vessel['trail']=trail[-30:]
                self.vessels[mmsi]=vessel
                if len(self.vessels)>500:
                    oldest=min(self.vessels,key=lambda k:self.vessels[k]['_received'])
                    del self.vessels[oldest]
                self.status='connected'
                self.detail='Receiving live AIS reports. Ship reception age is shown on the map.'
                self.last_message=now.isoformat()
                self.received_count+=1
        except (KeyError,TypeError,ValueError,AttributeError,UnicodeDecodeError,OverflowError):
            return

    def snapshot(self,now=None):
        now=now or datetime.now(timezone.utc)
        with self.lock:
            self.vessels={k:v for k,v in self.vessels.items() if (now-v['_received']).total_seconds()<=300}
            vessels=[]
            for v in self.vessels.values():
                age=max(0,(now-v['_received']).total_seconds())
                row={k:value for k,value in v.items() if not k.startswith('_')}
                row.update(age_seconds=age,stale=age>120)
                vessels.append(row)
            return {'provider':'AISStream','status':self.status,'detail':self.detail,
                    'compression_enabled':self.compression,'last_message_utc':self.last_message,
                    'received_count':self.received_count,'vessels':vessels}

    def run(self,stop,connector=None):
        if not self.enabled or not self._api_key:
            return
        if connector is None:
            try:
                from websockets.sync.client import connect
                connector=connect
            except ImportError:
                with self.lock:
                    self.status='dependency_missing'
                    self.detail='Install the optional dependency: python3 -m pip install -r requirements-ais.txt'
                return
        delay=1
        while not stop.is_set():
            try:
                with connector(ENDPOINT,compression='deflate',open_timeout=10,
                               ping_interval=20,ping_timeout=20,close_timeout=2,max_size=1_000_000) as socket:
                    socket.send(json.dumps(self.subscription()))
                    with self.lock:
                        self.status='subscribing'
                        self.detail='Connection open; awaiting subscription confirmation.'
                    connected_at=datetime.now(timezone.utc)
                    while not stop.is_set():
                        try:
                            raw=socket.recv(timeout=1)
                        except TimeoutError:
                            with self.lock:
                                if self.status=='subscribing' and (datetime.now(timezone.utc)-connected_at).total_seconds()>15:
                                    raise ConnectionError('No subscription confirmation')
                            continue
                        self.ingest(raw)
                        if self.status=='subscription_error':
                            return
                        if self.status=='connected':
                            delay=1
            except Exception:
                with self.lock:
                    self.status='reconnecting'
                    self.detail='AIS connection interrupted. Retrying with backoff; retained reports age out after 5 minutes.'
            if stop.wait(delay+random.uniform(0,min(delay,2))):
                break
            delay=min(60,delay*2)
