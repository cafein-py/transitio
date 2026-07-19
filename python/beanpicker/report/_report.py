"""Build and render merged validation reports.

The grouped JSON follows the canonical gtfs-validator report convention
(notices grouped by code with ``totalNotices`` and ``sampleNotices``), so
beanpicker's local tier-two notices and a hosted canonical report merge
into one comparable document.
"""

from __future__ import annotations

import datetime
import html
import re

_SEVERITY_ORDER = {"ERROR": 0, "WARNING": 1, "INFO": 2}
_MAX_SAMPLES = 50


def _md(value):
    """Neutralise Markdown syntax and HTML in an untrusted value."""
    text = str(value).replace("\n", " ").replace("\r", " ")
    text = re.sub(r"([\\`*_{}\[\]()#+!|~-])", r"\\\1", text)
    text = text.replace("<", "&lt;").replace(">", "&gt;")
    return text if len(text) <= 200 else text[:200] + "\u2026"


def build_report(validation, *, hosted=None, provenance=None):
    """Merge a ``validate_feed`` result with an optional hosted report.

    Parameters
    ----------
    validation : dict
        The ``beanpicker.validate_feed`` result.
    hosted : dict, optional
        A hosted canonical-validator JSON report for the same dataset
        version (``MobilityDatabase.validation_report``); its notice groups
        are merged by code.
    provenance : dict, optional
        Provenance block (feed/dataset IDs, source URL, checksums,
        retrieval timestamp) — typically the download sidecar contents —
        embedded verbatim for reproducibility.

    Returns
    -------
    dict
        ``{"summary": {...}, "notices": [...]}``. Each notice group carries
        ``code``, ``severity``, ``source`` (``local``/``hosted``/``both``),
        ``totalNotices`` (local occurrences), ``hostedTotalNotices`` when
        the hosted report has the same code, and up to 50
        ``sampleNotices`` context mappings.
    """
    groups = {}
    for notice in validation.get("notices", []):
        key = notice["code"]
        group = groups.setdefault(
            key,
            {
                "code": key,
                "severity": notice["severity"],
                "source": "local",
                "totalNotices": 0,
                "sampleNotices": [],
            },
        )
        group["totalNotices"] += 1
        if len(group["sampleNotices"]) < _MAX_SAMPLES:
            group["sampleNotices"].append(notice.get("context", {}))

    for hosted_group in (hosted or {}).get("notices", []):
        code = hosted_group.get("code")
        if code is None:
            continue
        total = hosted_group.get("totalNotices", 0)
        if code in groups:
            groups[code]["source"] = "both"
            groups[code]["hostedTotalNotices"] = total
            groups[code]["hostedSampleNotices"] = hosted_group.get("sampleNotices", [])[
                :_MAX_SAMPLES
            ]
        else:
            # Hosted-only groups keep the hosted count as totalNotices so
            # canonical consumers see the real occurrence count;
            # localTotalNotices records that this tier saw none.
            groups[code] = {
                "code": code,
                "severity": str(hosted_group.get("severity", "INFO")).upper(),
                "source": "hosted",
                "totalNotices": total,
                "localTotalNotices": 0,
                "hostedTotalNotices": total,
                "sampleNotices": hosted_group.get("sampleNotices", [])[:_MAX_SAMPLES],
            }

    notices = sorted(
        groups.values(),
        key=lambda group: (
            _SEVERITY_ORDER.get(group["severity"], 3),
            -(group["totalNotices"] + group.get("hostedTotalNotices", 0)),
            group["code"],
        ),
    )
    counts = {"errors": 0, "warnings": 0, "infos": 0}
    bucket = {"ERROR": "errors", "WARNING": "warnings", "INFO": "infos"}
    for group in notices:
        if group["source"] != "hosted":
            name = bucket.get(group["severity"])
            if name:
                counts[name] += group["totalNotices"]
    # Validator-side sampling means retained notices are lower bounds.
    suppressed = sum(
        notice.get("context", {}).get("suppressedCount", 0)
        for notice in validation.get("notices", [])
        if notice["code"] == "notice_limit_reached"
    )
    return {
        "summary": {
            "validator": _validator_stamp(),
            "generatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "counts": counts,
            "suppressedNotices": suppressed,
            "countsAreLowerBounds": suppressed > 0,
            "serviceWindow": validation.get("service_window"),
            "rowCounts": validation.get("row_counts", {}),
            "provenance": provenance,
            "hostedReportIncluded": hosted is not None,
        },
        "notices": notices,
    }


def _validator_stamp():
    import beanpicker

    return {"name": "beanpicker", "version": beanpicker.__version__}


def render_markdown(report):
    """Render a merged report as Markdown."""
    summary = report["summary"]
    lines = [
        "# GTFS validation report",
        "",
        f"beanpicker {summary['validator']['version']} — {summary['generatedAt']}",
        "",
        f"**Errors:** {summary['counts']['errors']}  ",
        f"**Warnings:** {summary['counts']['warnings']}  ",
        f"**Infos:** {summary['counts']['infos']}",
        "",
    ]
    if summary.get("countsAreLowerBounds"):
        lines += [
            f"Notice sampling was active ({summary['suppressedNotices']} "
            "suppressed); counts are lower bounds.",
            "",
        ]
    if summary.get("serviceWindow"):
        start, end = summary["serviceWindow"]
        lines += [f"Computed service window: {_md(start)} – {_md(end)}", ""]
    if summary.get("provenance"):
        lines.append("## Provenance")
        lines.append("")
        for key, value in sorted(summary["provenance"].items()):
            lines.append(f"- {_md(key)}: {_md(value)}")
        lines.append("")
    lines += ["## Notices", ""]
    if not report["notices"]:
        lines.append("No notices.")
    else:
        lines.append("| code | severity | source | local | hosted |")
        lines.append("| --- | --- | --- | --- | --- |")
        for group in report["notices"]:
            hosted_total = group.get("hostedTotalNotices", "")
            local_total = group.get("localTotalNotices", group["totalNotices"])
            lines.append(
                f"| {_md(group['code'])} | {_md(group['severity'])} "
                f"| {_md(group['source'])} "
                f"| {_md(local_total)} | {_md(hosted_total)} |"
            )
    return "\n".join(lines) + "\n"


def render_html(report):
    """Render a merged report as a self-contained HTML page."""
    summary = report["summary"]
    e = html.escape
    rows = []
    for group in report["notices"]:
        local_total = group.get("localTotalNotices", group["totalNotices"])
        rows.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                e(str(group["code"])),
                e(str(group["severity"])),
                e(str(group["source"])),
                e(str(local_total)),
                e(str(group.get("hostedTotalNotices", ""))),
            )
        )
    sampling = ""
    if summary.get("countsAreLowerBounds"):
        sampling = (
            f"<p><em>Notice sampling was active "
            f"({e(str(summary['suppressedNotices']))} suppressed); "
            "counts are lower bounds.</em></p>"
        )
    window = ""
    if summary.get("serviceWindow"):
        start, end = summary["serviceWindow"]
        window = f"<p>Computed service window: {e(start)} – {e(end)}</p>"
    provenance = ""
    if summary.get("provenance"):
        items = "".join(
            f"<li>{e(str(key))}: {e(str(value))}</li>"
            for key, value in sorted(summary["provenance"].items())
        )
        provenance = f"<h2>Provenance</h2><ul>{items}</ul>"
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>GTFS validation report</title>"
        "<style>body{font-family:sans-serif;margin:2em}"
        "table{border-collapse:collapse}td,th{border:1px solid #999;"
        "padding:4px 8px}</style></head><body>"
        f"<h1>GTFS validation report</h1>"
        f"<p>beanpicker {e(summary['validator']['version'])} — "
        f"{e(summary['generatedAt'])}</p>"
        f"<p>Errors: {summary['counts']['errors']} · "
        f"Warnings: {summary['counts']['warnings']} · "
        f"Infos: {summary['counts']['infos']}</p>"
        f"{sampling}{window}{provenance}"
        "<h2>Notices</h2>"
        "<table><tr><th>code</th><th>severity</th><th>source</th>"
        "<th>local</th><th>hosted</th></tr>"
        f"{''.join(rows)}</table></body></html>"
    )
