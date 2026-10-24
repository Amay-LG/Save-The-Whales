"""Santa Barbara replay geography with wholly simulated navigation and traffic.

Archived route coordinates are observed AIS positions. The navigation graph is
an illustrative +/-4 km corridor, not a charted or legally permitted route.
"""
from functools import lru_cache
import heapq
import json
import math
from pathlib import Path
import uuid

from .routing import Zone, contains, lerp, route_time, segment_parts, segment_time

ORIGIN = (-120.7, 33.85)
COS = math.cos(math.radians(34.2))
SEPARATION_KM = .65


def project(p):
    return (6371 * math.radians(p[0] - ORIGIN[0]) * COS,
            6371 * math.radians(p[1] - ORIGIN[1]))


def unproject(p):
    return (ORIGIN[0] + math.degrees(p[0] / (6371 * COS)),
            ORIGIN[1] + math.degrees(p[1] / 6371))


def circle(name, point, radius, speed=None):
    return Zone(name, tuple((point[0]+radius*math.cos(i*math.tau/16),
                             point[1]+radius*math.sin(i*math.tau/16)) for i in range(16)), speed)


def nearest_on_route(p, path):
    best, along, travelled = (math.inf, None), 0, 0
    for a,b in zip(path,path[1:]):
        length=math.dist(a,b)
        if length < 1e-9:
            continue
        t=max(0,min(1,sum((p[i]-a[i])*(b[i]-a[i]) for i in (0,1))/length**2))
        d=math.dist(p,lerp(a,b,t))
        if d<best[0]:
            best=(d,travelled+t*length)
        travelled+=length
    return best


def at_distance(path, distance):
    for i,(a,b) in enumerate(zip(path,path[1:])):
        length=math.dist(a,b)
        if distance<=length:
            p=lerp(a,b,distance/length if length else 0)
            return p,[p]+list(path[i+1:])
        distance-=length
    return path[-1],[path[-1]]


def move_route(path, hours, zones, speed):
    """Pure motion proposal, splitting at every zone boundary and waypoint."""
    path=list(path)
    left=hours
    while left>1e-12 and len(path)>1:
        parts=segment_parts(path[0],path[1],zones,service_speed=speed)
        if parts is None:
            return path, hours-left
        reached=True
        for p,q,v in parts:
            duration=math.dist(p,q)/v
            if duration<=left+1e-12:
                left=max(0,left-duration)
            else:
                path[0]=lerp(p,q,left/duration)
                left=0
                reached=False
                break
        if reached:
            path.pop(0)
    return path,hours-left


def swept_distance(a,b,c,d):
    """Minimum separation over simultaneous linear moves, not endpoint checks."""
    r=(a[0]-c[0],a[1]-c[1])
    v=((b[0]-a[0])-(d[0]-c[0]),(b[1]-a[1])-(d[1]-c[1]))
    vv=sum(x*x for x in v)
    t=max(0,min(1,-sum(r[i]*v[i] for i in (0,1))/vv)) if vv else 0
    return math.hypot(r[0]+t*v[0],r[1]+t*v[1])


class RegionalSimulation:
    def __init__(self):
        self.run_id=uuid.uuid4().hex
        self.data=json.loads((Path(__file__).parent/'data'/'santa_barbara.json').read_text())
        self.land=[Zone('Land · Natural Earth',tuple(project(p) for p in ring)) for ring in self.data['land']]
        self.land_boxes=[(min(p[0] for p in z.polygon),max(p[0] for p in z.polygon),
                          min(p[1] for p in z.polygon),max(p[1] for p in z.polygon)) for z in self.land]
        self.track=next(t for t in self.data['tracks'] if t['recorded_name']=='VENUS HORIZON')
        self.original=[project(p['coordinate']) for p in self.track['points']]
        if not all(self.clear(a,b) for a,b in zip(self.original,self.original[1:])):
            raise ValueError('Recorded route intersects the bundled land mask; select another track')
        self.route=list(self.original)
        self.planned_route=list(self.route)
        self.old_route=[]
        self.start,self.destination=self.route[0],self.route[-1]
        self.position=self.start
        self.zones=[]
        self.elapsed=0
        self.event_at=24/60
        self.event_done=False
        self.status='navigating'
        self.control='following recorded route'
        self.speed=24.0
        self.advisories=[]
        self.impact=None
        self.hold_hours=0
        self.min_separation=math.inf
        self.last_traffic_plan=-1
        self.holding=False
        self.baseline=route_time(self.route,[])
        self.nodes=self.build_nodes()
        # Place the traffic instances along recorded geometry with fictional offsets.
        self.traffic=[]
        self.live_vessels=[]
        for i,(offset,speed) in enumerate([(4,14),(19,21),(37,19),(58,22)]):
            position,route=at_distance(self.original,offset)
            self.traffic.append({'id':f'SIM-{i+2:02}', 'route':route,'position':position,
                                 'speed':speed,'active':True})
        candidates=[]
        for record in self.data['whale_sightings']:
            p=project(record['coordinate'])
            dist,along=nearest_on_route(p,self.original)
            if dist<4 and along is not None and 11<along<40:
                candidates.append((abs(along-17)+dist,record,p))
        if not candidates:
            raise ValueError('No historical sighting suitable for the regional demonstration')
        _,self.event_record,self.event_point=min(candidates,key=lambda row:row[0])
        self.announce('Santa Barbara simulation underway. WG-01 follows archived coordinates from VENUS HORIZON, recorded January 1, 2024. Four other ships are simulated. Automatic whale-zone routing and traffic monitoring are active.')

    @lru_cache(maxsize=40000)
    def clear(self,a,b):
        x0,x1=sorted((a[0],b[0]));y0,y1=sorted((a[1],b[1]))
        nearby=[z for z,(lx0,lx1,ly0,ly1) in zip(self.land,self.land_boxes)
                if x1>=lx0 and x0<=lx1 and y1>=ly0 and y0<=ly1]
        return math.isfinite(segment_time(a,b,nearby))

    def announce(self,text):
        self.advisories.append({'id':len(self.advisories)+1,'minute':self.elapsed*60,'text':text})

    def build_nodes(self):
        length=sum(math.dist(a,b) for a,b in zip(self.original,self.original[1:]))
        nodes=[]
        for station in range(math.ceil(length/1.5)+1):
            p,remaining=at_distance(self.original,min(length,station*1.5))
            q=remaining[1] if len(remaining)>1 else self.original[-2]
            dx,dy=q[0]-p[0],q[1]-p[1]
            norm=math.hypot(dx,dy) or 1
            for offset in range(-4,5):
                n=(p[0]-dy/norm*offset,p[1]+dx/norm*offset)
                if not any(contains(n,z.polygon) for z in self.land):
                    nodes.append((station,offset,n))
        return nodes

    def plan(self,traffic_zones=()):
        """Dijkstra on forward station/lateral corridor graph; costs split at zones."""
        zones=self.zones+list(traffic_zones)
        nodes={i:p for i,(_,_,p) in enumerate(self.nodes)}
        start,goal=len(nodes),len(nodes)+1
        nodes[start],nodes[goal]=self.position,self.destination
        lookup={(s,o):i for i,(s,o,p) in enumerate(self.nodes)}
        connections={i:[] for i in nodes}
        for i,(station,offset,p) in enumerate(self.nodes):
            for step in (1,2):
                for lateral in range(-2,3):
                    j=lookup.get((station+step,offset+lateral))
                    if j is not None:
                        connections[i].append(j)
            if math.dist(p,self.destination)<4:
                connections[i].append(goal)
        closest=sorted(range(start),key=lambda i:math.dist(self.position,nodes[i]))[:24]
        connections[start]=closest+[goal] if math.dist(self.position,self.destination)<4 else closest
        distance,previous,queue={start:0}, {},[(0,start)]
        while queue:
            cost,i=heapq.heappop(queue)
            if cost>distance[i]:
                continue
            if i==goal:
                path=[nodes[i]]
                while i!=start:
                    i=previous[i];path.append(nodes[i])
                return path[::-1]
            for j in connections[i]:
                a,b=nodes[i],nodes[j]
                if not self.clear(a,b):
                    continue
                dt=segment_time(a,b,zones)
                candidate=cost+dt
                if candidate<distance.get(j,math.inf):
                    distance[j],previous[j]=candidate,i
                    heapq.heappush(queue,(candidate,j))
        raise ValueError('No route in the demonstration corridor')

    def replan(self,reason,traffic_zones=(),before_override=None):
        before=route_time(self.route,self.zones) if before_override is None else before_override
        try:
            new=self.plan(traffic_zones)
        except ValueError:
            was_holding=self.holding
            self.control='holding: no route in corridor'
            self.holding=True
            if not was_holding:
                self.announce('Simulation advisory. No route is available in the demonstration corridor. Holding position.')
            return
        after=route_time(new,self.zones)
        self.old_route=list(self.route)
        self.route=new
        self.planned_route=list(new)
        self.holding=False
        self.control=reason
        delta=(after-before)*60
        self.impact={'arrival_change_min':delta,'remaining_min':after*60,'reason':reason}
        self.announce(f'Simulation advisory. {reason}. Route updated from the current simulated ship position. Arrival estimate changes by {abs(delta):.1f} simulated minutes {"later" if delta>=0 else "earlier"}. Traffic holds may add further time.')

    def introduce_whales(self):
        self.event_done=True
        before=route_time(self.route,self.zones)
        self.zones.append(circle('Historical sighting · assumed slow zone',self.event_point,3,6))
        old_under_new=route_time(self.route,self.zones)
        self.replan('Historical whale report introduced as a simulated 3 km buffer, limited to 6 km/h',before_override=before)
        if self.impact:
            after=route_time(self.route,self.zones)
            self.impact['arrival_change_min']=(after-before)*60
            self.impact['saved_min']=(old_under_new-after)*60
            self.announce(f'Whale-zone impact: arrival is {(after-before)*60:.1f} simulated minutes later than the previous estimate. The selected route saves {(old_under_new-after)*60:.1f} simulated minutes versus continuing on the old route under the new limit. The underlying sighting dates to {self.event_record["observed"][:10]}; this is a staged event, not a current detection.')

    def step(self,hours):
        if self.holding and self.elapsed-self.last_traffic_plan>1/60:
            self.last_traffic_plan=self.elapsed
            nearby=[circle('Live AIS report · simulation buffer',tuple(v['position']),1.3)
                    for v in self.live_vessels if math.dist(self.position,v['position'])<5]
            self.replan('Traffic updated; retrying the simulated course',nearby)
        # Predict constant motion for the next eight minutes to detect overtaking.
        if self.elapsed-self.last_traffic_plan>10/60 and not self.holding:
            future,_=move_route(self.route,8/60,self.zones,self.speed)
            threats=[]
            for ship in self.traffic:
                if not ship['active']:
                    continue
                forecast,_=move_route(ship['route'],8/60,self.zones,ship['speed'])
                if swept_distance(self.position,future[0],ship['position'],forecast[0])<1.2:
                    threats.append(circle(ship['id']+' simulated traffic buffer',ship['position'],1.3))
            for report in self.live_vessels:
                p=tuple(report['position'])
                if swept_distance(self.position,future[0],p,p)<1.2:
                    threats.append(circle('Live AIS report · simulation buffer',p,1.3))
            if threats:
                self.last_traffic_plan=self.elapsed
                self.replan('Predicted simulated traffic encounter; choosing a passing route',threats)
        proposals=[]
        for ship in self.traffic:
            proposal,_=move_route(ship['route'],hours,self.zones,ship['speed'])
            proposals.append(proposal)
        own,used=move_route(self.route,hours,self.zones,self.speed) if not self.holding else (self.route,0)
        # Live reports remain fixed at their last received locations; they are
        # distinct from simulated traffic and are never accelerated/extrapolated.
        live_hold=any(swept_distance(self.position,own[0],tuple(v['position']),tuple(v['position']))<SEPARATION_KM
                      for v in self.live_vessels)
        if live_hold:
            own=list(self.route)
        # Resolve moving traffic conflicts before committing any movement. Iterate
        # to account for a stopped vessel changing another vessel's swept path.
        old=[self.position]+[s['position'] for s in self.traffic]
        paths=[own]+proposals
        active=[True]+[s['active'] for s in self.traffic]
        stopped=set()
        for _ in range(len(paths)*2):
            changed=False
            for i in range(len(paths)):
                for j in range(i+1,len(paths)):
                    if not active[i] or not active[j]:
                        continue
                    if swept_distance(old[i],paths[i][0],old[j],paths[j][0])<SEPARATION_KM:
                        # Controlled ship yields first. Traffic priority is stable.
                        loser=i if i==0 else j
                        if loser not in stopped:
                            stopped.add(loser);paths[loser]=[old[loser]];changed=True
                        else:
                            other=j if loser==i else i
                            if other not in stopped:
                                stopped.add(other);paths[other]=[old[other]];changed=True
            if not changed:
                break
        for i in range(len(paths)):
            for j in range(i+1,len(paths)):
                if active[i] and active[j]:
                    self.min_separation=min(self.min_separation,swept_distance(old[i],paths[i][0],old[j],paths[j][0]))
        if live_hold:
            stopped.add(0)
        if 0 not in stopped and not self.holding:
            self.route=own
            self.position=own[0]
            if self.control=='yielding to simulated traffic':
                self.control='traffic clear; resuming course'
        elif 0 in stopped:
            self.hold_hours+=hours
            if self.control!='yielding to simulated traffic':
                self.announce('Simulation advisory. Yielding to another simulated vessel to preserve the demonstration separation buffer. Navigation resumes automatically when clear.')
            self.control='yielding to simulated traffic'
        for i,ship in enumerate(self.traffic,1):
            if i not in stopped:
                ship['route']=proposals[i-1];ship['position']=proposals[i-1][0]
                ship['active']=len(ship['route'])>1
        self.elapsed+=used if len(self.route)==1 and 0 not in stopped else hours
        if len(self.route)==1:
            self.status='arrived'
            self.announce(f'Simulated voyage complete. Arrival after {self.elapsed*60:.1f} simulated minutes, including {self.hold_hours*60:.1f} minutes yielding to traffic.')

    def advance(self,hours):
        while hours>1e-12 and self.status=='navigating':
            if not self.event_done and self.elapsed>=self.event_at-1e-12:
                self.introduce_whales()
            dt=min(hours,5/3600)
            if not self.event_done:
                dt=min(dt,self.event_at-self.elapsed)
            self.step(dt)
            hours-=dt

    def snapshot(self):
        return {'mode':'santa-barbara','run_id':self.run_id,'position':self.position,
                'start':self.start,'destination':self.destination,'route':self.route,
                'planned_route':self.planned_route,'old_route':self.old_route,
                'elapsed_min':self.elapsed*60,'remaining_min':route_time(self.route,self.zones)*60,
                'status':self.status,'control':self.control,'event_done':self.event_done,
                'zones':[{'name':z.name,'polygon':z.polygon,'speed':z.speed} for z in self.zones],
                'advisories':self.advisories,'impact':self.impact,'hold_min':self.hold_hours*60,
                'min_separation_km':self.min_separation if math.isfinite(self.min_separation) else None,
                'traffic':[{'id':s['id'],'position':s['position'],'speed_kmh':s['speed'],'active':s['active']} for s in self.traffic],
                'event_record':self.event_record,'separation_km':SEPARATION_KM}

    def geography(self):
        return {'land':[[project(p) for p in ring] for ring in self.data['land']],
                'tracks':[{'name':t['recorded_name'],'date':t['date'],'coordinates':[project(p['coordinate']) for p in t['points']]} for t in self.data['tracks']],
                'primary_track':self.track,'primary_route':self.original,
                'sightings':[dict(r,position=project(r['coordinate'])) for r in self.data['whale_sightings']],
                'lanes':[{'kind':f['properties']['INFORM'].strip() or 'TSS separation / reference',
                          'polygon':[project(p) for p in f['geometry']['coordinates'][0]]} for f in self.data['shipping_lanes']['features']],
                'sources':self.data['sources'], 'bbox':self.data['bbox'],
                'places':[{'name':name,'position':project((lon,lat))} for name,lon,lat in [
                    ('Santa Barbara',-119.6982,34.4208),('Ventura',-119.2945,34.2805),
                    ('Point Conception',-120.471,34.45),('Santa Cruz Island',-119.74,34.0),
                    ('Santa Rosa Island',-120.10,33.97),('San Miguel Island',-120.36,34.03),
                    ('Anacapa',-119.37,34.015)]]}
