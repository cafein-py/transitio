"""Vectorised readings of GTFS clock times, service dates and trips."""

from __future__ import annotations

import numpy as np
import pandas as pd

_CLOCK = r"\d+:[0-5]\d:[0-5]\d"
# Longer hours saturate here, so the integer conversion never overflows.
_MAX_HOURS = 999_999
_SINGLE_DIGIT_HOUR = r"\d:\d\d:\d\d"
_DATE = r"\d{8}"
_SEQUENCE = r"\d{1,18}"
_WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

#: A calendar row spanning more days than this (about 110 years) is not
#: expanded.
MAX_SERVICE_DAYS = 40_000
#: Days expanded from one calendar.txt at most; the longest rows past it are
#: not expanded.
MAX_EXPANDED_DAYS = 50_000_000
#: A calendar row whose end is this many days or more after its start is a
#: placeholder rather than a timetable period, as the validator's
#: ``calendar_span_truncated`` notice counts it.
PLACEHOLDER_DAYS = 4000
# A trip signature joins two 64-bit hashes taken under these keys.
_HASH_KEYS = ("transitio-trip-1", "transitio-trip-2")
# Legal-form words an agency key drops from the end of a name.
_LEGAL_FORMS = frozenset(
    "ltd limited inc llc plc corp gmbh ag kg sa sas srl spa oy oyj ab as asa bv nv".split()
)


def _matching(text, pattern):
    return np.array(text.str.fullmatch(pattern).fillna(False), dtype=bool)


def clock_seconds(values):
    """Seconds after midnight of each ``H:MM:SS`` value, hours past
    999,999 counted as 999,999; NaN where malformed."""
    text = values.str.strip()
    valid = _matching(text, _CLOCK)
    clock = text[valid]
    hours = clock.str.slice(stop=-6)
    hours = hours.where(hours.str.len() <= len(str(_MAX_HOURS)), str(_MAX_HOURS))
    seconds = np.full(len(text), np.nan)
    seconds[valid] = (
        hours.astype("int64") * 3600
        + clock.str.slice(-5, -3).astype("int64") * 60
        + clock.str.slice(-2).astype("int64")
    ).to_numpy()
    return pd.Series(seconds, index=values.index)


def parse_dates(values):
    """``datetime64[D]`` of each ``YYYYMMDD`` value; NaT where it names no date."""
    text = values.str.strip()
    valid = _matching(text, _DATE)
    number = np.zeros(len(text), dtype="int64")
    number[valid] = text[valid].astype("int64").to_numpy()
    year, month, day = number // 10000, number // 100 % 100, number % 100
    months = ((year - 1970) * 12 + month - 1).astype("datetime64[M]")
    dates = months.astype("datetime64[D]") + (day - 1).astype("timedelta64[D]")
    valid &= (year >= 1) & (month >= 1) & (month <= 12) & (day >= 1)
    valid &= dates.astype("datetime64[M]") == months
    return np.where(valid, dates, np.datetime64("NaT", "D"))


def padded_clocks(values):
    """Stripped clock times, a single-digit hour zero-padded."""
    text = values.str.strip()
    return text.where(~_matching(text, _SINGLE_DIGIT_HOUR), "0" + text)


def service_span(tables, within):
    """The first and last day any service runs within ``within``, a
    ``(first, last)`` pair of dates, from calendar.txt and
    calendar_dates.txt; None when none runs then. Rows with an unreadable
    date are skipped.
    """
    dates, _ = _service_days(tables, within, ends=True)
    if dates.empty:
        return None
    days = dates["date"].to_numpy().astype("datetime64[D]")
    return days.min().item(), days.max().item()


def placeholder_rows(tables):
    """The calendar.txt rows whose end is :data:`PLACEHOLDER_DAYS` days or
    more after their start, as ``service_id``, ``start`` and ``end``; rows
    with an unreadable date are left out."""
    calendar = tables.get("calendar.txt", pd.DataFrame())
    start, end = (parse_dates(_column(calendar, f"{e}_date")) for e in ("start", "end"))
    services = _column(calendar, "service_id").to_numpy()
    rows = pd.DataFrame({"service_id": services, "start": start, "end": end})
    return rows[end - start >= np.timedelta64(PLACEHOLDER_DAYS, "D")]


def service_dates(tables):
    """The dates each service runs, from calendar.txt and calendar_dates.txt.

    Returns ``(dates, unexpanded)``: the ``(service_id, date)`` rows, and
    the ids of the services left out because a date, weekday flag,
    exception type or column of theirs cannot be read, a calendar row of
    theirs spans more than :data:`MAX_SERVICE_DAYS` days, or its days are
    past :data:`MAX_EXPANDED_DAYS`.
    """
    dates, unexpanded = _service_days(tables)
    return dates[~dates["service_id"].isin(unexpanded)], unexpanded


def _service_days(tables, within=None, ends=False):
    """``(dates, unexpanded)``: the ``(service_id, date)`` days services
    run, and the ids of services whose days are not all there (see
    :func:`service_dates`). ``within``, a ``(first, last)`` pair of dates,
    keeps the days between them; ``ends`` expands only enough days at each
    end of a calendar row to hold its first and last running days, which
    needs no limits.
    """
    low = high = None
    if within is not None:
        low, high = (np.datetime64(day, "D") for day in within)
    unexpanded = set()
    empty = {
        "service_id": np.array([], dtype=object),
        "date": np.array([], dtype="datetime64[D]"),
    }
    added = removed = pd.DataFrame(empty)
    exceptions = tables.get("calendar_dates.txt", pd.DataFrame())
    if not {"date", "exception_type"} <= set(exceptions.columns):
        unexpanded.update(exceptions.get("service_id", ()))
    elif "service_id" in exceptions.columns:
        listed = pd.DataFrame(
            {
                "service_id": exceptions["service_id"].to_numpy(),
                "date": parse_dates(exceptions["date"]),
            }
        )
        kind = exceptions["exception_type"].str.strip().to_numpy()
        unreadable = listed["date"].isna() | ~np.isin(kind, ("1", "2"))
        unexpanded.update(listed.loc[unreadable, "service_id"])
        inside = np.array(listed["date"].notna(), dtype=bool)
        if within is not None:
            inside &= ((listed["date"] >= low) & (listed["date"] <= high)).to_numpy()
        added, removed = listed[inside & (kind == "1")], listed[inside & (kind == "2")]
    frames = [added]
    calendar = tables.get("calendar.txt")
    if calendar is not None and "service_id" in calendar.columns:
        blank = pd.Series("", index=calendar.index)
        start = parse_dates(calendar.get("start_date", blank))
        end = parse_dates(calendar.get("end_date", blank))
        services = calendar["service_id"]
        readable = ~(np.isnat(start) | np.isnat(end))
        if within is not None:
            start, end = np.maximum(start, low), np.minimum(end, high)
        span = np.where(readable, (end - start).astype("int64") + 1, 0).clip(0)
        count = edge = span
        if ends:
            # A week of running days holds at least one day no removal
            # hides, so each row's first and last running days lie within a
            # week per removal of its service, plus one, of its ends.
            hidden = services.map(removed.groupby("service_id").size())
            edge = 7 * (1 + hidden.fillna(0).to_numpy(dtype="int64"))
            count = np.minimum(span, 2 * edge)
        else:
            left_out = ~readable | (span > MAX_SERVICE_DAYS)
            for day in _WEEKDAYS:
                flag = _column(calendar, day).str.strip()
                left_out |= ~np.array(flag.isin(("0", "1")), dtype=bool)
            # A service left out expands none of its rows, so the budget
            # goes to the others.
            gone = unexpanded | set(services[left_out])
            left_out = np.array(services.isin(gone), dtype=bool)
            count = edge = np.where(left_out, 0, span)
            order = np.argsort(count, kind="stable")
            beyond = np.zeros(len(count), dtype=bool)
            beyond[order] = np.cumsum(count[order]) > MAX_EXPANDED_DAYS
            count = np.where(beyond, 0, count)
            unexpanded.update(services[left_out | beyond])
        rows = np.repeat(np.arange(len(calendar)), count)
        step = np.arange(len(rows)) - np.repeat(np.cumsum(count) - count, count)
        step = np.where(step < edge[rows], step, span[rows] - count[rows] + step)
        days = start[rows] + step.astype("timedelta64[D]")
        flags = np.column_stack(
            [
                (
                    np.array(calendar[day].str.strip() == "1", dtype=bool)
                    if day in calendar.columns
                    else np.zeros(len(calendar), dtype=bool)
                )
                for day in _WEEKDAYS
            ]
        )
        # 1970-01-01, day 0, was a Thursday.
        runs = flags[rows, (days.astype("int64") + 3) % 7]
        frames.append(
            pd.DataFrame(
                {"service_id": services.to_numpy()[rows[runs]], "date": days[runs]}
            )
        )
    dates = pd.concat(frames, ignore_index=True).drop_duplicates()
    if len(removed):
        gone = pd.MultiIndex.from_frame(removed)
        dates = dates[~pd.MultiIndex.from_frame(dates).isin(gone)]
    return dates.reset_index(drop=True), unexpanded


def _column(table, name):
    """``table[name]``, or blanks where the column is absent."""
    if name in table.columns:
        return table[name]
    return pd.Series("", index=table.index, dtype=object)


def _stripped(table, name, blank=""):
    values = _column(table, name).str.strip()
    return values.where(values != "", blank)


def _keyed(frame, keys):
    """``frame`` indexed by ``keys``, the first row of each key kept."""
    frame = frame.set_axis(keys.to_numpy())
    return frame[~frame.index.duplicated()]


def _digest(frame, key):
    # Only text columns take the key; numeric ones are hashes already or
    # positions and counts.
    return pd.util.hash_pandas_object(frame, index=False, hash_key=key).to_numpy()


def agency_keys(names):
    """Each agency name of the series ``names`` as compared across feeds:
    casefolded, diacritics and punctuation dropped, whitespace collapsed
    (:func:`transitio.index.places._normalize`), then its trailing
    legal-form words (``Ltd``, ``GmbH``, ``Oy``, ``S.A.`` and the like)
    dropped, its first word always kept."""
    from transitio.index.places import _normalize

    def key(name):
        words = _normalize(name).split()
        while len(words) > 1 and words[-1] in _LEGAL_FORMS:
            words.pop()
        return " ".join(words)

    return names.map(key)


def route_keys(tables):
    """Each route's key, indexed by ``route_id`` (the first row of each id
    kept): ``agency``, its agency name as :func:`agency_keys` compares it;
    ``name``, its short name, else long name, casefolded; ``type``, its
    route type; and its ``continuous_pickup`` and ``continuous_drop_off``,
    blank when unset."""
    agency, routes = (
        tables.get(name, pd.DataFrame()) for name in ("agency.txt", "routes.txt")
    )
    names = agency_keys(_stripped(agency, "agency_name"))
    route_agency = _column(routes, "agency_id")
    agency_names = route_agency.map(_keyed(names, _column(agency, "agency_id")))
    if len(agency) == 1:
        # A single-agency feed may leave a route's agency_id blank.
        agency_names = agency_names.mask(route_agency.str.strip() == "", names.iloc[0])
    short = _stripped(routes, "route_short_name")
    keys = pd.DataFrame(
        {
            "agency": agency_names.fillna(""),
            "name": short.mask(short == "", _stripped(routes, "route_long_name")),
            "type": _stripped(routes, "route_type"),
            "continuous_pickup": _stripped(routes, "continuous_pickup"),
            "continuous_drop_off": _stripped(routes, "continuous_drop_off"),
        }
    )
    keys["name"] = keys["name"].str.casefold()
    return _keyed(keys, _column(routes, "route_id"))


def trip_signatures(tables, with_stops=False):
    """The signature of each trip in a feed's tables.

    A signature is 128 bits, written as 32 hex digits, over the trip's route
    short name, else long name, casefolded, and route type, its
    ``wheelchair_accessible`` and ``bikes_allowed`` (blank as 0), and per
    stop, in ``stop_sequence`` order: the stop's coordinates rounded to 5
    decimals, arrival and departure times with single-digit hours padded,
    ``pickup_type`` and ``drop_off_type`` (blank as 0), and the effective
    ``continuous_pickup`` and ``continuous_drop_off`` (the stop time's value,
    else the route's, else 1). The agency, headsigns, short names,
    ``shape_id`` and shape geometry, ``timepoint`` and
    ``shape_dist_traveled`` are not part of it. A frequency-based trip, one
    named in frequencies.txt, has its times as seconds after its first
    departure, and is also signed over its frequencies.txt rows in any
    order: ``start_time`` and ``end_time`` in seconds, ``headway_secs`` and
    ``exact_times`` (blank as 0); it never signs equal to a timetabled trip.

    Returns one ``trip_id``, ``service_id``, ``signature`` row per signed
    trip. A trip listed twice in trips.txt, one naming a stop or route the
    feed lacks, one whose stop times cannot be ordered (a blank,
    non-numeric or repeated ``stop_sequence``, or one over 18 digits past
    leading zeros), and a frequency-based trip whose first departure, or a
    frequency row's ``start_time``, ``end_time`` or ``headway_secs``, cannot
    be read are not signed.

    With ``with_stops``, returns ``(signatures, stops)``, ``stops`` the
    signed trips' stop times, by trip and in ``stop_sequence`` order, as
    :func:`~transitio.gtfs._near.near_matches` compares them: ``trip_id``,
    ``position`` (from 0), ``stop_id``, the stop's ``lon`` and ``lat``,
    ``seconds`` (the departure time, else the arrival time, a
    frequency-based trip's counted from its first departure; NaN when
    neither can be read), ``pickup`` and ``drop_off`` (blank as 0),
    ``continuous`` (whether an effective ``continuous_pickup`` or
    ``continuous_drop_off`` is not 1), and per trip ``route`` (a hash of
    its route's name and type and whether it is frequency-based), ``runs``
    (a hash of its frequencies.txt rows, 0 for a timetabled trip), and
    ``wheelchair`` and ``bikes`` (blank as 0).
    """
    from transitio.index.fingerprint import COORDINATE_DECIMALS

    none = pd.DataFrame(columns=["trip_id", "stop_id", "stop_sequence"], dtype=object)
    trips = tables.get("trips.txt", none)
    stop_times = tables.get("stop_times.txt", none)
    if not set(none.columns) <= set(stop_times.columns):
        stop_times = none
    if "trip_id" not in trips.columns:
        trips = none
    trips = trips.drop_duplicates("trip_id", keep=False).set_index("trip_id")
    stops = tables.get("stops.txt", pd.DataFrame())
    keys = route_keys(tables)
    trips = trips[_column(trips, "route_id").isin(keys.index)]
    located = pd.DataFrame(
        {
            axis: pd.to_numeric(_stripped(stops, f"stop_{axis}"), errors="coerce")
            for axis in ("lat", "lon")
        }
    )
    located = _keyed(located, _column(stops, "stop_id"))
    coordinates = located.round(COORDINATE_DECIMALS).add(0.0).astype(str)
    stop_rows = coordinates.index.get_indexer(stop_times["stop_id"])

    codes, trip_ids = pd.factorize(stop_times["trip_id"])
    raw = stop_times["stop_sequence"].str.strip()
    text = raw.str.lstrip("0").mask(raw.str.fullmatch("0+").fillna(False), "0")
    # Up to 18 digits a sequence number fits an int64 exactly.
    orderable = _matching(text, _SEQUENCE)
    sequence = np.zeros(len(text), dtype="int64")
    sequence[orderable] = text[orderable].astype("int64").to_numpy()
    order = np.lexsort((sequence, codes))
    ordered, ranked = codes[order], sequence[order]
    repeated = (ordered[1:] == ordered[:-1]) & (ranked[1:] == ranked[:-1])
    unsigned = np.r_[codes[~orderable | (stop_rows < 0)], ordered[1:][repeated]]
    starts = np.flatnonzero(np.r_[True, ordered[1:] != ordered[:-1]])[: len(order)]
    row_keys = keys.reindex(
        stop_times["trip_id"].map(_column(trips, "route_id")).to_numpy()
    )

    def effective(name):
        value = _stripped(stop_times, name).to_numpy()
        value = np.where(value != "", value, row_keys[name].fillna("").to_numpy())
        return np.where(value != "", value, "1")

    frequencies = tables.get("frequencies.txt", pd.DataFrame())
    listed = trip_ids.get_indexer(_column(frequencies, "trip_id"))
    headway = trip_ids.isin(_column(frequencies, "trip_id"))
    first = np.full(len(trip_ids), np.nan)
    departures = _column(stop_times, "departure_time").iloc[order[starts]]
    first[ordered[starts]] = clock_seconds(departures).to_numpy()
    timed = np.flatnonzero(headway[codes])

    def clock(kind):
        # A frequency-based trip's times count from its first departure.
        column = _column(stop_times, f"{kind}_time")
        text = np.array(padded_clocks(column), dtype=object)
        after = clock_seconds(column.iloc[timed]).to_numpy() - first[codes[timed]]
        known = ~np.isnan(after)
        text[timed[known]] = after[known].astype("int64").astype(str)
        return text

    start, end = (
        clock_seconds(_column(frequencies, f"{edge}_time")).to_numpy()
        for edge in ("start", "end")
    )
    spacing = _stripped(frequencies, "headway_secs")
    readable = _matching(spacing, r"\d+") & ~np.isnan(start + end)
    periods = pd.DataFrame(
        {
            "start": start,
            "end": end,
            "headway": spacing.str.lstrip("0").to_numpy(),
            "exact_times": _stripped(frequencies, "exact_times", "0").to_numpy(),
        }
    )
    named = listed >= 0
    unreadable = np.flatnonzero(headway & np.isnan(first))
    unsigned = np.r_[unsigned, listed[named & ~readable], unreadable]
    arrival, departure = (clock(kind) for kind in ("arrival", "departure"))
    rows = pd.DataFrame(
        {
            "arrival": arrival,
            "departure": departure,
            "pickup": _stripped(stop_times, "pickup_type", "0").to_numpy(),
            "drop_off": _stripped(stop_times, "drop_off_type", "0").to_numpy(),
            "continuous_pickup": effective("continuous_pickup"),
            "continuous_drop_off": effective("continuous_drop_off"),
        }
    )
    counts = np.diff(np.r_[starts, len(order)])
    positions = np.arange(len(order)) - np.repeat(starts, counts)
    signed = ~np.isin(ordered[starts], unsigned)
    signed &= trip_ids[ordered[starts]].isin(trips.index)
    signed_codes = ordered[starts][signed]
    signed_ids = trip_ids[signed_codes]
    trips = trips[trips.index.isin(signed_ids)]

    def per_trip(values):
        return pd.Series(values, index=signed_ids).reindex(trips.index).to_numpy()

    route = keys.reindex(_column(trips, "route_id").to_numpy())
    whole = route[["name", "type"]].set_axis(trips.index)
    whole = whole.assign(
        wheelchair=_stripped(trips, "wheelchair_accessible", "0"),
        bikes=_stripped(trips, "bikes_allowed", "0"),
        count=per_trip(counts[signed]),
        headway=per_trip(headway[signed_codes]),
    )
    halves = []
    for key in _HASH_KEYS:
        stop = np.append(_digest(coordinates, key), np.uint64(0))[stop_rows]
        placed = pd.DataFrame(
            {"row": _digest(rows.assign(stop=stop), key)[order], "position": positions}
        )
        # Each row's hash is taken with its position in the trip, and the
        # sum over the trip wraps around at 64 bits; frequency rows are
        # summed without a position.
        placed = _digest(placed, key)
        sums = np.add.reduceat(placed, starts)[signed] if len(order) else placed
        runs = np.zeros(len(trip_ids), dtype="uint64")
        np.add.at(runs, listed[named], _digest(periods, key)[named])
        whole_hash = _digest(
            whole.assign(stops=per_trip(sums), runs=per_trip(runs[signed_codes])), key
        )
        halves.append(np.char.mod("%016x", whole_hash))
    signatures = pd.DataFrame(
        {
            "trip_id": trips.index.to_numpy(),
            "service_id": _column(trips, "service_id").to_numpy(),
            "signature": np.char.add(*halves) if len(trips) else [],
        }
    )
    if not with_stops:
        return signatures
    kept = np.repeat(signed, counts)
    at, trip = order[kept], ordered[kept]
    stop = stop_rows[at]
    placed = trips.index.get_indexer(trip_ids[trip])
    departure, arrival = (
        clock_seconds(_column(stop_times, f"{kind}_time").iloc[at])
        for kind in ("departure", "arrival")
    )
    seconds = departure.fillna(arrival).to_numpy()
    stopping = rows[["continuous_pickup", "continuous_drop_off"]] != "1"
    return signatures, pd.DataFrame(
        {
            "trip_id": trip_ids[trip],
            "position": positions[kept],
            "stop_id": located.index[stop],
            "lon": located["lon"].to_numpy()[stop],
            "lat": located["lat"].to_numpy()[stop],
            "seconds": seconds - np.where(headway[trip], first[trip], 0.0),
            "pickup": rows["pickup"].to_numpy()[at],
            "drop_off": rows["drop_off"].to_numpy()[at],
            "continuous": stopping.any(axis=1).to_numpy()[at],
            # Hashed under the last key, as are the frequency rows.
            "route": _digest(whole[["name", "type", "headway"]], key)[placed],
            "runs": runs[trip],
            "wheelchair": whole["wheelchair"].to_numpy()[placed],
            "bikes": whole["bikes"].to_numpy()[placed],
        }
    )
