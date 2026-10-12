"""Which of a place's feeds to use, and why the others are left out.

:meth:`Place.recommend` and :meth:`Area.recommend` answer with a
:class:`Recommendation`. They consider the feeds serving the place at the
tiers asked for, cross-border ones included: by default local and regional
ones (``DEFAULT_TIERS``) at a city or a metro, every tier at a region or a
country. A feed serving the place only at other tiers is listed as not
considered, as is a feed the index marks as a private or campus service or
an unofficial compilation. A feed is left out when its timetable as indexed
does not run on the day (the study day, else today), whether or not it was
stale when indexed, when it needs a paid account, or when the index measured
no service for it.

The candidates are taken the most local first: the one whose coverage
covers the smallest area, then the open one, the newer timetable and the
fewer stops (:func:`_local_key`), so an operator's own feed comes before an
aggregate repeating it and fetch, which keeps a trip in the first feed
delivering it, keeps the operator's trips. On an index that records which
feeds run the same lines (each tier edge's ``summary["overlap"]``), the
place's departures are counted once per mode family, rail, subway and tram
being one family, and each candidate is taken when it adds a departure the
feeds taken before it do not run. A ``goal`` stops the selection once the
taken feeds cover that share of the departures and ``FAMILY_TARGET`` of
each family holding at least ``FAMILY_SHARE`` of them, and ``max_feeds``
once that many are taken. A merged index measures overlap within each build
only and lists, per block, the feeds its build compared (``compared``): the
feeds are compared within the largest group measured together, and the
feeds the index did not compare with them are taken as well, their trips
repeated in the feeds before them being dropped when they are fetched. On an
index without overlap evidence every feed is taken.
"""

import math
import numbers
from collections import namedtuple

from transitio.index.feeds import (
    MARKS,
    _access_note,
    _family_blocks,
    _names,
    _percent,
    _summed_service,
    _universe,
    _unrun,
)

DEFAULT_TIERS = ("local", "regional")
FAMILY_TARGET = 0.8
FAMILY_SHARE = 0.01
# A share of the place's departures below this is float residue: a feed
# adding no more adds nothing.
_NOTHING = 1e-9
# A feed left out names a second taken feed running at least this much more
# of its departures.
_SECOND_FEED = 0.05
_FAMILY_WORDS = {"rail": "rail, subway and tram"}

Choice = namedtuple("Choice", ["feed", "reason", "adds"])


class Recommendation:
    """Which feeds to take for a place or an area, and why the others are
    left out.

    ``taken`` and ``left_out`` hold a :class:`Choice` per feed: the
    :class:`~transitio.index.IndexedFeed`, the reason in words, and
    ``adds``, the share of the place's departures the feed adds to the
    compared feeds taken before it (for a feed left out, to all the compared
    feeds taken), None where it was not compared. ``coverage`` is the share
    of the place's departures, each counted once, that the taken feeds run, and
    ``coverage_by_family`` the same per mode family (``"rail"`` for rail,
    subway and tram), both counting only the feeds the index records overlap
    for. ``basis`` is ``"overlap"``, or ``"departures"`` on an index without
    overlap evidence, where the coverages are None. ``note`` says when a
    ``target`` or ``max_feeds`` stopped the selection, which feeds were taken
    without comparison, or that the index records no overlap. ``tiers`` are
    the tiers whose feeds were considered, those asked for less those
    excluded, None for every tier; a feed serving the place at other tiers
    only is left out as not considered.
    ``str()`` gives the answer as lines of text.
    """

    def __init__(
        self,
        place,
        when,
        taken,
        left_out,
        coverage,
        coverage_by_family,
        basis,
        note,
        tiers=None,
        excluded=(),
    ):
        self.place = place
        self.when = when
        self.taken = list(taken)
        self.left_out = list(left_out)
        self.coverage = coverage
        self.coverage_by_family = coverage_by_family
        self.basis = basis
        self.note = note
        self.tiers = None if tiers is None else tuple(tiers)
        self._excluded = frozenset(excluded)

    @property
    def feed_ids(self):
        """The ids of the taken feeds, in the order they were taken."""
        return [choice.feed.feed_id for choice in self.taken]

    def to_dataframe(self):
        """The taken feeds, then those left out, one row each: ``feed_id``,
        ``name``, ``taken``, ``adds`` and ``reason``."""
        import pandas

        rows = [
            {
                "feed_id": choice.feed.feed_id,
                "name": choice.feed.name,
                "taken": taken,
                "adds": choice.adds,
                "reason": choice.reason,
            }
            for taken, choices in ((True, self.taken), (False, self.left_out))
            for choice in choices
        ]
        return pandas.DataFrame(
            rows, columns=["feed_id", "name", "taken", "adds", "reason"]
        )

    def __str__(self):
        head = _label(self.place) + (f", {self.when}" if self.when else "")
        count = len(self.taken)
        take = f"take {count} feed{'' if count == 1 else 's'}"
        if self.basis == "departures":
            lines = [f"{head}: {take}"]
        else:
            share = _percent(self.coverage)
            # "over 99 %" is a bound already, not an estimate.
            share = share if share.startswith("over") else f"about {share}"
            lines = [
                f"{head}: {take}, covering {share} of the departures the index "
                "records there, each counted once"
            ]
        if self.tiers is not None:
            lines.append(f"  {_considered(self.tiers, self._excluded)}")
        lines += [f"  {self.note}"] if self.note else []
        lines += [f"  + {_named(c.feed)}: {c.reason}" for c in self.taken]
        alike = {}
        for choice in self.left_out:
            alike.setdefault(choice.reason, []).append(choice.feed)
        for reason, feeds in alike.items():
            names = ", ".join(f.feed_id for f in feeds)
            lines.append(
                f"  - {_named(feeds[0]) if len(feeds) == 1 else names}: {reason}"
            )
        return "\n".join(lines)

    def __repr__(self):
        return (
            f"Recommendation({_label(self.place)!r}, feed_ids={self.feed_ids!r}, "
            f"basis={self.basis!r})"
        )


def _default_tiers(target):
    """The tiers a place or an area considers by default: every tier (None)
    at a region or a country, or an area with one among its parts, else
    ``DEFAULT_TIERS``."""
    from transitio.index.places import Place

    places = [target] if isinstance(target, Place) else [p.place for p in target.parts]
    if any(place.kind in ("region", "country") for place in places):
        return None
    return DEFAULT_TIERS


def recommend_for(target, when, tiers, exclude, goal, max_feeds):
    """The :class:`Recommendation` for ``target``, a place or an area, from
    the feeds serving it at ``tiers`` (every tier when None, the kind of
    place's own when ``"default"``), cross-border ones included, listing
    those serving it at other tiers only as not considered."""
    if isinstance(tiers, str) and tiers == "default":
        tiers = _default_tiers(target)
    else:
        tiers = _names(tiers)
    exclude = _names(exclude)
    excluded = set(exclude or ())
    feeds = target.feeds(
        tiers=tiers, exclude=exclude, categories=None, international=True
    )
    outside = []
    if tiers is not None or excluded:
        considered = {feed.feed_id for feed in feeds}
        outside = [
            feed
            for feed in target.feeds(categories=None, international=True)
            if feed.feed_id not in considered
        ]
    if tiers is not None or excluded:
        named = _TIER_ORDER[:4] if tiers is None else tiers
        tiers = tuple(tier for tier in named if tier not in excluded)
    return recommend(
        target,
        feeds,
        when,
        goal=goal,
        max_feeds=max_feeds,
        tiers=tiers,
        outside=outside,
        excluded=excluded,
    )


def recommend(
    target,
    feeds,
    when,
    *,
    goal=None,
    max_feeds=None,
    tiers=None,
    outside=(),
    excluded=(),
):
    """The :class:`Recommendation` for ``target``, a place or an area, from
    its ``feeds`` on ``when`` (today when None): the feeds at the ``tiers``
    considered (every tier when None), with ``outside`` the feeds serving it
    at other tiers only and ``excluded`` the tiers left out by ``exclude``;
    see :meth:`~transitio.index.Place.recommend`."""
    if goal is not None and not 0 < goal <= 1:
        raise ValueError(f"target must be above 0 and at most 1, not {goal!r}")
    if max_feeds is not None and (
        isinstance(max_feeds, bool)
        or not isinstance(max_feeds, numbers.Integral)
        or max_feeds < 1
    ):
        raise ValueError(
            f"max_feeds must be a whole number of at least 1, not {max_feeds!r}"
        )
    if when is not None:
        from transitio.catalog._models import as_date

        try:
            when = as_date(when)
        except (TypeError, ValueError):
            raise ValueError(f"when must be a date, not {when!r}") from None
    from transitio.pipeline._fetch import _today

    day = when or _today()
    blocks = _family_blocks(feeds)
    left_out, candidates = [], []
    for feed in feeds:
        reason = _excluded(feed, day, blocks)
        if reason is None:
            candidates.append(feed)
        else:
            left_out.append(Choice(feed, _with_access(feed, reason), None))
    left_out += [
        Choice(f, _with_access(f, _not_considered(f, excluded)), None) for f in outside
    ]
    blocks = {f.feed_id: blocks[f.feed_id] for f in candidates if f.feed_id in blocks}
    apart = _apart(candidates, blocks)
    blocks = {i: block for i, block in blocks.items() if i not in apart}
    order = sorted(candidates, key=_local_key)
    if not blocks:
        taken, rest, note = _without_overlap(order, max_feeds)
        left_out = _largest_first(rest) + _largest_first(left_out)
        return Recommendation(
            target,
            when,
            taken,
            left_out,
            None,
            None,
            "departures",
            note,
            tiers,
            excluded,
        )
    universe = _universe(blocks)
    total = sum(universe.values())
    by_id = {feed.feed_id: feed for feed in candidates}
    compared, chosen, rest, uncompared = [], [], [], []
    reached = False
    capped = targeted = 0
    for feed in order:
        block = blocks.get(feed.feed_id)
        full = max_feeds is not None and len(chosen) >= max_feeds
        stopped = (
            "the feeds taken reach the target without it"
            if reached
            else f"at most {_feeds_are(max_feeds)} taken"
        )
        if block is None:
            why = (
                "the index measured it apart from the feeds compared"
                if feed.feed_id in apart
                else "the index records no overlap for it"
            )
            if reached or full:
                targeted, capped = targeted + reached, capped + (not reached)
                reason = f"not compared: {why}; {stopped}"
                rest.append(Choice(feed, _with_access(feed, reason), None))
                continue
            reason = (
                f"not compared: {why}, so what it adds is unknown; fetch leaves out "
                "the trips it repeats of the feeds before it"
            )
            chosen.append(Choice(feed, _with_access(feed, reason), None))
            uncompared.append(feed)
            continue
        adds = sum(_unrun(block, compared).values()) / total
        if adds <= _NOTHING:
            stops = [by_id[i].stop_count for i in compared]
            stops = None if None in stops else sum(stops)
            reason = _repeats(feed, block, compared, stops)
            rest.append(Choice(feed, _with_access(feed, reason), adds))
            continue
        if reached or full:
            targeted, capped = targeted + reached, capped + (not reached)
            reason = f"adds {_percent(adds)} of the place's departures; {stopped}"
            rest.append(Choice(feed, _with_access(feed, reason), adds))
            continue
        compared.append(feed.feed_id)
        chosen.append(
            _taken_choice(
                by_id, blocks, compared, len(compared) - 1, universe, bool(uncompared)
            )
        )
        if goal is not None:
            by_family = _covered(blocks, compared, universe)
            coverage = sum(by_family[f] * u for f, u in universe.items()) / total
            reached = _reached(by_family, coverage, universe, goal)
    by_family = _covered(blocks, compared, universe)
    coverage = sum(by_family[f] * u for f, u in universe.items()) / total
    notes = []
    if capped:
        notes.append(
            f"at most {_feeds_are(max_feeds)} taken; {_feeds_are(capped)} left out "
            "for it"
        )
    if targeted:
        notes.append(
            f"the feeds taken reach the target of {_percent(goal)} of the "
            f"departures; {_feeds_are(targeted)} left out for it"
        )
    elif goal is not None and not reached:
        short = [f for f in _families(universe) if by_family[f] < FAMILY_TARGET]
        missing = (
            f"{_percent(goal)} of them"
            if coverage < goal
            else f"{_percent(FAMILY_TARGET)} of "
            + " and of ".join(_FAMILY_WORDS.get(f, f) for f in short)
        )
        notes.append(
            f"the feeds taken cover {_percent(coverage)} of the departures, "
            f"short of {missing}"
            + ("" if capped else ": no other feed adds a departure")
        )
    if uncompared:
        departures = sum(
            _summed_service(feed)["departures_per_day"] or 0.0 for feed in uncompared
        )
        notes.append(
            "the coverage counts only the feeds compared; "
            f"{_feeds_are(len(uncompared))} taken without comparison "
            f"({departures:,.0f} departures a day)"
        )
    left_out = _largest_first(rest) + _largest_first(left_out)
    note = "; ".join(notes) or None
    return Recommendation(
        target,
        when,
        chosen,
        left_out,
        coverage,
        by_family,
        "overlap",
        note,
        tiers,
        excluded,
    )


def _excluded(feed, day, blocks):
    """Why ``feed`` is no candidate on ``day``, or None: its timetable as
    indexed must run on the day, whether or not it was stale when indexed."""
    if feed.mark is not None:
        reason = "not considered: fetch takes it when named in feeds="
        return f"{MARKS[feed.mark]}, {reason}"
    ended = feed.stale_when_indexed
    if ended is not None and ended < day:
        return f"stale when indexed: its timetable ended {ended}"
    start, end = feed.service_start, feed.service_end
    if end is not None and end < day:
        return (
            f"its timetable as indexed ends {end}, before {day} "
            "(a newer download may run then)"
        )
    if start is not None and start > day:
        return f"its timetable as indexed starts {start}, after {day}"
    provider = feed._provider()
    if feed.access == "key" and provider is not None and provider.free is False:
        return _access_note(feed)
    departures = _summed_service(feed)["departures_per_day"]
    if feed.feed_id not in blocks and not departures:
        return "not measured: the index has no timetable for it here"
    return None


def _area(feed):
    """The area the feed's coverage covers, in square degrees; None without a
    readable coverage."""
    import shapely

    if feed.coverage is None:
        return None
    try:
        geometry = shapely.from_wkb(feed.coverage)
    except (shapely.errors.GEOSException, TypeError, ValueError):
        return None
    area = None if geometry is None or geometry.is_empty else geometry.area
    return area if area is not None and math.isfinite(area) else None


def _local_key(feed):
    """Orders feeds the most local first: the smallest coverage area (an
    unknown one counting as the largest), then open before key, the newer
    timetable, fewer stops (unknown counting as most), then the id."""
    area = _area(feed)
    start = feed.service_start
    return (
        math.inf if area is None else area,
        feed.access == "key",
        -start.toordinal() if start is not None else 0,
        math.inf if feed.stop_count is None else feed.stop_count,
        feed.feed_id,
    )


def _apart(feeds, blocks):
    """The ids among ``blocks`` outside the group of feeds the index measured
    together that holds the most departures, the groups joined by the
    ``compared`` lists of the feeds' overlap blocks; empty when no block
    carries one or all are measured together."""
    links = {feed_id: set() for feed_id in blocks}
    listed = False
    for feed in feeds:
        if feed.feed_id not in blocks:
            continue
        for edge in feed.edges.values():
            compared = (edge.summary.get("overlap") or {}).get("compared")
            if not isinstance(compared, list):
                continue
            listed = True
            for other in compared:
                if other in links and other != feed.feed_id:
                    links[feed.feed_id].add(other)
                    links[other].add(feed.feed_id)
    if not listed:
        return set()
    groups, seen = [], set()
    for start in sorted(blocks):
        if start in seen:
            continue
        group, stack = set(), [start]
        while stack:
            feed_id = stack.pop()
            if feed_id not in group:
                group.add(feed_id)
                stack.extend(links[feed_id] - group)
        seen |= group
        groups.append(group)
    if len(groups) < 2:
        return set()

    def held(group):
        return sum(_universe({i: blocks[i] for i in group}).values())

    kept = max(groups, key=lambda group: (held(group), sorted(group)))
    return set(blocks) - kept


def _covered(blocks, taken, universe):
    """Per family, the share of the place's departures the ``taken`` feeds
    run, each adding what the feeds before it do not."""
    covered = dict.fromkeys(universe, 0.0)
    for n, feed_id in enumerate(taken):
        for family, value in _unrun(blocks[feed_id], taken[:n]).items():
            covered[family] += value
    return {f: min(1.0, covered[f] / u) if u else 0.0 for f, u in universe.items()}


def _families(universe):
    """The families holding at least ``FAMILY_SHARE`` of the place's
    departures, the largest first."""
    total = sum(universe.values())
    held = [(-u, f) for f, u in universe.items() if u >= FAMILY_SHARE * total]
    return [family for _, family in sorted(held)]


def _reached(by_family, coverage, universe, goal):
    return coverage >= goal and all(
        by_family[family] >= FAMILY_TARGET for family in _families(universe)
    )


def _taken_choice(by_id, blocks, taken, n, universe, after_uncompared=False):
    """The :class:`Choice` of the ``n``-th taken feed, saying "the compared
    feeds above" when ``after_uncompared`` feeds taken without comparison
    come before it."""
    feed_id = taken[n]
    added = _unrun(blocks[feed_id], taken[:n])
    gain = sum(added.values())
    adds = gain / sum(universe.values())
    if n == 0:
        shares = "; ".join(
            f"{_percent(min(1.0, added.get(f, 0.0) / universe[f]))} of "
            f"{_FAMILY_WORDS.get(f, f)}"
            for f in _families(universe)
        )
        reason = f"covers {_percent(adds)} of the place's departures ({shares})"
    else:
        family = max(added, key=added.get)
        words = _FAMILY_WORDS.get(family, family)
        above = "the compared feeds above" if after_uncompared else "the feeds above"
        if added[family] >= gain / 2:
            reason = (
                f"adds {words} {above} lack: "
                f"{_percent(added[family] / universe[family])} of the place's {words}"
            )
        else:
            reason = f"adds {_percent(adds)} of the place's departures"
            if after_uncompared:
                reason += " to the compared feeds above"
    return Choice(by_id[feed_id], _with_access(by_id[feed_id], reason), adds)


def _repeats(feed, block, taken, stops):
    """ "repeats X (N % of its departures)" for a feed adding no departure to
    the ``taken`` ones, naming the one running the most of them and, when it
    runs ``_SECOND_FEED`` more of them, a second."""
    total = sum(block[0].values())

    def run(others):
        return 1 - sum(_unrun(block, others).values()) / total if total else 1.0

    if not taken:
        return "adds no departure"
    first = max(taken, key=lambda i: run([i]))
    names, share = first, run([first])
    others = [i for i in taken if i != first]
    if others:
        second = max(others, key=lambda i: run([first, i]))
        if run([first, second]) - share >= _SECOND_FEED:
            names, share = f"{first} and {second}", run([first, second])
    reason = f"repeats {names} ({_percent(share)} of its departures)"
    if stops and feed.stop_count and feed.stop_count > 2 * stops:
        reason += f"; {feed.stop_count:,} stops against {stops:,}"
    return reason


def _without_overlap(order, max_feeds):
    """``(taken, left_out, note)`` on an index without overlap evidence: every
    feed in ``order``, at most ``max_feeds`` of them."""
    if not order:
        return [], [], "no feed is left to take"
    taken, rest = [], []
    for feed in order:
        if max_feeds is not None and len(taken) == max_feeds:
            reason = (
                "not compared: the index records no overlap for it; "
                f"at most {_feeds_are(max_feeds)} taken"
            )
            rest.append(Choice(feed, _with_access(feed, reason), None))
            continue
        departures = _summed_service(feed)["departures_per_day"] or 0.0
        reason = f"{departures:,.0f} departures a day as indexed"
        taken.append(Choice(feed, _with_access(feed, reason), None))
    taking = "every feed is taken, the most local first"
    if rest:
        taking = (
            f"at most {max_feeds} are taken, the most local first, and "
            f"{_feeds_are(len(rest))} left out"
        )
    note = (
        f"this index records no overlap between feeds, so {taking}; fetch leaves "
        "out the trips a feed repeats of the feeds before it"
    )
    return taken, rest, note


def _feeds_are(count):
    """``count`` feeds are, or 1 feed is, in words."""
    return f"{count} feed{' is' if count == 1 else 's are'}"


def _largest_first(choices):
    """``choices`` by their feeds' departures in the place, the most first,
    then by id."""

    def key(choice):
        departures = _summed_service(choice.feed)["departures_per_day"]
        return (-(departures or 0.0), choice.feed.feed_id)

    return sorted(choices, key=key)


# The tiers in their order of reach, for the words that name them.
_TIER_ORDER = ("local", "regional", "national", "international", "unknown")


def _tier_words(tiers):
    """``tiers`` in their order of reach, joined as words: "local and
    regional"."""
    ordered = sorted(
        tiers, key=lambda t: (_TIER_ORDER.index(t) if t in _TIER_ORDER else 9, t)
    )
    if len(ordered) < 2:
        return "".join(ordered)
    return f"{', '.join(ordered[:-1])} and {ordered[-1]}"


def _considered(tiers, excluded=()):
    """The line saying which tiers' feeds a recommendation considered and
    which ``tiers=None`` adds, the ``excluded`` ones apart."""
    if not tiers:
        line = (
            "considers no feed"
            if "unknown" in excluded
            else "considers feeds of unknown tier only"
        )
    else:
        line = f"considers {_tier_words(tiers)} feeds only"
        if "unknown" not in excluded:
            line += ", and those of unknown tier"
    missing = [t for t in _TIER_ORDER[:4] if t not in tiers and t not in excluded]
    if missing:
        line += f"; tiers=None adds {_tier_words(missing)} feeds"
    return line


def _not_considered(feed, excluded=()):
    """Why a feed serving the place at other tiers only is left out, saying
    so when ``exclude`` left out every one of them."""
    tiers = {edge.tier for edge in feed.edges.values()}
    reason = (
        f"not considered: serves the place at the {_tier_words(tiers)} "
        f"tier{'' if len(tiers) == 1 else 's'}"
    )
    return reason + (", which exclude= leaves out" if tiers <= set(excluded) else "")


def _with_access(feed, reason):
    """``reason`` with the account the feed needs, where it needs one."""
    note = _access_note(feed)
    return reason if note is None or note == reason else f"{reason}; {note}"


def _label(target):
    """A place as its name and kind; an area by its number of places."""
    from transitio.index.places import Place

    if isinstance(target, Place):
        return f"{target.name} ({target.kind})"
    return f"The area's {len(target.parts)} places"


def _named(feed):
    """A feed's id, with its name when it has one of its own."""
    if feed.name and feed.name != feed.feed_id:
        return f"{feed.feed_id} ({feed.name})"
    return feed.feed_id
