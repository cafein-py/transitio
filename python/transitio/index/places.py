"""Place name resolution over a published index's ``places`` table.

A query — a name, a QID or own ``tp_`` id (a former id or a carried QID
included), or a :class:`Place` — resolves to one :class:`Place` through a
defined ranking, never a guess: the query is normalised and matched
against every place's labels and aliases in every language, candidates score on
match strength then ``kind`` precedence then feed count, and a winner is taken
only when it is the sole exact match or beats the runner-up by the ambiguity
margin. A city's namesakes do not compete with it: a metro in its country
shares its name because it is the city's metro or named after it, and a
same-named area containing it that runs much the same service (no more than
the margin beyond the city's feeds) is the city itself. Anything else
raises :class:`AmbiguousPlaceError` with the candidates, or
:class:`PlaceNotFoundError`.
"""

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

_QID = re.compile(r"\AQ[1-9][0-9]*\Z")
# The index's own place id (schema 6); a query in this form is an id lookup.
# The registry bounds the number; the reader accepts the form.
_OWN_ID = re.compile(r"\Atp_[1-9][0-9]*\Z")

# Slash and middot variants that, like every dash, join whole words.
_SLASH_SEPARATORS = frozenset("/\\⁄∕·−")

# Where a label comes from, ranked: the primary name, a translated name, an alias.
_NAME, _TRANSLATION, _ALIAS = 0, 1, 2
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
        the relevance categories (the place kind's default view unless named;
        ``None`` for all) and ``international=True`` adds the cross-border
        feeds. See :func:`transitio.index.feeds.feeds_for_place`.
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

    def __eq__(self, other):
        return isinstance(other, Place) and other.id == self.id

    def __hash__(self):
        return hash(self.id)

    def __repr__(self):
        return f"Place({self.id}, {self.kind}, {self.name!r})"


def _labels_of(record):
    """Each label a place carries, with its source and the source's rank."""
    yield record["name"], "name", _NAME
    for language, text in record["names"].items():
        yield text, language, _TRANSLATION
    for text in record["aliases"]:
        yield text, "alias", _ALIAS


class _NameIndex:
    """Every label of every place, normalised and sorted, for prefix queries.

    One row per distinct normalised label per place (the first source in
    name, translation, alias order winning), sorted by the label, with the
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
                if norm and norm not in seen:
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
            qids = record["concordances"].get("wikidata", [])
            for alias in [*record["former_ids"], *qids]:
                self._aliases.setdefault(alias, place_id)

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

    def _candidates(self, query, kind=None):
        query_norm = _normalize(query)
        query_tokens = query_norm.split()
        scored = []
        for place_id, labels in self._labels.items():
            record = self._records[place_id]
            if kind is not None and record["kind"] != kind:
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
        return [self.get(place_id) for _, place_id in self._qualified(query, kind)]

    def _qualified(self, query, kind):
        """The candidates for ``query``: as written when a label matches it
        exactly, else, for "Name, Qualifier, ...", the candidates for the name
        that lie within a place each qualifier names — a region, a country or
        a country's code ("London, Ontario", "City of London, UK")."""
        scored = self._candidates(query, kind)
        if "," not in query or any(tier == _EXACT for tier, _ in scored):
            return scored
        name, *rest = query.split(",")
        qualifiers = [_normalize(part) for part in rest if _normalize(part)]
        if not _normalize(name) or not qualifiers:
            return scored
        return [
            (tier, place_id)
            for tier, place_id in self._candidates(name, kind)
            if all(self._within(place_id, qualifier) for qualifier in qualifiers)
        ]

    def _within(self, place_id, qualifier):
        """Whether a place containing ``place_id`` — an ancestor, or the
        country its country code names — carries ``qualifier`` as a label."""
        containing = [place.id for place in self.get(place_id).ancestors]
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

    def resolve(self, query, kind=None):
        if isinstance(query, Place):
            return query
        if isinstance(query, str) and (_QID.match(query) or _OWN_ID.match(query)):
            place = self.get(query)
            if place is None:
                raise PlaceNotFoundError(f"no place with id {query!r} in the index")
            return place
        scored = self._qualified(query, kind)
        if not scored:
            raise PlaceNotFoundError(f"no place matches {query!r}")
        return self.get(self._winner(query, scored))

    def _winner(self, query, scored):
        namesakes = self._namesakes(scored)
        if namesakes:
            winner = self._decide([item for item in scored if item[1] not in namesakes])
            # Setting namesakes aside only lets a city win; any other winner
            # there would be one the full contest never chose.
            if winner is not None and self._records[winner]["kind"] == "city":
                return winner
        winner = self._decide(scored)
        if winner is not None:
            return winner
        candidates = [self.get(pid) for _, pid in scored]
        error = AmbiguousPlaceError(
            f"{query!r} matches several places: "
            + ", ".join(repr(c) for c in candidates)
        )
        error.candidates = tuple(candidates)
        raise error

    def _decide(self, scored):
        """The sole candidate, the sole exact match, or a top candidate that
        beats the runner-up by the margin; None when none of these holds."""
        if len(scored) == 1:
            return scored[0][1]
        exact = [pid for tier, pid in scored if tier == _EXACT]
        if len(exact) == 1:
            return exact[0]
        top_id, runner_id = scored[0][1], scored[1][1]
        top_feeds = self._feed_count(top_id)
        # The default margin: the runner-up has fewer than half the winner's
        # feeds, i.e. the winner carries strictly more than twice as many. With no
        # feed counts yet (declared edges arrive later) this never fires, so
        # genuinely tied names stay ambiguous rather than guessed.
        if top_feeds and top_feeds > 2 * self._feed_count(runner_id):
            return top_id
        return None

    def _namesakes(self, scored):
        """The exact matches that share an exact-match city's name because of
        that city: the metros in its country, and the areas containing it
        whose feeds stay within the margin of the city's. A metro elsewhere
        shares the name by coincidence (London, UK against London, Ontario),
        and a containing area with far more service is a place of its own
        (New York State against New York City); both stay."""
        exact = [pid for tier, pid in scored if tier == _EXACT]
        cities = [pid for pid in exact if self._records[pid]["kind"] == "city"]
        if not cities:
            return set()
        countries = {self._records[pid].get("country_code") for pid in cities}
        countries.discard(None)
        namesakes = {
            pid
            for pid in exact
            if self._records[pid]["kind"] == "metro"
            and self._records[pid].get("country_code") in countries
        }
        for city in cities:
            feeds = self._feed_count(city)
            if not feeds:
                continue  # without feed counts the service cannot be compared
            containing = {place.id for place in self.get(city).ancestors}
            namesakes.update(
                pid
                for pid in exact
                if pid in containing and self._feed_count(pid) <= 2 * feeds
            )
        return namesakes
