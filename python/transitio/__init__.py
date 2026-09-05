"""AOI-driven OSM and GTFS acquisition, validation and repair."""

__all__ = [
    "Dataset",
    "Feed",
    "FeedBuilder",
    "FeedEditor",
    "OsmEditor",
    "FetchResult",
    "AtlasFeed",
    "IndexedFeed",
    "MobilityDatabase",
    "Place",
    "TransitlandAtlas",
    "exceptions",
    "build_feed",
    "compare_feed_history",
    "compare_feeds",
    "crop_feed",
    "edit",
    "fetch",
    "fetch_pbf",
    "gtfs",
    "index",
    "merge_feeds",
    "merge_tables",
    "osm",
    "patch_feed",
    "pipeline",
    "place",
    "places",
    "repair",
    "infer_shapes",
    "repair_feed",
    "report",
    "shapes",
    "validate",
    "validate_feed",
    "__version__",
]


def __getattr__(name):
    if name in ("AtlasFeed", "Dataset", "Feed", "MobilityDatabase", "TransitlandAtlas"):
        from transitio import catalog

        return getattr(catalog, name)
    if name == "infer_shapes":
        from transitio.shapes import infer_shapes

        return infer_shapes
    if name == "fetch_pbf":
        from transitio.osm import fetch_pbf

        return fetch_pbf
    if name in ("FeedBuilder", "FeedEditor", "OsmEditor", "build_feed"):
        from transitio import edit

        return getattr(edit, name)
    if name in ("fetch", "FetchResult"):
        from transitio import pipeline

        return getattr(pipeline, name)
    if name in (
        "compare_feed_history",
        "compare_feeds",
        "crop_feed",
        "merge_feeds",
        "merge_tables",
        "patch_feed",
    ):
        from transitio import gtfs

        return getattr(gtfs, name)
    if name == "repair_feed":
        from transitio.repair import repair_feed

        return repair_feed
    if name == "validate_feed":
        from transitio.validate import validate_feed

        return validate_feed
    if name in ("IndexedFeed", "Place", "place", "places"):
        from transitio import index

        return getattr(index, name)
    if name in (
        "edit",
        "exceptions",
        "gtfs",
        "index",
        "osm",
        "pipeline",
        "repair",
        "report",
        "shapes",
        "validate",
    ):
        import importlib

        return importlib.import_module(f"transitio.{name}")
    if name == "__version__":
        from transitio._core import __version__

        return __version__
    raise AttributeError(f"module 'transitio' has no attribute {name!r}")
