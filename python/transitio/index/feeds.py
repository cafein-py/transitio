"""The feed-membership read API: which feeds serve a place, and how.

:meth:`Place.feeds` queries the index's membership edges for one place and
returns :class:`IndexedFeed` objects — a feed joined with its matched edges.
``edges`` is the authoritative per-tier record; the singular fields are
aggregates over *the tiers the query matched*: ``confidence`` is the minimum
across them (a feed is only as trustworthy as its weakest qualifying edge),
``needs_review`` the *or*, and ``selector`` the union — always a
:class:`Selector` object, never ``None``, with ``unavailable`` dominating,
because a union that silently omitted the unfilterable part would be exactly
the wrong answer.

An edge is *unknown* only when its tier is ``"unknown"``; ``on_unknown``
governs those (``"include"``, the default, keeps them flagged), while
``min_confidence`` separately filters classified-but-unconfident edges.
"""

import json
import math

__all__ = ["IndexedFeed", "Selector", "TierEdge"]


def _parse(value):
    """A JSON-string column value as Python, passing dicts/None through."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, str):
        return json.loads(value)
    return value


def _scalar(value):
    return None if isinstance(value, float) and math.isnan(value) else value


class Selector:
    """Which routes of a feed a query's tiers select.

    ``state`` is ``"whole_feed"`` (every route qualifies, no filtering),
    ``"complete"`` (filter to ``route_ids``) or ``"unavailable"`` (no safe
    filtering possible).
    """

    def __init__(self, state, route_ids=(), declared_as=None):
        self.state = state
        self.route_ids = tuple(route_ids)
        self.declared_as = declared_as

    def __repr__(self):
        if self.state == "complete":
            return f"Selector(state='complete', route_ids={self.route_ids!r})"
        return f"Selector(state={self.state!r})"


class TierEdge:
    """One membership edge, as the query matched it."""

    def __init__(self, record):
        self.tier = record["tier"]
        self.confidence = float(record["confidence"])
        self.tier_confidence = float(record["tier_confidence"])
        self.method = record["method"]
        self.needs_review = bool(record["needs_review"])
        self.selector_state = record["selector_state"]
        self.selector = _parse(record.get("selector"))
        self.evidence = _parse(record.get("evidence"))

    def __repr__(self):
        return (
            f"TierEdge({self.tier!r}, confidence={self.confidence}, "
            f"needs_review={self.needs_review}, method={self.method!r})"
        )


class IndexedFeed:
    """A feed serving a place: its identity row plus the matched tier edges."""

    def __init__(self, row, edges):
        self._row = row
        self.edges = edges

    @property
    def feed_id(self):
        return self._row["feed_id"]

    @property
    def onestop_id(self):
        return _scalar(self._row.get("onestop_id"))

    @property
    def name(self):
        return _scalar(self._row.get("name"))

    @property
    def spec(self):
        return self._row.get("spec")

    @property
    def coverage_source(self):
        return _scalar(self._row.get("coverage_source"))

    @property
    def snapshot(self):
        """The snapshot id this feed row was published under."""
        return _scalar(self._row.get("snapshot"))

    @property
    def license(self):
        """The feed's verbatim licence block, from its Atlas record, or None."""
        atlas = _parse(self._row.get("atlas"))
        return (atlas or {}).get("license")

    @property
    def tiers(self):
        return frozenset(self.edges)

    @property
    def confidence(self):
        """The minimum membership confidence across the matched tiers."""
        return min(edge.confidence for edge in self.edges.values())

    @property
    def needs_review(self):
        return any(edge.needs_review for edge in self.edges.values())

    @property
    def selector(self):
        """The union of the matched edges' selectors; the weakest link decides.

        Fail-safe: an unknown selector state, or a ``complete`` edge carrying no
        route ids, counts as ``unavailable`` — a trusted empty selector would let
        downstream filtering silently drop routes.
        """
        states = {edge.selector_state for edge in self.edges.values()}
        if states - {"whole_feed", "complete"}:
            return Selector("unavailable")
        if "whole_feed" in states:
            # A whole-feed claim absorbs any route subset it is unioned with.
            return Selector("whole_feed")
        route_ids = set()
        declared = []
        for edge in self.edges.values():
            selector = edge.selector or {}
            if not selector.get("route_id"):
                return Selector("unavailable")
            route_ids.update(selector["route_id"])
            if selector.get("declared_as") is not None:
                declared.append(selector["declared_as"])
        # A single curator predicate stays visible; a union of several has no
        # one predicate to show.
        declared_as = declared[0] if len(declared) == 1 else None
        return Selector("complete", sorted(route_ids), declared_as)

    def __repr__(self):
        return (
            f"IndexedFeed({self.feed_id!r}, tiers={sorted(self.tiers)}, "
            f"confidence={self.confidence})"
        )


class FeedList(list):
    """The feeds matching one query, with a tabular export."""

    def to_geodataframe(self):
        """The feeds as a GeoDataFrame, one row per feed.

        The geometry column is empty for now: per-feed coverage geometry (the
        crawled stop hull) is not part of the index yet.
        """
        import geopandas

        columns = (
            "feed_id",
            "onestop_id",
            "name",
            "spec",
            "coverage_source",
            "tiers",
            "confidence",
            "needs_review",
            "selector_state",
        )
        rows = [
            {
                "feed_id": feed.feed_id,
                "onestop_id": feed.onestop_id,
                "name": feed.name,
                "spec": feed.spec,
                "coverage_source": feed.coverage_source,
                "tiers": sorted(feed.tiers),
                "confidence": feed.confidence,
                "needs_review": feed.needs_review,
                "selector_state": feed.selector.state,
            }
            for feed in self
        ]
        # Built column-wise so an empty result keeps the documented columns.
        data = {column: [row[column] for row in rows] for column in columns}
        return geopandas.GeoDataFrame(
            data, geometry=[None] * len(rows), crs="EPSG:4326"
        )


def _matched(edges, tiers, exclude, on_unknown, min_confidence):
    """The edges of one feed the query matches, keyed by tier."""
    matched = {}
    for edge in edges:
        if edge["tier"] == "unknown":
            if on_unknown != "include":
                continue
        elif tiers is not None and edge["tier"] not in tiers:
            continue
        if exclude and edge["tier"] in exclude:
            continue
        if min_confidence is not None and edge["confidence"] < min_confidence:
            continue
        matched.setdefault(edge["tier"], TierEdge(edge))
    return matched


def feeds_for_place(
    index,
    place,
    *,
    tiers=None,
    exclude=None,
    spec="gtfs",
    on_unknown="include",
    min_confidence=None,
):
    """The :class:`IndexedFeed` list for ``place``, filtered by the query.

    A feed is returned when its spec is selected — ``spec="gtfs"`` by default,
    ``spec=None`` for everything, a list to narrow — and at least one of its
    edges to the place survives the query; a feed whose every edge is excluded
    (or unknown under ``on_unknown="exclude"``) is dropped. Feeds come back
    sorted by id.
    """
    if on_unknown not in ("include", "exclude"):
        raise ValueError("on_unknown must be 'include' or 'exclude'")
    allowed = None if spec is None else {spec} if isinstance(spec, str) else set(spec)
    if index.edges is None:
        return FeedList()
    place_edges = index.edges[index.edges["place_id"] == place.id]
    by_feed = {}
    for edge in place_edges.to_dict("records"):
        by_feed.setdefault(edge["feed_id"], []).append(edge)
    rows = {row["feed_id"]: row for row in index.feeds.to_dict("records")}
    found = FeedList()
    for feed_id in sorted(by_feed):
        row = rows.get(feed_id)
        if row is None:
            continue
        if allowed is not None and row.get("spec") not in allowed:
            continue
        matched = _matched(by_feed[feed_id], tiers, exclude, on_unknown, min_confidence)
        if matched:
            found.append(IndexedFeed(row, matched))
    return found
