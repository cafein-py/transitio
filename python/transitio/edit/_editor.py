"""Feed building and editing over pandas tables."""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import pandas as pd

from transitio.exceptions import InvalidFeedError

_GTFS_TABLES = frozenset(
    {
        "agency.txt",
        "areas.txt",
        "attributions.txt",
        "booking_rules.txt",
        "calendar.txt",
        "calendar_dates.txt",
        "fare_attributes.txt",
        "fare_leg_join_rules.txt",
        "fare_leg_rules.txt",
        "fare_media.txt",
        "fare_products.txt",
        "fare_rules.txt",
        "fare_transfer_rules.txt",
        "feed_info.txt",
        "frequencies.txt",
        "levels.txt",
        "location_group_stops.txt",
        "location_groups.txt",
        "networks.txt",
        "pathways.txt",
        "rider_categories.txt",
        "route_networks.txt",
        "routes.txt",
        "shapes.txt",
        "stop_areas.txt",
        "stop_times.txt",
        "stops.txt",
        "timeframes.txt",
        "transfers.txt",
        "translations.txt",
        "trips.txt",
    }
)

# Declared-size budget for loading an archive into memory; the Rust
# validator applies its own budgets when the saved feed is scanned.
_MAX_TOTAL_BYTES = 2 << 30

_DAY_COLUMNS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
_DAY_SHORTCUTS = {
    "daily": _DAY_COLUMNS,
    "weekdays": _DAY_COLUMNS[:5],
    "weekend": _DAY_COLUMNS[5:],
}


def parse_gtfs_time(value):
    """Seconds since midnight from ``H:MM:SS`` (hours may exceed 24)."""
    if isinstance(value, bool):
        raise ValueError(f"invalid GTFS time: {value!r}")
    if isinstance(value, (int, float)):
        if value < 0 or value != int(value):
            raise ValueError(f"invalid GTFS time: {value!r}")
        return int(value)
    parts = str(value).strip().split(":")
    if len(parts) != 3:
        raise ValueError(f"invalid GTFS time: {value!r}")
    try:
        hours, minutes, seconds = (int(part) for part in parts)
    except ValueError:
        raise ValueError(f"invalid GTFS time: {value!r}") from None
    if hours < 0 or not 0 <= minutes < 60 or not 0 <= seconds < 60:
        raise ValueError(f"invalid GTFS time: {value!r}")
    return hours * 3600 + minutes * 60 + seconds


def format_gtfs_time(seconds):
    """``HH:MM:SS`` from seconds since midnight (over-midnight kept)."""
    if isinstance(seconds, bool) or seconds != int(seconds):
        raise ValueError(f"invalid GTFS time in seconds: {seconds!r}")
    seconds = int(seconds)
    if seconds < 0:
        raise ValueError("GTFS times cannot be negative")
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _haversine_m(a, b):
    """Great-circle meters between two (lat, lon) points."""
    from math import asin, cos, radians, sin, sqrt

    lat1, lon1, lat2, lon2 = map(radians, (*a, *b))
    h = (
        sin((lat2 - lat1) / 2) ** 2
        + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * 6371008.8 * asin(sqrt(h))


def _as_yyyymmdd(value):
    if hasattr(value, "strftime"):
        return value.strftime("%Y%m%d")
    return str(value)


def _safe_entry_name(name):
    """Whether a zip entry name is safe to carry through to the output."""
    if "\\" in name or "\x00" in name or name.startswith("/"):
        return False
    parts = name.split("/")
    return all(part not in ("", ".", "..") for part in parts)


class FeedBuilder:
    """Build a GTFS feed from scratch, one entity at a time.

    Tables live as pandas DataFrames of strings in :attr:`tables`
    (keyed by filename); the ``add_*`` helpers append rows with the
    right columns, and :meth:`save` writes the zip atomically and runs
    transitio's validator over it (the routing-oriented rule subset,
    under canonical notice codes — not the full MobilityData
    validator). Direct DataFrame edits through
    :attr:`tables` are supported for anything the helpers do not cover.
    """

    def __init__(self):
        self.tables = {}
        self._extra_entries = {}

    # -- table plumbing ---------------------------------------------------

    def _append(self, filename, row):
        return self._append_rows(filename, [row])

    def _append_rows(self, filename, rows):
        rows = [
            {key: "" if value is None else str(value) for key, value in row.items()}
            for row in rows
        ]
        addition = pd.DataFrame(rows, dtype=str)
        table = self.tables.get(filename)
        if table is None:
            self.tables[filename] = addition.fillna("")
        else:
            self.tables[filename] = pd.concat(
                [table, addition], ignore_index=True
            ).fillna("")
        return self

    # -- entity helpers ---------------------------------------------------

    def add_agency(self, agency_id, name, url, timezone, **fields):
        """Add an agency row."""
        return self._append(
            "agency.txt",
            {
                "agency_id": agency_id,
                "agency_name": name,
                "agency_url": url,
                "agency_timezone": timezone,
                **fields,
            },
        )

    def add_stop(self, stop_id, name, lat, lon, **fields):
        """Add a stop at WGS84 ``lat``/``lon``."""
        return self._append(
            "stops.txt",
            {
                "stop_id": stop_id,
                "stop_name": name,
                "stop_lat": lat,
                "stop_lon": lon,
                **fields,
            },
        )

    def add_route(self, route_id, route_type, short_name, *, agency_id=None, **fields):
        """Add a route; ``route_type`` is the numeric GTFS type."""
        return self._append(
            "routes.txt",
            {
                "route_id": route_id,
                "agency_id": agency_id,
                "route_short_name": short_name,
                "route_type": route_type,
                **fields,
            },
        )

    def add_service(self, service_id, days, start_date, end_date):
        """Add a calendar row.

        ``days`` is an iterable of weekday names, or one of the
        shortcuts ``"daily"``, ``"weekdays"``, ``"weekend"``.
        """
        if isinstance(days, str):
            try:
                days = _DAY_SHORTCUTS[days]
            except KeyError:
                raise ValueError(
                    f"unknown day shortcut {days!r}; "
                    f"valid: {sorted(_DAY_SHORTCUTS)}"
                ) from None
        days = {str(day).lower() for day in days}
        unknown = days - set(_DAY_COLUMNS)
        if unknown:
            raise ValueError(f"unknown weekday names {sorted(unknown)}")
        row = {"service_id": service_id}
        for column in _DAY_COLUMNS:
            row[column] = "1" if column in days else "0"
        row["start_date"] = _as_yyyymmdd(start_date)
        row["end_date"] = _as_yyyymmdd(end_date)
        return self._append("calendar.txt", row)

    def add_shape(self, shape_id, geometry, *, distances=True):
        """Add a shape polyline for trips to reference.

        ``geometry`` is a shapely LineString in WGS84 (x = longitude,
        e.g. from :func:`~transitio.edit.snap_to_network`) or a sequence
        of ``(lat, lon)`` pairs. With ``distances``, cumulative
        great-circle ``shape_dist_traveled`` values in meters are
        written, which cafein uses for per-trip travel distances.
        """
        coords = getattr(geometry, "coords", None)
        if coords is not None:
            points = [(c[1], c[0]) for c in coords]
        else:
            points = [(float(lat), float(lon)) for lat, lon in geometry]
        if len(points) < 2:
            raise ValueError("a shape needs at least two points")
        total = 0.0
        rows = []
        for sequence, (lat, lon) in enumerate(points, start=1):
            row = {
                "shape_id": shape_id,
                "shape_pt_lat": repr(lat),
                "shape_pt_lon": repr(lon),
                "shape_pt_sequence": sequence,
            }
            if distances:
                if sequence > 1:
                    total += _haversine_m(points[sequence - 2], (lat, lon))
                row["shape_dist_traveled"] = f"{total:.1f}"
            rows.append(row)
        for row in rows:
            self._append("shapes.txt", row)
        return self

    def add_trip(
        self, route_id, service_id, trip_id, stops, *, shape_id=None, **fields
    ):
        """Add a scheduled trip.

        ``stops`` is a sequence of ``(stop_id, arrival, departure)``
        with times as ``H:MM:SS`` strings or seconds since midnight;
        ``shape_id`` references a polyline added with :meth:`add_shape`.
        All inputs are checked before anything is appended, so a bad
        tuple never leaves a partial trip behind.
        """
        if shape_id is not None:
            fields["shape_id"] = shape_id
        rows = [
            {
                "trip_id": trip_id,
                "arrival_time": format_gtfs_time(parse_gtfs_time(arrival)),
                "departure_time": format_gtfs_time(parse_gtfs_time(departure)),
                "stop_id": stop_id,
                "stop_sequence": sequence,
            }
            for sequence, (stop_id, arrival, departure) in enumerate(stops, start=1)
        ]
        self._append(
            "trips.txt",
            {
                "route_id": route_id,
                "service_id": service_id,
                "trip_id": trip_id,
                **fields,
            },
        )
        for row in rows:
            self._append("stop_times.txt", row)
        return self

    def add_frequency_trip(
        self,
        route_id,
        service_id,
        trip_id,
        stops,
        *,
        start,
        end,
        headway,
        shape_id=None,
        **fields,
    ):
        """Add a frequency-based (headway) trip.

        ``stops`` is a sequence of ``(stop_id, offset)`` where the
        offset (seconds, or ``H:MM:SS``) is the travel time from the
        start of a run; the template run departs at ``start`` and a run
        starts every ``headway`` seconds until ``end``.
        """
        start_s = parse_gtfs_time(start)
        end_s = parse_gtfs_time(end)
        if end_s <= start_s:
            raise ValueError("frequency end must be after start")
        if isinstance(headway, bool) or headway != int(headway) or int(headway) <= 0:
            raise ValueError("headway must be a positive whole number of seconds")
        headway = int(headway)
        template = [
            (
                stop_id,
                start_s + parse_gtfs_time(offset),
                start_s + parse_gtfs_time(offset),
            )
            for stop_id, offset in stops
        ]
        self.add_trip(
            route_id, service_id, trip_id, template, shape_id=shape_id, **fields
        )
        return self._append(
            "frequencies.txt",
            {
                "trip_id": trip_id,
                "start_time": format_gtfs_time(start_s),
                "end_time": format_gtfs_time(end_s),
                "headway_secs": int(headway),
            },
        )

    def _table(self, filename):
        try:
            return self.tables[filename]
        except KeyError:
            raise ValueError(f"feed has no {filename}") from None

    @property
    def stops(self):
        """The stops table as a WGS84 GeoDataFrame copy.

        Write changes back through ``tables["stops.txt"]`` (or, on an
        editor, :meth:`FeedEditor.update_stop`); the geometry column
        here is derived, not stored.
        """
        import geopandas as gpd
        from shapely.geometry import Point

        table = self._table("stops.txt")
        lat = pd.to_numeric(table.get("stop_lat"), errors="coerce")
        lon = pd.to_numeric(table.get("stop_lon"), errors="coerce")
        geometry = [
            Point(x, y) if pd.notna(x) and pd.notna(y) else None
            for x, y in zip(lon, lat)
        ]
        return gpd.GeoDataFrame(table.copy(), geometry=geometry, crs="EPSG:4326")

    def set_stops(self, frame):
        """Write a (Geo)DataFrame back as the stops table.

        A geometry column, when present, overwrites ``stop_lat`` /
        ``stop_lon``; everything else is stored as strings. This is the
        write-back counterpart of the :attr:`stops` view.
        """
        geometry = getattr(frame, "geometry", None)
        crs = getattr(geometry, "crs", None)
        if crs is not None:
            epsg = crs.to_epsg()
            if epsg != 4326:
                frame = frame.to_crs("EPSG:4326")
                geometry = frame.geometry
        table = pd.DataFrame(frame).copy()
        if geometry is not None:
            table["stop_lat"] = [
                "" if point is None else repr(point.y) for point in geometry
            ]
            table["stop_lon"] = [
                "" if point is None else repr(point.x) for point in geometry
            ]
            table = table.drop(columns=[geometry.name], errors="ignore")
        self.tables["stops.txt"] = table.astype(str).reset_index(drop=True)
        return self

    @property
    def shapes(self):
        """Shapes as a WGS84 GeoDataFrame, one LineString per shape_id."""
        import geopandas as gpd
        from shapely.geometry import LineString

        table = self._table("shapes.txt").copy()
        table["_lat"] = pd.to_numeric(table["shape_pt_lat"], errors="coerce")
        table["_lon"] = pd.to_numeric(table["shape_pt_lon"], errors="coerce")
        table["_seq"] = pd.to_numeric(table["shape_pt_sequence"], errors="coerce")
        records = []
        for shape_id, group in table.sort_values("_seq").groupby("shape_id", sort=True):
            coordinates = [
                (lon, lat)
                for lon, lat in zip(group["_lon"], group["_lat"])
                if pd.notna(lon) and pd.notna(lat)
            ]
            records.append(
                {
                    "shape_id": shape_id,
                    "geometry": (
                        LineString(coordinates) if len(coordinates) > 1 else None
                    ),
                }
            )
        return gpd.GeoDataFrame(records, crs="EPSG:4326")

    # -- output -----------------------------------------------------------

    def save(self, path, *, check=True, **budgets):
        """Write the feed zip atomically and validate it.

        Parameters
        ----------
        path : str or pathlib.Path
            Output feed zip.
        check : bool, default True
            Raise :class:`~transitio.exceptions.InvalidFeedError` when
            the validator reports ERROR-severity notices (the report is
            on the exception's ``report`` attribute and the file is
            still written); ``False`` skips the gate but still returns
            the report.
        **budgets
            ``validate_feed`` keyword arguments.

        Returns
        -------
        dict
            The ``validate_feed`` report of the written feed.
        """
        from transitio.validate import validate_feed

        path = Path(path)
        staging = path.with_name(path.name + ".part")
        for target in (path, staging):
            if target.is_symlink():
                raise ValueError(f"{target} is a symlink; refusing to follow it")
        unsafe = [name for name in self.tables if not _safe_entry_name(name)]
        if unsafe:
            raise ValueError(f"unsafe table names: {unsafe}")
        with zipfile.ZipFile(staging, "w", zipfile.ZIP_DEFLATED) as archive:
            for filename, table in self.tables.items():
                with archive.open(filename, "w") as handle:
                    wrapper = io.TextIOWrapper(handle, encoding="utf-8", newline="")
                    table.to_csv(wrapper, index=False, lineterminator="\n")
                    wrapper.flush()
                    wrapper.detach()
            for filename, content in self._extra_entries.items():
                if _safe_entry_name(filename) and filename not in self.tables:
                    archive.writestr(filename, content)

        # Validate the staging bytes, then publish that exact artifact.
        try:
            report = validate_feed(staging, **budgets)
        except Exception:
            staging.unlink(missing_ok=True)
            raise
        os.replace(staging, path)
        if check:
            errors = sum(
                1 for notice in report["notices"] if notice["severity"] == "ERROR"
            )
            if errors:
                error = InvalidFeedError(
                    f"saved feed has {errors} error-severity notices "
                    "(pass check=False to skip this gate)"
                )
                error.report = report
                raise error
        return report


class FeedEditor(FeedBuilder):
    """Edit an existing GTFS feed.

    Loads every root-level ``.txt`` table into a string DataFrame in
    :attr:`tables`; anything else in the archive (``locations.geojson``,
    nested or unknown entries) is preserved verbatim on save. All
    :class:`FeedBuilder` helpers work for additions, and
    :meth:`~FeedBuilder.save` validates the result.
    """

    def __init__(self, path, *, max_total_bytes=_MAX_TOTAL_BYTES):
        super().__init__()
        self.source = Path(path)
        with zipfile.ZipFile(self.source) as archive:
            declared = sum(
                info.file_size for info in archive.infolist() if not info.is_dir()
            )
            if declared > max_total_bytes:
                raise ValueError(
                    f"archive declares {declared} uncompressed bytes, over the "
                    f"{max_total_bytes} budget (raise max_total_bytes to load)"
                )
            for info in archive.infolist():
                if info.is_dir():
                    continue
                name = info.filename
                if not _safe_entry_name(name):
                    continue
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    continue  # never carry symlink entries along
                if name in _GTFS_TABLES:
                    self.tables[name] = pd.read_csv(
                        archive.open(name),
                        dtype=str,
                        keep_default_na=False,
                        encoding="utf-8-sig",
                    )
                else:
                    # Unknown files (any extension) are preserved verbatim,
                    # never parsed and rewritten.
                    self._extra_entries[name] = archive.read(name)

    # -- edit helpers ------------------------------------------------------

    def update_stop(self, stop_id, **fields):
        """Set columns of one stop row (creating columns as needed)."""
        return self._update("stops.txt", "stop_id", stop_id, fields)

    def update_route(self, route_id, **fields):
        """Set columns of one route row (creating columns as needed)."""
        return self._update("routes.txt", "route_id", route_id, fields)

    def _update(self, filename, key_column, key, fields):
        table = self._table(filename)
        mask = table[key_column] == str(key)
        if not mask.any():
            raise ValueError(f"no {key_column} {key!r} in {filename}")
        for column, value in fields.items():
            if column not in table.columns:
                table[column] = ""
            table.loc[mask, column] = "" if value is None else str(value)
        return self

    def set_headway(self, trip_id, headway, *, window=None, start=None, end=None):
        """Change a frequency trip's headway (and optionally its window).

        A trip may carry several frequency windows; ``window`` selects
        one by its current start time and is required in that case.
        """
        if isinstance(headway, bool) or headway != int(headway) or int(headway) <= 0:
            raise ValueError("headway must be a positive whole number of seconds")
        new_start = None if start is None else format_gtfs_time(parse_gtfs_time(start))
        new_end = None if end is None else format_gtfs_time(parse_gtfs_time(end))
        frequencies = self._table("frequencies.txt")
        mask = frequencies["trip_id"] == str(trip_id)
        if window is not None:
            mask &= frequencies["start_time"] == format_gtfs_time(
                parse_gtfs_time(window)
            )
        matches = int(mask.sum())
        if matches == 0:
            raise ValueError(f"no matching frequency row for trip {trip_id!r}")
        if matches > 1:
            raise ValueError(
                f"trip {trip_id!r} has {matches} frequency windows; "
                "select one with window=<current start time>"
            )
        frequencies.loc[mask, "headway_secs"] = str(int(headway))
        if new_start is not None:
            frequencies.loc[mask, "start_time"] = new_start
        if new_end is not None:
            frequencies.loc[mask, "end_time"] = new_end
        return self

    def shift_trip(self, trip_id, seconds):
        """Shift a trip's stop_times (and frequency window) in time."""
        table = self._table("stop_times.txt")
        mask = table["trip_id"] == str(trip_id)
        if not mask.any():
            raise ValueError(f"no trip {trip_id!r} in stop_times.txt")
        for column in ("arrival_time", "departure_time"):
            if column not in table.columns:
                continue
            selected = table.loc[mask, column]
            table.loc[mask, column] = [
                (
                    format_gtfs_time(parse_gtfs_time(value) + seconds)
                    if value.strip()
                    else value
                )
                for value in selected
            ]
        frequencies = self.tables.get("frequencies.txt")
        if frequencies is not None:
            fmask = frequencies["trip_id"] == str(trip_id)
            for column in ("start_time", "end_time"):
                selected = frequencies.loc[fmask, column]
                frequencies.loc[fmask, column] = [
                    format_gtfs_time(parse_gtfs_time(value) + seconds)
                    for value in selected
                ]
        return self

    def drop_route(self, route_id):
        """Remove a route and everything that references it.

        Cascades to trips, stop_times, frequencies, fare_rules,
        attributions and trip-to-trip transfers; save-time validation
        flags anything a feed references in less common ways.
        """
        route_id = str(route_id)
        routes = self._table("routes.txt")
        self.tables["routes.txt"] = routes[routes["route_id"] != route_id].reset_index(
            drop=True
        )
        trips = self.tables.get("trips.txt")
        doomed = set()
        if trips is not None:
            doomed = set(trips.loc[trips["route_id"] == route_id, "trip_id"])
            self.tables["trips.txt"] = trips[trips["route_id"] != route_id].reset_index(
                drop=True
            )
        for filename in ("stop_times.txt", "frequencies.txt"):
            table = self.tables.get(filename)
            if table is not None:
                self.tables[filename] = table[
                    ~table["trip_id"].isin(doomed)
                ].reset_index(drop=True)
        for filename, columns in (
            ("fare_rules.txt", ("route_id",)),
            ("route_networks.txt", ("route_id",)),
            ("attributions.txt", ("route_id", "trip_id")),
            (
                "transfers.txt",
                ("from_trip_id", "to_trip_id", "from_route_id", "to_route_id"),
            ),
        ):
            table = self.tables.get(filename)
            if table is None:
                continue
            keep = pd.Series(True, index=table.index)
            for column in columns:
                if column not in table.columns:
                    continue
                gone = doomed if "trip" in column else {route_id}
                keep &= ~table[column].isin(gone)
            self.tables[filename] = table[keep].reset_index(drop=True)
        return self
