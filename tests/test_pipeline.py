import zipfile

import httpx
import pytest

pytest.importorskip("transitio._core")

import transitio.catalog  # noqa: E402
import transitio.osm  # noqa: E402
from transitio.exceptions import StaleSelectorError  # noqa: E402
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


def test_fetch_requires_exactly_one_of_aoi_or_place():
    with pytest.raises(ValueError, match="exactly one of aoi= or place="):
        fetch()
    with pytest.raises(ValueError, match="exactly one of aoi= or place="):
        fetch((0, 0, 1, 1), place="X")


def test_download_indexed_prefers_mdb_then_atlas(tmp_path):
    from transitio.exceptions import DownloadError
    from transitio.pipeline._fetch import _download_indexed

    class _FakeFeed:
        def __init__(self, feed_id, mdb=None, atlas=None):
            self.feed_id = feed_id
            self._row = {"mdb": mdb, "atlas": atlas}

    class _RecordingDB:
        def __init__(self):
            self.calls = []

        def download_latest(self, feed, directory=None):
            self.calls.append(feed.latest_dataset_url)
            return tmp_path / "mdb.zip"

    class _RecordingAtlas:
        def __init__(self):
            self.calls = []

        def download(self, feed, directory=None):
            self.calls.append(feed.static_url)
            return tmp_path / "atlas.zip"

    db, atlas = _RecordingDB(), _RecordingAtlas()
    both = _FakeFeed(
        "f-a",
        mdb={"urls": {"direct_download": "https://m/a.zip"}},
        atlas={"urls": {"static_current": "https://a/a.zip"}},
    )
    assert _download_indexed(both, db, atlas, tmp_path).name == "mdb.zip"
    assert db.calls == ["https://m/a.zip"] and atlas.calls == []
    only = _FakeFeed("f-b", atlas={"urls": {"static_current": "https://a/b.zip"}})
    assert _download_indexed(only, db, atlas, tmp_path).name == "atlas.zip"
    assert atlas.calls == ["https://a/b.zip"]
    with pytest.raises(DownloadError, match="no downloadable url"):
        _download_indexed(_FakeFeed("f-c"), db, atlas, tmp_path)

    class _FailingDB:
        def download_latest(self, feed, directory=None):
            raise RuntimeError("boom")

    class _FailingAtlas:
        def download(self, feed, directory=None):
            raise RuntimeError("nope")

    # MDB present but failing falls back to Atlas.
    both_urls = _FakeFeed(
        "f-d",
        mdb={"urls": {"direct_download": "https://m/d.zip"}},
        atlas={"urls": {"static_current": "https://a/d.zip"}},
    )
    fallback_atlas = _RecordingAtlas()
    assert (
        _download_indexed(both_urls, _FailingDB(), fallback_atlas, tmp_path).name
        == "atlas.zip"
    )
    assert fallback_atlas.calls == ["https://a/d.zip"]
    # Both sources failing reports both.
    with pytest.raises(DownloadError, match="mdb.*atlas"):
        _download_indexed(both_urls, _FailingDB(), _FailingAtlas(), tmp_path)


def test_fetch_place_selects_downloads_and_processes(tmp_path, monkeypatch):
    import io as _io
    import sys as _sys

    _sys.path.insert(
        0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "scripts")
    )
    import transitio.index as transitio_index
    from index_build import licensing, publish
    from test_index_license import HULL, _cache
    from test_index_publish import _covered_feed, _edge

    feeds = [
        {
            **_covered_feed("f-a", coverage_source="crawl"),
            "coverage": HULL,
            "atlas": {"urls": {"static_current": "https://feeds.example/a.zip"}},
        }
    ]
    edges = [_edge("Q1757", "f-a", tier="local")]
    cache = _cache(tmp_path, feeds, edges)
    licensing.license_index(cache)
    publish.publish(cache)
    index = transitio_index.read_index(cache / "index")

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
    assert result.skipped == []
    assert result.selections == []


def test_fetch_aoi_rejects_place_only_arguments():
    for kwargs in (
        {"exclude": ["national"]},
        {"tiers": ["local"]},
        {"on_unknown": "exclude"},
    ):
        with pytest.raises(ValueError, match="apply only with place="):
            fetch((0, 0, 1, 1), **kwargs)


def test_fetch_place_rejects_country_code():
    with pytest.raises(ValueError, match="country_code= applies only with aoi="):
        fetch(place="X", country_code="FI")


def _place_index(tmp_path, feed):
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "scripts"))
    import transitio.index as transitio_index
    from index_build import licensing, publish
    from test_index_license import HULL, _cache
    from test_index_publish import _covered_feed, _edge

    feeds = [
        {**_covered_feed("f-a", coverage_source="crawl"), "coverage": HULL, **feed}
    ]
    cache = _cache(tmp_path, feeds, [_edge("Q1757", "f-a", tier="local")])
    licensing.license_index(cache)
    publish.publish(cache)
    return transitio_index.read_index(cache / "index")


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
    assert result.provenance["discovery_semantics_version"] == 1
    assert result.provenance["transitio_version"]


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
    place_obj._record["geometry"] = shapely.box(10.0, 50.0, 10.2, 50.2)
    second = fetch(place=place_obj, directory=out)
    assert first.feeds[0].name != second.feeds[0].name


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
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "scripts"))
    import transitio.index as transitio_index
    from index_build import licensing, publish, store
    from test_index_license import HULL, _cache
    from test_index_publish import _covered_feed, _edge

    feed = {
        **_covered_feed("f-a", coverage_source="crawl"),
        "coverage": HULL,
        "atlas": {"urls": {"static_current": "https://feeds.example/a.zip"}},
    }
    cache = _cache(tmp_path, [feed], [_edge("Q1757", "f-a", tier="local")])
    # Re-publish the classify generation with hand-crafted per-tier selectors,
    # keeping its lineage so licensing/publish accept it.
    feeds_classified, manifest = store.read_jsonl(
        cache / "classify", "edges.json", "feeds_classified.jsonl"
    )
    directory = store.open_subdir(cache, "classify")
    try:
        with store.exclusive_writer(directory):
            store.publish(
                cache / "classify",
                "edges.json",
                {
                    "feeds_classified.jsonl": store.jsonl_chunks(feeds_classified),
                    "edges.jsonl": store.jsonl_chunks(edges),
                },
                {**manifest, "edges": len(edges)},
                held=directory,
            )
    finally:
        directory.close()
    licensing.license_index(cache)
    publish.publish(cache)
    return transitio_index.read_index(cache / "index")


@pytest.mark.parametrize("repair", [False, True])
def test_fetch_place_crops_bundles_to_the_selected_routes(
    tmp_path, monkeypatch, repair
):
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "scripts"))
    from test_index_publish import _edge

    service = {"stops": 1, "routes": 1, "departures_per_day": 1.0}

    def sel_edge(tier, route):
        return {
            **_edge("Q1757", "f-a", tier=tier, service=service),
            "selector_state": "complete",
            "selector": {"route_id": [route]},
            "needs_review": False,
        }

    payload = _multi_route_gtfs()
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

    # repair=True rewrites the feed before the crop; the selector is validated
    # against the download and still cropped correctly on the repaired bytes.
    result = fetch(
        place="Q1757",
        index=index,
        directory=tmp_path / "out",
        crop=False,
        repair=repair,
        tiers=["local", "regional"],
        exclude=["national"],
    )
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
    # The delivered feed is referentially consistent (no validation errors).
    assert result.reports[0]["summary"]["counts"]["errors"] == 0
    (selection,) = result.selections
    assert selection["feed_id"] == "f-a"
    assert selection["selector_state"] == "complete"
    assert selection["trusted"] is True and selection["reason"] is None
    assert selection["kept"] == ["r-local", "r-reg"]
    assert selection["dropped"] == ["r-nat", "r-unknown"]
    # The audit names the contributing per-tier edges.
    by_tier = {e["tier"]: e["route_ids"] for e in selection["selected_by"]}
    assert by_tier == {"local": ["r-local"], "regional": ["r-reg"]}


def test_fetch_place_output_names_differ_by_selected_routes(tmp_path, monkeypatch):
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "scripts"))
    from test_index_publish import _edge

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
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "scripts"))
    from test_index_publish import _edge

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
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "scripts"))
    from test_index_publish import _edge

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
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "scripts"))
    from test_index_publish import _edge

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
