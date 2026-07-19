"""Feed cropping over the Rust core."""

from __future__ import annotations

import json
import os


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

    Spatially, trips serving at least one stop inside the AOI's bounding
    box are retained with their full stop sequences (or, with
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
    aoi : geometry, GeoDataFrame/GeoSeries or tuple, optional
        Area of interest; reduced to its bounding box (polygon-true
        cropping is not implemented).
    start_date, end_date : str, optional
        ``YYYYMMDD`` inclusive service-window bounds.
    full_trips_only : bool, default False
        Keep only trips whose every stop lies inside the AOI.
    **options
        The ``validate_feed`` keyword arguments (budgets,
        ``reference_date``).

    Returns
    -------
    dict
        ``{"row_counts": ..., "remaining_notices": [...],
        "service_window": ...}`` for the cropped feed.
    """
    if aoi is None and start_date is None and end_date is None:
        raise ValueError("nothing to crop: pass aoi and/or a date window")
    bbox = None
    if aoi is not None:
        from transitio.catalog._client import _bounds

        bbox = tuple(_bounds(aoi))
    from transitio import _core

    return json.loads(
        _core.crop_feed(
            os.fspath(path),
            os.fspath(output),
            bbox=bbox,
            start_date=start_date,
            end_date=end_date,
            full_trips_only=full_trips_only,
            **options,
        )
    )
