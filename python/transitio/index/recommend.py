"""Which of a place's feeds to use, and why the others are left out.

:meth:`Place.recommend` and :meth:`Area.recommend` answer with a
:class:`Recommendation`. A feed is left out first when it was stale when
indexed, when its timetable as indexed does not run on the day, when it
needs a paid account, or when the index measured no service for it.

On an index that records which feeds run the same lines (each tier edge's
``evidence["overlap"]``), the place's departures are counted once per mode
family, rail, subway and tram being one family, and feeds are taken one at
a time. Each step takes the feed adding the most departures the taken feeds
do not run, or among the feeds adding within ``SIZE_TOLERANCE`` of the
place's departures of that, the open one with the fewest stops. It stops
once the taken feeds cover ``TARGET`` of the departures and
``FAMILY_TARGET`` of each family holding at least ``FAMILY_SHARE`` of them,
when no feed adds ``MIN_GAIN``, or at ``MAX_FEEDS`` feeds; a taken feed
adding less than ``MIN_GAIN`` beside the others is then dropped. On an
index without that evidence the feed with the most departures is taken, or
a smaller one within ``SIZE_TOLERANCE`` of them.
"""

import math
import numbers
from collections import namedtuple

from transitio.index.feeds import (
    _access_note,
    _family_blocks,
    _percent,
    _summed_service,
    _universe,
    _unrun,
)

TARGET = 0.95
FAMILY_TARGET = 0.8
FAMILY_SHARE = 0.01
MIN_GAIN = 0.01
SIZE_TOLERANCE = 0.02
MAX_FEEDS = 4
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
    feeds taken before it (for a feed left out, to all of them), None where
    it was not compared. ``coverage`` is the share of the place's
    departures, each counted once, that the taken feeds run, and
    ``coverage_by_family`` the same per mode family (``"rail"`` for rail,
    subway and tram), both counting only the feeds the index records overlap
    for. ``basis`` is ``"overlap"``, or ``"departures"`` on an index without
    overlap evidence, where the coverages are None. ``note`` says when the
    target is not reached, which feeds the coverage leaves out, or that the
    index records no overlap.
    ``str()`` gives the answer as lines of text.
    """

    def __init__(
        self, place, when, taken, left_out, coverage, coverage_by_family, basis, note
    ):
        self.place = place
        self.when = when
        self.taken = list(taken)
        self.left_out = list(left_out)
        self.coverage = coverage
        self.coverage_by_family = coverage_by_family
        self.basis = basis
        self.note = note

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
        if self.basis == "departures":
            taken = [f"{c.feed.feed_id} ({c.reason})" for c in self.taken]
            lines = [": ".join([head, self.note] + taken)]
        else:
            count = len(self.taken)
            lines = [
                f"{head}: take {count} feed{'' if count == 1 else 's'}, covering "
                f"about {_percent(self.coverage)} of the departures the index "
                "records there, each counted once"
            ]
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


def recommend(target, feeds, when, *, goal=TARGET, max_feeds=MAX_FEEDS):
    """The :class:`Recommendation` for ``target``, a place or an area, from
    its ``feeds`` on ``when`` (today when None); see
    :meth:`~transitio.index.Place.recommend`."""
    if not 0 < goal <= 1:
        raise ValueError(f"target must be above 0 and at most 1, not {goal!r}")
    if (
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
        reason = _excluded(feed, when, day, blocks)
        if reason is None:
            candidates.append(feed)
        else:
            left_out.append(Choice(feed, _with_access(feed, reason), None))
    blocks = {f.feed_id: blocks[f.feed_id] for f in candidates if f.feed_id in blocks}
    if not blocks:
        taken, rest, note = _by_departures(candidates)
        left_out = _largest_first(rest) + _largest_first(left_out)
        return Recommendation(
            target, when, taken, left_out, None, None, "departures", note
        )
    universe = _universe(blocks)
    total = sum(universe.values())
    by_id = {feed.feed_id: feed for feed in candidates}
    taken, capped = _greedy(blocks, universe, by_id, goal, max_feeds)
    by_family = _covered(blocks, taken, universe)
    coverage = sum(by_family[f] * u for f, u in universe.items()) / total
    reached = _reached(by_family, coverage, universe, goal)
    note = None
    if not reached:
        short = [f for f in _families(universe) if by_family[f] < FAMILY_TARGET]
        missing = (
            f"{_percent(goal)} of them"
            if coverage < goal
            else f"{_percent(FAMILY_TARGET)} of "
            + " and of ".join(_FAMILY_WORDS.get(f, f) for f in short)
        )
        why = (
            f"at most {max_feeds} feed{' is' if max_feeds == 1 else 's are'} taken"
            if capped
            else f"no other feed adds {_percent(MIN_GAIN)} of the departures"
        )
        note = (
            f"the feeds taken cover {_percent(coverage)} of the departures, "
            f"short of {missing}: {why}"
        )
    uncompared = [f for f in candidates if f.feed_id not in blocks]
    if uncompared:
        count = len(uncompared)
        departures = sum(
            _summed_service(f)["departures_per_day"] or 0.0 for f in uncompared
        )
        left = (
            f"the coverage leaves out {count} feed{'' if count == 1 else 's'} the "
            f"index records no overlap for ({departures:,.0f} departures a day)"
        )
        note = left if note is None else f"{note}; {left}"
    chosen = [
        _taken_choice(by_id, blocks, taken, n, universe) for n in range(len(taken))
    ]
    stops = [by_id[feed_id].stop_count for feed_id in taken]
    stops = None if None in stops else sum(stops)
    rest = []
    for feed in candidates:
        if feed.feed_id in taken:
            continue
        block = blocks.get(feed.feed_id)
        reason, adds = _kept(feed), None
        if block is not None:
            adds = sum(_unrun(block, taken).values()) / total
            reason = reason or _repeats(feed, block, taken, goal, stops)
        if reason is None and block is None:
            reason = "not compared: the index records no overlap for it"
        elif reason is None and adds < MIN_GAIN:
            reason = f"adds too little: {_percent(adds)} of the place's departures"
        elif reason is None:
            reason = f"adds {_percent(adds)} of the place's departures"
            if reached:
                reason += "; the feeds taken reach the target without it"
            elif capped:
                reason += f"; at most {max_feeds} feeds are taken"
        rest.append(Choice(feed, _with_access(feed, reason), adds))
    left_out = _largest_first(rest) + _largest_first(left_out)
    return Recommendation(
        target, when, chosen, left_out, coverage, by_family, "overlap", note
    )


def _excluded(feed, when, day, blocks):
    """Why ``feed`` is no candidate on ``day``, or None."""
    ended = feed.stale_when_indexed
    if ended is not None:
        return f"stale when indexed: its timetable ended {ended}"
    start, end = feed.service_start, feed.service_end
    if end is not None and end < day:
        return (
            f"its timetable as indexed ends {end}, before {day} "
            "(a newer download may run then)"
        )
    if when is not None and start is not None and start > when:
        return f"its timetable as indexed starts {start}, after {when}"
    provider = feed._provider()
    if feed.access == "key" and provider is not None and provider.free is False:
        return _access_note(feed)
    departures = _summed_service(feed)["departures_per_day"]
    if feed.feed_id not in blocks and not departures:
        return "not measured: the index has no timetable for it here"
    return None


def _size_key(feed, gain):
    """Sorts the feeds adding about as much: open before key, fewer stops
    (unknown counting as most), more added, then the id."""
    stops = math.inf if feed.stop_count is None else feed.stop_count
    return (feed.access == "key", stops, -gain, feed.feed_id)


def _greedy(blocks, universe, by_id, goal, max_feeds):
    """``(taken, capped)``: the ids of the feeds taken, in order, and whether
    the selection stopped at ``max_feeds`` short of the target."""
    total = sum(universe.values())
    taken = []
    capped = False
    while len(taken) < len(blocks):
        if len(taken) == max_feeds:
            capped = True
            break
        gains = {
            feed_id: sum(_unrun(block, taken).values())
            for feed_id, block in blocks.items()
            if feed_id not in taken
        }
        best = max(gains.values())
        if best < MIN_GAIN * total:
            break
        near = [i for i, gain in gains.items() if gain >= best - SIZE_TOLERANCE * total]
        taken.append(min(_size_key(by_id[i], gains[i]) for i in near)[-1])
        by_family = _covered(blocks, taken, universe)
        coverage = sum(by_family[f] * u for f, u in universe.items()) / total
        if _reached(by_family, coverage, universe, goal):
            break
    for feed_id in reversed(list(taken)):
        others = [i for i in taken if i != feed_id]
        if sum(_unrun(blocks[feed_id], others).values()) < MIN_GAIN * total:
            taken.remove(feed_id)
    return taken, capped


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


def _taken_choice(by_id, blocks, taken, n, universe):
    """The :class:`Choice` of the ``n``-th taken feed."""
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
        if added[family] >= gain / 2:
            reason = (
                f"adds {words} the feeds above lack: "
                f"{_percent(added[family] / universe[family])} of the place's {words}"
            )
        else:
            reason = f"adds {_percent(adds)} of the place's departures"
    return Choice(by_id[feed_id], _with_access(by_id[feed_id], reason), adds)


def _kept(feed):
    """The feeds containing or carrying ``feed``, in words; None without
    any."""
    if feed.contained_in:
        return f"contained in {', '.join(feed.contained_in)}"
    carriers = set()
    for edge in feed.edges.values():
        carriers.update((edge.evidence or {}).get("carried_by") or {})
    if carriers:
        return f"carried by {', '.join(sorted(carriers))}"
    return None


def _repeats(feed, block, taken, goal, stops):
    """ "repeats X (N % of its departures)" when the taken feeds run at least
    ``goal`` of the feed's departures, naming the one running the most and,
    when it runs ``_SECOND_FEED`` more of them, a second; None otherwise."""
    total = sum(block[0].values())

    def run(others):
        return 1 - sum(_unrun(block, others).values()) / total

    if not taken or run(taken) < goal:
        return None
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


def _by_departures(candidates):
    """``(taken, left_out, note)`` on an index without overlap evidence."""
    departures = {
        f.feed_id: _summed_service(f)["departures_per_day"] or 0.0 for f in candidates
    }
    if not candidates:
        return [], [], "no feed is left to take"
    top = max(departures.values())
    near = [
        f for f in candidates if departures[f.feed_id] >= (1 - SIZE_TOLERANCE) * top
    ]
    pick = min(_size_key(f, departures[f.feed_id]) for f in near)[-1]
    summed = sum(departures.values())
    taken, rest = [], []
    for feed in candidates:
        share = departures[feed.feed_id] / summed
        if feed.feed_id == pick:
            reason = f"{departures[pick]:,.0f} departures a day as indexed"
            taken.append(Choice(feed, _with_access(feed, reason), None))
            continue
        reason = _kept(feed)
        if reason is None and share < MIN_GAIN:
            reason = (
                f"adds too little: {_percent(share)} of the departures summed "
                "over the place's feeds"
            )
        reason = reason or "not compared: the index records no overlap for it"
        rest.append(Choice(feed, _with_access(feed, reason), None))
    note = (
        "this index records no overlap between feeds; taking the feed with the "
        f"most departures, or a smaller one within {_percent(SIZE_TOLERANCE)} of it"
    )
    return taken, rest, note


def _largest_first(choices):
    """``choices`` by their feeds' departures in the place, the most first,
    then by id."""

    def key(choice):
        departures = _summed_service(choice.feed)["departures_per_day"]
        return (-(departures or 0.0), choice.feed.feed_id)

    return sorted(choices, key=key)


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
