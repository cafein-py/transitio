"""Feed repair over the Rust core."""

from __future__ import annotations

import json
import os


def repair_feed(path, output, **options):
    """Repair a GTFS zip under the gtfstidy contract and write the result.

    Two passes run over the parsed feed: fixable optional fields are reset
    to their spec defaults, and entities with unfixable errors are dropped
    with cascading removals (a dropped stop removes its stop times; trips
    left unusable are removed with their frequencies), keeping the repaired
    feed referentially consistent. The repaired feed serves the same trips
    with the same attributes from the passenger's perspective; semantically
    ambiguous data is dropped, never reconstructed. Calling this function
    is the opt-in — ``validate_feed`` never modifies anything.

    Parameters
    ----------
    path : str or pathlib.Path
        Source GTFS ``.zip``.
    output : str or pathlib.Path
        Destination path for the repaired ``.zip`` (overwritten).
    **options
        The ``validate_feed`` keyword arguments (size/row/notice budgets,
        ``reference_date``).

    Returns
    -------
    dict
        ``{"fixes": [...], "remaining_notices": [...],
        "service_window": ...}`` — every fix record names its action
        (``default_value``, ``clear_reference``, ``drop_entity``), location
        and the notice code that triggered it; ``remaining_notices`` come
        from revalidating the repaired feed, so callers see exactly what
        was repaired, what was dropped around, and what remains.

    Raises
    ------
    OSError
        If the feed cannot be read, exceeds the scan budgets (repairing a
        truncated snapshot would silently lose data), or the output cannot
        be written.
    """
    from transitio import _core

    return json.loads(_core.repair_feed(os.fspath(path), os.fspath(output), **options))
