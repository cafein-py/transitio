"""Merging several GTFS feeds into one."""

from __future__ import annotations

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

    timezones = set()
    defaulted = 0
    for tables, extras, prefix in zip(table_sets, extra_entries, prefixes):
        _reject_flex(tables, extras, prefix)
        agency = tables.get("agency.txt")
        if agency is not None and "agency_timezone" in agency.columns:
            timezones |= {
                value.strip() for value in agency["agency_timezone"] if value.strip()
            }
        riders = tables.get("rider_categories.txt")
        if riders is not None and "is_default_fare_category" in riders.columns:
            if (riders["is_default_fare_category"].str.strip() == "1").any():
                defaulted += 1
    if len(timezones) > 1:
        # The spec requires one agency_timezone across a dataset.
        raise ValueError(f"agency timezones differ across feeds: {sorted(timezones)}")
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
    return merged, sorted(dropped)


def merge_feeds(feeds, output, *, prefixes=None, check=True, **budgets):
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
    **budgets
        ``validate_feed`` keyword arguments.

    Returns
    -------
    dict
        The ``validate_feed`` report of the written feed, with a
        ``"dropped_files"`` key listing what the merge discarded.
    """
    from transitio.edit import FeedBuilder, FeedEditor

    feeds = list(feeds)
    if len(feeds) < 2:
        raise ValueError("need at least two feeds to merge")
    table_sets = []
    extra_entries = []
    for feed in feeds:
        tables = getattr(feed, "tables", None)
        if tables is None:
            editor = FeedEditor(feed)
            tables = editor.tables
            extras = editor._extra_entries
        else:
            extras = getattr(feed, "_extra_entries", {})
        table_sets.append(tables)
        extra_entries.append(list(extras))
    tables, dropped = merge_tables(
        table_sets, prefixes=prefixes, extra_entries=extra_entries
    )
    builder = FeedBuilder()
    builder.tables = tables
    try:
        report = builder.save(output, check=check, **budgets)
    except InvalidFeedError as error:
        error.report["dropped_files"] = dropped
        raise
    report["dropped_files"] = dropped
    return report
