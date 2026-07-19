import datetime
import hashlib
import json

import httpx
import pytest
from shapely.geometry import box

from transitio.catalog import TOKEN_ENV_VAR, MobilityDatabase
from transitio.catalog._client import _bounds
from transitio.catalog._models import Dataset, Feed
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
        bad_feed = Feed.from_api(dict(FEED_RECORD, id="../evil"))
        with pytest.raises(DownloadError, match="not safe"):
            db.download_latest(bad_feed)


def test_download_verifies_checksum_and_caches(tmp_path):
    payload = b"PK\x03\x04 fake gtfs zip"
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
    payload = b"PK\x03\x04 latest zip"
    requests = []
    routes = {
        "/feeds_v2.csv": lambda request: httpx.Response(200, text=CSV_BODY),
        "/mdb-10/latest.zip": lambda request: httpx.Response(200, content=payload),
    }
    with make_db(routes, tmp_path, requests, token=None) as db:
        with pytest.warns(UserWarning):
            (feed,) = db.search_feeds(country_code="FI")
        path = db.download_latest(feed)

    assert path.name == "latest.zip"
    assert path.read_bytes() == payload
    provenance = json.loads(path.with_suffix(".provenance.json").read_text())
    assert provenance["feed_id"] == "mdb-10"
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
