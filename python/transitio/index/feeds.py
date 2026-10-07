"""The feed-membership read API: which feeds serve a place, and how.

:meth:`Place.feeds` queries the index's membership edges for one place, and
:meth:`Area.feeds` for the places covering an area, and each returns
:class:`IndexedFeed` objects — a feed joined with its matched edges.
``edges`` is the authoritative per-tier record; the singular fields are
aggregates over *the tiers the query matched*: ``needs_review`` is the *or*,
``selector`` the union — always a :class:`Selector` object, never ``None``:
the whole feed when any edge selects it, else ``unavailable`` dominating,
because a union that silently omitted the unfilterable part would be exactly
the wrong answer — and ``service`` the
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
    scheduled stop-events at those stops per day, averaged over the period
    the feed's timetable covers: from the first to the last date running at
    least a quarter of its busiest day's trips, the quieter days inside that
    period included, not the calendar's whole extent. It is ``None`` when
    nothing could be measured: the feed's timetable was not read (a feed
    that legitimately skipped ``stop_times``, or a declared-only placement),
    or it carried no usable calendar to weight the events by. Snapshots
    built before this rule average over the calendar's whole extent.
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


# The mode families feeds are compared in: tram, subway and rail make one
# rail-bound family, since feeds type the same line differently (an S-Bahn
# as tram in one feed, as rail in another).
_FAMILIES = {"tram": "rail", "subway": "rail", "rail": "rail"}
# A copy of the build's rank.STALE_DAYS.
_STALE_DAYS = 30


class TierEdge:
    """One membership edge, as the query matched it."""

    def __init__(self, record):
        self.place_id = record["place_id"]
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
        # The feed's share of the place's service, as the rank stage
        # recorded it; None when unknown.
        self.share_of_place = _relevance((self.evidence or {}).get("share_of_place"))
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
    """A feed serving a place or an area: its identity row plus the matched
    tier edges, on schema 8 its GTFS-RT companions, and on schema 11 how to
    get the credentials a protected feed needs (from ``index``, when given)."""

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
        provider = self._provider()
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

    def _provider(self):
        """The :class:`AccessProvider` the feed's credentials come from, or
        None."""
        if self._index is None or self.access_provider is None:
            return None
        return self._index.access_provider(self.access_provider)

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
        return frozenset(edge.tier for edge in self.edges.values())

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
        or None on an index without relevance. The score grows with the
        feed's share of the place's service; a feed whose service had ended
        more than 30 days before it was crawled (stale when indexed) counts
        in no feed's share of the place and has a share of 0."""
        scores = [e.relevance for e in self.edges.values() if e.relevance is not None]
        return max(scores) if scores else None

    @property
    def share_of_place(self):
        """The feed's share of the place's service summed over its feeds, as
        the index recorded it (schema 7), the largest over the matched edges
        (0 for a feed stale when indexed); None when no matched edge records
        one."""
        shares = [e.share_of_place for e in self.edges.values()]
        shares = [share for share in shares if share is not None]
        return max(shares) if shares else None

    @property
    def stale_when_indexed(self):
        """The day the feed's timetable ended, when that was more than 30
        days before the index crawled it, as a ``datetime.date``; None
        otherwise."""
        for edge in self.edges.values():
            ended = _day((edge.evidence or {}).get("stale_when_indexed"))
            if ended is not None:
                return ended
        crawled = self.last_crawled
        crawled = _day(crawled[:10]) if isinstance(crawled, str) else None
        end = self.service_end
        if end is None or crawled is None or (crawled - end).days <= _STALE_DAYS:
            return None
        return end

    @property
    def overlap(self):
        """How much of the feed's service other feeds also run, from the
        matched edges' overlap evidence: ``{"departures": {mode: per day},
        "with": {feed_id: {mode: share}}}``, departures summed over the
        edges and each share weighted by them. None when no matched edge
        records it."""
        departures, run, found = {}, {}, False
        for edge in self.edges.values():
            block = (edge.evidence or {}).get("overlap")
            if not isinstance(block, dict):
                continue
            found = True
            counts = {m: float(v) for m, v in (block.get("departures") or {}).items()}
            for mode, value in counts.items():
                departures[mode] = departures.get(mode, 0.0) + value
            for other, shares in (block.get("with") or {}).items():
                for mode, share in shares.items():
                    key = (other, mode)
                    run[key] = run.get(key, 0.0) + counts.get(mode, 0.0) * share
        if not found:
            return None
        shares = {}
        for (other, mode), value in run.items():
            if departures.get(mode):
                shares.setdefault(other, {})[mode] = value / departures[mode]
        return {"departures": departures, "with": shares}

    @property
    def catalogue_name(self):
        """The feed's name in the Mobility Database, else in the Transitland
        Atlas, as the catalogue gives it; None without one."""
        for source in ("mdb", "atlas"):
            name = (_parse(self._row.get(source)) or {}).get("name")
            if name:
                return name
        return None

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
        """The union of the matched edges' selectors: the whole feed when an
        edge selects it, else the weakest link decides.

        Fail-safe: an unknown selector state, or a ``complete`` edge carrying no
        route ids, counts as ``unavailable`` — a trusted empty selector would let
        downstream filtering silently drop routes.
        """
        states = {edge.selector_state for edge in self.edges.values()}
        if "whole_feed" in states:
            # A whole-feed claim absorbs any selector it is unioned with.
            return Selector("whole_feed")
        if states - {"complete"}:
            return Selector("unavailable")
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

    def to_dataframe(self):
        """The feeds as a DataFrame, one row per feed, in the list's order,
        with the columns that rank them and a ``reason`` in words.

        ``departures_per_day``, ``stops`` and ``routes`` are the feed's
        service in the place, summed over an area's parts. ``modes`` lists
        the feed's modes there, most departures first. ``covers`` is the
        share of the departures of all the listed feeds, each departure
        counted once, that the feed runs, and ``repeats`` names up to three
        listed feeds running the most of the feed's own departures, with
        their shares; both come from the index's overlap evidence and are
        None without it. ``reason`` gives the category and tiers, the share
        of the departures summed over the place's feeds, the feed repeating
        the most of it, and where they apply, staleness, the feeds
        containing it and the account it needs.
        """
        import pandas

        blocks = _family_blocks(self)
        universe = sum(_universe(blocks).values())
        listed = [feed.feed_id for feed in self]
        rows = []
        for feed in self:
            departures = (feed.overlap or {"departures": {}})["departures"]
            block = blocks.get(feed.feed_id)
            repeats = None
            if block is not None:
                others = [i for i in listed if i != feed.feed_id]
                repeats = _repeated_by(block, others)[:3]
            rows.append(
                {
                    "feed_id": feed.feed_id,
                    "name": feed.name,
                    "catalogue_name": feed.catalogue_name,
                    "tiers": sorted(feed.tiers),
                    "relevance_category": feed.relevance_category,
                    "relevance": feed.relevance,
                    "share_of_place": feed.share_of_place,
                    **_summed_service(feed),
                    "modes": sorted(departures, key=departures.get, reverse=True),
                    "covers": (
                        min(1.0, sum(block[0].values()) / universe)
                        if block is not None and universe
                        else None
                    ),
                    "repeats": (
                        ", ".join(f"{i} {_percent(s)}" for i, s in repeats)
                        if repeats
                        else None
                    ),
                    "service_start": feed.service_start,
                    "service_end": feed.service_end,
                    "stale_when_indexed": feed.stale_when_indexed,
                    "contained_in": feed.contained_in,
                    "access": feed.access,
                    "stop_count": feed.stop_count,
                    "reason": _ranking_reason(feed, repeats),
                }
            )
        return pandas.DataFrame(rows, columns=_TABLE_COLUMNS)


_TABLE_COLUMNS = (
    "feed_id",
    "name",
    "catalogue_name",
    "tiers",
    "relevance_category",
    "relevance",
    "share_of_place",
    "departures_per_day",
    "stops",
    "routes",
    "modes",
    "covers",
    "repeats",
    "service_start",
    "service_end",
    "stale_when_indexed",
    "contained_in",
    "access",
    "stop_count",
    "reason",
)


def _summed_service(feed):
    """The feed's departures per day, stops and routes, each place of its
    matched edges counted once; None where no place records the number."""
    services = {edge.place_id: edge.service for edge in feed.edges.values()}
    summed = {}
    for field in ("departures_per_day", "stops", "routes"):
        values = [getattr(s, field) for s in services.values()]
        values = [value for value in values if value is not None]
        summed[field] = sum(values) if values else None
    return summed


def _family_blocks(feeds):
    """Each feed's overlap evidence by mode family, for the feeds that record
    departures: ``{feed_id: (departures, shares)}``, ``departures`` per
    family and ``shares`` per other feed, the share of each family's
    departures that feed also runs, summed over the places of the matched
    edges. The index records which lines another feed runs, so at each place
    a listed feed is credited with at most its own departures there in the
    family: a feed running the same lines with fewer trips does not stand in
    for all of them. Another feed recording no departures there keeps the
    recorded share."""
    by_place = {}
    for feed in feeds:
        for edge in feed.edges.values():
            block = (edge.evidence or {}).get("overlap")
            if not isinstance(block, dict):
                continue
            here = by_place.setdefault(edge.place_id, {})
            departures, run = here.setdefault(feed.feed_id, ({}, {}))
            counts = {m: float(v) for m, v in (block.get("departures") or {}).items()}
            for mode, value in counts.items():
                family = _FAMILIES.get(mode, mode)
                departures[family] = departures.get(family, 0.0) + value
            for other, shares in (block.get("with") or {}).items():
                mine = run.setdefault(other, {})
                for mode, share in shares.items():
                    family = _FAMILIES.get(mode, mode)
                    value = share * counts.get(mode, 0.0)
                    mine[family] = mine.get(family, 0.0) + value
    totals, credited = {}, {}
    for here in by_place.values():
        for feed_id, (departures, run) in here.items():
            total = totals.setdefault(feed_id, {})
            for family, value in departures.items():
                total[family] = total.get(family, 0.0) + value
            mine = credited.setdefault(feed_id, {})
            for other, by in run.items():
                theirs = here[other][0] if other in here else None
                into = mine.setdefault(other, {})
                for family, value in by.items():
                    if theirs is not None:
                        value = min(value, theirs.get(family, 0.0))
                    into[family] = into.get(family, 0.0) + value
    blocks = {}
    for feed_id, departures in totals.items():
        if sum(departures.values()) > 0:
            shares = {
                other: {
                    f: v / departures[f] for f, v in by.items() if departures.get(f)
                }
                for other, by in credited[feed_id].items()
            }
            blocks[feed_id] = (departures, shares)
    return blocks


def _unrun(block, others):
    """Per family, the departures of a feed that ``others`` do not run, by
    the largest share any of them runs there."""
    departures, shares = block
    return {
        family: value
        * (1 - max((shares.get(o, {}).get(family, 0.0) for o in others), default=0))
        for family, value in departures.items()
    }


def _repeated_by(block, others):
    """The ``(feed_id, share)`` of each of ``others`` running part of a
    feed's departures, the largest share first."""
    total = sum(block[0].values())
    found = [(o, 1 - sum(_unrun(block, [o]).values()) / total) for o in others]
    return sorted(
        [(o, share) for o, share in found if share > 0], key=lambda f: (-f[1], f[0])
    )


def _universe(blocks):
    """Departures per mode family over the feeds of ``blocks``, each counted
    once: each family's feeds in order of their departures there, each
    adding the departures that the feeds before it do not run."""
    universe = {}
    for family in {f for departures, _ in blocks.values() for f in departures}:
        order = sorted((-block[0].get(family, 0.0), i) for i, block in blocks.items())
        seen = []
        for _, feed_id in order:
            unrun = _unrun(blocks[feed_id], seen).get(family, 0.0)
            universe[family] = universe.get(family, 0.0) + unrun
            seen.append(feed_id)
    return universe


def _percent(share):
    """A share as the reasons print it: whole percent rounded down,
    "over 99 %" from 0.995, two significant figures under 1 %."""
    percent = share * 100
    if percent >= 100 - 1e-9:
        return "100 %"
    if percent >= 99.5:
        return "over 99 %"
    if percent >= 1:
        return f"{math.floor(percent + 1e-9)} %"
    if percent <= 0:
        return "0 %"
    return f"{percent:.{1 - math.floor(math.log10(percent))}f} %"


def _access_note(feed):
    """What the feed's access asks of a user, in words; None when open."""
    if feed.access != "key":
        return None
    provider = feed._provider()
    if provider is None:
        return "needs credentials the index has no details for"
    account = {True: "a free account", False: "a paid account"}.get(
        provider.free, "an account"
    )
    return f"needs {account} with {provider.name or provider.provider_id}"


def _ranking_reason(feed, repeats):
    """Why the feed ranks where it does, in words (see
    :meth:`FeedList.to_dataframe`)."""
    tiers = sorted(feed.tiers)
    reason = f"{', '.join(tiers)} tier{'s' if len(tiers) > 1 else ''}"
    if feed.relevance_category is not None:
        reason = f"{feed.relevance_category} ({reason})"
    if feed.share_of_place is not None:
        bases = {(e.evidence or {}).get("share_basis") for e in feed.edges.values()}
        basis = "stops" if bases == {"stops"} else "departures"
        share = _percent(feed.share_of_place)
        reason += f": {share} of the {basis} summed over the place's feeds"
    notes = []
    if repeats:
        notes.append(f"repeats {repeats[0][0]} ({_percent(repeats[0][1])})")
    ended = feed.stale_when_indexed
    if ended is not None:
        notes.append(f"stale when indexed: its timetable ended {ended}")
    if feed.contained_in:
        notes.append(f"contained in {', '.join(feed.contained_in)}")
    notes.append(_access_note(feed))
    return "; ".join([reason] + [note for note in notes if note])


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


def _link_edges(index, place_ids):
    """The cross-border edges to the places ``place_ids`` a country load does
    not carry in its edges, with the rows of the feeds they name."""
    if index.links is None or index.country is None:
        return [], {}
    links = index.links[index.links["place_id"].isin(place_ids)]
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


def feeds_for_place(index, place, **query):
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
    found = _feeds_for_places(index, [place], **query)
    for feed in found:
        feed.edges = {tier: edge for (_, tier), edge in feed.edges.items()}
    return found


def _feeds_for_places(
    index,
    places,
    *,
    tiers=None,
    exclude=None,
    spec="gtfs",
    on_unknown="include",
    requires=None,
    categories="default",
    international=False,
):
    """The :class:`IndexedFeed` list for ``places``, each feed once: every
    place answers as in :func:`feeds_for_place`, and a feed's matched edges
    are keyed by ``(place_id, tier)``, in the order of ``places``."""
    if on_unknown not in ("include", "exclude"):
        raise ValueError("on_unknown must be 'include' or 'exclude'")
    if categories not in (None, "default"):
        # Read once: every place applies the same categories.
        categories = frozenset(categories)
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
    ids = [place.id for place in places]
    records = []
    if index.edges is not None:
        records = index.edges[index.edges["place_id"].isin(ids)].to_dict("records")
    if ranked and not international:
        records = [e for e in records if not e.get("cross_border")]
    rows = {}
    if ranked and international:
        linked, rows = _link_edges(index, ids)
        records += linked
    if index.feeds is not None:
        named = {edge["feed_id"] for edge in records}
        own = index.feeds[index.feeds["feed_id"].isin(named)]
        rows.update((row["feed_id"], row) for row in own.to_dict("records"))
    wanted = dict.fromkeys(ids)
    if ranked:
        wanted = {
            p.id: _default_categories(p, tiers, categories, international)
            for p in places
        }
    by_feed = {}
    for edge in records:
        by_place = by_feed.setdefault(edge["feed_id"], {})
        by_place.setdefault(edge["place_id"], []).append(edge)
    found = FeedList()
    for feed_id in sorted(by_feed):
        row = rows.get(feed_id)
        if row is None:
            continue
        if allowed is not None and row.get("spec") not in allowed:
            continue
        matched = {}
        for place_id in ids:
            edges = by_feed[feed_id].get(place_id, ())
            query = (tiers, exclude, on_unknown, wanted[place_id])
            for tier, edge in _matched(edges, *query).items():
                matched[(place_id, tier)] = edge
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
