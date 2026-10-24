# Santa Barbara demonstration data

`santa_barbara.json` is a small, bundled snapshot, assembled October 24, 2026.
It is not a nautical chart or a complete scientific dataset.

- **Recorded ship paths:** NOAA/USCG Marine Cadastre AIS, January 1, 2024,
  from `https://noaaocm.blob.core.windows.net/ais/csv2/csv2024/ais-2024-01-01.csv.zst`.
  A partial-day archive stream was decoded; only complete CSV records were kept.
  Cargo/tanker reports within the regional bounding box were filtered, grouped by
  MMSI, ordered by source timestamp, and sampled approximately every 400 m.
  Nine vessel tracks are included. Each track retains sampled source timestamps
  and coordinates, original name, MMSI, and raw record count. These are segments
  of recorded passages, not declared voyage plans or real-time vessel positions.
  The main passage, VENUS HORIZON, has 267 raw positions, from 00:00:05 to 05:32:34
  UTC. Some received AIS data can contain errors; no interpolation is claimed as
  an additional observed report.
- **Land:** Natural Earth 1:10m land, public domain, clipped to the bounding box.
  Coordinates are rounded to five decimals. Five polygons / 312 vertices. It is
  generalized geography, unsuitable for vessel navigation or clearance estimates.
  Source: https://www.naturalearthdata.com/downloads/10m-physical-vectors/
- **Shipping-lane reference:** NOAA CINMS ArcGIS item
  `f83e0ee8e8eb445abd23bf49d0d6ec94`, Southern California Traffic Separation Scheme.
  Its published terms permit informational/visual use. These polygons are drawn
  only as visual reference. The simulation does not enforce TSS or COLREGS rules.
  https://www.arcgis.com/home/item.html?id=f83e0ee8e8eb445abd23bf49d0d6ec94
- **Historical whales:** 182 CC0 human-observation records from a 300-record
  regional GBIF search sample, restricted to humpback whales. Dates are 2025–2026.
  Publisher: Happywhale / OBIS-SEAMAP, dataset DOI
  https://doi.org/10.82144/4991ec34, obtained via GBIF API.
  Each retained occurrence includes a GBIF source URL, event date, location,
  coordinate uncertainty when provided, and license. Non-CC0 records were
  omitted. Counts reflect this convenience sample, not survey effort, population
  density, absence, or current whale presence.

Source queries:

```
https://api.gbif.org/v1/occurrence/search?scientificName=Megaptera%20novaeangliae&decimalLatitude=33.8,34.6&decimalLongitude=-120.7,-119.0&hasCoordinate=true&limit=300
https://services2.arcgis.com/C8EMgrsFcRFL6LrL/arcgis/rest/services/Traffic_Separation_Scheme_SB/FeatureServer/0/query?where=1%3D1&outFields=*&outSR=4326&f=geojson
https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_land.geojson
```

The ship simulation reuses archived geometry, with fictional speeds, launch
offsets, and navigation decisions. The four traffic ships are independent
simulated instances along the primary recorded passage; they do not reproduce
simultaneous recorded traffic. Historical sighting dates differ from AIS dates.
The introduced whale zone is explicitly staged at simulated minute 24.

Preparation utilities live in `scripts/`. They require original downloaded
sources; raw country-wide datasets are intentionally not bundled. Runtime uses
only the small checked-in JSON file and requires no data download.
