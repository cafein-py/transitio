# beanpicker

AOI-driven OSM and GTFS acquisition, validation and repair — companion to
[pyrosm](https://github.com/HTenkanen/pyrosm) and cafein. beanpicker selects
and prepares the raw beans (OSM and GTFS data) that cafein brews into routing
results.

**Status: early development.** The Mobility Database catalog client is the
first working piece; discovery, download, validation and repair modules follow
the roadmap in the project plan.

## Quick example

```python
import beanpicker

# Requires a (free) Mobility Database refresh token, either passed
# explicitly or set as the MOBILITY_API_REFRESH_TOKEN environment variable.
db = beanpicker.MobilityDatabase()

feeds = db.search_feeds(aoi=helsinki_polygon)      # any shapely geometry
dataset = db.dataset_for(feeds[0], when="2026-09-01")
path = db.download(dataset)                        # cached, checksum-verified
report = db.validation_report(dataset)             # hosted canonical-validator report
```

## Installation

Not yet on PyPI. From source (requires a Rust toolchain):

```
pip install .
```

## License

MIT
