# transitio

AOI-driven OSM and GTFS acquisition, validation and repair — companion to
[pyrosm](https://github.com/HTenkanen/pyrosm) and cafein. transitio moves the
raw ingredients of routing — OSM extracts and GTFS timetables — from the open
data ecosystem to your area of interest, validated and repaired, ready for
cafein to brew into routing results.

**Status: early development.** Acquisition (Mobility Database catalog + OSM
extracts), GTFS validation, repair and cropping are in place, tied together by
the one-call `transitio.fetch` pipeline.

## Quick example

```python
import transitio

# One call: OSM extract + validated GTFS feeds for an area of interest.
result = transitio.fetch(helsinki_polygon)        # any shapely geometry,
                                                   # bbox tuple or place name
result.osm_pbf     # cropped OSM extract (path)
result.feeds       # downloaded, cropped and validated GTFS feeds (paths)
result.reports     # per-feed merged validation reports
result.skipped     # (feed id, reason) for anything left out

net = result.to_cafein()   # routable cafein.TransportNetwork
osm = result.to_pyrosm()   # pyrosm.OSM reader over the extract
```

`fetch` accepts `when="2026-09-01"` to pick the dataset versions covering a
service day (needs a free Mobility Database API token, passed as
`refresh_token=` or via the `MOBILITY_API_REFRESH_TOKEN` environment
variable), `modes=["rail", "tram"]` to keep only feeds serving given modes,
and `repair=True` to repair feeds before use. With a token, GTFS downloads
are catalogued dataset versions verified against catalog checksums; without
one, the latest hosted zips are fetched as-is — unverified moving targets.

### Inferring missing route shapes

Many feeds ship without `shapes.txt`, leaving every consumer to draw
straight lines between stops. `infer_shapes` fills that gap from an OSM
extract — matching OSM route relations where they exist, map matching
over tram, rail and bus-drivable networks where they do not — and
writes a feed carrying real alignments:

```python
report = transitio.infer_shapes(
    "feed.zip", "shaped.zip", pbf, strictness="strict"
)
report["written"]     # shapes written
report["shapes"]      # per shape: method, matched OSM relation, score
report["skipped"]     # per refused pattern: the stage that refused it
```

How much inference is acceptable is yours to choose. `"strict"` (the
default) writes only unambiguous matches; `"relaxed"` and
`"permissive"` trade certainty for coverage, which is the trade worth
making where a feed has no shapes at all and the alternative is a
straight line. Every shape is validated against the pattern's own stops
before it is written — each stop must lie on the alignment, in order —
so no level writes a shape the feed's own data contradicts.

On the Helsinki tram fixture with the feed's shapes withheld, the
levels measured (`scripts/validate_shapes.py`):

| level | shapes written | median length error | worst offset |
| ----- | -------------- | ------------------- | ------------ |
| strict | 35/80 | 0.9% | 42 m |
| relaxed | 40/80 | 0.9% | 34 m |
| permissive | 43/80 | 0.9% | 184 m |

Helsinki's OSM data is unusually good; expect a worse trade where it is
not, and keep the report.

### Lower-level access

Each pipeline stage is available on its own:

```python
db = transitio.MobilityDatabase()

feeds = db.search_feeds(aoi=helsinki_polygon)
dataset = db.dataset_for(feeds[0], when="2026-09-01")
path = db.download(dataset)                        # cached, checksum-verified
report = db.validation_report(dataset)             # hosted canonical-validator report

pbf = transitio.fetch_pbf(helsinki_polygon)       # cropped OSM extract
validation = transitio.validate_feed(path)        # canonical-code notices
transitio.repair_feed(path, "repaired.zip")       # gtfstidy-contract repair
transitio.crop_feed(path, "cropped.zip", aoi=helsinki_polygon)
```

## Documentation

The Sphinx site lives in `docs/`. Building it needs transitio itself
installed (autodoc imports the real package) plus the Sphinx toolchain:

```
pip install . -r docs/requirements.txt
sphinx-build -b html docs docs/_build/html
```

The hosted version lives at https://transitio.readthedocs.io.

## Installation

```
pip install transitio
```

Binary wheels cover Linux, macOS and Windows. Building from source instead
requires a Rust toolchain (`pip install .`).

## License

MIT
