"""The classification fingerprint: one canonical hash of the feed evidence an
edge was classified from, computed the same way at build time and at fetch
time so a selector is only ever applied to the feed it was derived from.

Two kinds, chosen by the evidence a feed had. ``route_stops`` — complete
route→stop evidence — hashes every route's ``(route_id, agency_id,
route_type, sorted served stop ids)`` plus the rounded coordinates of every
stop. ``feed_stops`` — a feed that legitimately skipped ``stop_times`` —
hashes ``(route_id, agency_id, route_type)`` plus the same coordinate set:
route ids alone would let a feed keep its ids while moving its stops across a
border or into a second city. Both are derivable from the downloaded feed
alone; neither includes anything computed against the boundary cache.
"""

import collections
import contextlib
import csv
import hashlib
import io
import json
import zipfile

__all__ = ["COORDINATE_DECIMALS", "KINDS", "compute", "from_feed"]

KINDS = ("route_stops", "feed_stops")
# ~1 m: absorbs float formatting churn, never a moved stop.
COORDINATE_DECIMALS = 5

# The GTFS members the two kinds read; a duplicate of any is rejected.
_MEMBERS = ("routes.txt", "stops.txt", "trips.txt", "stop_times.txt")


def compute(kind, routes, coords, served=None):
    """The hex digest of ``kind`` over the feed's evidence.

    ``routes`` maps a route id to ``{"route_type", "agency_id"}`` (an
    unparsable type is ``None``), ``coords`` a stop id to ``(lon, lat)`` and
    ``served`` — required for ``route_stops`` — a route id to the stop ids
    it schedules. The canonical form is streamed into the hash one record
    per line, sorted, so a large feed never exists twice in memory: a
    section marker, then one JSON array per route, then one per stop.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown fingerprint kind {kind!r}")
    if kind == "route_stops" and served is None:
        raise ValueError("route_stops needs the served stops per route")
    digest = hashlib.sha256()
    _line(digest, ["kind", kind])
    _line(digest, ["routes"])
    for route_id in sorted(routes):
        info = routes[route_id]
        row = [route_id, info.get("agency_id") or "", info.get("route_type")]
        if kind == "route_stops":
            row.append(sorted(served.get(route_id, ())))
        _line(digest, row)
    _line(digest, ["stops"])
    for stop_id in sorted(coords):
        x, y = coords[stop_id]
        _line(digest, [stop_id, _round(x), _round(y)])
    return digest.hexdigest()


def _line(digest, record):
    digest.update(
        json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    digest.update(b"\n")


def _round(value):
    # ``+ 0.0`` folds a negative zero, which JSON would spell differently.
    return round(float(value), COORDINATE_DECIMALS) + 0.0


def from_feed(path, kind):
    """``(digest, route_ids)`` recomputed from a downloaded feed's GTFS zip.

    Recompute the ``kind`` fingerprint from ``path`` and collect the route ids
    it carries, for validating a selector against the live feed it is applied
    to. ``digest`` is None when the archive lacks a member the kind needs, a
    member exceeds the build's size ceiling, or the download is unreadable — a
    feed that cannot produce the evidence the selector was built from, which
    the caller treats as a mismatch rather than a crash. The extraction mirrors
    the build stage's: root-level members read by name, ids kept verbatim,
    coordinates range-checked, traversal-only stop-time rows excluded — so an
    unchanged feed recomputes the byte-identical digest.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown fingerprint kind {kind!r}")
    # Fail closed: a malformed archive is an untrustworthy selector, never an
    # exception that aborts the fetch.
    try:
        with zipfile.ZipFile(path) as archive:
            # A member named twice is ambiguous: getinfo() here and the Rust
            # crop could resolve it to different occurrences, letting a crafted
            # archive be validated against one and filtered on another. A real
            # feed never repeats a member, so reject rather than guess.
            names = collections.Counter(info.filename for info in archive.infolist())
            if any(names[member] > 1 for member in _MEMBERS):
                return None, set()
            routes = _member_routes(archive)
            if routes is None:
                return None, set()
            present = set(routes)
            coords = _member_coords(archive)
            if coords is None:
                return None, present
            served = None
            if kind == "route_stops":
                served = _member_served(archive, routes)
                if served is None:
                    return None, present
            return compute(kind, routes, coords, served), present
    except Exception:  # noqa: B902 — any unreadable download is an untrusted selector
        # A bad zip, decode, decompression, oversize member or malformed field
        # is a selector we cannot trust, never an exception that aborts the
        # fetch. The parity test pins the digest, so a real feed still matches.
        return None, set()


class _MemberTooLarge(Exception):
    """A member's declared size is over the build's per-member ceiling."""


# Ceiling on one member's uncompressed size, mirroring the crawl's member
# ceiling: a member the build would have refused to extract has no fingerprint
# to recompute against, so at fetch time it is a miss, not an unbounded read.
# The ceiling matches the build deliberately -- a tighter fetch-time budget
# would mark a large but legitimate feed the build accepted as stale -- and the
# accumulation here is the same the build's extraction and the Rust crop of the
# very same download already do, so it adds no exposure beyond the existing path.
_MAX_MEMBER_BYTES = 2 * 1024 * 1024 * 1024


@contextlib.contextmanager
def _member(archive, name):
    """A csv reader over a root member, or None when the archive lacks it."""
    try:
        info = archive.getinfo(name)
    except KeyError:
        yield None
        return
    if info.file_size > _MAX_MEMBER_BYTES:
        raise _MemberTooLarge(name)
    text = io.TextIOWrapper(archive.open(info), encoding="utf-8-sig", errors="strict")
    try:
        yield csv.DictReader(text)
    finally:
        text.close()


def _member_routes(archive):
    """``{route_id: {"route_type", "agency_id"}}`` from ``routes.txt``, or
    None when it is absent; an unparsable type is None, ids stay verbatim."""
    with _member(archive, "routes.txt") as reader:
        if reader is None:
            return None
        routes = {}
        for row in reader:
            value = (row.get("route_type") or "").strip()
            route_id = row.get("route_id") or ""
            if not route_id:
                continue
            routes[route_id] = {
                "route_type": int(value) if value.isdigit() else None,
                "agency_id": row.get("agency_id") or "",
            }
        return routes


def _member_coords(archive):
    """``{stop_id: (lon, lat)}`` from ``stops.txt`` for parseable, in-range
    rows, or None when it is absent; ids verbatim, last row wins."""
    with _member(archive, "stops.txt") as reader:
        if reader is None:
            return None
        coords = {}
        for row in reader:
            try:
                x = float(row.get("stop_lon") or "")
                y = float(row.get("stop_lat") or "")
            except ValueError:
                continue
            stop_id = row.get("stop_id") or ""
            if stop_id and -180.0 <= x <= 180.0 and -90.0 <= y <= 90.0:
                coords[stop_id] = (x, y)
        return coords


def _member_served(archive, routes):
    """``{route_id: {stop_id}}`` scheduled by ``trips.txt``/``stop_times.txt``
    for known routes, or None when either member is absent; a stop both
    no-pickup and no-drop-off is traversal, not service, and is excluded."""
    with _member(archive, "trips.txt") as reader:
        if reader is None:
            return None
        trip_routes = {}
        for row in reader:
            trip_id = row.get("trip_id") or ""
            route_id = row.get("route_id") or ""
            if trip_id and route_id in routes:
                trip_routes[trip_id] = route_id
    with _member(archive, "stop_times.txt") as reader:
        if reader is None:
            return None
        served = {}
        for row in reader:
            route_id = trip_routes.get(row.get("trip_id") or "")
            stop_id = row.get("stop_id") or ""
            if route_id is None or not stop_id:
                continue
            if (row.get("pickup_type") or "").strip() == "1" and (
                row.get("drop_off_type") or ""
            ).strip() == "1":
                continue
            served.setdefault(route_id, set()).add(stop_id)
        return served
