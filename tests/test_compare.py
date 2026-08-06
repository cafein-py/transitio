import hashlib
import io
import pathlib
import zipfile

import httpx
import pytest

pytest.importorskip("transitio._core")

from transitio.gtfs import compare_feed_history, compare_feeds  # noqa: E402
from transitio.gtfs._compare import _area_overlap  # noqa: E402
from transitio.report import (  # noqa: E402
    render_comparison_html,
    render_comparison_markdown,
)

FEED = {
    "agency.txt": (
        "agency_id,agency_name,agency_url,agency_timezone\n"
        "hsl,HSL,https://hsl.fi,Europe/Helsinki\n"
    ),
    "stops.txt": (
        "stop_id,stop_name,stop_lat,stop_lon\n"
        "s1,Kamppi,60.169,24.931\n"
        "s2,Steissi,60.171,24.941\n"
    ),
    "routes.txt": "route_id,agency_id,route_short_name,route_type\nr1,hsl,1,3\n",
    "trips.txt": "route_id,service_id,trip_id\nr1,wk,t1\n",
    "stop_times.txt": (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "t1,08:00:00,08:00:00,s1,1\n"
        "t1,08:05:00,08:05:00,s2,2\n"
    ),
    "calendar.txt": (
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
        "start_date,end_date\n"
        "wk,1,1,1,1,1,1,1,20260101,20261231\n"
    ),
}


def write_zip(path, files):
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return path


def test_degraded_candidate_ranks_second(tmp_path):
    healthy = write_zip(tmp_path / "healthy.zip", FEED)
    degraded = write_zip(
        tmp_path / "degraded.zip",
        dict(
            FEED,
            **{"calendar_dates.txt": "service_id,date,exception_type\nwk,20260601,2\n"},
        ),
    )
    result = compare_feeds([degraded, healthy], "20260601")
    assert result["winner"] == "healthy"
    assert result["ranking"] == ["healthy", "degraded"]
    degraded_row = next(
        row for row in result["candidates"] if row["label"] == "degraded"
    )
    assert degraded_row["score"][0] == 1  # unusable at the target
    assert result["thresholds"]["areaOverlapCaveat"] == 0.5


def test_identical_candidates_tie_break_on_label(tmp_path):
    a = write_zip(tmp_path / "a.zip", FEED)
    b = write_zip(tmp_path / "b.zip", FEED)
    result = compare_feeds([b, a], "20260601")
    assert result["ranking"] == ["a", "b"]
    overlaps = [row["areaOverlap"] for row in result["candidates"]]
    assert overlaps == [1.0, 1.0]
    assert result["caveats"] == []


def test_input_validation(tmp_path):
    feed = write_zip(tmp_path / "a.zip", FEED)
    with pytest.raises(ValueError):
        compare_feeds([feed], "20260601")
    other = write_zip(tmp_path / "b.zip", FEED)
    with pytest.raises(ValueError):
        compare_feeds([feed, other], "20260601", labels=["x", "x"])
    with pytest.raises(ValueError):
        compare_feeds([feed, other], "June first")


def test_truncated_candidate_never_flattered(tmp_path):
    # The bigger feed gets truncated by the shared budget; its
    # deceptively small counts must rank it last, with a constructible
    # score despite the null moment.
    small = write_zip(tmp_path / "small.zip", FEED)
    big = write_zip(
        tmp_path / "big.zip",
        dict(
            FEED,
            **{
                "stop_times.txt": (
                    "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                    "t1,08:00:00,08:00:00,s1,1\n"
                    "t1,08:02:00,08:02:00,s2,2\n"
                    "t1,08:05:00,08:05:00,s1,3\n"
                )
            },
        ),
    )
    result = compare_feeds([big, small], "20260601", max_rows=2)
    big_row = next(row for row in result["candidates"] if row["label"] == "big")
    assert big_row["unreliableCounts"] is True
    assert big_row["moment"] is None
    assert isinstance(big_row["score"], list)
    assert result["winner"] == "small"


def test_truncated_transfers_never_flatter_a_usable_candidate(tmp_path):
    # Both candidates stay fully usable at the target; the truncated
    # transfers table retains MORE rows than the complete candidate has,
    # and must still rank second on the unreliable-counts component.
    modest = write_zip(
        tmp_path / "modest.zip",
        dict(
            FEED,
            **{"transfers.txt": "from_stop_id,to_stop_id,transfer_type\ns1,s2,0\n"},
        ),
    )
    truncated = write_zip(
        tmp_path / "truncated.zip",
        dict(
            FEED,
            **{
                "transfers.txt": (
                    "from_stop_id,to_stop_id,transfer_type\n"
                    "s1,s2,0\ns2,s1,0\ns1,s1,0\n"
                )
            },
        ),
    )
    result = compare_feeds([truncated, modest], "20260601", max_rows=2)
    rows = {row["label"]: row for row in result["candidates"]}
    assert rows["truncated"]["moment"] is not None  # still usable
    assert rows["truncated"]["unreliableCounts"] is True
    assert rows["truncated"]["transfers"] > rows["modest"]["transfers"]
    assert result["winner"] == "modest"


def test_sampling_suppression_ranks_after_complete_counts(tmp_path):
    clean = write_zip(tmp_path / "clean.zip", FEED)
    sampled = write_zip(
        tmp_path / "sampled.zip",
        dict(
            FEED,
            **{
                "stops.txt": (
                    "stop_id,stop_name,stop_lat,stop_lon\n"
                    "s1, Kamppi,60.169,24.931\n"
                    "s2, Steissi,60.171,24.941\n"
                )
            },
        ),
    )
    result = compare_feeds([sampled, clean], "20260601", max_notices_per_file=1)
    rows = {row["label"]: row for row in result["candidates"]}
    assert rows["sampled"]["moment"] is not None
    assert rows["sampled"]["unreliableCounts"] is True
    assert result["winner"] == "clean"


def test_disjoint_area_flags_caveat(tmp_path):
    local = write_zip(tmp_path / "local.zip", FEED)
    faraway = write_zip(
        tmp_path / "faraway.zip",
        dict(
            FEED,
            **{
                "stops.txt": (
                    "stop_id,stop_name,stop_lat,stop_lon\n"
                    "s1,Alpha,50.100,14.400\n"
                    "s2,Beta,50.110,14.410\n"
                )
            },
        ),
    )
    result = compare_feeds([local, faraway], "20260601")
    overlaps = {row["label"]: row["areaOverlap"] for row in result["candidates"]}
    assert overlaps["local"] == 0.0
    assert overlaps["faraway"] == 0.0
    assert len(result["caveats"]) == 2


def test_area_overlap_degenerate_and_nested():
    def row(label, bounds):
        return {"label": label, "stopBounds": bounds}

    point = row("p", [24.9, 60.1, 24.9, 60.1])
    same = row("q", [24.9, 60.1, 24.9, 60.1])
    other = row("r", [25.0, 60.2, 25.0, 60.2])
    assert _area_overlap(point, [point, same]) == 1.0
    assert _area_overlap(point, [point, other]) == 0.0
    outer = row("outer", [24.0, 60.0, 25.0, 61.0])
    inner = row("inner", [24.25, 60.25, 24.75, 60.75])
    assert _area_overlap(inner, [inner, outer]) == 0.25
    unbounded = row("u", None)
    assert _area_overlap(unbounded, [unbounded, outer]) is None


def zip_bytes(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def history_transport(datasets, bodies, requests=None):
    def handler(request):
        if requests is not None:
            requests.append(request.url.path)
        if request.url.path == "/v1/tokens":
            return httpx.Response(
                200, json={"access_token": "access-abc", "expires_in": 3600}
            )
        if request.url.path == "/v1/gtfs_feeds/mdb-1/datasets":
            return httpx.Response(200, json=datasets)
        name = request.url.path.rsplit("/", 1)[-1]
        if name in bodies:
            return httpx.Response(200, content=bodies[name])
        return httpx.Response(404, json={"detail": "not found"})

    return httpx.MockTransport(handler)


def dataset_record(dataset_id, downloaded_at, body):
    return {
        "id": dataset_id,
        "feed_id": "mdb-1",
        "hosted_url": f"https://files.example.com/{dataset_id}.zip",
        "downloaded_at": downloaded_at,
        "hash": hashlib.sha256(body).hexdigest(),
        "service_date_range_start": "2026-01-01",
        "service_date_range_end": "2026-12-31",
        "validation_report": None,
    }


def test_compare_feed_history_end_to_end(tmp_path, monkeypatch):
    healthy = zip_bytes(FEED)
    degraded = zip_bytes(
        dict(
            FEED,
            **{"calendar_dates.txt": "service_id,date,exception_type\nwk,20260601,2\n"},
        )
    )
    datasets = [
        dataset_record("mdb-1-202607", "2026-07-01T03:00:00Z", degraded),
        dataset_record("mdb-1-202606", "2026-06-01T03:00:00Z", healthy),
        # A version whose published range misses the day entirely.
        dict(
            dataset_record("mdb-1-202501", "2025-01-05T03:00:00Z", healthy),
            service_date_range_start="2025-01-01",
            service_date_range_end="2025-06-30",
        ),
    ]
    transport = history_transport(
        datasets, {"mdb-1-202607.zip": degraded, "mdb-1-202606.zip": healthy}
    )
    import transitio.catalog as catalog

    original = catalog.MobilityDatabase

    def patched(token=None, **kwargs):
        kwargs["cache_dir"] = tmp_path
        return original("refresh-xyz", transport=transport, **kwargs)

    monkeypatch.setattr(catalog, "MobilityDatabase", patched)
    result = compare_feed_history("mdb-1", "20260601")
    assert result["winner"] == "mdb-1-202606"
    assert set(result["ranking"]) == {"mdb-1-202606", "mdb-1-202607"}
    for row in result["candidates"]:
        assert row["provenance"]["datasetId"] == row["label"]
        # The path is the content-addressed install of the exact
        # ranked bytes.
        source = pathlib.Path(row["path"])
        assert source.exists()
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        assert digest == row["provenance"]["sha256"]
        assert source.name == f"{digest}.zip"

    with pytest.raises(ValueError, match="at least 2"):
        compare_feed_history("mdb-1", "20260601", limit=1)
    with pytest.raises(ValueError, match="no datasets cover"):
        compare_feed_history("mdb-1", "20250901")
    with pytest.raises(ValueError, match="mdb-1-202501"):
        compare_feed_history("mdb-1", "20250301")


def test_compare_feed_history_limit_caps_newest_first(tmp_path, monkeypatch):
    healthy = zip_bytes(FEED)
    requests = []
    # Served in scrambled order: the client sorts newest-first, the cap
    # keeps the two newest, and the excluded version is never fetched.
    datasets = [
        dataset_record("mdb-1-202604", "2026-04-01T03:00:00Z", healthy),
        dataset_record("mdb-1-202607", "2026-07-01T03:00:00Z", healthy),
        dataset_record("mdb-1-202606", "2026-06-01T03:00:00Z", healthy),
    ]
    transport = history_transport(
        datasets,
        {
            "mdb-1-202607.zip": healthy,
            "mdb-1-202606.zip": healthy,
            "mdb-1-202604.zip": healthy,
        },
        requests,
    )
    import transitio.catalog as catalog

    original = catalog.MobilityDatabase

    def patched(token=None, **kwargs):
        kwargs["cache_dir"] = tmp_path
        return original("refresh-xyz", transport=transport, **kwargs)

    monkeypatch.setattr(catalog, "MobilityDatabase", patched)
    result = compare_feed_history("mdb-1", "20260601", limit=2)
    assert set(result["ranking"]) == {"mdb-1-202607", "mdb-1-202606"}
    fetched = [path for path in requests if path.endswith(".zip")]
    assert "/mdb-1-202604.zip" not in [p[p.rfind("/") :] for p in fetched]
    for row in result["candidates"]:
        assert row["provenance"]["sha256"]


def test_compare_feed_history_needs_token(tmp_path, monkeypatch):
    from transitio.exceptions import MissingTokenError

    monkeypatch.delenv("MOBILITY_API_REFRESH_TOKEN", raising=False)
    with pytest.raises(MissingTokenError):
        compare_feed_history("mdb-1", "20260601", cache_dir=tmp_path)


def test_comparison_renderers(tmp_path):
    healthy = write_zip(tmp_path / "healthy.zip", FEED)
    degraded = write_zip(
        tmp_path / "degraded.zip",
        dict(
            FEED,
            **{"calendar_dates.txt": "service_id,date,exception_type\nwk,20260601,2\n"},
        ),
    )
    result = compare_feeds([healthy, degraded], "20260601", time="08:03")
    markdown = render_comparison_markdown(result)
    assert "GTFS feed comparison" in markdown
    assert "**healthy**" in markdown
    assert "| candidate |" in markdown
    page = render_comparison_html(result)
    assert page.startswith("<!doctype html>")
    assert "healthy" in page
