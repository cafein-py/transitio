import base64
import datetime
import hashlib
import json
import logging
import os
import pathlib
import urllib.parse
import warnings
import zipfile

import httpx
import pytest

pytest.importorskip("transitio._core")

import transitio.catalog  # noqa: E402
import transitio.osm  # noqa: E402
from transitio.exceptions import DownloadError, StaleSelectorError  # noqa: E402
from transitio.pipeline import fetch  # noqa: E402

GTFS = {
    "agency.txt": (
        "agency_id,agency_name,agency_url,agency_timezone\n"
        "hsl,HSL,https://hsl.fi,Europe/Helsinki\n"
    ),
    "stops.txt": (
        "stop_id,stop_name,stop_lat,stop_lon\n"
        "s1,Kamppi,60.169,24.931\ns2,Steissi,60.171,24.941\n"
    ),
    "routes.txt": "route_id,agency_id,route_short_name,route_type\nr1,hsl,1,3\n",
    "trips.txt": "route_id,service_id,trip_id\nr1,wk,t1\n",
    "stop_times.txt": (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "t1,08:00:00,08:00:00,s1,1\nt1,08:05:00,08:05:00,s2,2\n"
    ),
    "calendar.txt": (
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
        "start_date,end_date\nwk,1,1,1,1,1,0,0,20260101,20261231\n"
    ),
}

# Another agency's copy of GTFS, its trip an hour later.
HKL = {
    **GTFS,
    "agency.txt": GTFS["agency.txt"].replace("HSL", "HKL"),
    "stop_times.txt": GTFS["stop_times.txt"].replace("08:", "09:"),
}
# HKL also running GTFS's trip, its own trip as t2.
PARTIAL = {
    **HKL,
    "trips.txt": GTFS["trips.txt"] + "r1,wk,t2\n",
    "stop_times.txt": GTFS["stop_times.txt"]
    + HKL["stop_times.txt"].split("\n", 1)[1].replace("t1", "t2"),
}

CSV_BODY = (
    "id,data_type,status,is_official,provider,"
    "location.country_code,location.subdivision_name,location.municipality,"
    "location.bounding_box.minimum_latitude,location.bounding_box.maximum_latitude,"
    "location.bounding_box.minimum_longitude,location.bounding_box.maximum_longitude,"
    "urls.direct_download,urls.latest,urls.license\n"
    "mdb-10,gtfs,active,True,HSL,FI,Uusimaa,Helsinki,59.9,60.6,24.2,25.6,"
    "https://example.com/hsl.zip,https://files.example.com/mdb-10/latest.zip,"
    "https://example.com/license\n"
)


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    # A Monday inside the fixture feeds' 2026 calendars.
    monkeypatch.setattr(
        "transitio.pipeline._fetch._today", lambda: datetime.date(2026, 6, 1)
    )


@pytest.fixture
def pipeline_env(tmp_path, monkeypatch):
    import io as _io

    buffer = _io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in GTFS.items():
            archive.writestr(name, content)
    payload = buffer.getvalue()

    def handler(request):
        if request.url.path == "/feeds_v2.csv":
            return httpx.Response(200, text=CSV_BODY)
        if request.url.path == "/mdb-10/latest.zip":
            return httpx.Response(200, content=payload)
        return httpx.Response(404)

    monkeypatch.delenv("MOBILITY_API_REFRESH_TOKEN", raising=False)
    transport = httpx.MockTransport(handler)
    original = transitio.catalog.MobilityDatabase

    def patched(refresh_token=None, **kwargs):
        kwargs["transport"] = transport
        kwargs.setdefault("cache_dir", tmp_path)
        return original(refresh_token, **kwargs)

    monkeypatch.setattr("transitio.catalog.MobilityDatabase", patched)

    fake_pbf = tmp_path / "aoi.osm.pbf"
    fake_pbf.write_bytes(b"\x00fake")
    monkeypatch.setattr("transitio.osm._fetch.fetch_pbf", lambda *a, **k: fake_pbf)
    monkeypatch.setattr("transitio.osm.fetch_pbf", lambda *a, **k: fake_pbf)
    return tmp_path, fake_pbf


def test_fetch_end_to_end(pipeline_env):
    tmp_path, fake_pbf = pipeline_env
    with pytest.warns(UserWarning):
        result = fetch(
            (24.6, 60.1, 25.2, 60.4),
            directory=tmp_path,
            reference_date="20260601",
        )
    assert result.osm_pbf == fake_pbf
    assert result.osm_area.bounds == (24.6, 60.1, 25.2, 60.4)
    # Every stop lies in the area, so there is no OSM note.
    (entry,) = result.selection
    assert entry["stops_outside_osm"] == 0 and result.osm_note is None
    assert result.paths == {"mdb-10": tmp_path / "mdb-10.zip"}
    (report,) = result.reports
    assert report["summary"]["counts"]["errors"] == 0
    assert result.skipped == []
    assert result.repairs == [[]]
    pbf, feeds = result
    assert pbf == fake_pbf and feeds == result.feeds


def _zip(tables, compression=zipfile.ZIP_DEFLATED):
    import io as _io

    buffer = _io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in tables.items():
            # A fixed time, so the same tables always give the same bytes.
            member = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            archive.writestr(member, content, compress_type=compression)
    return buffer.getvalue()


def _area_fetch(monkeypatch, tmp_path, second, **options):
    """An area fetch over two hosted feeds, mdb-10 serving ``GTFS`` and mdb-11
    the zip bytes ``second``; ``options`` go to :func:`fetch`."""
    from transitio.catalog._client import MobilityDatabase

    payloads = {"/mdb-10/latest.zip": _zip(GTFS), "/mdb-11/latest.zip": second}
    row = CSV_BODY.splitlines()[1]
    csv = CSV_BODY + row.replace("mdb-10", "mdb-11").replace(",HSL,", ",HKL,") + "\n"

    def handler(request):
        if request.url.path == "/feeds_v2.csv":
            return httpx.Response(200, text=csv)
        if request.url.path in payloads:
            return httpx.Response(200, content=payloads[request.url.path])
        return httpx.Response(404)

    def patched(refresh_token=None, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return MobilityDatabase(refresh_token, **kwargs)

    monkeypatch.setattr("transitio.catalog.MobilityDatabase", patched)
    with pytest.warns(UserWarning):
        return fetch(
            (24.6, 60.1, 25.2, 60.4),
            directory=tmp_path / "out",
            cache_dir=tmp_path / "cache",
            reference_date="20260601",
            **options,
        )


def test_an_area_fetch_keeps_each_feeds_download_apart(pipeline_env, monkeypatch):
    tmp_path, _ = pipeline_env
    first = _area_fetch(monkeypatch, tmp_path, _zip(HKL))
    result = _area_fetch(monkeypatch, tmp_path, _zip(HKL))
    assert len(set(result.feeds)) == 2
    agencies = {zipfile.ZipFile(p).read("agency.txt") for p in result.feeds}
    assert len(agencies) == 2
    # The repeat downloads no feed: each is its one cached version, acquired
    # once; the directory holds only the crops, the repeat's replacing the
    # first call's.
    assert [e["cache"] for e in first.selection + result.selection] == (
        ["downloaded"] * 2 + ["reused"] * 2
    )
    cached = list((tmp_path / "cache").glob("gtfs/id-*/*.zip"))
    assert len(cached) == len({p.parent for p in cached}) == 2
    for path in cached:
        sidecar = json.loads(path.with_suffix(".provenance.json").read_text())
        assert len(sidecar["cache"]["sources"]) == 1
    assert sorted((tmp_path / "out").rglob("*.zip")) == sorted(result.feeds)
    assert sorted(p.name for p in result.feeds) == ["mdb-10.zip", "mdb-11.zip"]
    if os.name != "nt":
        crops = (tmp_path / "cache").rglob("*-cropped*.zip")
        assert not any(os.access(path, os.W_OK) for path in crops)

    # A refresh served a page instead of mdb-11's archive falls back to it.
    page = b"<html>maintenance</html>"
    refreshed = _area_fetch(monkeypatch, tmp_path, page, use_cache=False)
    assert sorted(e["cache"] for e in refreshed.selection) == ["fallback", "refreshed"]
    assert len(refreshed.feeds) == 2


def test_a_repeated_fetch_reuses_what_processing_made(pipeline_env, monkeypatch):
    import transitio.gtfs
    import transitio.gtfs._duplicates
    import transitio.validate

    tmp_path, _ = pipeline_env
    calls = []
    for module, name in (
        (transitio.gtfs, "crop_feed"),
        (transitio.validate, "validate_feed"),
        (transitio.gtfs._duplicates, "repeated_trips"),
    ):
        real = getattr(module, name)

        def counted(*args, _real=real, _name=name, **options):
            calls.append(_name)
            return _real(*args, **options)

        monkeypatch.setattr(module, name, counted)
    other = _zip(PARTIAL)
    first = _area_fetch(monkeypatch, tmp_path, other)
    made = ["crop_feed"] * 3 + ["repeated_trips"] + ["validate_feed"] * 3
    assert sorted(calls) == made
    assert [e["note"] for e in first.selection] == [
        None,
        "1 repeated trips of mdb-10 left out",
    ]
    assert [t["trip_id"] for t in _feed_tables(first.feeds[1])["trips.txt"]] == ["t2"]
    # A delivered copy is the caller's; the stored outputs serve the repeat
    # without matching, cropping or validating.
    first.feeds[0].write_bytes(b"")
    again = _area_fetch(monkeypatch, tmp_path, other)
    assert len(calls) == 7
    assert _timeless(first.reports) == _timeless(again.reports)
    assert first.repairs == again.repairs
    assert [e["note"] for e in again.selection] == [e["note"] for e in first.selection]
    # A stored output that changed is made again.
    output, *_ = sorted((tmp_path / "cache").rglob("outputs/*-cropped.zip"))
    output.chmod(0o644)
    output.write_bytes(b"changed")
    _area_fetch(monkeypatch, tmp_path, other)
    assert sorted(calls[7:]) == ["crop_feed", "validate_feed"]
    # A version deleted for an unreadable sidecar leaves no outputs behind.
    from transitio.catalog._cache import FeedCache

    cache, folder = FeedCache(tmp_path / "cache"), output.parent.parent
    (feed_id,) = [f for f in ("mdb-10", "mdb-11") if cache.folder(f) == folder]
    (sidecar,) = folder.glob("*.provenance.json")
    sidecar.write_text("{")
    assert cache.versions(feed_id) == []
    assert list((folder / "outputs").iterdir()) == []


def test_outputs_are_keyed_by_the_exact_area_and_the_budgets():
    from types import SimpleNamespace

    from shapely.geometry import Polygon, box

    from transitio.pipeline._fetch import _output_key

    version = SimpleNamespace(sha256="0" * 64)
    square, notched = box(0, 0, 1, 1), Polygon(
        [(0, 0), (1, 0), (1, 1), (0.5, 0.5), (0, 1)]
    )

    def key(area, crop=True, **budgets):
        return _output_key(version, area, None, crop, False, budgets)

    assert key(square) != key(notched)  # the same bounds
    assert key(square) != key(square, max_notices_per_file=5)
    assert key(square, crop=False) == key(notched, crop=False)


def test_an_area_fetch_delivers_the_same_content_once(pipeline_env, monkeypatch):
    tmp_path, _ = pipeline_env
    # Other archive bytes, the same files: stored rather than deflated.
    result = _area_fetch(monkeypatch, tmp_path, _zip(GTFS, zipfile.ZIP_STORED))
    assert len(result.feeds) == 1
    ((feed_id, reason),) = result.skipped
    other = ({"mdb-10", "mdb-11"} - {feed_id}).pop()
    assert reason == f"same content as {other}"
    first, second = result.selection
    assert first["decision"] == "delivered" and first["path"] == result.feeds[0]
    assert (second["feed_id"], second["same_as"]) == (feed_id, [other])
    assert first["index_window"] is None and first["name"] in {"HSL", "HKL"}
    # A feed skipped after its download still records where it came from.
    assert [e["fetched_from"] for e in result.selection] == ["mdb_latest"] * 2


def test_an_area_fetch_reports_a_page_that_is_no_zip_as_a_failed_download(
    pipeline_env, monkeypatch
):
    tmp_path, _ = pipeline_env
    result = _area_fetch(monkeypatch, tmp_path, b"<html>maintenance</html>")
    (entry,) = [e for e in result.selection if e["decision"] == "skipped"]
    assert entry["reason"] == f"download failed: {entry['download_errors']}"
    assert entry["download_errors"].endswith("not a zip archive")
    assert entry["fetched_from"] is None and len(result.feeds) == 1


OTHER_TRIPS = {**GTFS, "trips.txt": "route_id,service_id,trip_id\nr1,wk,t2\n"}


@pytest.mark.parametrize(
    "second, routes, replaced, same",
    [
        (_zip(GTFS), None, False, True),
        (_zip(GTFS, zipfile.ZIP_STORED), None, False, True),
        (_zip(OTHER_TRIPS), None, False, False),
        (_zip(GTFS), frozenset({"r1"}), False, True),
        (b"not a zip", None, False, False),
        # The delivered download changed after its check: nothing to match.
        (_zip(GTFS, zipfile.ZIP_STORED), None, True, False),
    ],
    ids=[
        "same-archive",
        "same-entries",
        "other-content",
        "other-routes",
        "unreadable",
        "changed-since",
    ],
)
def test_a_delivered_feed_is_recognised_by_its_content(
    tmp_path, second, routes, replaced, same
):
    from transitio.pipeline._fetch import _Delivered

    first, again = tmp_path / "a.zip", tmp_path / "b.zip"
    first.write_bytes(_zip(GTFS))
    again.write_bytes(second)
    delivered = _Delivered()
    delivered.add("feed-a", first, routes)
    if replaced:
        first.write_bytes(_zip(OTHER_TRIPS))
    assert delivered.same_as(again) == [("feed-a", routes)] * same


def test_fetch_when_without_token_warns(pipeline_env):
    tmp_path, _ = pipeline_env
    with pytest.warns(UserWarning) as caught:
        result = fetch(
            (24.6, 60.1, 25.2, 60.4),
            when="2026-06-01",
            directory=tmp_path,
        )
    assert any("cannot select historical" in str(w.message) for w in caught)
    assert len(result.feeds) == 1


def test_fetch_rejects_unknown_mode(pipeline_env):
    tmp_path, _ = pipeline_env
    with pytest.raises(ValueError, match="unknown modes"):
        fetch((24.6, 60.1, 25.2, 60.4), modes=["hovercraft"], directory=tmp_path)


def test_fetch_mode_accepts_bare_string(pipeline_env):
    tmp_path, _ = pipeline_env
    with pytest.warns(UserWarning):
        result = fetch(
            (24.6, 60.1, 25.2, 60.4),
            modes="bus",
            directory=tmp_path,
            reference_date="20260601",
        )
    assert len(result.feeds) == 1


def test_fetch_mode_filter(pipeline_env):
    tmp_path, _ = pipeline_env
    with pytest.warns(UserWarning):
        result = fetch(
            (24.6, 60.1, 25.2, 60.4),
            modes=["ferry"],
            directory=tmp_path,
            reference_date="20260601",
        )
    assert result.feeds == []
    assert len(result.skipped) == 1
    assert "ferry" in result.skipped[0][1]


def test_fetch_place_name_aoi(pipeline_env, monkeypatch):
    from shapely.geometry import box

    tmp_path, fake_pbf = pipeline_env
    monkeypatch.setattr(
        "transitio.osm._fetch._as_geometry",
        lambda aoi: box(24.6, 60.1, 25.2, 60.4),
    )
    with pytest.warns(UserWarning):
        result = fetch("Helsinki", directory=tmp_path, reference_date="20260601")
    assert len(result.feeds) == 1


def test_fetch_skips_day_outside_service_window(pipeline_env):
    tmp_path, _ = pipeline_env
    with pytest.warns(UserWarning):
        result = fetch(
            (24.6, 60.1, 25.2, 60.4),
            when="2027-06-01",
            directory=tmp_path,
        )
    assert result.feeds == []
    assert result.skipped == [("mdb-10", "service ended 2026-12-31")]
    assert result.selection[0]["feed_window"] == ["2026-01-01", "2026-12-31"]


def test_feed_modes_undeterminable(tmp_path):
    from transitio.pipeline._fetch import _feed_modes

    not_a_zip = tmp_path / "feed.zip"
    not_a_zip.write_bytes(b"not a zip archive")
    assert _feed_modes(not_a_zip) is None

    no_routes = tmp_path / "noroutes.zip"
    with zipfile.ZipFile(no_routes, "w") as archive:
        archive.writestr("agency.txt", "agency_id\n")
    assert _feed_modes(no_routes) is None


def test_feed_modes_read_stripped_values_and_uneven_rows(tmp_path):
    from transitio.pipeline._fetch import _feed_modes

    feed = tmp_path / "feed.zip"
    with zipfile.ZipFile(feed, "w") as archive:
        # A tram if the long row were truncated or shifted into an index;
        # the short row has a blank route_type.
        archive.writestr("routes.txt", "route_id, route_type \nr1,0,0\nr0\nr2, 3 \n")
    assert _feed_modes(feed) == {"bus"}


def test_mode_type_extended_blocks():
    from transitio.gtfs._schedule import MODE_TYPES

    assert 300 in MODE_TYPES["rail"]
    assert 100 in MODE_TYPES["rail"]
    assert {400, 500, 600, 12} <= MODE_TYPES["subway"]
    assert {200, 700, 800, 11} <= MODE_TYPES["bus"]
    assert {900, 906, 5} <= MODE_TYPES["tram"]
    assert {1000, 1200} <= MODE_TYPES["ferry"]


def test_rank_prefers_official_active_specific():
    from transitio.catalog import Feed
    from transitio.pipeline._fetch import _rank

    def make(feed_id, official, status, box_deg=None):
        raw = {}
        if box_deg is not None:
            raw["latest_dataset"] = {
                "bounding_box": {
                    "minimum_longitude": 0.0,
                    "maximum_longitude": box_deg,
                    "minimum_latitude": 0.0,
                    "maximum_latitude": box_deg,
                }
            }
        return Feed(
            id=feed_id,
            provider=None,
            status=status,
            official=official,
            producer_url=None,
            license_url=None,
            latest_dataset_url=None,
            locations=(),
            raw=raw,
        )

    national = make("mdb-1", True, "active", box_deg=10.0)
    regional = make("mdb-2", True, "active", box_deg=1.0)
    unofficial = make("mdb-3", False, "active", box_deg=0.5)
    inactive = make("mdb-4", True, "inactive", box_deg=0.5)
    unknown_extent = make("mdb-5", True, "active")

    ordered = sorted(
        [national, unofficial, unknown_extent, inactive, regional], key=_rank
    )
    assert [f.id for f in ordered] == [
        "mdb-2",  # official, active, most specific
        "mdb-1",  # official, active, larger extent
        "mdb-5",  # official, active, unknown extent
        "mdb-4",  # official but inactive
        "mdb-3",  # unofficial
    ]


def test_to_cafein_hands_feeds_and_pbf(tmp_path, monkeypatch):
    import sys
    import types

    from transitio.pipeline import FetchResult

    calls = {}

    class FakeNetwork:
        @classmethod
        def from_gtfs(cls, paths, **options):
            calls["paths"] = paths
            calls["options"] = options
            return "network"

    fake = types.ModuleType("cafein")
    fake.TransportNetwork = FakeNetwork
    monkeypatch.setitem(sys.modules, "cafein", fake)

    pbf = tmp_path / "aoi.osm.pbf"
    feed = tmp_path / "feed.zip"
    result = FetchResult(
        osm_pbf=pbf, feeds=[feed], reports=[{}], repairs=[[]], skipped=[]
    )
    assert result.to_cafein(walking_speed_kmph=5.0) == "network"
    assert calls["paths"] == [str(feed)]
    assert calls["options"] == {"osm_pbf": str(pbf), "walking_speed_kmph": 5.0}

    result.to_cafein(osm_pbf=None)
    assert calls["options"] == {"osm_pbf": None}


def test_to_cafein_without_feeds_or_cafein(tmp_path, monkeypatch):
    import builtins
    import sys

    from transitio.pipeline import FetchResult

    empty = FetchResult(
        osm_pbf=tmp_path / "aoi.osm.pbf", feeds=[], reports=[], repairs=[], skipped=[]
    )
    with pytest.raises(ValueError, match="no feeds"):
        empty.to_cafein()

    result = FetchResult(
        osm_pbf=tmp_path / "aoi.osm.pbf",
        feeds=[tmp_path / "feed.zip"],
        reports=[{}],
        repairs=[[]],
        skipped=[],
    )
    monkeypatch.delitem(sys.modules, "cafein", raising=False)
    real_import = builtins.__import__

    def no_cafein(name, *args, **kwargs):
        if name == "cafein":
            raise ImportError("No module named 'cafein'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_cafein)
    with pytest.raises(ImportError, match="cafein package is required"):
        result.to_cafein()


def test_to_pyrosm_opens_extract(tmp_path, monkeypatch):
    from transitio.pipeline import FetchResult

    opened = {}

    class FakeOSM:
        def __init__(self, filepath, **options):
            opened["filepath"] = filepath
            opened["options"] = options

    monkeypatch.setattr("pyrosm.OSM", FakeOSM)
    pbf = tmp_path / "aoi.osm.pbf"
    result = FetchResult(osm_pbf=pbf, feeds=[], reports=[], repairs=[], skipped=[])
    reader = result.to_pyrosm(bounding_box=[24.6, 60.1, 25.2, 60.4])
    assert isinstance(reader, FakeOSM)
    assert opened["filepath"] == str(pbf)
    assert opened["options"] == {"bounding_box": [24.6, 60.1, 25.2, 60.4]}


def test_fetch_requires_exactly_one_of_aoi_or_place():
    with pytest.raises(ValueError, match="exactly one of aoi= or place="):
        fetch()
    with pytest.raises(ValueError, match="exactly one of aoi= or place="):
        fetch((0, 0, 1, 1), place="X")


DIRECT, STATIC, LATEST = (
    "https://producer.example/gtfs.zip",
    "https://atlas.example/gtfs.zip",
    "https://files.example/mdb-9/latest.zip",
)


@pytest.mark.parametrize(
    "urls, broken, calls, source, failures",
    [
        ((DIRECT, STATIC, LATEST), {}, [DIRECT], "producer", []),
        (
            (DIRECT, STATIC, None),
            {DIRECT: "raise"},
            [DIRECT, STATIC],
            "producer",
            ["mdb: down"],
        ),
        (
            (DIRECT, STATIC, LATEST),
            {DIRECT: "raise", STATIC: "raise"},
            [DIRECT, STATIC, LATEST],
            "mdb_latest",
            ["mdb: down", "atlas: down"],
        ),
        (
            (DIRECT, None, LATEST),
            {DIRECT: "html"},
            [DIRECT, LATEST],
            "mdb_latest",
            ["mdb: not a zip archive"],
        ),
        (
            (DIRECT, DIRECT, LATEST),
            {DIRECT: "raise"},
            [DIRECT, LATEST],
            "mdb_latest",
            ["mdb: down"],
        ),
        ((None, STATIC, LATEST), {}, [STATIC], "producer", []),
        (
            (DIRECT, STATIC, LATEST),
            dict.fromkeys((DIRECT, STATIC, LATEST), "raise"),
            [DIRECT, STATIC, LATEST],
            None,
            ["mdb: down", "atlas: down", "mdb_latest: down"],
        ),
        ((None, None, None), {}, [], None, []),
    ],
    ids=[
        "direct",
        "atlas-after-direct",
        "hosted-copy-last",
        "html-fails",
        "each-url-once",
        "atlas-before-hosted-copy",
        "all-fail",
        "no-url",
    ],
)
def test_download_indexed_tries_the_producer_then_the_hosted_copy(
    tmp_path, urls, broken, calls, source, failures
):
    from types import SimpleNamespace

    from transitio.exceptions import DownloadError
    from transitio.pipeline._fetch import _download_indexed

    direct, static, latest = urls
    feed = SimpleNamespace(
        feed_id="f-a",
        access=None,
        _row={
            "mdb": {"urls": {"direct_download": direct, "latest": latest}},
            "atlas": {"urls": {"static_current": static}},
        },
    )
    called = []

    def serve(url, directory):
        called.append(url)
        if broken.get(url) == "raise":
            raise RuntimeError("down")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "latest.zip"
        path.write_bytes(b"<html></html>" if broken.get(url) else _zip(GTFS))
        return path

    db = SimpleNamespace(
        _fetch_latest=lambda proxy, directory: serve(
            proxy.latest_dataset_url, directory
        )
    )
    atlas = SimpleNamespace(
        _fetch_static=lambda record, directory: serve(record.static_url, directory)
    )
    if source is None:
        with pytest.raises(DownloadError) as caught:
            _download_indexed(feed, db, atlas, tmp_path, None)
        expected = "; ".join(failures) or "feed f-a has no downloadable url"
        assert str(caught.value) == expected
    else:
        path, fetched_from, seen, url = _download_indexed(
            feed, db, atlas, tmp_path, None
        )
        assert (zipfile.is_zipfile(path), fetched_from, seen, url) == (
            True,
            source,
            failures,
            calls[-1],
        )
    assert called == calls


def test_fetch_place_selects_downloads_and_processes(tmp_path, monkeypatch):
    import io as _io

    import transitio.index as transitio_index
    from index_fixture import HULL, covered_feed, edge, write_index

    # Two catalogue entries serving the same archive: one is delivered.
    feeds = [
        {
            **covered_feed(feed_id, coverage_source="crawl"),
            "coverage": HULL,
            "atlas": {"urls": {"static_current": f"https://feeds.example/{feed_id}"}},
        }
        for feed_id in ("f-a", "f-b")
    ]
    edges = [edge("Q1757", feed_id, tier="local") for feed_id in ("f-a", "f-b")]
    index = transitio_index.read_index(
        write_index(tmp_path / "index", feeds=feeds, edges=edges)
    )

    buffer = _io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in GTFS.items():
            archive.writestr(name, content)
    payload = buffer.getvalue()

    def fake_download(self, feed, directory=None):
        base = __import__("pathlib").Path(directory) if directory else tmp_path
        base = base / feed.feed_id
        base.mkdir(parents=True, exist_ok=True)
        path = base / "latest.zip"
        path.write_bytes(payload)
        return path

    fake_pbf = tmp_path / "aoi.osm.pbf"
    fake_pbf.write_bytes(b"\x00fake")
    monkeypatch.setattr(
        "transitio.catalog.TransitlandAtlas._fetch_static", fake_download
    )
    monkeypatch.setattr("transitio.osm.fetch_pbf", lambda *a, **k: fake_pbf)
    monkeypatch.setattr("transitio.osm._fetch.fetch_pbf", lambda *a, **k: fake_pbf)

    result = fetch(
        place="Q1757",
        index=index,
        directory=tmp_path / "out",
        crop=False,
        reference_date="20260601",
    )
    assert result.osm_pbf == fake_pbf
    assert len(result.feeds) == 1
    ((feed_id, reason),) = result.skipped
    assert reason == f"same content as {({'f-a', 'f-b'} - {feed_id}).pop()}"
    assert result.selections == []
    first, second = result.selection
    assert second["same_as"] == [first["feed_id"]]


def test_fetch_aoi_rejects_place_only_arguments():
    for kwargs in (
        {"exclude": ["national"]},
        {"tiers": ["local"]},
        {"on_unknown": "exclude"},
        {"contained": "keep"},
        {"credentials": {"p": {"key": "k"}}},
    ):
        with pytest.raises(ValueError, match="apply only with place="):
            fetch((0, 0, 1, 1), **kwargs)
    with pytest.raises(ValueError, match="'keep' or 'drop'"):
        fetch(place="X", contained="maybe")
    with pytest.raises(ValueError, match="'skip' or 'keep'"):
        fetch(place="X", expired="maybe")
    with pytest.raises(ValueError, match="'drop', 'exact' or 'keep'"):
        fetch(place="X", duplicate_trips="maybe")
    with pytest.raises(ValueError, match="disagree"):
        fetch(place="X", when="2026-06-01", reference_date="20260602")


def _partitioned_index(
    tmp_path, monkeypatch, feeds, contained=None, edges=None, providers=None
):
    """A schema-10 index serving Q1757 with ``feeds`` (``{feed id: extra
    columns}``) in that order, each a local feed at
    ``https://feeds.example/<feed id>``; ``edges`` adds edge fields by feed id.
    With ``providers`` (access provider records) a schema-11 index."""
    import transitio.index as transitio_index
    from index_fixture import HULL, PLACES, covered_feed, edge, write_partitioned_index

    version = 10 if providers is None else 11
    monkeypatch.setattr(
        "transitio.__version__", transitio_index.MIN_READER_VERSIONS[version]
    )
    rows = [
        {
            **covered_feed(feed_id, coverage_source="crawl"),
            "coverage": HULL,
            "home_country": "FI",
            "scope": "domestic",
            "atlas": {"urls": {"static_current": f"https://feeds.example/{feed_id}"}},
            **extra,
        }
        for feed_id, extra in feeds.items()
    ]
    records = [
        edge(
            "Q1757",
            feed_id,
            tier="local",
            relevance_category="primary",
            relevance=1 - position / 100,
            cross_border=False,
        )
        for position, feed_id in enumerate(feeds)
    ]
    for record in records:
        record.update((edges or {}).get(record["feed_id"], {}))
    directory = write_partitioned_index(
        tmp_path / "index",
        feeds=rows,
        places=[PLACES[0]],
        edges=records,
        contained=contained or {},
        access=None if providers is None else {"providers": providers},
    )
    return transitio_index.read_index(directory)


def test_contained_keep_leaves_no_feed_out(tmp_path, monkeypatch):
    import pathlib

    ids = ("f-a", "f-b", "f-c")
    index = _partitioned_index(
        tmp_path, monkeypatch, dict.fromkeys(ids, {}), contained={"f-c": ["f-b"]}
    )
    later = {"stop_times.txt": GTFS["stop_times.txt"].replace("08:", "10:")}
    other = {**GTFS, "agency.txt": GTFS["agency.txt"].replace("HSL", "VR"), **later}
    payloads = {"f-a": _zip(GTFS), "f-b": _zip(PARTIAL), "f-c": _zip(other)}
    fetched = []

    def fake_download(self, feed, directory=None):
        fetched.append(feed.feed_id)
        base = pathlib.Path(directory) / feed.feed_id
        base.mkdir(parents=True, exist_ok=True)
        path = base / "latest.zip"
        path.write_bytes(payloads[feed.feed_id])
        return path

    fake_pbf = tmp_path / "aoi.osm.pbf"
    fake_pbf.write_bytes(b"\x00fake")
    monkeypatch.setattr(
        "transitio.catalog.TransitlandAtlas._fetch_static", fake_download
    )
    monkeypatch.setattr("transitio.osm.fetch_pbf", lambda *a, **k: fake_pbf)
    monkeypatch.setattr("transitio.osm._fetch.fetch_pbf", lambda *a, **k: fake_pbf)
    result = fetch(
        place="Q1757",
        index=index,
        directory=tmp_path / "out",
        crop=False,
        contained="keep",
        reference_date="20260601",
    )
    decisions = [
        (e["feed_id"], e["decision"], e["contained_in"]) for e in result.selection
    ]
    assert sorted(fetched) == list(ids) and len(result.feeds) == 3
    assert result.contained == {}
    assert decisions == [(i, "delivered", []) for i in ids]


def _calendar(start, end, days="1111111"):
    """GTFS whose one service runs on ``days`` (Monday first) from ``start``
    to ``end``."""
    return {
        **GTFS,
        "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,"
        f"saturday,sunday,start_date,end_date\nwk,{','.join(days)},{start},{end}\n",
    }


DAY = "2026-06-07"  # the study day, a Sunday
ENDED, NOW = ("2021-01-01", "2021-12-31"), ("2026-01-01", "2026-12-31")
LATER, NEXT = ("2027-01-01", "2027-12-31"), ("2026-07-01", "2026-12-31")
SPRING = ("2026-01-01", "2026-05-01")
# Past the validator's calendar expansion cap.
LONG = ("1900-01-01", "2099-12-31")
ETAG, MODIFIED = {"etag": '"v1"'}, {"last_modified": "Tue, 01 Jun 2021 00:00:00 GMT"}
# The skip reasons the cases expect.
R_ENDED, R_SPRING = "service ended 2021-12-31", "service ended 2026-05-01"
R_STARTS = "service starts 2027-01-01, after 2026-06-07"
R_IDLE, SAME = f"no service on {DAY}", "; unchanged since indexed"


@pytest.mark.filterwarnings("ignore:no Mobility Database API token")
@pytest.mark.parametrize(
    "indexed, validators, probe, served, when, expired, downloaded, reason, window",
    [
        (ENDED, ETAG, 304, ENDED, DAY, "skip", False, R_ENDED + SAME, None),
        (ENDED, ETAG, 200, NOW, DAY, "skip", True, None, NOW),
        (ENDED, ETAG, "timeout", ENDED, DAY, "skip", True, R_ENDED, ENDED),
        (ENDED, {}, 304, NOW, DAY, "skip", True, None, NOW),
        (LATER, MODIFIED, 304, LATER, DAY, "skip", False, R_STARTS + SAME, None),
        (NOW, ETAG, 304, (*NOW, "1111100"), DAY, "skip", True, R_IDLE, NOW),
        (NOW, {}, 200, (*NOW, "0000000"), DAY, "skip", True, R_IDLE, None),
        (NOW, {}, 200, (*LONG, "1111100"), DAY, "skip", True, R_IDLE, LONG),
        (LATER, ETAG, 200, LATER, DAY, "skip", True, R_STARTS, LATER),
        (NEXT, ETAG, 304, NEXT, None, "skip", True, None, NEXT),
        (NOW, ETAG, 304, SPRING, None, "skip", True, R_SPRING, SPRING),
        (NOW, ETAG, 304, NOW, DAY, "skip", True, None, NOW),
        (ENDED, ETAG, 304, ENDED, None, "keep", True, None, ENDED),
    ],
    ids=[
        "ended-unchanged",
        "ended-renewed",
        "ended-probe-timeout",
        "ended-no-validators",
        "starts-after-unchanged",
        "weekdays-on-sunday",
        "unknown-window-idle",
        "capped-expansion-idle",
        "starts-after-renewed",
        "no-study-day-starts-next-month",
        "no-study-day-served-ended",
        "current",
        "keep-unchanged",
    ],
)
def test_date_rules_decide_before_and_after_download(
    tmp_path,
    monkeypatch,
    indexed,
    validators,
    probe,
    served,
    when,
    expired,
    downloaded,
    reason,
    window,
):
    from transitio.catalog import TransitlandAtlas

    monkeypatch.delenv("MOBILITY_API_REFRESH_TOKEN", raising=False)
    columns = {"service_start": indexed[0], "service_end": indexed[1], **validators}
    index = _partitioned_index(tmp_path, monkeypatch, {"f-a": columns})
    start, end, *days = (value.replace("-", "") for value in served)
    payload = _zip(_calendar(start, end, *days))
    downloads = []

    def handler(request):
        if request.method == "GET":
            downloads.append(request.url)
            return httpx.Response(200, content=payload)
        if probe == "timeout":
            raise httpx.ReadTimeout("slow host", request=request)
        # A 304 answers only the validators the index recorded.
        sent = {
            "etag": request.headers.get("If-None-Match"),
            "last_modified": request.headers.get("If-Modified-Since"),
        }
        matched = validators and all(sent[k] == v for k, v in validators.items())
        return httpx.Response(probe if matched else 200)

    class Served(TransitlandAtlas):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("transitio.catalog.TransitlandAtlas", Served)
    result = fetch(
        place="Q1757",
        index=index,
        directory=tmp_path / "out",
        crop=False,
        osm=False,
        when=when,
        expired=expired,
    )
    (entry,) = result.selection
    expected = (downloaded, reason, window and list(window))
    assert (bool(downloads), entry["reason"], entry["feed_window"]) == expected
    assert entry["decision"] == ("skipped" if reason else "delivered")
    assert entry["index_window"] == list(indexed)
    assert (entry["path"] is not None) == (entry["decision"] == "delivered")
    assert result.skipped == [("f-a", reason)] * bool(reason)
    assert result.selection_table().to_dict("records") == result.selection


API = "https://api.example.com/feed"
CDN = "https://cdn.example.org/feed.zip"
CURATED = "https://curated.example/feed.zip"
HOSTED = "https://files.example.com/f-a/latest.zip"
# Credential values with reserved characters and a space, one a prefix of
# another, and a Basic-auth password.
SECRET, PREFIX, PASSWORD = "a&b=c/d%e f", "a&b", "p@ss/w%rd=&"
PAYLOAD = base64.b64encode(f"user:{PASSWORD}".encode()).decode()
PROVIDERS = [
    {
        "provider_id": "p-key",
        "name": "Key Co",
        "registration_url": "https://key.example/join",
        "credential_fields": ["key"],
    },
    {"provider_id": "p-pair", "credential_fields": ["client_id", "client_secret"]},
    {"provider_id": "p-token", "credential_fields": ["token"]},
    {"provider_id": "p-basic", "credential_fields": ["username", "password"]},
]
KEY = {"access_provider": "p-key", "auth_method": "query_param"}
KEY["auth_params"] = {"key": "key"}
QUERY = {"access_provider": "p-pair", "auth_method": "query_param"}
QUERY["auth_params"] = {"id": "client_id", "secret": "client_secret"}
HEADER = {"access_provider": "p-token", "auth_method": "header"}
HEADER["auth_params"] = {"X-Api-Key": "token"}
BASIC = {"access_provider": "p-basic", "auth_method": "basic_auth", "auth_params": {}}


def _protected(columns, url=API):
    return {"access": "key", "download_url": url, **columns}


def _credentials_env(monkeypatch, tmp_path, env):
    """The credential variables ``env`` (``{name suffix: value}``) alone and
    an empty config directory."""
    import transitio.credentials

    for name in list(os.environ):
        if name.startswith("TRANSITIO_KEY_"):
            monkeypatch.delenv(name)
    for name, value in env.items():
        monkeypatch.setenv(f"TRANSITIO_KEY_{name}", value)
    config = str(tmp_path / "config")
    monkeypatch.setattr(
        transitio.credentials.platformdirs, "user_config_dir", lambda name: config
    )


def _serve(monkeypatch, handler):
    """Both catalogue clients over one stub transport running ``handler``."""
    from transitio.catalog import MobilityDatabase, TransitlandAtlas

    class Atlas(TransitlandAtlas):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    class Mdb(MobilityDatabase):
        def __init__(self, refresh_token=None, **kwargs):
            transport = httpx.MockTransport(handler)
            super().__init__(refresh_token, transport=transport, **kwargs)

    monkeypatch.delenv("MOBILITY_API_REFRESH_TOKEN", raising=False)
    monkeypatch.setattr("transitio.catalog.TransitlandAtlas", Atlas)
    monkeypatch.setattr("transitio.catalog.MobilityDatabase", Mdb)


@pytest.mark.filterwarnings("ignore:no Mobility Database API token")
def test_protected_feeds_are_decided_before_download(tmp_path, monkeypatch):
    from transitio.index import place

    unsupported = "its access method is not supported"
    cookie = {**HEADER, "auth_params": {"Cookie": "token"}}
    # Feed id, its access columns and why it is skipped.
    cases = [
        (
            "f-unresolved",
            {"access": "key", "download_url": API},
            "the index has no access details for it",
        ),
        (
            "f-unsupported",
            _protected({**HEADER, "auth_method": "unsupported"}),
            unsupported,
        ),
        ("f-cookie", _protected(cookie), unsupported),
        ("f-http", _protected(KEY, "http://api.example.com/f"), "its URL is not https"),
        ("f-missing", _protected(KEY), "credentials missing for p-key"),
        ("f-incomplete", _protected(QUERY), "credentials incomplete for p-pair"),
        (
            "f-header",
            _protected(HEADER),
            "credential token has characters its method cannot carry",
        ),
        (
            "f-basic",
            _protected(BASIC),
            "credential username has characters its method cannot carry",
        ),
    ]
    feeds = {feed_id: columns for feed_id, columns, _ in cases}
    index = _partitioned_index(tmp_path, monkeypatch, feeds, providers=PROVIDERS)
    env = {"P_KEY__KEY": "", "P_PAIR__CLIENT_ID": "i", "P_TOKEN__TOKEN": "tök"}
    env.update(P_BASIC__USERNAME="us:er", P_BASIC__PASSWORD="pw")
    _credentials_env(monkeypatch, tmp_path, env)
    requests = []
    _serve(monkeypatch, lambda request: requests.append(request))
    options = {"index": index, "directory": tmp_path / "out", "osm": False}
    result = fetch(place="Q1757", crop=False, **options)
    instructions = {
        feed.feed_id: feed.access_instructions()
        for feed in place("Q1757", index=index).feeds()
    }
    expected = [
        (feed_id, f"protected feed: {reason}; {instructions[feed_id]}")
        for feed_id, _, reason in cases
    ]
    assert result.skipped == expected
    assert "Register at https://key.example/join" in instructions["f-missing"]
    # The explicit argument is checked against the index before any download,
    # and no transitio frame of the traceback holds a value given with it.
    valid = {"p-token": {"token": SECRET}}
    for credentials, message in [
        ({"p-none": {"key": "k"}}, "lists no credential provider 'p-none'"),
        ({"p-key": {"other": "k"}}, "issues no credential field 'other'"),
        ({"p-key": {"key": ""}}, "credential 'key' must be a non-empty string"),
    ]:
        with pytest.raises(ValueError, match=message) as caught:
            fetch(place="Q1757", credentials={**valid, **credentials}, **options)
        trace = caught.value.__traceback__
        while trace is not None:
            frame = trace.tb_frame
            if frame.f_globals["__name__"].startswith("transitio."):
                assert not any(_leaks(repr(v)) for v in frame.f_locals.values())
            trace = trace.tb_next
    assert requests == []


def _leaks(text):
    """Whether ``text`` holds a credential of these tests in any encoding."""
    from transitio.catalog._access import _Secret

    forms = (SECRET, PREFIX, PASSWORD, PAYLOAD, f"Basic {PAYLOAD}")
    return any(_Secret(form).occurs_in(text) for form in forms)


def _carried(request):
    """Whether ``request`` carries the credentials of the test's method."""
    query = urllib.parse.parse_qs(request.url.query.decode())
    return (
        query == {"id": [PREFIX], "secret": [SECRET]}
        or request.headers.get("X-Api-Key") == SECRET
        or request.headers.get("Authorization") == f"Basic {PAYLOAD}"
    )


ENDED_ETAG = {"service_start": "2021-01-01", "service_end": "2021-12-31", **ETAG}
BACK = "https://api.example.com/back"
COOKIE = {"Set-Cookie": "s=1; Domain=example.org; Path=/"}
# The access URL redirecting to the CDN, which redirects back to it.
TO_CDN = {API: (302, {"Location": CDN, **COOKIE})}
ROUND_TRIP = {**TO_CDN, CDN: (302, {"Location": BACK})}
REFLECTED = {API: (302, {"Location": f"{CDN}?x={urllib.parse.quote(SECRET)}"})}
DELIVERED = [("GET", API), ("GET", CDN)]


FROM_HOSTED = "from the Mobility Database hosted copy"
MISSING = "protected feed: credentials missing for p-key; {instructions}"
HOSTED_404 = f"mdb_latest: {HOSTED}: HTTP 404 Not Found"
NONE = (None, None, None)


@pytest.mark.filterwarnings("ignore:no Mobility Database API token")
@pytest.mark.parametrize(
    "columns, credentials, routes, seen, outcome",
    [
        ({"download_url": CURATED}, None, {}, [("GET", CURATED)], NONE),
        (
            {"download_url": CURATED},
            None,
            {CURATED: (404, {})},
            [("GET", CURATED), ("GET", HOSTED)],
            (None, f"download_url: {CURATED}: HTTP 404 Not Found", FROM_HOSTED),
        ),
        (
            _protected(QUERY),
            {"p-pair": {"client_secret": SECRET}},
            TO_CDN,
            DELIVERED,
            NONE,
        ),
        (_protected(HEADER), None, TO_CDN, DELIVERED, NONE),
        (
            _protected(BASIC),
            {"p-basic": {"username": "user", "password": PASSWORD}},
            TO_CDN,
            DELIVERED,
            NONE,
        ),
        (_protected(HEADER), None, ROUND_TRIP, [*DELIVERED, ("GET", BACK)], NONE),
        (
            {**_protected(HEADER), **ENDED_ETAG},
            None,
            {},
            [("HEAD", API)],
            (R_ENDED + SAME, None, None),
        ),
        (
            _protected(HEADER),
            None,
            {API: (401, {})},
            [("GET", API), ("GET", HOSTED)],
            (None, f"download_url: {API}: HTTP 401 Unauthorized", FROM_HOSTED),
        ),
        (
            {**_protected(HEADER), "mdb": {"urls": {"direct_download": API}}},
            None,
            REFLECTED,
            [("GET", API)],
            (
                "download failed: download_url: redirect carries a credential",
                "download_url: redirect carries a credential",
                None,
            ),
        ),
        (
            _protected(KEY),
            None,
            {},
            [("GET", HOSTED)],
            (None, None, f"{FROM_HOSTED}; {MISSING}"),
        ),
        (
            _protected(KEY),
            None,
            {HOSTED: (404, {})},
            [("GET", HOSTED)],
            (f"download failed: {HOSTED_404}", HOSTED_404, MISSING),
        ),
        (
            _protected(HEADER),
            None,
            {API: RuntimeError(SECRET)},
            [("GET", API), ("GET", HOSTED)],
            (None, "download_url: ***", FROM_HOSTED),
        ),
    ],
    ids=[
        "open-download-url",
        "open-hosted-copy-second",
        "query-explicit-over-env",
        "header-env",
        "basic-explicit",
        "back-to-the-access-origin",
        "probe-unchanged",
        "refused",
        "location-reflects-a-credential",
        "keyless-hosted-copy",
        "keyless-hosted-copy-fails",
        "keyed-error-holding-a-credential",
    ],
)
def test_schema_11_downloads(
    tmp_path, monkeypatch, caplog, columns, credentials, routes, seen, outcome
):
    from transitio.index import place

    mdb = {"urls": {"direct_download": API, "latest": HOSTED}}
    feeds = {"f-a": {"mdb": mdb, **columns}}
    index = _partitioned_index(tmp_path, monkeypatch, feeds, providers=PROVIDERS)
    env = {"P_PAIR__CLIENT_ID": PREFIX, "P_PAIR__CLIENT_SECRET": "wrong"}
    env.update(P_TOKEN__TOKEN=SECRET, P_BASIC__USERNAME="wrong")
    _credentials_env(monkeypatch, tmp_path, env)
    payload = _zip(_calendar("20260101", "20261231"))
    requests = []

    def handler(request):
        url = str(request.url.copy_with(query=None))
        requests.append((request.method, url, request))
        route = routes.get(url, (200, {}))
        if isinstance(route, Exception):
            raise route
        status, headers = route
        if url == API and not _carried(request):
            status = 401
        elif request.method == "HEAD":
            status = 304
        # The access origin's reason phrase reflects a credential.
        phrase = {"reason_phrase": SECRET.encode()} if url == API else {}
        body = payload if status == 200 else b""
        return httpx.Response(status, headers=headers, content=body, extensions=phrase)

    _serve(monkeypatch, handler)
    caplog.set_level(logging.DEBUG, logger="httpx")
    with warnings.catch_warnings(record=True) as caught:
        result = fetch(
            place="Q1757",
            index=index,
            credentials=credentials,
            when=DAY,
            directory=tmp_path / "out",
            cache_dir=tmp_path / "cache",
            crop=False,
            osm=False,
        )
    (entry,) = result.selection
    assert [(method, url) for method, url, _ in requests] == seen
    (feed,) = place("Q1757", index=index).feeds()
    instructions = feed.access_instructions()
    expected = tuple(v and v.format(instructions=instructions) for v in outcome)
    assert (entry["reason"], entry["download_errors"], entry["note"]) == expected
    assert all("Cookie" not in request.headers for *_, request in requests)
    # Credentials reach the access URL alone, and the stub below sees them.
    carried = [_carried(request) for *_, request in requests]
    assert carried == [("access" in columns) and url == API for _, url in seen]
    hosted = [request for _, url, request in requests if url == HOSTED]
    assert not any(_leaks(f"{r.url} {dict(r.headers)}") for r in hosted)
    if entry["path"] is not None:
        sidecar = entry["path"].with_suffix(".provenance.json").read_text()
        source = ("producer", seen[0][1]) if seen[-1][1] != HOSTED else None
        expected = source or ("mdb_latest", HOSTED)
        assert (entry["fetched_from"], json.loads(sidecar)["source_url"]) == expected
    paths = "\n".join(str(path) for path in tmp_path.rglob("*"))
    sidecars = [p.read_text() for p in tmp_path.rglob("*.provenance.json")]
    logged = [record.getMessage() for record in caplog.records]
    texts = [repr(result), paths, *sidecars, *logged, *map(str, caught)]
    assert not any(map(_leaks, texts))


def test_protected_archives_are_not_shared(tmp_path):
    from transitio import _http
    from transitio.catalog._access import _Access, _Secret
    from transitio.pipeline._fetch import _Archives

    def handler(request):
        return httpx.Response(200, content=request.headers["X-Api-Key"].encode())

    first, second = (
        _Access(API, "header", {"X-Api-Key": "token"}, {"token": _Secret(token)})
        for token in ("t1", "t2")
    )
    archives, stub = _Archives(tmp_path), httpx.MockTransport(handler)
    with _http.client() as client:
        found = [archives.get(client, API, a, stub) for a in (first, second, first)]
    assert [path.read_bytes() for path, *_ in found] == [b"t1", b"t2", b"t1"]
    assert found[0] == found[2]


def _network(agency="HSL", start="20260101", stops=None, hours=(8,), **options):
    """GTFS of ``agency`` whose ``routes`` each run a trip from s2 to s3 at
    each of ``hours``, daily from ``start`` to ``end`` (through 2026 unless
    given), among stops s<i> (s0 to s9 unless ``stops`` names them); with a
    ``headway``, each trip repeats that often for an hour. ``types`` maps
    routes to a type other than bus, ``zone`` is the agency's time zone and
    ``extra`` adds rows by file."""
    stops = range(10) if stops is None else stops
    types, zone = options.get("types", {}), options.get("zone", "Europe/Helsinki")
    trips = [(r, h) for r in options.get("routes", ("r1",)) for h in hours]
    times = (
        "{0}{1},{1:02}:00:00,{1:02}:00:00,s2,1\n{0}{1},{1:02}:10:00,{1:02}:10:00,s3,2"
    )
    tables = {
        **_calendar(start, options.get("end", "20261231")),
        "agency.txt": GTFS["agency.txt"]
        .replace("HSL", agency)
        .replace("Europe/Helsinki", zone),
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\n"
        + "".join(f"s{i},S{i},{60 + i / 100:.2f},24.9\n" for i in stops),
        "routes.txt": "route_id,agency_id,route_short_name,route_type\n"
        + "".join(
            f"{r},hsl,{r},{types.get(r, 3)}\n" for r in options.get("routes", ("r1",))
        ),
        "trips.txt": "route_id,service_id,trip_id\n"
        + "".join(f"{r},wk,{r}{h}\n" for r, h in trips),
        "stop_times.txt": GTFS["stop_times.txt"].split("\n")[0]
        + "".join("\n" + times.format(r, h) for r, h in trips),
    }
    if options.get("transfers"):
        # Naming the first trip, which is then never compared as a repeat.
        tables["transfers.txt"] = (
            "from_stop_id,to_stop_id,from_trip_id,to_trip_id,transfer_type\n"
            "s2,s3,{0}{1},{0}{1},0\n".format(*trips[0])
        )
    if options.get("headway"):
        tables["frequencies.txt"] = (
            "trip_id,start_time,end_time,headway_secs\n"
            + "".join(
                f"{r}{h},{h:02}:00:00,{h + 1:02}:00:00,{options['headway']}\n"
                for r, h in trips
            )
        )
    for name, rows in options.get("extra", {}).items():
        tables[name] = tables[name].rstrip("\n") + "\n" + rows
    return _zip(tables)


# Specs for _network, plus "cut" (the routes a selector keeps), "in" (the
# containers the index names), "ended" (an index window that ended) and
# "renewed" (a probe answering 200).
NEW, OLD, OLDER = ({"start": f"2026{month}01"} for month in ("06", "05", "04"))
C, F = {"agency": "C"}, {"agency": "F"}
AB, ABC = {**C, "routes": ("a", "b")}, {**C, "routes": ("a", "b", "c")}
IN_C = {**F, "in": "C", "hours": (9,)}
ADDS = f"+ similar to A but adds service on {DAY}"
ON_DAY = {"when": DAY}
# A trip half a minute after the 09:00 trip, which it nearly repeats.
NEAR_NINE = {
    "trips.txt": "r1,wk,x\n",
    "stop_times.txt": "x,09:00:30,09:00:30,s2,1\nx,09:10:30,09:10:30,s3,2\n",
}
# A network on a placeholder calendar, and the note it gets.
HELD = {"start": "20000101", "end": "20990101"}
HELD_NOTE = "placeholder calendar 2000-01-01 to 2099-01-01"
KEPT = "+ kept: containment in C not proven current"
SKIP_C, WHOLE = "- contained in C [C]", "delivered whole: selector unavailable"


@pytest.mark.filterwarnings("ignore:no Mobility Database API token")
@pytest.mark.parametrize(
    "feeds, options, expected",
    [
        ({"X": F, "Y": F}, {}, {"X": "+", "Y": "- same content as X [X]"}),
        (
            {"X": {**ABC, "cut": "a b"}, "Y": {**ABC, "cut": "b c"}},
            {},
            {
                "X": "+ cut to 2 of 3 routes",
                "Y": "+ cut to 2 of 3 routes; 1 repeated trips of X left out [X] (1)",
            },
        ),
        ({"F": IN_C, "C": {**C, "renewed": True}}, {}, {"C": "+", "F": KEPT}),
        ({"F": {**IN_C, "renewed": True}, "C": C}, {}, {"C": "+", "F": KEPT}),
        (
            {"F": IN_C, "C": {**AB, "cut": "a"}},
            {},
            {
                "C": "+ cut to 1 of 2 routes",
                "F": f"+ {WHOLE}; kept: container C cropped to selected routes",
            },
        ),
        (
            {"F": IN_C, "C": {**C, "ended": True}},
            {},
            {"C": f"- {R_ENDED}{SAME}", "F": "+ kept: container C skipped"},
        ),
        (
            {"P": {**AB, "cut": "a"}, "C": AB, "F": IN_C},
            {},
            {
                "P": "+ cut to 1 of 2 routes",
                "C": f"+ {WHOLE}; 1 repeated trips of P left out [P] (1)",
                "F": f"{SKIP_C} -> P C",
            },
        ),
        (
            {"C": AB, "P": {**AB, "cut": "a"}, "F": IN_C},
            {},
            {"C": f"+ {WHOLE}", "P": "- same content as C [C]", "F": f"{SKIP_C} -> C"},
        ),
        (
            {"A": NEW, "B": OLD},
            ON_DAY,
            {"A": "+", "B": "- another version of A [A 1.0 1.0]"},
        ),
        (
            {"A": NEW, "B": {**OLD, "agency": "HSL Oy."}},
            ON_DAY,
            {"A": "+", "B": "- another version of A [A 1.0 1.0]"},
        ),
        (
            {"A": NEW, "B": {**OLD, "agency": ""}},
            ON_DAY,
            {"A": "+", "B": "- another version of A [A 1.0 1.0]"},
        ),
        (
            {"A": NEW, "B": {**OLD, "hours": (8, 9)}},
            ON_DAY,
            {"A": "+", "B": f"{ADDS}; 1 repeated trips of A left out (1)"},
        ),
        ({"A": NEW, "B": {**OLD, "hours": (9,)}}, ON_DAY, {"A": "+", "B": ADDS}),
        (
            {"A": {**NEW, "headway": 600}, "B": {**OLD, "headway": 600}},
            ON_DAY,
            {"A": "+", "B": "- another version of A [A 1.0 1.0]"},
        ),
        (
            {"A": {**NEW, "headway": 600}, "B": {**OLD, "headway": 300}},
            ON_DAY,
            {"A": "+", "B": ADDS},
        ),
        (
            {"A": {**NEW, "hours": (8, 9)}, "B": OLD, "C": {**OLDER, "hours": (10,)}},
            ON_DAY,
            {"A": "+", "B": "- another version of A [A 1.0 1.0]", "C": ADDS},
        ),
        (
            {"C": {**OLDER, "hours": (10,)}, "B": OLD, "A": {**NEW, "hours": (8, 9)}},
            ON_DAY,
            {"A": "+", "B": "- another version of A [A 1.0 1.0]", "C": ADDS},
        ),
        (
            {
                "A": NEW,
                "B": {**OLD, "stops": range(1, 11)},
                "C": {**OLDER, "stops": range(2, 12), "hours": (9,)},
            },
            ON_DAY,
            {"A": "+", "B": "- another version of A [A 1.0 0.818]", "C": "+"},
        ),
        (
            {"V": {**NEW, "hours": (9,)}, "C": OLD, "F": IN_C},
            ON_DAY,
            {"V": "+", "C": "+", "F": f"{SKIP_C} -> C"},
        ),
        (
            {"V": {**NEW, "hours": (9,)}, "E": OLD, "C": OLD, "F": IN_C},
            ON_DAY,
            {"V": "+", "E": "+", "C": "- same content as E [E]", "F": f"{SKIP_C} -> E"},
        ),
        (
            {
                "A": {**NEW, "stops": range(8)},
                "B": {**OLD, "stops": range(1, 9), "hours": (9,)},
            },
            ON_DAY,
            {"A": "+", "B": "+"},
        ),
        (
            {"A": NEW, "B": {**OLD, "transfers": True}},
            ON_DAY,
            {"A": "+", "B": "+ similar to A; kept, has transfers or pathways"},
        ),
        (
            {"A": {"start": "20260701"}, "B": OLD},
            {},
            {
                "A": "+ similar to B; kept, no study day",
                "B": "+ similar to A; kept, no study day",
            },
        ),
        (
            {"A": {**NEW, "end": "20991231"}, "B": OLD},
            ON_DAY,
            {
                "A": "+ placeholder calendar 2026-06-01 to 2099-12-31",
                "B": "- another version of A [A 1.0 1.0]",
            },
        ),
        (
            {
                "A": {**NEW, "stops": range(8)},
                "B": {**HELD, "stops": range(1, 9), "hours": (9,)},
            },
            ON_DAY,
            {"A": "+", "B": f"+ {HELD_NOTE}"},
        ),
        (
            {"A": HELD, "B": {**HELD, "routes": ("x",)}},
            ON_DAY,
            {"A": f"+ {HELD_NOTE}", "B": f"+ {HELD_NOTE}"},
        ),
        ({"X": F, "Y": C}, {}, {"X": "+", "Y": "- every trip repeats a trip of X (1)"}),
        (
            {"A": NEW, "B": {**OLD, **F}},
            ON_DAY,
            {"A": "+", "B": f"- every trip on {DAY} repeats a trip of A (1)"},
        ),
        ({"A": NEW, "B": {**OLD, **F}}, {}, {"A": "+", "B": "+"}),
        (
            {"A": NEW, "B": {**NEW, **F, "zone": "America/New_York"}},
            ON_DAY,
            {
                "A": "+",
                "B": "+ agency_timezone America/New_York; stops in Europe/Helsinki",
            },
        ),
        ({"X": F, "Y": C}, {"duplicate_trips": "keep"}, {"X": "+", "Y": "+"}),
        (
            {"A": {**C, "hours": (8, 9)}, "B": {**F, "extra": NEAR_NINE}},
            {"duplicate_trips": "exact"},
            {"A": "+", "B": "+ 1 repeated trips of A left out (1)"},
        ),
    ],
    ids=(
        "identical identical-overlapping-routes container-renewed "
        "contained-renewed container-cropped container-expired "
        "partial-copy-first partial-copy-after version-covered "
        "version-legal-form version-unnamed-agency version-extra-trip "
        "version-other-times version-headway version-other-headway "
        "versions-three versions-three-reversed "
        "version-chain version-protected-container "
        "version-same-content-container version-under-stop-threshold "
        "version-transfers versions-no-study-day placeholder-starting-later "
        "placeholder-under-stop-threshold placeholders-other-routes "
        "repeats-other-agency repeats-on-the-day repeats-not-every-date "
        "repeats-other-time-zone repeats-kept repeats-exact"
    ).split(),
)
def test_fetch_delivers_one_copy_per_service(
    tmp_path, monkeypatch, feeds, options, expected
):
    result, downloads = _fetch_networks(tmp_path, monkeypatch, feeds, **options)
    seen = {e["feed_id"]: _seen(e) for e in result.selection}
    # A feed left out as contained, then the delivered feeds carrying it.
    for feed_id, carriers in result.contained.items():
        seen[feed_id] += f" -> {' '.join(carriers)}"
    assert seen == expected
    if options.get("duplicate_trips") == "keep":
        # Nothing is compared, so no feed counts repeats.
        assert {e["duplicate_trips"] for e in result.selection} == {None}
    # A feed left out before download is never downloaded; every other is.
    early = ("contained in", "unchanged since indexed")
    assert sorted(downloads) == sorted(
        e["feed_id"]
        for e in result.selection
        if not any(w in (e["reason"] or "") for w in early)
    )
    paths = [e["path"] for e in result.selection if e["decision"] == "delivered"]
    assert sorted(result.feeds) == sorted(paths) and len(result.reports) == len(paths)
    # A feed skipped or left out as a version leaves nothing in the directory.
    assert sorted((tmp_path / "out").rglob("*.zip")) == sorted(paths)


def _fetch_networks(tmp_path, monkeypatch, feeds, **options):
    """``(result, downloaded)``: a fetch of Q1757 into ``tmp_path / "out"``
    over ``feeds``, ``{feed id: spec}`` in record order (the specs above),
    with ``options``, and the ids of the feeds downloaded."""
    from transitio.catalog._atlas import TransitlandAtlas

    monkeypatch.delenv("MOBILITY_API_REFRESH_TOKEN", raising=False)
    payloads, columns, edges, contained, renewed = {}, {}, {}, {}, set()
    for feed_id, spec in feeds.items():
        spec = dict(spec)
        cut, containers, ended = (spec.pop(key, None) for key in ("cut", "in", "ended"))
        renewed.update([feed_id] if spec.pop("renewed", False) else [])
        payloads[feed_id] = _network(**spec)
        columns[feed_id] = {"etag": '"v1"'}
        if ended:
            columns[feed_id].update(service_start=ENDED[0], service_end=ENDED[1])
        if cut:
            selector = {"route_id": cut.split()}
            fields = {"selector_state": "complete", "selector": selector}
            edges[feed_id] = _stamp_fingerprint([fields], payloads[feed_id])[0]
        if containers:
            contained[feed_id] = containers.split()
    index = _partitioned_index(tmp_path, monkeypatch, columns, contained, edges)
    downloads = []

    def handler(request):
        feed_id = request.url.path.strip("/")
        if request.method == "GET":
            downloads.append(feed_id)
            return httpx.Response(200, content=payloads[feed_id])
        return httpx.Response(200 if feed_id in renewed else 304)

    class Served(TransitlandAtlas):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("transitio.catalog.TransitlandAtlas", Served)
    result = fetch(
        place="Q1757",
        index=index,
        directory=tmp_path / "out",
        crop=False,
        osm=False,
        tiers=["local"] if edges else None,
        **options,
    )
    return result, downloads


def _seen(entry):
    """An entry as ``"+ <note> [<links>] (<n>)"``, or ``"- <reason> ..."``
    when skipped, ``<n>`` the trips left out as repeats, when any."""
    links = entry["same_as"] + entry["contained_in"]
    links = links + [*(entry["version_of"] or {}).values()]
    links = links and f"[{' '.join(map(str, links))}]"
    head = f"- {entry['reason']}" if entry["reason"] else "+"
    count = entry["duplicate_trips"] and f"({entry['duplicate_trips']})"
    return " ".join(filter(None, (head, entry["note"], links, count)))


# The function each fault patches, and the calls it fails.
FAULTS = {
    "deliver": (
        "transitio.pipeline._fetch._deliver",
        lambda *a: a[2]["feed_id"] == "A",
    ),
    "crop": ("transitio.gtfs.crop_feed", lambda *a, **o: "exclude_trips" in o),
    "replace": (
        "transitio._http.replacing",
        lambda p: p.suffix == ".zip" and p.exists(),
    ),
    "unlink": ("os.unlink", lambda p, **_: "out" in pathlib.Path(p).parts),
    "report": (
        "transitio.pipeline._fetch._report",
        lambda made, *a: made["path"].name.endswith("-deduplicated.zip"),
    ),
    # The comparison alone reads transfers.txt without a study day.
    "read": (
        "transitio.pipeline._fetch._read_tables",
        lambda p, n, *a: "transfers.txt" in n,
    ),
    # Only the staging of the deduplicated outputs keeps no cleanup error.
    "staging": (
        "tempfile.TemporaryDirectory",
        lambda **o: "ignore_cleanup_errors" in o,
    ),
}
# A repeat of C's 08:00 trip and a trip of its own; a trip on a service
# whose calendar cannot be read, and one on a service no calendar declares.
TWO = {**F, "hours": (8, 9)}
X_TIMES = "x,09:00:00,09:00:00,s2,1\nx,09:10:00,09:10:00,s3,2\n"
UNREAD = {
    "trips.txt": "r1,bad,x\n",
    "stop_times.txt": X_TIMES,
    "calendar.txt": "bad,1,1,1,1,1,1,1,20260101,2026-12-31\n",
}
UNDECLARED = {"trips.txt": "r1,none,x\n", "stop_times.txt": X_TIMES}
# A trip whose first stop is missing, which the crop leaves out.
UNUSABLE = {"trips.txt": "r1,wk,x\n", "stop_times.txt": X_TIMES.replace("s2", "s99")}
KEPT_TRIPS = "+ repeated trips kept: disk full"


@pytest.mark.filterwarnings("ignore:no Mobility Database API token")
@pytest.mark.parametrize(
    "second, options, fault, expected, trips",
    [
        (F, {}, "deliver", {"A": "- processing failed: disk full", "B": "+"}, 1),
        (TWO, {}, "crop", {"A": "+", "B": KEPT_TRIPS}, 2),
        (TWO, {}, "replace", {"A": "+", "B": KEPT_TRIPS}, 2),
        (F, {}, "unlink", {"A": "+", "B": KEPT_TRIPS}, 1),
        (TWO, {}, "report", {"A": "+", "B": KEPT_TRIPS}, 2),
        (TWO, {}, "staging", {"A": KEPT_TRIPS, "B": KEPT_TRIPS}, 2),
        (TWO, {}, "read", {"A": KEPT_TRIPS, "B": KEPT_TRIPS}, 2),
        (
            {**TWO, "extra": UNUSABLE},
            {},
            None,
            {
                "A": "+",
                "B": "+ repeated trips kept: the crop would also leave out 1 trips "
                "that repeat no other feed's",
            },
            3,
        ),
        (
            {**F, "routes": ("r1", "t"), "types": {"t": 0}},
            {"modes": "bus"},
            None,
            {
                "A": "+",
                "B": "- serves ['tram'] after repeated trips were left out, "
                "not ['bus'] (1)",
            },
            None,
        ),
        (
            {**F, "extra": UNREAD},
            {"when": DAY},
            None,
            {"A": "+", "B": "+ 1 repeated trips of A left out (1)"},
            1,
        ),
        # Left with no calendar, the feed is delivered as it was.
        (
            {**F, "extra": UNDECLARED},
            {"when": DAY},
            None,
            {
                "A": "+",
                "B": "+ repeated trips kept: the feed left would be missing "
                "calendar.txt and calendar_dates.txt",
            },
            2,
        ),
    ],
    ids=(
        "covering-feed-undelivered crop replace unlink report staging read "
        "unusable-trip modes unread-service undeclared-service"
    ).split(),
)
def test_a_feed_keeps_the_trips_no_delivered_feed_repeats(
    tmp_path, monkeypatch, second, options, fault, expected, trips
):
    import importlib

    if fault is not None:
        target, failing = FAULTS[fault]
        module, name = target.rsplit(".", 1)
        real = getattr(importlib.import_module(module), name)

        def faulty(*args, **kwargs):
            if failing(*args, **kwargs):
                raise OSError("disk full")
            return real(*args, **kwargs)

        monkeypatch.setattr(target, faulty)
    feeds = {"A": C, "B": second}
    result, _ = _fetch_networks(tmp_path, monkeypatch, feeds, **options)
    assert {e["feed_id"]: _seen(e) for e in result.selection} == expected
    (path,) = [e["path"] for e in result.selection if e["feed_id"] == "B"]
    assert (path and len(_feed_tables(path)["trips.txt"])) == trips


@pytest.mark.filterwarnings("ignore:no Mobility Database API token")
def test_a_failed_rematch_keeps_every_feed_as_delivered(tmp_path, monkeypatch):
    # The matching after B's withdrawal for its modes fails once.
    calls = _counted_matching(monkeypatch, failing=2)
    two_modes = {"agency": "G", "routes": ("r1", "t"), "types": {"t": 0}}
    feeds = {"A": C, "K": TWO, "B": two_modes, "D": {"agency": "H", "hours": (10,)}}
    options = {"modes": "bus"}
    options["cache_dir"] = tmp_path / "cache"
    result, _ = _fetch_networks(tmp_path / "first", monkeypatch, feeds, **options)
    kept = "+ repeated trips kept: matching failed"
    assert {e["feed_id"]: _seen(e) for e in result.selection} == dict.fromkeys(
        feeds, kept
    )
    assert not list(options["cache_dir"].rglob("outputs/*-deduplicated.zip"))
    # Nothing was stored, so the next call matches again.
    result, _ = _fetch_networks(tmp_path / "again", monkeypatch, feeds, **options)
    assert len(calls) == 4
    assert result.selection[1]["note"] == "1 repeated trips of A left out"


@pytest.mark.filterwarnings("ignore:no Mobility Database API token")
@pytest.mark.parametrize(
    "second, fault, note, trips",
    [
        (TWO, "removed", "its cached version was removed", 2),
        (F, "unlisted", "disk full", 1),
    ],
    ids=["removed", "unlisted"],
)
def test_a_version_gone_before_storing_keeps_its_trips(
    tmp_path, monkeypatch, second, fault, note, trips
):
    from transitio.catalog._cache import FeedCache
    from transitio.pipeline import _fetch

    real = _fetch._store_found

    def gone(cache, items, *args):
        if fault == "removed":
            # A concurrent clear removes B's version once compared.
            cache.delete(items[1].version)
        else:
            # Listing B's versions fails once, as B is to be withdrawn.
            listed = cache.versions
            failed = []

            def versions(feed_id):
                if feed_id == "B" and not failed:
                    failed.append(feed_id)
                    raise OSError("disk full")
                return listed(feed_id)

            monkeypatch.setattr(cache, "versions", versions)
        return real(cache, items, *args)

    monkeypatch.setattr(_fetch, "_store_found", gone)
    cache_dir = tmp_path / "cache"
    feeds = {"A": C, "B": second}
    result, _ = _fetch_networks(tmp_path, monkeypatch, feeds, cache_dir=cache_dir)
    assert _seen(result.selection[1]) == f"+ repeated trips kept: {note}"
    assert len(_feed_tables(result.feeds[1])["trips.txt"]) == trips
    assert not list(cache_dir.rglob("*-deduplicated.zip"))
    if fault == "removed":
        assert FeedCache(cache_dir).versions("B") == []


@pytest.mark.filterwarnings("ignore:no Mobility Database API token")
def test_the_deduplicating_crop_is_reported(tmp_path, monkeypatch):
    from transitio.pipeline._fetch import _dropped_note

    # Without crop=, it is B's first crop, which leaves out a dangling row.
    dangling = {"stop_times.txt": "r19,09:20:00,09:20:00,s99,3\n"}
    feeds = {"A": C, "B": {**TWO, "extra": dangling}}
    for call in ("cold", "warm"):
        result, _ = _fetch_networks(tmp_path / call, monkeypatch, feeds)
        assert _seen(result.selection[1]) == "+ 1 repeated trips of A left out (1)"
        assert _dropped_note(result.reports[1]) == (
            "dropped 1 stop_times.txt rows whose stop_id is not in stops.txt"
        )


def test_the_matching_reads_only_the_columns_it_compares(tmp_path):
    from transitio.pipeline._fetch import _read_tables

    # " trip_id " folds into trip_id, whose blank value gives way to t1.
    trips = "route_id, trip_id ,trip_id,shape_id\nr1,,t1,s1\n"
    path = tmp_path / "feed.zip"
    path.write_bytes(_zip({"trips.txt": trips, "stops.txt": "stop_id\ns1\n"}))
    tables = _read_tables(path, {"trips.txt": {"trip_id"}})
    assert {n: t.to_dict("list") for n, t in tables.items()} == {
        "trips.txt": {"trip_id": ["t1"]}
    }


@pytest.mark.filterwarnings("ignore:no Mobility Database API token")
def test_a_stored_comparison_names_the_feeds_compared(tmp_path, monkeypatch):
    # The same archive under another id is another feed for the note to name,
    # compared again; the first id's comparison is then read back unchanged.
    calls, seen, cache_dir = _counted_matching(monkeypatch), [], tmp_path / "cache"
    for n, first in enumerate("AZA"):
        feeds, before = {first: C, "B": TWO}, len(calls)
        options = {"cache_dir": cache_dir}
        result, _ = _fetch_networks(tmp_path / str(n), monkeypatch, feeds, **options)
        seen.append((result.selection[1]["note"], len(calls) - before))
    notes = [f"1 repeated trips of {first} left out" for first in "AZA"]
    assert seen == list(zip(notes, (1, 1, 0)))


def _counted_matching(monkeypatch, failing=None):
    """The calls made from now on to the trip matching, by number; the one
    numbered ``failing`` raises."""
    import transitio.gtfs._duplicates as duplicates

    real, calls = duplicates.repeated_trips, []

    def matched(*args, **kwargs):
        calls.append(len(calls) + 1)
        if calls[-1] == failing:
            raise ValueError("matching failed")
        return real(*args, **kwargs)

    monkeypatch.setattr(duplicates, "repeated_trips", matched)
    return calls


def _timeless(reports):
    """``reports`` without the time each was generated."""
    return [
        {**r, "summary": {k: v for k, v in r["summary"].items() if k != "generatedAt"}}
        for r in reports
    ]


@pytest.mark.filterwarnings("ignore:no Mobility Database API token")
def test_a_cached_version_serves_the_study_days_it_covers(tmp_path, monkeypatch):
    from transitio.catalog import TransitlandAtlas

    monkeypatch.delenv("MOBILITY_API_REFRESH_TOKEN", raising=False)
    # The index saw the feed ended, so a download is preceded by a probe.
    index = _partitioned_index(tmp_path, monkeypatch, {"f-a": ENDED_ETAG})
    spring = _zip(_calendar("20260101", "20260501"))
    weekdays = _zip(_calendar("20260101", "20261231", "1111100"))
    sha = {body: hashlib.sha256(body).hexdigest() for body in (spring, weekdays)}
    served, requests = {}, []

    def handler(request):
        requests.append(request.method)
        if served["body"] is None:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, content=served["body"])

    class Served(TransitlandAtlas):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("transitio.catalog.TransitlandAtlas", Served)
    options = dict(place="Q1757", index=index, crop=False, osm=False)
    options["cache_dir"] = tmp_path / "cache"

    def run(when, body, **extra):
        served["body"], before = body, len(requests)
        result = fetch(when=when, **options, **extra)
        (entry,) = result.selection
        report = result.reports[0]["summary"]["provenance"]["sha256"]
        return result, [entry["cache"], report, requests[before:]]

    first, seen = run("2026-03-02", spring)
    assert seen == ["downloaded", sha[spring], ["HEAD", "GET"]]
    # Offline, the day is served from the cache with neither download nor probe.
    again, seen = run("2026-03-02", None)
    assert seen == ["reused", sha[spring], []]
    columns = ("cache", "download_errors")
    tables = [
        [{k: v for k, v in e.items() if k not in columns} for e in r.selection]
        for r in (first, again)
    ]
    assert tables[0] == tables[1]
    assert _timeless(first.reports) == _timeless(again.reports)
    # A day the version misses downloads another and keeps it.
    assert run("2026-06-01", weekdays)[1] == [
        "downloaded",
        sha[weekdays],
        ["HEAD", "GET"],
    ]
    # The first day keeps its version though the newer one serves it too; a
    # new Sunday passes over the newer one, which runs no Sunday service.
    assert run("2026-03-02", None)[1] == ["reused", sha[spring], []]
    assert run("2026-03-08", None)[1] == ["reused", sha[spring], []]
    # A refresh whose every attempt fails falls back, deleting nothing.
    with pytest.warns(UserWarning, match="f-a: refresh failed"):
        _, seen = run("2026-06-01", b"<html>maintenance</html>", use_cache=False)
    assert seen == ["fallback", sha[weekdays], ["HEAD", "GET"]]
    assert len(list((tmp_path / "cache" / "gtfs").glob("id-*/*.zip"))) == 2


@pytest.mark.filterwarnings("ignore:no Mobility Database API token")
def test_containment_from_the_cache_follows_the_proofs_of_the_snapshot(
    tmp_path, monkeypatch
):
    from transitio.catalog import TransitlandAtlas
    from transitio.catalog._cache import FeedCache
    from transitio.pipeline._fetch import _prove

    monkeypatch.delenv("MOBILITY_API_REFRESH_TOKEN", raising=False)
    columns = {"f-a": ETAG, "f-b": ETAG}
    index = _partitioned_index(tmp_path, monkeypatch, columns, {"f-a": ["f-b"]})
    payloads = {"f-a": _zip(GTFS), "f-b": _zip(HKL)}
    online, requests = [True], []

    def handler(request):
        feed_id = request.url.path.strip("/")
        requests.append((request.method, feed_id))
        if not online[0]:
            raise httpx.ConnectError("offline", request=request)
        if request.method == "HEAD":
            # The container is unchanged since indexed, f-a is not.
            return httpx.Response(304 if feed_id == "f-b" else 200)
        return httpx.Response(200, content=payloads[feed_id])

    class Served(TransitlandAtlas):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("transitio.catalog.TransitlandAtlas", Served)
    cache_dir = tmp_path / "cache"
    options = dict(place="Q1757", index=index, crop=False, osm=False)

    def decisions():
        result = fetch(cache_dir=cache_dir, **options)
        return [(e["feed_id"], e["decision"], e["note"]) for e in result.selection]

    kept = "kept: containment in f-b not proven current"
    assert decisions() == [("f-a", "delivered", kept), ("f-b", "delivered", None)]
    # f-a's version as a probe under this snapshot would have proven it.
    cache = FeedCache(cache_dir)
    (version,) = cache.versions("f-a")
    _prove(cache, version, index.snapshot_id, "https://feeds.example/f-a")
    online[0], before = False, len(requests)
    assert decisions() == [("f-a", "skipped", None), ("f-b", "delivered", None)]
    # Under another snapshot neither version is proven, so f-a stays.
    index.snapshot["snapshot_id"] = "another"
    assert decisions() == [("f-a", "delivered", kept), ("f-b", "delivered", None)]
    assert requests[before:] == []


def test_a_container_read_from_the_hosted_copy_proves_nothing(tmp_path, monkeypatch):
    from transitio.catalog._cache import FeedCache

    hosted = "https://files.example.com/f-b/latest.zip"
    columns = {"f-a": ETAG, "f-b": {**ETAG, "mdb": {"urls": {"latest": hosted}}}}
    index = _partitioned_index(tmp_path, monkeypatch, columns, {"f-a": ["f-b"]})
    payloads = {"/f-a": _zip(GTFS), "/f-b/latest.zip": _zip(HKL)}
    online = [True]

    def handler(request):
        if not online[0]:
            raise httpx.ConnectError("offline", request=request)
        if request.method == "HEAD":
            return httpx.Response(304)  # both unchanged since indexed
        if request.url.path in payloads:
            return httpx.Response(200, content=payloads[request.url.path])
        return httpx.Response(404)  # the container's producer URL

    _serve(monkeypatch, handler)
    cache_dir = tmp_path / "cache"
    options = dict(place="Q1757", index=index, crop=False, osm=False)
    first = fetch(cache_dir=cache_dir, **options)
    online[0] = False
    again = fetch(cache_dir=cache_dir, **options)
    kept = "kept: containment in f-b not proven current"
    for result in (first, again):
        notes = {e["feed_id"]: (e["decision"], e["note"]) for e in result.selection}
        assert notes["f-a"] == ("delivered", kept)
        assert notes["f-b"][1].startswith("from the Mobility Database hosted copy")
    # The probe proved the producer's archive, not the hosted copy read.
    (version,) = FeedCache(cache_dir).versions("f-b")
    assert version.index_proofs == {}
    columns = ("cache", "download_errors")
    tables = [
        [{k: v for k, v in e.items() if k not in columns} for e in r.selection]
        for r in (first, again)
    ]
    assert tables[0] == tables[1]
    assert _timeless(first.reports) == _timeless(again.reports)


@pytest.mark.filterwarnings("ignore:no Mobility Database API token")
def test_a_cached_producer_copy_is_used_only_with_credentials(tmp_path, monkeypatch):
    mdb = {"urls": {"latest": HOSTED}}
    feeds = {"f-a": {"mdb": mdb, **_protected(KEY)}}
    index = _partitioned_index(tmp_path, monkeypatch, feeds, providers=PROVIDERS)
    payload = _zip(_calendar("20260101", "20261231"))
    requests = []

    def handler(request):
        requests.append(str(request.url.copy_with(query=None)))
        return httpx.Response(200, content=payload)

    _serve(monkeypatch, handler)
    options = dict(place="Q1757", index=index, crop=False, osm=False, when=DAY)
    options["cache_dir"] = tmp_path / "cache"
    results = []
    for env in ({"P_KEY__KEY": SECRET}, {}, {"P_KEY__KEY": SECRET}):
        _credentials_env(monkeypatch, tmp_path, env)
        (entry,) = fetch(**options).selection
        results.append((entry["fetched_from"], entry["cache"]))
    # Without the key the producer copy is passed over for the hosted copy.
    assert results == [
        ("producer", "downloaded"),
        ("mdb_latest", "downloaded"),
        ("producer", "reused"),
    ]
    assert requests == [API, HOSTED]


def test_a_cached_dataset_is_reported_alike_without_the_catalogue(
    tmp_path, monkeypatch
):
    from transitio.catalog._models import Dataset

    index = _place_index(
        tmp_path, {"mdb": {"mdb_id": "mdb-9", "urls": {"latest": "u"}}}
    )
    payload = _gtfs_payload()
    _stub_pbf_and_atlas(monkeypatch, tmp_path, payload)
    newest = Dataset.from_api(
        {
            "id": "mdb-9-newest",
            "feed_id": "mdb-9",
            "hosted_url": "https://x/z.zip",
            "validation_report": {"url_json": "https://x/r.json"},
        }
    )
    hosted = {"summary": {"validatorVersion": "6.0.0"}, "notices": []}

    def dataset_download(self, dataset, directory):
        path = directory / f"{dataset.id}.zip"
        path.write_bytes(payload)
        return path

    def unreachable(*args, **kwargs):
        raise RuntimeError("catalogue unreachable")

    catalogue = "transitio.catalog.MobilityDatabase"
    monkeypatch.setattr(f"{catalogue}.datasets", lambda self, feed: [newest])
    monkeypatch.setattr(f"{catalogue}.validation_report", lambda self, d: hosted)
    monkeypatch.setattr(f"{catalogue}._fetch_dataset", dataset_download)
    options = dict(place="Q1757", index=index, crop=False, refresh_token="tok")
    first = fetch(**options)
    for name in ("datasets", "validation_report", "_fetch_dataset"):
        monkeypatch.setattr(f"{catalogue}.{name}", unreachable)
    again = fetch(**options)
    assert [e["cache"] for e in first.selection + again.selection] == [
        "downloaded",
        "reused",
    ]
    assert _timeless(first.reports) == _timeless(again.reports)
    assert again.reports[0]["summary"]["provenance"]["dataset_id"] == "mdb-9-newest"


def test_fetch_place_rejects_country_code():
    with pytest.raises(ValueError, match="country_code= applies only with aoi="):
        fetch(place="X", country_code="FI")


def _place_index(tmp_path, feed):
    import transitio.index as transitio_index
    from index_fixture import HULL, covered_feed, edge, write_index

    feeds = [{**covered_feed("f-a", coverage_source="crawl"), "coverage": HULL, **feed}]
    edges = [edge("Q1757", "f-a", tier="local")]
    return transitio_index.read_index(
        write_index(tmp_path / "index", feeds=feeds, edges=edges)
    )


def _gtfs_payload():
    return _zip(GTFS, zipfile.ZIP_STORED)


def _stub_pbf_and_atlas(monkeypatch, tmp_path, payload):
    from pathlib import Path as _Path

    def fake_download(self, feed, directory=None):
        base = _Path(directory) if directory else tmp_path
        base.mkdir(parents=True, exist_ok=True)
        path = base / "latest.zip"
        path.write_bytes(payload)
        return path

    fake_pbf = tmp_path / "aoi.osm.pbf"
    fake_pbf.write_bytes(b"\x00fake")
    monkeypatch.setattr(
        "transitio.catalog.TransitlandAtlas._fetch_static", fake_download
    )
    monkeypatch.setattr("transitio.osm.fetch_pbf", lambda *a, **k: fake_pbf)
    monkeypatch.setattr("transitio.osm._fetch.fetch_pbf", lambda *a, **k: fake_pbf)
    return fake_pbf


def test_fetch_place_when_without_token_warns(tmp_path, monkeypatch):
    monkeypatch.delenv("MOBILITY_API_REFRESH_TOKEN", raising=False)
    index = _place_index(
        tmp_path, {"atlas": {"urls": {"static_current": "https://feeds.example/a.zip"}}}
    )
    _stub_pbf_and_atlas(monkeypatch, tmp_path, _gtfs_payload())
    with pytest.warns(UserWarning, match="cannot select"):
        result = fetch(
            place="Q1757",
            index=index,
            directory=tmp_path / "out",
            crop=False,
            when="2026-06-01",
        )
    assert [e["fetched_from"] for e in result.selection] == ["producer"]


def test_fetch_place_with_token_selects_the_covering_dataset(tmp_path, monkeypatch):
    from pathlib import Path as _Path

    from transitio.catalog._models import Dataset

    index = _place_index(
        tmp_path, {"mdb": {"mdb_id": "mdb-9", "urls": {"latest": "u"}}}
    )
    payload = _gtfs_payload()
    _stub_pbf_and_atlas(monkeypatch, tmp_path, payload)
    picked = Dataset.from_api(
        {"id": "mdb-9-2026", "feed_id": "mdb-9", "hosted_url": "https://x/z.zip"}
    )
    monkeypatch.setattr(
        "transitio.catalog.MobilityDatabase.dataset_for",
        lambda self, feed, when: picked,
    )

    def fake_dataset_download(self, dataset, directory=None):
        base = _Path(directory) if directory else tmp_path
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{dataset.id}.zip"
        path.write_bytes(payload)
        return path

    monkeypatch.setattr(
        "transitio.catalog.MobilityDatabase._fetch_dataset", fake_dataset_download
    )
    result = fetch(
        place="Q1757",
        index=index,
        directory=tmp_path / "out",
        crop=False,
        when="2026-06-01",
        refresh_token="tok",
    )
    assert result.reports[0]["summary"]["provenance"]["dataset_id"] == "mdb-9-2026"


def test_fetch_place_records_index_provenance(tmp_path, monkeypatch):
    index = _place_index(
        tmp_path, {"atlas": {"urls": {"static_current": "https://feeds.example/a.zip"}}}
    )
    _stub_pbf_and_atlas(monkeypatch, tmp_path, _gtfs_payload())
    result = fetch(place="Q1757", index=index, directory=tmp_path / "out", crop=False)
    assert result.provenance["snapshot"] == index.snapshot_id
    assert (
        result.provenance["discovery_semantics_version"]
        == transitio.index.DISCOVERY_SEMANTICS_VERSION
    )
    assert result.provenance["transitio_version"]


NEW_YORK = "America/New_York"
AIRPORT_STOPS = "stop_id,stop_name,stop_lat,stop_lon\ns1,Airport,21.332,-157.920\n"
PARIS_STOPS = "stop_id,stop_name,stop_lat,stop_lon\ns1,Louvre,48.861,2.336\n"
AIRPORT_NOTE = f"agency_timezone {NEW_YORK}; stops in Pacific/Honolulu"


@pytest.mark.parametrize(
    "zone, stops, budget, expected",
    [
        (NEW_YORK, AIRPORT_STOPS, None, AIRPORT_NOTE),
        ("Pacific/Honolulu", AIRPORT_STOPS, None, None),
        ("CET", PARIS_STOPS, None, None),
        (NEW_YORK, None, None, None),
        (NEW_YORK, AIRPORT_STOPS, 10, None),
    ],
    ids=["disagrees", "agrees", "equivalent", "no-stops", "over-budget"],
)
def test_timezone_note(tmp_path, zone, stops, budget, expected):
    from transitio.pipeline._fetch import _timezone_note

    files = {"agency.txt": GTFS["agency.txt"].replace("Europe/Helsinki", zone)}
    if stops is not None:
        files["stops.txt"] = stops
    path = tmp_path / "feed.zip"
    path.write_bytes(_zip(files))
    assert _timezone_note(path, budget) == expected


@pytest.mark.parametrize(
    "hidden, expected",
    [
        pytest.param(
            [("f-u", "unknown", {"unknown"})],
            "1 feed: f-u (unknown); tiers=['local', 'regional', 'national'] fetches it",
            id="unknown-only",
        ),
        pytest.param(
            [(f"f-{n}", "tertiary", {"national", "regional"}) for n in range(7)],
            "7 feeds: f-0 (tertiary), f-1 (tertiary), f-2 (tertiary), f-3 (tertiary),"
            " f-4 (tertiary) and 2 more; tiers=['regional', 'national'] fetches them",
            id="seven",
        ),
    ],
)
def test_hidden_view_note(hidden, expected):
    from types import SimpleNamespace

    from transitio.pipeline._fetch import _hidden_note

    feeds = [
        SimpleNamespace(feed_id=feed_id, relevance_category=category, tiers=tiers)
        for feed_id, category, tiers in hidden
    ]
    note = _hidden_note(SimpleNamespace(kind="city"), feeds)
    prefix = "default view (city: primary, secondary) holds none of the place's "
    assert note == prefix + expected


@pytest.mark.parametrize("path", ["area", "place"])
def test_a_delivered_feed_notes_an_agency_timezone_its_stops_disagree_with(
    pipeline_env, monkeypatch, path
):
    tmp_path, _ = pipeline_env
    agency = GTFS["agency.txt"].replace("Europe/Helsinki", NEW_YORK)
    payload = _zip({**GTFS, "agency.txt": agency})
    if path == "area":
        # mdb-10 serves the fixture feed, in Europe/Helsinki.
        selection = _area_fetch(monkeypatch, tmp_path, payload).selection
        notes = {entry["feed_id"]: entry["note"] for entry in selection}
        assert notes.pop("mdb-10") is None
    else:
        index = _place_index(
            tmp_path, {"atlas": {"urls": {"static_current": "https://f.example/a.zip"}}}
        )
        _stub_pbf_and_atlas(monkeypatch, tmp_path, payload)
        selection = fetch(
            place="Q1757", index=index, directory=tmp_path / "out"
        ).selection
        notes = {entry["feed_id"]: entry["note"] for entry in selection}
    assert all(entry["decision"] == "delivered" for entry in selection)
    (note,) = notes.values()
    assert note.endswith(f"agency_timezone {NEW_YORK}; stops in Europe/Helsinki")


@pytest.mark.parametrize(
    "served, error",
    [(None, "dataset download boom"), (b"<html></html>", "not a zip archive")],
    ids=["raises", "no-zip"],
)
def test_fetch_place_falls_back_to_atlas_when_the_dataset_download_fails(
    tmp_path, monkeypatch, served, error
):
    from transitio.catalog._models import Dataset

    index = _place_index(
        tmp_path,
        {
            "mdb": {"mdb_id": "mdb-9", "urls": {"latest": "u"}},
            "atlas": {"urls": {"static_current": "https://feeds.example/a.zip"}},
        },
    )
    _stub_pbf_and_atlas(monkeypatch, tmp_path, _gtfs_payload())
    picked = Dataset.from_api(
        {"id": "mdb-9-2026", "feed_id": "mdb-9", "hosted_url": "https://x/z.zip"}
    )
    monkeypatch.setattr(
        "transitio.catalog.MobilityDatabase.dataset_for",
        lambda self, feed, when: picked,
    )

    def failing(self, dataset, directory=None):
        if served is None:
            raise RuntimeError("dataset download boom")
        path = directory / f"{dataset.id}.zip"
        path.write_bytes(served)
        return path

    monkeypatch.setattr("transitio.catalog.MobilityDatabase._fetch_dataset", failing)
    result = fetch(
        place="Q1757",
        index=index,
        directory=tmp_path / "out",
        crop=False,
        when="2026-06-01",
        refresh_token="tok",
    )
    # The dataset download failed, so the Atlas fallback delivered the feed.
    assert len(result.feeds) == 1
    assert result.skipped == []
    (entry,) = result.selection
    assert (entry["fetched_from"], entry["download_errors"]) == (
        "producer",
        f"mdb dataset: {error}",
    )


def test_fetch_place_skips_a_feed_whose_provenance_sidecar_is_unreadable(
    tmp_path, monkeypatch
):
    index = _place_index(
        tmp_path, {"atlas": {"urls": {"static_current": "https://feeds.example/a.zip"}}}
    )
    _stub_pbf_and_atlas(monkeypatch, tmp_path, _gtfs_payload())
    from transitio.catalog import TransitlandAtlas

    stub = TransitlandAtlas._fetch_static

    def truncated(self, feed, directory):
        path = stub(self, feed, directory)
        path.with_suffix(".provenance.json").write_text("{")
        return path

    monkeypatch.setattr(TransitlandAtlas, "_fetch_static", truncated)
    out = tmp_path / "out"
    result = fetch(place="Q1757", index=index, directory=out, crop=False, osm=False)
    ((feed_id, reason),) = result.skipped
    assert feed_id == "f-a" and reason.startswith("processing failed: ")
    assert result.feeds == []


@pytest.mark.parametrize(
    "feed_id, name",
    [
        ("f-nvbw~ding", "f-nvbw~ding"),
        # A plain id spelling another id's ASCII form keeps a name of its own.
        ("f-abc", "f-abc"),
        ("F-ABC", "f-abc+{sha}"),
        ("f-u2f-pražskáintegrovanádoprava", "f-u2f-prazskaintegrovanadoprava+{sha}"),
        ("f-あおい交通", "f+{sha}"),
        ("con", "con+{sha}"),
        # 120 characters, cut to 80 and stripped of the "-" the cut ends on.
        ("f-" + "a" * 77 + "-" + "b" * 40, "f-" + "a" * 77 + "+{sha}"),
    ],
)
def test_delivered_name(feed_id, name):
    from transitio.pipeline._fetch import _delivered_name

    sha = hashlib.sha256(feed_id.encode("utf-8")).hexdigest()
    assert _delivered_name(feed_id) == name.format(sha=sha)


def test_a_place_fetch_keeps_its_download_as_a_cached_version(tmp_path, monkeypatch):
    index = _place_index(
        tmp_path, {"atlas": {"urls": {"static_current": "https://feeds.example/a.zip"}}}
    )
    _stub_pbf_and_atlas(monkeypatch, tmp_path, _gtfs_payload())
    out, cache = tmp_path / "out", tmp_path / "cache"
    options = dict(place="Q1757", index=index, directory=out, crop=False, osm=False)
    first = fetch(cache_dir=cache, **options)
    second = fetch(cache_dir=cache, use_cache=False, **options)
    assert [e["cache"] for e in first.selection + second.selection] == [
        "downloaded",
        "refreshed",
    ]
    # Identical downloads keep one version and record each acquisition.
    (version,) = (cache / "gtfs").glob("id-*/*.zip")
    sidecar = json.loads(version.with_suffix(".provenance.json").read_text())
    sources = sidecar["cache"]["sources"]
    assert [(s["fetched_from"], s["index_snapshot"]) for s in sources] == [
        ("producer", index.snapshot_id)
    ] * 2
    assert not list(cache.rglob(".staging"))
    # The directory holds the delivered copy; reports describe the first download.
    assert [p.name for p in out.rglob("*.zip")] == ["f-a.zip"]
    origin = [r["summary"]["provenance"] for r in first.reports + second.reports]
    assert origin[0] == origin[1]
    assert origin[0]["retrieved_at"] == sources[0]["retrieved_at"]
    with pytest.raises(ValueError, match="outside the download cache"):
        fetch(cache_dir=cache, **{**options, "directory": cache / "gtfs" / "out"})
    if os.name != "nt":
        # A link at a delivered name, into the cache, is replaced.
        linked = tmp_path / "linked"
        linked.mkdir()
        (linked / "f-a.zip").symlink_to(version)
        result = fetch(cache_dir=cache, **{**options, "directory": linked})
        assert result.feeds == [linked / "f-a.zip"]
        assert not result.feeds[0].is_symlink()
        assert version.read_bytes() == _gtfs_payload()


def test_fetch_place_from_a_bound_index_uses_its_snapshot(tmp_path, monkeypatch):
    import transitio

    index = _place_index(
        tmp_path, {"atlas": {"urls": {"static_current": "https://feeds.example/a.zip"}}}
    )
    _stub_pbf_and_atlas(monkeypatch, tmp_path, _gtfs_payload())
    place_obj = transitio.place("Q1757", index=index)
    result = fetch(place=place_obj, directory=tmp_path / "out", crop=False)
    assert result.provenance["snapshot"] == index.snapshot_id
    assert len(result.feeds) == 1


def test_fetch_place_with_token_and_no_date_uses_the_newest_dataset(
    tmp_path, monkeypatch
):
    from pathlib import Path as _Path

    from transitio.catalog._models import Dataset

    index = _place_index(
        tmp_path, {"mdb": {"mdb_id": "mdb-9", "urls": {"latest": "u"}}}
    )
    payload = _gtfs_payload()
    _stub_pbf_and_atlas(monkeypatch, tmp_path, payload)
    newest = Dataset.from_api(
        {"id": "mdb-9-newest", "feed_id": "mdb-9", "hosted_url": "https://x/z.zip"}
    )
    monkeypatch.setattr(
        "transitio.catalog.MobilityDatabase.datasets", lambda self, feed: [newest]
    )
    monkeypatch.setattr(
        "transitio.catalog.MobilityDatabase.validation_report",
        lambda self, dataset: {"summary": {"validatorVersion": "6.0.0"}, "notices": []},
    )

    def dataset_download(self, dataset, directory=None):
        base = _Path(directory) if directory else tmp_path
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{dataset.id}.zip"
        path.write_bytes(payload)
        return path

    monkeypatch.setattr(
        "transitio.catalog.MobilityDatabase._fetch_dataset", dataset_download
    )
    result = fetch(
        place="Q1757",
        index=index,
        directory=tmp_path / "out",
        crop=False,
        refresh_token="tok",
    )
    assert result.reports[0]["summary"]["provenance"]["dataset_id"] == "mdb-9-newest"


def test_fetch_place_dataset_selection_failure_falls_back(tmp_path, monkeypatch):
    index = _place_index(
        tmp_path,
        {
            "mdb": {"mdb_id": "mdb-9", "urls": {"latest": "u"}},
            "atlas": {"urls": {"static_current": "https://feeds.example/a.zip"}},
        },
    )
    _stub_pbf_and_atlas(monkeypatch, tmp_path, _gtfs_payload())

    def boom(self, feed):
        raise RuntimeError("catalogue down")

    monkeypatch.setattr("transitio.catalog.MobilityDatabase.datasets", boom)
    result = fetch(
        place="Q1757",
        index=index,
        directory=tmp_path / "out",
        crop=False,
        refresh_token="tok",
    )
    # The catalogue lookup failed, but the Atlas url still delivered the feed.
    assert len(result.feeds) == 1
    assert result.skipped == []


def test_fetch_place_output_names_differ_by_geometry(tmp_path, monkeypatch):
    # The same place id with different geometry must not share a stored
    # output, or one fetch would read back the other's differently-cropped feed.
    import shapely

    import transitio

    index = _place_index(
        tmp_path, {"atlas": {"urls": {"static_current": "https://feeds.example/a.zip"}}}
    )
    _stub_pbf_and_atlas(monkeypatch, tmp_path, _gtfs_payload())
    place_obj = transitio.place("Q1757", index=index)
    first = fetch(place=place_obj)
    # Still around the fixture's stops, so the crop keeps its trip.
    place_obj._record["geometry"] = shapely.box(24.92, 60.16, 24.95, 60.18)
    second = fetch(place=place_obj)
    assert first.feeds[0] != second.feeds[0]


# A two-part place: the first part holds the GTFS fixture's stops.
_SERVED = (24.9, 60.1, 25.1, 60.3)
_REMOTE = (26.0, 61.0, 26.2, 61.2)


@pytest.mark.parametrize(
    "stops, expected, inside",
    [
        pytest.param([[(60.169, 24.931)]], "served", [(24.931, 60.169)], id="one-part"),
        pytest.param(
            [[(60.169, 24.931)], [(61.1, 26.1), (60.169, 24.931)]],
            "whole",
            [(24.931, 60.169), (26.1, 61.1)],
            id="both-parts",
        ),
        pytest.param([[(59.0, 24.0)]], "whole", None, id="no-part"),
        pytest.param([], "whole", None, id="nothing-delivered"),
        # A feed whose stops cannot be read could serve the remote part.
        pytest.param([[(60.169, 24.931)], None], "whole", None, id="unreadable-stops"),
    ],
)
def test_osm_parts_are_those_holding_a_delivered_stop(
    tmp_path, stops, expected, inside
):
    import shapely

    from transitio.osm._fetch import _buffered
    from transitio.pipeline._fetch import _osm_parts, _osm_stops

    geometry = shapely.union_all([shapely.box(*_SERVED), shapely.box(*_REMOTE)])
    feeds = []
    for number, coords in enumerate(stops):
        path = tmp_path / f"{number}.zip"
        if coords is None:
            path.write_bytes(_zip({"agency.txt": GTFS["agency.txt"]}))
        else:
            rows = "".join(f"s{i},{y},{x}\n" for i, (y, x) in enumerate(coords))
            # A row without coordinates is not a located stop.
            rows += "s-blank,,\n"
            path.write_bytes(_zip({"stops.txt": "stop_id,stop_lat,stop_lon\n" + rows}))
        feeds.append(path)
    parts, located = _osm_parts(geometry, feeds)
    assert parts.equals(shapely.box(*_SERVED) if expected == "served" else geometry)
    assert [None if located[p] is None else located[p].tolist() for p in feeds] == [
        None if coords is None else [[x, y] for y, x in coords] for coords in stops
    ]
    # What the extract must cover: the located stops inside the grown parts,
    # each once.
    must_cover = _osm_stops(_buffered(parts, 1600), located)
    assert must_cover == (None if inside is None else shapely.multipoints(inside))


_PARTS_NOTE = "OSM area: 1 of 2 parts (247 of 488 km²)"


@pytest.mark.parametrize(
    "served, counts, expected",
    [
        pytest.param(True, (0, 2, 0), _PARTS_NOTE, id="parts"),
        pytest.param(
            False,
            (12, 40, 0),
            "OSM area: 12 of 40 located stops outside it",
            id="stops",
        ),
        pytest.param(
            True,
            (1, 3, 0),
            f"{_PARTS_NOTE}; 1 of 3 located stops outside it",
            id="parts-and-stops",
        ),
        pytest.param(
            False,
            (0, 40, 1),
            "OSM area: 0 of 40 located stops outside it (stops.txt of 1 feed not read)",
            id="unread",
        ),
        pytest.param(False, (0, 40, 0), None, id="neither"),
    ],
)
def test_osm_note_names_the_parts_and_stops_outside_the_area(served, counts, expected):
    import shapely

    from transitio.pipeline._fetch import _osm_note

    geometry = shapely.union_all([shapely.box(*_SERVED), shapely.box(*_REMOTE)])
    parts = shapely.box(*_SERVED) if served else geometry
    assert _osm_note(geometry, parts, *counts) == expected


def test_fetch_place_fetches_the_osm_extract_last_for_the_served_parts(
    tmp_path, monkeypatch
):
    import shapely

    import transitio
    from transitio.osm._fetch import _buffered

    index = _place_index(
        tmp_path, {"atlas": {"urls": {"static_current": "https://feeds.example/a.zip"}}}
    )
    fake_pbf = _stub_pbf_and_atlas(monkeypatch, tmp_path, _gtfs_payload())
    download = transitio.catalog.TransitlandAtlas._fetch_static
    events = []

    def recorded_download(self, feed, directory=None):
        events.append("feed")
        return download(self, feed, directory=directory)

    def recorded_fetch_pbf(aoi, **kwargs):
        events.append((aoi, kwargs["buffer_m"], kwargs["must_cover"]))
        return fake_pbf

    monkeypatch.setattr(
        "transitio.catalog.TransitlandAtlas._fetch_static", recorded_download
    )
    monkeypatch.setattr("transitio.osm.fetch_pbf", recorded_fetch_pbf)
    served = shapely.box(*_SERVED)
    place_obj = transitio.place("Q1757", index=index)
    place_obj._record["geometry"] = shapely.union_all([served, shapely.box(*_REMOTE)])
    result = fetch(place=place_obj, directory=tmp_path / "out", crop=False)

    feed, (aoi, buffer_m, must_cover) = events
    assert feed == "feed" and aoi.equals(served) and buffer_m == 1600
    assert must_cover == shapely.multipoints([(24.931, 60.169), (24.941, 60.171)])
    assert result.osm_pbf == fake_pbf
    assert result.osm_area.equals(_buffered(served, 1600))
    (entry,) = result.selection
    assert entry["decision"] == "delivered"
    assert result.osm_note.startswith("OSM area: 1 of 2 parts (")


# route -> (agency, stop, trip, route_type); a1 carries local+regional.
_ROUTE_SPECS = (
    ("r-local", "a1", "s-local", "t1", 3),
    ("r-reg", "a1", "s-reg", "t2", 2),
    ("r-nat", "a2", "s-nat", "t3", 2),
    ("r-unknown", "a2", "s-unknown", "t4", 3),
)


def _multi_route_gtfs(routes=_ROUTE_SPECS):
    # One route per agency/stop/trip so the crop cascade is observable per route.
    members = {
        "agency.txt": (
            "agency_id,agency_name,agency_url,agency_timezone\n"
            "a1,A1,https://a1,Europe/Helsinki\na2,A2,https://a2,Europe/Helsinki\n"
        ),
        # A shared hub as each trip's second stop makes every trip usable.
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\nhub,Hub,60.17,24.94\n"
        + "".join(f"{s},{s},60.18,24.95\n" for _, _, s, _, _ in routes),
        "routes.txt": "route_id,agency_id,route_short_name,route_type\n"
        + "".join(f"{r},{a},{r},{rt}\n" for r, a, _, _, rt in routes),
        # Per-route service and shape so calendar, calendar_dates and shapes
        # cascade with the routes the selector removes.
        "trips.txt": "route_id,service_id,trip_id,shape_id\n"
        + "".join(f"{r},wk-{r},{tr},sh-{r}\n" for r, _, _, tr, _ in routes),
        "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        + "".join(
            f"{tr},08:0{i}:00,08:0{i}:00,{s},1\n{tr},08:1{i}:00,08:1{i}:00,hub,2\n"
            for i, (_, _, s, tr, _) in enumerate(routes)
        ),
        "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,"
        "saturday,sunday,start_date,end_date\n"
        + "".join(f"wk-{r},1,1,1,1,1,0,0,20260101,20261231\n" for r, *_ in routes),
        "calendar_dates.txt": "service_id,date,exception_type\n"
        + "".join(f"wk-{r},20260102,2\n" for r, *_ in routes),
        "shapes.txt": "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n"
        + "".join(f"sh-{r},60.18,24.95,1\nsh-{r},60.17,24.94,2\n" for r, *_ in routes),
    }
    import io as _io

    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, c in members.items():
            z.writestr(n, c)
    return buf.getvalue()


def _with_invalid_wheelchair(payload):
    # An invalid wheelchair_accessible on t1: a repairable error inside the
    # retained area, so a repair after the crop is observable.
    import io as _io

    with zipfile.ZipFile(_io.BytesIO(payload)) as z:
        members = {n: z.read(n).decode() for n in z.namelist()}
    lines = members["trips.txt"].splitlines()
    lines[0] += ",wheelchair_accessible"
    lines[1:] = [f"{line},{'9' if ',t1,' in line else '0'}" for line in lines[1:]]
    members["trips.txt"] = "\n".join(lines) + "\n"
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, c in members.items():
            z.writestr(n, c)
    return buf.getvalue()


def _stamp_fingerprint(edges, payload, kind="route_stops"):
    # Stamp the real fingerprint of the stubbed download onto complete-selector
    # edges so fetch-time validation trusts them (build and download agree).
    import io as _io

    from transitio.index import fingerprint

    digest = fingerprint.from_feed(_io.BytesIO(payload), kind)[0]
    for edge in edges:
        if edge.get("selector_state") == "complete":
            edge["fingerprint_kind"] = kind
            edge["classification_fingerprint"] = digest
    return edges


def _feed_tables(path):
    import csv as _csv

    out = {}
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            out[name] = list(_csv.DictReader(z.read(name).decode().splitlines()))
    return out


def _selector_index(tmp_path, edges):
    import transitio.index as transitio_index
    from index_fixture import HULL, covered_feed, write_index

    feed = {
        **covered_feed("f-a", coverage_source="crawl"),
        "coverage": HULL,
        "atlas": {"urls": {"static_current": "https://feeds.example/a.zip"}},
    }
    return transitio_index.read_index(
        write_index(tmp_path / "index", feeds=[feed], edges=edges)
    )


@pytest.mark.parametrize("repair", [False, True])
def test_fetch_place_crops_bundles_to_the_selected_routes(
    tmp_path, monkeypatch, repair
):
    from index_fixture import edge as _edge

    service = {"stops": 1, "routes": 1, "departures_per_day": 1.0}

    def sel_edge(tier, route):
        return {
            **_edge("Q1757", "f-a", tier=tier, service=service),
            "selector_state": "complete",
            "selector": {"route_id": [route]},
            "needs_review": False,
        }

    payload = _with_invalid_wheelchair(_multi_route_gtfs())
    edges = _stamp_fingerprint(
        [
            sel_edge("local", "r-local"),
            sel_edge("regional", "r-reg"),
            sel_edge("national", "r-nat"),
        ],
        payload,
    )
    index = _selector_index(tmp_path, edges)
    _stub_pbf_and_atlas(monkeypatch, tmp_path, payload)

    # repair=True rewrites the cropped feed; the selector is validated against
    # the download, which is what the crop runs on.
    repaired_inputs = []
    if repair:
        import transitio.repair as repair_module

        real_repair = repair_module.repair_feed

        def recording_repair(path, output, **options):
            repaired_inputs.append(str(path))
            return real_repair(path, output, **options)

        monkeypatch.setattr(repair_module, "repair_feed", recording_repair)
    options = dict(place="Q1757", index=index, directory=tmp_path / "out")
    options.update(crop=False, repair=repair, tiers=["local", "regional"])
    result = fetch(exclude=["national"], **options)
    # A repeat reads back what was made, the repair's fixes included.
    again = fetch(exclude=["national"], **options)
    assert len(repaired_inputs) == repair and again.repairs == result.repairs
    assert _timeless(again.reports) == _timeless(result.reports)
    # the repair, when asked for, receives the cropped feed, kept read-only
    assert bool(repaired_inputs) == repair
    assert all("-cropped" in path for path in repaired_inputs)
    if os.name != "nt":
        assert not any(os.access(path, os.W_OK) for path in repaired_inputs)
    # Only the delivered feed reaches the directory, not the crop it repaired.
    assert list((tmp_path / "out").rglob("*.zip")) == result.feeds
    # The delivered feed keeps only the selected tiers' routes, and the crop
    # cascade drops the entities only the removed routes referenced.
    tables = _feed_tables(result.feeds[0])
    assert {r["route_id"] for r in tables["routes.txt"]} == {"r-local", "r-reg"}
    assert {t_["trip_id"] for t_ in tables["trips.txt"]} == {"t1", "t2"}
    assert {s["stop_id"] for s in tables["stops.txt"]} == {"s-local", "s-reg", "hub"}
    # a2 served only r-nat/r-unknown, so it cascades away.
    assert {a["agency_id"] for a in tables["agency.txt"]} == {"a1"}
    # Shapes and services (calendar and calendar_dates) cascade with the routes.
    assert {sh["shape_id"] for sh in tables["shapes.txt"]} == {"sh-r-local", "sh-r-reg"}
    assert {c["service_id"] for c in tables["calendar.txt"]} == {
        "wk-r-local",
        "wk-r-reg",
    }
    assert {d["service_id"] for d in tables["calendar_dates.txt"]} == {
        "wk-r-local",
        "wk-r-reg",
    }
    # The delivered feed is referentially consistent (no validation errors);
    # the planted enum value is a warning, reported only when left unrepaired.
    assert result.reports[0]["summary"]["counts"]["errors"] == 0
    codes = {group["code"] for group in result.reports[0]["notices"]}
    assert ("unexpected_enum_value" in codes) == (not repair)
    (selection,) = result.selections
    assert selection["feed_id"] == "f-a"
    assert result.selection[0]["note"] == "cut to 2 of 4 routes"
    assert selection["selector_state"] == "complete"
    assert selection["trusted"] is True and selection["reason"] is None
    assert selection["kept"] == ["r-local", "r-reg"]
    assert selection["dropped"] == ["r-nat", "r-unknown"]
    # The audit names the contributing per-tier edges.
    by_tier = {e["tier"]: e["route_ids"] for e in selection["selected_by"]}
    assert by_tier == {"local": ["r-local"], "regional": ["r-reg"]}
    # the repair fixes the retained trip's invalid value after the crop; a
    # crop alone leaves attributes untouched
    wheelchair = {
        t_["trip_id"]: t_["wheelchair_accessible"] for t_ in tables["trips.txt"]
    }
    if repair:
        assert wheelchair["t1"] == "0"
        assert any(f["field"] == "wheelchair_accessible" for f in result.repairs[0])
    else:
        assert wheelchair["t1"] == "9"
        assert result.repairs == [[]]


def test_fetch_place_output_names_differ_by_selected_routes(tmp_path, monkeypatch):
    from index_fixture import edge as _edge

    service = {"stops": 1, "routes": 1, "departures_per_day": 1.0}

    def sel_edge(tier, route):
        return {
            **_edge("Q1757", "f-a", tier=tier, service=service),
            "selector_state": "complete",
            "selector": {"route_id": [route]},
            "needs_review": False,
        }

    payload = _multi_route_gtfs()
    index = _selector_index(
        tmp_path,
        _stamp_fingerprint(
            [
                sel_edge("local", "r-local"),
                sel_edge("regional", "r-reg"),
                sel_edge("national", "r-nat"),
            ],
            payload,
        ),
    )
    _stub_pbf_and_atlas(monkeypatch, tmp_path, payload)
    a = fetch(place="Q1757", index=index, crop=False, tiers=["local"])
    b = fetch(place="Q1757", index=index, crop=False, tiers=["regional"])
    # Different tier selections must not share a stored cropped feed.
    assert a.feeds[0] != b.feeds[0]


def test_fetch_place_on_unknown_governs_bundle_routes(tmp_path, monkeypatch):
    from index_fixture import edge as _edge

    service = {"stops": 1, "routes": 1, "departures_per_day": 1.0}
    specs = (
        ("r-local", "a1", "s-local", "t1", 3),
        ("r-reg", "a1", "s-reg", "t2", 2),
        ("r-unknown", "a2", "s-unknown", "t3", 3),
    )
    payload = _multi_route_gtfs(specs)
    edges = _stamp_fingerprint(
        [
            {
                **_edge("Q1757", "f-a", tier="local", service=service),
                "selector_state": "complete",
                "selector": {"route_id": ["r-local"]},
                "needs_review": False,
            },
            {
                **_edge("Q1757", "f-a", tier="regional", service=service),
                "selector_state": "complete",
                "selector": {"route_id": ["r-reg"]},
                "needs_review": False,
            },
            # A complete unknown-tier edge: its route joins the selector union
            # under include and is dropped from it under exclude.
            {
                **_edge("Q1757", "f-a", tier="unknown", service=service),
                "selector_state": "complete",
                "selector": {"route_id": ["r-unknown"]},
                "needs_review": False,
            },
        ],
        payload,
    )
    index = _selector_index(tmp_path, edges)

    def run(on_unknown):
        _stub_pbf_and_atlas(monkeypatch, tmp_path, payload)
        return fetch(
            place="Q1757",
            index=index,
            directory=tmp_path / f"out-{on_unknown}",
            crop=False,
            tiers=["local", "regional"],
            on_unknown=on_unknown,
        )

    # include: the unknown route joins the trusted union, so the bundle keeps it.
    kept_in = {
        r["route_id"] for r in _feed_tables(run("include").feeds[0])["routes.txt"]
    }
    assert kept_in == {"r-local", "r-reg", "r-unknown"}
    # exclude: the unknown edge leaves the union, so only its route is cropped
    # out while the bundle stays.
    dropped_out = run("exclude")
    kept_out = {r["route_id"] for r in _feed_tables(dropped_out.feeds[0])["routes.txt"]}
    assert kept_out == {"r-local", "r-reg"}
    assert dropped_out.selections[0]["dropped"] == ["r-unknown"]


def test_fetch_place_stale_selector_follows_on_untrusted_selector(
    tmp_path, monkeypatch
):
    from index_fixture import edge as _edge

    service = {"stops": 1, "routes": 1, "departures_per_day": 1.0}

    def stale_edge(tier, route):
        # A route_stops fingerprint that never matches the download: the
        # selector is derived, but the feed it describes has since changed.
        return {
            **_edge("Q1757", "f-a", tier=tier, service=service),
            "selector_state": "complete",
            "selector": {"route_id": [route]},
            "fingerprint_kind": "route_stops",
            "classification_fingerprint": "0" * 64,
            "needs_review": False,
        }

    index = _selector_index(
        tmp_path,
        [stale_edge("local", "r-local"), stale_edge("national", "r-nat")],
    )
    payload = _multi_route_gtfs()

    # auto + tiers only: an untrustworthy selector is never silently filtered,
    # so the whole feed is delivered and the outcome is recorded.
    _stub_pbf_and_atlas(monkeypatch, tmp_path, payload)
    whole = fetch(
        place="Q1757",
        index=index,
        directory=tmp_path / "w",
        crop=False,
        tiers=["local"],
    )
    assert {r["route_id"] for r in _feed_tables(whole.feeds[0])["routes.txt"]} == {
        "r-local",
        "r-reg",
        "r-nat",
        "r-unknown",
    }
    (sel,) = whole.selections
    assert sel["trusted"] is False and sel["reason"] == "stale" and sel["kept"] is None
    assert whole.selection[0]["note"] == "delivered whole: selector out of date"

    # auto + exclude: the exclusion is a hard constraint, so the feed is skipped.
    _stub_pbf_and_atlas(monkeypatch, tmp_path, payload)
    excl = fetch(
        place="Q1757",
        index=index,
        directory=tmp_path / "e",
        crop=False,
        tiers=["local"],
        exclude=["national"],
    )
    assert excl.feeds == []
    assert any("untrustworthy selector" in reason for _, reason in excl.skipped)

    # error policy: an untrustworthy selector raises rather than guessing.
    _stub_pbf_and_atlas(monkeypatch, tmp_path, payload)
    with pytest.raises(StaleSelectorError):
        fetch(
            place="Q1757",
            index=index,
            directory=tmp_path / "x",
            crop=False,
            tiers=["local"],
            on_untrusted_selector="error",
        )

    # Offline, with only the stale cached version, the policy decides on it.
    def offline(self, feed, directory):
        raise RuntimeError("offline")

    monkeypatch.setattr("transitio.catalog.TransitlandAtlas._fetch_static", offline)
    options = dict(place="Q1757", index=index, crop=False, tiers=["local"])
    cached = fetch(**options)
    assert [e["cache"] for e in cached.selection] == ["reused"] and cached.feeds
    assert fetch(exclude=["national"], **options).feeds == []
    with pytest.raises(StaleSelectorError):
        fetch(on_untrusted_selector="error", **options)
    with pytest.warns(UserWarning, match="f-a: refresh failed"):
        refreshed = fetch(use_cache=False, **options)
    assert [e["cache"] for e in refreshed.selection] == ["fallback"]


def test_a_cached_version_lacking_a_selected_route_is_passed_over(
    tmp_path, monkeypatch
):
    import itertools

    from index_fixture import edge as _edge
    from transitio.catalog import _cache
    from transitio.pipeline import _fetch

    service = {"stops": 1, "routes": 1, "departures_per_day": 1.0}
    selected = {
        **_edge("Q1757", "f-a", tier="local", service=service),
        "selector_state": "complete",
        "selector": {"route_id": ["r-local"]},
        "needs_review": False,
    }
    index = _selector_index(tmp_path, [selected])

    def present(path, feed, sel):
        # Fingerprints match; the routes are those the version carries.
        return True, None, {r["route_id"] for r in _feed_tables(path)["routes.txt"]}

    def offline(self, feed, directory):
        raise RuntimeError("offline")

    clock = itertools.count()
    monkeypatch.setattr(_cache, "_now", lambda: f"2026-10-05T00:00:{next(clock):02d}")
    monkeypatch.setattr(_fetch, "_selector_trusted", present)
    monkeypatch.setattr("transitio.catalog.TransitlandAtlas._fetch_static", offline)
    carrying, lacking = _multi_route_gtfs(), _multi_route_gtfs(_ROUTE_SPECS[1:])
    cache = _cache.FeedCache(tmp_path / "cache")
    for payload in (carrying, lacking):  # the version lacking the route is newer
        with cache.lock("f-a"), cache.staging("f-a") as staging:
            (staging / "feed.zip").write_bytes(payload)
            _fetch._add_version(
                cache, "f-a", staging / "feed.zip", "u", "producer", None
            )
    options = dict(place="Q1757", index=index, crop=False, osm=False, tiers=["local"])
    result = fetch(cache_dir=tmp_path / "cache", **options)
    (entry,) = result.selection
    assert (entry["cache"], entry["note"]) == ("reused", "cut to 1 of 4 routes")
    origin = result.reports[0]["summary"]["provenance"]
    assert origin["sha256"] == hashlib.sha256(carrying).hexdigest()


@pytest.mark.parametrize(
    ("policy", "exclude", "on_unknown", "expected"),
    [
        ("auto", None, "include", "whole"),
        ("auto", [], "include", "whole"),  # empty exclude is not a constraint
        ("auto", (), "exclude", "skip"),  # on_unknown decides
        ("auto", ["national"], "include", "skip"),
        ("auto", None, "exclude", "skip"),
        ("whole", ["national"], "include", "whole"),
        ("drop", None, "include", "skip"),
        ("error", None, "include", "error"),
    ],
)
def test_untrusted_action_maps_the_policy(policy, exclude, on_unknown, expected):
    from transitio.pipeline._fetch import _untrusted_action

    assert _untrusted_action(policy, exclude, on_unknown) == expected


def test_fetch_place_excludes_an_unknown_only_feed(tmp_path, monkeypatch):
    from index_fixture import edge as _edge

    service = {"stops": 1, "routes": 1, "departures_per_day": 1.0}
    # A feed whose only matching edge is unknown-tier moves to skipped, not
    # delivered, under on_unknown="exclude".
    index = _selector_index(
        tmp_path, [_edge("Q1757", "f-a", tier="unknown", service=service)]
    )
    _stub_pbf_and_atlas(monkeypatch, tmp_path, _multi_route_gtfs())
    result = fetch(
        place="Q1757",
        index=index,
        directory=tmp_path / "out",
        crop=False,
        on_unknown="exclude",
    )
    assert result.feeds == []
    assert result.skipped == [("f-a", "only unknown-tier edges")]


_EXTRACT_FAILURE = "https://download.example/extract.osm.pbf: HTTP 404 Not Found"


@pytest.mark.parametrize(
    "osm, note",
    [(False, None), (True, f"OSM extract not fetched: {_EXTRACT_FAILURE}")],
    ids=["osm-off", "download-failed"],
)
def test_fetch_aoi_without_an_extract_keeps_the_feeds(
    pipeline_env, monkeypatch, osm, note
):
    tmp_path, _ = pipeline_env

    def unavailable(*a, **k):  # osm=False must not reach the OSM stage
        assert osm, "fetch_pbf called despite osm=False"
        raise DownloadError(_EXTRACT_FAILURE)

    monkeypatch.setattr("transitio.osm._fetch.fetch_pbf", unavailable)
    monkeypatch.setattr("transitio.osm.fetch_pbf", unavailable)
    with pytest.warns(UserWarning) as caught:
        result = fetch(
            (24.6, 60.1, 25.2, 60.4),
            directory=tmp_path,
            osm=osm,
            reference_date="20260601",
        )
    assert (result.osm_pbf, result.osm_area) == (None, None)
    assert len(result.feeds) == 1  # the GTFS side is unaffected
    assert (len(result.selection), result.osm_note) == (1, note)
    warned = [str(w.message) for w in caught if "OSM" in str(w.message)]
    assert warned == ([] if note is None else [f"{note}; osm_pbf is None"])


def test_fetch_place_without_osm_skips_the_extract(tmp_path, monkeypatch):
    import io as _io

    import transitio.index as transitio_index
    from index_fixture import HULL, covered_feed, edge, write_index

    feeds = [
        {
            **covered_feed("f-a", coverage_source="crawl"),
            "coverage": HULL,
            "atlas": {"urls": {"static_current": "https://feeds.example/a.zip"}},
        }
    ]
    edges = [edge("Q1757", "f-a", tier="local")]
    index = transitio_index.read_index(
        write_index(tmp_path / "index", feeds=feeds, edges=edges)
    )
    buffer = _io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in GTFS.items():
            archive.writestr(name, content)
    payload = buffer.getvalue()

    def fake_download(self, feed, directory=None):
        base = __import__("pathlib").Path(directory) if directory else tmp_path
        base.mkdir(parents=True, exist_ok=True)
        path = base / "latest.zip"
        path.write_bytes(payload)
        return path

    def forbidden(*a, **k):
        raise AssertionError("fetch_pbf called despite osm=False")

    monkeypatch.setattr(
        "transitio.catalog.TransitlandAtlas._fetch_static", fake_download
    )
    monkeypatch.setattr("transitio.osm.fetch_pbf", forbidden)
    monkeypatch.setattr("transitio.osm._fetch.fetch_pbf", forbidden)

    result = fetch(
        place="Q1757",
        index=index,
        directory=tmp_path / "out",
        crop=False,
        osm=False,
        reference_date="20260601",
    )
    assert result.osm_pbf is None
    assert len(result.feeds) == 1


def test_to_pyrosm_without_an_extract_is_refused(tmp_path):
    from transitio.pipeline import FetchResult

    result = FetchResult(osm_pbf=None, feeds=[], reports=[], repairs=[], skipped=[])
    with pytest.raises(ValueError, match="no OSM extract"):
        result.to_pyrosm()


def test_to_cafein_without_an_extract_builds_without_a_network(tmp_path, monkeypatch):
    import sys
    import types

    from transitio.pipeline import FetchResult

    calls = {}

    class FakeNetwork:
        @staticmethod
        def from_gtfs(paths, **options):
            calls["options"] = options
            return "network"

    fake = types.SimpleNamespace(TransportNetwork=FakeNetwork)
    monkeypatch.setitem(sys.modules, "cafein", fake)
    feed = tmp_path / "feed.zip"
    feed.write_bytes(b"PK")
    result = FetchResult(
        osm_pbf=None, feeds=[feed], reports=[{}], repairs=[[]], skipped=[]
    )
    assert result.to_cafein() == "network"
    assert "osm_pbf" not in calls["options"]  # no extract, no walking network
