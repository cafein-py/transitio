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

import dataclasses
import datetime
import json
import math

__all__ = [
    "AccessProvider",
    "IndexedFeed",
    "PlaceService",
    "RealtimeFeed",
    "Selector",
    "ServiceLevel",
    "TierEdge",
    "Validity",
    "Window",
]

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
# A region or country of at most 1,000 km² is town-sized and keeps its
# primary, secondary and tertiary feeds.
CATEGORY_ORDER = ("primary", "secondary", "tertiary", "international", "unknown")
DEFAULT_CATEGORIES = {
    "city": ("primary", "secondary"),
    "metro": ("primary", "secondary"),
    "region": ("secondary", "tertiary"),
    "country": ("tertiary",),
}
TOWN_MAX_KM2 = 1000.0


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


def _day(value):
    """An ISO date string as a ``datetime.date``, None for anything else."""
    value = _scalar(value)
    if not isinstance(value, str):
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


class Window:
    """A run of days over which a place has the same number of valid feeds."""

    def __init__(self, record):
        self.start = _day(record.get("start"))
        self.end = _day(record.get("end"))
        self.feeds = int(record.get("feeds") or 0)

    def __repr__(self):
        return f"Window({self.start}, {self.end}, feeds={self.feeds})"


class Validity:
    """The validity of a place's feeds (schema 9): how many are dated, the
    earliest start and latest end among them, the windows of constant feed
    count and the best window (most feeds, then the longest, then the
    earliest)."""

    def __init__(self, record):
        record = record or {}
        self.feeds_dated = int(record.get("feeds_dated") or 0)
        self.feeds_undated = int(record.get("feeds_undated") or 0)
        self.start = _day(record.get("start"))
        self.end = _day(record.get("end"))
        self.windows = [Window(w) for w in record.get("windows") or ()]
        best = record.get("best")
        self.best = Window(best) if best else None

    def on(self, day):
        """How many of the place's dated feeds are valid on ``day``."""
        for window in self.windows:
            if window.start and window.end and window.start <= day <= window.end:
                return window.feeds
        return 0

    def __repr__(self):
        return (
            f"Validity(feeds_dated={self.feeds_dated}, start={self.start}, "
            f"end={self.end}, best={self.best!r})"
        )


@dataclasses.dataclass(frozen=True)
class AccessProvider:
    """A provider that issues the credentials key-protected feeds take
    (schema 11): where to register, its documentation and terms, the
    credential fields it issues and whether an account is free (None when
    unknown)."""

    provider_id: str
    name: str | None
    registration_url: str | None
    docs_url: str | None
    terms_url: str | None
    credential_fields: tuple
    free: bool | None

    @property
    def env_names(self):
        """Each credential field, in order, with the environment variable
        that supplies it: ``TRANSITIO_KEY_<ID>__<FIELD>``, upper-cased with
        ``-`` as ``_``."""

        def upper(text):
            return text.upper().replace("-", "_")

        prefix = f"TRANSITIO_KEY_{upper(self.provider_id)}__"
        return {field: prefix + upper(field) for field in self.credential_fields}


def _access_provider(record):
    """An :class:`AccessProvider` from a row of the providers table."""
    fields = record.get("credential_fields")
    free = _scalar(record.get("free"))
    return AccessProvider(
        provider_id=record["provider_id"],
        name=_scalar(record.get("name")),
        registration_url=_scalar(record.get("registration_url")),
        docs_url=_scalar(record.get("docs_url")),
        terms_url=_scalar(record.get("terms_url")),
        credential_fields=() if fields is None else tuple(str(f) for f in fields),
        free=None if free is None else bool(free),
    )


class RealtimeFeed:
    """A GTFS-RT companion of a static feed (schema 8): its identity, the
    static feed it describes and the endpoints the catalogues carry."""

    def __init__(self, record):
        self._row = record
        self.feed_id = record["feed_id"]
        self.onestop_id = _scalar(record.get("onestop_id"))
        self.name = _scalar(record.get("name"))
        self.source = _scalar(record.get("source"))
        self.static_feed_id = _scalar(record.get("static_feed_id"))
        self.static_link_method = _scalar(record.get("static_link_method"))
        self.urls = _parse(record.get("urls")) or {}
        types = record.get("entity_types")
        self.entity_types = [] if types is None else [str(t) for t in types]
        allowed = record.get("redistribution_allowed")
        self.redistribution_allowed = (
            None if allowed is None or allowed != allowed else bool(allowed)
        )
        self.snapshot = _scalar(record.get("snapshot"))

    def __repr__(self):
        return (
            f"RealtimeFeed({self.feed_id!r}, static_feed_id={self.static_feed_id!r}, "
            f"entity_types={self.entity_types!r})"
        )


class IndexedFeed:
    """A feed serving a place: its identity row plus the matched tier edges,
    on schema 8 its GTFS-RT companions, and on schema 11 how to get the
    credentials a protected feed needs (from ``index``, when given)."""

    def __init__(self, row, edges, realtime=(), index=None):
        self._row = row
        self.edges = edges
        self.realtime = list(realtime)
        self._index = index

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
    def service_start(self):
        """The first date any of the feed's services runs (schema 9), a
        ``datetime.date``; None when the feed has no dated calendar."""
        return _day(self._row.get("service_start"))

    @property
    def service_end(self):
        """The last date any of the feed's services runs (schema 9)."""
        return _day(self._row.get("service_end"))

    @property
    def contained_in(self):
        """The ids of the larger feeds whose stops and routes contain this
        feed's (schema 10): a stop-and-route heuristic, not proof that every
        trip is carried. Empty before schema 10."""
        ids = self._row.get("contained_in")
        return [] if ids is None else [str(feed_id) for feed_id in ids]

    @property
    def download_url(self):
        """The URL the index crawls the feed from (schema 11); None for a
        feed without a static URL, and before schema 11."""
        return _scalar(self._row.get("download_url"))

    @property
    def access(self):
        """``"open"``, or ``"key"`` for a feed that needs credentials
        (schema 11); None before schema 11."""
        return _scalar(self._row.get("access"))

    @property
    def access_provider(self):
        """The id of the provider whose credentials the feed takes (schema
        11); None for an open feed, a protected feed the index has no access
        details for, and before schema 11."""
        return _scalar(self._row.get("access_provider"))

    @property
    def auth_method(self):
        """How the credentials are sent (schema 11): ``"query_param"``,
        ``"header"``, ``"basic_auth"`` or ``"unsupported"``; None for an open
        feed."""
        return _scalar(self._row.get("auth_method"))

    @property
    def auth_params(self):
        """Where each credential goes (schema 11): for ``query_param`` each
        query parameter with the credential field it carries, for ``header``
        the header with its field, empty for ``basic_auth``; None for an
        open feed."""
        return _parse(self._row.get("auth_params"))

    @property
    def registration_url(self):
        """The catalogue's registration page for the feed (schema 11), or
        None."""
        return _scalar(self._row.get("registration_url"))

    @property
    def access_url(self):
        """The URL the credentials belong to, the one the index crawls:
        ``download_url`` on schema 11, else the Atlas static feed URL, else
        the Mobility Database direct download."""
        return _crawl_url(self)

    def access_instructions(self):
        """How to get and give the credentials a protected feed needs, as one
        paragraph: who issues them, where to register, and how to hand them
        to transitio. None for an open feed and before schema 11."""
        if self.access != "key":
            return None
        name = self.name or self.feed_id
        provider = None
        if self._index is not None and self.access_provider is not None:
            provider = self._index.access_provider(self.access_provider)
        if provider is None:
            lead = "needs credentials; the index has no access details for it yet"
        else:
            lead = f"needs credentials from {provider.name or provider.provider_id}"
        parts = [f"{name} {lead}."]
        register = (provider and provider.registration_url) or self.registration_url
        if register:
            parts.append(f"Register at {register}.")
        if provider is not None:
            if provider.docs_url:
                parts.append(f"Documentation: {provider.docs_url}.")
            fields = ", ".join(f'"{f}": "..."' for f in provider.credential_fields)
            run = f'transitio.credentials.set("{provider.provider_id}", {{{fields}}})'
            names = ", ".join(provider.env_names.values())
            parts.append(f"Then run {run}" + (f" or set {names}." if names else "."))
        if self.auth_method == "unsupported":
            parts.append(
                "transitio cannot send these credentials itself in this version; "
                "download the feed by hand."
            )
        return " ".join(parts)

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
    place kind's default view (with ``primary`` for a town-sized region or
    country) — with ``international`` added when the cross-border feeds
    were asked for — unless tiers were named (a tier query is answered in
    tiers)."""
    if categories != "default":
        return None if categories is None else frozenset(categories)
    if tiers is not None:
        return None
    default = DEFAULT_CATEGORIES.get(place.kind)
    if default is None:
        return None
    if place.kind in ("region", "country") and _town_sized(place):
        default = ("primary", "secondary", "tertiary")
    return frozenset(default) | ({"international"} if international else set())


def _town_sized(place):
    """Whether ``place`` has a boundary of at most :data:`TOWN_MAX_KM2`."""
    from transitio.index.places import _as_shape
    from transitio.osm._fetch import _area_km2

    geometry = _as_shape(place.geometry)
    if geometry is None:
        return False
    return _area_km2(geometry) <= TOWN_MAX_KM2


def _crawl_url(feed):
    """The URL the index crawls ``feed`` from: ``download_url`` on schema 11,
    else the Atlas static feed URL, else the Mobility Database direct
    download."""
    from transitio.catalog._atlas import STATIC_URL

    row = feed._row
    if "download_url" in row:
        return _scalar(row["download_url"])
    atlas = (_parse(row.get("atlas")) or {}).get("urls") or {}
    mdb = (_parse(row.get("mdb")) or {}).get("urls") or {}
    return atlas.get(STATIC_URL) or mdb.get("direct_download")


def _hosted_url(feed):
    """The Mobility Database's hosted copy of ``feed`` (``urls.latest`` of
    its catalogue record), or None."""
    return ((_parse(feed._row.get("mdb")) or {}).get("urls") or {}).get("latest")


def _companions(index, feed_id, partition=None):
    """The :class:`RealtimeFeed` companions naming ``feed_id`` as their
    static feed: from the index's own realtime table, or from the partition
    holding a feed that came through a link. Empty before schema 8."""
    table = index.realtime
    if partition is not None and partition != index.country:
        table = index.realtime_in(partition)
    if table is None or not len(table):
        return []
    mine = table[table["static_feed_id"] == feed_id]
    return [RealtimeFeed(record) for record in mine.to_dict("records")]


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
            rows[row["feed_id"]] = {**row, "_partition": partition}
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

    On a schema-8 index the feeds table is GTFS only: ``spec="gtfs"`` and the
    default return them all, another spec an empty list, and each feed's
    GTFS-RT companions come as its ``realtime`` list.

    On a schema-7 index the place's default view applies: a city keeps its
    ``primary`` and ``secondary`` feeds, a region ``secondary`` and
    ``tertiary``, a country ``tertiary``, and a region or country whose
    boundary covers at most :data:`TOWN_MAX_KM2` (1,000 km²) is town-sized
    and keeps ``primary``, ``secondary`` and ``tertiary`` (``categories``
    names other categories, ``None`` keeps every category; a ``tiers`` query
    is answered in tiers instead), cross-border edges are left out unless
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
        feed = IndexedFeed(
            row,
            matched,
            _companions(index, feed_id, row.get("_partition")),
            index=index,
        )
        if needed <= feed.files:
            found.append(feed)
    if ranked:
        found.sort(key=_view_key)
    return found
