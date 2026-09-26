# Quickstart

## The one-call pipeline

`transitio.fetch` turns an area of interest, or a place in the feed index,
into everything a routing tool needs — a cropped OpenStreetMap extract plus
validated GTFS feeds:

```python
import transitio

result = transitio.fetch("Helsinki")
```

The area of interest can be a place name (geocoded via Nominatim), a shapely
geometry, a GeoDataFrame/GeoSeries, or a `(minx, miny, maxx, maxy)` bounding
box in WGS84. The pipeline:

1. downloads the smallest OpenStreetMap extract covering the area and crops
   it to the AOI geometry (skipped with `osm=False`),
2. discovers every GTFS feed overlapping the area in the Mobility Database
   (official feeds first, then by spatial specificity) — or, for a place,
   takes the feeds the index lists for it,
3. downloads each feed, crops it to the area (its polygon, else its bounding
   box), repairs it when asked, and validates it with the canonical notice
   codes,
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
    repair=True,              # apply the gtfstidy-contract repair after the crop
    osm=False,                # skip the OSM extract; osm_pbf is then None
)
```

## Feeds for an indexed place

The feed index knows which feeds serve each place, and at which tier:
`local`, `regional`, `national` or `international` service there. Install
the newest snapshot once:

```python
import transitio

transitio.index.refresh()
```

`fetch(place=...)` then selects the place's feeds by tier and crops them to
the place's boundary:

```python
augsburg = transitio.place("Augsburg")
result = transitio.fetch(
    place=augsburg,
    tiers=["local", "regional"],   # or exclude=["national", "international"]
    osm=False,
)
transitio.merge_feeds(result.feeds, "augsburg.gtfs.zip", check=False)
```

A feed that serves the place with only some of its routes is cropped to the
routes of the requested tiers. When that selection cannot be trusted — its
evidence was missing when the index was built, or the download no longer
matches it — `on_untrusted_selector` decides: `"auto"` (the default) skips
the feed when `exclude` was given and otherwise delivers it whole, `"whole"`
always delivers it whole, `"drop"` skips it and `"error"` raises
`StaleSelectorError`. National feeds, such as Germany's, are streamed
through the crop, so they fit in memory bounded by the area.

`merge_feeds` writes one feed from the cropped ones. With `check=False` it
keeps the file when the validator reports ERROR notices; the returned
report lists them.

## Editing a feed in the GUI

```
pip install "transitio-editor[snap]"
transitio edit feed.zip --osm-pbf helsinki.osm.pbf
```

The editor lives in its own package,
[transitio-editor](https://github.com/cafein-py/transitio-editor); the
`transitio edit` command delegates to it.

The editor opens on `http://127.0.0.1:8300`: stops and shapes render on
a map where they can be added, moved and drawn — with the `--osm-pbf`
extract, drawn route shapes snap to the street network, Remix-style.
Saving runs the validator and reports the notice counts. The HTTP API
behind the interface publishes its schema at `/openapi.json`.

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
transitio.patch_feed(path, "sibling.zip", "patched.zip")  # heal broken trips

report = transitio.report.build_report(
    validation, hosted=hosted
)
print(transitio.report.render_markdown(report))
```

## The feed index

The index is a versioned snapshot published by
[transitio-index](https://github.com/transitio-dev/transitio-index).
`transitio.index.refresh()` installs the newest one this transitio reads;
`transitio.index.installed()` lists the installed snapshots, and
`transitio.index.use(snapshot_id)` (or the `TRANSITIO_INDEX_SNAPSHOT`
environment variable) pins one for the process. Queries read the pinned
snapshot, else the newest installed one.

### Finding a place

```python
import transitio

augsburg = transitio.place("Augsburg")
transitio.place("London, Ontario")   # a qualifier names the region or country
transitio.places("Augsburg")      # every match, ranked best first
transitio.suggest("augs")         # type-ahead over names, translations, aliases
```

A name can match several places. A city wins over the metros in its country
that carry its name, and over a same-named area containing it that runs much
the same service: "Augsburg" is the city, not its three metros, and
"Helsinki" the city, not the Helsinki sub-region. Other places sharing a name
are decided as before, by the sole exact match or a clear lead in feeds; where
neither holds, as for London in the UK and in Canada, or New York City and
New York State, `place` raises `AmbiguousPlaceError`. A qualifier after a
comma names the region or country that holds the place — `"London, Ontario"`,
`"London, Canada"`, `"City of London, UK"` — and is matched against the names,
translations and aliases of the place's region and country, so codes such as
`UK` or `USA` work; a name that itself contains a comma, such as an alias
`"Queen's Park, Greater London"`, still matches as written. `kind` (`"city"`,
`"metro"`, `"region"`, `"country"`) restricts the scope, and a Wikidata id or
the index's own id picks one place.

The index keys every place by its own id, a `tp_<n>` that never changes or
gets reused, and keeps the external ids the place carries beside it:

```python
helsinki = transitio.place("Q1757")   # by Wikidata id, own id or name
helsinki.id            # "tp_9307"
helsinki.wikidata_id   # "Q1757", or None for a place without a Wikidata item
helsinki.concordances  # {"wikidata": ["Q1757"], "overture": ["..."]}
helsinki.former_ids    # ids merged into this place; each still resolves to it
```

`transitio.place("Q1757")` resolves through the concordances, a QID merged
into another place included, and `transitio.place("tp_…")` resolves a former
id to its successor. An index published before schema 6 reads the same way:
its QID is both the id and the only concordance.

### Moving around the hierarchy

```python
augsburg.subtype       # the source's own level, e.g. "county"
augsburg.ancestors     # Bavaria (region), then Germany (country)
augsburg.parent        # Bavaria; .children goes the other way
augsburg.metros        # the metros it belongs to, one per metro definition
augsburg.delineations()  # every area the place is part of, with its relation
```

A city can belong to a metro of each of four definitions, told apart by
`subtype`: `metropolitan statistical area` (US Census), `metropolitan region`
(Eurostat's NUTS-3 approximation), `functional urban area` (the Eurostat
Urban Audit) and `city-region (FAO)` (worldwide). `delineations()` returns
them with the containing region and country, each as a `Delineation` of
`relation`, `kind`, `subtype` and `place`.

### A place's feeds

```python
for feed in augsburg.feeds(tiers=["local", "regional"]):
    feed.feed_id, feed.tiers, feed.relevance_category, feed.relevance
    feed.service_start, feed.service_end   # the feed's service window
    feed.has_shapes, feed.license, feed.realtime
```

`feeds()` without tiers returns the place kind's default view: a city's or a
metro's primary and secondary feeds (its local and regional service), a
region's secondary and tertiary ones, a country's tertiary ones. `exclude`
drops named tiers and `requires=["shapes.txt"]` keeps only feeds carrying
those files. `feed.realtime` lists the GTFS-realtime companions tied to a
static feed; the companions the index could not tie to one are in
`Index.realtime_unlinked()`.

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
