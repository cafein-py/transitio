# Quickstart

## The one-call pipeline

`transitio.fetch` turns an area of interest into everything a routing tool
needs — a cropped OpenStreetMap extract plus validated GTFS feeds:

```python
import transitio

result = transitio.fetch("Helsinki")
```

The area of interest can be a place name (geocoded via Nominatim), a shapely
geometry, a GeoDataFrame/GeoSeries, or a `(minx, miny, maxx, maxy)` bounding
box in WGS84. The pipeline:

1. downloads the smallest OpenStreetMap extract covering the area and crops
   it to the AOI geometry,
2. discovers every GTFS feed overlapping the area in the Mobility Database
   (official feeds first, then by spatial specificity),
3. downloads each feed, crops it to the AOI, and validates it with the
   canonical notice codes,
4. returns the artefact paths together with per-feed merged reports and a
   `(feed id, reason)` record for everything it skipped.

```python
result.osm_pbf     # pathlib.Path of the cropped .osm.pbf
result.feeds       # list of GTFS zip paths
result.reports     # per-feed merged validation reports (dicts)
result.repairs     # per-feed repair fix logs (empty without repair=True)
result.skipped     # [(feed id, reason), ...]
```

Useful options:

```python
result = transitio.fetch(
    "Helsinki",
    when="2026-09-01",        # feeds must serve this day
    modes=["rail", "tram"],   # keep only feeds serving these modes
    repair=True,              # apply the gtfstidy-contract repair
)
```

## Scenario feeds from a GeoPackage

Draw a planned network in any GIS tool, attach headway attributes, and
turn it into a validated GTFS feed:

```python
import transitio

transitio.build_feed(
    "planned_network.gpkg",
    "scenario.zip",
    routes_layer="routes",        # LineStrings with attributes:
    timezone="Europe/Helsinki",   #   mode, headway_min, speed_kmh, days...
)
```

Stops come from an optional point layer (`stops_layer=`) snapped to each
route, or are interpolated along the alignments; route geometries become
GTFS shapes, so travel distances survive into routing. The result feeds
straight into `cafein.TransportNetwork.from_gtfs`.

## Handing off to cafein or pyrosm

```python
net = result.to_cafein()      # routable cafein.TransportNetwork
osm = result.to_pyrosm()      # pyrosm.OSM reader over the extract
```

`to_cafein()` forwards keyword arguments to
`cafein.TransportNetwork.from_gtfs`, so e.g. `result.to_cafein(ultra=True)`
works as expected.

## Using the pieces separately

Every pipeline stage is a standalone function:

```python
db = transitio.MobilityDatabase()                 # catalog client

feeds = db.search_feeds(aoi=(24.6, 60.1, 25.2, 60.4))
dataset = db.dataset_for(feeds[0], when="2026-09-01")
path = db.download(dataset)                        # cached, checksum-verified
hosted = db.validation_report(dataset)             # canonical-validator report

pbf = transitio.fetch_pbf((24.6, 60.1, 25.2, 60.4))

validation = transitio.validate_feed(path)        # canonical notice codes
transitio.repair_feed(path, "repaired.zip")       # logged, conservative
transitio.crop_feed(path, "cropped.zip", aoi=(24.6, 60.1, 25.2, 60.4))

report = transitio.report.build_report(
    validation, hosted=hosted
)
print(transitio.report.render_markdown(report))
```

## Reading the validation report

Notices follow the canonical
[gtfs-validator](https://gtfs-validator.mobilitydata.org/) codes and
severities, so local and hosted results merge into one document. Each notice
group records whether it was seen locally, by the hosted validator, or both:

```python
for group in report["notices"]:
    print(group["code"], group["severity"], group["source"],
          group["totalNotices"])
```

`report["summary"]` carries the severity totals, the computed service window
(the actual calendar activity, not the published range), row counts and the
provenance block of the downloaded dataset.
