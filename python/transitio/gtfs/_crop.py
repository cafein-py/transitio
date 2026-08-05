"""Feed cropping over the Rust core."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping


def _mapping(aoi):
    """The GeoJSON-style mapping of an AOI, or None."""
    interface = getattr(aoi, "__geo_interface__", None)
    if isinstance(interface, Mapping):
        return interface
    if isinstance(aoi, Mapping) and "type" in aoi and "coordinates" in aoi:
        return aoi
    return None


def _rings(coordinates):
    return [[(float(x), float(y)) for x, y in ring] for ring in coordinates]


def _polygon_parts(aoi):
    """The polygon parts of an AOI, or None when it is not polygonal.

    A part is a list of closed rings, its outer boundary first; the
    result is the GeoJSON MultiPolygon shape whatever the input was.
    """
    frame_geometry = getattr(aoi, "geometry", None)
    if frame_geometry is not None and hasattr(frame_geometry, "__iter__"):
        parts = []
        for geometry in frame_geometry:
            found = _polygon_parts(geometry)
            if found is None:
                return None
            parts.extend(found)
        return parts or None
    mapping = _mapping(aoi)
    if mapping is None:
        return None
    kind = mapping.get("type")
    if kind == "Polygon":
        return [_rings(mapping["coordinates"])]
    if kind == "MultiPolygon":
        return [_rings(part) for part in mapping["coordinates"]]
    if kind == "Feature":
        return _polygon_parts(mapping.get("geometry") or {})
    if kind == "GeometryCollection":
        parts = []
        for geometry in mapping.get("geometries", []):
            found = _polygon_parts(geometry)
            if found is None:
                return None
            parts.extend(found)
        return parts or None
    return None


def crop_feed(
    path,
    output,
    *,
    aoi=None,
    start_date=None,
    end_date=None,
    full_trips_only=False,
    **options,
):
    """Crop a GTFS zip to an area of interest and/or a date window.

    Spatially, trips serving at least one stop inside the AOI are
    retained with their full stop sequences (or, with
    ``full_trips_only``, only trips entirely inside); temporally, trips
    whose service can be active inside the window are retained. Everything
    else — stops, routes, shapes, calendars, frequencies, transfers,
    pathways, fares, agencies — cascades away to a referentially
    consistent feed. Retained trips keep their times and attributes
    untouched.

    Parameters
    ----------
    path : str or pathlib.Path
        Source GTFS ``.zip``.
    output : str or pathlib.Path
        Destination path for the cropped ``.zip``.
    aoi : geometry, GeoDataFrame/GeoSeries, mapping or tuple, optional
        Area of interest. Polygons and MultiPolygons — shapely
        geometries, a GeoDataFrame/GeoSeries of them, or a GeoJSON-style
        mapping — crop to the polygon itself, holes included; anything
        else (a bounding-box tuple, a point or line geometry) crops to
        its bounding box.
    start_date, end_date : str, optional
        ``YYYYMMDD`` inclusive service-window bounds.
    full_trips_only : bool, default False
        Keep only trips whose every stop lies inside the AOI.
    **options
        The ``validate_feed`` keyword arguments (budgets,
        ``reference_date``, ``reference_time``).

    Returns
    -------
    dict
        ``{"row_counts": ..., "remaining_notices": [...],
        "service_window": ...}`` for the cropped feed.
    """
    if aoi is None and start_date is None and end_date is None:
        raise ValueError("nothing to crop: pass aoi and/or a date window")
    bbox = None
    polygon = None
    if aoi is not None:
        polygon = _polygon_parts(aoi)
        if polygon is None:
            from transitio.catalog._client import _bounds

            bbox = tuple(_bounds(aoi))
    from transitio import _core

    return json.loads(
        _core.crop_feed(
            os.fspath(path),
            os.fspath(output),
            bbox=bbox,
            polygon=polygon,
            start_date=start_date,
            end_date=end_date,
            full_trips_only=full_trips_only,
            **options,
        )
    )
