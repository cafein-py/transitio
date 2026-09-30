"""The one-call acquisition pipeline."""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import io
import json
import os
import pathlib
import warnings
import zipfile

# Coarse mode names over GTFS route types, including the extended blocks:
# railway 100s and suburban railway 300s are rail; urban railway 400s,
# metro 500s, underground 600s and monorail join subway; coach 200s,
# bus 700s and trolleybus 800s join bus; tram 900s; water 1000s and
# ferry 1200s are ferry. Aerial, funicular, taxi and air map to no mode.
_MODE_TYPES = {
    "tram": {0, 5} | set(range(900, 1000)),
    "subway": {1, 12} | set(range(400, 700)),
    "rail": {2} | set(range(100, 200)) | set(range(300, 400)),
    "bus": {3, 11} | set(range(200, 300)) | set(range(700, 900)),
    "ferry": {4} | set(range(1000, 1100)) | set(range(1200, 1300)),
}

# Decompressed budget for the pre-validation routes.txt peek; any real
# routes.txt is far smaller, and validation applies the full budgets later.
_MODES_BYTE_CAP = 64 * 1024 * 1024

# Seconds a conditional HEAD probe may take before it counts as unanswered.
_PROBE_TIMEOUT = 5.0

# Two delivered feeds are versions of one service when their route keys and
# their stops, at these decimals, overlap (Jaccard) this much or more.
_ROUTE_OVERLAP = 0.9
_STOP_OVERLAP = 0.8
_STOP_DECIMALS = 3

# Metres the place path grows the OSM area by: cafein's default snap distance.
_OSM_BUFFER_M = 1600

# The fields of a selection-record entry, in selection_table's column order.
_SELECTION_FIELDS = (
    "feed_id",
    "name",
    "decision",
    "reason",
    "note",
    "index_window",
    "feed_window",
    "same_as",
    "contained_in",
    "version_of",
    "path",
)


@dataclasses.dataclass
class FetchResult:
    """What the pipeline produced for one AOI."""

    osm_pbf: pathlib.Path | None
    feeds: list
    reports: list
    repairs: list
    skipped: list
    selections: list = dataclasses.field(default_factory=list)
    provenance: dict = None
    # The index snapshot the feeds were discovered from; None for the AOI path,
    # which discovers by bounding box and has no snapshot.
    snapshot: str = None
    # {feed id: [ids of delivered feeds containing it]} over the delivered
    # feeds, from the index's contained_in (schema 10); empty otherwise.
    contained: dict = dataclasses.field(default_factory=dict)
    # One entry per candidate feed, in candidate order, with its decision;
    # ``skipped`` lists the same skips. A last entry with feed_id None notes
    # the place parts the OSM extract leaves out.
    selection: list = dataclasses.field(default_factory=list)
    # The WGS84 area the OSM extract was fetched for; None without one.
    osm_area: object = None

    def __iter__(self):  # convenient (pbf, feeds) unpacking
        return iter((self.osm_pbf, self.feeds))

    def selection_table(self):
        """The selection record as a ``pandas.DataFrame``, one row per
        candidate feed, then the OSM-area note row when there is one."""
        import pandas as pd

        return pd.DataFrame(self.selection, columns=list(_SELECTION_FIELDS))

    def to_cafein(self, **options):
        """Build a routable ``cafein.TransportNetwork`` from this result.

        The validated feeds and the OSM extract are handed to
        ``cafein.TransportNetwork.from_gtfs``; keyword arguments pass
        through (``walking_speed_kmph``, ``bounding_box``, ``ultra``,
        ...), and ``osm_pbf=None`` builds without the walking network.

        Requires the ``cafein`` package.
        """
        if not self.feeds:
            raise ValueError("no feeds to build a network from")
        try:
            import cafein
        except ImportError as error:
            raise ImportError(
                "the cafein package is required for to_cafein()"
            ) from error
        if self.osm_pbf is not None:
            options.setdefault("osm_pbf", os.fspath(self.osm_pbf))
        paths = [os.fspath(path) for path in self.feeds]
        return cafein.TransportNetwork.from_gtfs(paths, **options)

    def to_pyrosm(self, **options):
        """Open the OSM extract as a ``pyrosm.OSM`` reader.

        Keyword arguments pass through to ``pyrosm.OSM`` (for example
        ``bounding_box`` to read a sub-area of the cropped extract).
        """
        if self.osm_pbf is None:
            raise ValueError(
                "this result has no OSM extract; it was fetched with osm=False"
            )
        from pyrosm import OSM

        return OSM(os.fspath(self.osm_pbf), **options)


def _feed_modes(path):
    """Coarse modes served by a feed, from its routes.txt with values and
    header names stripped as ``FeedEditor`` strips them; rows with extra
    fields are skipped.

    Returns ``None`` when routes.txt cannot be read (missing, over the
    byte budget, or malformed) so the caller can report the feed as
    undeterminable rather than silently unfiltered.
    """
    import pandas as pd

    from transitio.edit._editor import _normalise_table

    try:
        with zipfile.ZipFile(path) as archive:
            with archive.open("routes.txt") as handle:
                data = handle.read(_MODES_BYTE_CAP + 1)
        if len(data) > _MODES_BYTE_CAP:
            return None
        # Read headerless, so the header row sets the width: a row with
        # extra fields is skipped wherever it is, never read as an index.
        rows = pd.read_csv(
            io.BytesIO(data),
            header=None,
            dtype=str,
            keep_default_na=False,
            encoding="utf-8-sig",
            encoding_errors="replace",
            on_bad_lines="skip",
        )
        routes = _normalise_table(rows.iloc[1:].set_axis(list(rows.iloc[0]), axis=1))[0]
        values = routes.loc[:, routes.columns == "route_type"].to_numpy().ravel()
        types = {int(value) for value in set(values) if value.lstrip("-").isdigit()}
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return None
    return {mode for mode, accepted in _MODE_TYPES.items() if types & accepted}


def _bbox_area(feed):
    """Bounding-box area of a feed's data, or ``None`` when unknown."""
    raw = feed.raw or {}
    bounding_box = (raw.get("latest_dataset") or {}).get("bounding_box") or {}
    values = []
    for key in (
        "minimum_longitude",
        "maximum_longitude",
        "minimum_latitude",
        "maximum_latitude",
    ):
        value = bounding_box.get(key, raw.get(f"location.bounding_box.{key}"))
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            return None
    min_lon, max_lon, min_lat, max_lat = values
    return abs(max_lon - min_lon) * abs(max_lat - min_lat)


def _rank(feed):
    """The plan's documented deterministic preference for overlapping feeds.

    Official before unofficial, active status before anything else, then
    spatial specificity (smaller data bounding box first, unknown extent
    last), with the feed ID as a stable tie-breaker.
    """
    area = _bbox_area(feed)
    return (
        not feed.official,
        feed.status != "active",
        area is None,
        area or 0.0,
        feed.id,
    )


def _today():
    return datetime.date.today()


def _window(start, end):
    """``[start, end]`` as ISO date strings, None when both are unknown."""
    if start is None and end is None:
        return None
    return [None if day is None else day.isoformat() for day in (start, end)]


def _misses(start, end, day, study):
    """Why a service window from ``start`` to ``end`` misses ``day``, or None.

    With a ``study`` day the window must cover it; otherwise it must only not
    end before it. An unknown bound (``None``) cannot miss.
    """
    if end is not None and end < day:
        return f"service ended {end.isoformat()}"
    if study and start is not None and start > day:
        return f"service starts {start.isoformat()}, after {day.isoformat()}"
    return None


def _idle(validation, day):
    """Whether a validation report proves that nothing runs on ``day``: its
    ``moment`` for the day counts no active trip and it carries the
    ``no_service_on_reference_date`` notice. A report without a moment for
    the day proves nothing."""
    moment = validation.get("moment") or {}
    return (
        moment.get("referenceDate") == day.strftime("%Y%m%d")
        and moment.get("activeTrips") == 0
        and any(
            notice.get("code") == "no_service_on_reference_date"
            for notice in validation.get("notices") or ()
        )
    )


def _entry(feed_id, name, index_window=None):
    """An undecided selection-record entry for one candidate feed."""
    entry = dict.fromkeys(_SELECTION_FIELDS)
    entry.update(
        feed_id=feed_id,
        name=name,
        index_window=index_window,
        same_as=[],
        contained_in=[],
    )
    return entry


def _skip(entry, reason, **fields):
    entry.update(decision="skipped", reason=reason, **fields)


def _note(entry, text):
    """Add ``text`` to an entry's note, after any note it has."""
    entry["note"] = text if entry["note"] is None else f"{entry['note']}; {text}"


def _skipped(selection):
    """The ``(feed id, reason)`` pairs of the skipped entries."""
    return [
        (entry["feed_id"], entry["reason"])
        for entry in selection
        if entry["decision"] == "skipped"
    ]


class _SkipFeed(Exception):
    """A per-feed reason to skip, carried out of the shared processing with
    the feed's computed service window when it was validated."""

    def __init__(self, reason, window=None):
        super().__init__(reason, window)
        self.reason = reason
        self.window = window

    def __str__(self):
        return self.reason


def _process_feed(
    path,
    *,
    geometry,
    tag,
    repair,
    crop,
    modes,
    day,
    study,
    hosted,
    budgets,
    routes=None,
):
    """Crop, repair, mode-filter, validate and report one downloaded feed.

    The computed service window is tested against ``day`` (None tests
    nothing): with a ``study`` day it must cover the day and the validation
    report must not prove the day idle; otherwise it must not end before it.

    Returns ``(path, report, fixes, present_routes, window)``; ``present_routes``
    is the set of ``route_id`` values in the downloaded feed as it enters the
    route crop, or ``None`` when a ``routes`` filter is not applied or that
    feed's routes.txt cannot be read — so a caller records an *undetermined*
    drop rather than a false empty one — and ``window`` the computed service
    window as ISO dates, None when unknown. Raises :class:`_SkipFeed` when the
    feed drops out. Shared by the AOI and the place paths.
    """
    from transitio.gtfs import crop_feed
    from transitio.repair import repair_feed
    from transitio.report import build_report
    from transitio.validate import validate_feed

    provenance = None
    sidecar = path.with_suffix(".provenance.json")
    if sidecar.exists():
        provenance = json.loads(sidecar.read_text())
    present_routes = None
    source_notices = []
    if crop or routes is not None:
        cropped = path.with_name(f"{path.stem}-cropped-{tag}.zip")
        report = crop_feed(
            path, cropped, aoi=geometry if crop else None, routes=routes, **budgets
        )
        if routes is not None:
            # From the crop's own scan of this feed, so the drop audit and the
            # crop describe the same bytes (no second read to race). ``None``
            # (routes.txt or its column absent) stays undetermined, not empty.
            source = report.get("source_routes")
            present_routes = None if source is None else set(source)
        # The crop writes trimmed tables; the source's whitespace is
        # reported with the feed.
        source_notices = report["source_notices"]
        path = cropped
    # The crop comes first, so the repair works on the area's feed rather
    # than on the whole source.
    fixes = []
    if repair:
        repaired = path.with_name(f"{path.stem}-repaired-{tag}.zip")
        fixes = repair_feed(path, repaired, **budgets)["fixes"]
        path = repaired
    if modes is not None:
        served = _feed_modes(path)
        if served is None:
            raise _SkipFeed("could not read routes.txt for mode filtering")
        if not served & modes:
            raise _SkipFeed(f"serves {sorted(served)}, not {sorted(modes)}")
    validation = validate_feed(path, **budgets)
    start = end = None
    if validation["service_window"]:
        start, end = (
            datetime.datetime.strptime(value, "%Y%m%d").date()
            for value in validation["service_window"]
        )
    window = _window(start, end)
    if day is not None:
        reason = _misses(start, end, day, study)
        if reason is None and study and _idle(validation, day):
            reason = f"no service on {day.isoformat()}"
        if reason is not None:
            raise _SkipFeed(reason, window)
    validation["notices"].extend(source_notices)
    report = build_report(validation, hosted=hosted, provenance=provenance)
    return path, report, fixes, present_routes, window


def _hash_stream(handle):
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1 << 20), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _snapshot(path):
    """``(archive SHA-256, entries)`` of the zip at ``path``, read through one
    open: the entries as sorted ``(name, CRC-32, size)`` from the central
    directory, nothing decompressed. None when it cannot be read."""
    try:
        with open(path, "rb") as handle:
            digest = _hash_stream(handle)
            handle.seek(0)
            with zipfile.ZipFile(handle) as archive:
                listing = sorted(
                    (info.filename, info.CRC, info.file_size)
                    for info in archive.infolist()
                    if not info.is_dir()
                )
        return digest, tuple(listing)
    except Exception:  # noqa: B902 — an unreadable archive matches nothing
        return None


def _entry_digests(path, digest):
    """Every entry of the zip at ``path`` as sorted ``(name, CRC-32, size,
    SHA-256)``, one row per entry, read through one open that must still hold
    the archive ``digest`` recorded earlier; None when it does not or the
    archive cannot be read."""
    try:
        with open(path, "rb") as handle:
            if _hash_stream(handle) != digest:
                return None
            handle.seek(0)
            rows = []
            with zipfile.ZipFile(handle) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    with archive.open(info) as member:
                        entry = _hash_stream(member)
                    rows.append((info.filename, info.CRC, info.file_size, entry))
        return tuple(sorted(rows))
    except Exception:  # noqa: B902 — an unreadable archive matches nothing
        return None


class _Delivered:
    """The downloads a call has delivered, with the route filter each was
    cropped to (None: all routes), so a later download with the same content
    can be matched with them. Each download is described as it was when
    checked, before it was processed."""

    def __init__(self):
        self._feeds = []
        self._facts = {}
        self._entries = {}

    def _about(self, path):
        if path not in self._facts:
            self._facts[path] = _snapshot(path)
        return self._facts[path]

    def _digests(self, path):
        if path not in self._entries:
            self._entries[path] = _entry_digests(path, self._facts[path][0])
        return self._entries[path]

    def same_as(self, path):
        """The ``(feed id, routes)`` of each delivered feed whose download
        holds the same content as ``path``, in delivery order. Same content:
        equal archive digests, or equal entry listings (name, CRC-32, size)
        confirmed by equal SHA-256 digests of every entry. An archive that
        cannot be read, or that changed since it was checked, matches
        nothing."""
        facts = self._about(path)
        if facts is None:
            return []
        found = []
        for feed_id, other, cropped_to in self._feeds:
            recorded = self._facts[other]
            if recorded is None:
                continue
            if facts[0] == recorded[0]:
                found.append((feed_id, cropped_to))
            elif facts[1] and facts[1] == recorded[1]:
                mine = self._digests(path)
                if mine is not None and mine == self._digests(other):
                    found.append((feed_id, cropped_to))
        return found

    def add(self, feed_id, path, routes=None):
        self._about(path)
        self._feeds.append((feed_id, path, routes))


def _covers(twins, routes):
    """Whether the deliveries ``twins`` (``(feed id, routes)`` of one archive)
    carry every route of ``routes``; None stands for all routes."""
    cuts = [cut for _, cut in twins]
    if any(cut is None for cut in cuts):
        return True
    return bool(cuts) and routes is not None and routes <= set().union(*cuts)


def _containers(feed, entries, carriers, cropped, current):
    """``(proven, notes)``: a candidate's containers carried whole and
    proven unchanged before download, and why each other decided one drops
    nothing."""
    proven, notes = [], []
    for container in feed.contained_in:
        if container == feed.feed_id or container not in entries:
            continue
        decision = entries[container]["decision"]
        if decision is None:
            continue
        if container in carriers:
            if current.get(container):
                proven.append(container)
            else:
                notes.append("kept: containment not proven current")
        elif container in cropped:
            notes.append(f"kept: container {container} cropped to selected routes")
        else:
            notes.append(f"kept: container {container} skipped")
    return proven, notes


def _containers_first(feeds):
    """``feeds`` with each one after the feeds among them that contain it
    (``contained_in``), in their given order otherwise."""
    by_id = {feed.feed_id: feed for feed in feeds}
    depth = {}

    def level(feed, seen=()):
        if feed.feed_id not in depth:
            above = [
                by_id[c]
                for c in feed.contained_in
                if c in by_id and c not in seen and c != feed.feed_id
            ]
            depth[feed.feed_id] = 1 + max(
                (level(c, (*seen, feed.feed_id)) for c in above), default=-1
            )
        return depth[feed.feed_id]

    return sorted(feeds, key=level)


def _read_tables(path, names, max_total_bytes=None):
    """The tables ``names`` of a feed zip, read as ``FeedEditor`` does; None
    when together they are over ``max_total_bytes`` (default: the
    ``FeedEditor`` budget)."""
    import pandas as pd

    from transitio.edit._editor import _MAX_TOTAL_BYTES, _normalise_table

    limit = _MAX_TOTAL_BYTES if max_total_bytes is None else max_total_bytes
    csv = {"dtype": str, "keep_default_na": False, "encoding": "utf-8-sig"}
    with zipfile.ZipFile(path) as archive:
        members = [m for m in archive.infolist() if m.filename in names]
        if sum(m.file_size for m in members) > limit:
            return None
        return {
            m.filename: _normalise_table(pd.read_csv(archive.open(m), **csv))[0]
            for m in members
        }


def _service(path, day=None, max_total_bytes=None):
    """A delivered feed's route keys, rounded stop coordinates and trip
    count; with a ``day``, the signatures of its trips running then that are
    not frequency-based, whether those are all of them, and whether it has
    transfers or pathways, from those tables only, read as ``FeedEditor``
    does. None when they are over ``max_total_bytes``, a route key has a
    blank part, a stop or station lacks coordinates, or with a ``day`` its
    calendars cannot be read."""
    import pandas as pd

    from transitio.gtfs._schedule import route_keys, service_dates, trip_signatures

    names = {"agency.txt", "routes.txt", "stops.txt", "trips.txt"}
    if day is not None:
        names |= {"stop_times.txt", "calendar.txt", "calendar_dates.txt"}
        names |= {"frequencies.txt", "transfers.txt", "pathways.txt"}
    try:
        tables = _read_tables(path, names, max_total_bytes)
        if tables is None:
            return None
        keys = route_keys(tables)[["agency", "name", "type"]]
        stops = tables["stops.txt"]
        points = stops[["stop_lat", "stop_lon"]].apply(pd.to_numeric, errors="coerce")
        # Stops and stations need coordinates; other location types may lack them.
        kind = stops.get("location_type", pd.Series("", index=stops.index))
        located = points[kind.str.strip().isin(("", "0", "1"))]
        if (keys == "").any(axis=None) or located.isna().any(axis=None):
            return None
        points = points.round(_STOP_DECIMALS).add(0.0).dropna()
        trips = tables.get("trips.txt", pd.DataFrame(columns=["trip_id", "service_id"]))
        found = {
            "routes": set(keys.itertuples(index=False, name=None)),
            "stops": set(points.itertuples(index=False, name=None)),
            "trips": len(trips),
        }
        if not (found["routes"] and found["stops"]):
            return None
        if day is None:
            return found
        dates, unexpanded = service_dates(tables)
        if unexpanded or dates.empty:
            return None
        running = dates.loc[dates["date"] == pd.Timestamp(day), "service_id"]
        on_day = trips.loc[trips["service_id"].isin(running), "trip_id"]
        signed = trip_signatures(tables)
        repeated = tables.get("frequencies.txt", pd.DataFrame(columns=["trip_id"]))
        signed = signed[
            signed["trip_id"].isin(on_day)
            & ~signed["trip_id"].isin(repeated["trip_id"])
        ]
    except Exception:  # noqa: B902 — an unreadable feed is never grouped
        return None
    found.update(
        day=set(signed["signature"]),
        complete=len(signed) == len(on_day),
        linked=any(len(tables.get(n, ())) for n in ("transfers.txt", "pathways.txt")),
    )
    return found


def _timezone_note(path, max_total_bytes=None):
    """``"agency_timezone <names>; stops in <zone>"`` for a feed declaring no
    time zone equivalent to the zone of most of its stops (:func:`_stop_zone`),
    compared over today and the next year as no calendar is read; None when
    one is, when either is unknown, or when agency.txt and stops.txt are
    unreadable or over ``max_total_bytes``."""
    from transitio.gtfs._merge import (
        _stop_zone,
        _timezone_interval,
        _timezones,
        _zone_classes,
    )

    try:
        tables = _read_tables(path, {"agency.txt", "stops.txt"}, max_total_bytes)
    except Exception:  # noqa: B902 — an unreadable feed gets no note
        return None
    if tables is None:
        return None
    declared, located = _timezones(tables), _stop_zone(tables)
    if not declared or located is None:
        return None
    classes = _zone_classes(declared | {located}, _timezone_interval([tables]))
    if any(classes[zone] == classes[located] for zone in declared):
        return None
    return f"agency_timezone {', '.join(sorted(declared))}; stops in {located}"


def _settle_versions(record, services, protected, day):
    """Settle the versions among the delivered feeds ``services`` (``{feed
    id: _service(...)}``) as :func:`fetch` describes, the connected
    components of the version pairs being the groups, and note or skip
    their entries in ``record``; returns the ids of the feeds left out."""
    entries = {entry["feed_id"]: entry for entry in record}
    order = {feed_id: position for position, feed_id in enumerate(entries)}

    def rank(feed_id):
        start = (entries[feed_id]["feed_window"] or [None])[0]
        later = -datetime.date.fromisoformat(start).toordinal() if start else 0
        return (start is None, later, -services[feed_id]["trips"], order[feed_id])

    ids = sorted(services, key=rank)
    pairs = {feed_id: {} for feed_id in ids}
    for position, one in enumerate(ids):
        for other in ids[position + 1 :]:
            overlaps = tuple(
                len(services[one][key] & services[other][key])
                / len(services[one][key] | services[other][key])
                for key in ("routes", "stops")
            )
            if overlaps[0] >= _ROUTE_OVERLAP and overlaps[1] >= _STOP_OVERLAP:
                pairs[one][other] = pairs[other][one] = overlaps
    removed, grouped = set(), set()
    for top in ids:
        if top in grouped or not pairs[top]:
            continue
        group, frontier = {top}, [top]
        while frontier:
            for partner in pairs[frontier.pop()]:
                if partner not in group:
                    group.add(partner)
                    frontier.append(partner)
        grouped |= group
        members = [feed_id for feed_id in ids if feed_id in group]
        if day is None:
            for member in members:
                partner = next(other for other in ids if other in pairs[member])
                _note(entries[member], f"similar to {partner}; kept, no study day")
            continue
        kept = [m for m in members if m == top or m in protected]
        covered = set().union(*(services[m]["day"] for m in kept))
        for member in members:
            if member in kept:
                continue
            service = services[member]
            partners = [other for other in kept if other in pairs[member]]
            adds = not (service["complete"] and service["day"] <= covered)
            if partners and not adds and not service["linked"]:
                removed.add(member)
                continue
            if partners:
                why = "; kept, has transfers or pathways"
                why = f" but adds service on {day}" if adds else why
                _note(entries[member], f"similar to {partners[0]}{why}")
            kept = sorted([*kept, member], key=ids.index)
            covered |= service["day"]
        for member in [m for m in members if m in removed]:
            partner = next(other for other in kept if other in pairs[member])
            route, stop = (round(share, 3) for share in pairs[member][partner])
            version = {"feed_id": partner, "route_overlap": route, "stop_overlap": stop}
            reason = f"another version of {partner}"
            _skip(entries[member], reason, note=None, path=None, version_of=version)
    return removed


def _download_indexed(feed, db, atlas, base_dir):
    """Download an indexed feed, preferring its Mobility Database URL over its
    Transitland Atlas URL (decision I: MDB wins where a feed has both), and
    falling back to Atlas when the MDB download fails. Each feed lands in its
    own digest-named directory under ``base_dir``, so several never collide."""
    from transitio.catalog import AtlasFeed, Feed
    from transitio.catalog._atlas import _feed_dir
    from transitio.exceptions import DownloadError
    from transitio.index.feeds import _parse

    mdb = _parse(feed._row.get("mdb")) or {}
    mdb_urls = mdb.get("urls") or {}
    mdb_url = mdb_urls.get("direct_download") or mdb_urls.get("latest")
    atlas_feed = AtlasFeed.from_record(
        _parse(feed._row.get("atlas")) or {}, feed_id=feed.feed_id
    )
    errors = []
    if mdb_url:
        try:
            proxy = Feed.from_api(
                {"id": feed.feed_id, "latest_dataset": {"hosted_url": mdb_url}}
            )
            return db.download_latest(
                proxy, directory=base_dir / _feed_dir(feed.feed_id)
            )
        except Exception as error:  # noqa: B902 — fall through to the fallback
            errors.append(f"mdb: {error}")
    if atlas_feed.static_url:
        try:
            return atlas.download(atlas_feed, directory=base_dir)
        except Exception as error:  # noqa: B902
            errors.append(f"atlas: {error}")
    if errors:
        raise DownloadError("; ".join(errors))
    raise DownloadError(f"feed {feed.feed_id} has no downloadable url")


def _unchanged_since_indexed(feed, http):
    """Whether the archive the index crawled for an indexed feed is still the
    one served: a conditional ``HEAD`` to the URL the crawl reads (the Atlas
    static feed, else the Mobility Database direct download), carrying the
    ETag and Last-Modified it recorded, answers 304 Not Modified. Returns
    that URL, or None: any other answer, a failed probe or no recorded
    validator is no proof."""
    from transitio.catalog._atlas import STATIC_URL
    from transitio.index.feeds import _parse, _scalar

    atlas = (_parse(feed._row.get("atlas")) or {}).get("urls") or {}
    mdb = (_parse(feed._row.get("mdb")) or {}).get("urls") or {}
    url = atlas.get(STATIC_URL) or mdb.get("direct_download")
    headers = {}
    etag = _scalar(feed._row.get("etag"))
    last_modified = _scalar(feed._row.get("last_modified"))
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    if not url or not headers:
        return None
    try:
        response = http.head(url, headers=headers, timeout=_PROBE_TIMEOUT)
    except Exception:  # noqa: B902 — an unanswered probe proves nothing
        return None
    return url if response.status_code == 304 else None


def fetch(
    aoi=None,
    when=None,
    *,
    place=None,
    tiers=None,
    exclude=None,
    on_unknown="include",
    on_untrusted_selector="auto",
    contained="drop",
    index=None,
    modes=None,
    expired="skip",
    repair=False,
    crop=True,
    osm=True,
    refresh_token=None,
    cache_dir=None,
    directory=None,
    country_code=None,
    **budgets,
):
    """Fetch everything cafein needs for an area in one call.

    Pass exactly one of ``aoi`` (a geometry, bbox or place name geocoded for
    the OSM stage) or ``place`` (a place name, QID or :class:`Place`). With
    ``place``, feeds are selected from the built index by tier -- ``tiers``,
    ``exclude`` and ``on_unknown`` filter the edges -- and the place geometry
    supplies the AOI; ``tiers``, ``exclude``, ``on_unknown``,
    ``on_untrusted_selector``, ``contained`` and ``index`` apply only with
    ``place``, and ``country_code`` only with ``aoi``. When a selector cannot be trusted --
    its evidence was missing at build time, or its fingerprint no longer
    matches the download -- ``on_untrusted_selector`` decides the outcome:
    ``"auto"`` (default) skips the feed when an ``exclude`` was asked for and
    otherwise delivers it whole with its tier treated as ``unknown``;
    ``"whole"`` always delivers it whole; ``"drop"`` always skips it;
    ``"error"`` raises :class:`~transitio.exceptions.StaleSelectorError`.
    A schema-10 index records the larger feeds whose stops and routes contain
    a feed's, and ``FetchResult.contained`` reports the delivered pairs. With
    ``contained="drop"`` (default) containers are processed first, and a
    contained feed is left out before download (``"contained in <id>"``)
    when a container was delivered whole (not cut to a route selection) or
    skipped as the same content as a feed delivered whole, and conditional
    ``HEAD`` probes (as for ``expired``) prove both archives unchanged since
    indexed: the container's, sent before its download, and the contained
    feed's. A container downloaded as a catalogued dataset proves nothing.
    Otherwise the feed is processed as usual and its ``note`` says why:
    ``"kept: containment not proven current"``, ``"kept: container <id>
    skipped"`` or ``"kept: container <id> cropped to selected routes"``.
    ``contained="keep"`` leaves no feed out for containment.

    Resolves and crops the OSM extract, discovers the GTFS feeds (overlapping
    the AOI, or the place's indexed feeds), downloads each feed, spatially
    crops it, optionally repairs it, validates it, and builds a merged report
    per feed.
    With an API token, downloads come from catalogued dataset versions
    (checksum-verified, with the hosted canonical-validator report);
    without one, the unversioned latest hosted zip is fetched — a moving
    target with no upstream checksum, documented in its provenance
    sidecar as such. Every overlapping feed is processed, in a
    deterministic order with official feeds first; one broken feed never
    aborts the others — it lands in ``skipped`` with its reason. A download
    whose content equals a feed already delivered in the call is skipped as
    ``"same content as <feed id>"`` when its routes are within those
    delivered from that archive (a feed delivered whole carries all);
    otherwise it is delivered cut to its own routes, ``same_as`` naming the
    earlier feed.

    On the place path, delivered feeds whose route keys (agency name, route
    short else long name, type) and stops (coordinates at 3 decimals) share
    0.9 and 0.8 or more are versions, ranked by later start, more trips,
    then candidate order. With ``when``, one is left out as ``"another
    version of <id>"`` when a kept version pairs with it and kept versions
    run, by trip signature, every non-frequency trip it runs on the day;
    the top version and the containers a left-out feed relied on stay, as
    does one with transfers or pathways. A left-out version's fares are not
    delivered. Without ``when`` none is left out; similar feeds are noted.
    A feed whose routes, stops or, with ``when``, calendars cannot be read
    is never a version.

    Parameters
    ----------
    aoi : geometry, GeoDataFrame/GeoSeries, tuple or str
        Area of interest (place names are geocoded via Nominatim once,
        and the resulting geometry drives every stage).
    when : str or datetime.date, optional
        Study day the feeds must run on, ``YYYY-MM-DD``. Dataset-version
        selection needs an API token; with or without one, a feed is
        skipped when its computed service window (the outer bounds of
        actual calendar activity, not the published range) ends before the
        day (``"service ended <end>"``) or starts after it (``"service
        starts <start>, after <day>"``), or when its validation report for
        the day counts no active trip and carries the
        ``no_service_on_reference_date`` notice (``"no service on
        <day>"``). Without ``when`` there is no study day, only today: a
        feed is skipped only when its computed window ended before today,
        and one that starts later or runs on other weekdays stays. An
        unknown window passes the window checks, and a report without a
        ``moment`` for the day passes the day check.
    modes : str or list of str, optional
        Keep only feeds serving at least one of ``tram``, ``subway``,
        ``rail``, ``bus``, ``ferry`` — decided from the delivered
        (post-crop) feed's routes.txt, since the catalog carries no mode
        metadata. Unknown mode names raise ``ValueError``.
    expired : {"skip", "keep"}, default "skip"
        With ``"skip"``, on the place path, an indexed feed whose index
        service window misses the day (ends before it, or starts after the
        study day) is skipped before download when a conditional ``HEAD``
        to the URL the index crawled, carrying the ETag or Last-Modified it
        recorded, answers 304 Not Modified: the served archive is still the
        one the index saw, and the reason ends ``"; unchanged since
        indexed"``. Any other answer, or no recorded validator, downloads
        the feed and leaves the decision to its computed window, as does a
        feed downloaded as a catalogued dataset (with an API token).
        ``"keep"`` sends no probe and, without ``when``, keeps a feed whose
        computed window ended; with ``when`` the window and day checks
        still apply.
    repair : bool, default False
        Repair each feed (gtfstidy contract) after the crop, before use;
        conservative default leaves feeds untouched.
    crop : bool, default True
        Spatially crop each feed to the area: to its polygon when it has
        one (a place's boundary included), otherwise to its bounding box.
    osm : bool, default True
        Fetch the OSM extract for the AOI. With ``place``, it is fetched
        after the feeds, for the place's parts that hold a stop of a
        delivered feed (the whole place when none does, nothing was
        delivered or a delivered feed's stops.txt cannot be read), each part
        grown by 1.6 km, cafein's default snap distance. With ``osm=False``
        the OSM stage is skipped and the result's ``osm_pbf`` is None, for
        callers who want only the GTFS feeds; ``to_pyrosm`` then raises and
        ``to_cafein`` builds without a walking network.
    refresh_token, cache_dir, directory, country_code
        Passed to the catalog and OSM layers.
    **budgets
        The ``validate_feed`` keyword arguments. A feed with a table that a
        budget cuts short cannot be cropped and lands in ``skipped``, the
        reason naming the file and the budget to raise. A reached
        ``max_notices_per_file`` does not stop the crop (the feed's report
        then carries ``notice_limit_reached``), but it does stop
        ``repair=True``. With ``when``, ``reference_date`` is the study day;
        a different one raises ``ValueError``.

    Returns
    -------
    FetchResult
        ``osm_pbf``, validated ``feeds`` (paths), merged ``reports`` and
        repair ``repairs`` (fix logs, empty without ``repair=True``) per
        kept feed, ``skipped`` (feed id, reason) pairs, the ``selection``
        record and, on the place path, the ``contained`` pairs among the
        delivered feeds. Reports merge the local validation of the delivered
        feed with the hosted report of the published dataset, so after
        cropping or repair the hosted side describes the pre-transform
        original. ``selection`` has one entry per candidate feed, in
        candidate order: ``feed_id``, ``name``, ``decision``
        (``"delivered"`` or ``"skipped"``), ``reason`` (why it was skipped),
        ``note`` (about a delivered feed: the routes it was cut to, why a
        contained feed was kept, a similar feed, an ``agency_timezone`` not
        equivalent to the zone of most of its stops, e.g. ``"agency_timezone
        America/New_York; stops in Pacific/Honolulu"``; several join with
        ``"; "``),
        ``index_window`` (the index's ``[start, end]``; None undated or on
        the area path), ``feed_window`` (the computed window of a validated
        download, delivered or not; None otherwise or when unknown),
        ``same_as`` (earlier deliveries of the same archive) and
        ``contained_in`` (the containers a containment skip names),
        ``version_of`` (for a left-out version, ``{"feed_id", "route_overlap",
        "stop_overlap"}`` against the highest-ranked kept version it pairs
        with) and ``path`` (the delivered feed). Windows are ISO dates.
        When the OSM extract leaves out parts of the place, a last entry
        with ``feed_id`` None notes them, e.g. ``"OSM area: 1 of 47 parts
        (1783 of 2188 km²)"``. ``FetchResult.selection_table()`` returns it
        as a DataFrame. ``osm_area`` is the WGS84 geometry the OSM extract
        was fetched for (None with ``osm=False``).
    """
    from transitio.catalog import MobilityDatabase
    from transitio.catalog._models import as_date
    from transitio.osm import fetch_pbf
    from transitio.osm._fetch import _as_geometry

    if (aoi is None) == (place is None):
        raise ValueError("pass exactly one of aoi= or place=")
    if aoi is not None and (
        tiers is not None
        or exclude is not None
        or index is not None
        or on_unknown != "include"
        or on_untrusted_selector != "auto"
        or contained != "drop"
    ):
        raise ValueError(
            "tiers=, exclude=, on_unknown=, on_untrusted_selector=, contained= "
            "and index= apply only with place="
        )
    if contained not in ("keep", "drop"):
        raise ValueError("contained= must be 'keep' or 'drop'")
    if expired not in ("skip", "keep"):
        raise ValueError("expired= must be 'skip' or 'keep'")
    if place is not None and country_code is not None:
        raise ValueError("country_code= applies only with aoi=")
    if on_untrusted_selector not in ("auto", "whole", "drop", "error"):
        raise ValueError(
            "on_untrusted_selector= must be 'auto', 'whole', 'drop' or 'error'"
        )

    if modes is not None:
        if isinstance(modes, str):
            modes = [modes]
        modes = {str(mode).lower() for mode in modes}
        unknown = modes - set(_MODE_TYPES)
        if unknown:
            raise ValueError(
                f"unknown modes {sorted(unknown)}; "
                f"valid modes are {sorted(_MODE_TYPES)}"
            )

    # The day the date rules test: the study day, else today. A downloaded
    # feed's computed window is tested against it unless expired="keep"
    # leaves nothing to test without a study day.
    study = when is not None
    day = as_date(when) if study else _today()
    window_day = day if study or expired == "skip" else None
    if study:
        existing = budgets.get("reference_date")
        if existing is not None and existing != day.strftime("%Y%m%d"):
            raise ValueError("when and reference_date disagree; pass only one")
    budgets.setdefault("reference_date", day.strftime("%Y%m%d") if study else None)

    if place is not None:
        return _fetch_place(
            place,
            tiers=tiers,
            exclude=exclude,
            on_unknown=on_unknown,
            on_untrusted_selector=on_untrusted_selector,
            contained=contained,
            index=index,
            when=when,
            day=day,
            window_day=window_day,
            expired=expired,
            modes=modes,
            repair=repair,
            crop=crop,
            osm=osm,
            refresh_token=refresh_token,
            cache_dir=cache_dir,
            directory=directory,
            budgets=budgets,
        )

    geometry = _as_geometry(aoi)

    # Transformed outputs carry a parameter digest so calls for different
    # AOIs or reference dates never overwrite each other's artefacts.
    tag = hashlib.sha256(
        json.dumps(
            {
                "bounds": [round(v, 6) for v in geometry.bounds],
                "reference_date": budgets.get("reference_date"),
                "crop": crop,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()[:16]

    osm_pbf = (
        fetch_pbf(geometry, cache_dir=cache_dir, directory=directory) if osm else None
    )

    from transitio.catalog._atlas import _feed_dir

    feeds, reports, repairs, record = [], [], [], []
    delivered = _Delivered()
    with MobilityDatabase(refresh_token, cache_dir=cache_dir) as db:
        if when is not None and not db._refresh_token:
            warnings.warn(
                "no Mobility Database API token: 'when' cannot select "
                "historical datasets, using the latest hosted datasets",
                UserWarning,
                stacklevel=2,
            )
        candidates = sorted(
            db.search_feeds(aoi=geometry, country_code=country_code), key=_rank
        )
        for feed in candidates:
            entry = _entry(feed.id, feed.raw.get("feed_name") or feed.provider)
            record.append(entry)
            dataset = None
            if db._refresh_token:
                try:
                    if when is not None:
                        dataset = db.dataset_for(feed, when)
                        if dataset is None:
                            _skip(entry, "no dataset covers the requested day")
                            continue
                    else:
                        # Prefer a versioned dataset (checksum, hosted
                        # report) over the unversioned moving target.
                        versions = db.datasets(feed)
                        dataset = versions[0] if versions else None
                except Exception as error:  # noqa: B902
                    _skip(entry, f"dataset selection failed: {error}")
                    continue
            # Each feed downloads into its own digest-named folder, so two
            # hosted latest.zip files never overwrite each other.
            target = pathlib.Path(directory) / _feed_dir(feed.id) if directory else None
            try:
                if dataset is not None:
                    path = db.download(dataset, directory=target)
                else:
                    path = db.download_latest(feed, directory=target)
            except Exception as error:  # noqa: B902
                _skip(entry, f"download failed: {error}")
                continue
            twins = [twin for twin, _ in delivered.same_as(path)]
            if twins:
                _skip(entry, f"same content as {', '.join(twins)}", same_as=twins)
                continue
            download = path
            hosted = None
            if dataset is not None:
                try:
                    hosted = db.validation_report(dataset)
                except Exception:  # noqa: B902 — the hosted report is optional
                    hosted = None

            try:
                # Modes are read from the delivered feed, after cropping, so an
                # aggregate serving buses only outside the AOI does not pass a
                # bus filter.
                path, report, fixes, _, window = _process_feed(
                    path,
                    geometry=geometry,
                    tag=tag,
                    repair=repair,
                    crop=crop,
                    modes=modes,
                    day=window_day,
                    study=study,
                    hosted=hosted,
                    budgets=budgets,
                )
            except _SkipFeed as skip:
                _skip(entry, skip.reason, feed_window=skip.window)
                continue
            except Exception as error:  # noqa: B902 — isolate per-feed failures
                _skip(entry, f"processing failed: {error}")
                continue
            entry.update(decision="delivered", feed_window=window, path=path)
            note = _timezone_note(path, budgets.get("max_total_bytes"))
            if note is not None:
                _note(entry, note)
            reports.append(report)
            repairs.append(fixes)
            feeds.append(path)
            delivered.add(feed.id, download)

    return FetchResult(
        osm_pbf=osm_pbf,
        feeds=feeds,
        reports=reports,
        repairs=repairs,
        skipped=_skipped(record),
        selection=record,
        osm_area=geometry if osm else None,
    )


def _selector_trusted(path, feed, sel):
    """``(trusted, reason, route_ids)`` for a feed's aggregated selector
    validated against the downloaded feed at ``path``.

    An ``unavailable`` selector is untrustworthy without validation -- it has
    no fingerprint to check. Otherwise every matched edge's stored fingerprint
    must recompute from the download by its own kind; the first that does not
    marks the selector stale. ``route_ids`` is the set the download carries,
    for the caller's route-presence check (each fingerprint kind reads the
    same ``routes.txt``, so the set is complete once any edge validated).
    """
    from transitio.index import fingerprint

    if sel.state == "unavailable":
        return False, "unavailable", set()
    recomputed = {}
    in_feed = set()
    for edge in feed.edges.values():
        kind = edge.fingerprint_kind
        stored = edge.classification_fingerprint
        if kind not in fingerprint.KINDS or not stored:
            return False, "unavailable", in_feed
        if kind not in recomputed:
            recomputed[kind] = fingerprint.from_feed(path, kind)
        digest, routes = recomputed[kind]
        in_feed |= routes
        if digest is None or digest != stored:
            return False, "stale", in_feed
    return True, None, in_feed


def _untrusted_action(policy, exclude, on_unknown):
    """Map an untrustworthy selector to ``"skip"``, ``"whole"`` or ``"error"``
    under ``on_untrusted_selector``.

    Under ``"auto"`` an explicit ``exclude`` is a hard constraint that cannot
    be honoured, so the feed is skipped; otherwise it is delivered whole with
    its tier treated as ``unknown``, and ``on_unknown`` decides its fate.
    """
    if policy == "error":
        return "error"
    if policy == "drop":
        return "skip"
    if policy == "whole":
        return "whole"
    # A truthy exclude names tiers to drop; an empty one excludes nothing, as
    # edge matching treats it, so it is not a hard constraint.
    if exclude:
        return "skip"
    return "skip" if on_unknown == "exclude" else "whole"


def _osm_parts(geometry, feeds):
    """The parts of ``geometry`` the OSM extract is fetched for: the union of
    those holding a stop of a delivered feed in ``feeds``, read from each
    one's stops.txt, else the whole geometry. A feed whose stops.txt cannot
    be read could serve any part, so it also yields the whole geometry."""
    import shapely

    from transitio.index.fingerprint import _member_coords

    parts = shapely.get_parts(geometry)
    tree = shapely.STRtree(parts)
    held = set()
    for path in feeds:
        try:
            with zipfile.ZipFile(path) as archive:
                coords = _member_coords(archive)
        except Exception:  # noqa: B902 — unreadable, like an absent stops.txt
            coords = None
        if coords is None:
            return geometry
        if coords:
            points = shapely.points(list(coords.values()))
            held.update(tree.query(points, predicate="intersects")[1].tolist())
    if len(held) in (0, len(parts)):
        return geometry
    return shapely.union_all(parts[sorted(held)])


def _osm_note(geometry, parts):
    """The selection-record note on the parts of ``geometry`` that ``parts``
    leaves out, None when it leaves out none."""
    import shapely

    from transitio.osm._fetch import _area_km2

    total, kept = shapely.get_num_geometries([geometry, parts])
    if kept == total:
        return None
    return (
        f"OSM area: {kept} of {total} parts "
        f"({_area_km2(parts):.0f} of {_area_km2(geometry):.0f} km²)"
    )


def _fetch_place(
    place,
    *,
    tiers,
    exclude,
    on_unknown,
    on_untrusted_selector,
    contained,
    index,
    when,
    day,
    window_day,
    expired,
    modes,
    repair,
    crop,
    osm,
    refresh_token,
    cache_dir,
    directory,
    budgets,
):
    """The ``fetch(place=...)`` path: the place geometry is the AOI, feeds come
    from the index by tier, each is downloaded MDB-then-Atlas (decision I), and
    a bundled feed is cropped to the routes its matched tiers select, the drop
    recorded in ``selections``. A feed whose index window misses ``day`` is
    skipped before download when a probe proves the archive unchanged since
    indexed; ``window_day`` is what the computed window is tested against.
    The versions among the delivered feeds are settled after the feed loop,
    and the OSM extract comes last, for the parts the remaining feeds serve."""
    import shapely

    from transitio import __version__
    from transitio.catalog import Feed, MobilityDatabase, TransitlandAtlas
    from transitio.catalog._atlas import _feed_dir
    from transitio.exceptions import StaleSelectorError
    from transitio.index import (
        DISCOVERY_SEMANTICS_VERSION,
        Place,
        _coerce_index,
        place as resolve_place,
    )
    from transitio.index.feeds import _parse
    from transitio.osm import fetch_pbf
    from transitio.osm._fetch import _buffered

    if isinstance(place, Place):
        place_obj = place
        resolved_index = place._lookup._index
    else:
        resolved_index = _coerce_index(index)
        place_obj = resolve_place(place, index=resolved_index)
    provenance = {
        "snapshot": None if resolved_index is None else resolved_index.snapshot_id,
        "discovery_semantics_version": DISCOVERY_SEMANTICS_VERSION,
        "transitio_version": __version__,
    }
    geometry = place_obj.geometry
    if geometry is None:
        raise ValueError(f"place {place_obj.id} has no geometry to fetch for")
    if isinstance(geometry, (bytes, bytearray)):
        geometry = shapely.from_wkb(bytes(geometry))
    study = when is not None

    tag = hashlib.sha256(
        json.dumps(
            {
                "place": place_obj.id,
                "snapshot": provenance["snapshot"],
                "bounds": [round(v, 6) for v in geometry.bounds],
                "reference_date": budgets.get("reference_date"),
                "crop": crop,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()[:16]

    offered = place_obj.feeds(tiers=tiers, exclude=exclude, on_unknown=on_unknown)
    kept = _containers_first(offered) if contained == "drop" else offered
    feeds, reports, repairs, selections, record = [], [], [], [], []
    delivered = _Delivered()
    delivered_ids = []
    entries = {}
    # Containment state: the feeds carried by a feed delivered whole (itself,
    # or the feed whose content it repeats), those delivered cut to routes,
    # and whether a container's probe proved it unchanged before download.
    carriers, cropped, current, protected = {}, set(), {}, set()
    container_ids = {c for feed in kept for c in feed.contained_in}
    probes, services, budget = {}, {}, budgets.get("max_total_bytes")

    def entry_for(feed):
        # The record follows candidate order, whatever order they are processed in.
        if feed.feed_id not in entries:
            window = _window(feed.service_start, feed.service_end)
            entries[feed.feed_id] = _entry(feed.feed_id, feed.name, window)
            record.append(entries[feed.feed_id])
        return entries[feed.feed_id]

    if on_unknown == "exclude":
        included = place_obj.feeds(tiers=tiers, exclude=exclude, on_unknown="include")
        kept_ids = {f.feed_id for f in kept}
        for feed in included:
            entry = entry_for(feed)
            if feed.feed_id not in kept_ids:
                _skip(entry, "only unknown-tier edges")
    for feed in offered:
        entry_for(feed)

    import platformdirs

    base_dir = (
        pathlib.Path(directory)
        if directory
        else pathlib.Path(cache_dir or platformdirs.user_cache_dir("transitio"))
        / "gtfs"
    )
    with (
        MobilityDatabase(refresh_token, cache_dir=cache_dir) as db,
        TransitlandAtlas(cache_dir=cache_dir) as atlas,
    ):
        if when is not None and not db._refresh_token:
            warnings.warn(
                "no Mobility Database API token: 'when' cannot select "
                "historical datasets, using the latest hosted datasets",
                UserWarning,
                stacklevel=2,
            )

        def unchanged(feed):
            # One probe per feed, shared by the date and containment rules.
            if feed.feed_id not in probes:
                probes[feed.feed_id] = _unchanged_since_indexed(feed, atlas._http)
            return probes[feed.feed_id]

        def expired_unchanged(feed, entry):
            # Index metadata describes the archive the index crawled, so it
            # decides a feed only before a download from the indexed URLs,
            # and only once a probe proves that archive unchanged.
            missed = _misses(feed.service_start, feed.service_end, day, study)
            if expired == "skip" and missed and unchanged(feed):
                _skip(entry, f"{missed}; unchanged since indexed")
                return True
            return False

        for feed in kept:
            entry = entry_for(feed)
            dataset = None
            errors = []
            if db._refresh_token:
                mdb_id = (_parse(feed._row.get("mdb")) or {}).get("mdb_id")
                if mdb_id:
                    try:
                        mdb_feed = Feed.from_api({"id": mdb_id})
                        if when is not None:
                            dataset = db.dataset_for(mdb_feed, when)
                            if dataset is None:
                                _skip(entry, "no dataset covers the requested day")
                                continue
                        else:
                            versions = db.datasets(mdb_feed)
                            dataset = versions[0] if versions else None
                    except Exception as error:  # noqa: B902 — fall back to the urls
                        errors.append(f"dataset selection: {error}")
            if dataset is None and expired_unchanged(feed, entry):
                continue
            notes = []
            if contained == "drop":
                proven, notes = _containers(feed, entries, carriers, cropped, current)
                if proven and dataset is None and unchanged(feed):
                    _skip(entry, f"contained in {proven[0]}", contained_in=proven)
                    protected.update(carriers[c] for c in proven)
                    continue
                if proven:
                    notes.append("kept: containment not proven current")
            # The hosted validation report only describes the dataset's own
            # bytes, so it is attached only when the dataset supplied them.
            path = None
            from_dataset = False
            if dataset is not None:
                try:
                    path = db.download(
                        dataset, directory=base_dir / _feed_dir(feed.feed_id)
                    )
                    from_dataset = True
                except Exception as error:  # noqa: B902 — try the fallback next
                    errors.append(f"mdb dataset: {error}")
                if path is None and expired_unchanged(feed, entry):
                    continue
            if path is None:
                probed = contained == "drop" and feed.feed_id in container_ids
                probed = probed and unchanged(feed)
                try:
                    path = _download_indexed(feed, db, atlas, base_dir)
                    if probed:
                        # The proof covers only a download from the probed URL.
                        sidecar = path.with_suffix(".provenance.json").read_text()
                        source = json.loads(sidecar).get("source_url")
                        current[feed.feed_id] = source == probed
                except Exception as error:  # noqa: B902
                    errors.append(str(error))
            if path is None:
                joined = "; ".join(e for e in errors if e)
                _skip(entry, f"download failed: {joined}")
                continue
            hosted = None
            if from_dataset:
                try:
                    hosted = db.validation_report(dataset)
                except Exception:  # noqa: B902 — the hosted report is optional
                    hosted = None
            # Route selection: a bundled feed whose matched tiers name a
            # trustworthy complete selector is cropped to those routes; a
            # whole-feed selector filters nothing. Every applied selector is
            # first validated against the download -- its build-time
            # fingerprint must recompute and every selected route id must be
            # present -- and an untrustworthy or unavailable selector routes
            # through on_untrusted_selector rather than filtering silently.
            # on_unknown="exclude" is itself an edge filter, so this activates
            # even without an explicit tiers/exclude query.
            routes = None
            selection = None
            if tiers is not None or exclude is not None or on_unknown != "include":
                sel = feed.selector
                selected_by = [
                    {
                        "tier": edge.tier,
                        "selector_state": edge.selector_state,
                        "route_ids": sorted(
                            (edge.selector or {}).get("route_id") or []
                        ),
                    }
                    for edge in feed.edges.values()
                ]
                trusted, reason, in_feed = _selector_trusted(path, feed, sel)
                if trusted and sel.state == "complete" and set(sel.route_ids) - in_feed:
                    trusted, reason = False, "route_absent"
                if not trusted:
                    action = _untrusted_action(
                        on_untrusted_selector, exclude, on_unknown
                    )
                    if action == "error":
                        error = StaleSelectorError(
                            f"{feed.feed_id}: selector untrustworthy ({reason})"
                        )
                        error.feed_id = feed.feed_id
                        raise error
                    selection = {
                        "feed_id": feed.feed_id,
                        "selector_state": sel.state,
                        "trusted": False,
                        "reason": reason,
                        "kept": None,
                        "dropped": None,
                        "declared_as": None,
                        "selected_by": selected_by,
                    }
                    if action == "skip":
                        _skip(entry, f"untrustworthy selector ({reason})")
                        selections.append(selection)
                        continue
                    # action == "whole": deliver unfiltered (routes stays None),
                    # the selection recording why it was not filtered.
                elif sel.state == "complete":
                    routes = set(sel.route_ids)
                    selection = {
                        "feed_id": feed.feed_id,
                        "selector_state": "complete",
                        "trusted": True,
                        "reason": None,
                        "kept": None,  # filled from the delivered feed below
                        "dropped": None,
                        "declared_as": sel.declared_as,
                        "selected_by": selected_by,
                    }
                else:
                    selection = {
                        "feed_id": feed.feed_id,
                        "selector_state": sel.state,
                        "trusted": True,
                        "reason": None,
                        "kept": None,
                        "dropped": [],
                        "declared_as": None,
                        "selected_by": selected_by,
                    }
            twins = delivered.same_as(path)
            same_as = [twin for twin, _ in twins]
            if _covers(twins, routes):
                _skip(entry, f"same content as {', '.join(same_as)}", same_as=same_as)
                whole = [twin for twin, cut in twins if cut is None]
                if whole:
                    carriers[feed.feed_id] = whole[0]
                if selection is not None:
                    selections.append(selection)
                continue
            download = path
            # A per-feed tag folds in the selected routes so the same feed
            # fetched under different tiers never overwrites an earlier output.
            feed_tag = tag
            if routes is not None:
                feed_tag = hashlib.sha256(
                    json.dumps(
                        {"tag": tag, "routes": sorted(routes)}, sort_keys=True
                    ).encode()
                ).hexdigest()[:16]
            try:
                path, report, fixes, present, window = _process_feed(
                    path,
                    geometry=geometry,
                    tag=feed_tag,
                    repair=repair,
                    crop=crop,
                    modes=modes,
                    day=window_day,
                    study=study,
                    hosted=hosted,
                    budgets=budgets,
                    routes=routes,
                )
            except _SkipFeed as skip:
                _skip(entry, skip.reason, feed_window=skip.window)
                if selection is not None:
                    selections.append(selection)
                continue
            except Exception as error:  # noqa: B902 — isolate per-feed failures
                _skip(entry, f"processing failed: {error}")
                if selection is not None:
                    selections.append(selection)
                continue
            # ``present`` is the routes.txt the crop scanned, the download
            # before any repair: the audit is the selector's own action over
            # the feed's routes -- the selected routes it carried (``kept``)
            # and the rest it held that the selector removed (``dropped``).
            # A later repair may still change the delivered feed, and any
            # spatial crop is a separate transform reported in ``reports``,
            # not here. Both are None (undetermined) when routes.txt could
            # not be read. Only a trusted complete selector was cropped
            # (``routes`` is set).
            if selection is not None and routes is not None:
                selection["kept"] = (
                    None if present is None else sorted(present & routes)
                )
                selection["dropped"] = (
                    None if present is None else sorted(present - routes)
                )
            entry.update(
                decision="delivered", feed_window=window, path=path, same_as=same_as
            )
            if routes is not None:
                notes.insert(0, "cut to routes " + ", ".join(sorted(routes)))
                cropped.add(feed.feed_id)
            else:
                carriers[feed.feed_id] = feed.feed_id
            notes.append(_timezone_note(path, budget))
            for text in dict.fromkeys(filter(None, notes)):
                _note(entry, text)
            reports.append(report)
            repairs.append(fixes)
            feeds.append(path)
            delivered.add(feed.feed_id, download, routes)
            delivered_ids.append(feed.feed_id)
            if selection is not None:
                selections.append(selection)
            service = _service(path, day if study else None, budget)
            if service is not None:
                services[feed.feed_id] = service

    removed = _settle_versions(record, services, protected, day if study else None)
    rows = [n for n, feed_id in enumerate(delivered_ids) if feed_id not in removed]
    delivered_ids, feeds, reports, repairs = (
        [column[n] for n in rows] for column in (delivered_ids, feeds, reports, repairs)
    )
    pairs = {
        feed.feed_id: sorted(set(feed.contained_in) & set(delivered_ids))
        for feed in kept
        if feed.feed_id in delivered_ids
    }

    osm_pbf = osm_area = None
    if osm:
        parts = _osm_parts(geometry, feeds)
        osm_pbf = fetch_pbf(
            parts, buffer_m=_OSM_BUFFER_M, cache_dir=cache_dir, directory=directory
        )
        osm_area = _buffered(parts, _OSM_BUFFER_M)
        note = _osm_note(geometry, parts)
        if note is not None:
            record.append({**_entry(None, None), "note": note})

    return FetchResult(
        osm_pbf=osm_pbf,
        feeds=feeds,
        reports=reports,
        repairs=repairs,
        skipped=_skipped(record),
        selections=selections,
        provenance=provenance,
        snapshot=provenance["snapshot"],
        contained={feed_id: ids for feed_id, ids in pairs.items() if ids},
        selection=record,
        osm_area=osm_area,
    )
