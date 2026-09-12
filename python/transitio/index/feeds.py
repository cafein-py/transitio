"""The feed-membership read API: which feeds serve a place, and how.

:meth:`Place.feeds` queries the index's membership edges for one place and
returns :class:`IndexedFeed` objects — a feed joined with its matched edges.
``edges`` is the authoritative per-tier record; the singular fields are
aggregates over *the tiers the query matched*: ``needs_review`` is the *or*,
``selector`` the union — always a :class:`Selector` object, never ``None``,
with ``unavailable`` dominating, because a union that silently omitted the
unfilterable part would be exactly the wrong answer — and ``service`` the
feed's service level in the place (stops, routes, departures per day), which
every tier edge of the pair carries identically.

Membership is a fact, not a score: a feed is in the index for a place because
a scheduled stop lies there. An edge is *unknown* only when its tier is
``"unknown"``; ``on_unknown`` governs those (``"include"``, the default, keeps
them flagged in a tier query; a schema-7 place's default view lists its own
categories and shows unknown edges only when every category is asked for),
and ``needs_review`` marks the tiers a person should check.
"""

import json
import math

__all__ = ["IndexedFeed", "PlaceService", "Selector", "ServiceLevel", "TierEdge"]

# The files that define a fare: GTFS-Fares v1 (attributes/rules) and the v2
# fare products and leg/transfer/join rules. Companion tables are deliberately
# absent — the v2 zone and network tables (areas, stop_areas, networks,
# route_networks) and the fare_media, rider_categories and timeframes tables —
# since each only supports a product or rule and can ship without one, so
# counting them would call a feed with no priceable fare fare-bearing.
FARE_FILES = frozenset(
    {
        "fare_attributes.txt",
        "fare_rules.txt",
        "fare_products.txt",
        "fare_leg_rules.txt",
        "fare_leg_join_rules.txt",
        "fare_transfer_rules.txt",
    }
)


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


class ServiceLevel:
    """How much service a feed (or every feed together) offers in a place.

    ``stops`` are distinct scheduled stops inside the place, ``routes`` the
    routes with a scheduled stop there, and ``departures_per_day`` the
    scheduled stop-events at those stops per average calendar day — ``None``
    when nothing could be measured: the feed's timetable was not read (a
    feed that legitimately skipped ``stop_times``, or a declared-only
    placement), or it carried no usable calendar to weight the events by.
    """

    def __init__(self, record):
        record = record or {}
        self.stops = _count(record.get("stops"))
        self.routes = _count(record.get("routes"))
        departures = record.get("departures_per_day")
        self.departures_per_day = None if departures is None else float(departures)

    def __repr__(self):
        return (
            f"ServiceLevel(stops={self.stops}, routes={self.routes}, "
            f"departures_per_day={self.departures_per_day})"
        )


def _count(value):
    return None if value is None else int(value)


class PlaceService(ServiceLevel):
    """A place's service level summed over the feeds serving it."""

    def __init__(self, record):
        super().__init__(record)
        self.feeds = _count((record or {}).get("feeds")) or 0

    def __repr__(self):
        return (
            f"PlaceService(feeds={self.feeds}, stops={self.stops}, "
            f"routes={self.routes}, departures_per_day={self.departures_per_day})"
        )


# The relevance categories in the order a place's view lists them, and the
# categories each place kind shows by default: a city its primary and
# secondary feeds, a region its secondary and tertiary, a country its tertiary.
CATEGORY_ORDER = ("primary", "secondary", "tertiary", "international", "unknown")
DEFAULT_CATEGORIES = {
    "city": ("primary", "secondary"),
    "metro": ("primary", "secondary"),
    "region": ("secondary", "tertiary"),
    "country": ("tertiary",),
}


def _relevance(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


class TierEdge:
    """One membership edge, as the query matched it."""

    def __init__(self, record):
        self.tier = record["tier"]
        self.tier_confidence = float(record["tier_confidence"])
        self.method = record["method"]
        self.needs_review = bool(record["needs_review"])
        self.selector_state = record["selector_state"]
        self.classification_fingerprint = record.get("classification_fingerprint")
        self.fingerprint_kind = record.get("fingerprint_kind")
        self.selector = _parse(record.get("selector"))
        self.evidence = _parse(record.get("evidence"))
        self.service = ServiceLevel(_parse(record.get("service")))
        # Schema 7: the rank stage's relevance; None on an older index.
        self.relevance_category = _scalar(record.get("relevance_category"))
        self.relevance = _relevance(record.get("relevance"))
        cross = record.get("cross_border")
        self.cross_border = None if cross is None or cross != cross else bool(cross)

    def __repr__(self):
        return (
            f"TierEdge({self.tier!r}, tier_confidence={self.tier_confidence}, "
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
    def provenance(self):
        """What a reproduction must match to be exact: the snapshot id, the
        reader's discovery semantics version and the transitio version."""
        from transitio import __version__
        from transitio.index import DISCOVERY_SEMANTICS_VERSION

        return {
            "snapshot": self.snapshot,
            "discovery_semantics_version": DISCOVERY_SEMANTICS_VERSION,
            "transitio_version": __version__,
        }

    @property
    def coverage(self):
        """The feed's published coverage geometry as WKB — the crawled stop
        hull, or the declared coverage — or None without one."""
        return _scalar(self._row.get("coverage"))

    @property
    def stop_count(self):
        value = _scalar(self._row.get("stop_count"))
        return None if value is None else int(value)

    @property
    def crawl_status(self):
        return _scalar(self._row.get("crawl_status"))

    @property
    def last_crawled(self):
        return _scalar(self._row.get("last_crawled"))

    @property
    def redistribution_allowed(self):
        """Whether the feed's licence permits redistributing data derived
        from it, as the build judged it; None when unknown."""
        value = _scalar(self._row.get("redistribution_allowed"))
        return None if value is None else bool(value)

    @property
    def license(self):
        """The feed's verbatim licence block, from its Atlas record, or None."""
        atlas = _parse(self._row.get("atlas"))
        return (atlas or {}).get("license")

    @property
    def files(self):
        """The GTFS files the feed's archive carries, as a frozenset of root
        file names — a capability hint recorded by the crawl (schema 5), empty
        for a snapshot that predates it or a feed the crawl never read."""
        value = self._row.get("files")
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return frozenset()
        return frozenset(str(name) for name in value)

    @property
    def has_shapes(self):
        """Whether the feed publishes route geometry (``shapes.txt``)."""
        return "shapes.txt" in self.files

    @property
    def has_fares(self):
        """Whether the feed defines a fare: a GTFS-Fares v1 attributes/rules
        file or a v2 product or rule file (see :data:`FARE_FILES`). Companion
        tables alone — zones, networks, media, rider categories, timeframes —
        do not count."""
        return not self.files.isdisjoint(FARE_FILES)

    @property
    def tiers(self):
        return frozenset(self.edges)

    @property
    def relevance_category(self):
        """The strongest relevance category among the matched edges, or None
        on an index without relevance."""
        found = [e.relevance_category for e in self.edges.values()]
        found = [c for c in found if c in CATEGORY_ORDER]
        return min(found, key=CATEGORY_ORDER.index) if found else None

    @property
    def relevance(self):
        """The pair's relevance score (identical on every edge of the pair),
        or None on an index without relevance."""
        scores = [e.relevance for e in self.edges.values() if e.relevance is not None]
        return max(scores) if scores else None

    @property
    def cross_border(self):
        return any(e.cross_border for e in self.edges.values())

    @property
    def service(self):
        """The feed's service level in the place — identical on every tier
        edge of the pair, so any matched edge's copy is the answer."""
        return next(iter(self.edges.values())).service

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
        return f"IndexedFeed({self.feed_id!r}, tiers={sorted(self.tiers)})"


class FeedList(list):
    """The feeds matching one query, with a tabular export."""

    def to_geodataframe(self):
        """The feeds as a GeoDataFrame, one row per feed.

        The geometry is each feed's published coverage (the crawled stop
        hull, or the declared coverage); a feed without one has none.
        """
        import geopandas
        import shapely

        columns = (
            "feed_id",
            "onestop_id",
            "name",
            "spec",
            "coverage_source",
            "tiers",
            "stops",
            "routes",
            "departures_per_day",
            "needs_review",
            "selector_state",
            "files",
        )
        rows = [
            {
                "feed_id": feed.feed_id,
                "onestop_id": feed.onestop_id,
                "name": feed.name,
                "spec": feed.spec,
                "coverage_source": feed.coverage_source,
                "tiers": sorted(feed.tiers),
                "stops": feed.service.stops,
                "routes": feed.service.routes,
                "departures_per_day": feed.service.departures_per_day,
                "needs_review": feed.needs_review,
                "selector_state": feed.selector.state,
                "files": sorted(feed.files),
            }
            for feed in self
        ]
        # Built column-wise so an empty result keeps the documented columns.
        data = {column: [row[column] for row in rows] for column in columns}
        hulls = [
            None if feed.coverage is None else shapely.from_wkb(feed.coverage)
            for feed in self
        ]
        return geopandas.GeoDataFrame(data, geometry=hulls, crs="EPSG:4326")


def _matched(edges, tiers, exclude, on_unknown, categories=None):
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
        if categories is not None and edge.get("relevance_category") not in categories:
            continue
        matched.setdefault(edge["tier"], TierEdge(edge))
    return matched


def _default_categories(place, tiers, categories, international):
    """The relevance categories a query keeps: the ones asked for, else the
    place kind's default view — with ``international`` added when the
    cross-border feeds were asked for — unless tiers were named (a tier
    query is answered in tiers)."""
    if categories != "default":
        return None if categories is None else frozenset(categories)
    if tiers is not None:
        return None
    default = DEFAULT_CATEGORIES.get(place.kind)
    if default is None:
        return None
    return frozenset(default) | ({"international"} if international else set())


def _link_edges(index, place):
    """The cross-border edges to ``place`` a country load does not carry in
    its edges, with the rows of the feeds they name."""
    if index.links is None or index.country is None:
        return [], {}
    links = index.links[index.links["place_id"] == place.id]
    records = links.to_dict("records")
    rows = {}
    for partition in sorted({r["feed_partition"] for r in records}):
        for row in index.feeds_in(partition).to_dict("records"):
            rows[row["feed_id"]] = row
    return records, rows


def _view_key(feed):
    """Sort key of a place's view: category order, then relevance high to
    low, then the feed id."""
    category = feed.relevance_category
    rank = CATEGORY_ORDER.index(category) if category in CATEGORY_ORDER else 99
    return (rank, -(feed.relevance or 0.0), feed.feed_id)


def feeds_for_place(
    index,
    place,
    *,
    tiers=None,
    exclude=None,
    spec="gtfs",
    on_unknown="include",
    requires=None,
    categories="default",
    international=False,
):
    """The :class:`IndexedFeed` list for ``place``, filtered by the query.

    A feed is returned when its spec is selected — ``spec="gtfs"`` by default,
    ``spec=None`` for everything, a list to narrow — and at least one of its
    edges to the place survives the query; a feed whose every edge is excluded
    (or unknown under ``on_unknown="exclude"``) is dropped. ``requires`` names
    GTFS files the feed's manifest must carry (``"shapes.txt"``, or several);
    a feed whose recorded manifest lacks one — including a feed from a snapshot
    that predates the manifest, whose manifest is empty — is dropped, the
    fail-closed reading of "must have this capability".

    On a schema-7 index the place's default view applies: a city keeps its
    ``primary`` and ``secondary`` feeds, a region ``secondary`` and
    ``tertiary``, a country ``tertiary`` (``categories`` names other
    categories, ``None`` keeps every category; a ``tiers`` query is answered
    in tiers instead), cross-border edges are left out unless
    ``international=True`` adds them — for a country load, from the links
    table and the partitions holding their feeds — and the feeds come back
    by category, then relevance high to low, then id. An older index has no
    relevance: every feed is listed, sorted by id.
    """
    if on_unknown not in ("include", "exclude"):
        raise ValueError("on_unknown must be 'include' or 'exclude'")
    if requires is None:
        needed = frozenset()
    else:
        needed = frozenset([requires] if isinstance(requires, str) else requires)
        if not all(isinstance(name, str) for name in needed):
            raise ValueError("requires must name GTFS files as strings")
    allowed = None if spec is None else {spec} if isinstance(spec, str) else set(spec)
    ranked = index.links is not None or (
        index.edges is not None and "relevance_category" in index.edges.columns
    )
    if index.edges is None and not (ranked and international):
        return FeedList()
    records = []
    if index.edges is not None:
        records = index.edges[index.edges["place_id"] == place.id].to_dict("records")
    if ranked and not international:
        records = [e for e in records if not e.get("cross_border")]
    rows = {}
    if index.feeds is not None:
        rows = {row["feed_id"]: row for row in index.feeds.to_dict("records")}
    if ranked and international:
        linked, link_rows = _link_edges(index, place)
        records += linked
        rows = {**link_rows, **rows}
    wanted = (
        _default_categories(place, tiers, categories, international) if ranked else None
    )
    by_feed = {}
    for edge in records:
        by_feed.setdefault(edge["feed_id"], []).append(edge)
    found = FeedList()
    for feed_id in sorted(by_feed):
        row = rows.get(feed_id)
        if row is None:
            continue
        if allowed is not None and row.get("spec") not in allowed:
            continue
        matched = _matched(by_feed[feed_id], tiers, exclude, on_unknown, wanted)
        if not matched:
            continue
        feed = IndexedFeed(row, matched)
        if needed <= feed.files:
            found.append(feed)
    if ranked:
        found.sort(key=_view_key)
    return found
