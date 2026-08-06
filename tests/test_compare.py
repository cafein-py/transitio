import zipfile

import pytest

pytest.importorskip("transitio._core")

from transitio.gtfs import compare_feeds  # noqa: E402
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
