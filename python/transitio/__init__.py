"""AOI-driven OSM and GTFS acquisition, validation and repair."""

__all__ = [
    "Dataset",
    "Feed",
    "FeedBuilder",
    "FeedEditor",
    "OsmEditor",
    "FetchResult",
    "MobilityDatabase",
    "exceptions",
    "build_feed",
    "crop_feed",
    "edit",
    "fetch",
    "fetch_pbf",
    "gtfs",
    "osm",
    "pipeline",
    "repair",
    "repair_feed",
    "report",
    "validate",
    "validate_feed",
    "__version__",
]


def __getattr__(name):
    if name in ("Dataset", "Feed", "MobilityDatabase"):
        from transitio import catalog

        return getattr(catalog, name)
    if name == "fetch_pbf":
        from transitio.osm import fetch_pbf

        return fetch_pbf
    if name in ("FeedBuilder", "FeedEditor", "OsmEditor", "build_feed"):
        from transitio import edit

        return getattr(edit, name)
    if name in ("fetch", "FetchResult"):
        from transitio import pipeline

        return getattr(pipeline, name)
    if name == "crop_feed":
        from transitio.gtfs import crop_feed

        return crop_feed
    if name == "repair_feed":
        from transitio.repair import repair_feed

        return repair_feed
    if name == "validate_feed":
        from transitio.validate import validate_feed

        return validate_feed
    if name in (
        "edit",
        "exceptions",
        "gtfs",
        "osm",
        "pipeline",
        "repair",
        "report",
        "validate",
    ):
        import importlib

        return importlib.import_module(f"transitio.{name}")
    if name == "__version__":
        from transitio._core import __version__

        return __version__
    raise AttributeError(f"module 'transitio' has no attribute {name!r}")
