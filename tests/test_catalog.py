import datetime
import gzip
import hashlib
import io
import itertools
import json
import os
import time
import zipfile

import httpx
import pytest
from shapely.geometry import box

from transitio import _http
from transitio.catalog import _cache
from transitio.catalog import (
    TOKEN_ENV_VAR,
    AtlasFeed,
    MobilityDatabase,
    TransitlandAtlas,
)
from transitio.catalog._client import _bounds
from transitio.catalog._models import Dataset, Feed
from transitio.catalog._nested import extract_feed, split_fragment
from transitio.exceptions import DownloadError, MissingTokenError

FEED_RECORD = {
    "id": "mdb-1",
    "provider": "Helsinki Region Transport",
    "status": "active",
    "official": True,
    "source_info": {
        "producer_url": "https://example.com/gtfs.zip",
        "license_url": "https://example.com/license",
    },
    "latest_dataset": {"hosted_url": "https://files.example.com/mdb-1/latest.zip"},
    "locations": [{"country_code": "FI", "municipality": "Helsinki"}],
}

CSV_HEADER = (
    "id,data_type,status,is_official,provider,"
    "location.country_code,location.subdivision_name,location.municipality,"
    "location.bounding_box.minimum_latitude,"
    "location.bounding_box.maximum_latitude,"
    "location.bounding_box.minimum_longitude,"
    "location.bounding_box.maximum_longitude,"
    "urls.direct_download,urls.latest,urls.license"
)

CSV_ROWS = [
    "mdb-10,gtfs,active,True,HSL,FI,Uusimaa,Helsinki,59.9,60.6,24.2,25.6,"
    "https://example.com/hsl.zip,https://files.example.com/mdb-10/latest.zip,"
    "https://example.com/license",
    "mdb-11,gtfs,deprecated,False,Old Operator,FI,Uusimaa,Helsinki,"
    "59.9,60.6,24.2,25.6,https://example.com/old.zip,,",
    "mdb-12,gtfs_rt,active,True,HSL RT,FI,Uusimaa,Helsinki,"
    "59.9,60.6,24.2,25.6,https://example.com/rt,,",
    "mdb-13,gtfs,active,True,Skanetrafiken,SE,,,55.3,56.5,12.5,14.6,"
    "https://example.com/skane.zip,,",
]

CSV_BODY = "\n".join([CSV_HEADER, *CSV_ROWS]) + "\n"

DATASET_RECORD = {
    "id": "mdb-1-202606",
    "feed_id": "mdb-1",
    "hosted_url": "https://files.example.com/mdb-1-202606.zip",
    "downloaded_at": "2026-06-20T03:00:00Z",
    "hash": None,
    "service_date_range_start": "2026-06-15",
    "service_date_range_end": "2026-12-13",
    "validation_report": {"url_json": "https://files.example.com/report.json"},
}

OLD_DATASET_RECORD = {
    "id": "mdb-1-202501",
    "feed_id": "mdb-1",
    "hosted_url": "https://files.example.com/mdb-1-202501.zip",
    "downloaded_at": "2025-01-05T03:00:00Z",
    "hash": None,
    "service_date_range_start": "2025-01-01",
    "service_date_range_end": "2025-06-30",
    "validation_report": None,
}


def make_handler(routes, requests=None):
    """MockTransport handler serving the token endpoint plus given routes."""

    def handler(request):
        if requests is not None:
            requests.append(request)
        if request.url.path == "/v1/tokens":
            return httpx.Response(
                200, json={"access_token": "access-abc", "expires_in": 3600}
            )
        entry = routes.get(request.url.path)
        if entry is None:
            return httpx.Response(404, json={"detail": "not found"})
        if callable(entry):
            return entry(request)
        return httpx.Response(200, json=entry)

    return handler


def make_db(routes, tmp_path, requests=None, token="refresh-xyz"):
    transport = httpx.MockTransport(make_handler(routes, requests))
    db = MobilityDatabase(token, cache_dir=tmp_path, transport=transport)
    db._retry_wait = 0.0
    return db


def api_requests(requests, path):
    return [r for r in requests if r.url.path == path]


def test_search_feeds_bbox_and_auth(tmp_path):
    requests = []
    routes = {"/v1/gtfs_feeds": [FEED_RECORD]}
    with make_db(routes, tmp_path, requests) as db:
        feeds = db.search_feeds(aoi=box(24.6, 60.1, 25.2, 60.4))

    assert len(feeds) == 1
    feed = feeds[0]
    assert feed.id == "mdb-1"
    assert feed.provider == "Helsinki Region Transport"
    assert feed.official is True
    assert feed.license_url == "https://example.com/license"

    (request,) = api_requests(requests, "/v1/gtfs_feeds")
    params = dict(request.url.params)
    assert params["dataset_latitudes"] == "60.1,60.4"
    assert params["dataset_longitudes"] == "24.6,25.2"
    assert params["bounding_filter_method"] == "partially_enclosed"
    assert request.headers["Authorization"] == "Bearer access-abc"


def test_search_feeds_status_filter(tmp_path):
    deprecated = dict(FEED_RECORD, id="mdb-2", status="deprecated")
    routes = {"/v1/gtfs_feeds": [FEED_RECORD, deprecated]}
    with make_db(routes, tmp_path) as db:
        assert [f.id for f in db.search_feeds(country_code="FI")] == ["mdb-1"]
        both = db.search_feeds(country_code="FI", status=None)
        assert [f.id for f in both] == ["mdb-1", "mdb-2"]


def test_search_feeds_invalid_enclosure(tmp_path):
    with make_db({}, tmp_path) as db:
        with pytest.raises(ValueError):
            db.search_feeds(aoi=(24.6, 60.1, 25.2, 60.4), enclosure="overlapping")


def test_bounds_accepts_tuple_and_rejects_junk():
    assert _bounds((24.6, 60.1, 25.2, 60.4)) == (24.6, 60.1, 25.2, 60.4)
    with pytest.raises(ValueError):
        _bounds("helsinki")
    with pytest.raises(ValueError):
        _bounds((24.6, 60.1))


def test_pagination(tmp_path):
    records = [dict(FEED_RECORD, id=f"mdb-{i}") for i in range(150)]
    requests = []

    def feeds_endpoint(request):
        offset = int(request.url.params["offset"])
        limit = int(request.url.params["limit"])
        return httpx.Response(200, json=records[offset : offset + limit])

    routes = {"/v1/gtfs_feeds": feeds_endpoint}
    with make_db(routes, tmp_path, requests) as db:
        feeds = db.search_feeds(country_code="FI", limit=150)

    assert len(feeds) == 150
    pages = api_requests(requests, "/v1/gtfs_feeds")
    assert [(p.url.params["offset"], p.url.params["limit"]) for p in pages] == [
        ("0", "100"),
        ("100", "100"),
    ]


def test_search_feeds_status_filter_spans_pages(tmp_path):
    deprecated = [
        dict(FEED_RECORD, id=f"mdb-{i}", status="deprecated") for i in range(100)
    ]
    active = [dict(FEED_RECORD, id=f"mdb-{100 + i}") for i in range(3)]
    records = deprecated + active
    requests = []

    def feeds_endpoint(request):
        offset = int(request.url.params["offset"])
        limit = int(request.url.params["limit"])
        return httpx.Response(200, json=records[offset : offset + limit])

    routes = {"/v1/gtfs_feeds": feeds_endpoint}
    with make_db(routes, tmp_path, requests) as db:
        feeds = db.search_feeds(country_code="FI", limit=2)

    # All page-1 records are deprecated; pagination must continue to page 2.
    assert [f.id for f in feeds] == ["mdb-100", "mdb-101"]
    assert len(api_requests(requests, "/v1/gtfs_feeds")) == 2


def test_datasets_sorted_newest_first(tmp_path):
    routes = {"/v1/gtfs_feeds/mdb-1/datasets": [OLD_DATASET_RECORD, DATASET_RECORD]}
    with make_db(routes, tmp_path) as db:
        datasets = db.datasets("mdb-1")
    assert [d.id for d in datasets] == ["mdb-1-202606", "mdb-1-202501"]


def test_dataset_for_picks_covering_dataset(tmp_path):
    routes = {"/v1/gtfs_feeds/mdb-1/datasets": [DATASET_RECORD, OLD_DATASET_RECORD]}
    with make_db(routes, tmp_path) as db:
        assert db.dataset_for("mdb-1", "2026-09-01").id == "mdb-1-202606"
        assert db.dataset_for("mdb-1", "2026-09-01 08:00").id == "mdb-1-202606"
        assert db.dataset_for("mdb-1", datetime.date(2025, 3, 1)).id == "mdb-1-202501"
        assert db.dataset_for("mdb-1", "2024-01-01") is None


def test_dataset_for_rejects_non_dates(tmp_path):
    with make_db({}, tmp_path) as db:
        with pytest.raises(TypeError):
            db.dataset_for("mdb-1", 20260901)


def test_as_date_accepts_zulu_datetime_strings():
    from transitio.catalog._models import as_date

    assert as_date("2026-09-01T08:00:00Z") == datetime.date(2026, 9, 1)
    assert as_date("2026-09-01") == datetime.date(2026, 9, 1)


def test_download_rejects_unsafe_ids(tmp_path):
    with make_db({}, tmp_path) as db:
        bad_dataset = Dataset.from_api(dict(DATASET_RECORD, id="../evil"))
        with pytest.raises(DownloadError, match="not safe"):
            db.download(bad_dataset)


def test_download_verifies_checksum_and_caches(tmp_path):
    payload = _zip_bytes({"agency.txt": b"agency_id\nd\n"})
    record = dict(DATASET_RECORD, hash=hashlib.sha256(payload).hexdigest())
    dataset = Dataset.from_api(record)
    requests = []
    routes = {"/mdb-1-202606.zip": lambda request: httpx.Response(200, content=payload)}
    with make_db(routes, tmp_path, requests) as db:
        path = db.download(dataset)
        assert path.read_bytes() == payload

        provenance = json.loads(path.with_suffix(".provenance.json").read_text())
        assert provenance["dataset_id"] == "mdb-1-202606"
        assert provenance["sha256"] == record["hash"]
        assert provenance["service_date_range"] == ["2026-06-15", "2026-12-13"]

        assert db.download(dataset) == path

    downloads = api_requests(requests, "/mdb-1-202606.zip")
    assert len(downloads) == 1
    assert "Authorization" not in downloads[0].headers


def test_download_checksum_mismatch(tmp_path):
    record = dict(DATASET_RECORD, hash="0" * 64)
    dataset = Dataset.from_api(record)
    routes = {"/mdb-1-202606.zip": lambda request: httpx.Response(200, content=b"junk")}
    with make_db(routes, tmp_path) as db:
        with pytest.raises(DownloadError, match="checksum mismatch"):
            db.download(dataset)
    assert not list(tmp_path.rglob("*.zip"))
    assert not list(tmp_path.rglob("*.part"))


def test_validation_report(tmp_path):
    report = {"summary": {"validatorVersion": "6.0.0"}, "notices": []}
    routes = {"/report.json": report}
    with make_db(routes, tmp_path) as db:
        assert db.validation_report(Dataset.from_api(DATASET_RECORD)) == report
        assert db.validation_report(Dataset.from_api(OLD_DATASET_RECORD)) is None


def test_missing_token_raises_for_api_methods(tmp_path, monkeypatch):
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    with make_db({}, tmp_path, token=None) as db:
        with pytest.raises(MissingTokenError, match="refresh token"):
            db.datasets("mdb-1")


def test_token_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV_VAR, "env-token")
    requests = []
    routes = {"/v1/gtfs_feeds": [FEED_RECORD]}
    with make_db(routes, tmp_path, requests, token=None) as db:
        db.search_feeds(country_code="FI")
    (token_request,) = api_requests(requests, "/v1/tokens")
    assert json.loads(token_request.content) == {"refresh_token": "env-token"}


def test_retry_on_transient_errors(tmp_path):
    attempts = []

    def flaky(request):
        attempts.append(request)
        if len(attempts) < 3:
            return httpx.Response(429)
        return httpx.Response(200, json=[FEED_RECORD])

    routes = {"/v1/gtfs_feeds": flaky}
    with make_db(routes, tmp_path) as db:
        feeds = db.search_feeds(country_code="FI")
    assert len(feeds) == 1
    assert len(attempts) == 3


def test_csv_fallback_without_token(tmp_path, monkeypatch):
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    requests = []
    routes = {"/feeds_v2.csv": lambda request: httpx.Response(200, text=CSV_BODY)}
    with make_db(routes, tmp_path, requests, token=None) as db:
        with pytest.warns(UserWarning, match="CSV catalogue"):
            feeds = db.search_feeds(aoi=box(24.6, 60.1, 25.2, 60.4))

    # gtfs_rt, deprecated and out-of-bbox rows are filtered out.
    assert [f.id for f in feeds] == ["mdb-10"]
    feed = feeds[0]
    assert feed.provider == "HSL"
    assert feed.official is True
    assert feed.license_url == "https://example.com/license"
    assert feed.latest_dataset_url == "https://files.example.com/mdb-10/latest.zip"
    assert feed.locations[0]["country_code"] == "FI"

    (request,) = requests
    assert request.url.host == "files.mobilitydatabase.org"
    assert "Authorization" not in request.headers


def test_csv_fallback_filters_and_cache(tmp_path, monkeypatch):
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    requests = []
    routes = {"/feeds_v2.csv": lambda request: httpx.Response(200, text=CSV_BODY)}
    with make_db(routes, tmp_path, requests, token=None) as db:
        with pytest.warns(UserWarning):
            swedish = db.search_feeds(country_code="SE")
            both_statuses = db.search_feeds(country_code="FI", status=None)

    assert [f.id for f in swedish] == ["mdb-13"]
    assert [f.id for f in both_statuses] == ["mdb-10", "mdb-11"]
    # The CSV itself is fetched once and cached.
    assert len(requests) == 1


def test_csv_fallback_tolerates_alternate_headers(tmp_path, monkeypatch):
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    header = (
        "id,data_type,status,official,provider,"
        "country_code,subdivision_name,municipality,"
        "minimum_latitude,maximum_latitude,minimum_longitude,maximum_longitude,"
        "urls.direct_download_url,urls.latest_url,urls.license_url"
    )
    row = (
        "mdb-20,gtfs,active,True,HSL,FI,Uusimaa,Helsinki,59.9,60.6,24.2,25.6,"
        "https://example.com/hsl.zip,https://files.example.com/mdb-20/latest.zip,"
        "https://example.com/license"
    )
    body = f"{header}\n{row}\n"
    routes = {"/feeds_v2.csv": lambda request: httpx.Response(200, text=body)}
    with make_db(routes, tmp_path, token=None) as db:
        with pytest.warns(UserWarning):
            feeds = db.search_feeds(aoi=box(24.6, 60.1, 25.2, 60.4), country_code="FI")

    (feed,) = feeds
    assert feed.id == "mdb-20"
    assert feed.official is True
    assert feed.latest_dataset_url == "https://files.example.com/mdb-20/latest.zip"
    assert feed.locations[0]["municipality"] == "Helsinki"


def test_dataset_for_scans_all_versions(tmp_path):
    filler = [
        dict(
            DATASET_RECORD,
            id=f"mdb-1-filler-{i}",
            service_date_range_start="2026-06-15",
            service_date_range_end="2026-12-13",
        )
        for i in range(120)
    ]
    records = filler + [OLD_DATASET_RECORD]
    requests = []

    def datasets_endpoint(request):
        offset = int(request.url.params["offset"])
        limit = int(request.url.params["limit"])
        return httpx.Response(200, json=records[offset : offset + limit])

    routes = {"/v1/gtfs_feeds/mdb-1/datasets": datasets_endpoint}
    with make_db(routes, tmp_path, requests) as db:
        # The only dataset covering early 2025 sits past the first page.
        assert db.dataset_for("mdb-1", "2025-03-01").id == "mdb-1-202501"
    assert len(api_requests(requests, "/v1/gtfs_feeds/mdb-1/datasets")) == 2


def test_token_present_skips_csv(tmp_path):
    requests = []
    routes = {"/v1/gtfs_feeds": [FEED_RECORD]}
    with make_db(routes, tmp_path, requests) as db:
        feeds = db.search_feeds(country_code="FI")
    assert [f.id for f in feeds] == ["mdb-1"]
    assert feeds[0].latest_dataset_url == "https://files.example.com/mdb-1/latest.zip"
    assert not [r for r in requests if r.url.path == "/feeds_v2.csv"]


def test_download_latest(tmp_path, monkeypatch):
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    payload = _zip_bytes({"agency.txt": b"agency_id\nl\n"})
    requests = []
    routes = {
        "/feeds_v2.csv": lambda request: httpx.Response(200, text=CSV_BODY),
        "/mdb-10/latest.zip": lambda request: httpx.Response(200, content=payload),
    }
    with make_db(routes, tmp_path, requests, token=None) as db:
        with pytest.warns(UserWarning):
            (feed,) = db.search_feeds(country_code="FI")
        path = db.download_latest(feed)
        copy = db.download_latest(feed, directory=tmp_path / "out")

    # Without a directory the cached version is returned, named by content;
    # with one, a copy at today's path and the flat provenance fields.
    assert path.name == f"{hashlib.sha256(payload).hexdigest()}.zip"
    assert copy == tmp_path / "out" / "latest.zip"
    assert path.read_bytes() == copy.read_bytes() == payload
    provenance = json.loads(copy.with_suffix(".provenance.json").read_text())
    assert provenance["feed_id"] == "mdb-10" and "cache" not in provenance
    assert provenance["source_url"] == feed.latest_dataset_url
    (download,) = [r for r in requests if r.url.path == "/mdb-10/latest.zip"]
    assert "Authorization" not in download.headers


def test_download_latest_without_url(tmp_path, monkeypatch):
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    routes = {"/feeds_v2.csv": lambda request: httpx.Response(200, text=CSV_BODY)}
    with make_db(routes, tmp_path, token=None) as db:
        with pytest.warns(UserWarning):
            feeds = db.search_feeds(country_code="FI", status=None)
        old = [f for f in feeds if f.id == "mdb-11"][0]
        with pytest.raises(DownloadError, match="latest-dataset url"):
            db.download_latest(old)


def test_expired_access_token_is_refreshed_once(tmp_path):
    calls = {"feeds": 0, "tokens": 0}

    def feeds_endpoint(request):
        calls["feeds"] += 1
        if calls["feeds"] == 1:
            return httpx.Response(401)
        return httpx.Response(200, json=[FEED_RECORD])

    routes = {"/v1/gtfs_feeds": feeds_endpoint}
    requests = []
    with make_db(routes, tmp_path, requests) as db:
        feeds = db.search_feeds(country_code="FI")
    assert len(feeds) == 1
    assert len(api_requests(requests, "/v1/tokens")) == 2


ATLAS_RECORD = {
    "onestop_id": "f-dr5r-nyctsubway",
    "name": "MTA Subway",
    "spec": "gtfs",
    "urls": {"static_current": "https://feeds.example.com/nyct.zip"},
    "authorization": {"type": "header"},
}


def test_atlas_feed_from_record_parses_the_block():
    feed = AtlasFeed.from_record(ATLAS_RECORD, feed_id="f-dr5r-nyctsubway")
    assert feed.feed_id == "f-dr5r-nyctsubway"
    assert feed.onestop_id == "f-dr5r-nyctsubway"
    assert feed.static_url == "https://feeds.example.com/nyct.zip"
    assert feed.spec == "gtfs" and feed.name == "MTA Subway"
    assert feed.requires_auth is True
    # The feed id falls back to the onestop id; a record with neither is refused.
    assert AtlasFeed.from_record({"onestop_id": "f-x", "urls": {}}).feed_id == "f-x"
    with pytest.raises(DownloadError, match="no feed id or onestop id"):
        AtlasFeed.from_record({"urls": {}})


def test_atlas_download_writes_the_gtfs_and_provenance(tmp_path):
    payload = _zip_bytes({"agency.txt": b"agency_id\nt\n"})
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=payload)

    transport = httpx.MockTransport(handler)
    feed = AtlasFeed.from_record(ATLAS_RECORD, feed_id="f-dr5r-nyctsubway")
    with TransitlandAtlas(cache_dir=tmp_path, transport=transport) as atlas:
        path = atlas.download(feed)
        assert path.read_bytes() == payload
        provenance = json.loads(path.with_suffix(".provenance.json").read_text())
        assert provenance["feed_id"] == "f-dr5r-nyctsubway"
        assert provenance["onestop_id"] == "f-dr5r-nyctsubway"
        assert provenance["source"] == "atlas"
        assert provenance["source_url"] == feed.static_url
        assert provenance["sha256"] == hashlib.sha256(payload).hexdigest()
        # The cached copy serves the next call; a refresh fetches it again.
        assert atlas.download(feed) == path
        assert len(requests) == 1
        assert atlas.download(feed, use_cache=False) == path
    assert len(requests) == 2
    assert not list(tmp_path.rglob("*.part"))


def test_atlas_download_refuses_a_feed_without_a_url(tmp_path):
    with TransitlandAtlas(cache_dir=tmp_path) as atlas:
        no_url = AtlasFeed.from_record({"onestop_id": "f-x", "urls": {}})
        with pytest.raises(DownloadError, match="no static download url"):
            atlas.download(no_url)


def test_atlas_download_handles_tilde_and_unicode_ids(tmp_path):
    # The cache dir is a digest, so a normal tilde id or a Unicode id -- which
    # an ASCII filesystem-id check would reject -- downloads and keeps its real
    # id in the provenance sidecar.
    payload = _zip_bytes({"agency.txt": b"agency_id\nu\n"})
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=payload)
    )
    with TransitlandAtlas(cache_dir=tmp_path, transport=transport) as atlas:
        for onestop_id in ("f-dr7-mtanyc~metro~north", "f-台東区"):
            feed = AtlasFeed.from_record(
                {"onestop_id": onestop_id, "urls": {"static_current": "https://x/z"}}
            )
            path = atlas.download(feed)
            assert path.read_bytes() == payload
            provenance = json.loads(path.with_suffix(".provenance.json").read_text())
            assert provenance["feed_id"] == onestop_id


def test_provenance_sidecar_is_not_written_through_a_symlink(tmp_path):
    payload = _zip_bytes({"agency.txt": b"agency_id\ns\n"})
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=payload)
    )
    feed = AtlasFeed.from_record(ATLAS_RECORD, feed_id="f-dr5r-nyctsubway")
    outside = tmp_path / "outside.txt"
    outside.write_text("do not clobber")
    with TransitlandAtlas(cache_dir=tmp_path, transport=transport) as atlas:
        path = atlas.download(feed)
        sidecar = path.with_suffix(".provenance.json")
        # A hostile symlink pre-placed at the sidecar path before a re-download.
        sidecar.unlink()
        sidecar.symlink_to(outside)
        atlas.download(feed)
    # The write replaced the symlink with a real file; the target is untouched.
    assert not sidecar.is_symlink()
    assert json.loads(sidecar.read_text())["feed_id"] == "f-dr5r-nyctsubway"
    assert outside.read_text() == "do not clobber"


def test_atlas_download_namespaces_feeds_in_a_shared_directory(tmp_path):
    # Several feeds downloaded into one directory never collide on latest.zip.
    payloads = {
        "/a.zip": _zip_bytes({"a.txt": b"A"}),
        "/b.zip": _zip_bytes({"b.txt": b"B"}),
    }
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=payloads[request.url.path])
    )
    out = tmp_path / "out"
    feed_a = AtlasFeed.from_record(
        {"onestop_id": "f-a", "urls": {"static_current": "https://x/a.zip"}}
    )
    feed_b = AtlasFeed.from_record(
        {"onestop_id": "f-b", "urls": {"static_current": "https://x/b.zip"}}
    )
    with TransitlandAtlas(cache_dir=tmp_path, transport=transport) as atlas:
        path_a = atlas.download(feed_a, directory=out)
        path_b = atlas.download(feed_b, directory=out)
    assert path_a != path_b
    assert path_a.read_bytes() == payloads["/a.zip"]
    assert path_b.read_bytes() == payloads["/b.zip"]


def _moving_target(client, tmp_path, transport):
    """A download function for the MDB latest dataset or an Atlas feed."""
    url = "https://files.example.com/feed.zip"
    if client == "mdb":
        db = MobilityDatabase(None, cache_dir=tmp_path, transport=transport)
        feed = Feed.from_api({"id": "mdb-7", "latest_dataset": {"hosted_url": url}})
        return db, lambda **options: db.download_latest(feed, **options)
    atlas = TransitlandAtlas(cache_dir=tmp_path, transport=transport)
    feed = AtlasFeed.from_record({"onestop_id": "f-x", "urls": {"static_current": url}})
    return atlas, lambda **options: atlas.download(feed, **options)


@pytest.mark.parametrize("client", ["mdb", "atlas"])
def test_a_moving_target_is_served_from_the_cache_until_a_refresh(
    tmp_path, monkeypatch, client
):
    clock = itertools.count()
    monkeypatch.setattr(_cache, "_now", lambda: f"2026-10-05T00:00:{next(clock):02d}")
    served = {"body": _zip_bytes({"a.txt": b"1"})}
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=served["body"])

    owner, download = _moving_target(client, tmp_path, httpx.MockTransport(handler))
    with owner:
        first = download()
        assert download() == first and len(requests) == 1
        # Identical bytes keep the version and record the acquisition.
        assert download(use_cache=False) == first and len(requests) == 2
        sidecar = json.loads(first.with_suffix(".provenance.json").read_text())
        acquired = [r["retrieved_at"] for r in sidecar["cache"]["sources"]]
        assert len(acquired) == 2 and sidecar["retrieved_at"] == acquired[1]
        # A delivered copy describes the first acquisition.
        copy = download(directory=tmp_path / "out")
        provenance = json.loads(copy.with_suffix(".provenance.json").read_text())
        assert provenance["retrieved_at"] == acquired[0] != acquired[1]
        served["body"] = _zip_bytes({"a.txt": b"2"})
        second = download(use_cache=False)
        assert second != first and not first.exists()
        # A failed refresh raises and keeps the cached version.
        served["body"] = b"<html>maintenance</html>"
        with pytest.raises(DownloadError, match="not a zip archive"):
            download(use_cache=False)
        assert download() == second and len(requests) == 4
        assert not list(tmp_path.rglob("*.part"))


def _damage_sidecar(path, records=(), **fields):
    sidecar = path.with_suffix(".provenance.json")
    data = dict(json.loads(sidecar.read_text()), **fields)
    data["cache"].update(records)
    sidecar.write_text(json.dumps(data))


def _damage_archive(path):
    path.chmod(0o644)
    path.write_bytes(b"damaged")


def _squat(path):
    path.unlink()
    (path / "inner").mkdir(parents=True)


def _link_outside(path):
    outside = path.parent.parent / f"outside{path.suffix}"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)


@pytest.mark.parametrize(
    "damage",
    [
        _damage_archive,
        lambda path: _damage_sidecar(path, sha256="0" * 64),
        lambda path: _damage_sidecar(path, cache={"sources": [{}], "datasets": {}}),
        # A repeats record lacking its skip, note and window.
        lambda path: _damage_sidecar(
            path, records={"repeats": {"0" * 64: {"dropped": 1, "of": []}}}
        ),
        _squat,
        lambda path: _squat(path.with_suffix(".provenance.json")),
        pytest.param(
            _link_outside,
            marks=pytest.mark.skipif(os.name == "nt", reason="symlinks need admin"),
        ),
        pytest.param(
            lambda path: _link_outside(path.with_suffix(".provenance.json")),
            marks=pytest.mark.skipif(os.name == "nt", reason="symlinks need admin"),
        ),
    ],
    ids=[
        "archive",
        "digest",
        "records",
        "repeats-record",
        "archive-directory",
        "sidecar-directory",
        "linked-archive",
        "linked-sidecar",
    ],
)
def test_a_damaged_version_is_deleted_and_downloaded_again(tmp_path, damage):
    payload = _zip_bytes({"a.txt": b"1"})
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=payload)

    owner, download = _moving_target("mdb", tmp_path, httpx.MockTransport(handler))
    with owner:
        path = download()
        damage(path)
        fresh = download()
    assert fresh == path and not fresh.is_symlink()
    assert fresh.read_bytes() == payload
    assert len(requests) == 2


def test_a_dataset_is_reused_by_id_with_identical_bytes_stored_once(tmp_path):
    same = _zip_bytes({"a.txt": b"same"})
    other = _zip_bytes({"a.txt": b"other"})
    bodies = {"/a.zip": same, "/b.zip": same, "/c.zip": other}
    requests = []

    def serve(request):
        requests.append(request)
        return httpx.Response(200, content=bodies[request.url.path])

    def dataset(name, start):
        return Dataset.from_api(
            dict(
                DATASET_RECORD,
                id=f"mdb-1-{name}",
                hosted_url=f"https://files.example.com/{name}.zip",
                hash=hashlib.sha256(bodies[f"/{name}.zip"]).hexdigest(),
                service_date_range_start=start,
            )
        )

    bodies["/d.zip"] = b"<html>maintenance</html>"
    a, b, c, d = (
        dataset("a", "2026-01-01"),
        dataset("b", "2026-02-01"),
        dataset("c", "2026-03-01"),
        dataset("d", "2026-04-01"),
    )
    routes = {path: serve for path in bodies}
    with make_db(routes, tmp_path) as db:
        path_a = db.download(a)
        assert db.download(b) == path_a
        db.download(c)
        # The newer version does not stand in for a requested dataset.
        assert db.download(a) == path_a and db.download(b) == path_a
        # A page matching its catalogued hash is still no archive.
        with pytest.raises(DownloadError, match="not a zip archive"):
            db.download(d)
        assert db.download(a, use_cache=False) == path_a
        copy = db.download(b, directory=tmp_path / "out")
    paths = [r.url.path for r in requests]
    assert paths == ["/a.zip", "/b.zip", "/c.zip", "/d.zip", "/a.zip"]
    assert len(list((tmp_path / "gtfs").glob("id-*/*.zip"))) == 2
    assert copy == tmp_path / "out" / "mdb-1-b.zip"
    provenance = json.loads(copy.with_suffix(".provenance.json").read_text())
    assert provenance["dataset_id"] == "mdb-1-b" and "cache" not in provenance
    assert provenance["service_date_range"][0] == "2026-02-01"


def test_a_cached_dataset_never_stands_for_the_latest(tmp_path):
    old = _zip_bytes({"a.txt": b"old"})
    new = _zip_bytes({"a.txt": b"new"})
    record = dict(DATASET_RECORD, hash=hashlib.sha256(old).hexdigest())
    latest = {"body": new}
    routes = {
        "/mdb-1-202606.zip": lambda request: httpx.Response(200, content=old),
        "/mdb-1/latest.zip": lambda request: httpx.Response(
            200, content=latest["body"]
        ),
    }
    requests = []
    with make_db(routes, tmp_path, requests) as db:
        dataset_path = db.download(Dataset.from_api(record))
        feed = Feed.from_api(FEED_RECORD)
        assert db.download_latest(feed).read_bytes() == new
        # A refresh replaces every other version; the dataset comes again.
        db.download_latest(feed, use_cache=False)
        assert not dataset_path.exists()
        assert db.download(Dataset.from_api(record)) == dataset_path
        # The dataset's bytes served as the latest keep their version and
        # describe no dataset.
        latest["body"] = old
        copy = db.download_latest(feed, use_cache=False, directory=tmp_path / "out")
    assert copy.read_bytes() == old and dataset_path.exists()
    provenance = json.loads(copy.with_suffix(".provenance.json").read_text())
    assert "dataset_id" not in provenance and "service_date_range" not in provenance
    downloads = [r.url.path for r in requests if r.url.path.endswith(".zip")]
    assert downloads == [
        "/mdb-1-202606.zip",
        "/mdb-1/latest.zip",
        "/mdb-1/latest.zip",
        "/mdb-1-202606.zip",
        "/mdb-1/latest.zip",
    ]


@pytest.mark.skipif(os.name == "nt", reason="symlinks need admin on Windows")
@pytest.mark.parametrize("where", ["cache", "delivery"])
def test_a_symlinked_feed_directory_is_refused(tmp_path, where):
    outside = tmp_path / "outside"
    outside.mkdir()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=_zip_bytes({"a.txt": b"1"}))
    )
    owner, download = _moving_target("atlas", tmp_path, transport)
    out = tmp_path / "out"
    if where == "cache":
        folder = _cache.FeedCache(tmp_path).folder("f-x")
    else:
        folder = out / _cache._feed_dir("f-x")
    folder.parent.mkdir(parents=True)
    folder.symlink_to(outside)
    with owner, pytest.raises(DownloadError, match="is a symlink"):
        download(directory=out)
    assert not list(outside.iterdir())


@pytest.mark.skipif(os.name == "nt", reason="cached files stay writable on Windows")
def test_a_cached_file_is_read_only_and_a_delivered_copy_writable(tmp_path):
    payload = _zip_bytes({"a.txt": b"1"})
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=payload)
    )
    owner, download = _moving_target("atlas", tmp_path, transport)
    with owner:
        cached = download()
        copy = download(directory=tmp_path / "out")
    with pytest.raises(PermissionError):
        cached.open("ab")
    with copy.open("ab"):
        pass
    assert copy.read_bytes() == payload


URL = "https://feeds.example/gtfs.zip"
# Incompressible, so its gzip encoding is longer than the decoded bytes.
BODY = b"".join(hashlib.sha256(bytes([i])).digest() for i in range(64))
STRONG, WEAK, MODIFIED = '"v1"', 'W/"v1"', "Tue, 01 Jun 2021 00:00:00 GMT"
FRESH, RESUMED = (None, None), ("bytes=1024-", STRONG)


class _Body(httpx.SyncByteStream):
    """A response body cut by a ``ReadError`` after ``drop`` bytes, if given."""

    def __init__(self, data, drop):
        self._data, self._drop = data, drop

    def __iter__(self):
        yield self._data[: self._drop]
        if self._drop is not None:
            raise httpx.ReadError("connection reset")


def _answer(status=200, body=b"", *, drop=None, size=None, **headers):
    """A scripted answer; ``Content-Length`` is ``size``, else the body's."""
    headers = {name.replace("_", "-"): value for name, value in headers.items()}
    headers["Content-Length"] = str(len(body) if size is None else size)
    return status, headers, body, drop


def _rest(start, **headers):
    total = len(BODY)
    range_ = f"bytes {start}-{total - 1}/{total}"
    return _answer(206, BODY[start:], content_range=range_, **headers)


@pytest.mark.parametrize(
    "script, expected, sent, waits",
    [
        pytest.param(
            [_answer(body=BODY, drop=1024, etag=STRONG), _rest(1024, etag=STRONG)],
            BODY,
            [FRESH, RESUMED],
            [],
            id="drop-resumed-by-etag",
        ),
        pytest.param(
            [
                _answer(body=BODY[:1024], size=len(BODY), last_modified=MODIFIED),
                _rest(1024, last_modified=MODIFIED),
            ],
            BODY,
            [FRESH, ("bytes=1024-", MODIFIED)],
            [],
            id="short-body-resumed-by-last-modified",
        ),
        pytest.param(
            [_answer(body=BODY, drop=1024, etag=WEAK), _answer(body=BODY)],
            BODY,
            [FRESH, FRESH],
            [1.0],
            id="weak-etag-restarts",
        ),
        pytest.param(
            [_answer(body=BODY, drop=1024, etag=STRONG), _answer(body=BODY[::-1])],
            BODY[::-1],
            [FRESH, RESUMED],
            [],
            id="changed-file-restarts",
        ),
        pytest.param(
            [
                _answer(body=BODY, drop=1024, etag=STRONG),
                _rest(512, etag=STRONG),
                _answer(body=BODY),
            ],
            BODY,
            [FRESH, RESUMED, FRESH],
            [1.0],
            id="misaligned-resume-restarts",
        ),
        pytest.param(
            [_answer(body=gzip.compress(BODY, mtime=0), content_encoding="gzip")],
            BODY,
            [FRESH],
            [],
            id="gzip-encoded",
        ),
        pytest.param(
            [_answer(503), httpx.ReadTimeout, _answer(body=BODY)],
            BODY,
            [FRESH] * 3,
            [1.0, 2.0],
            id="transient-failures",
        ),
        pytest.param(
            [_answer(503)] * 3,
            "HTTP 503 Service Unavailable (3 requests)",
            [FRESH] * 3,
            [1.0, 2.0],
            id="retry-status-thrice",
        ),
        pytest.param(
            [_answer(body=BODY[:1024], size=len(BODY))] * 3,
            "body ended at 1024 of 2048 bytes (3 requests)",
            [FRESH] * 3,
            [1.0, 2.0],
            id="unpinned-short-body-thrice",
        ),
        pytest.param(
            [httpx.ConnectTimeout],
            "ConnectTimeout: timed out",
            [FRESH],
            [],
            id="connect-timeout",
        ),
        pytest.param([_answer(404)], "HTTP 404 Not Found", [FRESH], [], id="not-found"),
        pytest.param(
            [_answer(body=BODY, drop=1, etag=STRONG)] * 10,
            "ReadError: connection reset (10 requests)",
            [FRESH] + [("bytes=1-", STRONG)] * 9,
            [],
            id="request-cap",
        ),
    ],
)
def test_download_retries_and_resumes(
    tmp_path, monkeypatch, script, expected, sent, waits
):
    requests, slept = [], []

    def handler(request):
        requests.append((request.headers.get("Range"), request.headers.get("If-Range")))
        step = script[len(requests) - 1]
        if isinstance(step, type):
            raise step("timed out", request=request)
        status, headers, body, drop = step
        return httpx.Response(status, headers=headers, stream=_Body(body, drop))

    monkeypatch.setattr(time, "sleep", slept.append)
    path = tmp_path / "feed.zip"
    with _http.client(transport=httpx.MockTransport(handler)) as client:
        if isinstance(expected, bytes):
            digest = _http.download(client, URL, path)
            assert path.read_bytes() == expected
            assert digest == hashlib.sha256(expected).hexdigest()
        else:
            with pytest.raises(DownloadError) as caught:
                _http.download(client, URL, path)
            assert str(caught.value) == f"{URL}: {expected}"
            assert not list(tmp_path.iterdir())
    assert (requests, slept) == (sent, waits)


@pytest.mark.parametrize(
    "script, total",
    [
        pytest.param([_answer(body=BODY)], len(BODY), id="content-length"),
        pytest.param(
            [(200, {"ETag": STRONG}, BODY, 1024), _rest(1024, etag=STRONG)],
            len(BODY),
            id="content-range",
        ),
        pytest.param(
            [_answer(body=gzip.compress(BODY, mtime=0), content_encoding="gzip")],
            None,
            id="gzip-encoded",
        ),
        pytest.param([(200, {}, BODY, None)], None, id="chunked"),
    ],
)
def test_download_reports_the_bytes_written_and_the_total(tmp_path, script, total):
    answers, calls = iter(script), []

    def handler(request):
        status, headers, body, drop = next(answers)
        return httpx.Response(status, headers=headers, stream=_Body(body, drop))

    with _http.client(transport=httpx.MockTransport(handler)) as client:
        _http.download(
            client, URL, tmp_path / "feed.zip", progress=lambda *c: calls.append(c)
        )
    assert calls[-1] == (len(BODY), total)


@pytest.mark.parametrize(
    "fragment, member",
    [
        pytest.param(
            "#3/google_transit.zip", ("archive", "3/google_transit.zip"), id="archive"
        ),
        pytest.param("#feed%20dir/", ("folder", "feed dir"), id="folder"),
        pytest.param("#/abs.zip", None, id="absolute"),
        pytest.param("#a/../b.zip", None, id="parent"),
        pytest.param("", None, id="no-fragment"),
    ],
)
def test_split_fragment_names_the_member(fragment, member):
    url = "https://data.example/gtfs.zip"
    assert split_fragment(url + fragment) == (url, member)


def _zip_bytes(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


NESTED = {"agency.txt": b"agency_id\na\n", "stops.txt": b"stop_id\ns1\n"}
INNER = _zip_bytes(NESTED)
OVER = (
    "declares {} uncompressed bytes, over the 10 budget "
    "(raise max_total_bytes to read it)"
)


@pytest.mark.parametrize(
    "member, budget, expected",
    [
        pytest.param(
            ("archive", "1/google_transit.zip"), None, INNER, id="nested-as-is"
        ),
        pytest.param(
            ("archive", "2/google_transit.zip"), None, NESTED, id="nested-dir"
        ),
        pytest.param(("folder", "data"), None, NESTED, id="folder"),
        pytest.param(
            ("archive", "9/google_transit.zip"),
            None,
            "inner archive '9/google_transit.zip' not in the archive",
            id="missing-archive",
        ),
        pytest.param(
            ("folder", "nothere"), None, "no GTFS files under 'nothere'", id="none"
        ),
        pytest.param(
            ("archive", "1/google_transit.zip"),
            10,
            "inner archive '1/google_transit.zip' " + OVER.format(len(INNER)),
            id="nested-over-budget",
        ),
        pytest.param(
            ("folder", "data"),
            10,
            "the feed under 'data' " + OVER.format(sum(map(len, NESTED.values()))),
            id="folder-over-budget",
        ),
    ],
)
def test_extract_feed_writes_the_named_member(tmp_path, member, budget, expected):
    in_folder = {f"v2/{name}": content for name, content in NESTED.items()}
    outer = tmp_path / "outer.zip"
    outer.write_bytes(
        _zip_bytes(
            {
                "1/google_transit.zip": INNER,
                "2/google_transit.zip": _zip_bytes(
                    {
                        **in_folder,
                        "v2/old/stops.txt": b"stop_id\n",
                        "__MACOSX/v2/._stops.txt": b"",
                        "readme.txt": b"PTV",
                    }
                ),
                **{f"data/{name}": content for name, content in NESTED.items()},
                "readme.txt": b"PTV",
            }
        )
    )
    target = tmp_path / "feed" / "latest.zip"
    if isinstance(expected, str):
        with pytest.raises(DownloadError) as caught:
            extract_feed(outer, member, target, budget)
        assert str(caught.value) == expected
        assert not list(target.parent.iterdir())
        return
    digest = extract_feed(outer, member, target, budget)
    assert digest == hashlib.sha256(target.read_bytes()).hexdigest()
    if isinstance(expected, bytes):
        assert target.read_bytes() == expected
    else:
        with zipfile.ZipFile(target) as feed:
            assert {name: feed.read(name) for name in feed.namelist()} == expected
    assert [path.name for path in target.parent.iterdir()] == ["latest.zip"]


def test_the_cache_is_listed_and_cleared_by_age_feed_and_whole(tmp_path):
    import transitio.cache

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=_zip_bytes({"a.txt": b"1"}))
    )
    for client in ("mdb", "atlas"):
        owner, download = _moving_target(client, tmp_path, transport)
        with owner:
            download()
    table = transitio.cache.info(tmp_path)
    assert sorted(table["feed_id"]) == ["f-x", "mdb-7"]
    total = int(table["archive_bytes"].sum() + table["outputs_bytes"].sum())
    assert table.attrs["logical_bytes"] == total > 0
    # Last used 25 and 23 hours ago: only the first is past a day.
    cache, now = _cache.FeedCache(tmp_path), datetime.datetime.now(
        datetime.timezone.utc
    )
    for feed_id, hours in (("mdb-7", 25), ("f-x", 23)):
        (version,) = cache.versions(feed_id)
        used = (now - datetime.timedelta(hours=hours)).isoformat()

        def age(sidecar, used=used):
            sidecar["last_used_at"] = used

        cache.update(version, age)
    assert transitio.cache.clear(tmp_path, older_than=datetime.timedelta(days=1)) > 0
    assert list(transitio.cache.info(tmp_path)["feed_id"]) == ["f-x"]
    assert transitio.cache.clear(tmp_path, feeds=["mdb-7"]) == 0
    # A folder an older transitio left goes with the rest; the locks and the
    # empty staging folder stay.
    (tmp_path / "gtfs" / "mdb-1").mkdir()
    (tmp_path / "gtfs" / "mdb-1" / "latest.zip").write_bytes(b"old")
    assert transitio.cache.clear(tmp_path) > 0
    left = sorted(p.name for p in (tmp_path / "gtfs").iterdir())
    assert left == [".locks", ".staging"]


def test_clear_waits_for_a_fetch_holding_a_feeds_lock(tmp_path):
    import threading

    import transitio.cache

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=_zip_bytes({"a.txt": b"1"}))
    )
    owner, download = _moving_target("mdb", tmp_path, transport)
    with owner:
        download()
    held, order = threading.Event(), []

    def fetching():
        with _cache.FeedCache(tmp_path).lock("mdb-7"):
            held.set()
            time.sleep(0.2)
            order.append("released")

    thread = threading.Thread(target=fetching)
    thread.start()
    held.wait()
    transitio.cache.clear(tmp_path)
    order.append("cleared")
    thread.join()
    assert order == ["released", "cleared"]


@pytest.mark.skipif(os.name == "nt", reason="each feed keeps its own copy on Windows")
def test_identical_archives_of_two_feeds_are_stored_once(tmp_path):
    import transitio.cache

    payload, requests = _zip_bytes({"a.txt": b"1"}), []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=payload)

    def download_both():
        paths = []
        for client in ("mdb", "atlas"):
            owner, download = _moving_target(
                client, tmp_path, httpx.MockTransport(handler)
            )
            with owner:
                paths.append(download())
        return paths

    mdb, atlas = download_both()
    blob = tmp_path / "gtfs" / "blobs" / mdb.name
    assert os.path.samefile(mdb, atlas) and os.path.samefile(blob, mdb)
    table = transitio.cache.info(tmp_path)
    assert list(table["shared_with"]) == [1, 1]
    assert table.attrs["saved_bytes"] == len(payload)
    # The shared file damaged: the first feed downloads into a new blob and
    # the second links to that, with no download.
    blob.chmod(0o644)
    blob.write_bytes(b"damaged")
    assert [p.read_bytes() for p in download_both()] == [payload] * 2
    assert len(requests) == 3
    # Clearing one feed keeps the blob for the other; the damaged file goes.
    assert transitio.cache.clear(tmp_path, feeds=["mdb-7"]) > 0
    assert os.path.samefile(blob, atlas)
    assert [p.name for p in blob.parent.iterdir()] == [blob.name]


@pytest.mark.skipif(os.name == "nt", reason="each feed keeps its own copy on Windows")
def test_where_links_fail_each_feed_keeps_its_own_copy(tmp_path, monkeypatch):
    import transitio.cache

    def refuse(*args, **kwargs):
        raise OSError("hard links not supported")

    monkeypatch.setattr(os, "link", refuse)
    payload = _zip_bytes({"a.txt": b"1"})
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=payload)
    )
    paths = []
    for client in ("mdb", "atlas"):
        owner, download = _moving_target(client, tmp_path, transport)
        with owner:
            paths.append(download())
    assert not os.path.samefile(*paths)
    table = transitio.cache.info(tmp_path)
    assert list(table["shared_with"]) == [0, 0] and table.attrs["saved_bytes"] == 0


@pytest.mark.skipif(os.name == "nt", reason="each feed keeps its own copy on Windows")
def test_a_damaged_version_is_copied_from_its_blob_where_links_fail(
    tmp_path, monkeypatch
):
    payload, requests = _zip_bytes({"a.txt": b"1"}), []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=payload)

    def download(client):
        owner, fetch = _moving_target(client, tmp_path, httpx.MockTransport(handler))
        with owner:
            return fetch()

    mdb, atlas = download("mdb"), download("atlas")
    # The shared file damaged: the first feed downloads a new blob.
    mdb.chmod(0o644)
    mdb.write_bytes(b"damaged")
    download("mdb")

    def refuse(*args, **kwargs):
        raise OSError("hard links not supported")

    monkeypatch.setattr(os, "link", refuse)
    # The second, unable to link to it, takes a copy without a download.
    assert download("atlas").read_bytes() == payload and len(requests) == 3
    assert not os.access(atlas, os.W_OK)
