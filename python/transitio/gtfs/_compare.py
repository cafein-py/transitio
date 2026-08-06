"""Rank candidate feeds by how well they serve a target date."""

from __future__ import annotations

import math
import pathlib

#: Below this share of bbox agreement with the other candidates, the
#: comparison flags a cross-area caveat.
AREA_OVERLAP_CAVEAT = 0.5

#: Readiness verdicts folded into the score, best first. Unknown or
#: null verdicts rank worst — no verdict is never an advantage.
READINESS_RANK = {
    "full": 0,
    "computable": 0,
    "partial": 1,
    "straight_line": 2,
    "absent": 2,
    "blocked": 3,
    None: 4,
}


def compare_feeds(candidates, when, *, time=None, labels=None, **budgets):
    """Compare candidate GTFS feeds for a user-specified date.

    Every candidate is validated with the date (and optional time) as
    the target moment, and the resulting measurements — activity at the
    moment, notice counts, readiness verdicts, transfers, service-window
    margin — are tabulated and ranked by a documented deterministic
    scoring tuple. ``transfers`` is the raw ``transfers.txt`` row
    count, a connections proxy only: timed interchange feasibility is
    not evaluated in this version, including when ``time`` is given.
    The winner is a recommendation; the full metric table is the
    product, and every scoring component is included per candidate so
    nothing about the ranking is hidden.

    Parameters
    ----------
    candidates : list of paths
        Two or more GTFS ``.zip`` files to compare.
    when : str or datetime.date
        The target day (``YYYYMMDD`` accepted).
    time : str, optional
        ``HH:MM``/``HH:MM:SS`` wall-clock time narrowing the moment.
    labels : list of str, optional
        Display labels, defaulting to the file stems; must be unique.
    **budgets
        The ``validate_feed`` budget keyword arguments.

    Returns
    -------
    dict
        ``{"when", "time", "candidates": [...], "ranking": [...],
        "winner", "caveats", "thresholds"}``. Candidate rows carry the
        metrics, their ``score`` tuple (ascending, lower is better) and
        ``areaOverlap`` against the other candidates' combined stop
        bounds. ``caveats`` lists comparison-level warnings (poor area
        overlap, unavailable bounds); ``thresholds`` echoes the
        constants the comparison applied.

    Raises
    ------
    ValueError
        For fewer than two candidates or non-unique labels.
    """
    from transitio.validate import validate_feed

    paths = [pathlib.Path(candidate) for candidate in candidates]
    if len(paths) < 2:
        raise ValueError("comparison needs at least two candidates")
    if labels is None:
        labels = [path.stem for path in paths]
    if len(labels) != len(paths):
        raise ValueError("labels must match candidates one to one")
    if len(set(labels)) != len(labels):
        raise ValueError(f"labels must be unique: {sorted(labels)}")
    when_ymd = _as_ymd(when)

    rows = []
    for label, path in zip(labels, paths):
        validation = validate_feed(
            path, reference_date=when_ymd, reference_time=time, **budgets
        )
        rows.append(_metric_row(label, path, validation, when_ymd))

    caveats = []
    for row in rows:
        row["areaOverlap"] = _area_overlap(row, rows)
        if row["stopBounds"] is None:
            caveats.append(f"{row['label']}: stop bounds unavailable")
        elif (
            row["areaOverlap"] is not None and row["areaOverlap"] < AREA_OVERLAP_CAVEAT
        ):
            caveats.append(
                f"{row['label']}: stop bounds overlap only "
                f"{row['areaOverlap']:.2f} of the other candidates "
                "(the comparison assumes same-area feeds)"
            )

    for row in rows:
        row["score"] = _score(row)
    ranking = sorted(rows, key=lambda row: row["score"])
    return {
        "when": when_ymd,
        "time": time,
        "candidates": rows,
        "ranking": [row["label"] for row in ranking],
        "winner": ranking[0]["label"],
        "caveats": caveats,
        "thresholds": {
            "areaOverlapCaveat": AREA_OVERLAP_CAVEAT,
            "readinessRank": {
                str(verdict): rank for verdict, rank in READINESS_RANK.items()
            },
        },
    }


def compare_feed_history(
    feed,
    when,
    *,
    time=None,
    refresh_token=None,
    cache_dir=None,
    directory=None,
    limit=None,
    **budgets,
):
    """Compare the historical dataset versions of one catalogued feed.

    Enumerates every Mobility Database dataset version whose published
    service range covers ``when``, downloads each (checksum-verified,
    cached, with the usual provenance sidecars), and delegates to
    :func:`compare_feeds` with the dataset ids as labels. Requires a
    catalog token — dataset history has no CSV fallback.

    Parameters
    ----------
    feed : Feed or str
        The catalogued feed, or its catalog ID.
    when : str or datetime.date
        The target day.
    time : str, optional
        Wall-clock time narrowing the moment.
    refresh_token, cache_dir, directory
        Passed to :class:`transitio.MobilityDatabase` / its download.
    limit : int, optional
        Compare at most this many covering versions, newest first;
        must be at least 2.
    **budgets
        The ``validate_feed`` budget keyword arguments.

    Returns
    -------
    dict
        The :func:`compare_feeds` result, with each candidate row
        carrying its dataset ``provenance`` (dataset id, catalogued
        sha256, download timestamp).

    Raises
    ------
    ValueError
        When ``limit`` is below 2, no dataset covers ``when``, or only
        one does (nothing to compare — the message names it).
    """
    import shutil
    import tempfile

    from transitio.catalog import MobilityDatabase
    from transitio.exceptions import DownloadError

    if limit is not None and limit < 2:
        raise ValueError("limit must be at least 2 to compare anything")
    # A real date object: the catalog's ISO parsing only accepts the
    # compact YYYYMMDD form from Python 3.11 on.
    import datetime

    when_ymd = _as_ymd(when)
    when_date = datetime.datetime.strptime(when_ymd, "%Y%m%d").date()
    workdir = None
    try:
        digests = {}
        with MobilityDatabase(refresh_token, cache_dir=cache_dir) as db:
            datasets = db.datasets_for(feed, when_date)
            if limit is not None:
                datasets = datasets[:limit]
            if not datasets:
                raise ValueError(f"no datasets cover {when_ymd}")
            if len(datasets) == 1:
                raise ValueError(
                    f"only dataset {datasets[0].id} covers {when_ymd}; "
                    "nothing to compare"
                )
            paths = []
            for index, dataset in enumerate(datasets):
                source = db.download(dataset, directory=directory)
                if workdir is None:
                    # Beside the downloads, so the final content-addressed
                    # install below is an atomic same-filesystem rename.
                    workdir = pathlib.Path(
                        tempfile.mkdtemp(prefix=".compare-", dir=source.parent)
                    )
                # The comparison reads a private snapshot, hashed while
                # copying — a shared cache path mutated between download
                # and ranking can neither change the compared bytes
                # unnoticed nor inherit the catalogued hash. Snapshot
                # names are index-based: dataset ids differing only in
                # case must not collide on case-insensitive filesystems.
                snapshot = workdir / f"{index}.zip"
                digest = _snapshot(source, snapshot)
                if dataset.hash and digest != dataset.hash:
                    raise DownloadError(
                        f"dataset {dataset.id} changed on disk: expected "
                        f"{dataset.hash}, found {digest}"
                    )
                digests[dataset.id] = digest
                paths.append(snapshot)
        result = compare_feeds(
            paths, when_ymd, time=time, labels=[d.id for d in datasets], **budgets
        )
        # Install each ranked snapshot durably under a content-addressed
        # name: the returned path holds the exact ranked bytes by
        # construction, immune to cache mutation races.
        import os

        installed = {}
        for dataset, snapshot in zip(datasets, paths):
            digest = digests[dataset.id]
            # A function-owned directory with content-addressed names:
            # nothing of the caller's is ever clobbered — a symlinked
            # directory is refused (the repo's accepted local-adversary
            # level; fd-level no-follow I/O is beyond it, as for the
            # repair staging), an existing entry is reused only when its
            # bytes verify, and a mismatching entry is never replaced.
            durable_dir = snapshot.parent.parent / "compared"
            durable_dir.mkdir(exist_ok=True)
            if durable_dir.is_symlink():
                raise DownloadError(f"refusing symlinked directory: {durable_dir}")
            durable = durable_dir / f"{digest}.zip"
            if durable.exists():
                if durable.is_symlink() or _sha256_of(durable) != digest:
                    raise DownloadError(
                        f"unexpected content at {durable}; refusing to replace it"
                    )
                snapshot.unlink()
            else:
                os.replace(snapshot, durable)
            installed[dataset.id] = durable
    finally:
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)
    provenance = {
        dataset.id: {
            "datasetId": dataset.id,
            "sha256": digests[dataset.id],
            "downloadedAt": (
                dataset.downloaded_at.isoformat() if dataset.downloaded_at else None
            ),
        }
        for dataset in datasets
    }
    for row in result["candidates"]:
        row["provenance"] = provenance[row["label"]]
        row["path"] = str(installed[row["label"]])
    return result


def _sha256_of(path):
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as reader:
        while chunk := reader.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot(source, target):
    """Copy ``source`` to ``target`` streaming through a SHA-256."""
    import hashlib

    digest = hashlib.sha256()
    with open(source, "rb") as reader, open(target, "wb") as writer:
        while chunk := reader.read(1 << 20):
            digest.update(chunk)
            writer.write(chunk)
    return digest.hexdigest()


def _as_ymd(when):
    text = str(when).replace("-", "")
    if len(text) != 8 or not text.isdigit():
        raise ValueError(f"invalid when: {when!r} (expected YYYYMMDD)")
    return text


def _window_margin(window, when_ymd):
    """Days from the target to the nearer window edge; negative outside."""
    if not window:
        return None
    import datetime

    day = datetime.datetime.strptime(when_ymd, "%Y%m%d").date()
    start = datetime.datetime.strptime(window[0], "%Y%m%d").date()
    end = datetime.datetime.strptime(window[1], "%Y%m%d").date()
    return min((day - start).days, (end - day).days)


def _metric_row(label, path, validation, when_ymd):
    notices = validation.get("notices", [])
    errors = sum(1 for n in notices if n["severity"] == "ERROR")
    warnings = sum(1 for n in notices if n["severity"] == "WARNING")
    suppressed = any(n["code"] == "notice_limit_reached" for n in notices)
    incomplete = validation.get("incomplete", [])
    readiness = validation.get("readiness") or {}
    distances = readiness.get("distances") or {}
    fares = readiness.get("fares") or {}
    return {
        "label": label,
        "path": str(path),
        "errors": errors,
        "warnings": warnings,
        "unreliableCounts": bool(suppressed or incomplete),
        "incomplete": incomplete,
        "moment": validation.get("moment"),
        "serviceWindow": validation.get("service_window"),
        "windowMarginDays": _window_margin(validation.get("service_window"), when_ymd),
        "transfers": validation.get("row_counts", {}).get("transfers.txt", 0),
        "distancesVerdict": distances.get("verdict"),
        "faresVerdict": fares.get("verdict"),
        "stopBounds": validation.get("stop_bounds"),
    }


def _area_overlap(row, rows):
    """IoU of this candidate's stop bbox against the union of all the
    OTHER candidates' bboxes — agreement, never self-inclusive share."""
    if row["stopBounds"] is None:
        return None
    others = [
        r["stopBounds"] for r in rows if r is not row and r["stopBounds"] is not None
    ]
    if not others:
        return None
    from shapely.geometry import box
    from shapely.ops import unary_union

    own = box(*row["stopBounds"])
    union_others = unary_union([box(*bounds) for bounds in others])
    union_all = unary_union([own, union_others])
    if union_all.area == 0.0:
        # Degenerate boxes (points/lines) collapse to empty geometries
        # under unary_union, so equality of the raw bounds is the only
        # meaningful overlap.
        matches = all(bounds == row["stopBounds"] for bounds in others)
        return 1.0 if matches else 0.0
    return own.intersection(union_others).area / union_all.area


def _score(row):
    """The documented deterministic ranking tuple, ascending.

    Components in order: unusable at the target (no service or trips —
    a null moment normalizes every activity metric to 0 and dominates
    via this flag), unreliable counts (sampling or truncation must
    never flatter a candidate), ERROR count, negated activity metrics,
    readiness ranks (distances then fares), WARNING count, negated
    transfers, negated window margin (unknown windows last), and the
    label as a stable tie-break.
    """
    moment = row["moment"]
    active_trips = (moment or {}).get("activeTrips", 0)
    active_routes = (moment or {}).get("activeRoutes", 0)
    stops_served = (moment or {}).get("stopsServed", 0)
    margin = row["windowMarginDays"]
    return [
        1 if moment is None or active_trips == 0 else 0,
        1 if row["unreliableCounts"] else 0,
        row["errors"],
        -active_trips,
        -active_routes,
        -stops_served,
        READINESS_RANK.get(row["distancesVerdict"], READINESS_RANK[None]),
        READINESS_RANK.get(row["faresVerdict"], READINESS_RANK[None]),
        row["warnings"],
        -row["transfers"],
        -margin if margin is not None else math.inf,
        row["label"],
    ]
