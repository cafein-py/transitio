"""Infer a feed's missing ``shapes.txt`` from an OSM extract.

A GTFS feed without shapes is a deficient feed: every consumer that
needs a route's real alignment — travel distance, emissions, a drawn
map — falls back to straight lines between stops. This module fills
that gap from OpenStreetMap, writing a feed whose shapes are real
alignments rather than reconstructions of what the operator meant.

Two strategies, best first, per distinct stop pattern:

1. **A matched route relation.** OSM's ``type=route`` relations carry
   the operator's own alignment; matching one to a GTFS pattern and
   stitching its member ways gives the truest geometry available.
2. **Map matching.** Where no relation matches, consecutive stops are
   connected by shortest paths over a mode graph — tram/subway/rail
   tracks, or a bus-drivable street network resolved through the PSV
   access hierarchy.

Both are validated before they are written: every stop must lie on the
alignment within tolerance, the stop positions must run monotonically
along it, and the total length must be plausible against the stops'
crow-fly distance. What survives is written as ``shapes.txt`` with
``shape_dist_traveled`` on the stop times; what does not is left
alone, so the feed never gains a shape that its own stops contradict.

How much uncertainty is acceptable is the caller's, through
``strictness`` — see `transitio.shapes._levels`.
"""

import collections
import dataclasses
import datetime
import io
import json
import os
import pathlib
import shutil
import stat
import tempfile
import zipfile

import geopandas as gpd
import numpy as np
import pandas as pd
import pyproj
import shapely

from transitio.exceptions import ShapeInferenceError
from transitio.shapes import _graph, _levels, _match, _relations, _stitch
from transitio.shapes._geometry import locate_on_shape, measures

#: Total-length plausibility band against the pattern's crow-fly
#: length: below the floor the alignment is too short to connect the
#: stops, above the ceiling it wanders.
LENGTH_RATIO = (0.8, 5.0)

EARTH_RADIUS = 6_371_000.0


def infer_shapes(path, output, pbf, *, strictness="strict", modes=None, check=True):
    """Write ``path``'s feed to ``output`` with inferred shapes.

    Parameters
    ----------
    path : str or pathlib.Path
        Source GTFS ``.zip``.
    output : str or pathlib.Path
        Destination for the written feed (overwritten). Every table of
        the source feed is carried over; ``shapes.txt`` is written and
        ``trips.txt``/``stop_times.txt`` gain the references to it.
    pbf : str or pathlib.Path
        An OSM ``.osm.pbf`` extract covering the feed's area, as
        fetched by :func:`transitio.fetch_pbf`.
    strictness : str or Level, default ``"strict"``
        ``"strict"``, ``"relaxed"``, ``"permissive"``, or a `Level`.
        Higher tolerance infers more shapes and more wrong ones; the
        report records what each shape rests on.
    modes : iterable of str, optional
        Mode families to infer for (``bus``, ``trolleybus``, ``tram``,
        ``subway``, ``train``, ``ferry``). Defaults to every mode the
        feed carries. Unknown names raise.
    check : bool, default True
        Raise :class:`transitio.exceptions.ShapeInferenceError` when the
        written feed carries error-severity notices the input did not.
        The file is still written, like ``InvalidFeedError``.

    Returns
    -------
    dict
        ``{"level": ..., "written": n, "patterns": n, "by_mode": {...},
        "shapes": [...], "skipped": [...]}``. Each ``shapes`` entry
        names its ``shape_id``, the ``method`` that produced it
        (``osm_relation`` or ``map_matched``), the OSM relation id
        where one was matched, the match ``score``, and the ``trips``
        it serves; each ``skipped`` entry names the pattern and the
        stage that refused it. Feeds already carrying a shape for a
        pattern keep it, recorded as ``method: "existing"``.

        A ``<output>.provenance.json`` sidecar records the same
        report alongside the feed, so an inferred alignment stays
        distinguishable from an operator-published one after the fact —
        the strictness a shape rests on is not recoverable from GTFS
        itself.

    Raises
    ------
    ValueError
        If ``strictness`` or a mode name is unknown, or ``output`` is
        one of the inputs.
    transitio.exceptions.ShapeInferenceError
        If ``check`` and the written feed validates worse than the
        input did.
    OSError
        If the feed or extract cannot be read, or the output written.
    """
    level = _levels.resolve(strictness)
    path = pathlib.Path(path)
    output = pathlib.Path(output)
    pbf = pathlib.Path(pbf)
    sidecar = _sidecar_for(output)
    for other, label in ((path, "the input feed"), (pbf, "the extract")):
        for destination, what in ((output, "output"), (sidecar, "provenance sidecar")):
            if _same_entry(destination, other):
                raise ValueError(f"{what} must differ from {label}")
    wanted = _resolve_modes(modes)
    inference = _Inference(os.fspath(pbf), level)

    # The extract is pinned by digest rather than copied — country
    # extracts are far too large to snapshot — and re-checked before
    # anything is published, so a run can never mix OSM versions.
    extract_digest = _digest(pbf)
    workdir = pathlib.Path(tempfile.mkdtemp(prefix="transitio-shapes-"))
    # The published file is staged in the OUTPUT directory: os.replace
    # is only atomic within one filesystem, and the system temp dir is
    # commonly on another.
    staged_dir = pathlib.Path(tempfile.mkdtemp(dir=output.parent, prefix=".transitio-"))
    try:
        snapshot = workdir / "input.zip"
        shutil.copyfile(path, snapshot)
        staged = staged_dir / "output.zip"
        try:
            report = _infer_into(
                snapshot,
                staged,
                pbf,
                level,
                wanted,
                inference,
                check,
                path,
                extract_digest,
            )
        except ShapeInferenceError as refused:
            # The feed is written for inspection, so its sidecar must
            # be too — but only if it still describes one OSM version.
            _require_stable_extract(pbf, extract_digest)
            _publish(staged, output, refused.report)
            raise
        _require_stable_extract(pbf, extract_digest)
        # Publish only what certification has seen.
        _publish(staged, output, report)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        shutil.rmtree(staged_dir, ignore_errors=True)
    return report


def _infer_into(
    snapshot, staged, pbf, level, wanted, inference, check, source_path, extract_digest
):
    """Infer into ``staged`` from the pinned ``snapshot``."""
    tables = _read_tables(snapshot)
    patterns, unusable = _patterns(tables, wanted)
    inherited, inherited_shapes = _inherited_provenance(source_path, snapshot)
    reserved = _reserved_shape_ids(tables)
    report = {
        "level": level.name,
        "thresholds": dataclasses.asdict(level),
        "written": 0,
        "patterns": len(patterns),
        "by_mode": collections.Counter(),
        "shapes": [],
        # Trips the intake could not turn into a pattern at all are
        # refusals too: the report is the audit trail, so nothing may
        # vanish from it silently.
        "skipped": list(unusable),
    }
    shape_rows = []
    assignments = {}
    for index, pattern in enumerate(patterns):
        # Whatever this pattern's trips already carry stays, reported
        # as its own entry — an inferred shape never claims the trips
        # that kept a published one.
        for existing_id, count in sorted(pattern.existing_shapes.items()):
            prior = inherited_shapes.get(existing_id)
            report["shapes"].append(
                {
                    "shape_id": existing_id,
                    # A shape this feed inherited from an earlier
                    # inference is still inferred — calling it
                    # "existing" would launder it into a published one.
                    "method": prior["method"] if prior else "existing",
                    "relation": prior["relation"] if prior else None,
                    "score": prior["score"] if prior else None,
                    # The strictness a shape rests on travels WITH the
                    # shape, so any number of runs can be unwound.
                    "level": prior.get("level") if prior else None,
                    "thresholds": prior.get("thresholds") if prior else None,
                    "osm_pbf_sha256": prior.get("osm_pbf_sha256") if prior else None,
                    "inferred_by": prior and "a previous run",
                    "trips": count,
                }
            )
        if not pattern.unshaped_trips:
            continue
        resolved = inference.resolve(pattern)
        if resolved is None:
            report["skipped"].append(
                {
                    "route_id": pattern.route_id,
                    "first_stop": pattern.stop_ids[0],
                    "last_stop": pattern.stop_ids[-1],
                    "stops": len(pattern.stop_ids),
                    "trips": sorted(pattern.unshaped_trips),
                    "stage": inference.last_stage,
                }
            )
            continue
        shape_id = _allocate_shape_id(index, reserved)
        lons, lats, along = resolved.lons, resolved.lats, resolved.along
        cumulative = measures(resolved.line)
        for sequence, (lon, lat, metres) in enumerate(zip(lons, lats, cumulative)):
            shape_rows.append((shape_id, lat, lon, sequence, round(metres, 3)))
        assignments[pattern.key] = (shape_id, along)
        report["written"] += 1
        report["by_mode"][pattern.mode] += 1
        report["shapes"].append(
            {
                "shape_id": shape_id,
                "method": resolved.method,
                "relation": resolved.relation,
                "score": resolved.score,
                "level": level.name,
                "thresholds": dataclasses.asdict(level),
                "osm_pbf_sha256": extract_digest,
                "trips": len(pattern.unshaped_trips),
            }
        )
    _write_feed(snapshot, staged, tables, shape_rows, assignments, patterns)
    report["by_mode"] = dict(report["by_mode"])
    report["osm_pbf"] = os.fspath(pbf)
    report["osm_pbf_sha256"] = extract_digest
    report["inherited"] = inherited
    report["feed_sha256"] = _digest(staged)
    _certify(snapshot, staged, report, check)
    return report


def _inherited_provenance(path, snapshot):
    """A previous run's sidecar, so twice-inferred shapes stay
    distinguishable from published ones.

    Returns ``(summary, by_shape)``: the prior run's own identity, and
    the evidence behind each shape id it inferred. Shapes carried into
    this run keep that evidence instead of being relabelled
    ``existing``, which would erase the fact that they were inferred
    at all. Only the prior run is kept — its own ``inherited`` block is
    dropped, so lineage does not grow without bound.
    """
    sidecar = _sidecar_for(pathlib.Path(path))
    record = _read_sidecar(sidecar)
    if record is None:
        return None, {}
    # A sidecar is trusted only when it names these very bytes: an
    # adjacent file with the right name may describe a different feed
    # entirely, and false provenance is worse than none.
    if record.get("feed_sha256") != _digest(snapshot):
        return None, {}
    summary = {
        "level": record.get("level"),
        "thresholds": record.get("thresholds"),
        "osm_pbf": record.get("osm_pbf"),
        "osm_pbf_sha256": record.get("osm_pbf_sha256"),
        "written_at": record.get("written_at"),
        "written": record.get("written"),
    }
    by_shape = {
        entry["shape_id"]: entry
        for entry in (record.get("shapes") or [])
        if isinstance(entry, dict)
        and entry.get("shape_id")
        and entry.get("method") not in (None, "existing")
    }
    return summary, by_shape


def _reserved_shape_ids(tables):
    """Every shape id the feed already uses, from either table."""
    reserved = set()
    shapes = tables.get("shapes.txt")
    if shapes is not None and "shape_id" in shapes:
        reserved.update(shapes["shape_id"].dropna())
    trips = tables.get("trips.txt")
    if trips is not None and "shape_id" in trips:
        reserved.update(trips["shape_id"].dropna())
    return reserved


def _allocate_shape_id(index, reserved):
    """A deterministic id that collides with nothing the feed uses."""
    candidate = f"transitio-{index}"
    suffix = 0
    while candidate in reserved:
        suffix += 1
        candidate = f"transitio-{index}-{suffix}"
    reserved.add(candidate)
    return candidate


def _require_stable_extract(pbf, expected):
    """Refuse if the OSM extract changed while the run was reading it."""
    if _digest(pbf) != expected:
        raise OSError("the OSM extract changed while inferring; nothing was written")


#: A provenance sidecar is discovered by name, so it is untrusted
#: input: bounded, never followed through a link, never anything but a
#: regular file, and never assumed to have the right shape.
MAX_SIDECAR_BYTES = 32 << 20


def _read_sidecar(sidecar):
    """A neighbouring provenance record, or ``None`` when it cannot be
    trusted to be one."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        handle = os.open(sidecar, flags)
    except OSError:
        return None
    try:
        status = os.fstat(handle)
        if not stat.S_ISREG(status.st_mode) or status.st_size > MAX_SIDECAR_BYTES:
            os.close(handle)
            return None
        with os.fdopen(handle, "rb") as reader:
            record = json.loads(reader.read(MAX_SIDECAR_BYTES).decode())
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    shapes = record.get("shapes")
    if shapes is not None and not isinstance(shapes, list):
        return None
    return record


def _sidecar_for(output):
    """The provenance path for a feed.

    Derived by replacing a ``.zip`` suffix and by appending otherwise,
    so an output already named ``*.provenance.json`` cannot resolve to
    its own sidecar and be overwritten by it.
    """
    if output.suffix.lower() == ".zip":
        candidate = output.with_suffix(".provenance.json")
        if candidate != output:
            return candidate
    return output.with_name(output.name + ".provenance.json")


def _publish(staged, output, report):
    """Put the feed and its sidecar in place.

    A stale sidecar is worse than a missing one — it describes a feed
    that is no longer there — so any previous provenance is removed
    first, the feed is replaced, and the new sidecar written last. Every
    interruption therefore leaves either the old pair or a feed with no
    provenance, which is detectable; it never leaves a feed beside a
    sidecar describing something else. Concurrent runs against one
    destination are the caller's to serialise.
    """
    sidecar = _sidecar_for(output)
    sidecar.unlink(missing_ok=True)
    os.replace(staged, output)
    _write_provenance(output, report)


def _digest(path):
    """The SHA-256 of a file, read in bounded chunks."""
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _same_entry(a, b):
    """Whether two paths name the same file on disk."""
    try:
        return a.resolve() == b.resolve() or (
            a.exists() and b.exists() and a.samefile(b)
        )
    except OSError:
        return False


def _write_provenance(output, report):
    """The report beside the feed: GTFS cannot say a shape was
    inferred, so the sidecar does.

    Staged in the output's own directory and published by
    ``os.replace``, so a partial sidecar never sits beside a complete
    feed and an existing symlink is replaced rather than followed.
    """
    record = dict(report)
    record["written_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    sidecar = _sidecar_for(output)
    handle, staged = tempfile.mkstemp(dir=sidecar.parent, prefix=".provenance-")
    try:
        with os.fdopen(handle, "w") as writer:
            writer.write(json.dumps(record, indent=2))
        os.replace(staged, sidecar)
    except BaseException:
        pathlib.Path(staged).unlink(missing_ok=True)
        raise


def _certify(path, output, report, check):
    """Refuse to leave a feed that validates worse than it arrived.

    Inference only adds shapes, so any new error-severity notice is a
    defect in what was written. Notices are compared by full identity
    with multiplicity — a second occurrence of a code the input already
    carried is still a feed made worse — and a sampled or truncated
    validation refuses outright, because absence of evidence there is
    not evidence of absence.
    """
    from transitio.validate import validate_feed

    # Generous notice budgets: certification compares complete notice
    # sets, and the default per-file cap is reached by ordinary large
    # feeds. Refusing those would make certification useless; the
    # reliability check below then guards what remains.
    budgets = {"max_notices_per_file": CERTIFY_NOTICE_BUDGET}
    before = validate_feed(os.fspath(path), **budgets)
    after = validate_feed(os.fspath(output), **budgets)
    for validation, label in ((before, "input"), (after, "output")):
        if _unreliable(validation):
            error = ShapeInferenceError(
                f"{label} validation was sampled or truncated, so the "
                "inferred feed cannot be certified; raise the budgets"
            )
            error.report = report
            raise error
    counted_before = _errors(before)
    counted_after = _errors(after)
    introduced = sorted(
        (counted_after - counted_before).elements(), key=lambda item: item[0]
    )
    report["introduced_notices"] = [
        {"code": code, "context": context} for code, context in introduced
    ]
    if introduced and check:
        error = ShapeInferenceError(
            "inferred shapes introduced error-severity notices; the "
            "output was written for inspection: "
            + ", ".join(sorted({code for code, _ in introduced}))
        )
        error.report = report
        raise error


def _unreliable(validation):
    """Whether a validation saw less than the whole feed."""
    return bool(validation.get("incomplete")) or any(
        notice["code"] in ("notice_limit_reached", "too_many_rows")
        for notice in validation.get("notices", [])
    )


def _errors(validation):
    """Error-severity notices as a multiset of ``(code, context)``.

    Identity, not just the code: fixing one occurrence while
    introducing another under the same code must still count.
    """
    return collections.Counter(
        (notice["code"], json.dumps(notice.get("context"), sort_keys=True))
        for notice in validation.get("notices", [])
        if notice["severity"] == "ERROR"
    )


def _cut(projected, lons, lats, along):
    """The alignment between the first and last stop, as `_Resolved`.

    Vertices outside the served span are dropped and the ends
    interpolated, so ``shape_dist_traveled`` starts at zero on the
    first stop and the written geometry covers exactly the run.
    """
    metres = np.asarray(measures(projected), dtype=float)
    start, stop = float(along[0]), float(along[-1])
    xs = np.asarray(lons, dtype=float)
    ys = np.asarray(lats, dtype=float)
    inside = (metres > start) & (metres < stop)
    cut_lons = [float(np.interp(start, metres, xs))]
    cut_lats = [float(np.interp(start, metres, ys))]
    cut_lons.extend(xs[inside].tolist())
    cut_lats.extend(ys[inside].tolist())
    cut_lons.append(float(np.interp(stop, metres, xs)))
    cut_lats.append(float(np.interp(stop, metres, ys)))
    if len(cut_lons) < 2:
        return None
    coordinates = shapely.get_coordinates(projected)
    px = np.interp([start, stop], metres, coordinates[:, 0])
    py = np.interp([start, stop], metres, coordinates[:, 1])
    inside_points = coordinates[inside]
    cut_projected = shapely.LineString(
        np.vstack([[px[0], py[0]], inside_points, [px[1], py[1]]])
    )
    return _Resolved(
        cut_projected,
        cut_lons,
        cut_lats,
        (np.asarray(along, dtype=float) - start).tolist(),
        None,
        None,
        None,
    )


class _Resolved:
    __slots__ = ("line", "lons", "lats", "along", "method", "relation", "score")

    def __init__(self, line, lons, lats, along, method, relation, score):
        self.line = line
        self.lons = lons
        self.lats = lats
        self.along = along
        self.method = method
        self.relation = relation
        self.score = score


class _Pattern:
    """One distinct (route, stop sequence) of the feed."""

    __slots__ = (
        "key",
        "route_id",
        "mode",
        "stop_ids",
        "latlon",
        "trips",
        "unshaped_trips",
        "existing_shapes",
        "shape_id",
        "short_name",
        "long_name",
        "agency",
    )

    def __init__(self, **fields):
        for name, value in fields.items():
            setattr(self, name, value)


class _Inference:
    """Per-run OSM state: relations, stitched lines, mode graphs."""

    def __init__(self, pbf, level):
        self._pbf = pbf
        self._level = level
        self._relations = None
        self._by_mode = {}
        self._rail_ways = None
        self._streets = _UNSET
        self._graphs = {}
        self._canonical = {}
        self._lines = {}
        self._directed = {}
        self._transformer = None
        self._crs = None
        self.last_stage = None

    def resolve(self, pattern):
        """The best alignment for one pattern, or ``None``."""
        self.last_stage = None
        if not self._set_projection(pattern.latlon):
            self.last_stage = "no-projection"
            return None
        crow = _crow_fly(pattern.latlon)
        matched = self._from_relation(pattern, crow)
        if matched is not None:
            return matched
        return self._from_graph(pattern, crow)

    def _from_relation(self, pattern, crow):
        query = _match.Pattern(
            stop_ids=tuple(pattern.stop_ids),
            stop_xy=self._project(pattern.latlon[:, 1], pattern.latlon[:, 0]),
            short_name=pattern.short_name,
            long_name=pattern.long_name,
            agency=pattern.agency,
        )
        entries = [
            (relation, self._canonical_xy(relation))
            for relation in self._mode_relations(pattern.mode)
        ]
        selection, diagnostics = _match.select(query, entries, self._level)
        # Diagnostics are read for the refusal stage and dropped: on a
        # metropolitan bus feed, retaining every candidate record for
        # every pattern is patterns × relations dictionaries.
        if selection is None:
            self.last_stage = _dominant_stage(diagnostics)
            return None
        score = next(
            (r["score"] for r in diagnostics if r.get("outcome") == "selected"), None
        )
        if self._line(selection.relation, False, pattern.mode) is None:
            self.last_stage = "stitch"
            return None
        directed = self._directed[selection.relation.id]
        if directed:
            orientations = () if selection.reversed else (False,)
        else:
            orientations = (selection.reversed, not selection.reversed)
        for reversed_ in orientations:
            validated = self._validated(selection.relation, reversed_, pattern, crow)
            if validated is not None:
                validated.method = "osm_relation"
                validated.relation = selection.relation.id
                validated.score = score
                return validated
        self.last_stage = "validation"
        return None

    def _validate_line(self, projected, lons, lats, pattern, crow):
        """The shared gates over one candidate alignment: denser than
        the stop sequence, every stop on it within tolerance, stop
        positions monotone, total length plausible. ``None`` when any
        of them refuses. The result is cut to the served span."""
        if crow <= 0:
            return None
        along = locate_on_shape(projected, pattern.latlon, self._transformer)
        if along is None:
            return None
        total = along[-1] - along[0]
        if not LENGTH_RATIO[0] <= total / crow <= LENGTH_RATIO[1]:
            return None
        cut = _cut(projected, lons, lats, along)
        if cut is None:
            return None
        # Density is judged on the geometry actually written: a long
        # relation can be dense elsewhere while the served span holds
        # no more vertices than the pattern has stops.
        if shapely.get_num_coordinates(cut.line) <= len(pattern.latlon):
            return None
        return cut

    def _validated(self, relation, reversed_, pattern, crow):
        stitched = self._line(relation, reversed_, pattern.mode)
        if stitched is None:
            return None
        projected, lons, lats = stitched
        # A relation covers its whole route; this pattern may serve
        # only part of it (a short working). The shape written is the
        # span between its first and last stop, never the whole line.
        return self._validate_line(projected, lons, lats, pattern, crow)

    def _from_graph(self, pattern, crow):
        graph = self._graph(pattern.mode)
        if graph is None:
            self.last_stage = self.last_stage or "no-graph"
            return None
        stop_xy = self._project(pattern.latlon[:, 1], pattern.latlon[:, 0])
        bound = _graph.DETOUR_BOUNDS[pattern.mode] * self._level.detour
        segments = _graph.match_chain(graph, stop_xy, bound)
        if segments is None:
            self.last_stage = "map-match"
            return None
        chained = graph.chain([path for _, path in segments])
        if chained is None:
            self.last_stage = "map-match"
            return None
        line, lons, lats = chained
        # The same gates as a relation match: the concatenated path is
        # a candidate alignment like any other, and a self-intersecting
        # graph route can put stops out of order along it.
        validated = self._validate_line(line, lons, lats, pattern, crow)
        if validated is None:
            self.last_stage = "validation"
            return None
        validated.method = "map_matched"
        # The evidence behind a graph match: how far the path runs
        # against the stops' crow-fly.
        validated.score = round(sum(length for length, _ in segments) / crow, 4)
        return validated

    def _mode_relations(self, mode):
        if self._relations is None:
            self._relations = _relations.route_relations(self._pbf)
        if mode not in self._by_mode:
            values = _match.MODE_ROUTES[mode]
            self._by_mode[mode] = [
                relation for relation in self._relations if relation.route in values
            ]
        return self._by_mode[mode]

    def _canonical_xy(self, relation):
        key = (self._crs, relation.id)
        if key not in self._canonical:
            ordered = _match.boarding_positions(relation)
            if not ordered:
                self._canonical[key] = None
            else:
                xy = self._project(
                    [entry[1] for entry in ordered], [entry[2] for entry in ordered]
                )
                kinds = [entry[0] for entry in ordered]
                self._canonical[(self._crs, relation.id)] = _match.collapse_positions(
                    kinds, xy
                )
        return self._canonical[(self._crs, relation.id)]

    def _line(self, relation, reversed_=False, mode=None):
        key = (self._crs, relation.id, reversed_)
        if key in self._lines:
            return self._lines[key]
        if reversed_:
            forward = self._line(relation, False, mode)
            key = (self._crs, relation.id, reversed_)
            if forward is None or self._directed[relation.id]:
                self._lines[key] = None
            else:
                projected, lons, lats = forward
                self._lines[key] = (
                    shapely.reverse(projected),
                    lons[::-1],
                    lats[::-1],
                )
            return self._lines[key]
        ways = [
            member
            for member in relation.members
            if member.kind == "way"
            and not member.role.startswith("stop")
            and not member.role.startswith("platform")
        ]
        if any(member.role != "" for member in ways):
            self._lines[key] = None
            self._directed[relation.id] = True
            return None
        self._directed[relation.id] = any(
            _stitch.effective_direction(member.tags, mode) != 0 for member in ways
        )
        try:
            line = _stitch.stitch(ways, mode=mode)
        except _stitch.StitchRefusal:
            self._lines[key] = None
        else:
            coordinates = shapely.get_coordinates(line)
            xy = self._project(coordinates[:, 0], coordinates[:, 1])
            key = (self._crs, relation.id, reversed_)
            self._lines[key] = (
                shapely.LineString(xy),
                coordinates[:, 0].tolist(),
                coordinates[:, 1].tolist(),
            )
        return self._lines[key]

    def _graph(self, mode):
        if mode in _graph.RAIL_VALUES:
            key = ("rail", _graph.RAIL_VALUES[mode], self._crs)
        elif mode in _graph.BUS_FAMILIES:
            key = ("bus", self._crs)
        else:
            return None
        if key not in self._graphs:
            if key[0] == "rail":
                if self._rail_ways is None:
                    self._rail_ways = _relations.rail_ways(self._pbf)
                self._graphs[key] = _graph.rail_graph(
                    self._rail_ways, self._project, key[1]
                )
            else:
                network = self._street_network()
                self._graphs[key] = (
                    None
                    if network is None
                    else _graph.bus_graph(*network, self._project)
                )
        return self._graphs[key]

    def _street_network(self):
        if self._streets is _UNSET:
            import pyrosm

            from transitio.shapes import _permissions

            osm = pyrosm.OSM(self._pbf, engine="out_of_core", workers="auto")
            self._streets = osm.get_network(
                network_type="driving+service",
                custom_filter=_permissions.UNBUSABLE_FILTER,
                filter_type="exclude",
                nodes=True,
                extra_attributes=[
                    "psv",
                    "bus",
                    "vehicle",
                    "motor_vehicle",
                    "oneway:bus",
                    "oneway:psv",
                ],
            )
        return self._streets

    def _set_projection(self, latlon):
        try:
            crs = gpd.GeoSeries(
                gpd.points_from_xy(latlon[:, 1], latlon[:, 0]), crs="EPSG:4326"
            ).estimate_utm_crs()
        except RuntimeError:
            return False
        key = crs.to_epsg() or crs.to_wkt()
        if self._crs != key:
            self._crs = key
            self._transformer = pyproj.Transformer.from_crs(
                "EPSG:4326", crs, always_xy=True
            )
        return True

    def _project(self, lons, lats):
        x, y = self._transformer.transform(np.asarray(lons), np.asarray(lats))
        return np.column_stack([x, y])


_UNSET = object()


def _dominant_stage(diagnostics):
    """The stage that stopped the most candidates — the honest reason
    a pattern found no relation."""
    stages = collections.Counter(
        record.get("stage") for record in diagnostics if isinstance(record, dict)
    )
    if not stages:
        return "no-candidates"
    if any(record.get("stage") == "scored" for record in diagnostics):
        outcomes = collections.Counter(
            record.get("outcome") for record in diagnostics if record.get("outcome")
        )
        return outcomes.most_common(1)[0][0] if outcomes else "scored"
    return stages.most_common(1)[0][0]


def _crow_fly(latlon):
    """Great-circle meters along the stop chain."""
    lat = np.radians(latlon[:, 0])
    lon = np.radians(latlon[:, 1])
    half = (
        np.sin(np.diff(lat) / 2) ** 2
        + np.cos(lat[:-1]) * np.cos(lat[1:]) * np.sin(np.diff(lon) / 2) ** 2
    )
    return float((2 * EARTH_RADIUS * np.arcsin(np.sqrt(half))).sum())


def _resolve_modes(modes):
    if modes is None:
        return None
    requested = tuple(modes)
    unknown = sorted(set(requested) - set(_match.MODE_ROUTES))
    if unknown:
        raise ValueError("unknown mode name(s): " + ", ".join(unknown))
    return frozenset(requested)


#: Intake budgets, matching ``validate_feed``'s defaults: a hostile or
#: corrupt archive must not exhaust memory before certification runs.
MAX_ENTRY_BYTES = 1 << 30
MAX_TOTAL_BYTES = 2 << 30

#: Per-file notice budget for the certification validations. High
#: enough that an ordinary large feed is compared in full; a feed that
#: still saturates it is refused rather than certified on a sample.
CERTIFY_NOTICE_BUDGET = 1_000_000

#: Archive-entry count budget: every member is copied to the output.
MAX_ENTRIES = 10_000


def _check_budgets(
    path, max_entry_bytes=MAX_ENTRY_BYTES, max_total_bytes=MAX_TOTAL_BYTES
):
    """Refuse an archive that would cost more than the budgets allow.

    Every member counts, not only the parsed tables: the output copies
    them all, so a thousand sub-limit ancillary entries are as
    expensive as one oversized table. Read from the central directory,
    before anything is decompressed.
    """
    total = 0
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(set(names)) != len(names):
            # Rewriting an archive with duplicate names would silently
            # collapse them onto the last entry.
            duplicates = sorted({n for n in names if names.count(n) > 1})
            raise OSError(
                "feed has duplicate archive entries: " + ", ".join(duplicates[:5])
            )
        if len(infos) > MAX_ENTRIES:
            raise OSError(
                f"feed has {len(infos)} archive entries, over the {MAX_ENTRIES} budget"
            )
        for info in infos:
            if info.file_size > max_entry_bytes:
                raise OSError(
                    f"{info.filename} is {info.file_size} bytes uncompressed, "
                    f"over the {max_entry_bytes} budget"
                )
            total += info.file_size
            if total > max_total_bytes:
                raise OSError(f"feed exceeds the {max_total_bytes}-byte intake budget")


def _read_tables(
    path, max_entry_bytes=MAX_ENTRY_BYTES, max_total_bytes=MAX_TOTAL_BYTES
):
    """The feed's tables as DataFrames, keyed by member name."""
    _check_budgets(path, max_entry_bytes, max_total_bytes)
    tables = {}
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if not info.filename.endswith(".txt"):
                continue
            with archive.open(info) as member:
                try:
                    # keep_default_na=False: "NA", "NULL", "N/A" are
                    # legal GTFS ids and text, not missing values. Only
                    # a truly empty field is blank.
                    tables[info.filename] = pd.read_csv(
                        member, dtype=str, keep_default_na=False, na_values=[]
                    )
                except pd.errors.EmptyDataError:
                    # A zero-byte member is an empty table, not a
                    # malformed feed.
                    tables[info.filename] = pd.DataFrame()
    return tables


def _patterns(tables, wanted):
    """The feed's distinct (route, stop sequence) patterns."""
    stop_times = tables["stop_times.txt"].copy()
    trips = tables["trips.txt"]
    routes = tables["routes.txt"]
    stops = tables["stops.txt"]
    agency = tables.get("agency.txt")

    located = stops.drop_duplicates("stop_id").set_index("stop_id")
    coordinates = located[["stop_lat", "stop_lon"]].apply(
        pd.to_numeric, errors="coerce"
    )
    # A blank coordinate is legal GTFS for some location types; such a
    # stop simply cannot anchor an alignment, so its patterns are
    # skipped rather than crashing the run.
    usable_stops = set(coordinates.dropna().index)
    stop_times["stop_sequence"] = pd.to_numeric(
        stop_times["stop_sequence"], errors="coerce"
    )
    # A trip with an unorderable stop time is refused whole: shaping it
    # from only the rows that parsed would leave the dropped stop
    # unchecked against the alignment.
    unorderable = set(stop_times.loc[stop_times["stop_sequence"].isna(), "trip_id"])
    stop_times = stop_times.dropna(subset=["stop_sequence"]).sort_values(
        ["trip_id", "stop_sequence"], kind="stable"
    )
    sequences = stop_times.groupby("trip_id")["stop_id"].apply(tuple)

    route_of = trips.set_index("trip_id")["route_id"]
    shape_of = trips.set_index("trip_id")["shape_id"] if "shape_id" in trips else None
    # A trip whose shape_id resolves to nothing is shapeless in
    # practice: dangling references are common in feeds that lost
    # their shapes.txt, and treating them as shaped would leave the
    # feed exactly as broken as it arrived.
    usable_shapes = _usable_shapes(tables.get("shapes.txt"))
    route_meta = routes.set_index("route_id")
    identity = _agency_identity(agency)

    grouped = {}
    unusable = []

    def refuse(trip_id, route_id, stage):
        unusable.append(
            {"route_id": route_id, "trips": [trip_id], "stops": None, "stage": stage}
        )

    for trip_id, sequence in sequences.items():
        route_id = route_of.get(trip_id)
        if route_id is None or route_id not in route_meta.index:
            refuse(trip_id, route_id, "invalid-route-metadata")
            continue
        try:
            route_type = int(float(route_meta.loc[route_id, "route_type"]))
        except (TypeError, ValueError):
            refuse(trip_id, route_id, "invalid-route-metadata")
            continue
        mode = _match.mode_of(route_type)
        if mode is None:
            refuse(trip_id, route_id, "unsupported-mode")
            continue
        if wanted is not None and mode not in wanted:
            continue  # deliberately out of scope, not a refusal
        if trip_id in unorderable:
            refuse(trip_id, route_id, "unorderable-stop-times")
            continue
        if any(stop not in usable_stops for stop in sequence):
            refuse(trip_id, route_id, "missing-stop-coordinate")
            continue
        key = (route_id, sequence)
        entry = grouped.get(key)
        if entry is None:
            row = route_meta.loc[route_id]
            entry = _Pattern(
                key=key,
                route_id=route_id,
                mode=mode,
                stop_ids=list(sequence),
                latlon=coordinates.loc[list(sequence)].to_numpy(),
                trips=[],
                unshaped_trips=[],
                existing_shapes=collections.Counter(),
                shape_id=None,
                short_name=_text(row.get("route_short_name")),
                long_name=_text(row.get("route_long_name")),
                agency=identity.get(
                    _text(row.get("agency_id")), identity.get(None, ())
                ),
            )
            grouped[key] = entry
        entry.trips.append(trip_id)
        # Shape status is per trip: a pattern can mix shaped and
        # shapeless trips, and only the shapeless ones want inference.
        usable = None
        if shape_of is not None:
            value = shape_of.get(trip_id)
            if not pd.isna(value) and value in usable_shapes:
                usable = value
        if usable is None:
            entry.unshaped_trips.append(trip_id)
        else:
            entry.existing_shapes[usable] += 1
    return list(grouped.values()), unusable


def _usable_shapes(shapes):
    """The shape ids that actually carry a drawable line (≥ 2 points)."""
    if shapes is None or shapes.empty or "shape_id" not in shapes:
        return frozenset()
    counts = shapes.groupby("shape_id").size()
    return frozenset(counts[counts >= 2].index)


def _agency_identity(agency):
    """agency_id → the folded-comparison identity strings, plus a
    ``None`` key for feeds whose routes omit ``agency_id``."""
    identity = {}
    if agency is None or agency.empty:
        return {None: ()}
    rows = agency.to_dict("records")
    for row in rows:
        values = tuple(
            value
            for value in (row.get("agency_name"), row.get("agency_id"))
            if _text(value)
        )
        if _text(row.get("agency_id")):
            identity[row["agency_id"]] = values
    # A single-agency feed may leave agency_id blank on the agency, the
    # routes, or both — its one identity still applies to every route.
    if len(rows) == 1:
        identity[None] = tuple(
            value
            for value in (rows[0].get("agency_name"), rows[0].get("agency_id"))
            if _text(value)
        )
    else:
        identity[None] = ()
    return identity


def _text(value):
    if isinstance(value, str) and value.strip():
        return value
    return None


def _write_feed(path, output, tables, shape_rows, assignments, patterns):
    """The source feed with the inferred shapes written in.

    Existing shape rows are carried through with their own schema —
    extension columns kept, blank optional fields left blank — because
    a run that infers nothing must leave the table byte-for-byte
    valid.
    """
    trips = tables["trips.txt"].copy()
    stop_times = tables["stop_times.txt"].copy()
    shape_of_trip = {}
    distance_of_trip = {}
    for pattern in patterns:
        assigned = assignments.get(pattern.key)
        if assigned is None:
            continue
        shape_id, along = assigned
        # Only the trips that lacked a usable shape are assigned: a
        # pattern can mix shaped and shapeless trips, and an
        # operator-published shape is never overwritten.
        for trip_id in pattern.unshaped_trips:
            shape_of_trip[trip_id] = shape_id
            distance_of_trip[trip_id] = along

    if shape_of_trip:
        if "shape_id" not in trips:
            trips["shape_id"] = pd.NA
        trips["shape_id"] = [
            shape_of_trip.get(trip_id, existing)
            for trip_id, existing in zip(trips["trip_id"], trips["shape_id"])
        ]
        stop_times = _with_distances(stop_times, distance_of_trip)

    shapes = _shapes_table(tables.get("shapes.txt"), shape_rows)
    with (
        zipfile.ZipFile(path) as source,
        zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as out,
    ):
        wrote_shapes = False
        for name in source.namelist():
            if name == "trips.txt":
                out.writestr(name, _to_csv(trips))
            elif name == "stop_times.txt":
                out.writestr(name, _to_csv(stop_times))
            elif name == "shapes.txt":
                out.writestr(name, _to_csv(shapes))
                wrote_shapes = True
            else:
                # Copied members are budgeted and streamed: an
                # oversized ancillary file must not be expanded whole.
                info = source.getinfo(name)
                if info.file_size > MAX_ENTRY_BYTES:
                    raise OSError(
                        f"{name} is {info.file_size} bytes uncompressed, "
                        f"over the {MAX_ENTRY_BYTES} budget"
                    )
                with source.open(info) as reader, out.open(name, "w") as writer:
                    shutil.copyfileobj(reader, writer, length=1 << 20)
        if not wrote_shapes and shapes is not None:
            out.writestr("shapes.txt", _to_csv(shapes))


def _shapes_table(existing, shape_rows):
    """The existing shape rows plus the inferred ones, one schema.

    ``None`` when there is nothing to write at all.
    """
    columns = [
        "shape_id",
        "shape_pt_lat",
        "shape_pt_lon",
        "shape_pt_sequence",
        "shape_dist_traveled",
    ]
    added = pd.DataFrame(
        [
            {
                "shape_id": shape_id,
                "shape_pt_lat": f"{lat:.6f}",
                "shape_pt_lon": f"{lon:.6f}",
                "shape_pt_sequence": str(sequence),
                "shape_dist_traveled": str(metres),
            }
            for shape_id, lat, lon, sequence, metres in shape_rows
        ],
        columns=columns,
    )
    if existing is None:
        return added if len(added) else None
    if existing.empty:
        # A present-but-empty table keeps its own schema when nothing
        # is added, and gains the core schema when something is.
        if not len(added):
            return existing
        if not len(existing.columns):
            return added
    elif not len(added):
        return existing
    # The union schema keeps the feed's own extension columns; the
    # inferred rows simply have nothing to say in them.
    return pd.concat([existing, added], ignore_index=True)[
        list(existing.columns)
        + [column for column in added.columns if column not in existing.columns]
    ]


def _to_csv(table):
    """CSV bytes with missing values written blank, never ``nan``."""
    return table.to_csv(index=False, na_rep="")


def _with_distances(stop_times, distance_of_trip):
    """``shape_dist_traveled`` filled in for the shaped trips."""
    if "shape_dist_traveled" not in stop_times:
        stop_times["shape_dist_traveled"] = None
    order = pd.to_numeric(stop_times["stop_sequence"], errors="coerce")
    stop_times = stop_times.assign(_order=order).sort_values(
        ["trip_id", "_order"], kind="stable"
    )
    values = list(stop_times["shape_dist_traveled"])
    positions = collections.defaultdict(int)
    trip_ids = list(stop_times["trip_id"])
    for index, trip_id in enumerate(trip_ids):
        along = distance_of_trip.get(trip_id)
        if along is None:
            continue
        seat = positions[trip_id]
        if seat < len(along):
            values[index] = round(along[seat] - along[0], 3)
        positions[trip_id] = seat + 1
    stop_times["shape_dist_traveled"] = values
    return stop_times.drop(columns=["_order"])
