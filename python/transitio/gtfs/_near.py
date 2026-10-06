"""Pairing the trips of a merge input that nearly repeat earlier inputs'."""

from __future__ import annotations

import itertools

import numpy as np
import pandas as pd

from transitio.shapes._stitch import _meters

#: Aligned stops lie at most this many metres apart.
NEAR_METRES = 50
#: Aligned stops' times lie at most this many seconds apart.
NEAR_SECONDS = 180
#: The mean absolute time difference over aligned stops is at most this
#: many seconds.
NEAR_MEAN_SECONDS = 60
#: A later trip of ``n`` stops leaves at most ``min(NEAR_UNALIGNED, n // 5)``
#: of them unaligned.
NEAR_UNALIGNED = 2
# Metres per degree, as _meters counts them.
_DEGREE = 111_320.0


def near_matches(earlier, later, keep=None):
    """The ``later`` trips that nearly repeat ``earlier`` trips.

    Both are the stop rows :func:`~transitio.gtfs._schedule.trip_signatures`
    returns ``with_stops``. A later trip of ``n`` stops nearly repeats an
    earlier trip when their routes' name and mode are equal, as are their
    frequencies.txt rows (a timetabled trip never nearly repeats a
    frequency-based one); its stops align in order with the earlier trip's,
    each aligned pair within :data:`NEAR_METRES` metres (equirectangular)
    and :data:`NEAR_SECONDS` seconds, at least two aligning and at most
    ``min(NEAR_UNALIGNED, n // 5)`` left unaligned; the mean absolute time
    difference at aligned stops is at most :data:`NEAR_MEAN_SECONDS`
    seconds; pickup and drop-off types agree at aligned stops, but for
    drop-off at the first aligned stop and pickup at the last; and
    ``wheelchair_accessible`` and ``bikes_allowed`` agree where both are 1
    or 2. Each stop aligns with the first earlier stop past the one aligned
    before it that is within reach, so an alignment may be missed, but none
    breaking these rules is accepted. Trips with a stop lacking coordinates
    or times, or with continuous stopping, are not compared.

    Only pairs where one of the later trip's first ``NEAR_UNALIGNED + 1``
    stops is within reach of an earlier trip's stop are compared; ``keep``,
    given those pairs' ``later`` and ``earlier`` trip ids, returns which to
    compare.

    Returns ``(pairs, stops)``: a ``later``, ``earlier``, ``unaligned`` row
    per pair, and a ``later``, ``earlier``, ``position``, ``later_stop``,
    ``earlier_stop`` row per stop of each pair's later trip, ``position``
    its position in the trip and ``earlier_stop`` the stop aligned with it
    (NaN when unaligned).
    """
    earlier, later = _comparable(earlier), _comparable(later)
    pairs = _candidates(earlier, later)
    if keep is not None and len(pairs):
        pairs = pairs[np.asarray(keep(pairs), dtype=bool)].reset_index(drop=True)
    return _aligned(earlier, later, pairs)


def _comparable(stops):
    """``stops`` without the trips that are not compared."""
    codes = pd.factorize(stops["trip_id"])[0]
    lacking = stops[["lon", "lat", "seconds"]].isna().any(axis=1) | stops["continuous"]
    excluded = np.zeros(len(stops), dtype=bool)
    excluded[codes[lacking.to_numpy()]] = True
    return stops[~excluded[codes]].reset_index(drop=True)


def _reach(later, rows, earlier, others):
    """Whether each of the ``later`` stop times ``rows`` is within reach of
    the ``earlier`` one of ``others`` beside it, and their absolute time
    differences."""
    metres = _meters(
        (later["lon"].to_numpy()[rows], later["lat"].to_numpy()[rows]),
        (earlier["lon"].to_numpy()[others], earlier["lat"].to_numpy()[others]),
    )
    gaps = np.abs(
        later["seconds"].to_numpy()[rows] - earlier["seconds"].to_numpy()[others]
    )
    return (metres <= NEAR_METRES) & (gaps <= NEAR_SECONDS), gaps


def _candidates(earlier, later):
    """The ``later``, ``earlier`` trip pairs where one of the later trip's
    first stops is within reach of an earlier trip's, found through grid
    cells :data:`NEAR_METRES` square and :data:`NEAR_SECONDS` long."""
    anchors = later[later["position"] <= NEAR_UNALIGNED]
    # Cells span NEAR_METRES at the highest latitude, so stops within reach
    # of each other lie in the same or neighbouring cells; near a pole, a
    # cell spans 360 degrees of longitude.
    top = np.abs(np.r_[earlier["lat"], anchors["lat"]]).max(initial=0.0)
    height = NEAR_METRES / _DEGREE
    width = height / max(np.cos(np.radians(top)), height / 360)

    def cells(stops):
        return pd.DataFrame(
            {
                "x": np.floor(stops["lon"].to_numpy() / width).astype("int64"),
                "y": np.floor(stops["lat"].to_numpy() / height).astype("int64"),
                "t": np.floor(stops["seconds"].to_numpy() / NEAR_SECONDS).astype(
                    "int64"
                ),
                "route": stops["route"].to_numpy(),
                "runs": stops["runs"].to_numpy(),
            }
        )

    grid = cells(earlier).assign(other=np.arange(len(earlier)))
    reach = cells(anchors).assign(row=np.arange(len(anchors)))
    shifted = pd.concat(
        [
            reach.assign(x=reach["x"] + dx, y=reach["y"] + dy, t=reach["t"] + dt)
            for dx, dy, dt in itertools.product((-1, 0, 1), repeat=3)
        ],
        ignore_index=True,
    )
    hits = shifted.merge(grid, on=["x", "y", "t", "route", "runs"])
    rows, others = hits["row"].to_numpy(), hits["other"].to_numpy()
    close, _ = _reach(anchors, rows, earlier, others)
    pairs = pd.DataFrame(
        {
            "later": anchors["trip_id"].to_numpy()[rows[close]],
            "earlier": earlier["trip_id"].to_numpy()[others[close]],
        }
    )
    return pairs.drop_duplicates(ignore_index=True)


def _spans(starts, counts):
    """``counts[k]`` indices from ``starts[k]`` on, for each ``k`` in turn."""
    return np.repeat(starts - np.cumsum(counts) + counts, counts) + np.arange(
        counts.sum()
    )


def _firsts(stops, trip_ids):
    """The first row in ``stops`` of each of ``trip_ids``, and its number of
    stops."""
    starts = np.flatnonzero(stops["position"].to_numpy() == 0)
    sizes = np.diff(np.r_[starts, len(stops)])
    at = pd.Index(stops["trip_id"].to_numpy()[starts]).get_indexer(trip_ids)
    return starts[at], sizes[at]


def _aligned(earlier, later, pairs):
    """The ``pairs`` whose stops align, as :func:`near_matches` returns
    them."""
    if pairs.empty:
        columns = ["later", "earlier", "position", "later_stop", "earlier_stop"]
        return pairs.assign(unaligned=0), pd.DataFrame(columns=columns)
    mine, n = _firsts(later, pairs["later"])
    theirs, _ = _firsts(earlier, pairs["earlier"])
    # Each pair's later stop times, then the earlier trip's stop times
    # within NEAR_SECONDS of each, by keys ordered by trip (its first row)
    # and time.
    pair = np.repeat(np.arange(len(pairs)), n)
    rows = _spans(mine, n)
    seconds = [
        stops["seconds"].to_numpy().astype("int64") for stops in (later, earlier)
    ]
    low = min(times.min() for times in seconds) - NEAR_SECONDS
    span = max(times.max() for times in seconds) - low + NEAR_SECONDS + 1
    places = earlier["position"].to_numpy()
    trip = np.maximum.accumulate(np.where(places == 0, np.arange(len(earlier)), 0))
    keys = trip * span + seconds[1] - low
    order = np.argsort(keys, kind="stable")
    keys = keys[order]
    wanted = theirs[pair] * span + seconds[0][rows] - low
    first = np.searchsorted(keys, wanted - NEAR_SECONDS)
    found = np.searchsorted(keys, wanted + NEAR_SECONDS, side="right") - first
    # An edge joins a later stop time (an index into rows) and an earlier
    # one within reach.
    edges = np.repeat(np.arange(len(rows)), found)
    others = order[_spans(first, found)]
    close, gaps = _reach(later, rows[edges], earlier, others)
    edges, others, gaps = edges[close], others[close], gaps[close]
    j, i = later["position"].to_numpy()[rows[edges]], places[others]
    order = np.lexsort((i, j, pair[edges]))
    edges, others, gaps, j, i = (a[order] for a in (edges, others, gaps, j, i))

    # Stop by stop along the later trips, each pair at once.
    width, depth = n.max(), i.max() + 2
    keys = (pair[edges] * width + j) * depth + i
    limit = np.minimum(NEAR_UNALIGNED, n // 5)
    after = np.full(len(pairs), -1)  # the earlier position aligned last
    opening, closing = np.full(len(pairs), -1), np.full(len(pairs), -1)
    aligned = np.zeros(len(pairs), dtype="int64")
    total = np.zeros(len(pairs))
    chosen = []
    for position in range(width):
        active = np.flatnonzero((n > position) & (position - aligned <= limit))
        group = (active * width + position) * depth
        at = np.searchsorted(keys, group + after[active] + 1)
        at = np.minimum(at, len(keys) - 1)
        hit = (keys[at] > group + after[active]) & (keys[at] < group + depth)
        active, at = active[hit], at[hit]
        opening[active] = np.where(aligned[active] == 0, position, opening[active])
        closing[active] = position
        after[active] = i[at]
        aligned[active] += 1
        total[active] += gaps[at]
        chosen.append(at)
    chosen = np.concatenate(chosen)
    which, where = pair[edges[chosen]], j[chosen]
    later_rows, earlier_rows = rows[edges[chosen]], others[chosen]

    def differs(column):
        values = later[column].to_numpy()[later_rows]
        return values != earlier[column].to_numpy()[earlier_rows]

    wrong = differs("pickup") & (where != closing[which])
    wrong |= differs("drop_off") & (where != opening[which])
    accepted = np.bincount(which[wrong], minlength=len(pairs)) == 0
    for column in ("wheelchair", "bikes"):
        ours = later[column].to_numpy()[mine]
        other = earlier[column].to_numpy()[theirs]
        known = np.isin(ours, ("1", "2")) & np.isin(other, ("1", "2"))
        accepted &= ~known | (ours == other)
    unaligned = n - aligned
    accepted &= (unaligned <= limit) & (aligned >= 2)
    accepted &= total <= NEAR_MEAN_SECONDS * aligned

    matched = np.full(len(rows), np.nan, dtype=object)
    matched[edges[chosen]] = earlier["stop_id"].to_numpy()[earlier_rows]
    stops = pd.DataFrame(
        {
            "later": pairs["later"].to_numpy()[pair],
            "earlier": pairs["earlier"].to_numpy()[pair],
            "position": later["position"].to_numpy()[rows],
            "later_stop": later["stop_id"].to_numpy()[rows],
            "earlier_stop": matched,
        }
    )
    pairs = pairs[accepted].assign(unaligned=unaligned[accepted])
    return pairs.reset_index(drop=True), stops[accepted[pair]].reset_index(drop=True)
