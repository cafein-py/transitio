"""The read layer over a published feed index.

An index is a directory of ``feeds.parquet`` (one row per feed), an optional
``places.parquet`` (one row per place, with boundary geometry), an optional
``edges.parquet`` (one membership row per place/feed/tier) and a
``snapshot.json`` manifest. :func:`read_index` loads one and returns an
:class:`Index` exposing the manifest, the feeds as a DataFrame, the places as a
GeoDataFrame and the edges as a DataFrame (``None`` for tables the build
predates). Building an index is a maintainer step
(``scripts/build_index.py --stage publish``).

Only schema versions this transitio understands are accepted, so a newer index
is refused with a clear upgrade message rather than misread. :func:`refresh`
installs the newest published snapshot this reader supports and :func:`use`
selects among installed ones; a query given no index reads the active one.
"""

import hashlib
import io
import json
import os
import re
import stat
from pathlib import Path

from transitio.exceptions import IncompatibleIndexError, PlaceNotFoundError
from transitio.index.feeds import IndexedFeed, Selector
from transitio.index.places import Place, _PlaceLookup

__all__ = [
    "Index",
    "IndexedFeed",
    "Place",
    "Selector",
    "read_index",
    "place",
    "places",
    "refresh",
    "use",
    "installed",
    "SUPPORTED_SCHEMA_VERSIONS",
    "MIN_READER_VERSIONS",
    "DISCOVERY_SEMANTICS_VERSION",
]

# The index schema versions this reader understands. A snapshot outside the set
# is refused rather than read against columns that may have moved.
SUPPORTED_SCHEMA_VERSIONS = frozenset({3})

# The oldest transitio that reads each schema version: what a snapshot records
# as its reader floor, fixed per schema rather than taken from the build.
MIN_READER_VERSIONS = {3: "0.11.0"}

# Bumped whenever name resolution, ranking, promotion or filtering changes:
# the snapshot pins the data, this pins how the reader interprets it, and a
# result that records both (with the transitio version) is reproducible.
DISCOVERY_SEMANTICS_VERSION = 1

FEEDS_FILE = "feeds.parquet"
PLACES_FILE = "places.parquet"
EDGES_FILE = "edges.parquet"
SNAPSHOT_FILE = "snapshot.json"

# Ceilings on what one index file may be, so a swapped-in or damaged file cannot
# read an unbounded amount into memory. A real index is a few MB.
_MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
_MAX_FEEDS_BYTES = 512 * 1024 * 1024
_MAX_PLACES_BYTES = 512 * 1024 * 1024
_MAX_EDGES_BYTES = 512 * 1024 * 1024

# The columns a schema_version 3 feeds table carries. A correctly-hashed but
# structurally wrong Parquet is refused against this rather than misread later.
_SCHEMA_COLUMNS = frozenset(
    {
        "feed_id",
        "onestop_id",
        "mdb_id",
        "id_minted",
        "source",
        "spec",
        "name",
        "aliases",
        "crosswalk_method",
        "crosswalk_confidence",
        "static_feed_id",
        "static_link_method",
        "atlas",
        "mdb",
        "gbfs",
        "crawlable",
        "uncrawlable_reason",
        "coverage_source",
        "coverage",
        "stop_count",
        "etag",
        "last_modified",
        "last_crawled",
        "crawl_status",
        "snapshot",
    }
)

# The columns a schema_version 3 edges table carries.
_EDGES_COLUMNS = frozenset(
    {
        "place_id",
        "feed_id",
        "tier",
        "service",
        "tier_confidence",
        "method",
        "rehomed_from",
        "evidence",
        "curation",
        "merged_evidence",
        "curation_history",
        "classification_fingerprint",
        "fingerprint_kind",
        "selector_state",
        "selector",
        "needs_review",
        "snapshot",
    }
)

# The columns a schema_version 3 places table carries, geometry included.
_PLACES_COLUMNS = frozenset(
    {
        "place_id",
        "kind",
        "source_subtype",
        "name",
        "names",
        "aliases",
        "default_metro_id",
        "resolution_method",
        "curated",
        "parent_id",
        "metro_ids",
        "member_ids",
        "country_code",
        "overture_id",
        "osm_relation_id",
        "statistical_area_id",
        "geonames_id",
        "geometry_source",
        "service",
        "snapshot",
        "geometry",
    }
)


def _load_table(read, data, path, table):
    """Read Parquet bytes into a frame, or refuse with a controlled error.

    A correctly-hashed but unreadable table — a duplicated column label, a
    truncated page — otherwise escapes as a raw Arrow exception.
    """
    try:
        return read(io.BytesIO(data))
    except Exception as error:
        raise IncompatibleIndexError(f"{path}: not a readable {table} table ({error})")


def _check_columns(frame, expected, path, version, table):
    """Refuse a table whose column labels are not exactly ``expected``."""
    columns = set(frame.columns)
    if columns != expected:
        raise IncompatibleIndexError(
            f"{path}: {table} columns do not match schema_version {version} "
            f"(missing {sorted(expected - columns)}, "
            f"unexpected {sorted(columns - expected)})"
        )


def _check_snapshot_column(frame, snapshot, path, table):
    """Every row's ``snapshot`` must equal the manifest's ``snapshot_id``.

    The rows surface the id through the public API, so a divergence would report
    two different snapshots for one index.
    """
    if not (frame["snapshot"] == snapshot["snapshot_id"]).all():
        raise IncompatibleIndexError(
            f"{path}: {table} rows carry a snapshot other than the manifest's "
            f"snapshot_id"
        )


def _read_regular(path, limit):
    """The bytes of ``path``, refusing a symlink, special file, or over-size read.

    Opened ``O_NOFOLLOW`` where the platform has it, then checked by ``fstat`` on
    the open descriptor rather than a separate ``lstat`` on the name, so a swap
    between the check and the read cannot slip a symlink or special file past it.
    ``O_NONBLOCK`` keeps a FIFO named in place of a file from blocking the open
    until a writer appears — it opens, is seen not to be regular, and is refused.
    Windows lacks ``O_NOFOLLOW`` and follows a symlink here — its symlinks need
    privilege to create, the residual the build store also accepts.
    """
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        handle = os.open(path, flags)
    except OSError as error:
        raise IncompatibleIndexError(f"{path}: cannot read ({error.strerror})")
    try:
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode):
            raise IncompatibleIndexError(f"{path}: not a regular file")
        with os.fdopen(handle, "rb", closefd=False) as opened:
            data = opened.read(limit + 1)
        if len(data) > limit:
            raise IncompatibleIndexError(f"{path}: over the {limit}-byte ceiling")
        return data
    finally:
        os.close(handle)


class Index:
    """A resolved index: its manifest, feeds, and (if present) places and edges."""

    def __init__(self, snapshot, feeds, places=None, edges=None):
        self.snapshot = snapshot
        self.feeds = feeds
        self.places = places
        self.edges = edges

    @property
    def snapshot_id(self):
        return self.snapshot["snapshot_id"]

    @property
    def schema_version(self):
        return self.snapshot["schema_version"]

    @property
    def discovery_semantics_version(self):
        """The discovery semantics the snapshot was built under."""
        return self.snapshot.get("discovery_semantics_version")

    def __repr__(self):
        places = "None" if self.places is None else len(self.places)
        edges = "None" if self.edges is None else len(self.edges)
        return (
            f"<Index snapshot_id={self.snapshot_id!r} feeds={len(self.feeds)} "
            f"places={places} edges={edges} "
            f"schema_version={self.schema_version}>"
        )


def read_index(path):
    """Read the index at ``path``, or raise if it is unsupported or corrupt.

    ``pandas`` (and its ``pyarrow`` Parquet engine, a required dependency) reads
    ``feeds.parquet``. The manifest is read first, so an incompatible snapshot
    is refused before the larger file is touched, and the Parquet's bytes are
    checked against the ``feeds_sha256`` the manifest records. That match proves
    the Parquet and manifest are the paired halves of one build — not that the
    build is authentic; authenticating a downloaded snapshot against its release
    checksum is a later addition. Each file is read as a size-bounded regular
    file, so a symlinked or over-large one is refused rather than followed. The
    manifest's ``snapshot_id`` and the Parquet's columns are checked against the
    schema version, so a structurally wrong but correctly-hashed index is refused
    rather than misread downstream.
    """
    import pandas

    path = Path(path)
    snapshot = json.loads(
        _read_regular(path / SNAPSHOT_FILE, _MAX_SNAPSHOT_BYTES).decode("utf-8")
    )
    version = snapshot.get("schema_version")
    # A real int only: bool is an int subclass and ``True == 1``, and ``1.0``
    # also equals ``1``, so a malformed version must not slip through.
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version not in SUPPORTED_SCHEMA_VERSIONS
    ):
        raise IncompatibleIndexError(
            f"feed index schema_version {version!r} is not one this transitio "
            f"reads ({sorted(SUPPORTED_SCHEMA_VERSIONS)}); upgrade transitio"
        )
    _check_reader_range(snapshot, path)
    if not isinstance(snapshot.get("snapshot_id"), str):
        raise IncompatibleIndexError(
            f"{path / SNAPSHOT_FILE}: manifest declares no snapshot_id"
        )
    data = _read_regular(path / FEEDS_FILE, _MAX_FEEDS_BYTES)
    expected = snapshot.get("feeds_sha256")
    if not isinstance(expected, str):
        raise IncompatibleIndexError(
            f"{path / SNAPSHOT_FILE}: manifest declares no feeds_sha256"
        )
    if hashlib.sha256(data).hexdigest() != expected:
        raise IncompatibleIndexError(
            f"{path / FEEDS_FILE}: does not match the snapshot's feeds_sha256"
        )
    feeds = _load_table(pandas.read_parquet, data, path / FEEDS_FILE, "feeds")
    _check_columns(feeds, _SCHEMA_COLUMNS, path / FEEDS_FILE, version, "feeds")
    _check_snapshot_column(feeds, snapshot, path / FEEDS_FILE, "feeds")
    return Index(
        snapshot,
        feeds,
        _read_places(path, snapshot, version),
        _read_edges(path, snapshot, version),
    )


def _read_places(path, snapshot, version):
    """The places GeoDataFrame, or None when the index carries no places.

    Read only when the manifest declares a ``places_sha256``; the Parquet is then
    a size-bounded regular file, its bytes checked against that digest, and its
    columns against the schema before it is returned.
    """
    expected = snapshot.get("places_sha256")
    if expected is None:
        return None
    if not isinstance(expected, str):
        raise IncompatibleIndexError(
            f"{path / SNAPSHOT_FILE}: places_sha256 is not a string"
        )
    import geopandas

    data = _read_regular(path / PLACES_FILE, _MAX_PLACES_BYTES)
    if hashlib.sha256(data).hexdigest() != expected:
        raise IncompatibleIndexError(
            f"{path / PLACES_FILE}: does not match the snapshot's places_sha256"
        )
    places = _load_table(geopandas.read_parquet, data, path / PLACES_FILE, "places")
    _check_columns(places, _PLACES_COLUMNS, path / PLACES_FILE, version, "places")
    _check_snapshot_column(places, snapshot, path / PLACES_FILE, "places")
    return places


def _read_edges(path, snapshot, version):
    """The edges DataFrame, or None when the index carries no edges.

    Read only when the manifest declares an ``edges_sha256``; the Parquet is then
    a size-bounded regular file, its bytes checked against that digest, and its
    columns against the schema before it is returned.
    """
    expected = snapshot.get("edges_sha256")
    if expected is None:
        return None
    if not isinstance(expected, str):
        raise IncompatibleIndexError(
            f"{path / SNAPSHOT_FILE}: edges_sha256 is not a string"
        )
    import pandas

    data = _read_regular(path / EDGES_FILE, _MAX_EDGES_BYTES)
    if hashlib.sha256(data).hexdigest() != expected:
        raise IncompatibleIndexError(
            f"{path / EDGES_FILE}: does not match the snapshot's edges_sha256"
        )
    edges = _load_table(pandas.read_parquet, data, path / EDGES_FILE, "edges")
    _check_columns(edges, _EDGES_COLUMNS, path / EDGES_FILE, version, "edges")
    _check_snapshot_column(edges, snapshot, path / EDGES_FILE, "edges")
    return edges


# Version components are bounded: the manifest is an untrusted input and an
# unbounded digit run would be a slow or refused int() rather than a version.
_VERSION = re.compile(
    r"(?P<release>\d{1,9}(?:\.\d{1,9}){0,7})"
    r"(?:[-.]?(?P<pre>a|b|rc|alpha|beta|dev)\.?(?P<pre_n>\d{0,9}))?"
    r"(?:\.post(?P<post>\d{1,9}))?"
    r"(?:\+[0-9A-Za-z.]{1,64})?"
)
_MAX_VERSION_LENGTH = 128
# Pre-release labels in their standard order, below a final release.
_PRE_RANK = {"dev": 0, "a": 1, "alpha": 1, "b": 2, "beta": 2, "rc": 3}
_FINAL = 4


def _version_key(version):
    """A comparable key for a version, or None when it is not one: the
    release numbers, then finals above pre-releases, then the post number."""
    text = str(version).strip()
    match = _VERSION.fullmatch(text) if len(text) <= _MAX_VERSION_LENGTH else None
    if match is None:
        return None
    release = tuple(int(part) for part in match.group("release").split("."))
    while len(release) > 1 and release[-1] == 0:
        release = release[:-1]
    if match.group("pre") is None:
        pre = (_FINAL, 0)
    else:
        pre = (_PRE_RANK[match.group("pre")], int(match.group("pre_n") or 0))
    return (release, pre, int(match.group("post") or 0))


def _check_reader_range(snapshot, path):
    """A schema-3 manifest names the discovery semantics it was built under
    and the transitio version that introduced its schema; a reader older
    than that refuses rather than misreads, and a manifest without them is
    incomplete."""
    semantics = snapshot.get("discovery_semantics_version")
    if not isinstance(semantics, int) or isinstance(semantics, bool):
        raise IncompatibleIndexError(
            f"{path / SNAPSHOT_FILE}: manifest declares no discovery_semantics_version"
        )
    minimum = snapshot.get("min_reader_version")
    if not isinstance(minimum, str) or _version_key(minimum) is None:
        raise IncompatibleIndexError(
            f"{path / SNAPSHOT_FILE}: manifest declares no min_reader_version"
        )
    from transitio import __version__

    current = _version_key(__version__)
    if current is None:
        # Fail closed: a reader that cannot place itself cannot vouch for
        # its compatibility.
        raise IncompatibleIndexError(
            f"this transitio's version {__version__!r} cannot be compared with "
            f"the index's min_reader_version {minimum!r}"
        )
    if current < _version_key(minimum):
        raise IncompatibleIndexError(
            f"feed index needs transitio >= {minimum} (this is {__version__}); "
            "upgrade transitio"
        )


def _coerce_index(index):
    """The index a query reads: the one given (an :class:`Index` or a path),
    else the active installed snapshot, resolved lazily."""
    if index is None:
        from transitio.index._refresh import active_index

        return active_index()
    if isinstance(index, Index):
        return index
    return read_index(index)


def _feed_count_for(index):
    """A place_id -> distinct-feed-count callable over the index's edges."""
    if index.edges is None:
        return None
    counts = index.edges.groupby("place_id")["feed_id"].nunique().to_dict()
    return lambda place_id: counts.get(place_id, 0)


def _lookup_for(index):
    lookup = getattr(index, "_place_lookup", None)
    if lookup is None:
        if index.places is None:
            raise PlaceNotFoundError("this index carries no places")
        lookup = _PlaceLookup(
            index.places, feed_count=_feed_count_for(index), index=index
        )
        index._place_lookup = lookup
    return lookup


def place(query, *, kind=None, index=None):
    """Resolve ``query`` to a single :class:`Place`, or raise.

    ``query`` is a name, a QID, or a :class:`Place`. A bare city name promotes to
    its default metro; ``kind`` pins the scope and suppresses promotion. Raises
    :class:`~transitio.exceptions.PlaceNotFoundError` or
    :class:`~transitio.exceptions.AmbiguousPlaceError`.
    """
    return _lookup_for(_coerce_index(index)).resolve(query, kind=kind)


def places(query, *, index=None):
    """The places matching ``query``, ranked best first (never promoted)."""
    return _lookup_for(_coerce_index(index)).search(query)


from transitio.index._refresh import installed, refresh, use  # noqa: E402
