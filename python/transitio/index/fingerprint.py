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

import hashlib
import json

__all__ = ["COORDINATE_DECIMALS", "KINDS", "compute"]

KINDS = ("route_stops", "feed_stops")
# ~1 m: absorbs float formatting churn, never a moved stop.
COORDINATE_DECIMALS = 5


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
