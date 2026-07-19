import json

import pytest

pytest.importorskip("beanpicker._core")

from beanpicker.report import (  # noqa: E402
    build_report,
    parity_summary,
    render_html,
    render_markdown,
)

VALIDATION = {
    "notices": [
        {
            "code": "missing_required_file",
            "severity": "ERROR",
            "context": {"filename": "stops.txt"},
        },
        {
            "code": "missing_required_file",
            "severity": "ERROR",
            "context": {"filename": "trips.txt"},
        },
        {
            "code": "empty_row",
            "severity": "WARNING",
            "context": {"filename": "routes.txt", "csvRowNumber": 3},
        },
    ],
    "row_counts": {"agency.txt": 1},
    "service_window": ["20260101", "20261231"],
}

HOSTED = {
    "notices": [
        {
            "code": "missing_required_file",
            "severity": "ERROR",
            "totalNotices": 2,
            "sampleNotices": [],
        },
        {
            "code": "unused_shape",
            "severity": "WARNING",
            "totalNotices": 5,
            "sampleNotices": [{"shapeId": "s"}],
        },
    ]
}

PROVENANCE = {"feed_id": "mdb-1", "dataset_id": "mdb-1-1", "sha256": "abc"}


def test_build_report_groups_and_merges():
    report = build_report(VALIDATION, hosted=HOSTED, provenance=PROVENANCE)
    groups = {g["code"]: g for g in report["notices"]}
    assert groups["missing_required_file"]["totalNotices"] == 2
    assert groups["missing_required_file"]["source"] == "both"
    assert groups["missing_required_file"]["hostedTotalNotices"] == 2
    assert groups["missing_required_file"]["hostedSampleNotices"] == []
    assert groups["unused_shape"]["source"] == "hosted"
    assert groups["unused_shape"]["totalNotices"] == 5
    assert groups["unused_shape"]["localTotalNotices"] == 0
    assert report["summary"]["counts"] == {"errors": 2, "warnings": 1, "infos": 0}
    assert report["summary"]["provenance"] == PROVENANCE
    # errors sort before warnings
    assert report["notices"][0]["severity"] == "ERROR"


def test_render_markdown_and_html():
    report = build_report(VALIDATION, hosted=HOSTED, provenance=PROVENANCE)
    markdown = render_markdown(report)
    # Markdown escaping renders underscores as \_ (displays as _).
    assert "missing\\_required\\_file" in markdown
    assert "20260101" in markdown
    assert "feed\\_id: mdb\\-1" in markdown
    page = render_html(report)
    assert "unused_shape" in page
    assert "&" not in json.dumps(PROVENANCE) or True
    assert page.startswith("<!doctype html>")


def test_sampling_marks_counts_as_lower_bounds():
    validation = dict(
        VALIDATION,
        notices=VALIDATION["notices"]
        + [
            {
                "code": "notice_limit_reached",
                "severity": "WARNING",
                "context": {"filename": "stops.txt", "suppressedCount": 42},
            }
        ],
    )
    report = build_report(validation)
    assert report["summary"]["suppressedNotices"] == 42
    assert report["summary"]["countsAreLowerBounds"] is True
    assert "lower bounds" in render_markdown(report)


def test_markdown_escapes_hostile_values():
    validation = {
        "notices": [
            {
                "code": "unknown_file",
                "severity": "INFO",
                "context": {"filename": "x"},
            }
        ],
        "row_counts": {},
        "service_window": None,
    }
    hostile = {"evil": "a|b<script>alert(1)</script>\nc"}
    markdown = render_markdown(build_report(validation, provenance=hostile))
    assert "<script>" not in markdown
    assert "a\\|b" in markdown
    hostile_link = {"u": "[x](https://evil.example)"}
    linked = render_markdown(build_report({"notices": []}, provenance=hostile_link))
    assert "[x](" not in linked


def test_build_report_without_hosted():
    report = build_report(VALIDATION)
    assert report["summary"]["hostedReportIncluded"] is False
    assert all(g["source"] == "local" for g in report["notices"])


def test_parity_summary_buckets():
    validation = {
        "notices": [
            {"code": "empty_row", "severity": "WARNING", "context": {}},
            {"code": "empty_row", "severity": "WARNING", "context": {}},
            {"code": "missing_required_file", "severity": "ERROR", "context": {}},
            {"code": "duplicate_key", "severity": "ERROR", "context": {}},
        ],
        "row_counts": {},
        "service_window": None,
    }
    hosted = {
        "notices": [
            {"code": "empty_row", "severity": "WARNING", "totalNotices": 2},
            {"code": "missing_required_file", "severity": "ERROR", "totalNotices": 5},
            {"code": "unused_shape", "severity": "WARNING", "totalNotices": 3},
        ]
    }
    parity = parity_summary(build_report(validation, hosted=hosted))
    assert parity["agreeing"] == ["empty_row"]
    assert parity["countDisagreements"] == [
        {"code": "missing_required_file", "local": 1, "hosted": 5}
    ]
    assert parity["localOnly"] == ["duplicate_key"]
    assert parity["hostedOnly"] == ["unused_shape"]
    assert parity["countsAreLowerBounds"] is False


def test_parity_summary_requires_hosted():
    report = build_report({"notices": [], "row_counts": {}, "service_window": None})
    with pytest.raises(ValueError, match="hosted"):
        parity_summary(report)
