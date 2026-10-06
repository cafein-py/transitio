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

1. downloads the smallest OpenStreetMap extract that contains the area, or
   a set of extracts smaller in total, merged into one (Geofabrik, BBBike or
   Movisda), and crops it to the area's bounding box (skipped with
   `osm=False`),
2. discovers every GTFS feed overlapping the area in the Mobility Database
   (official feeds first, then by spatial specificity) — or, for a place,
   takes the feeds the index lists for it,
3. downloads each feed, crops it to the area (its polygon, else its bounding
   box), repairs it when asked, and validates it with the canonical notice
   codes,
4. leaves out of each delivered feed the trips an earlier delivered feed
   also runs,
5. returns the artefact paths together with per-feed merged reports and a
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

Some feeds need a free account with their provider. `fetch` sends their
credentials when they are set, with `transitio.credentials.set`, an
environment variable or `credentials={"<provider>": {"<field>": "..."}}`.
Without them, it reads the feed from the Mobility Database's hosted copy
when there is one, and otherwise skips the feed; either way the record
gives a reason such as `"protected feed: credentials missing for
trafiklab"`, followed by the feed's `access_instructions()`: who issues
the credentials and where to register. When a download with credentials
fails, the hosted copy is read without them.

Feeds often publish the same trips: a city operator's buses also appear in
the regional and national feeds, under another agency name and with times a
minute apart. `fetch` delivers each such trip once. The feed earlier in the
selection record keeps it, for a place the order of `place.feeds()` (by
category, then relevance), and the later feed is delivered without it,
noted `"<n> repeated trips of <feed ids> left out"`. Trips are compared as
`merge_feeds` compares them: the same route name and type, and the same
stops and times, or nearly (within 50 m and 3 minutes). With `when`, the
trips of that day are compared. Without it, a trip is left out only when the
earlier feeds run it on every date it runs, which reads every feed's whole
calendar: for the nine feeds of the Munich metro area the comparison took
about 50 seconds, against about 15 seconds for one day, and memory peaked at
8 GB against 5 to 6 GB. A feed is left out only when every trip in scope,
with `when` that day's, repeats a trip of an earlier feed; a trip the
comparison cannot read, such as one whose calendar cannot be read, keeps its
feed. The delivered feeds are separate feeds, so no transfer or pathway
links two of them.

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

The crop keeps each trip that serves the area whole, with all its stops, so
some stops can lie beyond the area of the OSM extract (`result.osm_area`).
cafein may then find no walking network near such a stop and give it no
footpaths. A journey that starts and ends in the area is routed as before;
only a walking transfer at such a stop can be lost. The `stops_outside_osm` column of
`result.selection_table()` counts each delivered feed's located stops, those
with usable coordinates, outside `osm_area`, and the last row of the record
sums them. The count is geometric and can differ from the number cafein
reports without footpaths, either way: the extract spans the bounding box
of the area and cafein snaps a stop up to 1.6 km away, while a stop inside
the area can still lie far from any street or path.

## The download cache

Every feed `fetch` downloads is kept in the download cache, the `gtfs`
folder under `cache_dir` (by default the platform's cache directory), one
version per distinct archive, named by its SHA-256. A later call uses a
cached version that serves its study day instead of downloading the feed,
offline too, and a repeated request uses the version it used before, so a
repeated run delivers the same feeds and reports. A day no cached version
serves downloads the feed and keeps the older versions. The crops, repairs
and validation results made from a version are kept with it and read back
by a call that makes the same, as are the trips found repeating those of the
other feeds the call delivers.

```python
transitio.fetch(place="Helsinki", when="2026-09-01")   # downloads
result = transitio.fetch(place="Helsinki", when="2026-09-01")
result.selection_table()["cache"]                       # "reused" per feed

transitio.fetch(place="Helsinki", when="2026-09-01", use_cache=False)
```

`use_cache=False` downloads every feed again and replaces its cached
versions; when every attempt for a feed fails, the version a cached call
would use is delivered with a warning. A warm cache can decide differently
from a cold one: a cached version that serves the day is used without the
probe `expired="skip"` sends, and a cached feed counts as unchanged since
indexed for `contained="drop"` only when that was proven under the index
snapshot in use. For a feed that needs an account, credentials for its
provider count alike whichever key they hold, and a call without them uses
only copies fetched without them.

With `directory=` the delivered feeds are copied there; without it they are
the files in the cache, read-only on Linux and macOS.

```python
import datetime

transitio.cache.info()        # one row per cached version, with its sizes
transitio.cache.clear(older_than=datetime.timedelta(days=30))  # unused 30 days
transitio.cache.clear()       # everything
```

`clear` waits for a fetch working on a feed; a feed a running fetch adds
after `clear` started is left for the next call.

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
"Helsinki" the city, not the Helsinki sub-region. It also wins over the areas
in its country that list its name only as an alias: "Taipei" is the city, not
New Taipei or Taiwan. Where no city carries a name, a region or country of
that name takes the city's place: "Istanbul" is the province, not its metro.
Names count in a place's own languages, those of its country, and in English:
"München" is Munich, and a place abroad that carries "Buenos Aires" only as a
label in another language is no rival to the Argentine capital. A label in
another language still counts for a place known far more widely, so "Meksyk",
Polish for Mexico, stays ambiguous rather than naming a place in Poland.
Where a place carries a name as its name or in English, a place carrying it
only in another of its own languages does not compete either, unless known
far more widely: "Bergen", Dutch for Mons in Belgium, is no rival to the
towns named Bergen.
Before any of these, in an index recording populations (schema 11), a city of
at least 200,000 people carrying a name as its name or in English wins when it
has more than twice the population of every other place of that name recording
one, whatever their feeds or labels: "Lima" is Lima, Peru, not Lima, Ohio,
while "Meksyk", which Mexico City carries only in Polish, still stays
ambiguous. It does not where a region or country of the name contains no city
of the name ("Victoria": the city in British Columbia and the Australian
state), or where a city of the name without a recorded population is known at
least as widely, as San Jose, California, a city of the San Francisco urban
centre, is.
Other places sharing a name
are decided by the sole exact match or a clear lead in feeds. Feed counts
reflect how well each country's feeds are catalogued, so a lead in feeds does
not decide against a place of that name abroad whose name is recorded in far
more languages. Where feeds do not decide, a place of the name recorded in far
more languages than every other, at home or abroad, wins: "Moscow" is Moscow,
Russia, however many more feeds Moscow, Idaho, has, and "Cali, Colombia" is
the city rather than a lesser-known place there. Where nothing decides, as
for Springfield or Victoria,
`place` raises `AmbiguousPlaceError`. Only a full name resolves: a partial
one such as `"Augs"` raises `PlaceNotFoundError`, whose `candidates` hold the
places it partly matches, and `suggest` completes it. A qualifier after a
comma names the region or country that holds the place — `"London, Ontario"`,
`"London, Canada"`, `"City of London, UK"` — and is matched against the names,
translations and aliases of the place's region and country, so codes such as
`UK` or `USA` work. A town or other containing place counts only when no
region or country holds a place of the name, so `"Copenhagen, Denmark"` is in
Denmark, not in the town of Denmark, New York. A name that itself contains a
comma, such as an alias `"Queen's Park, Greater London"`, still matches as
written; a label in another language equal to the whole query answers only
when the qualified name names no single place, so `"Halifax, Canada"` is the
city, not the region labelled "Halifax (Canadà)" in Piedmontese. `kind` (`"city"`,
`"metro"`, `"region"`, `"country"`) restricts the scope, and a Wikidata id or
the index's own id picks one place. The metro definitions below each name a
metro after its core city; one metro under several definitions (they share
member places) answers with the first of `functional urban area`,
`metropolitan statistical area`, `metropolitan region` and
`city-region (FAO)`, so `transitio.place("Stockholm", kind="metro")` is its
functional urban area, and `definition="metropolitan region"` picks another
definition. A city's own metros, those in its country, keep every
definition, so that dropping the others cannot hand the city's name to one
of them; `"Cambridge, United Kingdom"`, with no city of the name in the UK,
is its metropolitan region. A US
metropolitan statistical area is named after its principal cities, so a
city's name matches it only in part; the city's `metros` lists it.

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
region's secondary and tertiary ones, a country's tertiary ones. A region or
country of at most 1,000 km², such as Monaco or San Juan, is town-sized and
keeps its primary, secondary and tertiary feeds. When `fetch(place=...)`
without tiers finds the default view empty while the place has feeds, an
entry of the selection record names them and the `tiers` that fetch them,
and a warning repeats it. `exclude` drops named tiers and
`requires=["shapes.txt"]` keeps only feeds carrying those files.
`feed.realtime` lists the GTFS-realtime companions tied to a static feed;
the companions the index could not tie to one are in
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
