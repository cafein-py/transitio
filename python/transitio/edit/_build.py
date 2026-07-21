"""Building GTFS feeds from geodata layers (the attribute convention)."""

from __future__ import annotations

import os

import pandas as pd

from transitio.edit._editor import (
    FeedBuilder,
    _haversine_m,
    format_gtfs_time,
    parse_gtfs_time,
)

_MODE_ROUTE_TYPES = {
    "tram": 0,
    "subway": 1,
    "rail": 2,
    "bus": 3,
    "ferry": 4,
}

_DEFAULTS = {
    "days": "daily",
    "first_departure": "06:00:00",
    "last_departure": "22:00:00",
    "speed_kmh": 25.0,
}


# Shapefile DBF field names cap at 10 characters; every convention
# column longer than that has a short alias.
_ALIASES = {
    "route_short_name": ("short_name",),
    "headway_min": ("headway",),
    "first_departure": ("first_dep",),
    "last_departure": ("last_dep",),
    "bidirectional": ("bidir",),
    "duration_min": ("duration",),
}


def _value(row, column, default=None):
    value = row.get(column)
    if value is None:
        for alias in _ALIASES.get(column, ()):
            value = row.get(alias)
            if value is not None:
                break
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass
    if isinstance(value, str) and not value.strip():
        return default
    return value


def _line_latlon(geometry):
    """A LineString's vertices as (lat, lon) pairs (z dropped)."""
    return [(c[1], c[0]) for c in geometry.coords]


def _cumulative_m(points):
    """Cumulative great-circle meters along (lat, lon) vertices."""
    distances = [0.0]
    for previous, current in zip(points, points[1:]):
        distances.append(distances[-1] + _haversine_m(previous, current))
    return distances


def _stops_along(points, cumulative, spacing):
    """Interpolated (lat, lon, meters) stop positions along a line."""
    import math

    if not 0 < spacing < math.inf:
        raise ValueError("stop_spacing must be positive and finite")
    total = cumulative[-1]
    if total <= 0:
        raise ValueError("route geometry has zero length")
    count = max(2, int(round(total / spacing)) + 1)
    if count > 10_000:
        raise ValueError(
            f"stop_spacing {spacing} m would interpolate {count} stops "
            "on one route; use a coarser spacing"
        )
    targets = [total * i / (count - 1) for i in range(count)]
    positions = []
    segment = 0
    for target in targets:
        while segment < len(cumulative) - 2 and cumulative[segment + 1] < target:
            segment += 1
        start, end = cumulative[segment], cumulative[segment + 1]
        fraction = 0.0 if end == start else (target - start) / (end - start)
        lat = points[segment][0] + fraction * (
            points[segment + 1][0] - points[segment][0]
        )
        lon = points[segment][1] + fraction * (
            points[segment + 1][1] - points[segment][1]
        )
        positions.append((lat, lon, target))
    return positions


def _stops_from_layer(stops, points, cumulative, snap_distance):
    """(stop_id, meters-along) for layer stops within reach of the line."""
    from pyproj import Transformer
    from shapely.geometry import LineString, Point

    center_lat, center_lon = points[0]
    project = Transformer.from_crs(
        "EPSG:4326",
        f"+proj=aeqd +lat_0={center_lat} +lon_0={center_lon} +datum=WGS84",
        always_xy=True,
    ).transform
    line = LineString([project(lon, lat) for lat, lon in points])
    selected = []
    for stop_id, stop_lat, stop_lon in stops:
        point = Point(*project(stop_lon, stop_lat))
        if line.distance(point) <= snap_distance:
            selected.append((stop_id, line.project(point)))
    selected.sort(key=lambda item: item[1])
    return selected


def build_feed(
    source,
    output,
    *,
    timezone,
    routes_layer=None,
    stops_layer=None,
    agency_name="transitio scenario",
    agency_url="https://github.com/cafein-py/transitio",
    periods=None,
    stop_spacing=400.0,
    stop_snap_distance=100.0,
    service_start="20260101",
    service_end="20261231",
    exact_times=False,
    snap_to=None,
    network_type="driving",
    check=True,
    **budgets,
):
    """Build a frequency-based GTFS feed from geodata route layers.

    Reads route alignments (LineStrings) — from a GeoPackage/Shapefile
    path or a GeoDataFrame — under a small attribute convention, and
    writes a complete validated feed: geometries become shapes, stops
    come from an optional point layer (snapped to each route) or are
    interpolated at ``stop_spacing`` meters, and headway attributes
    become frequency-based trips in both directions.

    Route attributes (all optional except a headway):

    - ``route_id`` / ``route_short_name`` — identifiers (defaulted from
      the row index when absent).
    - ``mode`` — ``tram``/``subway``/``rail``/``bus``/``ferry``, or a
      numeric ``route_type`` column (default bus).
    - ``headway_min`` — headway in minutes over the operating window
      (``first_departure``/``last_departure`` columns, default
      06:00–22:00); or per-period columns ``headway_<name>`` matched to
      the ``periods`` mapping ``{name: (start, end)}``.
    - ``speed_kmh`` (default 25) or ``duration_min`` — one-way run time.
    - ``days`` — ``daily``/``weekdays``/``weekend`` or comma-separated
      weekday names (default daily).
    - ``bidirectional`` — truthy to also generate the reverse direction
      (default true).

    Shapefile DBF field names cap at 10 characters, so the long columns
    have short aliases: ``short_name``, ``headway``, ``first_dep``,
    ``last_dep``, ``bidir`` and ``duration``.

    Parameters
    ----------
    source : str, pathlib.Path or GeoDataFrame
        Route layer source; paths are read with geopandas.
    output : str or pathlib.Path
        Feed zip to write.
    timezone : str
        IANA agency timezone (required by GTFS).
    routes_layer, stops_layer : str, optional
        Layer names for multi-layer sources (GeoPackage); ``stops_layer``
        may also be a GeoDataFrame of stop points with ``stop_id`` /
        ``stop_name`` columns.
    periods : dict, optional
        ``{name: (start, end)}`` service periods for ``headway_<name>``
        columns, times as ``H:MM:SS``.
    stop_spacing : float, default 400
        Interpolated stop spacing in meters when no stop layer is given.
    stop_snap_distance : float, default 100
        Maximum meters from a route line for a layer stop to serve it.
    service_start, service_end : str, default 2026
        Calendar validity window, ``YYYYMMDD``.
    exact_times : bool, default False
        Expand every frequency window into individually scheduled trips
        (one per departure) instead of writing frequencies.txt.
    snap_to : str or pathlib.Path, optional
        OSM ``.osm.pbf`` extract; when given, each route geometry is
        routed along the street network through its vertices with
        :func:`~transitio.edit.snap_to_network` before becoming the
        shape (requires ``transitio[snap]``).
    network_type : str, default "driving"
        pyrosm network type for ``snap_to``.
    check : bool, default True
        Passed to :meth:`FeedBuilder.save`.
    **budgets
        ``validate_feed`` keyword arguments.

    Returns
    -------
    dict
        The validation report of the written feed.
    """
    import geopandas as gpd

    if isinstance(source, gpd.GeoDataFrame):
        routes = source
    else:
        kwargs = {"layer": routes_layer} if routes_layer else {}
        routes = gpd.read_file(os.fspath(source), **kwargs)
    if routes.crs is None:
        import warnings

        warnings.warn(
            "route layer has no CRS; assuming WGS84 coordinates",
            UserWarning,
            stacklevel=2,
        )
    elif routes.crs.to_epsg() != 4326:
        routes = routes.to_crs("EPSG:4326")

    stop_rows = None
    if stops_layer is not None:
        if isinstance(stops_layer, gpd.GeoDataFrame):
            stop_frame = stops_layer
        else:
            stop_frame = gpd.read_file(os.fspath(source), layer=stops_layer)
        if stop_frame.crs is not None and stop_frame.crs.to_epsg() != 4326:
            stop_frame = stop_frame.to_crs("EPSG:4326")
        stop_rows = []
        for (index, row), point in zip(stop_frame.iterrows(), stop_frame.geometry):
            if point is None or point.is_empty:
                continue
            stop_id = str(_value(row, "stop_id", f"stop-{index}"))
            name = str(_value(row, "stop_name", stop_id))
            stop_rows.append((stop_id, name, point.y, point.x))

    builder = FeedBuilder()
    builder.add_agency("agency", agency_name, agency_url, timezone)

    if stop_rows is not None:
        for stop_id, name, lat, lon in stop_rows:
            builder.add_stop(stop_id, name, lat, lon)

    for (index, row), geometry in zip(routes.iterrows(), routes.geometry):
        if geometry is None or geometry.geom_type != "LineString":
            raise ValueError(f"route {index} needs a LineString geometry")
        if snap_to is not None:
            from transitio.edit._snap import snap_to_network

            geometry = snap_to_network(
                _line_latlon(geometry), snap_to, network_type=network_type
            )
        route_id = str(_value(row, "route_id", f"route-{index}"))
        short_name = str(_value(row, "route_short_name", route_id))
        mode = _value(row, "mode")
        if mode is not None:
            try:
                route_type = _MODE_ROUTE_TYPES[str(mode).lower()]
            except KeyError:
                raise ValueError(
                    f"route {route_id}: unknown mode {mode!r}; "
                    f"valid: {sorted(_MODE_ROUTE_TYPES)}"
                ) from None
        else:
            raw_type = float(_value(row, "route_type", 3))
            if raw_type != int(raw_type):
                raise ValueError(f"route {route_id}: route_type must be a whole number")
            route_type = int(raw_type)
        builder.add_route(route_id, route_type, short_name, agency_id="agency")

        days = _value(row, "days")
        if days is None:
            from transitio.edit._editor import _DAY_COLUMNS

            present = [day for day in _DAY_COLUMNS if day in row.index]
            flagged = [
                day
                for day in present
                if str(_value(row, day, "")).lower() in ("1", "true", "yes")
            ]
            if present and not flagged:
                raise ValueError(
                    f"route {route_id}: weekday columns present "
                    "but no day is enabled"
                )
            days = flagged or _DEFAULTS["days"]
        elif "," in str(days):
            days = [part.strip() for part in str(days).split(",")]
        else:
            days = str(days)
        service_id = f"{route_id}-service"
        builder.add_service(service_id, days, service_start, service_end)

        points = _line_latlon(geometry)
        cumulative = _cumulative_m(points)
        total_m = cumulative[-1]

        shape_id = f"{route_id}-shape"
        builder.add_shape(shape_id, geometry)
        reverse_shape_id = f"{route_id}-shape-r"

        import math

        duration_min = _value(row, "duration_min")
        if duration_min is not None:
            run_seconds = float(duration_min) * 60.0
        else:
            speed = float(_value(row, "speed_kmh", _DEFAULTS["speed_kmh"]))
            if not 0 < speed < math.inf:
                raise ValueError(
                    f"route {route_id}: speed_kmh must be positive and finite"
                )
            run_seconds = total_m / (speed / 3.6)
        if not 0 < run_seconds < math.inf:
            raise ValueError(f"route {route_id}: run time must be positive and finite")

        if stop_rows is not None:
            served = _stops_from_layer(
                [(sid, lat, lon) for sid, _, lat, lon in stop_rows],
                points,
                cumulative,
                stop_snap_distance,
            )
            if len(served) < 2:
                raise ValueError(
                    f"route {route_id}: fewer than two stops within "
                    f"{stop_snap_distance} m of the line"
                )
            # Offsets rebase to the first served stop so every run departs
            # it at exactly the window's departure times; the run time
            # covers the served span.
            first_m = served[0][1]
            span_m = served[-1][1] - first_m
            if span_m <= 0:
                raise ValueError(f"route {route_id}: served stops span zero distance")
            sequence = [
                (stop_id, run_seconds * (meters - first_m) / span_m)
                for stop_id, meters in served
            ]
        else:
            sequence = []
            for position, (lat, lon, meters) in enumerate(
                _stops_along(points, cumulative, stop_spacing)
            ):
                stop_id = f"{route_id}-stop-{position + 1}"
                builder.add_stop(stop_id, stop_id, lat, lon)
                sequence.append((stop_id, run_seconds * meters / total_m))

        windows = []
        if periods:
            for name, (start, end) in periods.items():
                headway = _value(row, f"headway_{name}")
                if headway is not None:
                    windows.append((name, start, end, float(headway) * 60.0))
        headway_min = _value(row, "headway_min")
        if headway_min is not None and windows:
            raise ValueError(
                f"route {route_id}: give either headway_min or per-period "
                "headway_<name> columns, not both"
            )
        if headway_min is not None:
            windows.append(
                (
                    "day",
                    str(_value(row, "first_departure", _DEFAULTS["first_departure"])),
                    str(_value(row, "last_departure", _DEFAULTS["last_departure"])),
                    float(headway_min) * 60.0,
                )
            )
        if not windows:
            raise ValueError(
                f"route {route_id}: no headway attribute "
                "(headway_min or a headway_<period> column)"
            )

        bidirectional = _value(row, "bidirectional", True)
        bidirectional = str(bidirectional).lower() not in ("false", "0", "no")
        directions = [("0", sequence, shape_id)]
        if bidirectional:
            run_end = sequence[-1][1]
            reversed_sequence = [
                (stop_id, run_end - offset) for stop_id, offset in reversed(sequence)
            ]
            builder.add_shape(reverse_shape_id, list(reversed(_line_latlon(geometry))))
            directions.append(("1", reversed_sequence, reverse_shape_id))

        for direction, direction_sequence, direction_shape in directions:
            for name, start, end, headway_seconds in windows:
                trip_id = f"{route_id}-{name}-{direction}"
                offsets = [
                    (stop_id, int(round(offset)))
                    for stop_id, offset in direction_sequence
                ]
                start_s = parse_gtfs_time(start)
                end_s = parse_gtfs_time(end)
                headway_s = int(round(headway_seconds))
                if end_s <= start_s:
                    raise ValueError(
                        f"route {route_id}, period {name}: window end "
                        "must be after its start"
                    )
                if headway_s <= 0:
                    raise ValueError(
                        f"route {route_id}, period {name}: headway " "must be positive"
                    )
                if exact_times:
                    runs = len(range(start_s, end_s, headway_s))
                    if runs > 10_000:
                        raise ValueError(
                            f"route {route_id}, period {name}: exact_times "
                            f"would expand to {runs} trips; use frequencies "
                            "or a coarser headway"
                        )
                    trip_rows = []
                    time_rows = []
                    for run, departure in enumerate(
                        range(start_s, end_s, headway_s), start=1
                    ):
                        run_trip_id = f"{trip_id}-{run}"
                        trip_rows.append(
                            {
                                "route_id": route_id,
                                "service_id": service_id,
                                "trip_id": run_trip_id,
                                "shape_id": direction_shape,
                                "direction_id": direction,
                            }
                        )
                        for sequence_no, (stop_id, offset) in enumerate(
                            offsets, start=1
                        ):
                            when = format_gtfs_time(departure + offset)
                            time_rows.append(
                                {
                                    "trip_id": run_trip_id,
                                    "arrival_time": when,
                                    "departure_time": when,
                                    "stop_id": stop_id,
                                    "stop_sequence": sequence_no,
                                }
                            )
                    builder._append_rows("trips.txt", trip_rows)
                    builder._append_rows("stop_times.txt", time_rows)
                else:
                    builder.add_frequency_trip(
                        route_id,
                        service_id,
                        trip_id,
                        offsets,
                        start=format_gtfs_time(start_s),
                        end=format_gtfs_time(end_s),
                        headway=headway_s,
                        shape_id=direction_shape,
                        direction_id=direction,
                    )

    return builder.save(output, check=check, **budgets)
