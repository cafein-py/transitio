"""The classification fingerprint's canonicalisation."""

import hashlib
import io
import os
import warnings
import zipfile

import pytest

from transitio.index import fingerprint


def _feed_zip(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, text in members.items():
            archive.writestr(name, text)
    return buf.getvalue()


# Edge cases the reader's extraction must handle: unparsable route_type, a
# missing agency id, an empty route/stop/trip id, an out-of-range coordinate, a
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


@pytest.mark.parametrize("kind", ["route_stops", "feed_stops"])
def test_from_feed_reads_the_routes_and_reflects_the_stops(kind):
    data = _feed_zip(_MEMBERS)
    digest, present = fingerprint.from_feed(io.BytesIO(data), kind)
    assert present == {"r1", "r2", "r3"}
    # The same feed reads to the same digest.
    assert fingerprint.from_feed(io.BytesIO(data), kind)[0] == digest
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


def test_sorted_digests_ignore_order_count_repeats_and_spill_alike(monkeypatch):
    rows = [hashlib.sha256(bytes([i % 7])).digest()[:16] for i in range(40)]

    def digest(batch):
        with fingerprint._SortedDigests() as sorted_rows:
            for row in batch:
                sorted_rows.add(row)
            return sorted_rows.hexdigest("table\n")

    held = digest(rows)
    assert digest(rows[::-1]) == held
    assert digest(rows + rows[:1]) != held
    # One row per run and pairwise merges: records collapse across runs, and
    # runs merge in stages, to the same digest.
    monkeypatch.setattr(fingerprint, "_SPILL_ROWS", 1)
    monkeypatch.setattr(fingerprint, "_MERGE_FAN_IN", 2)
    assert digest(rows[::-1]) == held


_FEED = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\n"
    "a,A,https://a.example,Europe/Helsinki\n",
    "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\n"
    "s1,Central,60.2,24.9\ns2,Harbour,60.3,25.0\n",
    "routes.txt": "route_id,route_short_name,route_type\nr1,1,3\n",
    "trips.txt": "route_id,service_id,trip_id\nr1,wk,t1\nr1,wk,t2\n",
    "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,"
    "saturday,sunday,start_date,end_date\nwk,1,1,1,1,1,0,0,20260101,20261231\n",
    "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
    "t1,05:00:00,05:00:00,s1,1\nt1,05:10:00,05:10:00,s2,2\nt2,06:00:00,06:00:00,s1,1\n",
}


def _repackaged(members):
    """The same tables written differently: columns and rows reversed, an extra
    empty column, padded values, CRLF and a BOM, 60.200000 and 5:00:00."""
    out = {}
    for name, text in members.items():
        rows = [line.split(",")[::-1] + [""] for line in text.strip("\n").split("\n")]
        rows[0][-1] = "extra"
        lines = [",".join(f" {v} " if v else v for v in row) for row in rows]
        text = "\r\n".join([lines[0], *lines[:0:-1]]) + "\r\n"
        text = text.replace(" 60.2 ", " 60.200000 ").replace(" 05:00:00 ", " 5:00:00 ")
        out[name] = "﻿" + text
    return out


def test_identity_ignores_packaging_and_sees_each_table_change(tmp_path):
    base = fingerprint.identity(io.BytesIO(_feed_zip(_FEED)))
    assert set(base) == set(fingerprint.IDENTITY_TABLES) - {"calendar_dates.txt"}
    agency = _FEED["agency.txt"].replace("https://a.example", "https://b.example")
    repackaged = _repackaged({**_FEED, "agency.txt": agency})
    assert fingerprint.identity(io.BytesIO(_feed_zip(repackaged))) == base
    for name, text in repackaged.items():
        (tmp_path / name).write_bytes(text.encode("utf-8"))
    assert fingerprint.identity(tmp_path) == base
    changes = {
        "stops.txt": ("60.3,25.0", "60.31,25.0"),
        "stop_times.txt": ("06:00:00,06:00:00", "06:01:00,06:01:00"),
        "trips.txt": ("r1,wk,t2\n", ""),
        "routes.txt": ("r1,1,3\n", "r1,1,3\nr1,1,3\n"),
    }
    for table, (old, new) in changes.items():
        edited = {**_FEED, table: _FEED[table].replace(old, new)}
        found = fingerprint.identity(io.BytesIO(_feed_zip(edited)))
        assert {t for t in base if found[t] != base[t]} == {table}


_T = {"stops.txt": "s", "routes.txt": "r", "trips.txt": "t", "calendar.txt": "c"}


@pytest.mark.parametrize(
    ("identities", "groups"),
    [
        (
            {"a": {**_T, "stop_times.txt": "x"}, "b": {**_T, "stop_times.txt": "x"}},
            [["a", "b"]],
        ),
        ({"a": {**_T, "stop_times.txt": "x"}, "b": {**_T, "stop_times.txt": "y"}}, []),
        ({"b": _T, "a": {**_T, "stop_times.txt": "x"}, "c": _T}, [["a", "b", "c"]]),
        (
            {
                "a": {**_T, "stop_times.txt": "x"},
                "b": _T,
                "c": {**_T, "stop_times.txt": "y"},
            },
            [],
        ),
        ({"a": _T, "b": {**_T, "calendar_dates.txt": "d"}}, []),
        ({"a": None, "b": _T, "c": {**_T, "trips.txt": ""}}, []),
        ({"a": {**_T, "calendar.txt": ""}, "b": {**_T, "calendar.txt": ""}}, []),
    ],
    ids=[
        "equal",
        "times-differ",
        "untimed-joins",
        "untimed-ambiguous",
        "calendars-differ",
        "unusable",
        "no-calendar",
    ],
)
def test_identical_groups(identities, groups):
    assert fingerprint.identical_groups(identities) == groups


def test_an_unreadable_source_has_no_identity():
    dup = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # the duplicate name is the point
        with zipfile.ZipFile(dup, "w") as archive:
            for name, text in _FEED.items():
                archive.writestr(name, text)
            archive.writestr("stops.txt", _FEED["stops.txt"])
    deflated = io.BytesIO()
    with zipfile.ZipFile(deflated, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("stops.txt", _FEED["stops.txt"] * 50)
    corrupt = bytearray(deflated.getvalue())
    corrupt[60:70] = b"\xff" * 10
    # A compression method zipfile cannot decode (99) raises NotImplementedError.
    unsupported = bytearray(_feed_zip({"stops.txt": _FEED["stops.txt"]}))
    for signature, offset in ((b"PK\x03\x04", 8), (b"PK\x01\x02", 10)):
        at = unsupported.index(signature) + offset
        unsupported[at : at + 2] = (99).to_bytes(2, "little")
    # A member declaring fewer bytes than it holds is cut short and fails its CRC.
    understated = bytearray(_feed_zip({"stops.txt": _FEED["stops.txt"]}))
    for signature, offset in ((b"PK\x03\x04", 22), (b"PK\x01\x02", 24)):
        at = understated.index(signature) + offset
        understated[at : at + 4] = (10).to_bytes(4, "little")
    sources = [
        io.BytesIO(b"not a zip"),
        io.BytesIO(bytes(understated)),
        dup,
        io.BytesIO(bytes(corrupt)),
        io.BytesIO(bytes(unsupported)),
        io.BytesIO(_feed_zip({**_FEED, "stops.txt": b"stop_id\n\xff\n"})),
        io.BytesIO(_feed_zip({**_FEED, "routes.txt": "route_id,route_id\nr1,r1\n"})),
        io.BytesIO(_feed_zip({**_FEED, "routes.txt": 'route_id\n"r1\n'})),
        io.BytesIO(_feed_zip({"stops.txt": "stop_id\n" + "s" * (1 << 17) + "x\n"})),
    ]
    for source in sources:
        assert fingerprint.identity(source) is None
    oversize = io.BytesIO(_feed_zip(_FEED))
    assert fingerprint.identity(oversize, max_member_bytes=10) is None


def test_a_directory_member_must_be_a_regular_file_under_the_ceiling(tmp_path):
    folder = tmp_path / "feed"
    folder.mkdir()
    for name, text in _FEED.items():
        (folder / name).write_text(text)
    assert fingerprint.identity(folder) is not None
    assert fingerprint.identity(folder, max_member_bytes=10) is None
    (folder / "stops.txt").unlink()
    if hasattr(os, "mkfifo"):
        os.mkfifo(folder / "stops.txt")  # never opened, so nothing blocks
        assert fingerprint.identity(folder) is None
        (folder / "stops.txt").unlink()
    (tmp_path / "elsewhere.txt").write_text(_FEED["stops.txt"])
    try:
        (folder / "stops.txt").symlink_to(tmp_path / "elsewhere.txt")
    except OSError:
        pytest.skip("symlinks are not available here")
    assert fingerprint.identity(folder) is None


def test_only_a_clock_time_gets_its_hour_padded():
    def times(value):
        text = f"trip_id,arrival_time,stop_id,stop_sequence\nt1,{value},s1,1\n"
        found = fingerprint.identity(io.BytesIO(_feed_zip({"stop_times.txt": text})))
        return found["stop_times.txt"]

    assert times("5:00:00") == times("05:00:00")
    assert times("x:00:00") != times("0x:00:00")
