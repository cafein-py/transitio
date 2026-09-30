"""Merging several GTFS feeds into one."""

from __future__ import annotations

import collections
import datetime
import json
import math
import pathlib
import tempfile
import warnings

import pandas as pd

from transitio.exceptions import InvalidFeedError
from transitio.gtfs._duplicates import drop_duplicate_trips
from transitio.validate import _structure

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
# Stops looked up per input to locate its time zone, one tzfpy call each.
_ZONE_SAMPLE = 500


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


def merge_tables(
    table_sets, *, prefixes=None, extra_entries=None, duplicate_trips="drop"
):
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
    dataset-wide (all-blank) ``attributions.txt`` row. Every input keeps
    its default rider categories (``is_default_fare_category``), as GTFS
    sets the default per fare product; a fare product with a blank
    ``rider_category_id`` is one of those wildcard scopes and then sees
    each input's default.

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
    :func:`merge_feeds` leaves out the inputs of another time zone instead
    by default.

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
    duplicate_trips : {"drop", "keep"}, default "drop"
        ``"drop"`` leaves out each input's trips that repeat trips kept
        from the inputs before it: same route key, stops, times and pickup
        and drop-off behaviour (see
        :func:`~transitio.gtfs._schedule.trip_signatures`). Headsigns,
        short names, ``shape_id`` and shape geometry, ``timepoint`` and
        ``shape_dist_traveled`` may differ; the earlier trip's are kept.
        On each date, one earlier trip covers one later trip, and a later
        trip goes only when covered on every date it runs; a block
        (``block_id``) of several trips goes only when one earlier block
        covers it trip for trip. Frequency-based trips, trips in a
        trip-specific transfer and trips of a calendar spanning over
        40,000 days are never compared. A dropped trip's rows go with it,
        and each of its stops a kept trip still serves is linked both ways
        to the earlier trip's stop (``transfer_type`` 2). ``"keep"`` keeps
        every trip.

    Returns
    -------
    tuple
        ``(tables, dropped)`` — the merged tables dict and the sorted
        list of file names discarded by the merge.
    """
    tables, dropped, _ = _merge_tables(
        table_sets,
        prefixes=prefixes,
        extra_entries=extra_entries,
        duplicate_trips=duplicate_trips,
    )
    return tables, dropped


def _merge_tables(
    table_sets,
    *,
    prefixes,
    extra_entries,
    duplicate_trips="drop",
    interval=None,
    classes=None,
    labels=None,
    positions=None,
    allow_single=False,
):
    """:func:`merge_tables`, plus the details the merge report carries;
    ``interval`` and ``classes`` may come from a caller that compared the
    zones already, ``labels`` name the inputs in a refusal, ``positions``
    are their positions in the report, and ``allow_single`` lets one input
    through, prefixed as any other."""
    if duplicate_trips not in ("drop", "keep"):
        raise ValueError(
            f"duplicate_trips must be 'drop' or 'keep', not {duplicate_trips!r}"
        )
    table_sets = list(table_sets)
    if len(table_sets) < (1 if allow_single else 2):
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
    for tables, extras, prefix in zip(table_sets, extra_entries, prefixes):
        _reject_flex(tables, extras, prefix)
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

    dropped = set()
    parts = {}
    prefixed = []
    for tables, extras, prefix in zip(table_sets, extra_entries, prefixes):
        dropped.update(extras)
        prefixed.append(_prefix_feed(tables, prefix, dropped))
        for filename, table in prefixed[-1].items():
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
    duplicates = {"dropped": 0, "by_feed": {}, "unexpanded_services": 0}
    duplicates["stop_links"] = 0
    at = range(len(prefixed)) if positions is None else positions
    if duplicate_trips == "drop":
        duplicates = drop_duplicate_trips(merged, prefixed, at)
    details = {
        "timezone_interval": (
            None if interval is None else [instant.isoformat() for instant in interval]
        ),
        "timezone_aliases": aliases,
        "duplicate_trips": duplicates,
        "rider_defaults": _rider_defaults(prefixed, at),
    }
    return merged, sorted(dropped), details


def _ids_where(table, column, selector, value):
    """The distinct non-blank ``column`` values, sorted, of the rows whose
    ``selector`` is ``value`` once stripped; a missing ``selector`` is blank."""
    if table is None or column not in table.columns:
        return []
    ids = table[column]
    if selector in table.columns:
        ids = ids[table[selector].str.strip() == value]
    elif value:
        return []
    return sorted(set(ids[ids.str.strip() != ""]))


def _rider_defaults(prefixed, positions):
    """Each input's default rider categories and the fare products open to
    every rider category, by input position, for the inputs with either;
    ``[]`` unless two or more inputs declare a default."""
    entries = [
        {
            "feed": position,
            "defaults": _ids_where(
                tables.get("rider_categories.txt"),
                "rider_category_id",
                "is_default_fare_category",
                "1",
            ),
            "wildcard_products": _ids_where(
                tables.get("fare_products.txt"),
                "fare_product_id",
                "rider_category_id",
                "",
            ),
        }
        for position, tables in zip(positions, prefixed)
    ]
    if sum(bool(entry["defaults"]) for entry in entries) < 2:
        return []
    return [
        entry for entry in entries if entry["defaults"] or entry["wildcard_products"]
    ]


def _timezones(tables):
    agency = tables.get("agency.txt")
    if agency is None or "agency_timezone" not in agency.columns:
        return set()
    return {value.strip() for value in agency["agency_timezone"] if value.strip()}


def _stop_zone(tables):
    """The time zone holding most of an input's stops, by ``tzfpy``.

    Up to 500 stops with parseable in-range coordinates are looked up,
    evenly spaced by row; a stop at sea (an empty or ``Etc/`` answer) is not
    counted, and a tie goes to the zone met first. None when stops.txt is
    absent or no stop is located.
    """
    import tzfpy

    stops = tables.get("stops.txt")
    if stops is None or not {"stop_lat", "stop_lon"} <= set(stops.columns):
        return None
    lat = pd.to_numeric(stops["stop_lat"], errors="coerce")
    lon = pd.to_numeric(stops["stop_lon"], errors="coerce")
    inside = lat.between(-90.0, 90.0) & lon.between(-180.0, 180.0)
    step = max(1, math.ceil(inside.sum() / _ZONE_SAMPLE))
    points = zip(lon[inside].iloc[::step], lat[inside].iloc[::step])
    found = (tzfpy.get_tz(x, y) for x, y in points)
    counts = collections.Counter(z for z in found if z and not z.startswith("Etc/"))
    return counts.most_common(1)[0][0] if counts else None


def _most_declared(declared, vouched=None):
    """The key most inputs vouch for, from one set of keys per input
    (``vouched``: the keys its stops support); ties go to the key most
    inputs declare, then the earliest input's, then the least key. Without
    ``vouched``, the key most inputs declare."""
    counts, first = {}, {}
    for position, keys in enumerate(declared):
        for key in keys:
            counts[key] = counts.get(key, 0) + 1
            first.setdefault(key, position)
    support = collections.Counter(key for keys in vouched or () for key in keys)
    return min(counts, key=lambda key: (-support[key], -counts[key], first[key], key))


def _timezone_outliers(table_sets, classes=None, located=None):
    """``{position: [time zones]}`` for the feeds declaring a time zone not
    equivalent to the common one; a feed that declares none differs from
    nothing.

    A feed vouches for the declared zone its stops lie in (``located``, one
    :func:`_stop_zone` per feed, which ``classes`` must cover). The common
    zone is the one most feeds vouch for; ties go to the one most feeds
    declare, then the earliest feed's, then the first by name.
    """
    declared = [_timezones(tables) for tables in table_sets]
    zones = set().union(*declared)
    if len(zones) < 2:
        return {}
    if classes is None:
        zones |= set(filter(None, located or ()))
        classes = _zone_classes(zones, _timezone_interval(table_sets))
    grouped = [{classes[zone] for zone in found} for found in declared]
    if len(set().union(*grouped)) < 2:
        return {}
    vouched = None
    if located is not None:
        vouched = [
            {group for group in groups if zone in group}
            for groups, zone in zip(grouped, located)
        ]
    common = _most_declared(grouped, vouched)
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


def _identity(notice, lookup):
    """A notice's identity across feeds, ``(code, context)``, and the sorted
    positions of the inputs its ids name.

    Row numbers are left out, as concatenation shifts rows. Ids are the
    values under keys ending in ``Id``, ``IdA`` or ``IdB``, under
    ``parentStation``, and under ``fieldValue`` when the notice's file and
    field hold ids; one reading ``"<prefix>:<rest>"``, the prefix a key of
    ``lookup``, names the input at ``lookup[prefix]`` and compares as
    ``<rest>``.
    """
    context = notice.get("context") or {}
    if "childFieldName" in context:
        column = (context.get("childFilename"), context.get("childFieldName"))
    else:
        column = (context.get("filename"), context.get("fieldName"))
    value_is_id = column[1] in _ID_COLUMNS.get(column[0], ())
    kept, named = {}, set()
    for key, value in context.items():
        if "RowNumber" in key or "rowNumber" in key:
            continue
        is_id = key.endswith(("Id", "IdA", "IdB")) or key == "parentStation"
        if isinstance(value, str) and (is_id or key == "fieldValue" and value_is_id):
            prefix, colon, rest = value.partition(":")
            if colon and prefix in lookup:
                named.add(lookup[prefix])
                value = rest
        kept[key] = value
    return (notice["code"], json.dumps(kept, sort_keys=True)), tuple(sorted(named))


def _split_errors(merged, inputs, prefixes):
    """Split the merged feed's error-severity notices into those inherited
    from each input and those the merge introduced.

    ``inputs`` holds one validation per input, in ``prefixes`` order, and
    notices compare by :func:`_identity`. A notice naming one input is
    inherited when that input carries it, one naming several is
    introduced, and one naming none goes to the earliest input carrying
    it. Named notices match first, and each input notice matches once.
    Returns ``(inherited, introduced)``: a ``Counter`` of codes per input,
    and one of the codes left.
    """
    lookup = {prefix: position for position, prefix in enumerate(prefixes)}
    pools = [
        _structure._errors(validation, key=lambda notice: _identity(notice, {})[0])
        for validation in inputs
    ]
    holders = {}
    for position, pool in enumerate(pools):
        for identity in pool:
            holders.setdefault(identity, []).append(position)
    found = _structure._errors(merged, key=lambda notice: _identity(notice, lookup))
    # Named notices first, so an id-less one cannot take their match.
    ordered = [item for item in found.items() if item[0][1]]
    ordered += [item for item in found.items() if not item[0][1]]
    inherited = [collections.Counter() for _ in pools]
    introduced = collections.Counter()
    for (identity, named), count in ordered:
        if not named:
            candidates = holders.get(identity, ())
        else:
            candidates = named if len(named) == 1 else ()
        for position in candidates:
            matched = min(count, pools[position][identity])
            if matched:
                pools[position][identity] -= matched
                inherited[position][identity[0]] += matched
                count -= matched
        if count:
            introduced[identity[0]] += count
    return inherited, introduced


def _gate(report, check, tables, table_sets, prefixes, positions, budgets):
    """Record the merged feed's inherited and introduced error-severity
    notices on ``report`` and refuse the feed as ``check`` says; ``tables``
    are the merged tables, ``table_sets`` the inputs'."""
    from transitio.edit import FeedBuilder

    report["inherited_errors"] = []
    report["introduced_errors"] = {"errors": 0, "codes": {}}
    if not any(notice["severity"] == "ERROR" for notice in report["notices"]):
        return
    budgets = dict(budgets)
    budgets["max_notices_per_file"] = max(
        budgets.get("max_notices_per_file") or 0, _structure.CERTIFY_NOTICE_BUDGET
    )
    validations, sources = [report], list(enumerate(table_sets))
    if _structure._unreliable(report):
        validations, sources = [], [("merged", tables), *sources]
    with tempfile.TemporaryDirectory(prefix="transitio-merge-") as workdir:
        for name, source in sources:
            if validations and _structure._unreliable(validations[-1]):
                break
            builder = FeedBuilder()
            builder.tables = source
            path = pathlib.Path(workdir, f"{name}.zip")
            validations.append(
                builder.save(path, check=False, change_log=False, **budgets)
            )
            path.unlink()
    if any(_structure._unreliable(validation) for validation in validations):
        report["inherited_errors"] = report["introduced_errors"] = None
        message = (
            "cannot tell inherited errors from introduced ones: validation was "
            "sampled or truncated; raise the budgets or pass check=False"
        )
    else:
        inherited, introduced = _split_errors(validations[0], validations[1:], prefixes)
        report["inherited_errors"] = [
            {
                "feed": position,
                "errors": sum(codes.values()),
                "codes": dict(sorted(codes.items())),
            }
            for position, codes in zip(positions, inherited)
            if codes
        ]
        new = sum(introduced.values())
        report["introduced_errors"] = {
            "errors": new,
            "codes": dict(sorted(introduced.items())),
        }
        carried = sum(entry["errors"] for entry in report["inherited_errors"])
        if check == "strict":
            if not carried + new:
                return
            message = (
                f"merged feed has {carried + new} error-severity notices, {carried} "
                "of them inherited from the inputs (check=True refuses only those "
                "the merge introduced)"
            )
        elif new:
            listed = ", ".join(
                f"{code} {count}"
                for code, count in sorted(
                    introduced.items(), key=lambda item: (-item[1], item[0])
                )
            )
            message = (
                f"the merge introduced {new} error-severity notices ({listed}); "
                f"{carried} more are inherited from the inputs, see "
                "report['inherited_errors'] (check=False skips this gate)"
            )
        else:
            return
    error = InvalidFeedError(message)
    error.report = report
    raise error


def merge_feeds(
    feeds,
    output,
    *,
    prefixes=None,
    check=True,
    timezones="skip",
    duplicate_trips="drop",
    **budgets,
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
    check : {True, "strict", False}, default True
        ``True`` raises :class:`~transitio.exceptions.InvalidFeedError`
        when the merge introduced ERROR-severity notices, those of the
        written feed that its inputs do not carry; ``"strict"`` raises on
        any ERROR-severity notice and ``False`` never raises. The report is
        on the exception and the file is still written. The inputs are
        validated only when the written feed has an ERROR-severity notice:
        each as the merge read it, with ``budgets`` and at least 1,000,000
        notices per file, and the merged tables again with those budgets
        when the written feed's validation was sampled or truncated. Notices
        compare by code and context, without row numbers and with ids
        read without their prefix; a notice naming no input's ids counts
        against the earliest input carrying it. When a validation is still
        sampled or truncated, inherited notices cannot be told from
        introduced ones, and ``True`` and ``"strict"`` both raise. Unless
        ``check`` is ``False``, the tables of in-memory inputs are copied
        first, so the merge holds them twice.
    timezones : {"skip", "refuse"}, default "skip"
        Feeds declaring ``agency_timezone`` names that are not equivalent
        (see :func:`merge_tables`) cannot share one dataset. ``"skip"``
        leaves out, with a ``UserWarning``, the feeds whose time zone is
        not equivalent to the common one and merges the rest, each keeping
        the prefix it had among all the inputs, even when one feed is left.
        A feed vouches for the zone it declares when most of its stops lie
        in an equivalent zone, by ``tzfpy`` over at most 500 of them; the
        common zone is the one most feeds vouch for, then the one most
        feeds declare, then the earliest feed's, then the first by name.
        When every feed would be left out, it raises as ``"refuse"`` does.
        ``"refuse"`` raises :class:`~transitio.exceptions.InvalidFeedError`
        (also a ``ValueError``) naming each input with its zones, by
        position, prefix and path.
    duplicate_trips : {"drop", "keep"}, default "drop"
        Whether to leave out the trips an input repeats from the inputs
        before it (see :func:`merge_tables`).
    **budgets
        ``validate_feed`` keyword arguments.

    Returns
    -------
    dict
        The ``validate_feed`` report of the written feed, with a
        ``"dropped_files"`` key listing what the merge discarded and a
        ``"skipped_feeds"`` key listing the feeds left out, one
        ``{"feed": <input position>, "timezones": [...], "stop_timezone":
        <zone of most of its stops, or None>}`` each, and a
        ``"header_fixes"`` key listing the header names normalised when
        an input was read (see :class:`~transitio.edit.FeedEditor`), one
        ``{"feed": <input position>, "file": ..., "columns": [{"from":
        [<original names>], "to": <name>}, ...]}`` per input and file, and
        a ``"trimmed_values"`` key counting the values stripped of
        surrounding whitespace, one ``{"feed": <input position>, "file":
        ..., "count": <n>}`` per input and file.
        ``"timezone_interval"`` gives the UTC instants ``[start, end]``
        (ISO 8601) over which time zone names are compared, and
        ``"timezone_aliases"`` maps each ``agency_timezone`` name replaced
        to the name used. ``"duplicate_trips"`` counts the trips dropped
        as repeats, ``{"dropped": <n>, "by_feed": {<input position>: <n>},
        "unexpanded_services": <n>, "stop_links": <n>}``: the services not
        expanded, whose trips were never compared, and the stop pairs
        linked. ``"rider_defaults"`` is ``[]`` unless two or more inputs
        declare a default rider category; it then lists one ``{"feed":
        <input position>, "defaults": [<rider_category_id>, ...],
        "wildcard_products": [<fare_product_id>, ...]}`` per input with a
        default or with fare products left open to every rider category
        (blank ``rider_category_id``), which the merged feed makes eligible
        for several defaults; ids are the merged ones, sorted.
        ``"inherited_errors"`` lists the ERROR-severity notices
        the written feed carries from its inputs, one ``{"feed": <input
        position>, "errors": <n>, "codes": {<code>: <n>}}`` per input with
        any, and ``"introduced_errors"`` counts the others, ``{"errors":
        <n>, "codes": {<code>: <n>}}``; both are ``None`` with
        ``check=False`` and when the validations were sampled or
        truncated.
    """
    from transitio.edit import FeedBuilder, FeedEditor

    if timezones not in ("refuse", "skip"):
        raise ValueError(f"timezones must be 'refuse' or 'skip', not {timezones!r}")
    if not (isinstance(check, bool) or check == "strict"):
        raise ValueError(f"check must be True, False or 'strict', not {check!r}")
    feeds = list(feeds)
    if len(feeds) < 2:
        raise ValueError("need at least two feeds to merge")
    table_sets = []
    extra_entries = []
    header_fixes = []
    trimmed_values = []
    sources = []
    for position, feed in enumerate(feeds):
        tables = getattr(feed, "tables", None)
        if tables is None:
            feed = FeedEditor(feed)
            tables = feed.tables
        elif check:
            # The caller's tables may change before the gate validates them.
            tables = {name: table.copy() for name, table in tables.items()}
        fixes = getattr(feed, "_header_fixes", {})
        header_fixes.extend(
            {"feed": position, "file": name, "columns": fixes[name]}
            for name in sorted(fixes)
        )
        counts = getattr(feed, "_value_fixes", {})
        trimmed_values.extend(
            {"feed": position, "file": name, "count": counts[name]}
            for name in sorted(counts)
        )
        table_sets.append(tables)
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
    positions = list(range(len(table_sets)))
    outliers = {}
    if timezones == "skip" and classes and len(set(classes.values())) > 1:
        located = [_stop_zone(tables) for tables in table_sets]
        more = set(filter(None, located)) - zones
        if more:
            classes = _zone_classes(zones | more, interval)
        outliers = _timezone_outliers(table_sets, classes, located)
    kept = [i for i in positions if i not in outliers]
    # With every feed left out, the merge below refuses them all.
    if outliers and kept:
        skipped = [
            {"feed": position, "timezones": found, "stop_timezone": located[position]}
            for position, found in sorted(outliers.items())
        ]
        left = "; ".join(
            f"{labels[position]}: {', '.join(found)}"
            + (f", stops in {located[position]}" if located[position] else "")
            for position, found in sorted(outliers.items())
        )
        warnings.warn(
            f"left out {left}; see report['skipped_feeds'] "
            "(timezones='refuse' raises instead)",
            UserWarning,
            stacklevel=2,
        )
        table_sets = [table_sets[i] for i in kept]
        extra_entries = [extra_entries[i] for i in kept]
        names = [names[i] for i in kept]
        labels = [labels[i] for i in kept]
        positions = kept
    tables, dropped, details = _merge_tables(
        table_sets,
        prefixes=names,
        extra_entries=extra_entries,
        duplicate_trips=duplicate_trips,
        interval=interval,
        classes=classes,
        labels=labels,
        positions=positions,
        allow_single=True,
    )
    extra = {
        "dropped_files": dropped,
        "skipped_feeds": skipped,
        "header_fixes": header_fixes,
        "trimmed_values": trimmed_values,
        **details,
    }
    builder = FeedBuilder()
    builder.tables = tables
    report = builder.save(output, check=False, **budgets)
    report.update(extra)
    if check:
        _gate(report, check, tables, table_sets, names, positions, budgets)
    else:
        report["inherited_errors"] = report["introduced_errors"] = None
    return report
