import os

import pytest

if os.environ.get("TRANSITIO_REQUIRE_TEST_DATA"):
    # In CI a missing native extension is a build failure, not a skip.
    import transitio._core  # noqa: F401
else:
    pytest.importorskip("transitio._core")

from transitio.report import build_report, render_html, render_markdown  # noqa: E402
from transitio.validate import validate_feed  # noqa: E402

# The sample feed's canonical query day, shared with cafein and r5py.
REFERENCE_DATE = "20220222"


def test_validate_helsinki_sample(helsinki_gtfs):
    report = validate_feed(helsinki_gtfs, reference_date=REFERENCE_DATE)
    assert report["row_counts"]["stops.txt"] > 100
    assert report["row_counts"]["stop_times.txt"] > 1000
    window = report["service_window"]
    assert window is not None
    start, end = window
    assert start <= REFERENCE_DATE <= end
    # A real production extract should carry no structural errors that
    # would make it unusable for routing.
    fatal = {
        "missing_required_file",
        "missing_calendar_and_calendar_date_files",
        "unreadable_file",
        "csv_parsing_failed",
    }
    hit = [n for n in report["notices"] if n["code"] in fatal]
    assert hit == [], hit


def test_moment_validation_on_helsinki_sample(helsinki_gtfs):
    report = validate_feed(
        helsinki_gtfs, reference_date=REFERENCE_DATE, reference_time="08:30"
    )
    # A February weekday morning: the feed as a whole serves the moment.
    feed_level = {
        "no_service_on_reference_date",
        "no_trips_at_reference_time",
        "service_level_below_baseline",
    }
    hit = [n for n in report["notices"] if n["code"] in feed_level]
    assert hit == [], hit
    # 2022-02-22 falls in the school winter-break week, so routes that
    # run only on school days are legitimately silent that day.
    inactive = [
        n for n in report["notices"] if n["code"] == "route_inactive_on_reference_date"
    ]
    assert inactive, "expected winter-break school routes to be flagged"
    for notice in inactive:
        assert notice["context"]["referenceDate"] == REFERENCE_DATE
        assert 0 < notice["context"]["activeDays"] <= notice["context"]["windowDays"]


def test_readiness_on_helsinki_sample(helsinki_gtfs):
    report = validate_feed(helsinki_gtfs)
    distances = report["readiness"]["distances"]
    predicted = distances["predicted"]
    assert sum(predicted.values()) > 0
    # A real production feed carries usable shapes for most trips.
    assert predicted["shape_dist"] + predicted["shape_linref"] > 0
    fares = report["readiness"]["fares"]
    assert fares["verdict"] in {"computable", "partial", "absent", "blocked"}


def test_readiness_distribution_matches_cafein(helsinki_gtfs):
    cafein_geometry = pytest.importorskip("cafein.geometry")
    from collections import Counter

    report = validate_feed(helsinki_gtfs)
    predicted = report["readiness"]["distances"]["predicted"]
    actual = Counter(
        provenance for _, _, provenance in cafein_geometry.trip_distances(helsinki_gtfs)
    )
    for tier in ("shape_dist", "shape_linref", "crow_fly"):
        assert predicted[tier] == actual.get(tier, 0), tier


def test_report_renders_for_helsinki(helsinki_gtfs):
    validation = validate_feed(helsinki_gtfs, reference_date=REFERENCE_DATE)
    report = build_report(validation, provenance={"source": "r5py sample v1.1.1"})
    assert report["summary"]["counts"]["errors"] >= 0
    markdown = render_markdown(report)
    assert "GTFS validation report" in markdown
    page = render_html(report)
    assert page.startswith("<!doctype html>")
