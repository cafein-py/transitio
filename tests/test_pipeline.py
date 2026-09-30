import datetime
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
    assert len(result.feeds) == 1
    assert "-cropped-" in result.feeds[0].name
    assert result.feeds[0].suffix == ".zip"
    (report,) = result.reports
    assert report["summary"]["counts"]["errors"] == 0
    assert result.skipped == []
    assert result.repairs == [[]]
    pbf, feeds = result
    assert pbf == fake_pbf and feeds == result.feeds


def _zip(tables, compression=zipfile.ZIP_DEFLATED):
    import io as _io

    buffer = _io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
        for name, content in tables.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _area_fetch(monkeypatch, tmp_path, second):
    """An area fetch over two hosted feeds, mdb-10 serving ``GTFS`` and mdb-11
    the zip bytes ``second``."""
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
        kwargs["cache_dir"] = tmp_path / "cache"
        return MobilityDatabase(refresh_token, **kwargs)

    monkeypatch.setattr("transitio.catalog.MobilityDatabase", patched)
    with pytest.warns(UserWarning):
        return fetch(
            (24.6, 60.1, 25.2, 60.4),
            directory=tmp_path / "out",
            reference_date="20260601",
        )


def test_an_area_fetch_keeps_each_feeds_download_apart(pipeline_env, monkeypatch):
    tmp_path, _ = pipeline_env
    other = {**GTFS, "agency.txt": GTFS["agency.txt"].replace("HSL", "HKL")}
    result = _area_fetch(monkeypatch, tmp_path, _zip(other))
    assert len(set(result.feeds)) == 2
    agencies = {zipfile.ZipFile(p).read("agency.txt") for p in result.feeds}
    assert len(agencies) == 2


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
    from transitio.pipeline._fetch import _MODE_TYPES

    assert 300 in _MODE_TYPES["rail"]
    assert 100 in _MODE_TYPES["rail"]
    assert {400, 500, 600, 12} <= _MODE_TYPES["subway"]
    assert {200, 700, 800, 11} <= _MODE_TYPES["bus"]
    assert {900, 906, 5} <= _MODE_TYPES["tram"]
    assert {1000, 1200} <= _MODE_TYPES["ferry"]


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
        download_latest=lambda proxy, directory: serve(
            proxy.latest_dataset_url, directory
        )
    )
    atlas = SimpleNamespace(
        download=lambda record, directory: serve(record.static_url, directory)
    )
    if source is None:
        with pytest.raises(DownloadError) as caught:
            _download_indexed(feed, db, atlas, tmp_path, None)
        expected = "; ".join(failures) or "feed f-a has no downloadable url"
        assert str(caught.value) == expected
    else:
        path, fetched_from, seen = _download_indexed(feed, db, atlas, tmp_path, None)
        assert (zipfile.is_zipfile(path), fetched_from, seen) == (
            True,
            source,
            failures,
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
    monkeypatch.setattr("transitio.catalog.TransitlandAtlas.download", fake_download)
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
    assert [p.name for p in result.feeds] == ["latest.zip"]
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
    ):
        with pytest.raises(ValueError, match="apply only with place="):
            fetch((0, 0, 1, 1), **kwargs)
    with pytest.raises(ValueError, match="'keep' or 'drop'"):
        fetch(place="X", contained="maybe")
    with pytest.raises(ValueError, match="'skip' or 'keep'"):
        fetch(place="X", expired="maybe")
    with pytest.raises(ValueError, match="disagree"):
        fetch(place="X", when="2026-06-01", reference_date="20260602")


def _partitioned_index(tmp_path, monkeypatch, feeds, contained=None, edges=None):
    """A schema-10 index serving Q1757 with ``feeds`` (``{feed id: extra
    columns}``) in that order, each a local feed at
    ``https://feeds.example/<feed id>``; ``edges`` adds edge fields by feed id."""
    import transitio.index as transitio_index
    from index_fixture import HULL, PLACES, covered_feed, edge, write_partitioned_index

    monkeypatch.setattr(
        "transitio.__version__", transitio_index.MIN_READER_VERSIONS[10]
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
    )
    return transitio_index.read_index(directory)


def test_a_kept_contained_feed_is_reported(tmp_path, monkeypatch):
    import pathlib

    ids = ("f-a", "f-b")
    index = _partitioned_index(
        tmp_path, monkeypatch, dict.fromkeys(ids, {}), contained={"f-a": ["f-b"]}
    )
    other = {**GTFS, "agency.txt": GTFS["agency.txt"].replace("HSL", "HKL")}
    payloads = {"f-a": _zip(GTFS), "f-b": _zip(other)}
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
    monkeypatch.setattr("transitio.catalog.TransitlandAtlas.download", fake_download)
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
    assert sorted(fetched) == list(ids) and len(result.feeds) == 2
    assert result.contained == {"f-a": ["f-b"]}
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


def _network(agency="HSL", start="20260101", stops=None, hours=(8,), **options):
    """GTFS of ``agency`` whose ``routes`` each run a trip from s2 to s3 at
    each of ``hours``, daily from ``start`` to ``end`` (through 2026 unless
    given), among stops s<i> (s0 to s9 unless ``stops`` names them); with a
    ``headway``, each trip repeats that often for an hour."""
    stops = range(10) if stops is None else stops
    trips = [(r, h) for r in options.get("routes", ("r1",)) for h in hours]
    times = (
        "{0}{1},{1:02}:00:00,{1:02}:00:00,s2,1\n{0}{1},{1:02}:10:00,{1:02}:10:00,s3,2"
    )
    tables = {
        **_calendar(start, options.get("end", "20261231")),
        "agency.txt": GTFS["agency.txt"].replace("HSL", agency),
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\n"
        + "".join(f"s{i},S{i},{60 + i / 100:.2f},24.9\n" for i in stops),
        "routes.txt": "route_id,agency_id,route_short_name,route_type\n"
        + "".join(f"{r},hsl,{r},3\n" for r in options.get("routes", ("r1",))),
        "trips.txt": "route_id,service_id,trip_id\n"
        + "".join(f"{r},wk,{r}{h}\n" for r, h in trips),
        "stop_times.txt": GTFS["stop_times.txt"].split("\n")[0]
        + "".join("\n" + times.format(r, h) for r, h in trips),
    }
    if options.get("transfers"):
        tables["transfers.txt"] = "from_stop_id,to_stop_id,transfer_type\ns2,s3,0\n"
    if options.get("headway"):
        tables["frequencies.txt"] = (
            "trip_id,start_time,end_time,headway_secs\n"
            + "".join(
                f"{r}{h},{h:02}:00:00,{h + 1:02}:00:00,{options['headway']}\n"
                for r, h in trips
            )
        )
    return _zip(tables)


# Specs for _network, plus "cut" (the routes a selector keeps), "in" (the
# containers the index names), "ended" (an index window that ended) and
# "renewed" (a probe answering 200).
NEW, OLD, OLDER = ({"start": f"2026{month}01"} for month in ("06", "05", "04"))
C, F = {"agency": "C"}, {"agency": "F"}
AB, ABC = {**C, "routes": ("a", "b")}, {**C, "routes": ("a", "b", "c")}
IN_C, ADDS = {**F, "in": "C"}, f"+ similar to A but adds service on {DAY}"
# A network on a placeholder calendar, and the note it gets.
HELD = {"start": "20000101", "end": "20990101"}
HELD_NOTE = "placeholder calendar 2000-01-01 to 2099-01-01"
KEPT, SKIP_C = "+ kept: containment not proven current", "- contained in C [C]"


@pytest.mark.filterwarnings("ignore:no Mobility Database API token")
@pytest.mark.parametrize(
    "feeds, when, expected",
    [
        ({"X": F, "Y": F}, None, {"X": "+", "Y": "- same content as X [X]"}),
        (
            {"X": {**ABC, "cut": "a b"}, "Y": {**ABC, "cut": "b c"}},
            None,
            {"X": "+ cut to routes a, b", "Y": "+ cut to routes b, c [X]"},
        ),
        ({"F": IN_C, "C": {**C, "renewed": True}}, None, {"C": "+", "F": KEPT}),
        ({"F": {**IN_C, "renewed": True}, "C": C}, None, {"C": "+", "F": KEPT}),
        (
            {"F": IN_C, "C": {**AB, "cut": "a"}},
            None,
            {
                "C": "+ cut to routes a",
                "F": "+ kept: container C cropped to selected routes",
            },
        ),
        (
            {"F": IN_C, "C": {**C, "ended": True}},
            None,
            {"C": f"- {R_ENDED}{SAME}", "F": "+ kept: container C skipped"},
        ),
        (
            {"P": {**AB, "cut": "a"}, "C": AB, "F": IN_C},
            None,
            {"P": "+ cut to routes a", "C": "+ [P]", "F": SKIP_C},
        ),
        (
            {"C": AB, "P": {**AB, "cut": "a"}, "F": IN_C},
            None,
            {"C": "+", "P": "- same content as C [C]", "F": SKIP_C},
        ),
        (
            {"A": NEW, "B": OLD},
            DAY,
            {"A": "+", "B": "- another version of A [A 1.0 1.0]"},
        ),
        (
            {"A": NEW, "B": {**OLD, "agency": "HSL Oy."}},
            DAY,
            {"A": "+", "B": "- another version of A [A 1.0 1.0]"},
        ),
        (
            {"A": NEW, "B": {**OLD, "agency": ""}},
            DAY,
            {"A": "+", "B": "- another version of A [A 1.0 1.0]"},
        ),
        ({"A": NEW, "B": {**OLD, "hours": (8, 9)}}, DAY, {"A": "+", "B": ADDS}),
        ({"A": NEW, "B": {**OLD, "hours": (9,)}}, DAY, {"A": "+", "B": ADDS}),
        (
            {"A": {**NEW, "headway": 600}, "B": {**OLD, "headway": 600}},
            DAY,
            {"A": "+", "B": "- another version of A [A 1.0 1.0]"},
        ),
        (
            {"A": {**NEW, "headway": 600}, "B": {**OLD, "headway": 300}},
            DAY,
            {"A": "+", "B": ADDS},
        ),
        (
            {"A": {**NEW, "hours": (8, 9)}, "B": OLD, "C": {**OLDER, "hours": (8, 10)}},
            DAY,
            {"A": "+", "B": "- another version of A [A 1.0 1.0]", "C": ADDS},
        ),
        (
            {"C": {**OLDER, "hours": (8, 10)}, "B": OLD, "A": {**NEW, "hours": (8, 9)}},
            DAY,
            {"A": "+", "B": "- another version of A [A 1.0 1.0]", "C": ADDS},
        ),
        (
            {
                "A": NEW,
                "B": {**OLD, "stops": range(1, 11)},
                "C": {**OLDER, "stops": range(2, 12)},
            },
            DAY,
            {"A": "+", "B": "- another version of A [A 1.0 0.818]", "C": "+"},
        ),
        ({"V": NEW, "C": OLD, "F": IN_C}, DAY, {"V": "+", "C": "+", "F": SKIP_C}),
        (
            {"V": NEW, "E": OLD, "C": OLD, "F": IN_C},
            DAY,
            {"V": "+", "E": "+", "C": "- same content as E [E]", "F": SKIP_C},
        ),
        (
            {"A": {**NEW, "stops": range(8)}, "B": {**OLD, "stops": range(1, 9)}},
            DAY,
            {"A": "+", "B": "+"},
        ),
        (
            {"A": NEW, "B": {**OLD, "transfers": True}},
            DAY,
            {"A": "+", "B": "+ similar to A; kept, has transfers or pathways"},
        ),
        (
            {"A": {"start": "20260701"}, "B": OLD},
            None,
            {
                "A": "+ similar to B; kept, no study day",
                "B": "+ similar to A; kept, no study day",
            },
        ),
        (
            {"A": {**NEW, "end": "20991231"}, "B": OLD},
            DAY,
            {
                "A": "+ placeholder calendar 2026-06-01 to 2099-12-31",
                "B": "- another version of A [A 1.0 1.0]",
            },
        ),
        (
            {"A": {**NEW, "stops": range(8)}, "B": {**HELD, "stops": range(1, 9)}},
            DAY,
            {"A": "+", "B": f"+ {HELD_NOTE}"},
        ),
        (
            {"A": HELD, "B": {**HELD, "routes": ("x",)}},
            DAY,
            {"A": f"+ {HELD_NOTE}", "B": f"+ {HELD_NOTE}"},
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
        "placeholder-under-stop-threshold placeholders-other-routes"
    ).split(),
)
def test_fetch_delivers_one_copy_per_service(
    tmp_path, monkeypatch, feeds, when, expected
):
    from transitio.catalog import TransitlandAtlas

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
        when=when,
        tiers=["local"] if edges else None,
    )

    def seen(e):
        links = e["same_as"] + e["contained_in"] + [*(e["version_of"] or {}).values()]
        links = links and f"[{' '.join(map(str, links))}]"
        head = f"- {e['reason']}" if e["reason"] else "+"
        return " ".join(filter(None, (head, e["note"], links)))

    assert {e["feed_id"]: seen(e) for e in result.selection} == expected
    # A feed left out before download is never downloaded; every other is.
    early = ("contained in", "unchanged since indexed")
    assert sorted(downloads) == sorted(
        e["feed_id"]
        for e in result.selection
        if not any(w in (e["reason"] or "") for w in early)
    )
    paths = [e["path"] for e in result.selection if e["decision"] == "delivered"]
    assert sorted(result.feeds) == sorted(paths) and len(result.reports) == len(paths)


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
    import io as _io

    buffer = _io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in GTFS.items():
            archive.writestr(name, content)
    return buffer.getvalue()


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
    monkeypatch.setattr("transitio.catalog.TransitlandAtlas.download", fake_download)
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
    assert [p.name for p in result.feeds] == ["latest.zip"]


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
        "transitio.catalog.MobilityDatabase.download", fake_dataset_download
    )
    result = fetch(
        place="Q1757",
        index=index,
        directory=tmp_path / "out",
        crop=False,
        when="2026-06-01",
        refresh_token="tok",
    )
    assert [p.name for p in result.feeds] == ["mdb-9-2026.zip"]


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


def test_fetch_place_falls_back_to_atlas_when_the_dataset_download_fails(
    tmp_path, monkeypatch
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
        raise RuntimeError("dataset download boom")

    monkeypatch.setattr("transitio.catalog.MobilityDatabase.download", failing)
    result = fetch(
        place="Q1757",
        index=index,
        directory=tmp_path / "out",
        crop=False,
        when="2026-06-01",
        refresh_token="tok",
    )
    # The dataset download failed, so the Atlas fallback delivered latest.zip.
    assert [p.name for p in result.feeds] == ["latest.zip"]
    assert result.skipped == []
    (entry,) = result.selection
    assert (entry["fetched_from"], entry["download_errors"]) == (
        "producer",
        "mdb dataset: dataset download boom",
    )


def test_fetch_place_skips_a_feed_whose_provenance_sidecar_is_unreadable(
    tmp_path, monkeypatch
):
    index = _place_index(
        tmp_path, {"atlas": {"urls": {"static_current": "https://feeds.example/a.zip"}}}
    )
    _stub_pbf_and_atlas(monkeypatch, tmp_path, _gtfs_payload())
    out = tmp_path / "out"
    out.mkdir()
    # The stub writes no sidecar, so this truncated one stays beside the zip.
    (out / "latest.provenance.json").write_text("{")
    result = fetch(place="Q1757", index=index, directory=out, crop=False, osm=False)
    ((feed_id, reason),) = result.skipped
    assert feed_id == "f-a" and reason.startswith("processing failed: ")
    assert result.feeds == []


def test_fetch_place_from_a_bound_index_uses_its_snapshot(tmp_path, monkeypatch):
    import transitio

    index = _place_index(
        tmp_path, {"atlas": {"urls": {"static_current": "https://feeds.example/a.zip"}}}
    )
    _stub_pbf_and_atlas(monkeypatch, tmp_path, _gtfs_payload())
    place_obj = transitio.place("Q1757", index=index)
    result = fetch(place=place_obj, directory=tmp_path / "out", crop=False)
    assert result.provenance["snapshot"] == index.snapshot_id
    assert [p.name for p in result.feeds] == ["latest.zip"]


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

    monkeypatch.setattr("transitio.catalog.MobilityDatabase.download", dataset_download)
    result = fetch(
        place="Q1757",
        index=index,
        directory=tmp_path / "out",
        crop=False,
        refresh_token="tok",
    )
    assert [p.name for p in result.feeds] == ["mdb-9-newest.zip"]


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
    assert [p.name for p in result.feeds] == ["latest.zip"]
    assert result.skipped == []


def test_fetch_place_output_names_differ_by_geometry(tmp_path, monkeypatch):
    # The same place id with different geometry must not share an output name,
    # or one fetch would overwrite the other's differently-cropped feed.
    import shapely

    import transitio

    index = _place_index(
        tmp_path, {"atlas": {"urls": {"static_current": "https://feeds.example/a.zip"}}}
    )
    _stub_pbf_and_atlas(monkeypatch, tmp_path, _gtfs_payload())
    out = tmp_path / "out"
    place_obj = transitio.place("Q1757", index=index)
    first = fetch(place=place_obj, directory=out)
    # Still around the fixture's stops, so the crop keeps its trip.
    place_obj._record["geometry"] = shapely.box(24.92, 60.16, 24.95, 60.18)
    second = fetch(place=place_obj, directory=out)
    assert first.feeds[0].name != second.feeds[0].name


# A two-part place: the first part holds the GTFS fixture's stops.
_SERVED = (24.9, 60.1, 25.1, 60.3)
_REMOTE = (26.0, 61.0, 26.2, 61.2)


@pytest.mark.parametrize(
    "stops, expected",
    [
        pytest.param([[(60.169, 24.931)]], "served", id="one-part"),
        pytest.param([[(60.169, 24.931)], [(61.1, 26.1)]], "whole", id="both-parts"),
        pytest.param([[(59.0, 24.0)]], "whole", id="no-part"),
        pytest.param([], "whole", id="nothing-delivered"),
        # A feed whose stops cannot be read could serve the remote part.
        pytest.param([[(60.169, 24.931)], None], "whole", id="unreadable-stops"),
    ],
)
def test_osm_parts_are_those_holding_a_delivered_stop(tmp_path, stops, expected):
    import shapely

    from transitio.pipeline._fetch import _osm_parts

    geometry = shapely.union_all([shapely.box(*_SERVED), shapely.box(*_REMOTE)])
    feeds = []
    for number, coords in enumerate(stops):
        path = tmp_path / f"{number}.zip"
        if coords is None:
            path.write_bytes(_zip({"agency.txt": GTFS["agency.txt"]}))
        else:
            rows = "".join(f"s{i},{y},{x}\n" for i, (y, x) in enumerate(coords))
            path.write_bytes(_zip({"stops.txt": "stop_id,stop_lat,stop_lon\n" + rows}))
        feeds.append(path)
    parts = _osm_parts(geometry, feeds)
    assert parts.equals(shapely.box(*_SERVED) if expected == "served" else geometry)


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
    download = transitio.catalog.TransitlandAtlas.download
    events = []

    def recorded_download(self, feed, directory=None):
        events.append("feed")
        return download(self, feed, directory=directory)

    def recorded_fetch_pbf(aoi, **kwargs):
        events.append((aoi, kwargs["buffer_m"]))
        return fake_pbf

    monkeypatch.setattr(
        "transitio.catalog.TransitlandAtlas.download", recorded_download
    )
    monkeypatch.setattr("transitio.osm.fetch_pbf", recorded_fetch_pbf)
    served = shapely.box(*_SERVED)
    place_obj = transitio.place("Q1757", index=index)
    place_obj._record["geometry"] = shapely.union_all([served, shapely.box(*_REMOTE)])
    result = fetch(place=place_obj, directory=tmp_path / "out", crop=False)

    feed, (aoi, buffer_m) = events
    assert feed == "feed" and aoi.equals(served) and buffer_m == 1600
    assert result.osm_pbf == fake_pbf
    assert result.osm_area.equals(_buffered(served, 1600))
    *_, note = result.selection
    assert note["feed_id"] is None and note["decision"] is None
    assert note["note"].startswith("OSM area: 1 of 2 parts (")


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
    result = fetch(
        place="Q1757",
        index=index,
        directory=tmp_path / "out",
        crop=False,
        repair=repair,
        tiers=["local", "regional"],
        exclude=["national"],
    )
    # the repair, when asked for, receives the cropped feed
    assert bool(repaired_inputs) == repair
    assert all("-cropped-" in path for path in repaired_inputs)
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
    assert result.selection[0]["note"] == "cut to routes r-local, r-reg"
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
    out = tmp_path / "out"
    a = fetch(place="Q1757", index=index, directory=out, crop=False, tiers=["local"])
    b = fetch(place="Q1757", index=index, directory=out, crop=False, tiers=["regional"])
    # Different tier selections must not overwrite each other's cropped feed.
    assert a.feeds[0].name != b.feeds[0].name


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
    "osm, notes",
    [(False, []), (True, [f"OSM extract not fetched: {_EXTRACT_FAILURE}"])],
    ids=["osm-off", "download-failed"],
)
def test_fetch_aoi_without_an_extract_keeps_the_feeds(
    pipeline_env, monkeypatch, osm, notes
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
    # The note entry comes after the feed's.
    assert [(e["feed_id"], e["note"]) for e in result.selection[1:]] == [
        (None, note) for note in notes
    ]
    warned = [str(w.message) for w in caught if "OSM" in str(w.message)]
    assert warned == [f"{note}; osm_pbf is None" for note in notes]


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

    monkeypatch.setattr("transitio.catalog.TransitlandAtlas.download", fake_download)
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
    assert [p.name for p in result.feeds] == ["latest.zip"]


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
