"""Place name resolution over a published index's ``places`` table.

A query — a name, a QID or own ``tp_`` id (a former id or a carried QID
included), or a :class:`Place` — resolves to one :class:`Place` through a
defined ranking, never a guess: the query is normalised and matched
against every place's labels and aliases in every language, candidates score on
match strength then ``kind`` precedence then feed count, and only an exact
match can win: the sole exact match, one that beats the runner-up by the
ambiguity margin, or, where the margin does not decide, the place of the name
known far more widely than every other exact match that is not a metro, at
home or abroad ("Moscow" is Moscow, Russia, not Moscow, Idaho). Before any of
these, a city of at least 200,000 people (schema 11) carrying the name as its
name or in English wins when it has more than twice the population of every
other exact match recording one ("Lima" is Lima, Peru, not Lima, Ohio), unless
a region or country of the name contains no city of the name (Victoria, the
city in British Columbia and the Australian state) or a city of the name
without a recorded population is known at least as widely, as San Jose,
California, a city of the San Francisco urban centre, is. A place's own
names are its name and its labels in its country's languages (from Unicode
CLDR) or English. Where a place carries the
name as its own, one reaching it only through a label in another language
does not compete unless it is known far more widely, by the languages its
name is recorded in; where a place carries it as its name or in English, one
carrying it only in another of its own languages ("Bergen", Dutch for Mons)
does not compete either, on the same terms. The margin never favours a place
reached only through an alias or a label in another language over one
carrying the name as its own, nor decides against a place of the name in
another country known far more widely. A city's namesakes do not compete
with it: a metro in its country shares its name because it is the city's
metro or named after it, a same-named area containing it that runs much the
same service (no more than the margin beyond the city's feeds) is the city
itself, and a place inside it or, not being a city, in its country that
reaches its name only through an alias is named after it. Where that
containing area is a city of the name known far more widely, the inner one
is the namesake instead: one city recorded twice (València and its
comarca). Where no exact
match is a city, a region or country of the name (a province, an emirate, a
dependency) stands as the city. Metros of the name sharing a member place
are one metro under several definitions, and only those of the earliest
definition in the default order compete, except a city's own metros (those
in its country), which keep every definition so that dropping the others
cannot hand the city's name to one of them; a named definition keeps only
its metros. Anything else raises
:class:`AmbiguousPlaceError` with the candidates, or
:class:`PlaceNotFoundError` with the partial matches, if any.
"""

import functools
import json
import math
import re
import threading
import unicodedata
from collections import defaultdict, namedtuple

from transitio.exceptions import (
    AmbiguousPlaceError,
    PlaceNotFoundError,
    TransitioError,
)

# The places table is read back through GeoParquet, so list columns arrive as
# arrays and null cells as NaN, not None; these coerce both to plain Python.


def _as_list(value):
    if value is None:
        return []
    try:
        return list(value)
    except TypeError:
        return []


def _as_dict(value):
    if value is None:
        return {}
    try:
        return dict(value)
    except (TypeError, ValueError):
        return {}


def _as_str(value):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return value


def _as_shape(value):
    """A geometry cell as a shapely geometry, WKB decoded; None for a null
    or an empty geometry."""
    import shapely

    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, (bytes, bytearray)):
        value = shapely.from_wkb(bytes(value))
    return None if value.is_empty else value


def _as_concordances(value):
    """The ``{namespace: [ids]}`` block, whether stored as a mapping or as
    its JSON text; anything else is an empty block."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return {
        namespace: [str(v) for v in _as_list(ids)]
        for namespace, ids in _as_dict(value).items()
    }


# Match strength, strongest first: an exact label/alias beats a prefix beats a
# query whose tokens are a subset of the label's.
_EXACT, _PREFIX, _SUBSET = 3, 2, 1

# kind precedence for the metro-default world: a metro outranks the city it
# contains, which outranks the region, which outranks the country.
_KIND_ORDER = {"metro": 0, "city": 1, "region": 2, "country": 3}

# The metro definitions (a metro's ``source_subtype``) in the order a lookup
# picks among one metro's: the commuting-based ones (Urban Audit FUAs, US
# MSAs), Eurostat's NUTS-3 metropolitan regions, then the FAO city-regions,
# global and with uneven boundaries. Any other ranks after these.
_METRO_DEFINITIONS = (
    "functional urban area",
    "metropolitan statistical area",
    "metropolitan region",
    "city-region (FAO)",
)


def _definition_rank(definition):
    """A metro definition's place in ``_METRO_DEFINITIONS``; any other after."""
    if definition in _METRO_DEFINITIONS:
        return _METRO_DEFINITIONS.index(definition)
    return len(_METRO_DEFINITIONS)


# ``_decide``'s answer when the margin would favour an alias over a name: no
# decision, and no other contest may overturn it.
_VETOED = object()

# A place with at least this many language labels, and more than twice
# another's, is known far more widely: abroad it keeps the feed margin from
# deciding, where feeds do not decide it wins, and a label in another language
# keeps it in the contest.
_WELL_KNOWN = 100

# A city of at least this many people wins its name over far smaller places
# of it (see ``_most_populous``).
_LARGE_CITY = 200_000

# The partial matches a PlaceNotFoundError message names; all are candidates.
_PARTIAL_SHOWN = 10

_QID = re.compile(r"\AQ[1-9][0-9]*\Z")
# The index's own place id (schema 6); a query in this form is an id lookup.
# The registry bounds the number; the reader accepts the form.
_OWN_ID = re.compile(r"\Atp_[1-9][0-9]*\Z")

# Slash and middot variants that, like every dash, join whole words.
_SLASH_SEPARATORS = frozenset("/\\⁄∕·−")

# Where a label comes from, ranked: the primary name, a name in the place's own
# languages or English, an alias, a name in another language.
_NAME, _TRANSLATION, _ALIAS, _RARE = 0, 1, 2, 3
# Sorts after every character a normalised label can hold, so ``prefix + _AFTER``
# bounds the labels that start with ``prefix``.
_AFTER = "\U0010ffff"

# A type-ahead suggestion: the place, the label to show, and the label that
# matched as the place carries it with its source (``"name"``, a language
# code, or ``"alias"``).
Suggestion = namedtuple("Suggestion", ["place", "label", "matched", "source"])


def _is_separator(char):
    """Whether ``char`` joins whole words and so should become a space.

    Every Unicode dash (category ``Pd`` — the ASCII hyphen through the en/em and
    the typographic U+2010/U+2011 variants) plus the common slash and middot
    marks. Other punctuation (apostrophes, periods, parentheses) is dropped
    instead, so intra-word marks fold away.
    """
    return char in _SLASH_SEPARATORS or unicodedata.category(char) == "Pd"


def _normalize(text):
    """Casefold ``text``, strip diacritics and punctuation, collapse whitespace."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(text))
    bare = "".join(c for c in decomposed if not unicodedata.combining(c))
    kept = []
    for char in bare.casefold():
        if char.isalnum() or char.isspace():
            kept.append(char)
        elif _is_separator(char):
            kept.append(" ")
    return " ".join("".join(kept).split())


@functools.cache
def _country_languages():
    """``{country code: base language codes}``: each country's official, de
    facto official and official regional languages, from Unicode CLDR."""
    from importlib.resources import files

    path = files("transitio.index").joinpath("country_languages.json")
    table = json.loads(path.read_text(encoding="utf-8"))
    return {code: frozenset(languages) for code, languages in table.items()}


@functools.cache
def _own_language(country_code, language):
    """Whether ``language``, a label's language code (``de``, ``de-at``,
    ``zh-Latn-pinyin``), is one of a place's own: its base code is English,
    Wikidata's multilingual ``mul``, or a language of ``country_code``. A
    country the table lacks has English and ``mul`` only."""
    base = str(language).lower().replace("_", "-").split("-", 1)[0]
    return base in ("en", "mul") or base in _country_languages().get(country_code, ())


# One delineation of a place: the place itself, an administrative ancestor
# it lies within, or a metro it is a member of, with the row's kind and subtype.
Delineation = namedtuple("Delineation", ["relation", "kind", "subtype", "place"])


class Place:
    """A resolved place: its identity, hierarchy, names and boundary."""

    def __init__(self, record, lookup):
        self._record = record
        self._lookup = lookup

    @property
    def id(self):
        return self._record["place_id"]

    @property
    def kind(self):
        return self._record["kind"]

    @property
    def name(self):
        return self._record.get("name")

    @property
    def subtype(self):
        """The place's ``source_subtype``: ``locality``, ``county``, ``region``
        or ``country``, or a metro's definition such as ``functional urban
        area``; None for a row without one."""
        return self._record.get("source_subtype")

    @property
    def names(self):
        return dict(self._record.get("names") or {})

    @property
    def aliases(self):
        return list(self._record.get("aliases") or [])

    @property
    def country_code(self):
        return self._record.get("country_code")

    @property
    def wikidata_id(self):
        """The place's QID, or None: the id itself before schema 6."""
        return self._record.get("wikidata_id")

    @property
    def concordances(self):
        """``{namespace: [ids]}`` — every external id the place carries."""
        return {k: list(v) for k, v in self._record.get("concordances", {}).items()}

    @property
    def former_ids(self):
        """Own ids merged into this place; each still resolves to it."""
        return list(self._record.get("former_ids") or [])

    @property
    def metro_ids(self):
        return list(self._record.get("metro_ids") or [])

    @property
    def member_ids(self):
        return list(self._record.get("member_ids") or [])

    @property
    def default_metro_id(self):
        return self._record.get("default_metro_id")

    @property
    def service(self):
        """The place's transit service level, summed over the feeds serving
        it: ``feeds``, ``stops``, ``routes`` and ``departures_per_day``.
        The sums leave out feeds stale when indexed
        (:attr:`~transitio.index.IndexedFeed.relevance`), and in a merged
        index they are summed over every build's feeds.

        The numbers describe how much service the index knows about in the
        place — a capital's thousands of daily stop-events against a small
        town's few dozen — not how certain the membership is.
        """
        from transitio.index.feeds import PlaceService, _parse

        return PlaceService(_parse(self._record.get("service")))

    @property
    def validity(self):
        """The validity of the feeds serving this place (schema 9): a
        :class:`transitio.index.feeds.Validity` with the dated feed count,
        the span, the windows of constant feed count and the best window;
        None when the index predates it or no feed serves the place."""
        from transitio.index.feeds import Validity, _parse

        record = _parse(self._record.get("validity"))
        return None if record is None else Validity(record)

    @property
    def geometry(self):
        return self._record.get("geometry")

    @property
    def centre(self):
        """The point the index gives as the place's centre (schema 11), a
        shapely ``Point`` in lon/lat; None when the index has none for the
        place or predates schema 11. For a point inside the place either
        way, ``place.centre or place.geometry.representative_point()`` (the
        centroid can lie outside)."""
        return _as_shape(self._record.get("centre"))

    @property
    def population(self):
        """The 2025 population of the urban centre (GHS-UCDB) the index
        matched the place to (schema 11), which can extend beyond the
        place's boundary: for Kochi, India, the 5 million of the
        conurbation. None when the index records none for the place or
        predates schema 11."""
        from transitio.index.feeds import _count, _scalar

        return _count(_scalar(self._record.get("population")))

    @property
    def parent(self):
        """The administrative parent :class:`Place`, or ``None``."""
        parent_id = self._record.get("parent_id")
        return self._lookup.get(parent_id) if parent_id else None

    @property
    def children(self):
        """The places whose parent is this one, in id order."""
        return self._lookup.children(self.id)

    @property
    def ancestors(self):
        """The administrative chain upwards, nearest first: the parent, its
        parent, and so on until a place has none the index holds; a place
        already in the chain ends it, so a malformed table cannot loop."""
        chain, seen = [], {self.id}
        place = self.parent
        while place is not None and place.id not in seen:
            chain.append(place)
            seen.add(place.id)
            place = place.parent
        return chain

    @property
    def metros(self):
        """The metros this place belongs to, resolved from ``metro_ids``."""
        return self._lookup.resolve_ids(self.metro_ids)

    @property
    def members(self):
        """The places that make up this one, resolved from ``member_ids``."""
        return self._lookup.resolve_ids(self.member_ids)

    def delineations(self):
        """Every delineation of this place as a :class:`Delineation`: the
        place itself, then its ancestors nearest first, then the metros it
        is a member of by subtype, name and id — one row of the places
        table each, told apart by ``kind`` and ``subtype``."""
        rows = [Delineation("itself", self.kind, self.subtype, self)]
        rows += [Delineation("within", p.kind, p.subtype, p) for p in self.ancestors]
        metros = sorted(
            self.metros, key=lambda m: (m.subtype or "", m.name or "", m.id)
        )
        rows += [Delineation("member of", m.kind, m.subtype, m) for m in metros]
        return rows

    def feeds(
        self,
        *,
        tiers=None,
        exclude=None,
        spec="gtfs",
        on_unknown="include",
        requires=None,
        categories="default",
        international=False,
    ):
        """The feeds serving this place, as :class:`IndexedFeed` objects.

        ``tiers`` keeps only edges of those tiers, ``exclude`` drops edges of
        the named tiers (a feed with nothing left is dropped), ``spec`` selects
        the feed kind (static GTFS by default; ``None`` for everything, a list
        to narrow), ``on_unknown`` governs unknown-tier edges and ``requires``
        keeps only feeds whose manifest carries the named GTFS files (for
        example ``"shapes.txt"``). On a schema-7 index ``categories`` picks
        the relevance categories (the place kind's default view unless named,
        in which a region or country of at most 1,000 km² keeps its primary
        feeds too; ``None`` for all) and ``international=True`` adds the
        cross-border feeds. See :func:`transitio.index.feeds.feeds_for_place`.
        """
        return self._lookup.feeds(
            self,
            tiers=tiers,
            exclude=exclude,
            spec=spec,
            on_unknown=on_unknown,
            requires=requires,
            categories=categories,
            international=international,
        )

    def recommend(
        self, when=None, *, tiers=None, exclude=None, target=0.95, max_feeds=4
    ):
        """Which of the place's feeds to use on ``when`` (a date, today when
        None), and why the others are left out, as a
        :class:`~transitio.index.Recommendation`; ``print()`` it to read it.

        Every feed serving the place is a candidate, whatever its category;
        ``tiers`` and ``exclude`` narrow them as in :meth:`feeds`. A feed is
        left out when it was stale when indexed, when its timetable as
        indexed does not run on the day, or when it needs a paid account. On
        an index that records which feeds run the same lines, feeds are
        taken until they cover ``target`` of the place's departures, each
        counted once, and 80 % of each mode's (rail, subway and tram
        together), at most ``max_feeds`` of them; among feeds adding about
        as much, the open one with the fewest stops is taken. Without that
        evidence the feed with the most departures is taken. See
        :mod:`transitio.index.recommend`.
        """
        from transitio.index.recommend import recommend

        feeds = self.feeds(tiers=tiers, exclude=exclude, categories=None)
        return recommend(self, feeds, when, goal=target, max_feeds=max_feeds)

    def __eq__(self, other):
        return isinstance(other, Place) and other.id == self.id

    def __hash__(self):
        return hash(self.id)

    def __repr__(self):
        return f"Place({self.id}, {self.kind}, {self.name!r})"


# A place covering part of an area: the index's place, the share of the
# place inside the area, the share of the area it holds, and whether it is
# a whole part.
AreaPart = namedtuple("AreaPart", ["place", "inside", "holds", "whole"])

# A candidate at least this much inside an area may be a whole part; one
# holding at least this much of the area, a partial part.
_WHOLE_SHARE = 0.5
_PARTIAL_SHARE = 0.01


class Area:
    """The index places that cover an area (:func:`transitio.index.area`).

    ``geometry`` is the area as given, in WGS84, and ``country`` the
    ``country=`` filter or None. ``parts`` holds an :class:`AreaPart` per
    place, ordered by ``holds`` descending, then place id, and ``coverage``
    is the share of the area's land the parts cover, from 0 to 1.
    """

    def __init__(self, geometry, country, parts, coverage, index):
        self.geometry = geometry
        self.country = country
        self.parts = tuple(parts)
        self.coverage = coverage
        self._index = index

    def feeds(
        self,
        *,
        tiers=None,
        exclude=None,
        spec="gtfs",
        on_unknown="include",
        requires=None,
        categories="default",
        international=False,
    ):
        """The feeds serving the area's parts, as :class:`IndexedFeed`
        objects, each feed once.

        Each part answers as :meth:`Place.feeds` does, its kind's default
        view included, with the same arguments. A feed's ``edges`` are keyed
        by ``(place_id, tier)``, its ``selector`` unites theirs (a
        ``whole_feed`` one makes it whole, else an ``unavailable`` one makes
        it unavailable, otherwise the route ids are united) and ``service`` is its
        service in the first part it serves. The feeds come back as a
        place's view orders them.
        """
        from transitio.index.feeds import _feeds_for_places

        return _feeds_for_places(
            self._index,
            [part.place for part in self.parts],
            tiers=tiers,
            exclude=exclude,
            spec=spec,
            on_unknown=on_unknown,
            requires=requires,
            categories=categories,
            international=international,
        )

    def recommend(
        self, when=None, *, tiers=None, exclude=None, target=0.95, max_feeds=4
    ):
        """Which of the feeds serving the area's parts to use on ``when``,
        as :meth:`Place.recommend` answers for a place, each feed's
        departures and the shares other feeds run summed over the parts."""
        from transitio.index.recommend import recommend

        feeds = self.feeds(tiers=tiers, exclude=exclude, categories=None)
        return recommend(self, feeds, when, goal=target, max_feeds=max_feeds)

    def __repr__(self):
        from transitio.osm._fetch import _fmt_coord

        bounds = ", ".join(_fmt_coord(value) for value in self.geometry.bounds)
        return (
            f"Area(bounds=({bounds}), parts={len(self.parts)}, "
            f"coverage={self.coverage:.2f})"
        )


def _labels_of(record):
    """Each label a place carries, with its source and the source's rank."""
    yield record["name"], "name", _NAME
    country = record["country_code"]
    for language, text in record["names"].items():
        own = _own_language(country, language)
        yield text, language, _TRANSLATION if own else _RARE
    for text in record["aliases"]:
        yield text, "alias", _ALIAS


class _NameIndex:
    """Every label of every place, normalised and sorted, for prefix queries.

    One row per distinct normalised label per place (its best-ranked source
    winning, the first of equals), sorted by the label, with the
    place's kind, country and feed count beside it so a query ranks a slice
    without touching the records. The slice of labels starting with a prefix
    is found by two binary searches; a table of a few million rows answers
    in milliseconds.
    """

    def __init__(self, records, feed_count):
        import pyarrow as pa

        columns = {
            key: []
            for key in (
                "norm",
                "owner",
                "text",
                "source",
                "source_rank",
                "kind",
                "kind_rank",
                "country",
                "feeds",
            )
        }
        for place_id, record in records.items():
            seen = {}
            for text, source, rank in _labels_of(record):
                norm = _normalize(text)
                if norm and (norm not in seen or rank < seen[norm][2]):
                    seen[norm] = (text, source, rank)
            count = feed_count(place_id)
            for norm, (text, source, rank) in seen.items():
                columns["norm"].append(norm)
                columns["owner"].append(place_id)
                columns["text"].append(text)
                columns["source"].append(source)
                columns["source_rank"].append(rank)
                columns["kind"].append(record["kind"])
                columns["kind_rank"].append(_KIND_ORDER.get(record["kind"], 9))
                columns["country"].append(record["country_code"])
                columns["feeds"].append(count)
        types = {"source_rank": pa.int8(), "kind_rank": pa.int8(), "feeds": pa.int32()}
        table = pa.table(
            {
                key: pa.array(values, types.get(key, pa.string()))
                for key, values in columns.items()
            }
        )
        self._table = table.sort_by("norm").combine_chunks()
        self._norms = self._table["norm"].chunk(0) if table.num_rows else None

    def _lower_bound(self, key):
        """The first row whose label sorts at or after ``key``."""
        low, high = 0, self._table.num_rows
        while low < high:
            middle = (low + high) // 2
            if self._norms[middle].as_py() < key:
                low = middle + 1
            else:
                high = middle
        return low

    def query(self, prefix, *, limit, kinds=None, country=None):
        """The best ``(place_id, text, source)`` per place whose label starts
        with ``prefix``, ranked: an exact label first, then kind precedence,
        the label's source, more feeds, the label and the id."""
        import pyarrow as pa
        import pyarrow.compute as pc

        norm = _normalize(prefix)
        if not norm or self._norms is None:
            return []
        low = self._lower_bound(norm)
        part = self._table.slice(low, self._lower_bound(norm + _AFTER) - low)
        # A value set is typed, so an empty filter keeps nothing rather than
        # raising against the string column.
        if kinds is not None:
            wanted = [kinds] if isinstance(kinds, str) else list(kinds)
            wanted = pa.array(wanted, pa.string())
            part = part.filter(pc.is_in(part["kind"], value_set=wanted))
        if country is not None:
            codes = [country] if isinstance(country, str) else list(country)
            codes = pa.array(codes, pa.string())
            part = part.filter(pc.is_in(part["country"], value_set=codes))
        if part.num_rows == 0:
            return []
        part = part.append_column("exact", pc.equal(part["norm"], norm))
        part = part.sort_by(
            [
                ("exact", "descending"),
                ("kind_rank", "ascending"),
                ("source_rank", "ascending"),
                ("feeds", "descending"),
                ("norm", "ascending"),
                ("owner", "ascending"),
            ]
        )
        hits, seen = [], set()
        rows = zip(*(part[name].to_pylist() for name in ("owner", "text", "source")))
        for owner, text, source in rows:
            if owner in seen:
                continue
            seen.add(owner)
            hits.append((owner, text, source))
            if len(hits) == limit:
                break
        return hits


class _PlaceLookup:
    """Resolution over one index's places, with an optional feed-count ranker."""

    def __init__(self, places, *, feed_count=None, index=None):
        self._feed_count = feed_count or (lambda place_id: 0)
        self._index = index
        self._records = {}
        self._labels = {}
        self._children = defaultdict(list)
        self._countries = defaultdict(list)
        self._definitions = set()  # the metro definitions the index holds
        self._name_index = None  # built on the first suggestion, under the lock
        self._name_lock = threading.Lock()
        # A former id or a QID the place carries resolves to it; a real id
        # always wins over an alias of another place.
        self._aliases = {}
        for record in places.to_dict("records"):
            record["names"] = _as_dict(record.get("names"))
            for key in ("aliases", "metro_ids", "member_ids", "former_ids"):
                record[key] = _as_list(record.get(key))
            for key in (
                "name",
                "parent_id",
                "default_metro_id",
                "country_code",
                "source_subtype",
            ):
                record[key] = _as_str(record.get(key))
            place_id = record["place_id"]
            record["concordances"] = _as_concordances(record.get("concordances"))
            qid = _as_str(record.get("wikidata_id"))
            if qid is None and _QID.match(place_id):
                qid = place_id  # before schema 6 the QID is the id
            record["wikidata_id"] = qid
            qids = record["concordances"].get("wikidata", [])
            if qid is not None and qid not in qids:
                record["concordances"]["wikidata"] = [qid, *qids]
            self._records[place_id] = record
            self._labels[place_id] = self._normalized_labels(record)
            if record["parent_id"]:
                self._children[record["parent_id"]].append(place_id)
            if record["kind"] == "country" and record["country_code"]:
                self._countries[record["country_code"]].append(place_id)
            if record["kind"] == "metro" and record["source_subtype"]:
                self._definitions.add(record["source_subtype"])
            qids = record["concordances"].get("wikidata", [])
            for alias in [*record["former_ids"], *qids]:
                self._aliases.setdefault(alias, place_id)

    def _own_names(self, place_id, primary=False):
        """The normalized name of a place and its labels in its own languages
        (see ``_own_language``); its aliases and other labels left out. With
        ``primary``, only its name and its English and ``mul`` labels, the
        names it carries whatever the reader's language."""
        record = self._records[place_id]
        country = None if primary else record["country_code"]
        own = [
            text
            for language, text in record["names"].items()
            if _own_language(country, language)
        ]
        return {_normalize(text) for text in [record["name"], *own]}

    @staticmethod
    def _normalized_labels(record):
        raw = [record["name"], *record["names"].values(), *record["aliases"]]
        labels = {}
        for text in raw:
            norm = _normalize(text)
            if norm:
                labels[norm] = tuple(norm.split())
        return list(labels.items())

    def get(self, place_id):
        """The place with this id, or the one a former id or QID names."""
        record = self._records.get(place_id)
        if record is None and place_id in self._aliases:
            record = self._records[self._aliases[place_id]]
        return Place(record, self) if record is not None else None

    def children(self, place_id):
        return [self.get(cid) for cid in sorted(self._children.get(place_id, []))]

    def resolve_ids(self, place_ids):
        return [place for place in map(self.get, place_ids) if place is not None]

    def feeds(self, place, **query):
        if self._index is None:
            raise TransitioError("this lookup is not attached to an index")
        from transitio.index.feeds import feeds_for_place

        return feeds_for_place(self._index, place, **query)

    def area(self, geometry, country=None):
        """The :class:`Area` this index's places make of ``geometry`` (see
        :func:`transitio.index.area`)."""
        import numpy as np
        import shapely

        from transitio.osm._fetch import _areas_km2

        places = self._index.places
        rows = places.sindex.query(geometry, predicate="intersects")
        ids = places["place_id"].to_numpy()[rows]
        kinds = places["kind"].to_numpy()[rows]
        if country is not None:
            mine = places["country_code"].to_numpy()[rows] == country
            rows, ids, kinds = rows[mine], ids[mine], kinds[mine]
        # The candidates have a feed; every country place bounds the land.
        fed = np.array([self._feed_count(pid) > 0 for pid in ids], dtype=bool)
        candidate = np.isin(kinds, ["city", "region", "country"]) & fed
        land = kinds == "country"
        keep = candidate | land
        rows, ids, candidate, land = rows[keep], ids[keep], candidate[keep], land[keep]
        geoms = places.geometry.to_numpy()[rows]
        clips = shapely.intersection(geoms, geometry)
        aoi = np.array([geometry], dtype=object)
        sizes = _areas_km2(np.concatenate([geoms[candidate], clips[candidate], aoi]))
        ids = ids[candidate].tolist()
        own, clipped, total = np.split(sizes, [len(ids), 2 * len(ids)])
        if not total[0]:
            return Area(geometry, country, (), 0.0, self._index)
        inside = np.divide(clipped, own, out=np.zeros(len(ids)), where=own > 0)
        inside, holds = np.minimum(inside, 1.0), np.minimum(clipped / total[0], 1.0)
        share = dict(zip(ids, zip(inside.tolist(), holds.tolist())))
        above = {pid: {p.id for p in self.get(pid).ancestors} for pid in ids}
        half = {pid for pid in ids if share[pid][0] >= _WHOLE_SHARE}
        whole = {pid for pid in half if not above[pid] & half}
        partial = {
            pid
            for pid in ids
            if pid not in half
            and share[pid][1] >= _PARTIAL_SHARE
            and not above[pid] & whole
        }
        # A broad place gives way to a narrower one that is a part itself.
        partial -= set().union(*(above[pid] for pid in partial | whole))
        chosen = sorted(whole | partial, key=lambda pid: (-share[pid][1], pid))
        held = dict(zip(ids, clips[candidate]))
        covered = shapely.union_all([held[pid] for pid in chosen])
        ground = shapely.union_all([*clips[land], covered])
        covered_km2, land_km2 = _areas_km2([covered, ground])
        coverage = min(float(covered_km2 / land_km2), 1.0) if land_km2 else 0.0
        parts = [AreaPart(self.get(pid), *share[pid], pid in whole) for pid in chosen]
        return Area(geometry, country, parts, coverage, self._index)

    def _tier(self, query_norm, query_tokens, labels):
        best = 0
        query_set = set(query_tokens)
        for label_norm, label_tokens in labels:
            if label_norm == query_norm:
                return _EXACT
            if query_norm and label_norm.startswith(query_norm):
                best = max(best, _PREFIX)
            elif query_set and query_set <= set(label_tokens):
                best = max(best, _SUBSET)
        return best

    def _candidates(self, query, kind=None, definition=None):
        query_norm = _normalize(query)
        query_tokens = query_norm.split()
        scored = []
        for place_id, labels in self._labels.items():
            record = self._records[place_id]
            if kind is not None and record["kind"] != kind:
                continue
            if definition is not None and record["source_subtype"] != definition:
                continue
            tier = self._tier(query_norm, query_tokens, labels)
            if tier:
                scored.append((tier, place_id))
        scored.sort(
            key=lambda item: (
                -item[0],
                _KIND_ORDER.get(self._records[item[1]]["kind"], 9),
                -self._feed_count(item[1]),
                item[1],
            )
        )
        return scored

    def search(self, query, kind=None):
        scored = self._readings(query, kind)[0][1]
        return [self.get(place_id) for _, place_id in scored]

    def _readings(self, query, kind, definition=None):
        """The readings of the query as ``(name, scored)`` pairs, in the order
        they answer. A query without a qualifier, or one an exact match
        carries as its own name or an alias, reads as written. Otherwise
        "Name, Qualifier, ..." reads as the candidates for the name that lie
        within a place each qualifier names: a containing region or country,
        or the country its code names ("London, Ontario", "City of London,
        UK"), or any containing place when that leaves no exact match. A
        label in another language equal to the whole query answers when the
        qualified reading has no exact match, and after it when that one
        cannot decide between several."""
        scored = self._candidates(query, kind, definition)
        norm = _normalize(query)
        exact = [pid for tier, pid in scored if tier == _EXACT]
        if "," not in query or any(
            norm in self._own_names(pid) or self._has_alias(pid, norm) for pid in exact
        ):
            return [(query, scored)]
        name, *rest = query.split(",")
        qualifiers = [_normalize(part) for part in rest if _normalize(part)]
        if not _normalize(name) or not qualifiers:
            return [(query, scored)]
        candidates = self._candidates(name, kind, definition)
        for wide in (False, True):
            within = [
                (tier, pid)
                for tier, pid in candidates
                if all(self._within(pid, qualifier, wide) for qualifier in qualifiers)
            ]
            if any(tier == _EXACT for tier, _ in within):
                break
        if not exact:
            return [(name, within)]
        if all(tier != _EXACT for tier, _ in within):
            return [(query, scored)]
        return [(name, within), (query, scored)]

    def _within(self, place_id, qualifier, wide):
        """Whether a place containing ``place_id`` carries ``qualifier`` as a
        label: an ancestor that is a region or country, or the country its
        country code names; with ``wide``, any ancestor."""
        containing = [
            place.id
            for place in self.get(place_id).ancestors
            if wide or place.kind in ("region", "country")
        ]
        containing += self._countries.get(
            self._records[place_id].get("country_code"), []
        )
        return any(
            label == qualifier
            for other in containing
            for label, _ in self._labels[other]
        )

    def prepare(self):
        """Build the sorted labels once; concurrent cold calls wait for one build."""
        with self._name_lock:
            if self._name_index is None:
                self._name_index = _NameIndex(self._records, self._feed_count)
        return self._name_index

    def suggest(self, prefix, *, limit, kinds=None, country=None, lang=None):
        if not _normalize(prefix):
            return []  # nothing to match, so nothing to build yet
        table = self.prepare()
        hits = table.query(prefix, limit=limit, kinds=kinds, country=country)
        suggestions = []
        for owner, text, source in hits:
            record = self._records[owner]
            label = (record["names"].get(lang) if lang else None) or record["name"]
            label = label or owner  # a nameless row still shows something
            suggestions.append(Suggestion(Place(record, self), label, text, source))
        return suggestions

    def resolve(self, query, kind=None, definition=None):
        if definition is not None:
            if kind not in (None, "metro"):
                raise ValueError(
                    f"definition= names a metro definition, but kind={kind!r}"
                )
            if definition not in self._definitions:
                held = sorted(self._definitions, key=lambda d: (_definition_rank(d), d))
                raise ValueError(
                    f"no metro in the index is a {definition!r}; its metro "
                    f"definitions: {', '.join(map(repr, held)) or 'none'}"
                )
            kind = "metro"
        if isinstance(query, Place):
            return query
        if isinstance(query, str) and (_QID.match(query) or _OWN_ID.match(query)):
            place = self.get(query)
            if place is None:
                raise PlaceNotFoundError(f"no place with id {query!r} in the index")
            return place
        first = None
        for name, scored in self._readings(query, kind, definition):
            try:
                return self._answer(query, name, scored)
            except AmbiguousPlaceError as error:
                first = first or error
        raise first

    def _answer(self, query, name, scored):
        """The place one reading of ``query`` names, or raise."""
        if not scored:
            raise PlaceNotFoundError(f"no place matches {query!r}")
        if all(tier != _EXACT for tier, _ in scored):
            partial = [self.get(pid) for _, pid in scored]
            shown = ", ".join(repr(p) for p in partial[:_PARTIAL_SHOWN])
            if len(partial) > _PARTIAL_SHOWN:
                shown += f", and {len(partial) - _PARTIAL_SHOWN} more"
            error = PlaceNotFoundError(
                f"no place is named {query!r}; partial matches: {shown}"
            )
            error.candidates = tuple(partial)
            raise error
        return self.get(self._winner(query, scored, name))

    def _one_definition(self, scored):
        """``scored`` less the exact metro matches another definition of the
        same metro outranks. Exact matches sharing a member place, directly or
        along a chain, are one metro under several definitions and keep those
        of the earliest definition in ``_METRO_DEFINITIONS``. Shared members,
        not country codes, group them: a cross-border metro may be filed
        under a neighbour's code (Basel's FAO region under France), while
        same-named metros of different countries (Athens, US and Greece) share
        none and stay rivals."""
        records = self._records
        exact = [
            pid
            for tier, pid in scored
            if tier == _EXACT and records[pid]["kind"] == "metro"
        ]
        members = {pid: set(records[pid]["member_ids"]) for pid in exact}
        group = {}
        for start in exact:
            if start in group:
                continue
            group[start] = start
            pending = [start]
            while pending:
                current = pending.pop()
                for other in exact:
                    if other not in group and members[current] & members[other]:
                        group[other] = start
                        pending.append(other)
        rank = {pid: _definition_rank(records[pid]["source_subtype"]) for pid in exact}
        best = {}
        for pid in exact:
            best[group[pid]] = min(rank[pid], best.get(group[pid], rank[pid]))
        dropped = {pid for pid in exact if rank[pid] > best[group[pid]]}
        return [(tier, pid) for tier, pid in scored if pid not in dropped]

    def _winner(self, query, scored, name):
        exact = self._contenders(scored, name)
        populous = self._most_populous(exact, name)
        if populous is not None:
            return populous
        # A city's own metros keep every definition, so the full contest cannot
        # hand its name to one of them once the others are gone.
        namesakes, _ = self._namesakes(exact, name)
        rest = self._one_definition(
            [item for item in scored if item[1] not in namesakes]
        )
        kept = namesakes | {pid for _, pid in rest}
        scored = [item for item in scored if item[1] in kept]
        exact = self._contenders(scored, name)
        namesakes, anchors = self._namesakes(exact, name)
        winner = None
        if namesakes:
            narrowed = [pid for pid in exact if pid not in namesakes]
            winner = self._decide(narrowed, name)
            # Setting namesakes aside only lets an anchor win; any other winner
            # there would be one the full contest never chose.
            if winner is not None and winner is not _VETOED:
                if winner in anchors:
                    return winner
                winner = None
        if winner is not _VETOED:  # a veto stands; the full contest may not overturn it
            winner = self._decide(exact, name)
        if winner is not None and winner is not _VETOED:
            return winner
        candidates = [self.get(pid) for _, pid in scored]
        error = AmbiguousPlaceError(
            f"{query!r} matches several places: "
            + ", ".join(repr(c) for c in candidates)
        )
        error.candidates = tuple(candidates)
        raise error

    def _contenders(self, scored, name):
        """The exact matches, ranked, less those reaching ``name`` only through
        a label in a language not their own while another carries it as its
        own (Pinto, Spain, lists Buenos Aires in Irish). A place with an alias
        of the name stays, and so does one far better known than every
        own-name match that is not a metro, so an exonym ("Meksyk", Polish for
        Mexico) is not handed to a place in Poland of that name. Where a place
        that is not a metro carries ``name`` as a primary name (see
        ``_own_names``), one carrying it only in another of its languages
        leaves too, its aliases aside, unless far better known than every such
        place: "Bergen", Dutch for Mons, is no rival to the towns named Bergen.
        Metros, which carry their city's name in its language, stay."""
        records = self._records
        exact = [pid for tier, pid in scored if tier == _EXACT]
        norm = _normalize(name)
        own = {pid for pid in exact if norm in self._own_names(pid)}
        if not own:
            return exact
        named = [pid for pid in own if records[pid]["kind"] != "metro"]
        labels = max((len(records[pid]["names"]) for pid in named), default=0)
        exact = [
            pid
            for pid in exact
            if pid in own
            or self._has_alias(pid, norm)
            or self._far_better_known(pid, labels)
        ]
        primary = {pid for pid in named if norm in self._own_names(pid, primary=True)}
        if not primary:
            return exact
        labels = max(len(records[pid]["names"]) for pid in primary)
        return [
            pid
            for pid in exact
            if pid in primary or pid not in named or self._far_better_known(pid, labels)
        ]

    def _most_populous(self, exact, name):
        """The exact-match city carrying ``name`` as a primary name (see
        ``_own_names``) with the largest population, when that is at least
        ``_LARGE_CITY`` and more than twice that of every other exact match
        recording one; otherwise None. None, too, where another city carrying
        ``name`` as a primary name records no population and has at least
        ``_WELL_KNOWN`` labels and no fewer than the largest, since the index
        records a centre's population on its main city only (San Jose,
        California, lies in the San Francisco centre); and where a region or
        country carrying ``name`` as its own contains none of the exact-match
        cities, a rival whose population cannot be compared (Australia's
        Victoria). One containing such a city is that city's unit (Istanbul's
        province)."""
        records = self._records
        norm = _normalize(name)
        population = {pid: self.get(pid).population for pid in exact}
        primary = [
            pid
            for pid in exact
            if records[pid]["kind"] == "city"
            and norm in self._own_names(pid, primary=True)
        ]
        recorded = [pid for pid in primary if population[pid] is not None]
        if not recorded:
            return None
        top = max(recorded, key=population.get)
        size = population[top]
        if size < _LARGE_CITY or any(
            size <= 2 * population[pid]
            for pid in exact
            if pid != top and population[pid] is not None
        ):
            return None
        labels = max(_WELL_KNOWN, len(records[top]["names"]))
        if any(
            population[pid] is None and len(records[pid]["names"]) >= labels
            for pid in primary
        ):
            return None
        cities = [pid for pid in exact if records[pid]["kind"] == "city"]
        containing = {place.id for pid in cities for place in self.get(pid).ancestors}
        if any(
            records[pid]["kind"] in ("region", "country")
            and pid not in containing
            and norm in self._own_names(pid)
            for pid in exact
        ):
            return None
        return top

    def _has_alias(self, place_id, norm):
        """Whether a place carries an alias normalizing to ``norm``."""
        aliases = self._records[place_id]["aliases"]
        return any(_normalize(alias) == norm for alias in aliases)

    def _decide(self, exact, name):
        """Among the exact matches ``exact``, ranked: the sole one, or the top
        one when it beats the runner-up by the margin and no better-known
        place abroad bars it; otherwise the place ``_best_known`` finds, or
        None. ``_VETOED`` when the margin alone would decide against the name:
        it never favours a place reached only through an alias or a label in
        another language over one carrying ``name`` as its own (see
        ``_own_names``): Saint Paul, Minnesota, whose aliases include São
        Paulo, has more feeds than São Paulo itself in a thinly covered index."""
        if len(exact) < 2:
            return exact[0] if exact else None
        top_id, runner_id = exact[0], exact[1]
        top_feeds = self._feed_count(top_id)
        # The default margin: the runner-up has fewer than half the winner's
        # feeds, i.e. the winner carries strictly more than twice as many. With no
        # feed counts yet (declared edges arrive later) this never fires, so
        # genuinely tied names stay ambiguous rather than guessed.
        if top_feeds and top_feeds > 2 * self._feed_count(runner_id):
            norm = _normalize(name)
            own = {pid for pid in exact if norm in self._own_names(pid)}
            if own and top_id not in own:
                return _VETOED
            if not self._better_known_abroad(top_id, exact, name):
                return top_id
        return self._best_known(exact, name)

    def _best_known(self, exact, name):
        """The exact match, not a metro, carrying ``name`` as its name and far
        better known (``_far_better_known``) than every other exact match that
        is not a metro, in its country or abroad; None when there is none.
        Metros carry at most one label and do not count. Of Colombia's two
        cities named Cali, one feed each, the one with 129 labels is the Cali
        a reader means, not the one with 5."""
        records = self._records
        rivals = sorted(
            (pid for pid in exact if records[pid]["kind"] != "metro"),
            key=lambda pid: -len(records[pid]["names"]),
        )
        if not rivals or _normalize(records[rivals[0]]["name"]) != _normalize(name):
            return None
        labels = len(records[rivals[1]]["names"]) if len(rivals) > 1 else 0
        return rivals[0] if self._far_better_known(rivals[0], labels) else None

    def _better_known_abroad(self, top_id, exact, name):
        """Whether an exact match in another country, not a metro, carrying
        ``name`` as its name, has at least ``_WELL_KNOWN`` language labels
        and more than twice the leader's. A metro carries at most one label,
        so a metro leader counts those of the best-labelled such place in its
        own country, or none. Feed counts compare service within one
        country's coverage; labels in many languages mark a place known far
        beyond it, as Moscow, Russia, is beside Moscow, Idaho. A barred margin
        leaves the decision to ``_best_known``."""
        records = self._records
        norm = _normalize(name)
        named = [
            pid
            for pid in exact
            if records[pid]["kind"] != "metro"
            and _normalize(records[pid]["name"]) == norm
        ]
        country = records[top_id].get("country_code")
        leaders = [top_id]
        if records[top_id]["kind"] == "metro":
            leaders = [
                pid for pid in named if records[pid].get("country_code") == country
            ]
        labels = max((len(records[pid]["names"]) for pid in leaders), default=0)
        return any(
            self._far_better_known(pid, labels)
            for pid in named
            if records[pid].get("country_code") != country
        )

    def _far_better_known(self, place_id, labels):
        """Whether a place has at least ``_WELL_KNOWN`` language labels and
        more than twice ``labels``."""
        count = len(self._records[place_id]["names"])
        return count >= _WELL_KNOWN and count > 2 * labels

    def _namesakes(self, exact, name):
        """``(namesakes, anchors)``. The anchors are the exact-match cities or,
        when no exact match is a city, the exact-match regions and countries,
        which then stand as the city (Istanbul's province, the Hong Kong
        dependency). An anchor's namesakes, sharing its name because of it,
        are the metros in its country and the areas containing it whose feeds
        stay within the margin of its own. Where such an area is a city
        carrying ``name`` as its own and far better known
        (``_far_better_known``) than the anchor, the anchor is the namesake
        instead: one city recorded twice (València and its comarca), the
        better-known record standing for both. A place reaching ``name`` only
        through an alias is a namesake, never an anchor, when it lies inside a
        city carrying ``name`` as a name of its own, whether or not that city
        still competes (Puente Aranda, a district of Bogotá, lists Bogotá),
        or, not being a city, in such a city's country (New Taipei and Taiwan
        list Taipei). A metro elsewhere shares the name by coincidence
        (London, UK against London, Ontario), a containing area with far more
        service is a place of its own (New York State against New York City),
        and a city elsewhere may use an alias as its everyday name (Newcastle
        for Newcastle upon Tyne); all stay."""
        records = self._records
        norm = _normalize(name)
        own = {pid for pid in exact if norm in self._own_names(pid)}
        named = {pid for pid in own if records[pid]["kind"] == "city"}
        named_countries = {records[pid].get("country_code") for pid in named}
        named_countries.discard(None)
        aliased = {
            pid
            for pid in exact
            if pid not in own
            and self._has_alias(pid, norm)
            and (
                (
                    records[pid]["kind"] != "city"
                    and records[pid].get("country_code") in named_countries
                )
                or any(
                    place.kind == "city" and norm in self._own_names(place.id)
                    for place in self.get(pid).ancestors
                )
            )
        }
        cities = [pid for pid in exact if records[pid]["kind"] == "city"]
        if cities:
            anchors = {pid for pid in cities if pid not in aliased}
        else:
            anchors = {
                pid for pid in exact if records[pid]["kind"] in ("region", "country")
            }
        if not anchors:
            return set(), anchors
        countries = {records[pid].get("country_code") for pid in anchors}
        countries.discard(None)
        namesakes = aliased | {
            pid
            for pid in exact
            if records[pid]["kind"] == "metro"
            and records[pid].get("country_code") in countries
        }
        for anchor in anchors:
            feeds = self._feed_count(anchor)
            if not feeds:
                continue  # without feed counts the service cannot be compared
            containing = {place.id for place in self.get(anchor).ancestors}
            labels = len(records[anchor]["names"])
            for pid in exact:
                if pid not in containing or self._feed_count(pid) > 2 * feeds:
                    continue
                if pid in named and self._far_better_known(pid, labels):
                    namesakes.add(anchor)
                else:
                    namesakes.add(pid)
        return namesakes, anchors
