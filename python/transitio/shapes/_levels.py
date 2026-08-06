"""Strictness levels — how much inference the caller will accept.

Shape inference trades coverage against certainty, and the right
trade depends on the feed. Where OSM is dense and the feed is
well-formed, refusing every uncertain match costs little and keeps
the written shapes trustworthy. Where a feed has no shapes at all and
OSM is patchy — the case these levels exist for — a 70%-confidence
alignment beats the straight line that would otherwise stand in.

The levels move the *judgement* thresholds only. Three things never
relax at any level, because they produce alignments that are
impossible rather than merely uncertain:

- the mode filter (a bus route never matches a tram relation),
- one-way and ring-direction legality in the stitcher,
- barrier and access legality in the mode graphs.

Everything a level loosens is recorded: an inferred shape carries the
level and the evidence that produced it, so a permissive run can be
audited or re-reviewed rather than silently trusted.
"""

import dataclasses


@dataclasses.dataclass(frozen=True)
class Level:
    """One strictness setting's thresholds.

    ``accept``/``margin`` bound the stop-sequence edit distance and the
    lead the winner needs over the runner-up; ``corridor_meters`` and
    ``containment`` the spatial eligibility corridor; ``detour`` scales
    the per-mode map-matching detour bounds; ``ref_required`` decides
    whether an exact route-ref agreement gates candidacy or merely
    ranks it; ``operator_mismatch_disqualifies`` whether disagreeing
    operator metadata removes a candidate or only demotes it.
    """

    name: str
    accept: float
    margin: float
    corridor_meters: float
    containment: float
    detour: float
    ref_required: bool
    operator_mismatch_disqualifies: bool


STRICT = Level(
    name="strict",
    accept=0.25,
    margin=0.05,
    corridor_meters=500.0,
    containment=0.90,
    detour=1.0,
    ref_required=True,
    operator_mismatch_disqualifies=True,
)
"""Calibrated on Helsinki against withheld ground truth: matches only
where the evidence is unambiguous. Uncertain patterns are left without
a shape rather than given a plausible-looking wrong one."""

RELAXED = Level(
    name="relaxed",
    accept=0.45,
    margin=0.0,
    corridor_meters=750.0,
    containment=0.70,
    detour=1.4,
    ref_required=True,
    operator_mismatch_disqualifies=False,
)
"""For feeds with partial OSM coverage: the route identity must still
agree, but partial stop-sequence overlap, a wider corridor and a
near-tie between candidates no longer refuse."""

PERMISSIVE = Level(
    name="permissive",
    accept=0.70,
    margin=0.0,
    corridor_meters=1500.0,
    containment=0.50,
    detour=2.5,
    ref_required=False,
    operator_mismatch_disqualifies=False,
)
"""For feeds where a guess beats a straight line: route identity
becomes a ranking signal rather than a gate, so a schedule variant can
inherit its parent route's alignment. Expect wrong corridors; keep the
provenance and review the output."""

LEVELS = {level.name: level for level in (STRICT, RELAXED, PERMISSIVE)}


def resolve(strictness):
    """The `Level` for a name or `Level`; raises on anything else."""
    if isinstance(strictness, Level):
        return strictness
    try:
        return LEVELS[strictness]
    except (KeyError, TypeError):
        raise ValueError(
            f"unknown strictness {strictness!r}; expected one of "
            + ", ".join(sorted(LEVELS))
        ) from None
