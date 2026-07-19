import zipfile

import httpx
import pytest

pytest.importorskip("transitio._core")

import transitio.catalog  # noqa: E402
import transitio.osm  # noqa: E402
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
    assert len(result.feeds) == 1
    assert "-cropped-" in result.feeds[0].name
    assert result.feeds[0].suffix == ".zip"
    (report,) = result.reports
    assert report["summary"]["counts"]["errors"] == 0
    assert result.skipped == []
    assert result.repairs == [[]]
    pbf, feeds = result
    assert pbf == fake_pbf and feeds == result.feeds


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
    ((feed_id, reason),) = result.skipped
    assert feed_id == "mdb-10"
    assert "no service on the requested day" in reason
    assert "20260101..20261231" in reason


def test_feed_modes_undeterminable(tmp_path):
    from transitio.pipeline._fetch import _feed_modes

    not_a_zip = tmp_path / "feed.zip"
    not_a_zip.write_bytes(b"not a zip archive")
    assert _feed_modes(not_a_zip) is None

    no_routes = tmp_path / "noroutes.zip"
    with zipfile.ZipFile(no_routes, "w") as archive:
        archive.writestr("agency.txt", "agency_id\n")
    assert _feed_modes(no_routes) is None


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
