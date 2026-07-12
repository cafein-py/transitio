"""AOI-driven OSM and GTFS acquisition, validation and repair."""

__all__ = [
    "Dataset",
    "Feed",
    "MobilityDatabase",
    "exceptions",
    "fetch_pbf",
    "osm",
    "__version__",
]


def __getattr__(name):
    if name in ("Dataset", "Feed", "MobilityDatabase"):
        from beanpicker import catalog

        return getattr(catalog, name)
    if name == "fetch_pbf":
        from beanpicker.osm import fetch_pbf

        return fetch_pbf
    if name in ("exceptions", "osm"):
        import importlib

        return importlib.import_module(f"beanpicker.{name}")
    if name == "__version__":
        from beanpicker._core import __version__

        return __version__
    raise AttributeError(f"module 'beanpicker' has no attribute {name!r}")
