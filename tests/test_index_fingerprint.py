"""The classification fingerprint's canonicalisation."""

import io
import sys
import warnings
import zipfile
from pathlib import Path

import pytest

from transitio.index import fingerprint

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


def _feed_zip(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, text in members.items():
            archive.writestr(name, text)
    return buf.getvalue()


# Edge cases the two extractions must agree on: unparsable route_type, a missing
# agency id, an empty route/stop/trip id, an out-of-range coordinate, a
# duplicate stop (last row wins), a traversal-only stop-time (excluded), and a
# trip naming a route the feed lacks.
_MEMBERS = {
    "routes.txt": ("route_id,agency_id,route_type\n" "r1,a,3\nr2,,900\nr3,b,x\n,c,3\n"),
    "stops.txt": (
        "stop_id,stop_lon,stop_lat\n"
        "s1,24.9,60.2\ns2,25.0,60.3\ns3,999,60.0\n,24.0,60.0\n"
        "s1,24.95,60.25\ns4,24.8,60.1\n"
    ),
    "trips.txt": (
        "trip_id,route_id,service_id\nt1,r1,wk\nt2,r2,wk\nt3,rX,wk\n,r1,wk\n"
    ),
    "stop_times.txt": (
        "trip_id,stop_id,stop_sequence,pickup_type,drop_off_type\n"
        "t1,s1,1,0,0\nt1,s2,2,0,0\nt1,s4,3,1,1\nt2,s2,1,,\n"
        "t3,s1,1,0,0\nt1,,4,0,0\n"
    ),
}


def _build_digest(data, kind):
    # The build extraction, run on the same members, is the source of truth.
    from index_build import classify, crawl

    def read(name):
        return io.BytesIO(zipfile.ZipFile(io.BytesIO(data)).read(name))

    routes, _ = classify._read_routes(read("routes.txt"))
    rows, _ = crawl.stop_rows(read("stops.txt"))
    coords = {sid: (x, y) for sid, x, y in rows if sid}
    served = None
    if kind == "route_stops":
        trip_routes, services, _ = classify._read_trips(read("trips.txt"), routes)
        served, *_ = classify._read_stop_times(
            read("stop_times.txt"), trip_routes, services, None
        )
    return fingerprint.compute(kind, routes, coords, served)


@pytest.mark.parametrize("kind", ["route_stops", "feed_stops"])
def test_from_feed_reproduces_the_build_digest(kind):
    data = _feed_zip(_MEMBERS)
    digest, present = fingerprint.from_feed(io.BytesIO(data), kind)
    assert digest == _build_digest(data, kind)
    assert present == {"r1", "r2", "r3"}
    # A moved stop goes stale; a member the kind needs being absent is a miss.
    moved = {**_MEMBERS, "stops.txt": _MEMBERS["stops.txt"].replace("60.2", "61.2")}
    assert fingerprint.from_feed(io.BytesIO(_feed_zip(moved)), kind)[0] != digest
    trimmed = {n: t for n, t in _MEMBERS.items() if n != "stop_times.txt"}
    missing, _ = fingerprint.from_feed(io.BytesIO(_feed_zip(trimmed)), kind)
    assert missing == (None if kind == "route_stops" else digest)


def test_from_feed_fails_closed_on_unreadable_or_oversize(monkeypatch):
    # A malformed download is an untrusted selector, never a crash.
    assert fingerprint.from_feed(io.BytesIO(b"not a zip"), "feed_stops") == (
        None,
        set(),
    )
    # A duplicate required member is ambiguous, so it is rejected outright.
    dup = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # the duplicate name is the point
        with zipfile.ZipFile(dup, "w") as archive:
            for name, text in _MEMBERS.items():
                archive.writestr(name, text)
            archive.writestr("routes.txt", _MEMBERS["routes.txt"])  # a second copy
    assert fingerprint.from_feed(dup, "feed_stops") == (None, set())
    # A member over the ceiling is a miss, not an unbounded read.
    data = _feed_zip(_MEMBERS)
    monkeypatch.setattr(fingerprint, "_MAX_MEMBER_BYTES", 1)
    assert fingerprint.from_feed(io.BytesIO(data), "feed_stops") == (None, set())


ROUTES = {
    "r1": {"route_type": 3, "agency_id": "a"},
    "r2": {"route_type": None, "agency_id": ""},
}
COORDS = {"s1": (24.9384, 60.1699), "s2": (24.95, 60.17)}
SERVED = {"r1": {"s2", "s1"}, "r2": {"s2"}}


def test_the_kinds_differ_and_both_see_a_moved_stop():
    # Same route ids throughout: only the stop geography changes, which is
    # exactly the case route ids alone would miss.
    route_stops = fingerprint.compute("route_stops", ROUTES, COORDS, SERVED)
    feed_stops = fingerprint.compute("feed_stops", ROUTES, COORDS)
    assert route_stops != feed_stops
    moved = {**COORDS, "s2": (25.1, 60.17)}
    assert fingerprint.compute("route_stops", ROUTES, moved, SERVED) != route_stops
    assert fingerprint.compute("feed_stops", ROUTES, moved) != feed_stops
    # A route serving one stop fewer changes the strong kind only.
    fewer = {**SERVED, "r1": {"s1"}}
    assert fingerprint.compute("route_stops", ROUTES, COORDS, fewer) != route_stops
    assert fingerprint.compute("feed_stops", ROUTES, COORDS) == feed_stops


def test_coordinates_are_rounded_and_order_is_canonical():
    jitter = {"s2": (24.950000004, 60.17), "s1": (24.9384, 60.16990000001)}
    assert fingerprint.compute("feed_stops", ROUTES, jitter) == fingerprint.compute(
        "feed_stops", dict(reversed(list(ROUTES.items()))), COORDS
    )
    # Negative zero and zero spell the same.
    assert fingerprint.compute("feed_stops", {}, {"s": (-0.0, 0.0)}) == (
        fingerprint.compute("feed_stops", {}, {"s": (0.0, 0.0)})
    )


@pytest.mark.parametrize(
    ("kind", "served", "message"),
    [
        ("stops", SERVED, "unknown fingerprint kind"),
        ("route_stops", None, "served stops"),
    ],
)
def test_bad_inputs_are_refused(kind, served, message):
    with pytest.raises(ValueError, match=message):
        fingerprint.compute(kind, ROUTES, COORDS, served)
