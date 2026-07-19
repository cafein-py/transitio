import os

import pytest

if os.environ.get("BEANPICKER_REQUIRE_TEST_DATA"):
    # In CI a missing native extension is a build failure, not a skip.
    import beanpicker._core  # noqa: F401
else:
    pytest.importorskip("beanpicker._core")

from beanpicker.report import build_report, render_html, render_markdown  # noqa: E402
from beanpicker.validate import validate_feed  # noqa: E402

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


def test_report_renders_for_helsinki(helsinki_gtfs):
    validation = validate_feed(helsinki_gtfs, reference_date=REFERENCE_DATE)
    report = build_report(validation, provenance={"source": "r5py sample v1.1.1"})
    assert report["summary"]["counts"]["errors"] >= 0
    markdown = render_markdown(report)
    assert "GTFS validation report" in markdown
    page = render_html(report)
    assert page.startswith("<!doctype html>")
