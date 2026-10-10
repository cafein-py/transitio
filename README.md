# transitio: Transit feeds in and out

**Find the right public transport feeds for any city, validated and ready for
editing.**

Give transitio a city, and it looks up the GTFS feeds that serve it in its
feed index, a catalogue of the world's public transport feeds and the places
they serve. It recommends which feeds to use and says why it leaves out the
others, then downloads them, crops them to the city and validates them. It
also downloads the OpenStreetMap data to route on. The feeds and the extract
go straight to [cafein](https://github.com/cafein-py/cafein) for routing, and
the extract opens in [pyrosm](https://github.com/HTenkanen/pyrosm) for the
street network.

**Status:** early development.

## Installation

```
pip install transitio
```

Wheels cover Linux, macOS and Windows; building from source needs a Rust
toolchain (`pip install .`). `pip install "transitio[notebook]"` also
installs ipywidgets, so the download progress bars show as widgets in
Jupyter. `result.to_cafein()` needs [cafein](https://github.com/cafein-py/cafein)
as well (`pip install cafein`).

## Example

```python
import transitio

transitio.index.refresh()             # once: install the feed index

turku = transitio.place("Turku")
rec = turku.recommend()               # which feeds to use, and why
result = transitio.fetch(feeds=rec)   # download, crop and validate

result.paths                          # the feed files
result.osm_pbf                        # the OpenStreetMap extract
network = result.to_cafein()          # a network to route on (needs cafein)
```

## What transitio does

- **Finds places and their feeds.** Look up a city, metro area, region or
  country by name and list the feeds that serve it, local to international
  ([Finding places](https://transitio.readthedocs.io/en/latest/finding_places.html)).
- **Chooses the feeds to use.** `recommend()` takes the feeds that carry a
  place's service and says why it leaves the others out
  ([Choosing feeds](https://transitio.readthedocs.io/en/latest/choosing_feeds.html)).
- **Fetches the data.** Download the feeds for a place, a box or any polygon,
  cropped to it, and an OpenStreetMap extract of the area
  ([Fetching data](https://transitio.readthedocs.io/en/latest/fetching_data.html)).
- **Reuses its downloads.** Every download is kept in a cache, so running the
  same analysis again delivers the same feeds, offline too
  ([The download cache](https://transitio.readthedocs.io/en/latest/download_cache.html)).
- **Checks, repairs and edits feeds.** Validate any GTFS feed with the notice
  codes of the canonical GTFS validator, repair the defects that can be fixed
  without changing the trips riders see, and edit a feed with undo and redo
  ([Working with a GTFS feed](https://transitio.readthedocs.io/en/latest/working_with_feeds.html)).
- **Crops and merges feeds.** Cut a feed to an area or a date range, merge
  several feeds into one, or replace a feed's broken trips with those of
  another
  ([Cropping and merging feeds](https://transitio.readthedocs.io/en/latest/cropping_and_merging.html)).
- **Builds scenario feeds.** Turn routes drawn in a GIS tool, with their
  headways, into a GTFS feed
  ([Building scenario feeds](https://transitio.readthedocs.io/en/latest/building_feeds.html)).
- **Searches the catalogues.** Query the Mobility Database and download
  OpenStreetMap extracts directly
  ([Catalogues and OSM extracts](https://transitio.readthedocs.io/en/latest/catalogues.html)).
- **Draws missing route shapes.** `infer_shapes` draws the shapes a feed
  lacks from OpenStreetMap (below).

## Drawing missing route shapes

Many feeds have no `shapes.txt`, so their routes show as straight lines
between stops. `infer_shapes` draws the shapes from an OpenStreetMap extract:
it follows the OpenStreetMap route relations where they exist, and matches
each route's stops to the tram, rail or road network where they do not.

```python
report = transitio.infer_shapes(
    "feed.zip", "shaped.zip", pbf, strictness="strict"
)
report["written"]     # shapes written
report["shapes"]      # per shape: method, matched OSM relation, score
report["skipped"]     # per refused pattern: the step that refused it
```

`strictness` sets how much guessing to accept: `"strict"` (the default)
writes only unambiguous matches, while `"relaxed"` and `"permissive"` write
more shapes with less certainty. Before a shape is written, the pattern's
own stops must lie along it in order. With the shapes of Helsinki's tram
feed withheld, the three levels gave (`scripts/validate_shapes.py`):

| level | shapes written | median length error | worst offset |
| ----- | -------------- | ------------------- | ------------ |
| strict | 35/80 | 0.9% | 42 m |
| relaxed | 40/80 | 0.9% | 34 m |
| permissive | 43/80 | 0.9% | 184 m |

Helsinki's OpenStreetMap data is unusually complete; elsewhere, expect fewer
and less exact shapes.

## Documentation

The documentation, with the Quickstart, the tutorials and the API reference,
is at https://transitio.readthedocs.io. To build it locally, install
transitio and the Sphinx toolchain:

```
pip install . -r docs/requirements.txt
sphinx-build -b html docs docs/_build/html
```

The feed index is built in a separate repository,
[transitio-dev/transitio-index](https://github.com/transitio-dev/transitio-index).

## License

MIT
