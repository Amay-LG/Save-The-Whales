"""Prepare small offline fixtures from downloaded public source files.

Run with Python 3.14+ after downloading sources listed in the regional README.
The AIS input may be a partial zstd stream: only complete CSV rows are retained,
and each emitted route records its own exact time coverage. No inferred points
are added. Runtime of WhaleGuard remains Python 3.10+ with no dependencies.
"""
import argparse
import csv
import json
import math
from pathlib import Path

BOX = (-120.7, 33.85, -119.0, 34.6)


def clip(poly):
    for axis, limit, sign in [(0, BOX[0], 1), (0, BOX[2], -1), (1, BOX[1], 1), (1, BOX[3], -1)]:
        out = []
        for a, b in zip(poly, poly[1:] + poly[:1]):
            ia, ib = sign * (a[axis] - limit) >= 0, sign * (b[axis] - limit) >= 0
            if ia:
                out.append(a)
            if ia != ib:
                t = (limit - a[axis]) / (b[axis] - a[axis])
                out.append([a[i] + t * (b[i] - a[i]) for i in (0, 1)])
        poly = out
        if not poly:
            break
    return poly


def distance(a, b):
    return math.hypot((a[0]-b[0])*92, (a[1]-b[1])*111)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prefix', default='/tmp/whaleguard')
    args = parser.parse_args()
    prefix = args.prefix
    target = Path('whaleguard/data')
    target.mkdir(exist_ok=True)
    # Extraction can be rerun separately with a downloaded AIS archive.
    raw = json.loads(Path(prefix+'-tracks.json').read_text())
    tracks=[]
    for mmsi, rows in raw.items():
        rows=sorted({r['time']:r for r in rows}.values(),key=lambda r:r['time'])
        selected=[]
        for r in rows:
            p=[r['lon'],r['lat']]
            if not selected or distance(selected[-1]['coordinate'],p)>=.4:
                selected.append({'coordinate':p,'time':r['time']+'Z','speed_kn':r['sog']})
        if rows and selected and selected[-1]['time'] != rows[-1]['time']+'Z':
            r=rows[-1]; selected.append({'coordinate':[r['lon'],r['lat']],'time':r['time']+'Z','speed_kn':r['sog']})
        if len(selected)>10:
            tracks.append({'mmsi':mmsi,'recorded_name':rows[0]['name'],'date':'2024-01-01',
                           'points':selected,'raw_point_count':len(rows)})
    land=[]
    source=json.loads(Path(prefix+'-land.geojson').read_text())
    for feature in source['features']:
        geometry=feature['geometry']
        polygons=geometry['coordinates'] if geometry['type']=='MultiPolygon' else [geometry['coordinates']]
        for polygon in polygons:
            ring=polygon[0]
            # Bounding-box test avoids clipping distant rings across the globe.
            if max(p[0] for p in ring)<BOX[0] or min(p[0] for p in ring)>BOX[2] or max(p[1] for p in ring)<BOX[1] or min(p[1] for p in ring)>BOX[3]:
                continue
            ring=clip(ring)
            if len(ring)>=3:
                # Preserve detailed geometry for collision checks; rounded only at ~1 m.
                land.append([[round(x,5),round(y,5)] for x,y in ring])
    whales=[]
    for r in json.loads(Path(prefix+'-whales.json').read_text())['results']:
        if '/zero/' not in r.get('license',''):
            continue  # Keep this tiny bundled sample entirely CC0.
        if r.get('basisOfRecord')!='HUMAN_OBSERVATION':
            continue
        whales.append({'id':str(r['key']), 'species':r['species'],
                       'coordinate':[r['decimalLongitude'],r['decimalLatitude']],
                       'observed':r.get('eventDate'), 'uncertainty_m':r.get('coordinateUncertaintyInMeters'),
                       'license':'CC0', 'source_url':f"https://www.gbif.org/occurrence/{r['key']}"})
    data={'bbox':BOX,'tracks':tracks,'land':land,'whale_sightings':whales,
          'shipping_lanes':json.loads(Path(prefix+'-tss.geojson').read_text()),
          'sources':[
              {'name':'NOAA / USCG Marine Cadastre AIS, 2024-01-01 (partial-day extraction)', 'url':'https://noaaocm.blob.core.windows.net/ais/csv2/csv2024/ais-2024-01-01.csv.zst'},
              {'name':'Natural Earth 1:10m land, public domain', 'url':'https://www.naturalearthdata.com/downloads/10m-physical-vectors/'},
              {'name':'NOAA CINMS Southern California TSS (visual reference)', 'url':'https://www.arcgis.com/home/item.html?id=f83e0ee8e8eb445abd23bf49d0d6ec94'},
              {'name':'Happywhale / OBIS-SEAMAP via GBIF, historical humpback observations (CC0 subset)', 'url':'https://doi.org/10.82144/4991ec34'}]}
    (target/'santa_barbara.json').write_text(json.dumps(data,separators=(',',':')))
    print('tracks',len(tracks),'land polygons',len(land),'land vertices',sum(map(len,land)),'CC0 sightings',len(whales))


if __name__=='__main__':
    main()
