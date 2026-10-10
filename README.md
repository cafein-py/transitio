# transitio: Transit feeds in and out, ready for routing

**Find the right public transport feeds for any city, validated and ready for
routing or editing.**

`transitio` is a Python library that helps you to find public transport feeds
serving a given city and prepares them for routing. `transitio`'s feed index
catalogues the world's public transport feeds in GTFS format and the places
they serve (currently listing approximately 150,000 places). The General
Transit Feed Specification (GTFS) is the standard format for public transport
timetables, used by thousands of transport authorities across the world. With
`transitio`, you can get recommendations on which feeds to use for a given
city (and time) and an explanation of why certain feeds should be left out.
`transitio` then helps you to download the recommended feeds, crop them to the
given area and validate them to avoid using broken or defective feeds. With
`transitio`, you can also download
[OpenStreetMap](https://www.openstreetmap.org/) data for the same area (using
[pyrosm](https://github.com/HTenkanen/pyrosm) under the hood) if you want to
do multimodal routing that combines public transport and walking.

**Status:** early development.

## Installation

```
pip install transitio
```

Prebuilt Python packages (wheels) are available for Linux, macOS and Windows.
Building from source with `pip install .` requires a Rust toolchain. To show
the download progress bars as widgets in Jupyter, install ipywidgets with
`pip install "transitio[notebook]"`. `result.to_cafein()` also requires
[cafein](https://github.com/cafein-py/cafein); install it with
`pip install cafein`.

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

## What can you do with transitio?

- **Find places and their feeds.** Look up a city, metro area, region or
  country by name and list the feeds that serve it, from local to
  international
  ([Finding places](https://transitio.readthedocs.io/en/latest/finding_places.html)).
- **Choose the feeds to use.** `recommend()` considers the feeds that serve a
  place, selects which to use and explains why it leaves the others out
  ([Choosing feeds](https://transitio.readthedocs.io/en/latest/choosing_feeds.html)).
- **Fetch the data.** Download the feeds for a place, a box or any polygon
  and crop them to that area. Also download an OpenStreetMap extract of the
  area
  ([Fetching data](https://transitio.readthedocs.io/en/latest/fetching_data.html)).
- **Reuse downloads.** Every feed that `fetch` downloads is kept in a
  cache. This lets the same analysis use the same feeds again, even offline
  ([The download cache](https://transitio.readthedocs.io/en/latest/download_cache.html)).
- **Check, repair and edit feeds.** Validate any GTFS feed with the notice
  codes of the canonical GTFS validator. Repair defects that can be fixed
  without changing the trips riders see. Edit a feed with undo and redo
  ([Working with a GTFS feed](https://transitio.readthedocs.io/en/latest/working_with_feeds.html)).
- **Crop and merge feeds.** Cut a feed to an area or a date range, merge
  several feeds into one, or replace a feed's broken trips with those of
  another
  ([Cropping and merging feeds](https://transitio.readthedocs.io/en/latest/cropping_and_merging.html)).
- **Build scenario feeds.** Turn routes drawn in a GIS tool and their
  headways (how often they run) into a GTFS feed
  ([Building scenario feeds](https://transitio.readthedocs.io/en/latest/building_feeds.html)).
- **Search the catalogues.** Query the Mobility Database and download
  OpenStreetMap extracts directly
  ([Catalogues and OSM extracts](https://transitio.readthedocs.io/en/latest/catalogues.html)).
- **Draw missing route shapes.** `infer_shapes` draws the shapes a feed
  lacks from OpenStreetMap (below).

## Drawing missing route shapes

Many feeds do not have `shapes.txt`, so their routes appear as straight lines
between stops. `infer_shapes` draws route shapes from an OpenStreetMap
extract. It follows OpenStreetMap route relations where they exist.
Otherwise, it matches each route's stops to the tram, rail or road network.

```python
report = transitio.infer_shapes(
    "feed.zip", "shaped.zip", pbf, strictness="strict"
)
report["written"]     # shapes written
report["shapes"]      # per shape: method, matched OSM relation, score
report["skipped"]     # per refused pattern: the step that refused it
```

`strictness` sets how much guessing to accept. `"strict"` (the default)
writes only unambiguous matches. `"relaxed"` and `"permissive"` write more
shapes with less certainty. Before `infer_shapes` writes a shape, the stops
in the pattern must lie along it in order. When the shapes of Helsinki's tram
feed were withheld, the three levels produced these results
(`scripts/validate_shapes.py`):

| level | shapes written | median length error | worst offset |
| ----- | -------------- | ------------------- | ------------ |
| strict | 35/80 | 0.9% | 42 m |
| relaxed | 40/80 | 0.9% | 34 m |
| permissive | 43/80 | 0.9% | 184 m |

Helsinki's OpenStreetMap data is unusually complete. Elsewhere, expect fewer
and less accurate shapes.

## Documentation

The documentation is available at https://transitio.readthedocs.io and
includes the Quickstart, the tutorials and the API reference. To build it
locally, install `transitio` and the Sphinx toolchain:

```
pip install . -r docs/requirements.txt
sphinx-build -b html docs docs/_build/html
```

The feed index is built in a separate repository,
[transitio-dev/transitio-index](https://github.com/transitio-dev/transitio-index).

## License

MIT
