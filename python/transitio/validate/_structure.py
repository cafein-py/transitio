"""Structural validation over the Rust core."""

from __future__ import annotations

import json
import os


def validate_feed(
    path,
    *,
    max_entry_bytes=None,
    max_total_bytes=None,
    max_rows=None,
    max_columns=None,
    max_notices_per_file=None,
    reference_date=None,
):
    """Validate a GTFS zip and return the collected notices.

    The current rule set covers the structural tier (file presence, column
    shape, row shape, primary-key uniqueness) plus the field-format and
    referential-integrity tiers of transitio's routing-oriented catalogue:
    date/time/number/enum/timezone formats, required and conditionally
    required fields, coordinate sanity, calendar and frequency ranges,
    agency consistency, parent-station relations and cross-table foreign
    keys. Semantic rules (stop-time progression, calendar coverage, shapes,
    frequency overlaps) follow. Notice codes and severities use the
    canonical gtfs-validator naming; the canonical *grouped report*
    rendering that merges these notices with hosted validation reports is
    provided by the upcoming report module, not by this function's flat
    notice list.

    Parameters
    ----------
    path : str or pathlib.Path
        Path of the GTFS ``.zip`` file.
    max_entry_bytes : int, optional
        Uncompressed-size budget per archive entry (default 1 GiB). A file
        over budget is reported as ``unreadable_file`` and skipped.
    max_total_bytes : int, optional
        Cumulative uncompressed-size budget (default 2 GiB), enforced while
        reading.
    max_rows : int, optional
        Rows retained per file (default 20 million); reading past the cap
        raises a ``too_many_rows`` notice and stops for that file.
    max_columns : int, optional
        Column-count guard per file (default 1000).
    max_notices_per_file : int, optional
        Row-level notices retained per file (default 10000); further
        occurrences are counted in a ``notice_limit_reached`` notice.
    reference_date : str, optional
        ``YYYYMMDD`` day for calendar-expiry checks; defaults to today.

    Returns
    -------
    dict
        ``{"notices": [...], "row_counts": {...}, "service_window": ...}``.
        Each notice carries ``code``, ``severity``
        (``ERROR``/``WARNING``/``INFO``) and a ``context`` mapping with the
        notice-specific fields. ``service_window`` is the actual computed
        service-day span as a ``[start, end]`` pair of ``YYYYMMDD`` strings
        (or ``None``) — use it to verify the optimistic published dataset
        ranges from the catalog.

    Raises
    ------
    OSError
        If the file cannot be opened or is not a readable zip archive.
    """
    from transitio import _core

    # fspath (not str) preserves the platform path representation, so
    # non-UTF-8 filenames on Unix survive the boundary.
    return json.loads(
        _core.scan_feed(
            os.fspath(path),
            max_entry_bytes=max_entry_bytes,
            max_total_bytes=max_total_bytes,
            max_rows=max_rows,
            max_columns=max_columns,
            max_notices_per_file=max_notices_per_file,
            reference_date=reference_date,
        )
    )
