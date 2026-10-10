"""The one-call acquisition pipeline."""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import hashlib
import io
import json
import math
import os
import pathlib
import re
import shutil
import tempfile
import time
import warnings
import zipfile

# Decompressed budget for the pre-validation routes.txt peek; any real
# routes.txt is far smaller, and validation applies the full budgets later.
_MODES_BYTE_CAP = 64 * 1024 * 1024

# Seconds a conditional HEAD probe may take before it counts as unanswered.
_PROBE_TIMEOUT = 5.0

# Two delivered feeds are versions of one service when their route keys and
# their stops, at these decimals, overlap (Jaccard) this much or more.
_ROUTE_OVERLAP = 0.9
_STOP_OVERLAP = 0.8
_STOP_DECIMALS = 3

# Metres the place path grows the OSM area by: cafein's default snap distance.
_OSM_BUFFER_M = 1600

# The share of an area's land the feed index's places must cover for
# fetch(aoi=...) to select the area's feeds from the index.
_AREA_COVERAGE = 0.5
_NO_INDEX = "no compatible feed index is installed"

# Feed ids a delivered feed is named by as they are: lowercase ASCII, at
# most 100 characters, and none of the device names Windows reserves.
_PLAIN_NAME = re.compile(r"[a-z0-9][a-z0-9_~-]*")
_DEVICE_NAMES = frozenset(
    ["con", "prn", "aux", "nul"]
    + [f"{port}{n}" for port in ("com", "lpt") for n in range(1, 10)]
)

# The fields of a selection-record entry, in selection_table's column order.
_SELECTION_FIELDS = (
    "feed_id",
    "name",
    "decision",
    "reason",
    "note",
    "index_window",
    "feed_window",
    "same_as",
    "contained_in",
    "version_of",
    "duplicate_trips",
    "fetched_from",
    "download_errors",
    "cache",
    "stops_outside_osm",
    "path",
)


@dataclasses.dataclass
class FetchResult:
    """What the pipeline produced for one AOI."""

    osm_pbf: pathlib.Path | None
    feeds: list
    reports: list
    repairs: list
    skipped: list
    # How each feed's route selector was checked and applied on the index
    # paths (keys as fetch's Returns lists them); empty without tiers,
    # exclude or on_unknown="exclude".
    selections: list = dataclasses.field(default_factory=list)
    provenance: dict = None
    # The index snapshot the feeds were discovered from; None for the
    # catalogue path, which discovers by bounding box and has no snapshot.
    snapshot: str = None
    # {feed id: [ids of the delivered feeds carrying it]} for each feed left
    # out as contained, in selection order; empty otherwise.
    contained: dict = dataclasses.field(default_factory=dict)
    # One entry per candidate feed, in candidate order, with its decision;
    # ``skipped`` lists the same skips.
    selection: list = dataclasses.field(default_factory=list)
    # The WGS84 area the OSM extract was fetched for; None without one. A
    # failed extract download leaves it and osm_pbf None. On the place path,
    # its parts farther than 1.6 km from every delivered stop may lack OSM data.
    osm_area: object = None
    # The feeds an empty default view hides and the tiers that fetch them.
    view_note: str | None = None
    # The place parts the OSM extract leaves out and the delivered stops
    # outside its area, or why it was not fetched.
    osm_note: str | None = None
    # The index places the feeds were selected for: the place, the area's
    # parts, or none on the catalogue path.
    places: list = dataclasses.field(default_factory=list)

    def __iter__(self):  # convenient (pbf, feeds) unpacking
        return iter((self.osm_pbf, self.feeds))

    @property
    def paths(self):
        """``{feed id: path}`` of the delivered feeds, in the order of
        ``feeds``, so ``list(result.paths.values()) == result.feeds``."""
        ids = {
            entry["path"]: entry["feed_id"]
            for entry in self.selection
            if entry["decision"] == "delivered"
        }
        return {ids[path]: path for path in self.feeds}

    def selection_table(self):
        """The selection record as a ``pandas.DataFrame``, one row per
        candidate feed."""
        import pandas as pd

        return pd.DataFrame(self.selection, columns=list(_SELECTION_FIELDS))

    def to_cafein(self, **options):
        """Build a routable ``cafein.TransportNetwork`` from this result.

        The validated feeds and the OSM extract are handed to
        ``cafein.TransportNetwork.from_gtfs``; keyword arguments pass
        through (``walking_speed_kmph``, ``bounding_box``, ``ultra``,
        ...), and ``osm_pbf=None`` builds without the walking network.

        Requires the ``cafein`` package.
        """
        if not self.feeds:
            raise ValueError("no feeds to build a network from")
        try:
            import cafein
        except ImportError as error:
            raise ImportError(
                "the cafein package is required for to_cafein()"
            ) from error
        if self.osm_pbf is not None:
            options.setdefault("osm_pbf", os.fspath(self.osm_pbf))
        paths = [os.fspath(path) for path in self.feeds]
        return cafein.TransportNetwork.from_gtfs(paths, **options)

    def to_pyrosm(self, **options):
        """Open the OSM extract as a ``pyrosm.OSM`` reader.

        Keyword arguments pass through to ``pyrosm.OSM`` (for example
        ``bounding_box`` to read a sub-area of the cropped extract).
        """
        if self.osm_pbf is None:
            raise ValueError(
                "this result has no OSM extract: it was fetched with osm=False "
                "or the extract download failed"
            )
        from pyrosm import OSM

        return OSM(os.fspath(self.osm_pbf), **options)


def _feed_modes(path):
    """Coarse modes served by a feed, from its routes.txt with values and
    header names stripped as ``FeedEditor`` strips them; rows with extra
    fields are skipped.

    Returns ``None`` when routes.txt cannot be read (missing, over the
    byte budget, or malformed) so the caller can report the feed as
    undeterminable rather than silently unfiltered.
    """
    import pandas as pd

    from transitio.edit._editor import _normalise_table
    from transitio.gtfs._schedule import MODE_TYPES

    try:
        with zipfile.ZipFile(path) as archive:
            with archive.open("routes.txt") as handle:
                data = handle.read(_MODES_BYTE_CAP + 1)
        if len(data) > _MODES_BYTE_CAP:
            return None
        # Read headerless, so the header row sets the width: a row with
        # extra fields is skipped wherever it is, never read as an index.
        rows = pd.read_csv(
            io.BytesIO(data),
            header=None,
            dtype=str,
            keep_default_na=False,
            encoding="utf-8-sig",
            encoding_errors="replace",
            on_bad_lines="skip",
        )
        routes = _normalise_table(rows.iloc[1:].set_axis(list(rows.iloc[0]), axis=1))[0]
        values = routes.loc[:, routes.columns == "route_type"].to_numpy().ravel()
        types = {int(value) for value in set(values) if value.lstrip("-").isdigit()}
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return None
    return {mode for mode, accepted in MODE_TYPES.items() if types & accepted}


def _bbox_area(feed):
    """Bounding-box area of a feed's data, or ``None`` when unknown."""
    raw = feed.raw or {}
    bounding_box = (raw.get("latest_dataset") or {}).get("bounding_box") or {}
    values = []
    for key in (
        "minimum_longitude",
        "maximum_longitude",
        "minimum_latitude",
        "maximum_latitude",
    ):
        value = bounding_box.get(key, raw.get(f"location.bounding_box.{key}"))
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            return None
    min_lon, max_lon, min_lat, max_lat = values
    return abs(max_lon - min_lon) * abs(max_lat - min_lat)


def _rank(feed):
    """The plan's documented deterministic preference for overlapping feeds.

    Official before unofficial, active status before anything else, then
    spatial specificity (smaller data bounding box first, unknown extent
    last), with the feed ID as a stable tie-breaker.
    """
    area = _bbox_area(feed)
    return (
        not feed.official,
        feed.status != "active",
        area is None,
        area or 0.0,
        feed.id,
    )


def _from_recommendation(feeds, place, aoi, when):
    """``(place, when)`` for a fetch whose ``feeds`` may be a
    :class:`~transitio.index.Recommendation`: its place and day, refusing a
    ``place``, ``aoi`` or ``when`` that differs from them."""
    from transitio.catalog._models import as_date
    from transitio.index.recommend import Recommendation

    if not isinstance(feeds, Recommendation):
        return place, when
    if aoi is not None or (place is not None and place != feeds.place):
        raise ValueError(
            "feeds= is a recommendation, which names its place: pass no place= "
            "or aoi="
        )
    if when is not None and as_date(when) != (feeds.when or _today()):
        raise ValueError(
            f"feeds= is a recommendation for {feeds.when or _today()}, "
            f"not for {as_date(when)}: pass no when="
        )
    return feeds.place, feeds.when if when is None else when


def _today():
    return datetime.date.today()


def _window(start, end):
    """``[start, end]`` as ISO date strings, None when both are unknown."""
    if start is None and end is None:
        return None
    return [None if day is None else day.isoformat() for day in (start, end)]


def _service_window(validation):
    """The first and last dates of a validation report's computed service
    window, None for both when unknown."""
    if not validation["service_window"]:
        return None, None
    return tuple(
        datetime.datetime.strptime(value, "%Y%m%d").date()
        for value in validation["service_window"]
    )


def _misses(start, end, day, study):
    """Why a service window from ``start`` to ``end`` misses ``day``, or None.

    With a ``study`` day the window must cover it; otherwise it must only not
    end before it. An unknown bound (``None``) cannot miss.
    """
    if end is not None and end < day:
        return f"service ended {end.isoformat()}"
    if study and start is not None and start > day:
        return f"service starts {start.isoformat()}, after {day.isoformat()}"
    return None


def _idle(validation, day):
    """Whether a validation report proves that nothing runs on ``day``: its
    ``moment`` for the day counts no active trip and it carries the
    ``no_service_on_reference_date`` notice. A report without a moment for
    the day proves nothing."""
    moment = validation.get("moment") or {}
    return (
        moment.get("referenceDate") == day.strftime("%Y%m%d")
        and moment.get("activeTrips") == 0
        and any(
            notice.get("code") == "no_service_on_reference_date"
            for notice in validation.get("notices") or ()
        )
    )


def _missing_files(validation):
    """Why a validation report makes a feed unreadable, or None: the required
    files it lacks, and both calendar files when neither is present."""
    notices = validation["notices"]
    names = sorted(
        {
            notice["context"]["filename"]
            for notice in notices
            if notice.get("code") == "missing_required_file"
        }
    )
    parts = []
    if names:
        noun = "file" if len(names) == 1 else "files"
        parts.append(f"missing required {noun} {', '.join(names)}")
    if any(
        notice.get("code") == "missing_calendar_and_calendar_date_files"
        for notice in notices
    ):
        parts.append("missing calendar.txt and calendar_dates.txt")
    return "; ".join(parts) or None


def _entry(feed_id, name, index_window=None):
    """An undecided selection-record entry for one candidate feed."""
    entry = dict.fromkeys(_SELECTION_FIELDS)
    entry.update(
        feed_id=feed_id,
        name=name,
        index_window=index_window,
        same_as=[],
        contained_in=[],
    )
    return entry


def _skip(entry, reason, **fields):
    entry.update(decision="skipped", reason=reason, **fields)


def _note(entry, text):
    """Add ``text`` to an entry's note, after any note it has."""
    entry["note"] = text if entry["note"] is None else f"{entry['note']}; {text}"


def _dropped_note(report):
    """``"dropped <n> <file> rows whose <field> is not in <parent>"``,
    ``"dropped <n> trips.txt rows left with fewer than two stop_times"`` or
    ``"dropped <n> exact duplicate <file> rows"`` for the rows the crop of a
    feed left out (several joined with ``", "``), or None when it left out
    none."""
    parts = []
    for record in report["summary"].get("droppedRows") or ():
        dropped = f"dropped {record['rowCount']}"
        if record["code"] == "unusable_trip":
            text = f"{record['filename']} rows left with fewer than two stop_times"
        elif record["code"] == "duplicate_key":
            text = f"exact duplicate {record['filename']} rows"
        else:
            text = (
                f"{record['filename']} rows whose {record['fieldName']} is not in "
                f"{record['parentFilename']}"
            )
        parts.append(f"{dropped} {text}")
    return ", ".join(parts) or None


def _skipped(selection):
    """The ``(feed id, reason)`` pairs of the skipped entries."""
    return [
        (entry["feed_id"], entry["reason"])
        for entry in selection
        if entry["decision"] == "skipped"
    ]


class _SkipFeed(Exception):
    """A per-feed reason to skip, carried out of the shared processing with
    the feed's computed service window when it was validated; ``missed_day``
    when the feed does not run on the requested day."""

    def __init__(self, reason, window=None, missed_day=False):
        super().__init__(reason, window, missed_day)
        self.reason = reason
        self.window = window
        self.missed_day = missed_day

    def __str__(self):
        return self.reason


def _process_feed(
    path,
    *,
    geometry,
    tag=None,
    repair,
    crop,
    modes,
    day,
    study,
    hosted,
    budgets,
    routes=None,
    provenance=None,
    outputs=None,
    progress=None,
):
    """Crop, repair, mode-filter, validate and report one downloaded feed,
    writing what it makes beside it; the report carries ``provenance``.

    A feed whose validation finds a required file missing drops out on any
    day. The computed service window is tested against ``day`` (None tests
    nothing): with a ``study`` day it must cover the day and the validation
    report must not prove the day idle; otherwise it must not end before it.

    With ``outputs``, ``(cache, version)`` of the cached version at ``path``,
    what the crop, repair and validation make is stored with the version
    (:func:`_store_output`) and a later call making the same reads it back
    instead (:func:`_stored_output`); the mode filter, the day checks and the
    report run again on every call. ``progress`` (:class:`_Progress`) says
    when the crop, repair and validation start.

    Returns ``(path, report, made, key, window)``: ``made`` what
    :func:`_transform` and the validation made, its ``present_routes`` the
    set of ``route_id`` values in the downloaded feed as it enters the route
    crop, or ``None`` when a ``routes`` filter is not applied or that feed's
    routes.txt cannot be read — so a caller records an *undetermined* drop
    rather than a false empty one — ``key`` the output key (None without
    ``outputs``) and ``window`` the computed service window as ISO dates,
    None when unknown. The report is :func:`_report`'s. Raises
    :class:`_SkipFeed` when the feed drops out. Shared by the catalogue and
    the index paths.
    """
    from transitio.validate import validate_feed

    made = key = None
    if outputs is not None:
        cache, version = outputs
        key = _output_key(version, geometry, routes, crop, repair, budgets)
        made = _stored_output(version, key)
    if made is None:
        if progress is not None:
            progress.processing(crop or routes is not None, repair)
        if outputs is None:
            folder, stem = path.parent, f"{path.stem}-{tag}"
        else:
            folder, stem = version.path.parent / "outputs", key
        made = _transform(
            path,
            folder,
            stem,
            geometry=geometry,
            repair=repair,
            crop=crop,
            budgets=budgets,
            routes=routes,
        )
        if outputs is not None:
            _store_output(cache, version, key, made)
    path = made["path"]
    if modes is not None:
        served = _feed_modes(path)
        if served is None:
            raise _SkipFeed("could not read routes.txt for mode filtering")
        if not served & modes:
            raise _SkipFeed(f"serves {sorted(served)}, not {sorted(modes)}")
    if "validation" not in made:
        made["validation"] = validate_feed(path, **budgets)
        if outputs is not None:
            _store_output(cache, version, key, made)
    validation = made["validation"]
    start, end = _service_window(validation)
    window = _window(start, end)
    missing = _missing_files(validation)
    if missing is not None:
        raise _SkipFeed(missing, window)
    if day is not None:
        reason = _misses(start, end, day, study)
        if reason is None and study and _idle(validation, day):
            reason = f"no service on {day.isoformat()}"
        if reason is not None:
            raise _SkipFeed(reason, window, missed_day=True)
    return path, _report(made, hosted, provenance), made, key, window


def _warn_undated(feed_ids, day, stacklevel):
    """Warn that, without a Mobility Database API token, the newest copies of
    ``feed_ids`` were taken and do not run on ``day``: a token lets fetch pick
    the dated copy that does. ``stacklevel`` points at the caller of fetch."""
    if feed_ids:
        warnings.warn(
            "no Mobility Database API token: the newest copies of "
            f"{', '.join(feed_ids)} do not run on {day.isoformat()}; with a token, "
            "fetch can pick a dated copy from the Mobility Database",
            UserWarning,
            stacklevel=stacklevel,
        )


def _mdb_id(feed):
    """The Mobility Database id of the indexed ``feed``, or None."""
    from transitio.index.feeds import _parse

    return (_parse(feed._row.get("mdb")) or {}).get("mdb_id")


def _report(made, hosted, provenance):
    """The report on the feed that processing ``made``: its validation, with the
    notices of the source the crop trimmed, merged with the ``hosted``
    report and carrying ``provenance``; its summary holds the crop's
    ``dropped_rows`` as ``droppedRows``, None when the feed was not
    cropped."""
    from transitio.report import build_report

    validation = made["validation"]
    validation = {
        **validation,
        "notices": validation["notices"] + made["source_notices"],
    }
    report = build_report(validation, hosted=hosted, provenance=provenance)
    report["summary"]["droppedRows"] = made["dropped"]
    return report


def _transform(path, folder, stem, **steps):
    """The crop and the repair :func:`_process_feed` asks of the feed at
    ``path``, each output ``<stem>-<step>.zip`` in ``folder``: a dict of the
    feed made (``path``, read-only), the routes it had as it entered the
    route crop, the notices of the source the crop trimmed, the rows the crop
    dropped and the repair's fixes. A failed step removes the outputs the
    call wrote."""
    made = {"present_routes": None, "source_notices": [], "dropped": None}
    written = []
    try:
        return _transformed(path, folder, stem, made, written, **steps)
    except BaseException:
        # A step that failed leaves none of the call's outputs behind.
        for output in written:
            with contextlib.suppress(OSError):
                os.unlink(output)
        raise


def _transformed(
    path, folder, stem, made, written, *, geometry, repair, crop, budgets, routes
):
    """The steps of :func:`_transform`, each output added to ``written``."""
    from transitio.catalog._cache import _directory
    from transitio.gtfs import crop_feed
    from transitio.repair import repair_feed

    if crop or routes is not None:
        _directory(folder)
        cropped = folder / f"{stem}-cropped.zip"
        written.append(cropped)
        report = crop_feed(
            path, cropped, aoi=geometry if crop else None, routes=routes, **budgets
        )
        if routes is not None:
            # From the crop's own scan of this feed, so the drop audit and the
            # crop describe the same bytes (no second read to race). ``None``
            # (no routes.txt) stays undetermined, not empty.
            source = report.get("source_routes")
            made["present_routes"] = None if source is None else set(source)
        # The crop writes trimmed tables; the source's whitespace is
        # reported with the feed.
        made["source_notices"] = report["source_notices"]
        made["dropped"] = report["dropped_rows"]
        path = _read_only(cropped)
    # The crop comes first, so the repair works on the area's feed rather
    # than on the whole source.
    made["fixes"] = []
    if repair:
        _directory(folder)
        repaired = folder / f"{stem}-repaired.zip"
        written.append(repaired)
        made["fixes"] = repair_feed(path, repaired, **budgets)["fixes"]
        path = _read_only(repaired)
    made["path"] = path
    return made


def _output_key(version, geometry, routes, crop, repair, budgets):
    """The SHA-256 naming what processing makes of ``version``: canonical
    JSON of the transitio release, the version's SHA-256, the exact area
    (when cropped to it), the routes, the crop and repair flags and the
    budgets, the reference date among them."""
    from transitio import __version__

    return _request_key(
        transitio=__version__,
        version=version.sha256,
        area=hashlib.sha256(geometry.wkb).hexdigest() if crop else None,
        routes=None if routes is None else sorted(routes),
        crop=crop,
        repair=repair,
        budgets=budgets,
    )


def _stored_output(version, key):
    """What :func:`_transform` and the validation made of ``version`` under
    ``key``, as stored with it; None when nothing is stored, or when the
    output no longer matches its SHA-256 or its results cannot be read."""
    from transitio import _http
    from transitio.catalog._cache import _OUTPUT_STEPS, _regular

    record = version.sidecar["cache"].get("outputs", {}).get(key)
    if record is None:
        return None
    folder = version.path.parent / "outputs"
    results = folder / f"{key}.json"
    try:
        names = [f"{key}-{step}.zip" for step in _OUTPUT_STEPS]
        if folder.is_symlink() or record["file"] not in (None, *names):
            return None
        path = version.path if record["file"] is None else folder / record["file"]
        if not (_regular(results) and _regular(path)):
            return None
        if _http.sha256_file(results) != record["results_sha256"]:
            return None
        if record["file"] is not None and _http.sha256_file(path) != record["sha256"]:
            return None
        made = json.loads(results.read_text())
        routes = made["present_routes"]
        shapes = [
            (routes, (list, type(None))),
            (made["source_notices"], list),
            (made["dropped"], (list, type(None))),
            (made["fixes"], list),
            (made.get("validation", {}), dict),
        ]
        if not all(isinstance(value, kind) for value, kind in shapes):
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    made.update(path=path, present_routes=None if routes is None else set(routes))
    return made


def _store_output(cache, version, key, made):
    """Store with ``version`` what processing ``made`` of it under ``key``,
    the validation once it ran: the results beside the output in the
    version's ``outputs`` folder, and the output's name and SHA-256 in its
    sidecar."""
    from transitio import _http
    from transitio.catalog._cache import _directory, _write_provenance

    folder = version.path.parent / "outputs"
    _directory(folder)
    path = made["path"]
    record = {"file": None, "sha256": None}
    if path != version.path:
        record.update(file=path.name, sha256=_http.sha256_file(path))
        # Only the last step's output is kept.
        intermediate = folder / f"{key}-cropped.zip"
        if intermediate != path and intermediate.exists():
            intermediate.unlink()
    routes = made["present_routes"]
    results = {
        "present_routes": None if routes is None else sorted(routes),
        "source_notices": made["source_notices"],
        "dropped": made["dropped"],
        "fixes": made["fixes"],
    }
    if "validation" in made:
        results["validation"] = made["validation"]
    _write_provenance(folder / f"{key}.json", results)
    record["results_sha256"] = _http.sha256_file(folder / f"{key}.json")

    def change(sidecar):
        sidecar["cache"].setdefault("outputs", {})[key] = record

    cache.update(version, change)


def _read_only(path):
    """``path``, made read-only on POSIX systems, as cached files are."""
    if os.name != "nt":
        os.chmod(path, 0o444)
    return path


def _snapshot(path):
    """``(archive SHA-256, entries)`` of the zip at ``path``, read through one
    open: the entries as sorted ``(name, CRC-32, size)`` from the central
    directory, nothing decompressed. None when it cannot be read."""
    from transitio._http import sha256_stream

    try:
        with open(path, "rb") as handle:
            digest = sha256_stream(handle)
            handle.seek(0)
            with zipfile.ZipFile(handle) as archive:
                listing = sorted(
                    (info.filename, info.CRC, info.file_size)
                    for info in archive.infolist()
                    if not info.is_dir()
                )
        return digest, tuple(listing)
    except Exception:  # noqa: B902 — an unreadable archive matches nothing
        return None


def _entry_digests(path, digest):
    """Every entry of the zip at ``path`` as sorted ``(name, CRC-32, size,
    SHA-256)``, one row per entry, read through one open that must still hold
    the archive ``digest`` recorded earlier; None when it does not or the
    archive cannot be read."""
    from transitio._http import sha256_stream

    try:
        with open(path, "rb") as handle:
            if sha256_stream(handle) != digest:
                return None
            handle.seek(0)
            rows = []
            with zipfile.ZipFile(handle) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    with archive.open(info) as member:
                        entry = sha256_stream(member)
                    rows.append((info.filename, info.CRC, info.file_size, entry))
        return tuple(sorted(rows))
    except Exception:  # noqa: B902 — an unreadable archive matches nothing
        return None


class _Delivered:
    """The downloads a call has delivered, with the route filter each was
    cropped to (None: all routes), so a later download with the same content
    can be matched with them. Each download is described as it was when
    checked, before it was processed."""

    def __init__(self):
        self._feeds = []
        self._facts = {}
        self._entries = {}

    def _about(self, path):
        if path not in self._facts:
            self._facts[path] = _snapshot(path)
        return self._facts[path]

    def _digests(self, path):
        if path not in self._entries:
            self._entries[path] = _entry_digests(path, self._facts[path][0])
        return self._entries[path]

    def same_as(self, path):
        """The ``(feed id, routes)`` of each delivered feed whose download
        holds the same content as ``path``, in delivery order. Same content:
        equal archive digests, or equal entry listings (name, CRC-32, size)
        confirmed by equal SHA-256 digests of every entry. An archive that
        cannot be read, or that changed since it was checked, matches
        nothing."""
        facts = self._about(path)
        if facts is None:
            return []
        found = []
        for feed_id, other, cropped_to in self._feeds:
            recorded = self._facts[other]
            if recorded is None:
                continue
            if facts[0] == recorded[0]:
                found.append((feed_id, cropped_to))
            elif facts[1] and facts[1] == recorded[1]:
                mine = self._digests(path)
                if mine is not None and mine == self._digests(other):
                    found.append((feed_id, cropped_to))
        return found

    def add(self, feed_id, path, routes=None):
        self._about(path)
        self._feeds.append((feed_id, path, routes))


def _covers(twins, routes):
    """Whether the deliveries ``twins`` (``(feed id, routes)`` of one archive)
    carry every route of ``routes``; None stands for all routes."""
    cuts = [cut for _, cut in twins]
    if any(cut is None for cut in cuts):
        return True
    return bool(cuts) and routes is not None and routes <= set().union(*cuts)


def _containers(feed, entries, carriers, cropped, current):
    """``(proven, notes)``: a candidate's containers carried whole and
    proven unchanged before download, and why each other decided one drops
    nothing."""
    proven, unproven, notes = [], [], []
    for container in feed.contained_in:
        if container == feed.feed_id or container not in entries:
            continue
        decision = entries[container]["decision"]
        if decision is None:
            continue
        if container in carriers:
            (proven if current.get(container) else unproven).append(container)
        elif container in cropped:
            notes.append(f"kept: container {container} cropped to selected routes")
        else:
            notes.append(f"kept: container {container} skipped")
    if unproven:
        notes.append(_unproven(unproven))
    return proven, notes


def _unproven(containers):
    """The note on a feed kept because its containment in ``containers`` is
    not proven current."""
    return f"kept: containment in {', '.join(containers)} not proven current"


def _containers_first(feeds):
    """``feeds`` with each one after the feeds among them that contain it
    (``contained_in``), in their given order otherwise."""
    by_id = {feed.feed_id: feed for feed in feeds}
    depth = {}

    def level(feed, seen=()):
        if feed.feed_id not in depth:
            above = [
                by_id[c]
                for c in feed.contained_in
                if c in by_id and c not in seen and c != feed.feed_id
            ]
            depth[feed.feed_id] = 1 + max(
                (level(c, (*seen, feed.feed_id)) for c in above), default=-1
            )
        return depth[feed.feed_id]

    return sorted(feeds, key=level)


def _read_tables(path, names, max_total_bytes=None):
    """The tables ``names`` of a feed zip, read as ``FeedEditor`` does, with
    ``names`` a mapping only the columns it lists for each, by stripped
    name; None when together they are over ``max_total_bytes`` (default:
    the ``FeedEditor`` budget)."""
    import pandas as pd

    from transitio.edit._editor import _MAX_TOTAL_BYTES, _normalise_table

    limit = _MAX_TOTAL_BYTES if max_total_bytes is None else max_total_bytes
    csv = {"dtype": str, "keep_default_na": False, "encoding": "utf-8-sig"}
    with zipfile.ZipFile(path) as archive:
        members = [m for m in archive.infolist() if m.filename in names]
        if sum(m.file_size for m in members) > limit:
            return None
        tables = {}
        for m in members:
            table = pd.read_csv(archive.open(m), **csv)
            if isinstance(names, dict):
                wanted = names[m.filename]
                table = table.loc[:, [str(c).strip() in wanted for c in table.columns]]
            tables[m.filename] = _normalise_table(table)[0]
        return tables


def _service(path, day=None, max_total_bytes=None):
    """A delivered feed's route keys, rounded stop coordinates and trip
    count, and whether it is one ``unnamed`` agency; with a ``day``, the
    signatures of its trips running then, whether those are all of them,
    whether it has transfers or pathways, and its ``placeholder`` calendar,
    from those tables only, read as ``FeedEditor`` does. A feed is one
    unnamed agency when it has at most one agency row, none named, and its
    routes name at most one ``agency_id``. When trips run on the day and
    each of their services has a calendar.txt row spanning
    :data:`~transitio.gtfs._schedule.PLACEHOLDER_DAYS` days or more, its
    placeholder calendar is the earliest start and latest end of those
    rows, a ``(start, end)`` pair of dates, and None otherwise. The result
    is None when the tables are over
    ``max_total_bytes``, a route key has a blank part (an unnamed agency's
    blank agency aside), a stop or station lacks coordinates, or with a
    ``day`` its calendars cannot be read."""
    import pandas as pd

    from transitio.gtfs._schedule import (
        _column,
        placeholder_rows,
        route_keys,
        service_dates,
        trip_signatures,
    )

    names = {"agency.txt", "routes.txt", "stops.txt", "trips.txt"}
    if day is not None:
        names |= {"stop_times.txt", "calendar.txt", "calendar_dates.txt"}
        names |= {"frequencies.txt", "transfers.txt", "pathways.txt"}
    try:
        tables = _read_tables(path, names, max_total_bytes)
        if tables is None:
            return None
        keys = route_keys(tables)[["agency", "name", "mode"]]
        agency = tables.get("agency.txt", pd.DataFrame())
        named = (_column(agency, "agency_name").str.strip() != "").any()
        ids = _column(tables.get("routes.txt", pd.DataFrame()), "agency_id").str.strip()
        unnamed = len(agency) <= 1 and not named and ids[ids != ""].nunique() <= 1
        stops = tables["stops.txt"]
        points = stops[["stop_lat", "stop_lon"]].apply(pd.to_numeric, errors="coerce")
        # Stops and stations need coordinates; other location types may lack them.
        kind = stops.get("location_type", pd.Series("", index=stops.index))
        located = points[kind.str.strip().isin(("", "0", "1"))]
        parts = keys[["name", "mode"]] if unnamed else keys
        if (parts == "").any(axis=None) or located.isna().any(axis=None):
            return None
        points = points.round(_STOP_DECIMALS).add(0.0).dropna()
        trips = tables.get("trips.txt", pd.DataFrame(columns=["trip_id", "service_id"]))
        found = {
            "routes": set(keys.itertuples(index=False, name=None)),
            "stops": set(points.itertuples(index=False, name=None)),
            "trips": len(trips),
            "unnamed": unnamed,
        }
        if not (found["routes"] and found["stops"]):
            return None
        if day is None:
            return found
        dates, unexpanded = service_dates(tables)
        if unexpanded or dates.empty:
            return None
        running = dates.loc[dates["date"] == pd.Timestamp(day), "service_id"]
        today = trips[trips["service_id"].isin(running)]
        on_day, used = today["trip_id"], today["service_id"]
        signed = trip_signatures(tables)
        signed = signed[signed["trip_id"].isin(on_day)]
        rows = placeholder_rows(tables)
        rows = rows[rows["service_id"].isin(used)]
        placeholder = None
        if len(used) and used.isin(rows["service_id"]).all():
            placeholder = (rows["start"].min().date(), rows["end"].max().date())
    except Exception:  # noqa: B902 — an unreadable feed is never grouped
        return None
    found.update(
        day=set(signed["signature"]),
        complete=len(signed) == len(on_day),
        linked=any(len(tables.get(n, ())) for n in ("transfers.txt", "pathways.txt")),
        placeholder=placeholder,
    )
    return found


def _timezone_note(path, max_total_bytes=None):
    """``"agency_timezone <names>; stops in <zone>"`` for a feed declaring no
    time zone equivalent to the zone of most of its stops (:func:`_stop_zone`),
    compared over today and the next year as no calendar is read; None when
    one is, when either is unknown, or when agency.txt and stops.txt are
    unreadable or over ``max_total_bytes``."""
    from transitio.gtfs._merge import (
        _stop_zone,
        _timezone_interval,
        _timezones,
        _zone_classes,
    )

    try:
        tables = _read_tables(path, {"agency.txt", "stops.txt"}, max_total_bytes)
    except Exception:  # noqa: B902 — an unreadable feed gets no note
        return None
    if tables is None:
        return None
    declared, located = _timezones(tables), _stop_zone(tables)
    if not declared or located is None:
        return None
    classes = _zone_classes(declared | {located}, _timezone_interval([tables]))
    if any(classes[zone] == classes[located] for zone in declared):
        return None
    return f"agency_timezone {', '.join(sorted(declared))}; stops in {located}"


def _settle_versions(record, services, protected, day):
    """Settle the versions among the delivered feeds ``services`` (``{feed
    id: _service(...)}``) as :func:`fetch` describes, the connected
    components of the version pairs being the groups, and note or skip
    their entries in ``record``; returns the ids of the feeds left out."""
    entries = {entry["feed_id"]: entry for entry in record}
    order = {feed_id: position for position, feed_id in enumerate(entries)}

    def start(feed_id):
        # An undated feed starts when its placeholder calendar does.
        span = services[feed_id].get("placeholder")
        if span is not None:
            return span[0]
        value = (entries[feed_id]["feed_window"] or [None])[0]
        return None if value is None else datetime.date.fromisoformat(value)

    def rank(feed_id):
        first = start(feed_id)
        later = -first.toordinal() if first else 0
        return (first is None, later, -services[feed_id]["trips"], order[feed_id])

    def supersedes(feed_id, other):
        # A dated feed starting after an undated one's placeholder calendar.
        first = start(feed_id)
        return (
            services[feed_id].get("placeholder") is None
            and services[other].get("placeholder") is not None
            and first is not None
            and first > start(other)
        )

    def undated(feed_id):
        span = services[feed_id].get("placeholder")
        return span and "placeholder calendar {} to {}".format(*span)

    ids = sorted(services, key=rank)
    for feed_id in filter(undated, ids):
        _note(entries[feed_id], undated(feed_id))
    # A pair with an unnamed agency compares routes by name and mode only.
    lines = {
        feed_id: {key[1:] for key in services[feed_id]["routes"]} for feed_id in ids
    }
    pairs = {feed_id: {} for feed_id in ids}
    for position, one in enumerate(ids):
        for other in ids[position + 1 :]:
            unnamed = services[one]["unnamed"] or services[other]["unnamed"]
            sides = [
                (lines[f] if unnamed else services[f]["routes"], services[f]["stops"])
                for f in (one, other)
            ]
            overlaps = tuple(len(a & b) / len(a | b) for a, b in zip(*sides))
            # An undated feed and a dated one starting later pair on stops alone.
            routed = supersedes(one, other) or supersedes(other, one)
            routed = routed or overlaps[0] >= _ROUTE_OVERLAP
            if routed and overlaps[1] >= _STOP_OVERLAP:
                pairs[one][other] = pairs[other][one] = overlaps
    removed, grouped = set(), set()
    for top in ids:
        if top in grouped or not pairs[top]:
            continue
        group, frontier = {top}, [top]
        while frontier:
            for partner in pairs[frontier.pop()]:
                if partner not in group:
                    group.add(partner)
                    frontier.append(partner)
        grouped |= group
        members = [feed_id for feed_id in ids if feed_id in group]
        if day is None:
            for member in members:
                partner = next(other for other in ids if other in pairs[member])
                _note(entries[member], f"similar to {partner}; kept, no study day")
            continue
        kept = [m for m in members if m == top or m in protected]
        covered = set().union(*(services[m]["day"] for m in kept))
        for member in members:
            if member in kept:
                continue
            service = services[member]
            partners = [other for other in kept if other in pairs[member]]
            if any(supersedes(other, member) for other in partners):
                removed.add(member)
                continue
            adds = not (service["complete"] and service["day"] <= covered)
            if partners and not adds and not service["linked"]:
                removed.add(member)
                continue
            if partners:
                why = "; kept, has transfers or pathways"
                why = f" but adds service on {day}" if adds else why
                _note(entries[member], f"similar to {partners[0]}{why}")
            kept = sorted([*kept, member], key=ids.index)
            covered |= service["day"]
        for member in [m for m in members if m in removed]:
            partners = [other for other in kept if other in pairs[member]]
            later = [other for other in partners if supersedes(other, member)]
            partner = (later or partners)[0]
            route, stop = (round(share, 3) for share in pairs[member][partner])
            version = {"feed_id": partner, "route_overlap": route, "stop_overlap": stop}
            reason = f"another version of {partner}"
            note = undated(member)
            _skip(entries[member], reason, note=note, path=None, version_of=version)
    return removed


@dataclasses.dataclass
class _Processed:
    """A feed processed for delivery: its selection-record ``entry``, its
    cached ``version`` and the ``(request key, dataset id)`` it serves, what
    processing ``made`` of it under the output ``key``, and its report's
    ``origin`` and ``hosted`` report."""

    entry: dict
    version: object
    served: tuple
    made: dict
    key: str
    origin: dict
    hosted: dict


def _drop_repeats(
    cache, record, processed, feeds, reports, directory, progress=None, **options
):
    """Leave out of the delivered feeds the trips that repeat a trip kept
    from a feed before them in ``record``, as :func:`fetch` describes.

    ``processed`` holds a :class:`_Processed` for each path of ``feeds``
    and report of ``reports``; those whose entry is delivered are compared.
    A feed cut of repeated trips gets its deduplicated output in ``feeds``,
    written over its delivered archive in ``directory``, and its report in
    ``reports``. A withdrawn feed's entry is skipped and its archive in
    ``directory`` removed. A failed replacement or removal keeps the feed as
    delivered, noted. Each compared entry gets its ``duplicate_trips``
    count. ``options`` are ``budgets``, ``modes``, ``day``, the study day or
    None, and ``duplicate_trips``, ``"keep"`` comparing nothing;
    ``progress`` (:class:`_Progress`) says when the comparison starts.
    Returns ``{feed id: [ids]}``: for each feed cut or withdrawn, the feeds
    holding the trips it repeated.
    """
    from transitio import _http

    position = {entry["feed_id"]: n for n, entry in enumerate(record)}
    order = sorted(
        (
            n
            for n, item in enumerate(processed)
            if item.entry["decision"] == "delivered"
        ),
        key=lambda n: position[processed[n].entry["feed_id"]],
    )
    if len(order) < 2 or options["duplicate_trips"] == "keep":
        return {}
    if progress is not None:
        progress.say(f"Comparing trips across {len(order)} feeds")
    items = [processed[n] for n in order]
    day, modes = options["day"], options["modes"]
    run = _request_key(
        made=[(item.entry["feed_id"], item.key) for item in items],
        duplicate_trips=options["duplicate_trips"],
        day=None if day is None else day.isoformat(),
        modes=None if modes is None else sorted(modes),
    )
    keys = [_request_key(run=run, made=item.key) for item in items]
    found = _stored_repeats(items, keys) or _repeats(cache, items, keys, **options)
    lost = {}
    for n, item, (outcome, made) in zip(order, items, found):
        entry = item.entry
        # None when not compared, else 0 until trips are left out below.
        entry["duplicate_trips"] = outcome["dropped"] and 0
        if outcome["skip"] is None and made is None:
            if outcome["note"] is not None:
                _note(entry, outcome["note"])
            continue
        try:
            # The report first, so a failure leaves the delivered feed whole.
            report = None if made is None else _report(made, item.hosted, item.origin)
            if outcome["skip"] is not None and directory:
                os.unlink(feeds[n])
                with contextlib.suppress(OSError):
                    os.unlink(feeds[n].with_suffix(".provenance.json"))
            elif made is not None and directory:
                with open(made["path"], "rb") as source:
                    with _http.replacing(feeds[n]) as handle:
                        shutil.copyfileobj(source, handle)
        except Exception as error:  # noqa: B902 — the feed stays as delivered
            _note(entry, f"repeated trips kept: {error}")
            continue
        entry["duplicate_trips"] = outcome["dropped"]
        lost[entry["feed_id"]] = outcome["of"]
        if outcome["skip"] is not None:
            _skip(entry, outcome["skip"], path=None, feed_window=outcome["window"])
            continue
        if not directory:
            feeds[n] = made["path"]
        reports[n] = report
        entry.update(path=feeds[n], feed_window=outcome["window"])
        _note(entry, outcome["note"])
    return lost


def _stored_repeats(items, keys):
    """What :func:`_repeats` stored of ``items`` under ``keys``, None unless
    every feed has its outcome and every cut feed its deduplicated output."""
    from transitio.catalog._cache import _repeats_record

    found = []
    for item, key in zip(items, keys):
        outcome = item.version.sidecar["cache"].get("repeats", {}).get(key)
        if not _repeats_record(key, outcome):
            return None
        made = None
        if outcome["skip"] is None and outcome["dropped"]:
            made = _stored_output(item.version, key)
            if made is None or "validation" not in made:
                return None
        found.append((outcome, made))
    return found


# The columns the matching of repeated trips and its time-zone check read,
# by table; _repeats adds calendar.txt's weekdays.
_MATCHED_COLUMNS = {
    "agency.txt": {"agency_id", "agency_name", "agency_timezone"},
    "stops.txt": {"stop_id", "stop_lat", "stop_lon"},
    "routes.txt": {
        "route_id",
        "agency_id",
        "route_short_name",
        "route_long_name",
        "route_type",
        "continuous_pickup",
        "continuous_drop_off",
    },
    "trips.txt": {
        "trip_id",
        "route_id",
        "service_id",
        "block_id",
        "wheelchair_accessible",
        "bikes_allowed",
    },
    "stop_times.txt": {
        "trip_id",
        "stop_id",
        "stop_sequence",
        "arrival_time",
        "departure_time",
        "pickup_type",
        "drop_off_type",
        "continuous_pickup",
        "continuous_drop_off",
    },
    "calendar.txt": {"service_id", "start_date", "end_date"},
    "calendar_dates.txt": {"service_id", "date", "exception_type"},
    "frequencies.txt": {
        "trip_id",
        "start_time",
        "end_time",
        "headway_secs",
        "exact_times",
    },
    "transfers.txt": {"from_trip_id", "to_trip_id"},
}


def _release_arrow_memory():
    """Give back to the system the memory Arrow's default pool keeps once
    pandas frees the strings in it, as far as the pool can."""
    import pyarrow

    release = getattr(pyarrow.default_memory_pool(), "release_unused", None)
    if release is not None:
        release()


def _repeats(cache, items, keys, budgets, modes, day, duplicate_trips):
    """Per feed of ``items``, in priority order, ``(outcome, made)``: the
    trips left out as repeats, with ``duplicate_trips="drop"`` also near
    repeats, of the trips kept from the feeds before it
    (:func:`~transitio.gtfs._duplicates.repeated_trips`), the feeds holding
    them, and a skip reason, or a note and the service window with
    ``made``, the deduplicated output (:func:`_without_repeats`). Once every
    feed is decided, each outcome is stored under its key of ``keys``
    (:func:`_store_found`). A feed declaring another time zone than the
    rest (as ``merge_feeds(timezones="skip")`` sets apart), over
    ``max_total_bytes`` or unreadable is not compared. A feed left with
    none of ``modes`` is withdrawn and the matching runs again without it.
    A failure keeps a feed's trips, with a note, and is not stored; a failed
    matching, or a feed's file that cannot be opened, keeps every feed's
    and stores nothing."""
    from transitio.gtfs._duplicates import _running, repeated_trips
    from transitio.gtfs._merge import _stop_zone, _timezone_outliers, _timezones
    from transitio.gtfs._schedule import _WEEKDAYS

    def unchanged(error):
        kept = {"dropped": None, "of": [], "skip": None, "window": None}
        return [({**kept, "note": f"repeated trips kept: {error}"}, None)] * len(items)

    names = dict(_MATCHED_COLUMNS)
    names["calendar.txt"] = names["calendar.txt"] | set(_WEEKDAYS)
    tables = []
    for item in items:
        try:
            read = _read_tables(
                item.made["path"], names, budgets.get("max_total_bytes")
            )
        except OSError as error:  # gone or unreachable: every feed keeps its trips
            return unchanged(error)
        except Exception:  # noqa: B902 — an unreadable feed is not compared
            read = None
        tables.append(read or {})
    held = [read.get("trips.txt", {}).get("trip_id") for read in tables]
    ids = [item.entry["feed_id"] for item in items]
    found, failed, near = [], set(), duplicate_trips == "drop"

    try:
        # The deduplicated outputs wait here until every feed is decided.
        staging = tempfile.TemporaryDirectory(
            dir=cache.root, ignore_cleanup_errors=True
        )
    except Exception as error:  # noqa: B902 — every feed keeps its trips
        return unchanged(error)
    with staging as scratch:
        while len(found) < len(items):
            try:
                if not found:
                    if len(set().union(*map(_timezones, tables))) > 1:
                        located = [_stop_zone(read) for read in tables]
                        for n in _timezone_outliers(tables, located=located):
                            tables[n] = {}
                    # The time-zone check reads every stop time, the
                    # matching only the day's.
                    if day is not None:
                        tables = [_running(read, (day, day)) for read in tables]
                        _release_arrow_memory()
                matched = repeated_trips(tables, near=near, day=day)
                _release_arrow_memory()
            except Exception as error:  # noqa: B902 — every feed keeps its trips
                return unchanged(error)
            for n in range(len(found), len(items)):
                trips, earlier, scope = matched[n]
                outcome = {"dropped": 0 if tables[n] else None, "skip": None}
                outcome.update(of=[ids[p] for p in earlier], note=None, window=None)
                made, again = None, False
                try:
                    if trips:
                        output = pathlib.Path(scratch) / f"{n}.zip"
                        made = _without_repeats(items[n].made, output, trips, budgets)
                        again = _decide(
                            outcome, made, held[n], trips, scope, modes, day
                        )
                except Exception as error:  # noqa: B902 — the feed keeps its trips
                    note = f"repeated trips kept: {error}"
                    outcome, made = {**outcome, "skip": None, "note": note}, None
                    failed.add(n)
                found.append((outcome, None if outcome["skip"] else made))
                if again:
                    tables[n] = {}
                    break
        return _store_found(cache, items, keys, found, failed)


def _store_found(cache, items, keys, found, failed):
    """``found``, what :func:`_repeats` decided of ``items``, once stored:
    a cut feed's deduplicated output moved to
    ``outputs/<key>-deduplicated.zip`` beside its version and stored
    (:func:`_store_output`), and each outcome but those at the positions
    ``failed`` stored with the version under its key of ``keys``. A cut feed
    whose output cannot be stored keeps its trips, noted; an outcome not
    stored is found again by a later call. Nothing is stored for a version
    the cache no longer lists, and its feed, cut or withdrawn, keeps its
    trips, noted."""
    from transitio.catalog._cache import _directory

    stored = []
    for n, (item, key, (outcome, made)) in enumerate(zip(items, keys, found)):
        feed_id, listed = item.entry["feed_id"], True
        try:
            with cache.lock(feed_id):
                versions = cache.versions(feed_id)
                listed = item.version.sha256 in {v.sha256 for v in versions}
                if listed and made is not None:
                    folder = item.version.path.parent / "outputs"
                    _directory(folder)
                    made = {**made, "path": folder / f"{key}-deduplicated.zip"}
                    os.replace(found[n][1]["path"], made["path"])
                    _store_output(cache, item.version, key, made)
                if listed and n not in failed:
                    _store_repeats(cache, item.version, key, outcome)
        except Exception as error:  # noqa: B902 — not stored, found again later
            if made is not None or outcome["skip"] is not None:
                note = f"repeated trips kept: {error}"
                outcome, made = {**outcome, "skip": None, "note": note}, None
        if not listed and (made is not None or outcome["skip"] is not None):
            note = "repeated trips kept: its cached version was removed"
            outcome, made = {**outcome, "skip": None, "note": note}, None
        stored.append((outcome, made))
    return stored


def _decide(outcome, made, held, trips, scope, modes, day):
    """Record in ``outcome`` what ``made``, the deduplicated output of the
    feed whose trips.txt holds the trip ids ``held``, without the repeated
    ``trips``, leaves: the number of its trips left out and its service
    window, and a skip reason when it lost every trip in ``scope`` or, with
    ``modes``, every requested mode, else the note. Returns whether it lost
    the modes. Raises ``ValueError`` when the crop left out other trips too
    or the output lacks a file GTFS requires."""
    left = _read_tables(made["path"], {"trips.txt"})["trips.txt"]
    gone = set(held) - set(left["trip_id"])
    if gone - trips:
        others = len(gone - trips)
        raise ValueError(
            f"the crop would also leave out {others} trips that repeat no "
            "other feed's"
        )
    window = _window(*_service_window(made["validation"]))
    outcome.update(dropped=len(gone), window=window)
    listed = ", ".join(outcome["of"])
    served = None if modes is None else _feed_modes(made["path"])
    if scope <= gone:
        on = "" if day is None else f" on {day.isoformat()}"
        outcome["skip"] = f"every trip{on} repeats a trip of {listed}"
        return False
    missing = _missing_files(made["validation"])
    if missing is not None:
        raise ValueError(f"the feed left would be {missing}")
    if modes is not None and served is None:
        raise ValueError("could not read routes.txt for mode filtering")
    if modes is not None and not served & modes:
        outcome["skip"] = (
            f"serves {sorted(served)} after repeated trips were left out, "
            f"not {sorted(modes)}"
        )
        return True
    outcome["note"] = f"{len(gone)} repeated trips of {listed} left out"
    return False


def _without_repeats(made, output, trips, budgets):
    """What processing ``made`` becomes without the trips ``trips``: its
    output cropped with :func:`~transitio.gtfs.crop_feed`'s
    ``exclude_trips`` to ``output``, read-only, and validated, the crop's
    source notices and dropped rows added to those ``made`` holds. A failed
    step removes the output."""
    from transitio.gtfs import crop_feed
    from transitio.validate import validate_feed

    try:
        cropped = crop_feed(
            made["path"], output, exclude_trips=sorted(trips), **budgets
        )
        made = {
            **made,
            "path": _read_only(output),
            "source_notices": made["source_notices"] + cropped["source_notices"],
            "dropped": (made["dropped"] or []) + cropped["dropped_rows"],
        }
        made["validation"] = validate_feed(output, **budgets)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(output)
        raise
    return made


def _store_repeats(cache, version, key, outcome):
    """Store with ``version`` what :func:`_repeats` decided of it under
    ``key``."""

    def change(sidecar):
        sidecar["cache"].setdefault("repeats", {})[key] = outcome

    cache.update(version, change)


class _Archives:
    """The archives a call reads feeds from by URL fragment, each downloaded
    into ``directory`` once, keyed by its URL without the fragment; a failed
    download is recorded and never repeated."""

    def __init__(self, directory):
        self._directory = pathlib.Path(directory)
        self._found = {}

    def get(self, http, url, access=None, transport=None):
        """``(path, sha256, retrieved_at)`` of the archive at ``url``,
        downloaded with the ``http`` client when first asked for, with the
        credentials of ``access`` over ``transport`` when given
        (:func:`transitio._http.download`). Raises :class:`DownloadError`
        with the failure's text when that download failed."""
        from transitio import _http
        from transitio.exceptions import DownloadError

        key = (url, access)
        if key not in self._found:
            path = self._directory / f"{len(self._found)}.zip"
            try:
                digest = _http.download(
                    http, url, path, access=access, transport=transport
                )
            except Exception as error:  # noqa: B902 — recorded for later calls
                self._found[key] = str(error)
            else:
                now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                self._found[key] = (path, digest, now)
        found = self._found[key]
        if isinstance(found, str):
            raise DownloadError(found)
        return found


def _download_indexed(
    feed,
    db,
    atlas,
    base_dir,
    archives,
    max_total_bytes=None,
    access=None,
    progress=None,
):
    """Download an indexed feed from the first of its URLs that serves a zip
    archive: the Mobility Database direct download, the Transitland Atlas
    static feed (decision I: MDB wins where a feed has both), then the
    Mobility Database hosted copy (``urls.latest``). On schema 11 the
    feed's ``download_url`` comes first and the hosted copy second. A
    protected feed (``feed.access == "key"``) is read from its access URL
    with the credentials of ``access``
    (:class:`~transitio.catalog._access._Access`), then from the hosted
    copy without them; without ``access``, from the hosted copy alone,
    never from its producer's URLs. Each URL is tried once;
    an attempt fails on any error or when the download is not a zip archive.
    Each feed lands in its own digest-named directory under ``base_dir``, so
    several never collide. A URL whose fragment names a member of the archive
    (:func:`~transitio.catalog._nested.split_fragment`) takes the archive
    from ``archives`` and extracts that member within ``max_total_bytes``;
    its sidecar records the archive's URL and SHA-256 beside the feed's.
    ``progress`` (:class:`_Progress`) says each failure another URL follows.

    Returns ``(path, fetched_from, failures, url)``: ``fetched_from`` is
    ``"producer"`` for the feed's own URLs and ``"mdb_latest"`` for the hosted
    copy, ``failures`` the ``"<source>: <error>"`` of each failed attempt and
    ``url`` the one the feed was read from.
    Raises :class:`DownloadError` naming the failures when every attempt
    fails."""
    from transitio.catalog import AtlasFeed, Feed
    from transitio.catalog._atlas import _feed_dir
    from transitio.catalog._client import _download_recorded, _write_provenance
    from transitio.catalog._nested import extract_feed, split_fragment
    from transitio.exceptions import DownloadError
    from transitio.index.feeds import _hosted_url, _parse

    mdb_urls = (_parse(feed._row.get("mdb")) or {}).get("urls") or {}
    atlas_feed = AtlasFeed.from_record(
        _parse(feed._row.get("atlas")) or {}, feed_id=feed.feed_id
    )

    def from_mdb(url):
        proxy = Feed.from_api(
            {"id": feed.feed_id, "latest_dataset": {"hosted_url": url}}
        )
        return db._fetch_latest(proxy, directory=base_dir / _feed_dir(feed.feed_id))

    def from_atlas(url):
        return atlas._fetch_static(atlas_feed, directory=base_dir)

    def from_url(url):
        path = base_dir / _feed_dir(feed.feed_id) / "latest.zip"
        record = {"feed_id": feed.feed_id}
        options = {"access": access, "transport": atlas._transport}
        return _download_recorded(atlas._http, url, path, record, **options)

    def from_archive(client, url, outer, member, keys):
        archive, archive_sha256, retrieved_at = archives.get(
            client._http, outer, keys, atlas._transport
        )
        path = base_dir / _feed_dir(feed.feed_id) / "latest.zip"
        provenance = {
            "feed_id": feed.feed_id,
            "source_url": url,
            "archive_url": outer,
            "archive_sha256": archive_sha256,
            "sha256": extract_feed(archive, member, path, max_total_bytes),
            "retrieved_at": retrieved_at,
        }
        _write_provenance(path.with_suffix(".provenance.json"), provenance)
        return path

    mdb = ("mdb", "producer", mdb_urls.get("direct_download"), from_mdb, db)
    static = ("atlas", "producer", atlas_feed.static_url, from_atlas, atlas)
    hosted = ("mdb_latest", "mdb_latest", _hosted_url(feed), from_mdb, db)
    attempts = (mdb, static, hosted)
    if access is not None:
        keyed = ("download_url", "producer", access.url, from_url, atlas)
        attempts = (keyed, hosted)
    elif feed.access == "key":
        attempts = (hosted,)
    elif "download_url" in feed._row:
        crawled = ("download_url", "producer", feed.download_url, from_url, atlas)
        attempts = (crawled, hosted, mdb, static)
    runs = {}
    for attempt in attempts:
        if attempt[2]:
            runs.setdefault(attempt[2], attempt)
    failures = []
    for n, (label, source, url, download, client) in enumerate(runs.values(), 1):
        outer, member = split_fragment(url)
        try:
            if member is None:
                path = download(url)
            else:
                # Only the access URL's attempt carries the credentials.
                keys = access if label == "download_url" else None
                path = from_archive(client, url, outer, member, keys)
        except Exception as error:  # noqa: B902 — try the next URL
            reason = str(error)
        else:
            if zipfile.is_zipfile(path):
                return path, source, failures, url
            reason = "not a zip archive"
        failures.append(f"{label}: {reason}")
        if progress is not None and n < len(runs):
            progress.retry(reason, outer)
    if failures:
        raise DownloadError("; ".join(failures))
    raise DownloadError(f"feed {feed.feed_id} has no downloadable url")


class _Progress:
    """What a :func:`fetch` call says on stderr as it runs, nothing when not
    ``shown`` (:mod:`transitio._progress`): a start line; per feed, numbered
    in the call's selection, a bar per download and a line for a failed URL,
    the crop and validation, a skip, a cached copy reused or a feed left out
    after delivery; a line per later step and a summary."""

    def __init__(self, shown):
        self.shown = shown
        self.started = time.monotonic()
        self.count = self.downloaded = 0
        self.feed_id = self.bar = None
        # Each feed's number, and its decision when its block ended.
        self.numbers, self.said = {}, {}
        # A protected feed's redaction of its credentials, by feed id.
        self.masks = {}

    def say(self, text):
        if self.shown:
            from transitio import _progress

            _progress.say(text)

    def start(self, count, where, skipped=()):
        """Say that ``count`` feeds are fetched ``where``, then number the
        record entries ``skipped`` before the feed loop and say why."""
        self.count = count
        self.say(f"Fetching {count} feed{'' if count == 1 else 's'} {where}")
        for entry in skipped:
            self._next(entry)
            self.line(f"skipped ({entry['reason']})")

    def _next(self, entry):
        """Make ``entry``'s feed the current one, numbered next."""
        self.feed_id = entry["feed_id"]
        self.numbers[self.feed_id] = len(self.numbers) + 1

    @property
    def prefix(self):
        return f"[{self.numbers[self.feed_id]}/{self.count}] {self.feed_id}"

    @contextlib.contextmanager
    def downloads(self, desc):
        """A bar described ``desc`` for each download in the block, their
        bytes counted."""
        from transitio import _progress

        self.bar = _progress.Download(desc) if self.shown else None
        try:
            with _progress.reporting(self.bar):
                yield
        finally:
            if self.bar is not None:
                self.bar.close()
                self.downloaded += self.bar.downloaded
            self.bar = None

    @contextlib.contextmanager
    def feed(self, entry):
        """The block of the next feed, ``entry`` its record: a bar per
        download, described ``[n/count] <id> (<name>)``, and once the block
        ends, a line when the feed was skipped or a cached copy reused."""
        self._next(entry)
        name = entry["name"]
        named = name and name != self.feed_id
        with self.downloads(f"{self.prefix} ({name})" if named else self.prefix):
            yield
            if entry["decision"] == "skipped":
                self.line(f"skipped ({entry['reason']})")
            elif entry["cache"] in ("reused", "fallback"):
                self.line("cached copy reused")
            self.said[self.feed_id] = entry["decision"]

    def line(self, text):
        """Say ``text`` about the current feed, below its closed bar."""
        if self.bar is not None:
            self.bar.close()
        mask = self.masks.get(self.feed_id)
        self.say(f"{self.prefix}: {text if mask is None else mask(text)}")

    def processing(self, crop, repair):
        """Say that the current feed's crop, when ``crop``, repair, when
        ``repair``, and validation start."""
        steps = [step for step, on in (("cropping", crop), ("repairing", repair)) if on]
        self.line(" and ".join(filter(None, [", ".join(steps), "validating"])))

    def left_out(self, record):
        """Say why each feed of ``record`` whose block ended delivered was
        skipped after all."""
        for entry in record:
            self.feed_id = entry["feed_id"]
            was = self.said.get(self.feed_id)
            if was == "delivered" and entry["decision"] == "skipped":
                self.said[self.feed_id] = "skipped"
                self.line(f"left out ({entry['reason']})")

    def retry(self, reason, url):
        """Say that the current feed's download from ``url`` failed for
        ``reason`` and the next URL follows."""
        reason = reason.removeprefix(f"{url}: ")
        self.line(f"download failed ({reason}), trying the next URL")

    def done(self, record):
        """Say how many feeds of ``record`` were delivered and skipped, the
        bytes downloaded and the time the call took."""
        delivered = sum(entry["decision"] == "delivered" for entry in record)
        skipped = sum(entry["decision"] == "skipped" for entry in record)
        parts = [
            f"{delivered} feed{'' if delivered == 1 else 's'} delivered",
            f"{skipped} skipped",
        ]
        if self.downloaded:
            unit, scale = ("GB", 1e9) if self.downloaded >= 1e9 else ("MB", 1e6)
            parts.append(f"{self.downloaded / scale:.1f} {unit} downloaded")
        minutes, seconds = divmod(round(time.monotonic() - self.started), 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            took = f"{hours} h {minutes} min"
        elif minutes:
            took = f"{minutes} min {seconds} s"
        else:
            took = f"{seconds} s"
        self.say(f"Done in {took}: {', '.join(parts)}")


def _held(cache, feeds, entry_for, progress):
    """``(feed, entry, staging)`` for each of ``feeds``, ``entry`` its
    selection-record entry (``entry_for``), the loop body running under the
    feed's cache lock with a fresh staging folder
    (:meth:`~transitio.catalog._cache.FeedCache.staging`) as the current
    feed of ``progress`` (:meth:`_Progress.feed`)."""
    for feed in feeds:
        entry = entry_for(feed)
        feed_id = entry["feed_id"]
        with (
            cache.lock(feed_id),
            cache.staging(feed_id) as staging,
            progress.feed(entry),
        ):
            yield feed, entry, staging


def _add_version(cache, feed_id, path, url, fetched_from, errors, **options):
    """Add the download staged at ``path`` from ``url`` as a version of
    ``feed_id`` (:meth:`~transitio.catalog._cache.FeedCache.publish`), the
    acquisition recorded with ``fetched_from``, the failed attempts before it
    (``errors``), ``with_credentials``, the ``snapshot`` in use and, from the
    sidecar beside a feed read out of a larger archive, that archive's URL and
    SHA-256; ``dataset`` and ``replace`` pass through. Returns the version."""
    from transitio import _http
    from transitio.catalog._cache import _now

    sidecar = path.with_suffix(".provenance.json")
    staged = json.loads(sidecar.read_text()) if sidecar.exists() else {}
    nested = {k: staged[k] for k in ("archive_url", "archive_sha256") if k in staged}
    source = {
        "source_url": url,
        **nested,
        "fetched_from": fetched_from,
        "with_credentials": options.pop("with_credentials", False),
        "download_errors": errors,
        "index_snapshot": options.pop("snapshot", None),
        "retrieved_at": _now(),
    }
    acquired = ("source_url", "sha256", "retrieved_at")
    record = {k: v for k, v in staged.items() if k not in acquired}
    record["feed_id"] = feed_id
    digest = _http.sha256_file(path)
    return cache.publish(feed_id, path, digest, record, source, used=False, **options)


def _request_key(**parts):
    """The SHA-256 naming a request: canonical JSON of ``parts``, what
    decides whether a cached version serves it."""
    text = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _candidates(cache, feed_id, key, keyless):
    """The cached versions of ``feed_id`` to try for the request ``key``:
    those that served it before first, otherwise newest first; with
    ``keyless`` only versions once acquired without credentials."""
    versions = [
        version
        for version in cache.versions(feed_id)
        if not keyless
        or any(
            not s.get("with_credentials") for s in version.sidecar["cache"]["sources"]
        )
    ]
    return sorted(versions, key=lambda version: key not in version.served)


def _context(version, key):
    """The dataset the request ``key`` reads ``version`` as: the one it read
    it as before, else the first the version holds; None for none."""
    if key in version.served:
        return version.served[key]
    return next(iter(version.datasets), None)


def _hosted(db, cache, version, dataset_id):
    """The hosted validation report of ``dataset_id`` stored with
    ``version``: fetched and stored at its first use (``"unavailable"`` when
    there is none or it cannot be fetched), read from the cache after."""
    from transitio.catalog._models import Dataset

    entry = version.datasets.get(dataset_id) if dataset_id else None
    if entry is None:
        return None
    if "report" not in entry:
        report, url = None, entry.get("validation_report_url")
        if url:
            record = {"id": dataset_id, "validation_report": {"url_json": url}}
            try:
                report = db.validation_report(Dataset.from_api(record))
            except Exception:  # noqa: B902 — the hosted report is optional
                report = None

        def change(sidecar):
            stored = "unavailable" if report is None else report
            sidecar["cache"]["datasets"][dataset_id].setdefault("report", stored)

        cache.update(version, change)
        entry = version.datasets.get(dataset_id, {})
    report = entry.get("report", "unavailable")
    return None if report == "unavailable" else report


def _warn_fallback(feed_id, errors, version):
    """Warn that a refresh of ``feed_id`` failed with ``errors`` and the
    cached ``version`` is delivered instead."""
    warnings.warn(
        f"{feed_id}: refresh failed ({errors or 'nothing to download'}); "
        f"using the cached version retrieved {version.retrieved_at}",
        UserWarning,
        stacklevel=3,
    )


def _prove(cache, version, snapshot, url):
    """Record that a probe of ``url`` proved ``version`` the archive the
    index ``snapshot`` crawled; an earlier proof stays."""

    def change(sidecar):
        sidecar["cache"].setdefault("index_proofs", {}).setdefault(snapshot, url)

    cache.update(version, change)


def _feed_cache(cache_dir, directory):
    """The download cache under ``cache_dir`` (default: the platform cache),
    its root created; a ``directory`` inside it, where a delivered copy could
    replace a cached version, is refused."""
    import platformdirs

    from transitio.catalog._cache import FeedCache, _directory

    cache = FeedCache(cache_dir or platformdirs.user_cache_dir("transitio"))
    if directory is not None:
        path, root = pathlib.Path(directory).resolve(), cache.root.resolve()
        if path == root or root in path.parents:
            raise ValueError("directory= must lie outside the download cache")
    _directory(cache.root)
    return cache


def _delivered_name(feed_id):
    """The name, without extension, ``feed_id``'s feed is delivered under:
    the id itself when it is a plain name, else its ASCII form cut to 80
    characters, ``+`` and the id's SHA-256. No plain name holds a ``+``."""
    from transitio.catalog._cache import _feed_dir
    from transitio.index.places import _normalize
    from transitio.osm._fetch import _slug

    plain = _PLAIN_NAME.fullmatch(feed_id) and len(feed_id) <= 100
    if plain and feed_id not in _DEVICE_NAMES:
        return feed_id
    slug = _slug(_normalize(feed_id))[:80].rstrip("-")
    return f"{slug}+{_feed_dir(feed_id).removeprefix('id-')}"


def _deliver(path, directory, provenance):
    """``path``, a feed made from a cached version, as delivered: copied into
    ``directory``, when given, as ``<name>.zip`` beside a sidecar of its
    ``provenance``, ``<name>`` being its feed's :func:`_delivered_name`. A
    file or link at either name is replaced."""
    from transitio.catalog._cache import _copy

    if directory:
        name = _delivered_name(provenance["feed_id"])
        path = _copy(path, pathlib.Path(directory) / f"{name}.zip", provenance)
    return path


def _report_provenance(version, dataset_id=None):
    """What a report on ``version`` records of its origin: the feed, the
    SHA-256 and its first acquisition (URL, retrieval time, where it came
    from, the attempts that failed before it, the archive it was read out
    of); for ``dataset_id`` the dataset and its service dates."""
    first = version.first_source
    origin = {"feed_id": version.sidecar["feed_id"], "sha256": version.sha256}
    for key in ("source_url", "archive_url", "archive_sha256", "retrieved_at"):
        if key in first:
            origin[key] = first[key]
    origin.update(
        fetched_from=first["fetched_from"], download_errors=first["download_errors"]
    )
    entry = version.datasets.get(dataset_id) if dataset_id else None
    if entry is not None:
        origin.update(
            dataset_id=dataset_id, service_date_range=entry["service_date_range"]
        )
    return origin


def _unchanged_since_indexed(feed, http, access=None, transport=None):
    """Whether the archive the index crawled for an indexed feed is still the
    one served: a conditional ``HEAD`` to the URL the crawl reads
    (``_crawl_url``: its download URL, else Atlas, else MDB), carrying the
    ETag and Last-Modified it recorded, answers 304 Not Modified. Returns
    that URL, or None: any other answer, a failed probe or no recorded
    validator is no proof. A URL fragment is not sent, so the probe of a
    feed inside a larger archive reaches that archive. A protected feed's
    probe sends the credentials of ``access`` over ``transport``, as its
    download does."""
    from transitio.index.feeds import _crawl_url, _scalar

    url = _crawl_url(feed)
    headers = {}
    etag = _scalar(feed._row.get("etag"))
    last_modified = _scalar(feed._row.get("last_modified"))
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    if not url or not headers:
        return None
    try:
        if access is None:
            status = http.head(url, headers=headers, timeout=_PROBE_TIMEOUT).status_code
        else:
            with (
                access.session(http, transport) as session,
                session.stream(
                    "HEAD", url, headers=headers, timeout=_PROBE_TIMEOUT
                ) as response,
            ):
                status = response.status_code
    except Exception:  # noqa: B902 — an unanswered probe proves nothing
        return None
    return url if status == 304 else None


def _access_for(feed, explicit):
    """``(access, reason)`` for a protected indexed feed: the
    :class:`~transitio.catalog._access._Access` its requests send its
    credentials with, or None and why none can be sent.
    ``explicit`` maps provider ids to the ``credentials=`` fields, which win
    over the stored ones (:mod:`transitio.credentials`)."""
    from transitio.catalog._access import _Access, _origin, _sends
    from transitio.credentials import _resolve

    provider = None
    if feed.access_provider is not None and feed._index is not None:
        provider = feed._index.access_provider(feed.access_provider)
    if provider is None:
        return None, "the index has no access details for it"
    method, params = feed.auth_method, feed.auth_params
    if not _sends(method, params, provider.credential_fields):
        return None, "its access method is not supported"
    url = feed.access_url
    try:
        https = _origin(url)[0] == "https"
    except Exception:  # noqa: B902 — no URL, or none httpx reads
        https = False
    if not https:
        return None, "its URL is not https"
    fields, missing = _resolve(provider, explicit.get(provider.provider_id))
    if missing:
        state = "incomplete" if fields else "missing"
        return None, f"credentials {state} for {provider.provider_id}"
    try:
        return _Access(url, method, params or {}, fields), None
    except ValueError as error:
        return None, str(error)


def _area_for(geometry, index, country_code):
    """``(area, None)`` when the feed index's places, of ``country_code``
    when given, cover at least half of ``geometry``'s land
    (:func:`transitio.index.area`); else ``(None, why)``, the reason the
    catalogue is searched instead. Without an installed index the catalogue
    is searched; a given ``index``, or a pinned snapshot, that cannot be read
    raises."""
    from transitio.exceptions import TransitioError
    from transitio.index import _coerce_index, area
    from transitio.index._refresh import _pinned

    if index is False:
        return None, "index=False searches the catalogue"
    try:
        resolved = _coerce_index(index)
    except TransitioError:
        if index is not None or _pinned()[1] is not None:
            raise
        return None, _NO_INDEX
    if resolved.places is None:
        return None, "the feed index carries no places"
    found = area(geometry, country=country_code, index=resolved)
    if found.coverage < _AREA_COVERAGE:
        # Rounded down, so a share just under the threshold never reads as it.
        share = math.floor(found.coverage * 100)
        return None, f"the feed index's places cover {share}% of the area"
    return found, None


def fetch(
    aoi=None,
    when=None,
    *,
    place=None,
    tiers=None,
    exclude=None,
    on_unknown="include",
    on_untrusted_selector="auto",
    contained="drop",
    feeds=None,
    index=None,
    credentials=None,
    modes=None,
    duplicate_trips="drop",
    expired="skip",
    repair=False,
    crop=True,
    osm=True,
    refresh_token=None,
    cache_dir=None,
    directory=None,
    country_code=None,
    use_cache=True,
    progress=True,
    **budgets,
):
    """Fetch everything cafein needs for an area in one call.

    Pass exactly one of ``aoi`` (a geometry, bbox or place name geocoded
    once) or ``place`` (a place name, QID or :class:`Place`). With
    ``place``, feeds are selected from the feed index by tier -- ``tiers``,
    ``exclude`` and ``on_unknown`` filter the edges -- and the place geometry
    supplies the AOI. With ``aoi``, the feeds come from the feed index too
    when its places cover at least half of the area's land
    (:func:`transitio.index.area`, ``country_code`` keeping one country's
    places): those of the area's places, selected as for ``place``
    (:meth:`~transitio.index.Area.feeds`), and ``FetchResult.places`` lists
    the places. When no index is installed, the index has no places or they
    cover less than half of the area, the call falls back to the catalogue
    path: the Mobility Database catalogue is searched for feeds whose
    bounding box meets the area's, and before any feed or OSM download a
    ``UserWarning`` gives the reason and the number of feeds found, e.g.
    ``"the feed index's places cover 20% of the area; 3 feeds from the
    Mobility Database catalogue by bounding box"``, and without an installed
    index how to install one. ``index=False`` searches the catalogue without
    a warning; it is refused with ``place``. ``tiers``, ``exclude``, ``on_unknown``,
    ``on_untrusted_selector``, ``contained``, ``feeds`` and ``credentials``
    apply on both index paths; on the catalogue path, ``tiers``,
    ``exclude``, ``on_unknown="exclude"``, another ``on_untrusted_selector``
    or ``contained`` than the default, ``feeds`` and ``credentials`` raise
    ``ValueError`` naming the option and the reason, before anything is
    downloaded. ``country_code`` applies only with ``aoi``. When
    a selector cannot be trusted -- its evidence was missing at build time,
    or its fingerprint no longer
    matches the download -- ``on_untrusted_selector`` decides the outcome:
    ``"auto"`` (default) skips the feed when an ``exclude`` was asked for and
    otherwise delivers it whole with its tier treated as ``unknown``;
    ``"whole"`` always delivers it whole; ``"drop"`` always skips it;
    ``"error"`` raises :class:`~transitio.exceptions.StaleSelectorError`.
    A schema-10 index records the larger feeds whose stops and routes contain
    a feed's (``IndexedFeed.contained_in``). With ``contained="drop"``
    (default) containers are processed first, and a contained feed is left
    out before download (``"contained in <id>"``)
    when a container was delivered whole (not cut to a route selection) or
    skipped as the same content as a feed delivered whole, and conditional
    ``HEAD`` probes (as for ``expired``) prove both archives unchanged since
    indexed: the container's, sent before its download, and the contained
    feed's. A container downloaded as a catalogued dataset proves nothing.
    Otherwise the feed is processed as usual and its ``note`` says why:
    ``"kept: containment in <ids> not proven current"``, ``"kept: container
    <id> skipped"`` or ``"kept: container <id> cropped to selected routes"``.
    ``FetchResult.contained`` maps each feed left out as contained to the
    delivered feeds that carry it, in selection order: its containers, each
    one skipped as the same content as a feed delivered whole standing for
    that feed, and, with a container that lost repeated trips (below), the
    feeds holding them; a feed with no such feed delivered is not listed.
    ``contained="keep"`` leaves no feed out for containment, and
    ``contained`` is empty.

    Resolves and crops the OSM extract, discovers the GTFS feeds (the
    indexed feeds of the place or the area, or the catalogue's overlapping
    the AOI), downloads each feed, spatially
    crops it, optionally repairs it, validates it, and builds a merged report
    per feed.
    With an API token, downloads come from catalogued dataset versions
    (checksum-verified, with the hosted canonical-validator report);
    without one, the unversioned latest hosted zip is fetched — a moving
    target with no upstream checksum, documented in its provenance
    sidecar as such. On the index paths, a feed without a catalogued dataset,
    or whose dataset download fails, is read from the first of its indexed
    URLs that serves a zip archive: the Mobility Database direct download,
    the Transitland Atlas static feed, then the Mobility Database hosted
    copy; on a schema-11 index the URL the index crawled
    (``IndexedFeed.download_url``) comes first and the hosted copy second.
    A URL fragment names the member of the archive the feed is read
    from, a nested zip (``.../gtfs.zip#1/google_transit.zip``) or a folder;
    each such archive is downloaded once per call, and the feed's sidecar
    records its ``archive_url`` and ``archive_sha256``. Each download is
    kept in the cache as a version of its feed, ``<cache_dir>/gtfs/<feed
    folder>/<sha256>.zip`` beside a ``.provenance.json`` sidecar recording
    every acquisition of those bytes; a download identical to a cached
    version keeps that version. A report's provenance describes the
    version's first acquisition: its ``source_url``, ``retrieved_at``,
    ``fetched_from`` and ``download_errors``, with ``sha256``, ``feed_id``
    and, for a catalogued dataset, ``dataset_id`` and
    ``service_date_range``.

    On the index paths a cached version that serves the request is used
    without a download. A feed's versions are tried before any dataset
    selection, probe or download: first the one that served the same request
    before, then the newest first, each checked as a download is (route
    selector, crop, validation, the day checks). One whose route selector no
    longer matches the index is passed over; when nothing else serves and no
    download succeeds, ``on_untrusted_selector`` decides on the newest such
    one. A day no cached version serves downloads the feed, keeping the older
    versions, so a repeated call delivers what it delivered before, offline
    too. A warm cache can decide differently from a cold one: a version that
    serves the day is used without the ``expired`` probe, and a cached
    version counts as unchanged since indexed for ``contained`` only when a
    probe proved it so under the index snapshot in use. A protected feed
    without usable credentials uses only versions once fetched without
    them; credentials for a provider count alike whichever key they hold.
    The hosted validation report of a dataset is stored with it at its first
    use, so a reused dataset is reported as when downloaded. The catalogue
    path reuses its feeds' versions alike, though the Mobility Database is
    still searched for the feeds; a dataset is selected only for a feed no cached
    version serves. What the crop, repair and validation make of a cached
    version is stored with it, keyed by the transitio release, the exact
    area when cropped to it, the routes, ``crop``, ``repair`` and the
    budgets, and a later call
    making the same reads it back; the mode filter, the day checks and the
    report run again on every call, and deleting a version deletes what was
    made of it. Every overlapping feed is processed, in a
    deterministic order with official feeds first; one broken feed never
    aborts the others — it lands in ``skipped`` with its reason. A feed
    lacking a file GTFS requires is skipped, with or without ``when`` and
    ``repair``: ``"missing required file agency.txt"`` (several
    names sorted, ``"missing required files ..."``) or ``"missing calendar.txt
    and calendar_dates.txt"``, joined with ``"; "`` when both apply; a crop
    that keeps no trip leaves both calendar files out. A download whose
    content equals a feed already delivered in the call is skipped as
    ``"same content as <feed id>"`` when its routes are within those
    delivered from that archive (a feed delivered whole carries all);
    otherwise it is delivered cut to its own routes, ``same_as`` naming the
    earlier feed.

    On the index paths, delivered feeds whose route keys (agency name, route
    short else long name, mode) and stops (coordinates at 3 decimals) share
    0.9 and 0.8 or more are versions, ranked by later start, more trips,
    then candidate order. Agency names compare casefolded, without
    diacritics, punctuation or a trailing legal form (``Ltd``, ``Oy``,
    ``S.A.`` and the like). A feed with at most one agency row, none named,
    whose routes name at most one ``agency_id`` is unnamed, and its pairs
    compare routes by name and mode only. With ``when``, one is left out as
    ``"another version of <id>"`` when a kept version pairs with it and kept
    versions run, by trip signature (which leaves out the agency), every
    trip it runs on the day, a headway trip matching one with the same
    stops, relative times and frequency rows; the top version and the
    containers a left-out feed relied on stay, as does one with transfers or
    pathways. With ``when``, a feed whose trips running on the day all
    belong to services with a calendar.txt row spanning 4,000 days or more
    is undated, noted ``"placeholder calendar <start> to <end>"``, the
    widest dates of those rows, and ranks by its placeholder start. An
    undated feed also pairs on stops alone with a dated one starting after
    its placeholder start, so its ``version_of`` route overlap may be under
    0.9, and a kept such version leaves it out whatever it runs on the day
    and its transfers or pathways; the top version and the containers
    relied on still stay. A left-out version's fares are not delivered.
    Without ``when`` none is left out; similar feeds are noted. A feed
    whose routes, stops or, with ``when``, calendars cannot be read, or with
    a blank agency name that is not unnamed, is never a version.

    With ``duplicate_trips="drop"`` (default) the delivered feeds do not
    repeat each other's trips. A trip that a feed earlier in the selection
    record also runs is left out of the later feed: on the index paths the
    record follows the view (:meth:`~transitio.index.Place.feeds` or
    :meth:`~transitio.index.Area.feeds`: category, then relevance, then id),
    on the catalogue path the order above.
    Trips compare as :func:`~transitio.gtfs.merge_feeds` compares them:
    route name and mode (the route type's basic mode, as ``modes`` names
    them, else the type itself, so local bus 704 equals bus 3), stops and
    times, nearly (within 50 m and 3 minutes; with ``"exact"`` only
    exactly), pickup and drop-off, and frequencies, whatever the agency.
    ``"keep"`` compares nothing and delivers every feed with all its trips.
    With ``when`` the trips running that day are compared, each earlier
    trip covering one later trip; without it a trip is left out only when
    it is covered on every date it runs, which reads the whole calendar
    and, on a large network, takes longer and more memory than a study day.
    A trip whose services cannot be read or are not declared, that cannot
    be signed or that transfers.txt names is never left out. A feed
    declaring a time zone not equivalent to the others' (as
    ``merge_feeds(timezones="skip")`` decides), over ``max_total_bytes`` or
    unreadable is not compared. A feed that loses trips is delivered
    cropped without them
    (:func:`~transitio.gtfs.crop_feed`'s ``exclude_trips``), so the stops,
    shapes, calendars, routes, transfers and pathways that only those trips
    used go too; with ``crop=False`` and no route selection this is the
    feed's first crop. Delivered feeds are separate feeds, so no transfer
    or pathway links them. The cut feed is validated again, its report
    (listing also the rows this crop left out) and ``feed_window`` come
    from it, and its note says ``"<n> repeated trips of <ids> left out"``.
    A feed whose every trip in scope repeats is skipped as ``"every trip
    repeats a trip of <ids>"`` (with ``when``, ``"every trip on <day>
    repeats a trip of <ids>"``), and with ``modes`` a feed left without a
    requested mode as ``"serves [...] after repeated trips were left out,
    not [...]"``, the later feeds then compared again without it; the
    ``feed_window`` of either is that of what it kept. A step that fails,
    a crop that would also leave out trips repeating nothing, or a cut feed
    lacking a file GTFS requires keeps the feed's trips, noted ``"repeated
    trips kept: <error>"``. What was found is stored with
    each feed's cached version, keyed by the delivered feeds and their
    outputs, ``duplicate_trips``, the day and ``modes``, and a later call
    comparing the same reads it back.

    Parameters
    ----------
    aoi : geometry, GeoDataFrame/GeoSeries, tuple or str
        Area of interest (place names are geocoded via Nominatim once,
        and the resulting geometry drives every stage).
    when : str or datetime.date, optional
        Study day the feeds must run on, ``YYYY-MM-DD``. Dataset-version
        selection needs an API token; with or without one, a feed is
        skipped when its computed service window (the outer bounds of
        actual calendar activity, not the published range) ends before the
        day (``"service ended <end>"``) or starts after it (``"service
        starts <start>, after <day>"``), or when its validation report for
        the day counts no active trip and carries the
        ``no_service_on_reference_date`` notice (``"no service on
        <day>"``). Without ``when`` there is no study day, only today: a
        feed is skipped only when its computed window ended before today,
        and one that starts later or runs on other weekdays stays. An
        unknown window passes the window checks, and a report without a
        ``moment`` for the day passes the day check.
    feeds : list of str, Recommendation or object with ``feed_ids``, optional
        Fetch only these feeds of the index: a list of feed ids (or one id),
        a :class:`~transitio.index.Recommendation`, whose place and day the
        call then takes (``fetch(feeds=place.recommend(day))``; another
        ``place``, any ``aoi`` or a ``when`` on another day raises
        ``ValueError``),
        or an object whose ``feed_ids`` attribute lists them. They are
        taken from every relevance category of the place, or of the area's
        places, not only the default view, and ``tiers``, ``exclude`` and
        ``on_unknown`` still apply; ``selection`` lists only them. An id
        not indexed for the place, or not in the tiers asked, raises
        ``ValueError`` before anything is downloaded, as does an empty list.
    credentials : mapping, optional
        Credentials for feeds that need an account with their provider, as
        ``{provider_id: {field: value}}``. They win, field by field, over
        the environment and the credentials file
        (:mod:`transitio.credentials`); a provider the index does not list,
        a field it does not issue or a value that is not a non-empty string
        raises ``ValueError`` before anything is downloaded. A protected
        feed (``IndexedFeed.access == "key"``) is not downloaded from its
        producer's URLs when the index has no access details for it
        (``"protected feed: the index has no access details for it"``),
        transitio cannot send its credentials (``"protected feed: its
        access method is not supported"``, a cookie included), its URL is
        not https (``"protected feed: its URL is not https"``), none or only
        some of the provider's fields are set (``"protected feed:
        credentials missing for <provider>"``, ``"... credentials
        incomplete for <provider>"``) or a value has characters its method
        cannot carry (``"protected feed: credential <field> has characters
        its method cannot carry"``); each reason ends with ``"; "`` and the
        feed's :meth:`~transitio.index.IndexedFeed.access_instructions`.
        Such a feed is read without credentials from the Mobility
        Database's hosted copy when its catalogue record names one (with an
        API token, from its dataset versions first, as an open feed); the
        reason then goes into the note of a delivered feed, after ``"from
        the Mobility Database hosted copy"`` when the copy was read, and of
        a failed download. A feed without a copy is skipped with the
        reason. Otherwise the feed is probed (for ``expired``) and
        downloaded from its access URL, then, if that fails, from the
        hosted copy without the credentials. They are sent only to the
        access URL's scheme, host and port: a redirect elsewhere carries
        none, and a redirect to http or holding a credential fails that
        download. No reason, note, path, sidecar or report holds a
        credential, beyond what a server writes into the feed itself, which
        is kept as served.
    modes : str or list of str, optional
        Keep only feeds serving at least one of ``tram``, ``subway``,
        ``rail``, ``bus``, ``ferry`` — decided from the delivered
        (post-crop) feed's routes.txt, since the catalog carries no mode
        metadata. Unknown mode names raise ``ValueError``.
    duplicate_trips : {"drop", "exact", "keep"}, default "drop"
        Whether to leave out of the delivered feeds the trips that repeat,
        or nearly repeat (``"drop"``), a trip of an earlier delivered feed,
        as above: ``"exact"`` leaves out exact repeats only, and ``"keep"``
        compares nothing. Another value raises ``ValueError``.
    expired : {"skip", "keep"}, default "skip"
        With ``"skip"``, on the index paths, an indexed feed whose index
        service window misses the day (ends before it, or starts after the
        study day) is skipped before download when a conditional ``HEAD``
        to the URL the index crawled, carrying the ETag or Last-Modified it
        recorded, answers 304 Not Modified: the served archive is still the
        one the index saw, and the reason ends ``"; unchanged since
        indexed"``. Any other answer, or no recorded validator, downloads
        the feed and leaves the decision to its computed window, as does a
        feed downloaded as a catalogued dataset (with an API token).
        ``"keep"`` sends no probe and, without ``when``, keeps a feed whose
        computed window ended; with ``when`` the window and day checks
        still apply.
    repair : bool, default False
        Repair each feed (gtfstidy contract) after the crop, before use;
        conservative default leaves feeds untouched.
    crop : bool, default True
        Spatially crop each feed to the area: to its polygon when it has
        one (a place's boundary included), otherwise to its bounding box.
        The crop, also run for a route selection, leaves out a trip naming
        a route the feed lacks, a stop_times row naming a stop it lacks, a
        trip such rows leave with fewer than two stop_times, and an exact
        repeat of a trips.txt row (:func:`~transitio.gtfs.crop_feed`); a
        feed whose routes.txt or stops.txt has no id column, or whose
        trips.txt repeats a ``trip_id`` with other values, is skipped.
    osm : bool, default True
        Fetch the OSM extract for the AOI. With ``place``, it is fetched
        after the feeds, for the place's parts that hold a stop of a
        delivered feed (the whole place when none does, nothing was
        delivered or a delivered feed's stops.txt cannot be read), each part
        grown by 1.6 km, cafein's default snap distance. Parts of that area
        farther than 1.6 km from every delivered stop may lack OSM data, as
        the extract need only cover the stops' surroundings. With ``aoi`` it
        covers the area itself, from the index or the catalogue. The crop keeps
        each trip that serves the area whole, so a delivered feed's stops
        can lie beyond the OSM area and get no footpaths in cafein;
        ``stops_outside_osm`` in ``selection`` counts them. With ``osm=False``
        the OSM stage is skipped and the result's ``osm_pbf`` is None, for
        callers who want only the GTFS feeds; ``to_pyrosm`` then raises and
        ``to_cafein`` builds without a walking network. A failed extract
        download does not abort the call: the feeds are still delivered,
        ``osm_pbf`` and ``osm_area`` are None, a ``UserWarning`` says so and
        ``osm_note`` holds ``"OSM extract not fetched: <error>"``;
        ``to_cafein`` then builds without a walking network, as
        with ``osm=False``. Other errors, such as ``ExtractNotFoundError``
        when no extract covers the area, still raise.
    directory : str or pathlib.Path, optional
        Where the delivered feeds are copied, each as ``<name>.zip`` beside
        its provenance sidecar ``<name>.provenance.json``: the cropped,
        route-filtered, repaired or deduplicated feed, or the cached version
        when none of those ran; a feed cut of repeated trips is written over
        its copy there. The name is the feed id when it is lowercase ASCII
        letters, digits, ``_``, ``~`` and ``-``, starting with a letter or
        digit, at most 100 characters and not a Windows device name
        (``con``, ``nul``, ``com1`` and the like); otherwise it is the id's
        ASCII form cut to 80 characters, ``+`` and the id's SHA-256, e.g.
        ``f-u2f-prazskaintegrovanadoprava+<sha256>`` for
        ``f-u2f-pražskáintegrovanádoprava``. A later call delivering the
        same feed into the same directory replaces its files, and one that
        delivers it and then leaves it out for repeating other feeds' trips
        removes its archive; a feed skipped before delivery leaves a file an
        earlier call delivered for it in place. Calls running at the same
        time need directories of their own. Without it the delivered feeds
        are the files in the cache, an untransformed one the read-only
        cached version itself. The OSM extract goes here too. It must lie
        outside the download cache (``ValueError``).
    index : Index, str, pathlib.Path or False, optional
        The feed index both index paths select from: an
        :class:`~transitio.index.Index` or the path of one, by default the
        installed index (:func:`transitio.index.refresh`). ``False``, with
        ``aoi`` only, searches the catalogue instead.
    refresh_token, cache_dir, country_code
        Passed to the catalog and OSM layers; downloads are cached under
        ``cache_dir``, by default the platform cache. ``country_code`` also
        keeps only that country's index places for ``aoi``.
    use_cache : bool, default True
        Serve feeds from the cache as above. ``False`` downloads every feed
        again and, once a download succeeds, deletes the feed's other
        versions; when every attempt fails, the version a cached call would
        use is delivered with a ``UserWarning``.
    progress : bool, default True
        Progress lines and download bars on stderr; a widget bar in Jupyter
        when ipywidgets is installed (``pip install "transitio[notebook]"``).
        Elsewhere a bar shows on a terminal only, and a line names the
        download instead. ``False`` prints nothing.
    **budgets
        The ``validate_feed`` keyword arguments. A feed with a table that a
        budget cuts short cannot be cropped and lands in ``skipped``, the
        reason naming the file and the budget to raise. A reached
        ``max_notices_per_file`` does not stop the crop (the feed's report
        then carries ``notice_limit_reached``), but it does stop
        ``repair=True``. ``max_total_bytes`` also bounds a feed read from
        inside a larger archive. With ``when``, ``reference_date`` is the
        study day; a different one raises ``ValueError``.

    Returns
    -------
    FetchResult
        ``osm_pbf``, validated ``feeds`` (paths), merged ``reports`` and
        repair ``repairs`` (fix logs, empty without ``repair=True``) per
        kept feed, ``paths`` (``{feed id: path}`` of the same feeds, in the
        order of ``feeds``), ``skipped`` (feed id, reason) pairs, the
        ``selection`` record, on the index paths ``selections`` and
        ``contained`` (above), and ``places``, the index places the feeds
        were selected for (the place, the area's parts; empty on the
        catalogue path). Reports merge the local validation of the
        delivered feed with the hosted report of the published dataset, so
        after cropping or repair the hosted side describes the pre-transform
        original. A report's ``summary["droppedRows"]`` lists the rows the
        crop left out (the ``dropped_rows`` of
        :func:`~transitio.gtfs.crop_feed`), None for a feed not cropped.
        ``selection`` has one entry per candidate feed, in
        candidate order: ``feed_id``, ``name``, ``decision``
        (``"delivered"`` or ``"skipped"``), ``reason`` (why it was skipped),
        ``note`` (about a delivered feed: first ``"cut to <n> of <m>
        routes"`` for one cut to a route selection (``"cut to <n> selected
        routes"`` when its routes.txt was not read), or ``"delivered whole:
        selector out of date"`` or ``"delivered whole: selector
        unavailable"`` for one whose selector was not trusted, as
        ``selections`` details; then why a contained feed was kept, a
        similar feed, ``"from the Mobility Database hosted copy"`` after a
        failed download, an ``agency_timezone`` not equivalent to the zone
        of most of its stops,
        e.g. ``"agency_timezone America/New_York; stops in
        Pacific/Honolulu"``, a placeholder calendar, which a left-out
        version keeps, rows the crop left out, e.g. ``"dropped 1860
        stop_times.txt rows whose stop_id is not in stops.txt"`` or
        ``"dropped 8 exact duplicate trips.txt rows"``, the repeated trips
        left out or kept; several join with ``"; "``),
        ``index_window`` (the index's ``[start, end]``; None undated or on
        the catalogue path), ``feed_window`` (the computed window of a validated
        download, delivered or not; None otherwise or when unknown),
        ``same_as`` (earlier deliveries of the same archive) and
        ``contained_in`` (the index's containers a containment skip names),
        ``version_of`` (for a left-out version, ``{"feed_id", "route_overlap",
        "stop_overlap"}`` against the highest-ranked kept version it pairs
        with, for an undated feed the highest-ranked dated one starting after
        its placeholder start when one does), ``duplicate_trips`` (the
        number of trips left out as repeats of an earlier delivered feed's:
        0 for a compared feed that lost none, the first included; None for
        a feed not compared: with ``duplicate_trips="keep"`` or fewer than
        two feeds delivered, one in another time zone, unreadable or over
        ``max_total_bytes``, and a feed decided before the comparison),
        ``fetched_from`` (where the
        download came from: ``"mdb_dataset"`` a catalogued dataset,
        ``"producer"`` the feed's own URL, its ``download_url`` or one from
        the Mobility Database or Transitland Atlas, ``"mdb_latest"`` the
        Mobility Database hosted copy; None when nothing was downloaded),
        ``download_errors`` (the failed download attempts before the one that
        worked, or all of them when none did, joined with ``"; "``; None when
        none failed or none was made), ``cache`` (``"downloaded"``,
        ``"reused"``, ``"refreshed"`` or ``"fallback"``, how the feed's
        version was obtained; None when none was), a reused version's
        ``fetched_from`` being its first acquisition's,
        ``stops_outside_osm`` (the delivered feed's located stops, the
        stops.txt rows with usable coordinates other than (0, 0), outside
        ``osm_area``; None without an extract or when its stops.txt cannot
        be read) and
        ``path`` (the delivered feed).
        Windows are ISO dates.
        ``selections``, with ``tiers``, ``exclude`` or
        ``on_unknown="exclude"``, has one entry per feed whose route
        selector was checked, in the order decided: ``feed_id``,
        ``selector_state`` (``"complete"``, ``"whole_feed"`` or
        ``"unavailable"``), ``trusted``, ``reason`` (why it was not trusted:
        ``"stale"``, ``"unavailable"`` or ``"route_absent"``; None when
        trusted), ``kept`` and ``dropped`` (the feed's routes the selector
        kept and removed, sorted; None when the feed was not cut, ``dropped``
        ``[]`` for a whole-feed selector, or when its routes.txt was not
        read), ``declared_as`` (the curator predicate of a complete selector
        made from one, else None) and ``selected_by`` (per matched edge
        ``{"tier", "selector_state", "route_ids"}``).
        When ``place`` is fetched without ``tiers`` and its default view
        (:meth:`~transitio.index.Place.feeds`) holds none of the place's
        feeds, or an area's places hold none of theirs in their views,
        ``view_note`` names them and the tiers that fetch them, and a
        ``UserWarning`` repeats it, e.g. ``"default view (region: secondary,
        tertiary) holds none of the place's 2 feeds: f-a (primary), f-b
        (primary); tiers=['local'] fetches them"``.
        When the OSM extract leaves out parts of the place, or delivered
        stops lie outside its area, ``osm_note`` notes them, e.g. ``"OSM
        area: 1 of 47 parts (1783 of 2188 km²); 4970 of 10026 located stops
        outside it"``, the stops summed over the delivered feeds, with
        ``"(stops.txt of 1 feed not read)"`` added for feeds not counted;
        when its download failed, it notes that instead, e.g. ``"OSM
        extract not fetched: Could not download any of the 1 extracts that
        contain the area:
        https://download.bbbike.org/osm/bbbike/Basel/Basel.osm.pbf (timed
        out)"``. ``FetchResult.selection_table()`` returns
        the record as a DataFrame. ``osm_area`` is the WGS84 geometry the OSM
        extract was fetched for (None with ``osm=False`` or a failed
        download).
    """
    progress = _Progress(progress)
    if credentials is not None:
        from transitio.credentials import _explicit

        credentials, problem = _explicit(credentials)
        if problem is not None:
            raise problem
    from transitio.catalog import MobilityDatabase
    from transitio.catalog._models import as_date
    from transitio.exceptions import DownloadError
    from transitio.osm._fetch import _as_geometry

    place, when = _from_recommendation(feeds, place, aoi, when)
    if (aoi is None) == (place is None):
        raise ValueError("pass exactly one of aoi= or place=")
    if contained not in ("keep", "drop"):
        raise ValueError("contained= must be 'keep' or 'drop'")
    if expired not in ("skip", "keep"):
        raise ValueError("expired= must be 'skip' or 'keep'")
    if place is not None and country_code is not None:
        raise ValueError("country_code= applies only with aoi=")
    if place is not None and index is False:
        raise ValueError("index=False applies only with aoi=")
    if on_untrusted_selector not in ("auto", "whole", "drop", "error"):
        raise ValueError(
            "on_untrusted_selector= must be 'auto', 'whole', 'drop' or 'error'"
        )
    if duplicate_trips not in ("drop", "exact", "keep"):
        raise ValueError("duplicate_trips= must be 'drop', 'exact' or 'keep'")
    if feeds is not None:
        feeds = getattr(feeds, "feed_ids", feeds)
        feeds = {feeds} if isinstance(feeds, str) else set(feeds)
        if not feeds:
            raise ValueError("feeds= names no feed")

    if modes is not None:
        from transitio.gtfs._schedule import MODE_TYPES

        if isinstance(modes, str):
            modes = [modes]
        modes = {str(mode).lower() for mode in modes}
        unknown = modes - set(MODE_TYPES)
        if unknown:
            raise ValueError(
                f"unknown modes {sorted(unknown)}; "
                f"valid modes are {sorted(MODE_TYPES)}"
            )

    # The day the date rules test: the study day, else today. A downloaded
    # feed's computed window is tested against it unless expired="keep"
    # leaves nothing to test without a study day.
    study = when is not None
    day = as_date(when) if study else _today()
    window_day = day if study or expired == "skip" else None
    if study:
        existing = budgets.get("reference_date")
        if existing is not None and existing != day.strftime("%Y%m%d"):
            raise ValueError("when and reference_date disagree; pass only one")
    budgets.setdefault("reference_date", day.strftime("%Y%m%d") if study else None)

    if aoi is not None:
        geometry = _as_geometry(aoi)
        place, why = _area_for(geometry, index, country_code)
    if place is not None:
        return _fetch_place(
            place,
            tiers=tiers,
            exclude=exclude,
            on_unknown=on_unknown,
            on_untrusted_selector=on_untrusted_selector,
            contained=contained,
            wanted=feeds,
            index=index,
            credentials=credentials,
            when=when,
            day=day,
            window_day=window_day,
            expired=expired,
            modes=modes,
            duplicate_trips=duplicate_trips,
            repair=repair,
            crop=crop,
            osm=osm,
            refresh_token=refresh_token,
            cache_dir=cache_dir,
            directory=directory,
            use_cache=use_cache,
            budgets=budgets,
            progress=progress,
        )

    # The options only the index paths honour, refused on the catalogue path.
    named = {
        "tiers=": tiers is not None,
        "exclude=": exclude is not None,
        f"on_unknown={on_unknown!r}": on_unknown != "include",
        f"on_untrusted_selector={on_untrusted_selector!r}": (
            on_untrusted_selector != "auto"
        ),
        f"contained={contained!r}": contained != "drop",
        "feeds=": feeds is not None,
        "credentials=": credentials is not None,
    }
    refused = [name for name, given in named.items() if given]
    if refused:
        need = "needs" if len(refused) == 1 else "need"
        raise ValueError(f"{', '.join(refused)} {need} the feed index; {why}")

    cache = _feed_cache(cache_dir, directory)
    osm_pbf = osm_note = None

    from transitio.catalog._client import _dataset_entry

    feeds, reports, repairs, record, processed = [], [], [], [], []
    delivered = _Delivered()
    # Everything but the version that decides whether one serves.
    key = _request_key(
        day=day.isoformat() if study else None,
        expired=expired,
        area=hashlib.sha256(geometry.wkb).hexdigest(),
        crop=crop,
        repair=repair,
        modes=None if modes is None else sorted(modes),
        budgets=budgets,
    )
    undated = []
    with MobilityDatabase(refresh_token, cache_dir=cache_dir) as db:
        tokenless = when is not None and not db._refresh_token
        # Without a token the search reads the catalogue export, downloaded
        # when the cached copy is missing or a day old.
        with progress.downloads("Downloading the Mobility Database catalogue"):
            found = db.search_feeds(aoi=geometry, country_code=country_code)
        candidates = sorted(found, key=_rank)
        progress.start(len(candidates), "from the Mobility Database catalogue")
        if index is not False:
            count = len(candidates)
            found = f"{count} feed{'' if count == 1 else 's'}"
            hint = ""
            if why == _NO_INDEX:
                hint = "; transitio.index.refresh() installs the feed index"
            warnings.warn(
                f"{why}; {found} from the Mobility Database catalogue by "
                f"bounding box{hint}",
                UserWarning,
                stacklevel=2,
            )
        if osm:
            progress.say("Fetching the OSM extract")
            with progress.downloads("Downloading the OpenStreetMap extract"):
                osm_pbf, osm_note = _osm_extract(
                    geometry,
                    cache_dir=cache_dir,
                    directory=directory,
                    progress=progress.bar or False,
                )

        def take(feed, entry, version, dataset_id, hosted, last=True):
            # ``version`` checked against the feeds delivered so far and
            # processed: "delivered" or "skipped", or "rejected" for a cached
            # candidate that is not the ``last`` to try and does not serve.
            path = version.path
            twins = [twin for twin, _ in delivered.same_as(path)]
            if twins:
                _skip(entry, f"same content as {', '.join(twins)}", same_as=twins)
                return "skipped"
            origin = _report_provenance(version, dataset_id)
            try:
                # Modes are read from the delivered feed, after cropping, so an
                # aggregate serving buses only outside the AOI does not pass a
                # bus filter.
                path, report, made, made_key, window = _process_feed(
                    path,
                    provenance=origin,
                    outputs=(cache, version),
                    geometry=geometry,
                    repair=repair,
                    crop=crop,
                    modes=modes,
                    day=window_day,
                    study=study,
                    hosted=hosted,
                    budgets=budgets,
                    progress=progress,
                )
                path = _deliver(path, directory, origin)
                cache.touch(version, served=(key, dataset_id))
            except _SkipFeed as skip:
                if not last:
                    return "rejected"
                if tokenless and skip.missed_day:
                    undated.append(feed.id)
                _skip(entry, skip.reason, feed_window=skip.window)
                return "skipped"
            except Exception as error:  # noqa: B902 — isolate per-feed failures
                if not last:
                    return "rejected"
                _skip(entry, f"processing failed: {error}")
                return "skipped"
            entry.update(decision="delivered", feed_window=window, path=path)
            for note in (
                _timezone_note(path, budgets.get("max_total_bytes")),
                _dropped_note(report),
            ):
                if note is not None:
                    _note(entry, note)
            reports.append(report)
            repairs.append(made["fixes"])
            feeds.append(path)
            served = (key, dataset_id)
            item = _Processed(entry, version, served, made, made_key, origin, hosted)
            processed.append(item)
            delivered.add(feed.id, version.path)
            return "delivered"

        def reuse(feed, entry, versions, label):
            # The cached versions in order until one decides the feed.
            for version in versions:
                if not cache.intact(version):
                    continue
                context = _context(version, key)
                hosted = _hosted(db, cache, version, context)
                outcome = take(feed, entry, version, context, hosted, last=False)
                if outcome == "rejected":
                    continue
                entry.update(fetched_from=version.first_source["fetched_from"])
                entry["cache"] = label
                if label == "fallback" and outcome == "delivered":
                    _warn_fallback(feed.id, entry["download_errors"], version)
                return True
            return False

        def failed(feed, entry, cached, reason, errors):
            # The feed could not be downloaded, for ``reason``; a refresh
            # falls back to what the cache serves.
            entry["download_errors"] = errors
            if use_cache or not reuse(feed, entry, cached, "fallback"):
                _skip(entry, reason)

        def entry_for(feed):
            record.append(_entry(feed.id, feed.raw.get("feed_name") or feed.provider))
            return record[-1]

        for feed, entry, staging in _held(cache, candidates, entry_for, progress):
            cached = _candidates(cache, feed.id, key, False)
            if use_cache and reuse(feed, entry, cached, "reused"):
                continue
            dataset = None
            if db._refresh_token:
                try:
                    if when is not None:
                        dataset = db.dataset_for(feed, when)
                        if dataset is None:
                            _skip(entry, "no dataset covers the requested day")
                            continue
                    else:
                        # Prefer a versioned dataset (checksum, hosted
                        # report) over the unversioned moving target.
                        versions = db.datasets(feed)
                        dataset = versions[0] if versions else None
                except Exception as error:  # noqa: B902
                    reason = f"dataset selection failed: {error}"
                    failed(feed, entry, cached, reason, f"dataset selection: {error}")
                    continue
            try:
                if dataset is not None:
                    path = db._fetch_dataset(dataset, directory=staging)
                else:
                    path = db._fetch_latest(feed, directory=staging)
            except Exception as error:  # noqa: B902
                failed(feed, entry, cached, f"download failed: {error}", str(error))
                continue
            fetched_from = "mdb_latest" if dataset is None else "mdb_dataset"
            try:
                version = _add_version(
                    cache,
                    feed.id,
                    path,
                    feed.latest_dataset_url if dataset is None else dataset.hosted_url,
                    fetched_from,
                    None,
                    dataset=dataset and {dataset.id: _dataset_entry(dataset)},
                    replace=not use_cache,
                )
            except DownloadError as error:  # not a zip archive
                failed(feed, entry, cached, f"download failed: {error}", str(error))
                continue
            except Exception as error:  # noqa: B902 — isolate per-feed failures
                _skip(entry, f"processing failed: {error}")
                continue
            entry.update(
                fetched_from=fetched_from,
                cache="downloaded" if use_cache else "refreshed",
            )
            # Read as a reuse would, so identical bytes report alike.
            dataset_id = _context(version, key)
            take(
                feed,
                entry,
                version,
                dataset_id,
                _hosted(db, cache, version, dataset_id),
            )

    _warn_undated(undated, day, stacklevel=3)
    repeats = dict(budgets=budgets, modes=modes, day=day if study else None)
    repeats["duplicate_trips"] = duplicate_trips
    _drop_repeats(
        cache, record, processed, feeds, reports, directory, progress, **repeats
    )
    progress.left_out(record)
    rows = [
        n for n, item in enumerate(processed) if item.entry["decision"] == "delivered"
    ]
    feeds, reports, repairs = (
        [column[n] for n in rows] for column in (feeds, reports, repairs)
    )
    if osm_pbf is not None:
        coords = {path: _stop_coords(path) for path in feeds}
        counts = _count_outside(record, geometry, coords)
        _warn_outside(*counts, stacklevel=3)
        osm_note = _osm_note(geometry, geometry, *counts)
    progress.done(record)
    return FetchResult(
        osm_pbf=osm_pbf,
        feeds=feeds,
        reports=reports,
        repairs=repairs,
        skipped=_skipped(record),
        selection=record,
        osm_area=None if osm_pbf is None else geometry,
        osm_note=osm_note,
    )


def _selector_trusted(path, feed, sel):
    """``(trusted, reason, route_ids)`` for a feed's aggregated selector
    validated against the downloaded feed at ``path``.

    An ``unavailable`` selector is untrustworthy without validation -- it has
    no fingerprint to check. Otherwise every matched edge's stored fingerprint
    must recompute from the download by its own kind; the first that does not
    marks the selector stale. ``route_ids`` is the set the download carries,
    for the caller's route-presence check (each fingerprint kind reads the
    same ``routes.txt``, so the set is complete once any edge validated).
    """
    from transitio.index import fingerprint

    if sel.state == "unavailable":
        return False, "unavailable", set()
    recomputed = {}
    in_feed = set()
    for edge in feed.edges.values():
        kind = edge.fingerprint_kind
        stored = edge.classification_fingerprint
        if kind not in fingerprint.KINDS or not stored:
            return False, "unavailable", in_feed
        if kind not in recomputed:
            recomputed[kind] = fingerprint.from_feed(path, kind)
        digest, routes = recomputed[kind]
        in_feed |= routes
        if digest is None or digest != stored:
            return False, "stale", in_feed
    return True, None, in_feed


def _untrusted_action(policy, exclude, on_unknown):
    """Map an untrustworthy selector to ``"skip"``, ``"whole"`` or ``"error"``
    under ``on_untrusted_selector``.

    Under ``"auto"`` an explicit ``exclude`` is a hard constraint that cannot
    be honoured, so the feed is skipped; otherwise it is delivered whole with
    its tier treated as ``unknown``, and ``on_unknown`` decides its fate.
    """
    if policy == "error":
        return "error"
    if policy == "drop":
        return "skip"
    if policy == "whole":
        return "whole"
    # A truthy exclude names tiers to drop; an empty one excludes nothing, as
    # edge matching treats it, so it is not a hard constraint.
    if exclude:
        return "skip"
    return "skip" if on_unknown == "exclude" else "whole"


def _stop_coords(path):
    """The located stops of the feed at ``path``, its stops.txt rows with a
    stop id and usable coordinates other than (0, 0), which stands for a
    missing position, as an ``(n, 2)`` array of ``(lon, lat)``; None when
    its stops.txt is absent or cannot be read."""
    import numpy as np

    from transitio.index.fingerprint import _member_coords

    try:
        with zipfile.ZipFile(path) as archive:
            coords = _member_coords(archive)
    except Exception:  # noqa: B902 — unreadable, like an absent stops.txt
        return None
    if coords is None:
        return None
    points = np.array(list(coords.values()), dtype=float).reshape(-1, 2)
    return points[points.any(axis=1)]


def _located_stops(coords):
    """The located stops of every feed in ``coords`` (:func:`_stop_coords`,
    by path) as one ``(n, 2)`` array; None when there is no feed or a feed's
    stops.txt cannot be read."""
    import numpy as np

    located = list(coords.values())
    if not located or any(points is None for points in located):
        return None
    return np.concatenate(located)


def _osm_parts(geometry, feeds):
    """``(parts, coords)``: ``coords`` maps each delivered feed in ``feeds``
    to its located stops (:func:`_stop_coords`), and ``parts`` is what of
    ``geometry`` the OSM extract is fetched for, the union of the parts
    holding such a stop, else the whole geometry. A feed whose stops.txt
    cannot be read could serve any part, so it also yields the whole
    geometry."""
    import numpy as np
    import shapely

    coords = {path: _stop_coords(path) for path in feeds}
    located = _located_stops(coords)
    if located is None:
        return geometry, coords
    parts = shapely.get_parts(geometry)
    points = shapely.points(located)
    held = np.unique(shapely.STRtree(parts).query(points, predicate="intersects")[1])
    if len(held) in (0, len(parts)):
        return geometry, coords
    return shapely.union_all(parts[held]), coords


def _osm_stops(area, coords):
    """The located stops in ``coords`` (:func:`_osm_parts`) inside ``area``
    as a MultiPoint, what the OSM extract must cover; None when nothing was
    delivered, a feed's stops.txt cannot be read or no stop lies inside."""
    import numpy as np
    import shapely

    located = _located_stops(coords)
    if located is None:
        return None
    shapely.prepare(area)
    inside = np.unique(located[shapely.intersects_xy(area, located)], axis=0)
    return shapely.multipoints(inside) if len(inside) else None


def _hidden_note(place, hidden):
    """The ``view_note`` on a place, or an area, whose default view holds
    none of its ``hidden`` feeds: the view's categories (for an area, its
    places'), the feeds (at most five named) and the tiers that fetch them,
    local, regional and national when only unknown edges remain."""
    from transitio.index import Area
    from transitio.index.feeds import CATEGORY_ORDER, _default_categories

    if isinstance(place, Area):
        view, whose = "default view of the area's places", "their"
    else:
        shown = _default_categories(place, None, "default", False) or ()
        categories = ", ".join(c for c in CATEGORY_ORDER if c in shown)
        view, whose = f"default view ({place.kind}: {categories})", "the place's"
    named = ", ".join(
        f"{feed.feed_id} ({feed.relevance_category})" for feed in hidden[:5]
    )
    if len(hidden) > 5:
        named += f" and {len(hidden) - 5} more"
    order = ("local", "regional", "national", "international")
    found = set().union(*(feed.tiers for feed in hidden))
    tiers = [tier for tier in order if tier in found] or list(order[:3])
    feeds, them = ("feed", "it") if len(hidden) == 1 else ("feeds", "them")
    return (
        f"{view} holds none of {whose} {len(hidden)} {feeds}: {named}; "
        f"tiers={tiers} fetches {them}"
    )


def _count_outside(record, area, coords):
    """Set ``stops_outside_osm`` on each delivered entry of ``record``: the
    located stops of its feed (``coords``, by path) outside ``area``, left
    None when they are None. Returns ``(outside, total, unread)``, the
    outside and located stops summed over the counted feeds and the number
    of delivered feeds not counted."""
    import shapely

    shapely.prepare(area)
    outside = total = unread = 0
    for entry in record:
        if entry["decision"] != "delivered":
            continue
        points = coords.get(entry["path"])
        if points is None:
            unread += 1
            continue
        count = len(points) - int(shapely.intersects_xy(area, points).sum())
        entry["stops_outside_osm"] = count
        outside += count
        total += len(points)
    return outside, total, unread


def _warn_outside(outside, total, unread, stacklevel):
    """Warn when most of the ``total`` located stops of the delivered feeds,
    ``outside`` of them, lie beyond the OSM extract; ``stacklevel`` points at
    the caller of fetch."""
    if total and outside * 2 > total:
        warnings.warn(
            f"{outside:,} of the {total:,} located stops of the delivered feeds "
            "lie outside the OSM extract: their trips run on beyond the area, and "
            "routing may find no streets or paths to walk to them. Fetch a "
            "larger area to cover them, or crop the feeds with "
            "transitio.crop_feed(..., full_trips_only=True) to keep only the "
            "trips inside the area",
            UserWarning,
            stacklevel=stacklevel,
        )


def _osm_note(geometry, parts, outside=0, total=0, unread=0):
    """The ``osm_note`` on the OSM area: the parts of ``geometry``
    that ``parts`` leaves out, and the ``outside`` of ``total`` located
    stops outside the area with the ``unread`` feeds whose stops.txt was not
    read; None when no part is left out and neither count is non-zero."""
    import shapely

    from transitio.osm._fetch import _area_km2

    clauses = []
    whole, kept = shapely.get_num_geometries([geometry, parts])
    if kept != whole:
        clauses.append(
            f"{kept} of {whole} parts "
            f"({_area_km2(parts):.0f} of {_area_km2(geometry):.0f} km²)"
        )
    if outside or unread:
        clause = f"{outside} of {total} located stops outside it"
        if unread:
            feeds = "feed" if unread == 1 else "feeds"
            clause += f" (stops.txt of {unread} {feeds} not read)"
        clauses.append(clause)
    return "OSM area: " + "; ".join(clauses) if clauses else None


def _osm_extract(area, **options):
    """``(path, None)`` for the OSM extract :func:`~transitio.osm.fetch_pbf`
    fetches for ``area``, or ``(None, note)`` with a ``UserWarning`` when its
    download fails; any other error raises."""
    from transitio.exceptions import DownloadError
    from transitio.osm import fetch_pbf

    try:
        return fetch_pbf(area, **options), None
    except DownloadError as error:
        note = f"OSM extract not fetched: {error}"
        warnings.warn(f"{note}; osm_pbf is None", UserWarning, stacklevel=3)
        return None, note


def _fetch_place(
    place,
    *,
    tiers,
    exclude,
    on_unknown,
    on_untrusted_selector,
    contained,
    wanted,
    index,
    credentials,
    when,
    day,
    window_day,
    expired,
    modes,
    duplicate_trips,
    repair,
    crop,
    osm,
    refresh_token,
    cache_dir,
    directory,
    use_cache,
    budgets,
    progress,
):
    """The index paths, ``fetch(place=...)`` and ``fetch(aoi=...)`` given an
    :class:`~transitio.index.Area`: the place geometry, or the area's, is the
    AOI, feeds come from the index by tier, each served by a cached version
    when one serves the request, else downloaded MDB-then-Atlas (decision I)
    and then from the MDB hosted copy (:func:`_download_indexed`), and a
    bundled feed is cropped to the routes its matched tiers select, the drop
    recorded in ``selections``. A feed whose index window misses ``day`` is
    skipped before download when a probe proves the archive unchanged since
    indexed; ``window_day`` is what the computed window is tested against.
    A protected feed is decided before anything else (:func:`_access_for`);
    without credentials it is read from the hosted copy alone, or skipped
    when it has none, and its texts in the record are masked
    (:meth:`_Access.redact`). The versions among the delivered feeds are
    settled after the feed loop, then their repeated trips left out
    (:func:`_drop_repeats`), and the OSM extract comes last, for the parts
    of the place the remaining feeds serve, or for the area. ``progress``
    (:class:`_Progress`) says how it goes."""
    from transitio import __version__
    from transitio.catalog import Feed, MobilityDatabase, TransitlandAtlas
    from transitio.catalog._client import _dataset_entry
    from transitio.credentials import _checked, _provider
    from transitio.exceptions import DownloadError, StaleSelectorError
    from transitio.index import (
        DISCOVERY_SEMANTICS_VERSION,
        Area,
        Place,
        _coerce_index,
        place as resolve_place,
    )
    from transitio.index.feeds import _hosted_url
    from transitio.index.places import _as_shape
    from transitio.osm._fetch import _buffered

    if isinstance(place, Area):
        place_obj, resolved_index = place, place._index
    elif isinstance(place, Place):
        place_obj = place
        resolved_index = place._lookup._index
    else:
        resolved_index = _coerce_index(index)
        place_obj = resolve_place(place, index=resolved_index)
    provenance = {
        "snapshot": None if resolved_index is None else resolved_index.snapshot_id,
        "discovery_semantics_version": DISCOVERY_SEMANTICS_VERSION,
        "transitio_version": __version__,
    }
    geometry = _as_shape(place_obj.geometry)
    if geometry is None:
        raise ValueError(f"place {place_obj.id} has no geometry to fetch for")
    study = when is not None

    def candidates(unknown):
        # The view's feeds, or with ``wanted`` the named ones of any category.
        found = place_obj.feeds(
            tiers=tiers,
            exclude=exclude,
            on_unknown=unknown,
            categories="default" if wanted is None else None,
        )
        return found if wanted is None else [f for f in found if f.feed_id in wanted]

    offered = candidates(on_unknown)
    if wanted is not None:
        missing = wanted.difference(f.feed_id for f in candidates("include"))
        if missing:
            where = (
                "the area's places"
                if isinstance(place_obj, Area)
                else f"place {place_obj.id}"
            )
            asked = "" if tiers is None and exclude is None else " in the tiers asked"
            raise ValueError(
                f"feeds= names feeds not indexed for {where}{asked}: "
                f"{', '.join(sorted(missing))}"
            )
    kept = _containers_first(offered) if contained == "drop" else offered
    # Credentials are checked and resolved before any download.
    explicit = {
        provider_id: _checked(_provider(provider_id, resolved_index), fields)
        for provider_id, fields in (credentials or {}).items()
    }
    decided = {
        feed.feed_id: _access_for(feed, explicit)
        for feed in kept
        if feed.access == "key"
    }
    progress.masks = {
        feed_id: access.redact
        for feed_id, (access, _) in decided.items()
        if access is not None
    }

    def datable(feed):
        # Whether a token would let fetch pick a dated copy of the feed: one
        # the Mobility Database lists, read without credentials (with them,
        # fetch takes no dataset version).
        access = decided.get(feed.feed_id, (None, None))[0]
        return tokenless and access is None and _mdb_id(feed) is not None

    feeds, reports, repairs, selections, record = [], [], [], [], []
    delivered = _Delivered()
    delivered_ids, processed = [], []
    entries = {}
    # Containment state: the feeds carried by a feed delivered whole (itself,
    # or the feed whose content it repeats), those delivered cut to routes,
    # and whether a container's probe proved it unchanged before download.
    carriers, cropped, current, protected = {}, set(), {}, set()
    # Feeds with a dated copy in the Mobility Database whose newest copy
    # does not run on the day, while no token can pick the dated one.
    undated, tokenless = [], False
    container_ids = {c for feed in kept for c in feed.contained_in}
    probes, services, budget = {}, {}, budgets.get("max_total_bytes")

    def entry_for(feed):
        # The record follows candidate order, whatever order they are processed in.
        if feed.feed_id not in entries:
            window = _window(feed.service_start, feed.service_end)
            entries[feed.feed_id] = _entry(feed.feed_id, feed.name, window)
            record.append(entries[feed.feed_id])
        return entries[feed.feed_id]

    def take(
        feed, entry, version, notes, dataset_id, hosted, key, last=True, inside=()
    ):
        """Check, process and record ``version`` of ``feed`` for the request
        ``key``: its route selection, its content against the feeds delivered
        so far, then :func:`_process_feed`; ``notes`` and ``hosted`` go into
        the delivered feed's note and report, and a version that serves is
        left out as contained in the proven containers ``inside``. Returns
        ``"delivered"`` or ``"skipped"``, the decision in ``entry``. A cached
        candidate that is not the ``last`` to try is passed over, ``entry``
        left undecided: ``"stale"`` when its route selector no longer
        matches, ``"rejected"`` when processing drops it."""
        path = version.path
        # Route selection: a bundled feed whose matched tiers name a
        # trustworthy complete selector is cropped to those routes; a
        # whole-feed selector filters nothing. Every applied selector is
        # first validated against the download -- its build-time
        # fingerprint must recompute and every selected route id must be
        # present -- and an untrustworthy or unavailable selector routes
        # through on_untrusted_selector rather than filtering silently.
        # on_unknown="exclude" is itself an edge filter, so this activates
        # even without an explicit tiers/exclude query.
        routes = selection = applied = None
        if tiers is not None or exclude is not None or on_unknown != "include":
            sel = feed.selector
            selected_by = [
                {
                    "tier": edge.tier,
                    "selector_state": edge.selector_state,
                    "route_ids": sorted((edge.selector or {}).get("route_id") or []),
                }
                for edge in feed.edges.values()
            ]
            trusted, reason, in_feed = _selector_trusted(path, feed, sel)
            if trusted and sel.state == "complete" and set(sel.route_ids) - in_feed:
                trusted, reason = False, "route_absent"
            if not trusted:
                if not last and reason != "unavailable":
                    # Another version may match the selector.
                    return "stale"
                action = _untrusted_action(on_untrusted_selector, exclude, on_unknown)
                if action == "error":
                    error = StaleSelectorError(
                        f"{feed.feed_id}: selector untrustworthy ({reason})"
                    )
                    error.feed_id = feed.feed_id
                    raise error
                selection = {
                    "feed_id": feed.feed_id,
                    "selector_state": sel.state,
                    "trusted": False,
                    "reason": reason,
                    "kept": None,
                    "dropped": None,
                    "declared_as": None,
                    "selected_by": selected_by,
                }
                if action == "skip":
                    _skip(entry, f"untrustworthy selector ({reason})")
                    selections.append(selection)
                    return "skipped"
                # action == "whole": deliver unfiltered (routes stays None),
                # the selection recording why it was not filtered.
                state = "unavailable" if reason == "unavailable" else "out of date"
                applied = f"delivered whole: selector {state}"
            elif sel.state == "complete":
                routes = set(sel.route_ids)
                selection = {
                    "feed_id": feed.feed_id,
                    "selector_state": "complete",
                    "trusted": True,
                    "reason": None,
                    "kept": None,  # filled from the delivered feed below
                    "dropped": None,
                    "declared_as": sel.declared_as,
                    "selected_by": selected_by,
                }
            else:
                selection = {
                    "feed_id": feed.feed_id,
                    "selector_state": sel.state,
                    "trusted": True,
                    "reason": None,
                    "kept": None,
                    "dropped": [],
                    "declared_as": None,
                    "selected_by": selected_by,
                }
        twins = delivered.same_as(path)
        same_as = [twin for twin, _ in twins]
        if _covers(twins, routes):
            _skip(entry, f"same content as {', '.join(same_as)}", same_as=same_as)
            whole = [twin for twin, cut in twins if cut is None]
            if whole:
                carriers[feed.feed_id] = whole[0]
            if selection is not None:
                selections.append(selection)
            return "skipped"
        origin = _report_provenance(version, dataset_id)
        try:
            path, report, made, made_key, window = _process_feed(
                path,
                provenance=origin,
                outputs=(cache, version),
                geometry=geometry,
                repair=repair,
                crop=crop,
                modes=modes,
                day=window_day,
                study=study,
                hosted=hosted,
                budgets=budgets,
                routes=routes,
                progress=progress,
            )
        except _SkipFeed as skip:
            if not last:
                return "rejected"
            if skip.missed_day and datable(feed):
                undated.append(feed.feed_id)
            _skip(entry, skip.reason, feed_window=skip.window)
            if selection is not None:
                selections.append(selection)
            return "skipped"
        except Exception as error:  # noqa: B902 — isolate per-feed failures
            if not last:
                return "rejected"
            _skip(entry, f"processing failed: {error}")
            if selection is not None:
                selections.append(selection)
            return "skipped"
        if inside:
            _skip(entry, f"contained in {inside[0]}", contained_in=list(inside))
            protected.update(carriers[c] for c in inside)
            if selection is not None:
                selections.append(selection)
            return "skipped"
        # ``present`` is the routes.txt the crop scanned, the download
        # before any repair: the audit is the selector's own action over
        # the feed's routes -- the selected routes it carried (``kept``)
        # and the rest it held that the selector removed (``dropped``).
        # A later repair may still change the delivered feed, and any
        # spatial crop is a separate transform reported in ``reports``,
        # not here. Both are None (undetermined) when routes.txt could
        # not be read. Only a trusted complete selector was cropped
        # (``routes`` is set).
        present = made["present_routes"]
        if selection is not None and routes is not None:
            selection["kept"] = None if present is None else sorted(present & routes)
            selection["dropped"] = None if present is None else sorted(present - routes)
        entry.update(
            decision="delivered", feed_window=window, path=path, same_as=same_as
        )
        if routes is not None:
            if present is None:
                applied = f"cut to {len(routes)} selected routes"
            else:
                applied = f"cut to {len(present & routes)} of {len(present)} routes"
            cropped.add(feed.feed_id)
        else:
            carriers[feed.feed_id] = feed.feed_id
        notes = [applied, *notes, _timezone_note(path, budget), _dropped_note(report)]
        for text in dict.fromkeys(filter(None, notes)):
            _note(entry, text)
        reports.append(report)
        repairs.append(made["fixes"])
        feeds.append(path)
        delivered.add(feed.feed_id, version.path, routes)
        delivered_ids.append(feed.feed_id)
        served = (key, dataset_id)
        item = _Processed(entry, version, served, made, made_key, origin, hosted)
        processed.append(item)
        if selection is not None:
            selections.append(selection)
        service = _service(path, day if study else None, budget)
        if service is not None:
            services[feed.feed_id] = service
        return "delivered"

    if on_unknown == "exclude":
        included = candidates("include")
        kept_ids = {f.feed_id for f in kept}
        for feed in included:
            entry = entry_for(feed)
            if feed.feed_id not in kept_ids:
                _skip(entry, "only unknown-tier edges")
    for feed in offered:
        entry_for(feed)
    view_note = None
    if tiers is None and wanted is None and not offered:
        # An empty default view may hide feeds a tier query would fetch.
        hidden = place_obj.feeds(
            exclude=exclude, on_unknown=on_unknown, categories=None
        )
        if hidden:
            view_note = _hidden_note(place_obj, hidden)
            warnings.warn(view_note, UserWarning, stacklevel=3)

    cache = _feed_cache(cache_dir, directory)
    snapshot = provenance["snapshot"]
    area = hashlib.sha256(geometry.wkb).hexdigest()
    where = (
        "for the area"
        if isinstance(place_obj, Area)
        else f"for {place_obj.name or place_obj.id}"
    )
    early = [entry for entry in record if entry["decision"] == "skipped"]
    progress.start(len(record), where, early)
    # Archives read by URL fragment live only for the call.
    with (
        MobilityDatabase(refresh_token, cache_dir=cache_dir) as db,
        TransitlandAtlas(cache_dir=cache_dir) as atlas,
        tempfile.TemporaryDirectory(dir=cache.root) as scratch,
    ):
        archives = _Archives(scratch)
        tokenless = when is not None and not db._refresh_token

        def unchanged(feed):
            # One probe per feed, shared by the date and containment rules.
            if feed.feed_id not in probes:
                access = decided.get(feed.feed_id, (None, None))[0]
                probes[feed.feed_id] = _unchanged_since_indexed(
                    feed, atlas._http, access, atlas._transport
                )
            return probes[feed.feed_id]

        def expired_unchanged(feed, entry):
            # Index metadata describes the archive the index crawled, so it
            # decides a feed only before a download from the indexed URLs,
            # and only once a probe proves that archive unchanged.
            missed = _misses(feed.service_start, feed.service_end, day, study)
            if expired == "skip" and missed and unchanged(feed):
                _skip(entry, f"{missed}; unchanged since indexed")
                if datable(feed):
                    undated.append(feed.feed_id)
                return True
            return False

        def request(feed, access):
            # Everything but the version that decides whether one serves.
            selector = None
            if tiers is not None or exclude is not None or on_unknown != "include":
                sel = feed.selector
                stored = sorted(
                    f"{edge.fingerprint_kind}:{edge.classification_fingerprint}"
                    for edge in feed.edges.values()
                )
                selector = [sel.state, sorted(sel.route_ids), stored]
            return _request_key(
                day=day.isoformat() if study else None,
                expired=expired,
                area=area,
                selector=selector,
                crop=crop,
                repair=repair,
                modes=None if modes is None else sorted(modes),
                budgets=budgets,
                credentials=access is not None,
                on_untrusted_selector=on_untrusted_selector,
                exclude=exclude,
                on_unknown=on_unknown,
                contained=contained,
                snapshot=snapshot,
            )

        def attempt(feed, entry, version, key, notes, keyless, label, last, proven):
            # One cached version through ``take``, the selection columns set
            # from its first acquisition once it decides the feed. Its proofs
            # alone, without a probe, show whether it is the archive the index
            # found contained in the ``proven`` containers.
            first = version.first_source
            notes = list(notes)
            inside = proven if snapshot in version.index_proofs else ()
            if proven and not inside:
                notes.append(_unproven(proven))
            if first["fetched_from"] == "mdb_latest" and (
                first["download_errors"] or keyless
            ):
                notes.append("from the Mobility Database hosted copy")
            notes.append(keyless)
            context = _context(version, key)
            hosted = _hosted(db, cache, version, context)
            current[feed.feed_id] = snapshot in version.index_proofs
            outcome = take(
                feed, entry, version, notes, context, hosted, key, last, inside
            )
            if outcome in ("delivered", "skipped"):
                entry.update(fetched_from=first["fetched_from"], cache=label)
            if label == "fallback" and outcome == "delivered":
                _warn_fallback(feed.feed_id, entry["download_errors"], version)
            return outcome

        def reuse(feed, entry, candidates, key, notes, keyless, label, proven=()):
            # The cached candidates in order until one decides the feed;
            # returns (decided, the newest passed over as stale).
            deferred = None
            for version in candidates:
                if not cache.intact(version):
                    continue
                outcome = attempt(
                    feed, entry, version, key, notes, keyless, label, False, proven
                )
                if outcome == "stale":
                    deferred = deferred or version
                elif outcome != "rejected":
                    return True, deferred
            return False, deferred

        def fall_back(feed, entry, candidates, key, notes, keyless, proven, deferred):
            # Without a download: a refresh falls back to what the cache
            # serves, then the selector policy decides on the newest stale
            # candidate. Returns whether the feed was decided.
            label = "reused" if use_cache else "fallback"
            if not use_cache:
                done, deferred = reuse(
                    feed, entry, candidates, key, notes, keyless, label, proven
                )
                if done:
                    return True
            if deferred is None:
                return False
            attempt(feed, entry, deferred, key, notes, keyless, label, True, proven)
            return True

        for feed, entry, staging in _held(cache, kept, entry_for, progress):
            access, refusal = decided.get(feed.feed_id, (None, None))
            keyless = None
            if refusal is not None:
                instructions = feed.access_instructions()
                keyless = f"protected feed: {refusal}; {instructions}"
            key = request(feed, access)
            # Without usable credentials only a copy fetched without them serves.
            candidates = _candidates(cache, feed.feed_id, key, refusal is not None)
            notes, proven = [], []
            if contained == "drop":
                proven, notes = _containers(feed, entries, carriers, cropped, current)
            deferred = None
            if use_cache:
                done, deferred = reuse(
                    feed, entry, candidates, key, notes, keyless, "reused", proven
                )
                if done:
                    continue
            if keyless is not None and _hosted_url(feed) is None:
                # Only a cached copy fetched without credentials could serve.
                if fall_back(
                    feed, entry, candidates, key, notes, keyless, proven, deferred
                ):
                    continue
                _skip(entry, keyless)
                continue
            dataset = None
            errors = []
            # A protected feed with credentials skips the dataset versions:
            # its fallback is the hosted copy alone.
            if db._refresh_token and access is None:
                mdb_id = _mdb_id(feed)
                if mdb_id:
                    try:
                        mdb_feed = Feed.from_api({"id": mdb_id})
                        if when is not None:
                            dataset = db.dataset_for(mdb_feed, when)
                            if dataset is None:
                                _skip(entry, "no dataset covers the requested day")
                                continue
                        else:
                            versions = db.datasets(mdb_feed)
                            dataset = versions[0] if versions else None
                    except Exception as error:  # noqa: B902 — fall back to the urls
                        errors.append(f"dataset selection: {error}")
            if dataset is None and expired_unchanged(feed, entry):
                continue
            if proven:
                if dataset is None and unchanged(feed):
                    _skip(entry, f"contained in {proven[0]}", contained_in=proven)
                    protected.update(carriers[c] for c in proven)
                    continue
                notes.append(_unproven(proven))
            # The hosted validation report only describes the dataset's own
            # bytes, so it is attached only when the dataset supplied them.
            path = fetched_from = url = None
            if dataset is not None:
                try:
                    url = dataset.hosted_url
                    path = db._fetch_dataset(dataset, directory=staging)
                    if not zipfile.is_zipfile(path):
                        path = None
                        raise DownloadError("not a zip archive")
                    fetched_from = "mdb_dataset"
                except Exception as error:  # noqa: B902 — try the fallback next
                    errors.append(f"mdb dataset: {error}")
                    progress.retry(str(error), url)
                if path is None and expired_unchanged(feed, entry):
                    continue
            if path is None:
                if contained == "drop" and feed.feed_id in container_ids:
                    # Probed before its download, a container can prove itself.
                    unchanged(feed)
                try:
                    path, fetched_from, failures, url = _download_indexed(
                        feed, db, atlas, staging, archives, budget, access, progress
                    )
                    errors.extend(failures)
                except Exception as error:  # noqa: B902
                    errors.append(str(error))
            # Set before the later checks, so a feed skipped after its
            # download still records where it came from.
            download_errors = "; ".join(e for e in errors if e) or None
            if access is not None and download_errors is not None:
                download_errors = access.redact(download_errors)
            entry.update(fetched_from=fetched_from, download_errors=download_errors)
            if path is None:
                if fall_back(
                    feed, entry, candidates, key, notes, keyless, proven, deferred
                ):
                    continue
                _skip(entry, f"download failed: {download_errors}", note=keyless)
                continue
            acquired_as = dataset.id if fetched_from == "mdb_dataset" else None
            try:
                version = _add_version(
                    cache,
                    feed.feed_id,
                    path,
                    url,
                    fetched_from,
                    download_errors,
                    with_credentials=access is not None and fetched_from == "producer",
                    snapshot=snapshot,
                    dataset=acquired_as and {acquired_as: _dataset_entry(dataset)},
                    replace=not use_cache,
                )
            except Exception as error:  # noqa: B902 — isolate per-feed failures
                _skip(entry, f"processing failed: {error}")
                continue
            path = version.path
            # Read as a reuse would, so identical bytes report alike.
            dataset_id = _context(version, key)
            probe = probes.get(feed.feed_id)
            acquired = version.sidecar["cache"]["sources"][-1]
            if snapshot and probe and acquired["source_url"] == probe:
                # The proof covers only a download from the probed URL.
                _prove(cache, version, snapshot, probe)
            current[feed.feed_id] = snapshot in version.index_proofs
            if fetched_from == "mdb_latest" and (download_errors or keyless):
                notes.append("from the Mobility Database hosted copy")
            notes.append(keyless)
            hosted = _hosted(db, cache, version, dataset_id)
            entry["cache"] = "downloaded" if use_cache else "refreshed"
            take(feed, entry, version, notes, dataset_id, hosted, key)

    _warn_undated(undated, day, stacklevel=4)
    removed = _settle_versions(record, services, protected, day if study else None)
    # A feed is delivered once the versions are settled and it stays.
    for n, (feed_id, item) in enumerate(zip(delivered_ids, processed)):
        if feed_id in removed:
            continue
        try:
            with cache.lock(feed_id):
                feeds[n] = _deliver(feeds[n], directory, item.origin)
                cache.touch(item.version, served=item.served)
        except Exception as error:  # noqa: B902 — isolate per-feed failures
            _skip(entries[feed_id], f"processing failed: {error}", path=None)
            removed.add(feed_id)
            continue
        entries[feed_id]["path"] = feeds[n]
    repeats = dict(budgets=budgets, modes=modes, day=day if study else None)
    repeats["duplicate_trips"] = duplicate_trips
    lost = _drop_repeats(
        cache, record, processed, feeds, reports, directory, progress, **repeats
    )
    for feed_id, (access, _) in decided.items():
        entry = entries[feed_id]
        for key in ("reason", "note", "download_errors"):
            if access is not None and entry[key] is not None:
                entry[key] = access.redact(entry[key])
    progress.left_out(record)
    rows = [
        n for n, item in enumerate(processed) if item.entry["decision"] == "delivered"
    ]
    delivered_ids, feeds, reports, repairs = (
        [column[n] for n in rows] for column in (delivered_ids, feeds, reports, repairs)
    )
    # A feed left out as contained is carried by its containers' carriers,
    # and those that lost repeated trips by the feeds holding them too.
    position = {entry["feed_id"]: n for n, entry in enumerate(record)}
    pairs = {}
    for entry in record:
        held = {carriers[c] for c in entry["contained_in"]}
        held |= {f for c in held for f in lost.get(c, ())}
        pairs[entry["feed_id"]] = sorted(held & set(delivered_ids), key=position.get)

    osm_pbf = osm_area = osm_note = None
    if osm:
        if isinstance(place, Area):
            # An area's extract covers the area itself, as on the catalogue path.
            parts = osm_area = geometry
            coords = {path: _stop_coords(path) for path in feeds}
            grown = {}
        else:
            parts, coords = _osm_parts(geometry, feeds)
            osm_area = _buffered(parts, _OSM_BUFFER_M)
            grown = {
                "buffer_m": _OSM_BUFFER_M,
                "must_cover": _osm_stops(osm_area, coords),
            }
        progress.say("Fetching the OSM extract")
        with progress.downloads("Downloading the OpenStreetMap extract"):
            osm_pbf, osm_note = _osm_extract(
                parts,
                cache_dir=cache_dir,
                directory=directory,
                progress=progress.bar or False,
                **grown,
            )
        if osm_pbf is None:
            osm_area = None
        else:
            counts = _count_outside(record, osm_area, coords)
            _warn_outside(*counts, stacklevel=4)
            osm_note = _osm_note(geometry, parts, *counts)

    progress.done(record)
    return FetchResult(
        osm_pbf=osm_pbf,
        feeds=feeds,
        reports=reports,
        repairs=repairs,
        skipped=_skipped(record),
        selections=selections,
        provenance=provenance,
        snapshot=provenance["snapshot"],
        contained={feed_id: ids for feed_id, ids in pairs.items() if ids},
        selection=record,
        osm_area=osm_area,
        view_note=view_note,
        osm_note=osm_note,
        places=(
            [part.place for part in place.parts]
            if isinstance(place, Area)
            else [place_obj]
        ),
    )
