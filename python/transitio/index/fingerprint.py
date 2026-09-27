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

The content identity (:func:`identity`) is a separate, per-table digest of a
feed's schedule tables, normalized so that two copies of one feed match
however they were packaged.
"""

import collections
import contextlib
import csv
import hashlib
import heapq
import io
import json
import lzma
import math
import os
import re
import struct
import tempfile
import zipfile
import zlib

__all__ = [
    "COORDINATE_DECIMALS",
    "IDENTITY_TABLES",
    "IDENTITY_VERSION",
    "KINDS",
    "compute",
    "from_feed",
    "identity",
]

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


IDENTITY_TABLES = (
    "stops.txt",
    "routes.txt",
    "trips.txt",
    "calendar.txt",
    "calendar_dates.txt",
    "stop_times.txt",
)
# Part of every table digest: a normalization change never matches old ones.
IDENTITY_VERSION = 1

# Row digests are sorted in memory up to _SPILL_ROWS, then written as sorted
# runs and merged at most _MERGE_FAN_IN at a time, so memory stays bounded
# whatever the table holds.
_SPILL_ROWS = 1 << 20
_MERGE_FAN_IN = 32
# 128 bits per row: a crafted row matching a given one still needs a second
# preimage, and a spilled national stop_times.txt takes half the disk.
_ROW_DIGEST_BYTES = 16
# One distinct row digest and how many rows share it.
_RECORD = struct.Struct(f">{_ROW_DIGEST_BYTES}sQ")

# What makes a source unreadable rather than a defect: zipfile raises most of
# these for a malformed or encrypted archive, csv and the decoder the rest.
_UNREADABLE = (
    OSError,
    EOFError,
    ValueError,
    RuntimeError,
    csv.Error,
    zipfile.BadZipFile,
    zlib.error,
    lzma.LZMAError,
    _MemberTooLarge,
)
_SINGLE_DIGIT_HOUR = re.compile(r"[0-9]:[0-9]{2}:[0-9]{2}")


def identity(source, *, max_member_bytes=_MAX_MEMBER_BYTES):
    """``{table: hex digest}`` over the feed's identity tables, or None.

    ``source`` is a GTFS zip (a path or a binary file object); members are
    read at the root. Each carried table in :data:`IDENTITY_TABLES` gets a
    digest of its rows, independent of column order, row order, whitespace
    around values, empty or absent optional columns, byte-order mark and line
    endings; stop coordinates are rounded to :data:`COORDINATE_DECIMALS` and
    single-digit stop-time hours zero-padded, and everything else, ids
    included, is kept verbatim. None when the source is unreadable: not a
    zip, an identity table named twice or over ``max_member_bytes``, invalid
    UTF-8 or CSV (a field longer than the ``csv`` field size limit
    included), a header naming a column twice.
    """
    try:
        with _Source(source, max_member_bytes) as members:
            digests = {}
            for table in IDENTITY_TABLES:
                raw = members.open(table)
                if raw is not None:
                    digests[table] = _table_digest(table, raw)
            return digests
    except _UNREADABLE:
        return None


class _Source:
    """The root members of a GTFS zip, opened by name."""

    def __init__(self, source, max_member_bytes):
        self._max = max_member_bytes
        self._archive = zipfile.ZipFile(source)
        names = collections.Counter(i.filename for i in self._archive.infolist())
        if any(names[table] > 1 for table in IDENTITY_TABLES):
            self._archive.close()
            raise ValueError("an identity table is named twice")

    def open(self, name):
        """A binary stream over member ``name``, or None when it is absent."""
        try:
            info = self._archive.getinfo(name)
        except KeyError:
            return None
        if info.file_size > self._max:
            raise _MemberTooLarge(name)
        return self._archive.open(info)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self._archive.close()


def _table_digest(table, raw):
    """The digest of one table's normalized rows, in sorted row order."""
    with (
        io.TextIOWrapper(
            raw, encoding="utf-8-sig", errors="strict", newline=""
        ) as text,
        _SortedDigests() as rows,
    ):
        reader = csv.reader(text, strict=True)
        header = [name.strip() for name in next(reader, [])]
        if len(set(header)) != len(header):
            raise ValueError(f"{table}: a column is named twice")
        fix = _fixes(table)
        order = sorted(range(len(header)), key=header.__getitem__)
        prefixes = [f"{len(name)}:{name}" for name in header]
        fixes = [fix.get(name) for name in header]
        for fields in reader:
            parts = []
            for index in order:
                if index >= len(fields):
                    continue
                value = fields[index].strip()
                if not value:
                    continue
                if fixes[index] is not None:
                    value = fixes[index](value)
                parts.append(f"{prefixes[index]}{len(value)}:{value}")
            if parts:
                encoded = "".join(parts).encode("utf-8")
                rows.add(hashlib.sha256(encoded).digest()[:_ROW_DIGEST_BYTES])
        return rows.hexdigest(f"{IDENTITY_VERSION}\n{table}\n")


def _fixes(table):
    if table == "stops.txt":
        return {"stop_lat": _coordinate, "stop_lon": _coordinate}
    if table == "stop_times.txt":
        return {"arrival_time": _clock, "departure_time": _clock}
    return {}


def _coordinate(value):
    try:
        number = float(value)
    except ValueError:
        return value
    return repr(_round(number)) if math.isfinite(number) else value


def _clock(value):
    return "0" + value if _SINGLE_DIGIT_HOUR.fullmatch(value) else value


class _SortedDigests:
    """Row digests hashed in sorted order as ``(digest, count)`` records.

    Up to ``_SPILL_ROWS`` digests are held and sorted in memory; beyond that
    each batch becomes a sorted run file, and every ``_MERGE_FAN_IN`` runs
    are merged into one, so neither memory nor open files grow with the
    table. Identical rows collapse into one record, and the in-memory and
    spilled paths hash the same records.
    """

    def __init__(self):
        self._rows = []
        self._count = 0
        self._directory = None
        self._runs = []

    def add(self, digest):
        self._count += 1
        self._rows.append(digest)
        if len(self._rows) >= _SPILL_ROWS:
            self._spill()

    def _spill(self):
        if self._directory is None:
            self._directory = tempfile.TemporaryDirectory(
                prefix="transitio-identity-", ignore_cleanup_errors=True
            )
        self._rows.sort()
        self._runs.append(self._write(_collapse((d, 1) for d in self._rows)))
        self._rows = []
        if len(self._runs) >= _MERGE_FAN_IN:
            runs, self._runs = self._runs, []
            self._runs.append(self._write(_merged(runs)))
            for run in runs:
                os.unlink(run)

    def _write(self, records):
        handle, path = tempfile.mkstemp(dir=self._directory.name)
        with os.fdopen(handle, "wb") as out:
            for digest, count in records:
                out.write(_RECORD.pack(digest, count))
        return path

    def hexdigest(self, header):
        final = hashlib.sha256(f"{header}{self._count}\n".encode("utf-8"))
        if self._directory is None:
            self._rows.sort()
            records = _collapse((d, 1) for d in self._rows)
        else:
            if self._rows:
                self._spill()
            records = _merged(self._runs)
        for digest, count in records:
            final.update(_RECORD.pack(digest, count))
        return final.hexdigest()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        if self._directory is not None:
            self._directory.cleanup()


def _collapse(records):
    """Sorted ``(digest, count)`` records with equal digests summed."""
    current, total = None, 0
    for digest, count in records:
        if digest == current:
            total += count
            continue
        if current is not None:
            yield current, total
        current, total = digest, count
    if current is not None:
        yield current, total


def _merged(runs):
    return _collapse(heapq.merge(*(_run_records(run) for run in runs)))


def _run_records(path):
    with open(path, "rb") as handle:
        while chunk := handle.read(_RECORD.size * 4096):
            yield from _RECORD.iter_unpack(chunk)
