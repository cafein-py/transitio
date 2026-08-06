"""Heal a feed by replacing broken trips with donor counterparts."""

from __future__ import annotations

import json
import pathlib

#: Matching thresholds, echoed in every patch report.
PATCH_TIME_TOLERANCE_S = 60
PATCH_STOP_MATCH_SHARE = 0.8
PATCH_STOP_RADIUS_M = 100.0
PATCH_BLANK_NAME_RADIUS_M = 25.0

#: Cap on LCS table cells per candidate comparison; a candidate over
#: the cap is skipped and the affected log entry carries a caveat.
_LCS_CELL_BUDGET = 4_000_000

#: Cap on LCS cells across a whole call, so many individually small
#: candidates cannot add up to unbounded work either.
_LCS_TOTAL_CELL_BUDGET = 200_000_000

#: Size bound for the advisory provenance sidecar.
_SIDECAR_MAX_BYTES = 1 << 20


def _thresholds():
    # Read at call time so the echo always states the applied values.
    return {
        "timeToleranceSeconds": PATCH_TIME_TOLERANCE_S,
        "stopMatchShare": PATCH_STOP_MATCH_SHARE,
        "stopRadiusMeters": PATCH_STOP_RADIUS_M,
        "blankNameRadiusMeters": PATCH_BLANK_NAME_RADIUS_M,
        "lcsCellBudget": _LCS_CELL_BUDGET,
        "lcsTotalCellBudget": _LCS_TOTAL_CELL_BUDGET,
    }


class _WorkBudget:
    """Shared LCS-cell allowance for one patch_feed call."""

    def __init__(self):
        self.remaining = _LCS_TOTAL_CELL_BUDGET

    def take(self, cells):
        if cells > _LCS_CELL_BUDGET or cells > self.remaining:
            return False
        self.remaining -= cells
        return True


#: Donor ERROR contexts checked against a candidate's imported closure.
_HEALTH_KEYS = {
    "tripId": "trips",
    "stopId": "stops",
    "routeId": "routes",
    "serviceId": "services",
    "shapeId": "shapes",
    "agencyId": "agencies",
    "levelId": "levels",
    "networkId": "networks",
}


def patch_feed(base, donor, output, *, when=None, check=True, **budgets):
    """Replace a base feed's broken trips with matched donor trips.

    Trips implicated in the base feed's ERROR-severity notices are
    dropped and replaced with healthy counterparts from the donor feed
    (same area and period), matched by agency, route and stop-sequence
    similarity. The donor subgraph is imported under an id prefix with
    full referential closure, and every action is logged with donor
    provenance. The gtfstidy semantic-equivalence guarantee does NOT
    hold here: the donor timetable may genuinely differ, so patching is
    opt-in and the report states what changed.

    Parameters
    ----------
    base, donor : path
        The feed to heal and the sibling feed supplying replacements.
        The base file is never modified.
    output : path
        Destination for the patched feed; must differ from both inputs.
    when : str or datetime.date, optional
        The target service day. The donor's computed service window
        must cover it, and the revalidation report then carries the
        ``moment`` block for the day.
    check : bool, default True
        Raise :class:`transitio.exceptions.PatchError` when the
        patched output still validates with ERROR notices (the file is
        still written, like ``InvalidFeedError``). The reliability
        refusal — sampled or truncated validation at any stage — ALWAYS
        applies, regardless of ``check``.
    **budgets
        The ``validate_feed`` budget keyword arguments.

    Returns
    -------
    dict
        ``{"patches", "thresholds", "donor", "semantic_equivalence",
        "remaining_notices", "service_window", "moment"}``.
    """
    import shutil
    import tempfile

    from transitio.exceptions import PatchError
    from transitio.gtfs._compare import _snapshot
    from transitio.validate import validate_feed

    base = pathlib.Path(base)
    donor = pathlib.Path(donor)
    output = pathlib.Path(output)
    if _same_entry(output, base) or _same_entry(output, donor):
        raise ValueError("output must differ from both input feeds")
    when_ymd = _as_ymd(when) if when is not None else None
    if when_ymd is not None:
        existing = budgets.get("reference_date")
        if existing is not None and _as_ymd(existing) != when_ymd:
            raise ValueError("when and reference_date disagree; pass only one")
        budgets["reference_date"] = when_ymd

    # Private snapshots make validation, matching, import and provenance
    # describe the same bytes even if the input paths are swapped
    # underneath; the donor digest is computed while copying.
    workdir = pathlib.Path(tempfile.mkdtemp(prefix="transitio-patch-"))
    try:
        base_snapshot = workdir / "base.zip"
        donor_snapshot = workdir / "donor.zip"
        _snapshot(base, base_snapshot)
        donor_sha = _snapshot(donor, donor_snapshot)

        base_validation = validate_feed(base_snapshot, **budgets)
        _require_reliable(base_validation, "base", PatchError)
        donor_validation = validate_feed(donor_snapshot, **budgets)
        _require_reliable(donor_validation, "donor", PatchError)
        if when_ymd is not None:
            window = donor_validation.get("service_window")
            if not window or not (window[0] <= when_ymd <= window[1]):
                raise PatchError(
                    f"donor service window {window} does not cover {when_ymd}"
                )

        broken = _error_ids(base_validation, "tripId")
        # Sets once, not per candidate: health filtering intersects them
        # for every entity type of every scored donor trip.
        donor_bad = {
            field: set(_error_ids(donor_validation, field)) for field in _HEALTH_KEYS
        }

        patches = []
        provenance = _donor_provenance(donor, donor_sha)
        report = _patch(
            base_snapshot,
            donor_snapshot,
            output,
            broken,
            donor_bad,
            patches,
            check,
            budgets,
            PatchError,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    for entry in patches:
        if entry["action"] == "replace_trip":
            entry["donor"] = dict(provenance)
    report["patches"] = patches
    report["thresholds"] = _thresholds()
    report["donor"] = provenance
    report["semantic_equivalence"] = False
    # The output exists and actions were taken by now, so the final
    # refusal still hands back the audit log.
    _require_reliable(
        report,
        "patched output",
        PatchError,
        notices="remaining_notices",
        report=report,
    )
    if check and any(
        notice["severity"] == "ERROR" for notice in report["remaining_notices"]
    ):
        error = PatchError(
            "patched feed still carries ERROR notices; the output was "
            "written for inspection"
        )
        error.report = report
        raise error
    return report


def _patch(base, donor, output, broken, donor_bad, patches, check, budgets, PatchError):
    import pandas as pd

    from transitio.edit import FeedEditor
    from transitio.gtfs._merge import (
        _backfill_agency,
        _normalise_networks,
        _prefix_feed,
        _reject_flex,
    )

    base_editor = FeedEditor(base)
    donor_editor = FeedEditor(donor)
    base_tables = base_editor.tables
    donor_tables = donor_editor.tables
    _reject_flex(base_tables, base_editor._extra_entries, "base feed")
    _reject_flex(donor_tables, donor_editor._extra_entries, "donor feed")
    _check_timezones(base_tables, donor_tables)
    _backfill_agency(base_tables, "base")
    _backfill_agency(donor_tables, "donor-src")

    pairs = _pair_agencies(base_tables, donor_tables, patches)
    donor_model = _FeedModel(donor_tables)
    base_model = _FeedModel(base_tables)

    assignments = {}
    taken = set()
    budget = _WorkBudget()
    for trip_id in sorted(broken):
        entry = _match_trip(
            trip_id, base_model, donor_model, pairs, donor_bad, taken, budget
        )
        if entry.get("action") == "replace_trip":
            assignments[trip_id] = entry
            taken.add(entry["donorTripId"])
        entry["triggered_by"] = sorted(broken[trip_id])
        patches.append(entry)

    dropped_trips = set(assignments)
    _drop_trip_rows(base_tables, dropped_trips, patches)

    if assignments:
        closure = _closure_tables(
            donor_model, {a["donorTripId"] for a in assignments.values()}
        )
        prefix = _free_prefix(base_tables)
        closure = _prefix_feed(closure, prefix, set())
        for entry in assignments.values():
            entry["newTripId"] = f"{prefix}:{entry['donorTripId']}"
        merged = {}
        for name in sorted(set(base_tables) | set(closure)):
            frames = [
                t for t in (base_tables.get(name), closure.get(name)) if t is not None
            ]
            merged[name] = (
                pd.concat(frames, ignore_index=True).fillna("")
                if len(frames) > 1
                else frames[0]
            )
        _normalise_networks(merged)
        base_editor.tables = merged

    from transitio.exceptions import InvalidFeedError

    try:
        validation = base_editor.save(output, check=check, change_log=False, **budgets)
    except InvalidFeedError as error:
        validation = error.report
    return {
        "remaining_notices": validation.get("notices", []),
        "incomplete": validation.get("incomplete", []),
        "service_window": validation.get("service_window"),
        "moment": validation.get("moment"),
    }


def _same_entry(output, source):
    # samefile catches case-insensitive and normalising filesystems,
    # where two different spellings name one directory entry.
    try:
        return output.samefile(source)
    except OSError:
        return output.resolve() == source.resolve()


def _as_ymd(when):
    text = str(when).replace("-", "")
    if len(text) != 8 or not text.isdigit():
        raise ValueError(f"invalid when: {when!r} (expected YYYYMMDD)")
    return text


def _require_reliable(validation, label, PatchError, notices="notices", report=None):
    codes = {n["code"] for n in validation.get(notices, [])}
    if "notice_limit_reached" in codes or validation.get("incomplete"):
        error = PatchError(
            f"{label} validation is sampled or truncated; absence of an "
            "ERROR is not evidence there — raise the limits to patch"
        )
        if report is not None:
            error.report = report
        raise error


def _error_ids(validation, field):
    """Entity ids named by ERROR notices, mapped to their codes."""
    found = {}
    for notice in validation.get("notices", []):
        if notice["severity"] != "ERROR":
            continue
        value = notice.get("context", {}).get(field)
        if value:
            found.setdefault(str(value), set()).add(notice["code"])
    return found


def _read_sidecar(sidecar):
    """Parse the sidecar defensively: the file is advisory metadata, so
    a symlink, FIFO, oversized or malformed file is None, never a hang
    or a crash. Platforms without ``O_NOFOLLOW`` cannot open it safely,
    so there catalog provenance is simply unavailable and the report
    carries the computed hash alone."""
    import os
    import stat

    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        # Without an atomic no-follow open the symlink check would be
        # racy; advisory metadata is not worth that, so fail closed.
        return None
    flags = os.O_RDONLY | no_follow | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(sidecar, flags)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _SIDECAR_MAX_BYTES:
            return None
        chunks = []
        remaining = _SIDECAR_MAX_BYTES
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    try:
        recorded = json.loads(b"".join(chunks).decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    return recorded if isinstance(recorded, dict) else None


def _donor_provenance(donor, computed):
    provenance = {"sha256": computed}
    recorded = _read_sidecar(donor.with_suffix(".provenance.json"))
    if recorded and recorded.get("sha256") == computed:
        for key in ("feed_id", "dataset_id"):
            if recorded.get(key):
                provenance[key] = recorded[key]
    else:
        provenance["catalog"] = "unavailable"
    return provenance


def _check_timezones(base_tables, donor_tables):
    zones = set()
    for tables in (base_tables, donor_tables):
        agency = tables.get("agency.txt")
        if agency is not None and "agency_timezone" in agency.columns:
            zones.update(
                value.strip() for value in agency["agency_timezone"] if value.strip()
            )
    if len(zones) > 1:
        raise ValueError(f"agency timezones differ across feeds: {sorted(zones)}")


def _agency_names(tables, patches, side):
    """Normalized agency name → agency_id; duplicates are unpairable."""
    agency = tables.get("agency.txt")
    names = {}
    duplicated = set()
    if agency is None:
        return names
    for _, row in agency.iterrows():
        name = str(row.get("agency_name", "")).strip().casefold()
        if not name:
            continue
        if name in names:
            duplicated.add(name)
        names[name] = str(row.get("agency_id", "")).strip()
    for name in duplicated:
        del names[name]
        patches.append(
            {"action": "unpairable_agency", "agencyName": name, "side": side}
        )
    return names


def _pair_agencies(base_tables, donor_tables, patches):
    """base agency_id → donor agency_id under the equivalence rule."""
    base_names = _agency_names(base_tables, patches, "base")
    donor_names = _agency_names(donor_tables, patches, "donor")
    pairs = {}
    for name, base_id in base_names.items():
        if name in donor_names:
            pairs[base_id] = donor_names[name]
    base_agency = base_tables.get("agency.txt")
    donor_agency = donor_tables.get("agency.txt")
    if (
        base_agency is not None
        and donor_agency is not None
        and len(base_agency) == 1
        and len(donor_agency) == 1
    ):
        # Two single-agency feeds pair automatically — the common
        # same-operator case with divergent ids.
        pairs[str(base_agency.iloc[0].get("agency_id", "")).strip()] = str(
            donor_agency.iloc[0].get("agency_id", "")
        ).strip()
    return pairs


class _FeedModel:
    """Indexed string-table views the matcher and closure walk share."""

    def __init__(self, tables):
        import pandas as pd

        self.tables = tables
        empty = pd.DataFrame()
        trips = tables.get("trips.txt", empty)
        self.trips = {
            str(row.get("trip_id", "")): row
            for _, row in trips.iterrows()
            if str(row.get("trip_id", "")).strip()
        }
        self.stop_times = {}
        #: Trips whose stop_times cannot be ordered with confidence.
        #: Dropping their bad rows would shorten the sequence and let a
        #: partial match clear the similarity floor, so they never match.
        self.unmatchable = set()
        seen_sequences = {}
        for _, row in tables.get("stop_times.txt", empty).iterrows():
            trip = str(row.get("trip_id", ""))
            raw = str(row.get("stop_sequence", "")).strip()
            # The length bound keeps int() under CPython's digit limit,
            # which would otherwise raise on an absurdly long field.
            if not (raw.isascii() and raw.isdigit() and len(raw) <= 18):
                self.unmatchable.add(trip)
                continue
            seq = int(raw)
            if seq in seen_sequences.setdefault(trip, set()):
                self.unmatchable.add(trip)
                continue
            seen_sequences[trip].add(seq)
            self.stop_times.setdefault(trip, []).append((seq, row))
        for rows in self.stop_times.values():
            rows.sort(key=lambda pair: pair[0])
        self.stops = {
            str(row.get("stop_id", "")): row
            for _, row in tables.get("stops.txt", empty).iterrows()
        }
        self.routes = {
            str(row.get("route_id", "")): row
            for _, row in tables.get("routes.txt", empty).iterrows()
        }
        self.frequencies = {}
        for _, row in tables.get("frequencies.txt", empty).iterrows():
            self.frequencies.setdefault(str(row.get("trip_id", "")), []).append(row)
        self._trip_keys = {}
        #: trip_id -> donor-health verdict, memoised per call.
        self.health = {}
        # route_id -> network_ids, built once: the closure walk runs
        # per candidate and would otherwise rescan the whole table.
        self.route_network_ids = {}
        for _, row in tables.get("route_networks.txt", empty).iterrows():
            network = str(row.get("network_id", ""))
            if network.strip():
                self.route_network_ids.setdefault(
                    str(row.get("route_id", "")), set()
                ).add(network)
        self.route_trips = {}
        for trip_id, row in self.trips.items():
            self.route_trips.setdefault(str(row.get("route_id", "")), []).append(
                trip_id
            )
        # (agency_id, route key) -> candidate trips, built once so a
        # base trip looks its candidates up instead of rescanning.
        self.candidates = {}
        for route_id, route in self.routes.items():
            trips_here = self.route_trips.get(route_id)
            if not trips_here:
                continue
            index = (str(route.get("agency_id", "")).strip(), _route_key(route))
            self.candidates.setdefault(index, []).extend(trips_here)
        for trips_here in self.candidates.values():
            trips_here.sort()

    def stop_sequence(self, trip_id):
        return [
            str(row.get("stop_id", "")) for _, row in self.stop_times.get(trip_id, [])
        ]

    def trip_keys(self, trip_id):
        """Stop keys of a trip, memoised: candidates repeat across
        base trips and the lookup is the matcher's inner loop."""
        keys = self._trip_keys.get(trip_id)
        if keys is None:
            keys = [self.stop_key(stop) for stop in self.stop_sequence(trip_id)]
            self._trip_keys[trip_id] = keys
        return keys

    def first_departure(self, trip_id):
        """The FIRST stop's departure_time, or ``None``.

        Neither a later stop nor the same stop's arrival stands in for
        it: the first would compare one trip's mid-route time against
        another's start, the second assumes a dwell that GTFS does not
        state.
        """
        rows = self.stop_times.get(trip_id)
        if not rows or trip_id in self.unmatchable:
            return None
        _, row = rows[0]
        return _seconds(str(row.get("departure_time", "")))

    def stop_key(self, stop_id):
        import math

        row = self.stops.get(stop_id)
        if row is None:
            return None
        name = str(row.get("stop_name", "")).strip().casefold()
        try:
            latitude = float(str(row.get("stop_lat", "")).strip())
            longitude = float(str(row.get("stop_lon", "")).strip())
        except ValueError:
            return None
        # Non-finite or out-of-range coordinates would poison the
        # haversine; an unlocatable stop simply never matches.
        if not (math.isfinite(latitude) and math.isfinite(longitude)):
            return None
        if abs(latitude) > 90.0 or abs(longitude) > 180.0:
            return None
        return name, (latitude, longitude)


def _seconds(value):
    """Strict GTFS HH:MM:SS; a malformed time is unavailable, not zero.

    The base feed is expected to be broken, so a lenient parse could
    normalise nonsense into a departure that scores against a healthy
    donor.
    """
    parts = value.strip().split(":")
    if len(parts) != 3:
        return None
    # GTFS spells times [H]HH:MM:SS in ASCII, minutes and seconds
    # always two digits; int() alone would accept 8:0:0 and other
    # numeral systems.
    if not all(part.isascii() and part.isdigit() for part in parts):
        return None
    if not 1 <= len(parts[0]) <= 3 or len(parts[1]) != 2 or len(parts[2]) != 2:
        return None
    hours, minutes, seconds = (int(part) for part in parts)
    if minutes > 59 or seconds > 59:
        return None
    return hours * 3600 + minutes * 60 + seconds


def _stops_match(base_key, donor_key):
    """The one canonical cross-feed stop relation: name + proximity;
    ids are feed-local and never compared."""
    from transitio.edit._editor import _haversine_m

    if base_key is None or donor_key is None:
        return False
    base_name, base_position = base_key
    donor_name, donor_position = donor_key
    distance = _haversine_m(base_position, donor_position)
    if base_name and donor_name:
        return base_name == donor_name and distance <= PATCH_STOP_RADIUS_M
    # Either side blank: the name check is unavailable, so only the
    # tighter proximity rule can vouch for identity.
    return distance <= PATCH_BLANK_NAME_RADIUS_M


def _similarity(base_keys, donor_keys, budget):
    """LCS share over the stop-match relation, against the longer side.

    ``None`` (not a score) when the comparison would exceed the
    per-candidate or the call-wide cell budget: refused work is a
    logged resource caveat, never a verdict.
    """
    if not base_keys or not donor_keys:
        return 0.0
    if not budget.take(len(base_keys) * len(donor_keys)):
        return None
    previous = [0] * (len(donor_keys) + 1)
    for base_key in base_keys:
        current = [0]
        for j, donor_key in enumerate(donor_keys, 1):
            if _stops_match(base_key, donor_key):
                current.append(previous[j - 1] + 1)
            else:
                current.append(max(previous[j], current[j - 1]))
        previous = current
    return previous[-1] / max(len(base_keys), len(donor_keys))


def _route_key(row):
    short = str(row.get("route_short_name", "")).strip()
    long = str(row.get("route_long_name", "")).strip()
    return (
        str(row.get("route_type", "")).strip(),
        short if short else long,
        bool(short),
    )


def _match_trip(trip_id, base_model, donor_model, pairs, donor_bad, taken, budget):
    base_row = base_model.trips.get(trip_id)
    if base_row is None:
        return {"action": "no_donor_match", "tripId": trip_id, "routeId": None}
    route_id = str(base_row.get("route_id", ""))
    entry = {"action": "no_donor_match", "tripId": trip_id, "routeId": route_id}
    base_route = base_model.routes.get(route_id)
    if base_route is None:
        return entry
    donor_agency = pairs.get(str(base_route.get("agency_id", "")).strip())
    if donor_agency is None:
        return entry
    # The route match is a candidate-narrowing pre-filter: several
    # matching donor routes pool their trips under one index key.
    candidates = donor_model.candidates.get((donor_agency, _route_key(base_route)))
    if not candidates:
        return entry

    base_departure = base_model.first_departure(trip_id)
    base_keys = base_model.trip_keys(trip_id)
    scored = []
    exhausted_at = None
    for donor_trip in candidates:  # the index keeps them sorted
        donor_departure = donor_model.first_departure(donor_trip)
        if base_departure is None or donor_departure is None:
            continue
        if abs(base_departure - donor_departure) > PATCH_TIME_TOLERANCE_S:
            continue
        share = _similarity(base_keys, donor_model.trip_keys(donor_trip), budget)
        if share is None:
            exhausted_at = donor_trip
            # The ranking is already incomplete, and enumerating the
            # rest is itself unbounded work: stop here.
            break
        if share >= PATCH_STOP_MATCH_SHARE:
            scored.append((-share, donor_trip))

    def unmatched(action):
        result = {"action": action, "tripId": trip_id, "routeId": route_id}
        if exhausted_at is not None:
            # Refused quadratic work is a resource caveat, not a
            # verdict: scoring stopped here and the candidates after
            # it were never examined.
            result["lcsBudgetExhaustedAt"] = exhausted_at
        return result

    if exhausted_at is not None:
        # An unscored candidate leaves the similarity ranking
        # incomplete; never select a donor from partial evidence.
        return unmatched(entry["action"])
    if not scored:
        return unmatched(entry["action"])
    scored.sort()
    healthy = [
        pair for pair in scored if not _donor_unhealthy(pair[1], donor_model, donor_bad)
    ]
    if not healthy:
        return unmatched("no_healthy_donor")
    for negative_share, donor_trip in healthy:
        if donor_trip in taken:
            continue
        return {
            "action": "replace_trip",
            "tripId": trip_id,
            "routeId": route_id,
            "donorTripId": donor_trip,
            "similarity": round(-negative_share, 4),
        }
    return unmatched("donor_taken")


def _closure_ids(donor_model, trip_id):
    """The typed id sets a donor trip's import would pull in."""
    row = donor_model.trips.get(trip_id)
    ids = {key: set() for key in _HEALTH_KEYS.values()}
    if row is None:
        return ids
    ids["trips"].add(trip_id)
    # Ids are opaque strings: stripping is only ever a blankness test,
    # the sets keep the exact values the donor rows carry.
    route_id = str(row.get("route_id", ""))
    service_id = str(row.get("service_id", ""))
    shape_id = str(row.get("shape_id", ""))
    if route_id.strip():
        ids["routes"].add(route_id)
    if service_id.strip():
        ids["services"].add(service_id)
    if shape_id.strip():
        ids["shapes"].add(shape_id)
    stack = [
        str(r.get("stop_id", "")) for _, r in donor_model.stop_times.get(trip_id, [])
    ]
    while stack:
        stop_id = stack.pop()
        if not stop_id.strip() or stop_id in ids["stops"]:
            continue
        ids["stops"].add(stop_id)
        stop = donor_model.stops.get(stop_id)
        if stop is None:
            continue
        parent = str(stop.get("parent_station", ""))
        if parent.strip():
            stack.append(parent)
        level = str(stop.get("level_id", ""))
        if level.strip():
            ids["levels"].add(level)
    route = donor_model.routes.get(route_id)
    if route is not None:
        agency = str(route.get("agency_id", ""))
        if agency.strip():
            ids["agencies"].add(agency)
        network = str(route.get("network_id", ""))
        if network.strip():
            ids["networks"].add(network)
    ids["networks"] |= donor_model.route_network_ids.get(route_id, set())
    return ids


def _donor_unhealthy(trip_id, donor_model, donor_bad):
    cached = donor_model.health.get(trip_id)
    if cached is not None:
        return cached
    closure = _closure_ids(donor_model, trip_id)
    unhealthy = any(
        closure[kind] & donor_bad.get(field, frozenset())
        for field, kind in _HEALTH_KEYS.items()
    )
    donor_model.health[trip_id] = unhealthy
    return unhealthy


def _drop_trip_rows(base_tables, dropped, patches):
    import pandas as pd

    if not dropped:
        return

    def put(table, frame):
        # A table emptied by the drop must disappear entirely: a
        # zero-row file would validate as an empty_file ERROR.
        if len(frame):
            base_tables[table] = frame.reset_index(drop=True)
        else:
            del base_tables[table]

    def keep(table, column):
        frame = base_tables.get(table)
        if frame is None or column not in frame.columns:
            return
        mask = frame[column].isin(dropped)
        if mask.any():
            put(table, frame[~mask])

    # trips.txt and stop_times.txt rows ARE the replaced trip; their
    # removal is what the replace_trip entry itself records.
    keep("trips.txt", "trip_id")
    keep("stop_times.txt", "trip_id")
    frequencies = base_tables.get("frequencies.txt")
    if frequencies is not None and "trip_id" in frequencies.columns:
        mask = frequencies["trip_id"].isin(dropped)
        if mask.any():
            for trip_id in sorted(set(frequencies.loc[mask, "trip_id"])):
                patches.append(
                    {
                        "action": "drop_dependent",
                        "filename": "frequencies.txt",
                        "ids": [str(trip_id)],
                        "reason": "schedules a replaced trip",
                    }
                )
            put("frequencies.txt", frequencies[~mask])
    for table, columns in (
        ("transfers.txt", ("from_trip_id", "to_trip_id")),
        ("attributions.txt", ("trip_id",)),
    ):
        frame = base_tables.get(table)
        if frame is None:
            continue
        present = [column for column in columns if column in frame.columns]
        if not present:
            continue
        mask = pd.Series(False, index=frame.index)
        for column in present:
            mask |= frame[column].isin(dropped)
        if mask.any():
            for _, row in frame[mask].iterrows():
                patches.append(
                    {
                        "action": "drop_dependent",
                        "filename": table,
                        "ids": sorted(
                            {
                                str(row[column])
                                for column in present
                                if str(row[column]) in dropped
                            }
                        ),
                        "reason": "references a replaced trip",
                    }
                )
            put(table, frame[~mask])
    translations = base_tables.get("translations.txt")
    if translations is not None and {"table_name", "record_id"} <= set(
        translations.columns
    ):
        mask = translations["table_name"].isin(("trips", "stop_times")) & translations[
            "record_id"
        ].isin(dropped)
        if mask.any():
            for _, row in translations[mask].iterrows():
                patches.append(
                    {
                        "action": "drop_dependent",
                        "filename": "translations.txt",
                        "ids": [str(row["record_id"])],
                        "reason": "translates a replaced trip",
                    }
                )
            put("translations.txt", translations[~mask])


def _closure_tables(donor_model, trip_ids):
    """One deduplicated union closure for every selected donor trip."""
    import pandas as pd

    union = {key: set() for key in _HEALTH_KEYS.values()}
    for trip_id in trip_ids:
        closure = _closure_ids(donor_model, trip_id)
        for kind, values in closure.items():
            union[kind] |= values

    tables = donor_model.tables
    out = {}

    def take(name, column, wanted):
        frame = tables.get(name)
        if frame is None or column not in frame.columns:
            return
        selected = frame[frame[column].isin(wanted)]
        if not selected.empty:
            out[name] = selected.reset_index(drop=True).copy()

    take("trips.txt", "trip_id", union["trips"])
    take("stop_times.txt", "trip_id", union["trips"])
    take("frequencies.txt", "trip_id", union["trips"])
    take("stops.txt", "stop_id", union["stops"])
    take("levels.txt", "level_id", union["levels"])
    take("routes.txt", "route_id", union["routes"])
    take("agency.txt", "agency_id", union["agencies"])
    take("calendar.txt", "service_id", union["services"])
    take("calendar_dates.txt", "service_id", union["services"])
    take("shapes.txt", "shape_id", union["shapes"])
    take("networks.txt", "network_id", union["networks"])

    # Associative rows come along only when EVERY id they reference is
    # inside the closure; donor translations and fares never do.
    def associative(name, checks):
        frame = tables.get(name)
        if frame is None:
            return
        keep = pd.Series(True, index=frame.index)
        relevant = pd.Series(False, index=frame.index)
        for column, wanted in checks:
            if column not in frame.columns:
                continue
            # Membership uses the raw id; the stripped view only says
            # whether the reference is blank.
            values = frame[column].astype(str)
            filled = values.str.strip() != ""
            relevant |= filled
            keep &= ~filled | values.isin(wanted)
        selected = frame[keep & relevant]
        if not selected.empty:
            out[name] = selected.reset_index(drop=True).copy()

    associative(
        "transfers.txt",
        [
            ("from_stop_id", union["stops"]),
            ("to_stop_id", union["stops"]),
            ("from_trip_id", union["trips"]),
            ("to_trip_id", union["trips"]),
            ("from_route_id", union["routes"]),
            ("to_route_id", union["routes"]),
        ],
    )
    associative(
        "attributions.txt",
        [
            ("agency_id", union["agencies"]),
            ("route_id", union["routes"]),
            ("trip_id", union["trips"]),
        ],
    )
    associative(
        "route_networks.txt",
        [("route_id", union["routes"]), ("network_id", union["networks"])],
    )
    return out


def _free_prefix(base_tables):
    """`donor`, or a numbered variant when base ids already use it."""
    from transitio.gtfs._merge import _ID_COLUMNS

    # One pass over the ids collects every occupied donor-prefix, so
    # a feed full of them cannot make selection quadratic.
    taken = set()
    for name, columns in _ID_COLUMNS.items():
        frame = base_tables.get(name)
        if frame is None:
            continue
        for column in columns:
            if column not in frame.columns:
                continue
            values = frame[column].astype(str)
            prefixed = values[values.str.startswith("donor")]
            heads = prefixed.str.split(":", n=1)
            taken.update(head[0] for head in heads if len(head) == 2)

    prefix = "donor"
    counter = 2
    while prefix in taken:
        prefix = f"donor{counter}"
        counter += 1
    return prefix
