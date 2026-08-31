"""Place name resolution over a published index's ``places`` table.

A query — a name, a QID, or a :class:`Place` — resolves to one :class:`Place`
through a defined ranking, never a guess: the query is normalised and matched
against every place's labels and aliases in every language, candidates score on
match strength then ``kind`` precedence then feed count, and a winner is taken
only when it is the sole exact match or beats the runner-up by the ambiguity
margin. A bare city name promotes to its default metro (decision M). Anything
else raises :class:`AmbiguousPlaceError` with the candidates, or
:class:`PlaceNotFoundError`.
"""

import math
import re
import unicodedata
from collections import defaultdict

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


# Match strength, strongest first: an exact label/alias beats a prefix beats a
# query whose tokens are a subset of the label's.
_EXACT, _PREFIX, _SUBSET = 3, 2, 1

# kind precedence for the metro-default world: a metro outranks the city it
# contains, which outranks the region, which outranks the country.
_KIND_ORDER = {"metro": 0, "city": 1, "region": 2, "country": 3}

_QID = re.compile(r"\AQ[1-9][0-9]*\Z")

# Slash and middot variants that, like every dash, join whole words.
_SLASH_SEPARATORS = frozenset("/\\⁄∕·−")


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


class Place:
    """A resolved place: its identity, hierarchy, names and boundary."""

    def __init__(self, record, lookup, *, promoted_from=None):
        self._record = record
        self._lookup = lookup
        self.promoted_from = promoted_from

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
    def names(self):
        return dict(self._record.get("names") or {})

    @property
    def aliases(self):
        return list(self._record.get("aliases") or [])

    @property
    def country_code(self):
        return self._record.get("country_code")

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
    def metros(self):
        """The metros this place belongs to, resolved from ``metro_ids``."""
        return self._lookup.resolve_ids(self.metro_ids)

    @property
    def members(self):
        """The places that make up this one, resolved from ``member_ids``."""
        return self._lookup.resolve_ids(self.member_ids)

    def feeds(
        self,
        *,
        tiers=None,
        exclude=None,
        spec="gtfs",
        on_unknown="include",
        min_confidence=None,
    ):
        """The feeds serving this place, as :class:`IndexedFeed` objects.

        ``tiers`` keeps only edges of those tiers, ``exclude`` drops edges of
        the named tiers (a feed with nothing left is dropped), ``spec`` selects
        the feed kind (static GTFS by default; ``None`` for everything, a list
        to narrow), ``on_unknown`` governs unknown-tier edges and
        ``min_confidence`` filters low-confidence ones. See
        :func:`transitio.index.feeds.feeds_for_place`.
        """
        return self._lookup.feeds(
            self,
            tiers=tiers,
            exclude=exclude,
            spec=spec,
            on_unknown=on_unknown,
            min_confidence=min_confidence,
        )

    def __eq__(self, other):
        return isinstance(other, Place) and other.id == self.id

    def __hash__(self):
        return hash(self.id)

    def __repr__(self):
        return f"Place({self.id}, {self.kind}, {self.name!r})"


class _PlaceLookup:
    """Resolution over one index's places, with an optional feed-count ranker."""

    def __init__(self, places, *, feed_count=None, index=None):
        self._feed_count = feed_count or (lambda place_id: 0)
        self._index = index
        self._records = {}
        self._labels = {}
        self._children = defaultdict(list)
        for record in places.to_dict("records"):
            record["names"] = _as_dict(record.get("names"))
            for key in ("aliases", "metro_ids", "member_ids"):
                record[key] = _as_list(record.get(key))
            for key in ("name", "parent_id", "default_metro_id", "country_code"):
                record[key] = _as_str(record.get(key))
            place_id = record["place_id"]
            self._records[place_id] = record
            self._labels[place_id] = self._normalized_labels(record)
            if record["parent_id"]:
                self._children[record["parent_id"]].append(place_id)

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
        record = self._records.get(place_id)
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
        return [self.get(place_id) for _, place_id in self._candidates(query, kind)]

    def resolve(self, query, kind=None):
        if isinstance(query, Place):
            return query
        if isinstance(query, str) and _QID.match(query):
            place = self.get(query)
            if place is None:
                raise PlaceNotFoundError(f"no place with id {query!r} in the index")
            return place
        scored = self._candidates(query, kind)
        if not scored:
            raise PlaceNotFoundError(f"no place matches {query!r}")
        winner_id = self._winner(query, scored)
        winner = self.get(winner_id)
        return self._promote(winner) if kind is None else winner

    def _winner(self, query, scored):
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
        candidates = [self.get(pid) for _, pid in scored]
        error = AmbiguousPlaceError(
            f"{query!r} matches several places: "
            + ", ".join(repr(c) for c in candidates)
        )
        error.candidates = tuple(candidates)
        raise error

    def _promote(self, place):
        """A bare city name resolves to its default metro (decision M)."""
        if place.kind != "city" or not place.default_metro_id:
            return place
        metro = self.get(place.default_metro_id)
        if metro is None:
            return place
        return Place(metro._record, self, promoted_from=place.id)
