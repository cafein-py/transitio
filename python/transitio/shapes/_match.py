"""GTFS-pattern ↔ OSM-route-relation matching.

A deterministic eligibility-then-selection rule: mode compatibility
(never relaxed), route-ref agreement, and a corridor check gate the
candidates; the operator/network filter narrows them; and the variant
with the lowest normalized stop-sequence distance wins when it clears
the level's acceptance threshold and runner-up margin. How much of
that refuses versus ranks is the caller's `Level` — see
`transitio.shapes._levels`. Every candidate's component outcome is
recorded, so a match can be audited whatever the level.

Two asymmetries matter. The corridor is measured as *the pattern's
stops covered by the relation* — "does this pattern run along this
route" — not the reverse: a short working legitimately covers only
part of its route's alignment, and demanding the reverse would refuse
every one of them. Scoring is likewise needle-in-haystack: the
pattern's stop sequence is aligned against the relation's boarding
sequence with leading and trailing relation stops free, so a partial
run scores on the part it actually serves.
"""

import dataclasses
import math
import re
import warnings

import numpy as np

from transitio.shapes._geometry import SNAP_TOLERANCE

#: Consecutive stop/platform members within this distance collapse to
#: one canonical boarding position, in meters.
STOP_COLLAPSE_METERS = 50.0

#: Ranking penalty applied to a candidate whose route ref disagrees,
#: at levels where the ref ranks instead of gating. Large enough that
#: a ref-agreeing candidate always outranks a disagreeing one at equal
#: sequence evidence, small enough to stay inside the permissive
#: acceptance threshold on its own.
REF_MISMATCH_PENALTY = 0.15

#: Mode families the matcher understands (keyed by the GTFS side), and
#: the OSM ``route=`` values each matches against. This mapping is the
#: one hard filter no strictness level relaxes.
MODE_ROUTES = {
    "tram": ("tram", "light_rail"),
    "subway": ("subway",),
    "train": ("train",),
    "bus": ("bus",),
    "trolleybus": ("trolleybus",),
    "ferry": ("ferry",),
}


def mode_of(route_type):
    """The GTFS route type's mode family, or ``None`` outside the map.

    Covers the basic types and the extended (Hierarchical Vehicle Type)
    ranges, which real European feeds use freely: coach and trolleybus
    ride with bus, suburban rail with train, urban/metro with subway.
    A type outside every range is not guessed at — it simply has no
    mode, and its patterns are left alone.
    """
    ranges = (
        ("train", ((100, 200), (300, 400))),  # rail, suburban rail
        ("subway", ((400, 500), (500, 600), (600, 700))),  # urban, metro, under
        ("bus", ((200, 300), (700, 800), (1500, 1600))),  # coach, bus, taxi-bus
        ("trolleybus", ((800, 900),)),
        ("tram", ((900, 1000),)),
        ("ferry", ((1000, 1100), (1200, 1300))),  # water, ferry
    )
    basic = {3: "bus", 11: "trolleybus", 0: "tram", 1: "subway", 2: "train", 4: "ferry"}
    if route_type in basic:
        return basic[route_type]
    for mode, spans in ranges:
        if any(low <= route_type < high for low, high in spans):
            return mode
    return None


@dataclasses.dataclass(frozen=True)
class Selection:
    """A selected relation and the orientation that won: ``reversed``
    means the pattern travels the member sequence backward."""

    relation: object
    reversed: bool


@dataclasses.dataclass(frozen=True)
class Pattern:
    """One GTFS stop pattern with its route's matching metadata.

    ``stop_xy`` is the stops' projected coordinates in meters, aligned
    with ``stop_ids``; ``agency`` carries the feed's raw agency identity
    strings (name and id) — empty when the feed has no agency.
    """

    stop_ids: tuple
    stop_xy: object
    short_name: str | None
    long_name: str | None
    agency: tuple


_FOLD = re.compile(r"[\W_]+")
_SPACE = re.compile(r"\s+")


def fold(text):
    """The case/whitespace/punctuation-insensitive comparison form —
    for names, where the containment rule permits it."""
    if text is None:
        return ""
    return _FOLD.sub("", str(text).casefold())


def fold_ref(text):
    """The ref comparison form: case and whitespace fold only —
    punctuation stays significant (``1-A`` never equals ``1A``)."""
    if text is None:
        return ""
    return _SPACE.sub("", str(text).casefold())


def boarding_positions(relation):
    """Ordered ``(is_stop, lon, lat)`` boarding members of a relation.

    ``stop``-role and ``platform``-role members (role variants like
    ``stop_exit_only`` included) with materialized geometry; way or
    area platforms contribute their centroid.
    """
    ordered = []
    for member in relation.members:
        role = member.role
        if not (role.startswith("stop") or role.startswith("platform")):
            continue
        if member.geometry is None:
            # An unresolved boarding member (clipped extract, stale
            # data) keeps its slot: it counts against corridor
            # containment and matches nothing in the sequence.
            ordered.append((role.startswith("stop"), math.nan, math.nan))
            continue
        point = member.geometry
        if point.geom_type != "Point":
            point = point.centroid
        ordered.append((role.startswith("stop"), point.x, point.y))
    return ordered


def collapse_positions(kinds, xy):
    """The canonical boarding positions, as an ``(n, 2)`` array.

    Consecutive members within `STOP_COLLAPSE_METERS` are one physical
    boarding location — relations commonly carry both a ``stop`` node
    and a ``platform`` member for it — collapsed to one position that
    prefers the ``stop``-role coordinate.
    """
    canonical = []
    for is_stop, (x, y) in zip(kinds, xy):
        if canonical:
            last = canonical[-1]
            if math.hypot(x - last[0], y - last[1]) <= STOP_COLLAPSE_METERS:
                if is_stop and not last[2]:
                    canonical[-1] = [x, y, True]
                continue
        canonical.append([x, y, is_stop])
    return np.asarray([position[:2] for position in canonical], dtype=float)


def edit_distance(a, b):
    """Unit-cost Levenshtein distance normalized by the longer length."""
    if not a and not b:
        return 0.0
    previous = list(range(len(b) + 1))
    for row, item in enumerate(a, start=1):
        current = [row]
        for column, other in enumerate(b, start=1):
            current.append(
                min(
                    previous[column] + 1,
                    current[column - 1] + 1,
                    previous[column - 1] + (item != other),
                )
            )
        previous = current
    return previous[-1] / max(len(a), len(b))


def sequence_distance(pattern_ids, assigned):
    """How far ``pattern_ids`` is from running along ``assigned``.

    An approximate-subsequence distance: the pattern is the needle and
    the relation's assigned boarding sequence the haystack, so leading
    and trailing relation stops cost nothing and the result is
    normalized by the pattern's own length. A pattern that serves the
    whole route scores exactly as plain edit distance would; a short
    working scores on the stops it actually serves instead of being
    penalized for the ones its route runs without it.
    """
    if not pattern_ids:
        return 0.0
    if not assigned:
        return 1.0
    # Row 0 all zeros: starting anywhere in the haystack is free.
    previous = [0] * (len(assigned) + 1)
    for row, item in enumerate(pattern_ids, start=1):
        current = [row]
        for column, other in enumerate(assigned, start=1):
            current.append(
                min(
                    previous[column] + 1,
                    current[column - 1] + 1,
                    previous[column - 1] + (item != other),
                )
            )
        previous = current
    # Ending anywhere in the haystack is free.
    return min(previous) / len(pattern_ids)


def select(pattern, entries, level):
    """The winning relation for a pattern, or ``None`` — plus the
    per-candidate diagnostics either way.

    ``entries`` are mode-compatible ``(relation, canonical_xy)`` pairs
    (``canonical_xy`` from `collapse_positions`, or ``None`` when the
    relation has no boarding members); ``level`` is the `Level` whose
    thresholds decide what refuses and what merely ranks.
    """
    diagnostics = []
    survivors = []
    for relation, canonical in entries:
        record = {
            "relation": relation.id,
            "route": relation.route,
            "ref": relation.ref,
            "stage": None,
            "ref_agrees": None,
            "pattern_covered": None,
            "relation_covered": None,
            "operator": None,
            "forward": None,
            "backward": None,
            "score": None,
            "reversed": None,
            "outcome": None,
        }
        diagnostics.append(record)
        record["ref_agrees"] = _ref_agrees(pattern, relation)
        if level.ref_required and not record["ref_agrees"]:
            record["stage"] = "ref"
            continue
        if canonical is None or not len(canonical):
            record["stage"] = "no-boarding"
            continue
        distance = np.hypot(
            canonical[:, 0, None] - pattern.stop_xy[None, :, 0],
            canonical[:, 1, None] - pattern.stop_xy[None, :, 1],
        )
        # An unresolved boarding member is a NaN row: it contributes no
        # coverage, but it must not erase what the resolved members
        # prove, so the reductions ignore NaN rather than propagate it.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            nearest = np.nanmin(distance, axis=1)
            # The eligibility direction: every pattern stop should lie
            # on the relation. The reverse fraction is recorded for
            # diagnostics but never gates — a short working covers only
            # part of its route's alignment by definition.
            per_stop = np.nanmin(distance, axis=0)
        nearest = np.nan_to_num(nearest, nan=np.inf)
        per_stop = np.nan_to_num(per_stop, nan=np.inf)
        record["pattern_covered"] = float((per_stop <= level.corridor_meters).mean())
        record["relation_covered"] = float((nearest <= level.corridor_meters).mean())
        if record["pattern_covered"] < level.containment:
            record["stage"] = "corridor"
            continue
        survivors.append((relation, record, distance, nearest))
    scored = []
    for relation, record, distance, nearest in _operator_filtered(
        pattern, survivors, level
    ):
        assigned = _assigned_ids(pattern, distance, nearest)
        forward = sequence_distance(pattern.stop_ids, assigned)
        backward = sequence_distance(pattern.stop_ids, assigned[::-1])
        penalty = 0.0 if record["ref_agrees"] else REF_MISMATCH_PENALTY
        record["stage"] = "scored"
        record["forward"] = forward
        record["backward"] = backward
        record["score"] = min(forward, backward) + penalty
        record["reversed"] = backward < forward
        scored.append((record["score"], relation, record))
    if not scored:
        return None, diagnostics
    scored.sort(key=lambda entry: entry[0])
    best_score, best, best_record = scored[0]
    if best_score > level.accept:
        for _, _, record in scored:
            record["outcome"] = "over-threshold"
        return None, diagnostics
    if len(scored) > 1 and scored[1][0] - best_score < level.margin:
        for _, _, record in scored:
            record["outcome"] = "near-tie"
        return None, diagnostics
    for _, _, record in scored[1:]:
        record["outcome"] = "runner-up"
    best_record["outcome"] = "selected"
    return Selection(best, bool(best_record["reversed"])), diagnostics


def _ref_agrees(pattern, relation):
    """Exact folded ref agreement; a ref-less relation is eligible only
    for a ref-less route, through name equality-or-containment."""
    short = fold_ref(pattern.short_name)
    ref = fold_ref(relation.ref)
    if short:
        return ref == short
    if ref:
        return False
    name = fold(relation.name)
    long_name = fold(pattern.long_name)
    if not name or not long_name:
        return False
    return name in long_name or long_name in name


def _operator_filtered(pattern, survivors, level):
    """The operator/network filter: *match* candidates when any exist,
    else the *absent* group. Present-and-disagreeing tags disqualify
    only when the level says so; otherwise they stay as last-resort
    candidates. Skipped when the feed names no agency."""
    identity = {fold(value) for value in pattern.agency if fold(value)}
    if not identity:
        return survivors
    matches = []
    absent = []
    mismatched = []
    for entry in survivors:
        relation, record = entry[0], entry[1]
        present = [
            fold(tag)
            for tag in (relation.operator, relation.network)
            if tag is not None
        ]
        if any(tag in identity for tag in present if tag):
            record["operator"] = "match"
            matches.append(entry)
        elif not any(present):
            record["operator"] = "absent"
            absent.append(entry)
        else:
            record["operator"] = "mismatch"
            mismatched.append(entry)
    if not level.operator_mismatch_disqualifies:
        # Metadata disagreement demotes rather than removes: the
        # groups stay in preference order.
        groups = (matches, absent, mismatched)
    else:
        for entry in mismatched:
            entry[1]["stage"] = "operator"
        groups = (matches, absent)
    for group in groups:
        if group:
            for other in groups:
                if other is not group:
                    for entry in other:
                        entry[1]["stage"] = "operator"
            return group
    return []


def _assigned_ids(pattern, distance, nearest):
    """Each canonical position's nearest pattern stop within the snap
    tolerance, or a gap symbol that matches nothing."""
    closest = distance.argmin(axis=1)
    return tuple(
        pattern.stop_ids[column] if nearest[row] <= SNAP_TOLERANCE else ("gap", row)
        for row, column in enumerate(closest)
    )
