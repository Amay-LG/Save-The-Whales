"""Filter complete CSV rows from a NOAA AIS zstd download (Python 3.14+).

Usage: python3 scripts/extract_santa_barbara_ais.py archive.csv.zst output.json
A truncated input is accepted explicitly, preserving only complete decoded rows.
The next preparation step sorts/deduplicates each track by source timestamp.
"""
import argparse
import csv
import json
from collections import defaultdict
from compression import zstd
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive')
    parser.add_argument('output')
    args=parser.parse_args()
    tracks=defaultdict(list)
    count=0
    partial=False
    try:
        with zstd.open(args.archive,'rt',newline='') as stream:
            for row in csv.DictReader(stream):
                count+=1
                try:
                    lon,lat=float(row['longitude']),float(row['latitude'])
                    speed=float(row['sog'])
                    vessel_type=int(row['vessel_type'] or 0)
                    if -120.65<lon<-119.05 and 33.85<lat<34.55 and 5<speed<30 and 70<=vessel_type<=89:
                        tracks[row['mmsi']].append({'time':row['base_date_time'],'lon':lon,'lat':lat,
                                                   'sog':speed,'name':row['vessel_name'],'type':vessel_type})
                except (KeyError,TypeError,ValueError):
                    continue
    except (EOFError,zstd.ZstdError):
        partial=True
    Path(args.output).write_text(json.dumps(tracks))
    print(f'{count} complete rows read; {len(tracks)} regional cargo/tanker tracks; partial archive: {partial}')


if __name__=='__main__':
    main()
