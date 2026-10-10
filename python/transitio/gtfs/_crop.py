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
    routes=None,
    exclude_trips=None,
    **options,
):
    """Crop a GTFS zip to an area of interest and/or a date window.

    Spatially, trips serving at least one stop inside the AOI are
    retained with their full stop sequences (or, with
    ``full_trips_only``, only trips entirely inside); temporally, trips
    whose service can be active inside the window are retained. Everything
    else — stops, routes, shapes, calendars, frequencies, transfers,
    pathways, fares, agencies — cascades away to a referentially
    consistent feed. An area, location group or network goes when the crop
    removed every row naming it; one that no row names stays. A fare rule
    naming a removed route or zone goes, and its fare goes whole when that
    leaves the fare without its route rules, its origin-destination rules
    or one of its contains zones, or when its agency goes, so no fare
    applies more widely than in the source. A fare without rules applies
    everywhere and stays. Header names and values
    are written without surrounding whitespace and with bytes that are not
    UTF-8 as U+FFFD, as ``validate_feed`` reads them; retained trips
    otherwise keep their times and attributes untouched. A trip naming a
    route that routes.txt lacks is not retained, and a retained trip's
    stop_times row naming a stop that stops.txt lacks is left out, the
    trip keeping its other rows unless fewer than two of its two or more
    remain; a row the reader skips as malformed counts as missing, and an
    empty ``route_id`` or ``stop_id`` is kept.
    A routes.txt or stops.txt without its id column refuses the crop with
    an ``OSError``. An exact repeat of a retained trips.txt row (every
    value equal once trimmed) is left out; a repeated ``trip_id`` with any
    value different refuses the crop with an ``OSError``.

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
    routes : iterable of str, optional
        Keep only trips whose ``route_id`` is in this set (applied alongside
        the area/date crop); ``None`` keeps every route.
    exclude_trips : iterable of str, optional
        Leave out the trips whose ``trip_id`` is in this set, with what only
        they used, as for any trip the crop does not retain; ``None`` leaves
        out none.
    full_trips_only : bool, default False
        Keep only trips whose every stop lies inside the AOI.
    **options
        The ``validate_feed`` keyword arguments (budgets,
        ``reference_date``, ``reference_time``). The budgets bound the
        tables parsed whole and the cropped feed; stop_times.txt, trips.txt
        and shapes.txt are streamed from the archive, so a national feed
        crops to a city within the defaults. A table that a row, byte or
        column budget cuts short, in the source or the cropped feed, refuses
        the crop with an ``OSError`` naming the file and the budget to
        raise; so does a table that cannot be read. A reached
        ``max_notices_per_file`` does not: the cropped feed's notices are
        then sampled, and a ``notice_limit_reached`` notice among them says
        so.

    Returns
    -------
    dict
        ``{"row_counts": ..., "source_routes": [...] or None,
        "source_notices": [...], "remaining_notices": [...],
        "service_window": ..., "dropped_rows": [...], "validation": ...}``
        for the cropped feed, ``validation`` being the cropped feed's
        :func:`~transitio.validate.validate_feed` report under the same
        options. ``source_routes`` is the distinct ``route_id`` values in the
        source routes.txt (before the crop), or ``None`` without routes.txt,
        so a caller can tell what a ``routes`` filter dropped.
        ``source_notices`` holds one ``leading_or_trailing_whitespaces``
        notice per source file the crop read and trimmed, its row numbered
        as in the source; shapes.txt is read only when a kept trip has a
        shape. ``dropped_rows`` has one record per file, field and code
        whose rows were left out, sorted: ``{"code", "filename",
        "fieldName", "parentFilename", "rowCount", "valueCount",
        "sampleValues"}``, the code being ``"foreign_key_violation"`` for a
        missing stop or route, ``"unusable_trip"`` (trips.txt ``trip_id``,
        ``parentFilename`` None) for trips left with fewer than two
        stop_times and ``"duplicate_key"`` (``parentFilename`` None) for
        exact repeats of a trips.txt row, the samples up to 50 of the
        distinct values, sorted.
    """
    if (
        aoi is None
        and start_date is None
        and end_date is None
        and routes is None
        and exclude_trips is None
    ):
        raise ValueError(
            "nothing to crop: pass aoi, a date window, routes and/or exclude_trips"
        )
    if isinstance(routes, str):
        routes = [routes]
    if isinstance(exclude_trips, str):
        exclude_trips = [exclude_trips]
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
            routes=None if routes is None else [str(r) for r in routes],
            exclude_trips=(
                None if exclude_trips is None else [str(t) for t in exclude_trips]
            ),
            **options,
        )
    )
