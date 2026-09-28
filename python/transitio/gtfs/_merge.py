"""Merging several GTFS feeds into one."""

from __future__ import annotations

import datetime
import math

import pandas as pd

from transitio.exceptions import InvalidFeedError

# Every standard column holding a feed-scoped identifier or a reference
# to one; all get the feed prefix so same-valued ids from different
# feeds never collide. Unknown files and columns pass through unprefixed.
_ID_COLUMNS = {
    "agency.txt": ("agency_id",),
    "areas.txt": ("area_id",),
    "attributions.txt": ("attribution_id", "agency_id", "route_id", "trip_id"),
    "booking_rules.txt": ("booking_rule_id",),
    "calendar.txt": ("service_id",),
    "calendar_dates.txt": ("service_id",),
    "fare_attributes.txt": ("fare_id", "agency_id"),
    "fare_leg_join_rules.txt": (
        "from_network_id",
        "to_network_id",
        "from_stop_id",
        "to_stop_id",
    ),
    "fare_leg_rules.txt": (
        "leg_group_id",
        "network_id",
        "from_area_id",
        "to_area_id",
        "from_timeframe_group_id",
        "to_timeframe_group_id",
        "fare_product_id",
    ),
    "fare_media.txt": ("fare_media_id",),
    "fare_products.txt": ("fare_product_id", "fare_media_id", "rider_category_id"),
    "fare_rules.txt": (
        "fare_id",
        "route_id",
        "origin_id",
        "destination_id",
        "contains_id",
    ),
    "fare_transfer_rules.txt": (
        "from_leg_group_id",
        "to_leg_group_id",
        "fare_product_id",
    ),
    "frequencies.txt": ("trip_id",),
    "levels.txt": ("level_id",),
    "location_group_stops.txt": ("location_group_id", "stop_id"),
    "location_groups.txt": ("location_group_id",),
    "networks.txt": ("network_id",),
    "pathways.txt": ("pathway_id", "from_stop_id", "to_stop_id"),
    "rider_categories.txt": ("rider_category_id",),
    "route_networks.txt": ("network_id", "route_id"),
    "routes.txt": ("route_id", "agency_id", "network_id"),
    "shapes.txt": ("shape_id",),
    "stop_areas.txt": ("area_id", "stop_id"),
    "stop_times.txt": (
        "trip_id",
        "stop_id",
        "location_group_id",
        "pickup_booking_rule_id",
        "drop_off_booking_rule_id",
    ),
    "stops.txt": ("stop_id", "parent_station", "zone_id", "level_id"),
    "timeframes.txt": ("timeframe_group_id", "service_id"),
    "transfers.txt": (
        "from_stop_id",
        "to_stop_id",
        "from_route_id",
        "to_route_id",
        "from_trip_id",
        "to_trip_id",
    ),
    "trips.txt": ("route_id", "service_id", "trip_id", "shape_id", "block_id"),
}

# Per-source-feed metadata that cannot describe a merger (feed_info) or
# whose record references break under id renaming (translations).
_DROPPED_TABLES = ("feed_info.txt", "translations.txt")

# UTC offsets reach from -12:00 to +14:00, so these margins take in every
# zone's local service day.
_EAST_MARGIN = datetime.timedelta(hours=14)
_WEST_MARGIN = datetime.timedelta(hours=12)
# Time zones are compared no further back and ahead of today than this.
_PAST_YEARS = 20
_FUTURE_YEARS = 10


class _TimezoneRefusal(InvalidFeedError, ValueError):
    """Inputs whose agency time zones cannot share one dataset."""


def _clean_prefixes(prefixes, count):
    if prefixes is None:
        return [f"f{index}" for index in range(1, count + 1)]
    prefixes = [str(prefix).strip() for prefix in prefixes]
    if len(prefixes) != count:
        raise ValueError(f"{count} feeds but {len(prefixes)} prefixes")
    if any(not prefix for prefix in prefixes):
        raise ValueError("prefixes must be non-empty")
    if any(":" in prefix for prefix in prefixes):
        raise ValueError("prefixes must not contain ':'")
    if len(set(prefixes)) != len(prefixes):
        raise ValueError("prefixes must be unique")
    return prefixes


def _reject_flex(tables, extras, label):
    # GTFS-Flex geometries live outside the CSV tables, so their feature
    # ids cannot be re-namespaced; merging would silently corrupt them.
    if "locations.geojson" in tables or "locations.geojson" in extras:
        raise ValueError(
            f"feed {label!r} carries locations.geojson (GTFS-Flex); "
            "merging Flex feeds is not supported"
        )
    stop_times = tables.get("stop_times.txt")
    if stop_times is not None and "location_id" in stop_times.columns:
        if (stop_times["location_id"].str.strip() != "").any():
            raise ValueError(
                f"feed {label!r} references GTFS-Flex locations in "
                "stop_times.location_id; merging Flex feeds is not supported"
            )


def _backfill_agency(tables, prefix):
    # A single-agency feed may leave agency_id blank (and blank
    # references to it); make both explicit so two such feeds do not
    # merge into a multi-agency feed with ambiguous blanks.
    agency = tables.get("agency.txt")
    if agency is None or len(agency) != 1:
        return
    if "agency_id" not in agency.columns:
        agency["agency_id"] = ""
    agency_id = str(agency["agency_id"].iloc[0])
    if not agency_id.strip():
        agency_id = prefix
        agency["agency_id"] = agency_id
    for filename in ("routes.txt", "fare_attributes.txt"):
        table = tables.get(filename)
        if table is None:
            continue
        if "agency_id" not in table.columns:
            table["agency_id"] = ""
        blank = table["agency_id"].str.strip() == ""
        table.loc[blank, "agency_id"] = agency_id


def _prefix_feed(tables, prefix, dropped):
    out = {}
    for filename, table in tables.items():
        if filename in _DROPPED_TABLES:
            dropped.add(filename)
            continue
        table = table.copy()
        for column in _ID_COLUMNS.get(filename, ()):
            if column not in table.columns:
                continue
            values = table[column]
            mask = values.str.strip() != ""
            table.loc[mask, column] = prefix + ":" + values[mask]
        out[filename] = table
    _backfill_agency(out, prefix)
    return out


def _normalise_networks(tables):
    # GTFS forbids routes.network_id alongside route_networks.txt; when
    # the inputs mix the two, move the column into route_networks rows.
    routes = tables.get("routes.txt")
    route_networks = tables.get("route_networks.txt")
    if routes is None or route_networks is None:
        return
    if "network_id" not in routes.columns:
        return
    mask = routes["network_id"].str.strip() != ""
    moved = routes.loc[mask, ["route_id", "network_id"]]
    tables["routes.txt"] = routes.drop(columns=["network_id"])
    if len(moved):
        tables["route_networks.txt"] = pd.concat(
            [route_networks, moved], ignore_index=True
        ).fillna("")


def merge_tables(table_sets, *, prefixes=None, extra_entries=None):
    """Merge several feeds' tables into one referentially consistent set.

    Every id (and every standard reference to one) gets the feed's
    prefix prepended as ``"<prefix>:"``, so ids from different feeds
    never collide and the original id is recoverable at the first
    ``:``. Per filename, rows are concatenated in input order over the
    union of columns (missing columns fill with ``""``).
    ``feed_info.txt`` and ``translations.txt`` are dropped (they
    describe one source feed and cannot survive id renaming); GTFS-Flex
    feeds are refused. Two residuals are documented rather than
    handled: nonstandard columns pass through unprefixed (id references
    in them go stale), and wildcard fare scopes — blank optional
    selectors in fare tables, or a fare with no ``fare_rules`` rows —
    widen from "this feed" to the whole merged feed, as does a
    dataset-wide (all-blank) ``attributions.txt`` row.

    The inputs' ``agency_timezone`` names must be equivalent, or the
    merge raises :class:`~transitio.exceptions.InvalidFeedError`, which is
    also a ``ValueError``, naming each input's zones. Two names are
    equivalent when the tz database (``zoneinfo``) knows both and their
    UTC offsets are equal at every quarter hour of one interval: from the
    earliest service date's start anywhere on Earth to the latest service
    date's end, extended by the latest stop time (or frequency window end
    plus its trip's span) in days, rounded up. Only service dates from 20
    years before today to 10 years after count, and the interval is
    clipped to them; with no service dates there, it is today and the
    year after. An unknown name is equivalent only to itself. The
    merged ``agency.txt`` then uses the name most inputs declare (ties:
    the earliest input's) for every agency; ``stop_timezone`` is kept.

    Parameters
    ----------
    table_sets : sequence of dict
        One ``tables`` dict per feed (GTFS filename -> string
        DataFrame), as on :class:`~transitio.edit.FeedEditor` /
        :class:`~transitio.edit.FeedBuilder`.
    prefixes : sequence of str, optional
        One id prefix per feed; whitespace-stripped, then required to
        be unique, non-empty and colon-free. Default ``f1..fN``.
    extra_entries : sequence of sequence of str, optional
        Per feed, the archive entries that are not GTFS tables; they
        are reported as dropped (``locations.geojson`` is refused).

    Returns
    -------
    tuple
        ``(tables, dropped)`` — the merged tables dict and the sorted
        list of file names discarded by the merge.
    """
    tables, dropped, _ = _merge_tables(
        table_sets, prefixes=prefixes, extra_entries=extra_entries
    )
    return tables, dropped


def _merge_tables(
    table_sets, *, prefixes, extra_entries, interval=None, classes=None, labels=None
):
    """:func:`merge_tables`, plus the time zone details the merge report
    carries; ``interval`` and ``classes`` may come from a caller that
    compared the zones already, and ``labels`` name the inputs in a
    refusal."""
    table_sets = list(table_sets)
    if len(table_sets) < 2:
        raise ValueError("need at least two feeds to merge")
    prefixes = _clean_prefixes(prefixes, len(table_sets))
    if extra_entries is None:
        extra_entries = [()] * len(table_sets)
    else:
        extra_entries = [tuple(extras) for extras in extra_entries]
        if len(extra_entries) != len(table_sets):
            raise ValueError("extra_entries must match the number of feeds")

    if labels is None:
        labels = [f"feed {i} ({prefix})" for i, prefix in enumerate(prefixes)]
    defaulted = 0
    for tables, extras, prefix in zip(table_sets, extra_entries, prefixes):
        _reject_flex(tables, extras, prefix)
        riders = tables.get("rider_categories.txt")
        if riders is not None and "is_default_fare_category" in riders.columns:
            if (riders["is_default_fare_category"].str.strip() == "1").any():
                defaulted += 1
    declared = [_timezones(tables) for tables in table_sets]
    zones = set().union(*declared)
    aliases = {}
    if len(zones) > 1:
        if classes is None:
            interval = _timezone_interval(table_sets)
            classes = _zone_classes(zones, interval)
        if len({classes[zone] for zone in zones}) > 1:
            # The spec requires one agency_timezone across a dataset.
            raise _TimezoneRefusal(
                "agency timezones differ across feeds: "
                + "; ".join(
                    f"{label}: {', '.join(sorted(found))}"
                    for label, found in zip(labels, declared)
                    if found
                )
            )
        used = _most_declared(declared)
        aliases = {zone: used for zone in sorted(zones) if zone != used}
    if defaulted > 1:
        raise ValueError(
            "more than one feed declares a default rider category "
            "(is_default_fare_category); these fare defaults cannot be merged"
        )

    dropped = set()
    parts = {}
    for tables, extras, prefix in zip(table_sets, extra_entries, prefixes):
        dropped.update(extras)
        for filename, table in _prefix_feed(tables, prefix, dropped).items():
            parts.setdefault(filename, []).append(table)
    merged = {
        filename: pd.concat(tables, ignore_index=True).fillna("")
        for filename, tables in parts.items()
    }
    _normalise_networks(merged)
    if aliases:
        agency = merged["agency.txt"]
        filled = agency["agency_timezone"].str.strip() != ""
        agency.loc[filled, "agency_timezone"] = used
    details = {
        "timezone_interval": (
            None if interval is None else [instant.isoformat() for instant in interval]
        ),
        "timezone_aliases": aliases,
    }
    return merged, sorted(dropped), details


def _timezones(tables):
    agency = tables.get("agency.txt")
    if agency is None or "agency_timezone" not in agency.columns:
        return set()
    return {value.strip() for value in agency["agency_timezone"] if value.strip()}


def _most_declared(declared):
    """The key most inputs declare (ties: the earliest input's, then the
    least key), from one set of keys per input."""
    counts, first = {}, {}
    for position, keys in enumerate(declared):
        for key in keys:
            counts[key] = counts.get(key, 0) + 1
            first.setdefault(key, position)
    return min(counts, key=lambda key: (-counts[key], first[key], key))


def _timezone_outliers(table_sets, classes=None):
    """``{position: [time zones]}`` for the feeds declaring a time zone not
    equivalent to the one most feeds declare (ties: the earliest feed's,
    then the first by name); a feed that declares none differs from
    nothing."""
    declared = [_timezones(tables) for tables in table_sets]
    zones = set().union(*declared)
    if len(zones) < 2:
        return {}
    if classes is None:
        classes = _zone_classes(zones, _timezone_interval(table_sets))
    grouped = [{classes[zone] for zone in found} for found in declared]
    if len(set().union(*grouped)) < 2:
        return {}
    common = _most_declared(grouped)
    return {
        position: sorted(found)
        for position, (found, groups) in enumerate(zip(declared, grouped))
        if groups - {common}
    }


def _today():
    return datetime.datetime.now(datetime.timezone.utc).date()


def _years_on(day, years):
    try:
        return day.replace(year=day.year + years)
    except ValueError:  # 29 February
        return day.replace(year=day.year + years, day=28)


def _midnight(day):
    return datetime.datetime.combine(day, datetime.time(), datetime.timezone.utc)


def _latest_service_time(tables):
    """Seconds after midnight of the latest service: the largest stop time,
    or a frequency window's end plus its template trip's span."""
    from transitio.gtfs._schedule import clock_seconds

    stop_times = tables.get("stop_times.txt", pd.DataFrame())
    times = pd.DataFrame(
        {
            column: clock_seconds(stop_times[column])
            for column in ("arrival_time", "departure_time")
            if column in stop_times.columns
        },
        index=stop_times.index,
    )
    latest = [times.max().max()]
    frequencies = tables.get("frequencies.txt")
    if frequencies is not None and "end_time" in frequencies.columns:
        span = 0.0
        if "trip_id" in frequencies.columns and "trip_id" in stop_times.columns:
            template = stop_times["trip_id"].isin(frequencies["trip_id"])
            trips = stop_times.loc[template, "trip_id"]
            rows = times[template]
            spans = rows.max(axis=1).groupby(trips).max()
            spans -= rows.min(axis=1).groupby(trips).min()
            span = frequencies["trip_id"].map(spans).fillna(0.0)
        latest.append((clock_seconds(frequencies["end_time"]) + span).max())
    return max((value for value in latest if pd.notna(value)), default=0.0)


def _timezone_interval(table_sets):
    """The UTC instants ``(start, end)`` over which time zones are compared.

    Service dates count from 20 years before today to 10 years after. The
    interval runs from 00:00 UTC on the earliest of them, less 14 hours, to
    00:00 UTC on the latest plus ``d`` days and 12 hours, ``d`` being the
    latest service-relative time in days, rounded up and at least 1, and is
    clipped to the same 30 years. Inputs with no service dates there use
    today and the year after.
    """
    from transitio.gtfs._schedule import service_span

    today = _today()
    low, high = _years_on(today, -_PAST_YEARS), _years_on(today, _FUTURE_YEARS)
    spans = [service_span(tables, (low, high)) for tables in table_sets]
    spans = [span for span in spans if span is not None]
    if not spans:
        return _midnight(today), _midnight(_years_on(today, 1))
    latest = max(_latest_service_time(tables) for tables in table_sets)
    days = datetime.timedelta(days=max(1, math.ceil(latest / 86400)))
    start = _midnight(min(first for first, _ in spans)) - _EAST_MARGIN
    end = _midnight(max(last for _, last in spans)) + days + _WEST_MARGIN
    return max(start, _midnight(low)), min(end, _midnight(high))


def _zone_classes(names, interval):
    """``{name: class}`` for time zone names, a class being the sorted names
    whose UTC offsets agree at every quarter hour of ``interval``; a name the
    tz database does not know forms a class of its own."""
    import hashlib
    import zoneinfo

    known = zoneinfo.available_timezones()
    instants = pd.date_range(*interval, freq="15min")
    universal = instants.tz_localize(None)
    classes = {}
    for name in sorted(names):
        key = ("unknown", name)
        if name in known:
            try:
                zone = zoneinfo.ZoneInfo(name)
            except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
                zone = None
            if zone is not None:
                # Offset vectors compare by digest, so only one is held.
                offsets = instants.tz_convert(zone).tz_localize(None) - universal
                key = hashlib.sha256(offsets.to_numpy().tobytes()).digest()
        classes.setdefault(key, []).append(name)
    return {name: tuple(members) for members in classes.values() for name in members}


def merge_feeds(
    feeds, output, *, prefixes=None, check=True, timezones="refuse", **budgets
):
    """Merge GTFS feeds into one zip, written atomically and validated.

    See :func:`merge_tables` for the merge semantics (id namespacing,
    dropped files, refusals and documented residuals).

    Parameters
    ----------
    feeds : sequence
        At least two inputs, each a path to a feed zip or an object
        with ``tables`` (a :class:`~transitio.edit.FeedEditor` /
        :class:`~transitio.edit.FeedBuilder`).
    output : str or pathlib.Path
        Destination path for the merged ``.zip``.
    prefixes : sequence of str, optional
        One id prefix per feed (see :func:`merge_tables`).
    check : bool, default True
        Raise :class:`~transitio.exceptions.InvalidFeedError` when the
        validator reports ERROR-severity notices (the report is on the
        exception and the file is still written).
    timezones : {"refuse", "skip"}, default "refuse"
        Feeds declaring ``agency_timezone`` names that are not equivalent
        (see :func:`merge_tables`) cannot share one dataset. ``"refuse"``
        raises :class:`~transitio.exceptions.InvalidFeedError` (also a
        ``ValueError``) naming each input with its zones, by position,
        prefix and path; ``"skip"`` leaves out the feeds whose time zone is not
        equivalent to the one most feeds declare (ties: the earliest
        feed's, then the first by name) and merges the rest, each keeping
        the prefix it had among all the inputs. Fewer than two feeds left
        still raises.
    **budgets
        ``validate_feed`` keyword arguments.

    Returns
    -------
    dict
        The ``validate_feed`` report of the written feed, with a
        ``"dropped_files"`` key listing what the merge discarded and a
        ``"skipped_feeds"`` key listing the feeds left out, one
        ``{"feed": <input position>, "timezones": [...]}`` each, and a
        ``"header_fixes"`` key listing the header names normalised when
        an input was read (see :class:`~transitio.edit.FeedEditor`), one
        ``{"feed": <input position>, "file": ..., "columns": [{"from":
        [<original names>], "to": <name>}, ...]}`` per input and file.
        ``"timezone_interval"`` gives the UTC instants ``[start, end]``
        (ISO 8601) over which time zone names are compared, and
        ``"timezone_aliases"`` maps each ``agency_timezone`` name replaced
        to the name used.
    """
    from transitio.edit import FeedBuilder, FeedEditor

    if timezones not in ("refuse", "skip"):
        raise ValueError(f"timezones must be 'refuse' or 'skip', not {timezones!r}")
    feeds = list(feeds)
    if len(feeds) < 2:
        raise ValueError("need at least two feeds to merge")
    table_sets = []
    extra_entries = []
    header_fixes = []
    sources = []
    for position, feed in enumerate(feeds):
        if getattr(feed, "tables", None) is None:
            feed = FeedEditor(feed)
        fixes = getattr(feed, "_header_fixes", {})
        header_fixes.extend(
            {"feed": position, "file": name, "columns": fixes[name]}
            for name in sorted(fixes)
        )
        table_sets.append(feed.tables)
        extra_entries.append(list(getattr(feed, "_extra_entries", {})))
        sources.append(getattr(feed, "source", None))
    names = _clean_prefixes(prefixes, len(table_sets))
    labels = [
        (
            f"feed {position} ({name})"
            if source is None
            else f"feed {position} ({name}, {source})"
        )
        for position, (name, source) in enumerate(zip(names, sources))
    ]
    interval, classes = _timezone_interval(table_sets), None
    zones = set().union(*(_timezones(tables) for tables in table_sets))
    if len(zones) > 1:
        classes = _zone_classes(zones, interval)
    skipped = []
    outliers = _timezone_outliers(table_sets, classes) if timezones == "skip" else {}
    if outliers:
        kept = [i for i in range(len(table_sets)) if i not in outliers]
        if len(kept) < 2:
            raise ValueError(
                f"fewer than two feeds share a time zone: {sorted(outliers.values())}"
            )
        skipped = [
            {"feed": position, "timezones": zones}
            for position, zones in sorted(outliers.items())
        ]
        table_sets = [table_sets[i] for i in kept]
        extra_entries = [extra_entries[i] for i in kept]
        names = [names[i] for i in kept]
        labels = [labels[i] for i in kept]
    tables, dropped, details = _merge_tables(
        table_sets,
        prefixes=names,
        extra_entries=extra_entries,
        interval=interval,
        classes=classes,
        labels=labels,
    )
    extra = {
        "dropped_files": dropped,
        "skipped_feeds": skipped,
        "header_fixes": header_fixes,
        **details,
    }
    builder = FeedBuilder()
    builder.tables = tables
    try:
        report = builder.save(output, check=check, **budgets)
    except InvalidFeedError as error:
        error.report.update(extra)
        raise
    report.update(extra)
    return report
