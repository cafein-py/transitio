"""The read layer over a published feed index.

Before schema 7 an index is a directory of ``feeds.parquet`` (one row per feed), an optional
``places.parquet`` (one row per place, with boundary geometry), an optional
``edges.parquet`` (one membership row per place/feed/tier) and a
``snapshot.json`` manifest. :func:`read_index` loads one and returns an
:class:`Index` exposing the manifest, the feeds as a DataFrame, the places as a
GeoDataFrame and the edges as a DataFrame (``None`` for tables the build
predates). Building an index is a maintainer step
(the transitio-index build's publish stage).

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
    "load",
    "links",
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
SUPPORTED_SCHEMA_VERSIONS = frozenset({4, 5, 6, 7, 8, 9})

# The oldest transitio that reads each schema version: what a snapshot records
# as its reader floor, fixed per schema rather than taken from the build.
# Schema 5 adds the per-feed ``files`` manifest and schema 6 keys places by
# their own id, with the QID beside it; all three ship first in 0.11.0.
# Schema 7 (partitions), 8 (the GTFS-RT companion table) and 9 (feed service
# spans and place validity) ship together.
MIN_READER_VERSIONS = {
    4: "0.11.0",
    5: "0.11.0",
    6: "0.11.0",
    7: "0.12.0",
    8: "0.12.0",
    9: "0.12.0",
}

# Bumped whenever name resolution, ranking, promotion or filtering changes:
# the snapshot pins the data, this pins how the reader interprets it, and a
# result that records both (with the transitio version) is reproducible.
DISCOVERY_SEMANTICS_VERSION = 1

FEEDS_FILE = "feeds.parquet"
REALTIME_FILE = "realtime.parquet"
PLACES_FILE = "places.parquet"
EDGES_FILE = "edges.parquet"
SNAPSHOT_FILE = "snapshot.json"
# Schema 7 is a directory of partitions: one per country code (its feeds by
# home country, its places, their domestic edges), ``international`` (the
# feeds without a home country) and ``links`` (every cross-border edge, with
# ``feed_partition`` naming the partition holding the feed).
# Schema 8 adds ``realtime.parquet`` beside a partition's feeds: the GTFS-RT
# companions of its static feeds, keyed by ``static_feed_id``; the
# ``international`` partition also holds the companions without one.
INTERNATIONAL_PARTITION = "international"
LINKS_PARTITION = "links"
_TABLE_FILES = {
    "feeds": FEEDS_FILE,
    "realtime": REALTIME_FILE,
    "places": PLACES_FILE,
    "edges": EDGES_FILE,
}
_PARTITION_NAME = re.compile(r"[A-Z]{2}|international|links")
# The tables each partition kind may carry; a country partition any of
# them. More partitions than countries, or more listed rows than one table
# may hold, is refused before anything is read.
_PARTITION_TABLES = {
    INTERNATIONAL_PARTITION: {"feeds", "realtime"},
    LINKS_PARTITION: {"edges"},
}
_MAX_PARTITIONS = 300

# Ceilings on what one index file may be, so a swapped-in or damaged file cannot
# read an unbounded amount into memory. A real index is a few MB.
_MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
_MAX_FEEDS_BYTES = 512 * 1024 * 1024
_MAX_PLACES_BYTES = 512 * 1024 * 1024
_MAX_EDGES_BYTES = 512 * 1024 * 1024

# The columns a schema_version 4 feeds table carries. A correctly-hashed but
# structurally wrong Parquet is refused against this rather than misread later.
# Schema 5 adds the ``files`` manifest; the check is keyed on the snapshot's
# version below, so a shipped schema-4 index still reads unchanged.
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
        "redistribution_allowed",
        "snapshot",
    }
)

# The feeds columns per schema version: schema 5 adds the ``files`` manifest,
# and schema 6 (which changes only the places table) keeps them.
_FEEDS_COLUMNS = {
    4: _SCHEMA_COLUMNS,
    5: _SCHEMA_COLUMNS | {"files"},
    6: _SCHEMA_COLUMNS | {"files"},
    # Schema 7 adds the classify stage's country fields.
    7: _SCHEMA_COLUMNS
    | {"files", "home_country", "country_shares", "scope", "declared_countries"},
}
# Schema 8: GTFS only, so the GBFS block goes; each static feed names its
# GTFS-RT companions, which ride in the realtime table.
_FEEDS_COLUMNS[8] = (_FEEDS_COLUMNS[7] - {"gbfs"}) | {"realtime_feed_ids"}
# Schema 9: the first and last date the feed's services run.
_FEEDS_COLUMNS[9] = _FEEDS_COLUMNS[8] | {"service_start", "service_end"}
_REALTIME_COLUMNS = frozenset(
    {
        "feed_id",
        "onestop_id",
        "mdb_id",
        "id_minted",
        "source",
        "name",
        "aliases",
        "crosswalk_method",
        "crosswalk_confidence",
        "static_feed_id",
        "static_link_method",
        "urls",
        "entity_types",
        "atlas",
        "mdb",
        "redistribution_allowed",
        "snapshot",
    }
)

# The columns an edges table carries, unchanged from schema_version 4 through 5.
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

# The columns a places table carries, geometry included; unchanged from
# schema_version 4 through 5. Schema 6 keys ``place_id`` by the index's own id
# and adds the nullable ``wikidata_id``, the ``concordances`` block of ids per
# namespace and the ``former_ids`` an alias row was merged from.
_PLACES_SCHEMA_COLUMNS = frozenset(
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
_PLACES_COLUMNS = {
    4: _PLACES_SCHEMA_COLUMNS,
    5: _PLACES_SCHEMA_COLUMNS,
    6: _PLACES_SCHEMA_COLUMNS | {"wikidata_id", "concordances", "former_ids"},
}
_PLACES_COLUMNS[7] = _PLACES_COLUMNS[6]
_PLACES_COLUMNS[8] = _PLACES_COLUMNS[6]
# Schema 9: the validity of the place's feeds and their overlap.
_PLACES_COLUMNS[9] = _PLACES_COLUMNS[6] | {"validity"}
# Schema 7 edges carry the rank stage's relevance; the links table also names
# the partition holding each edge's feed.
_RELEVANCE_COLUMNS = frozenset({"relevance_category", "relevance", "cross_border"})
_EDGES_COLUMNS_BY_VERSION = {
    version: _EDGES_COLUMNS for version in SUPPORTED_SCHEMA_VERSIONS if version < 7
}
_EDGES_COLUMNS_BY_VERSION[7] = _EDGES_COLUMNS | _RELEVANCE_COLUMNS
_EDGES_COLUMNS_BY_VERSION[8] = _EDGES_COLUMNS_BY_VERSION[7]
_EDGES_COLUMNS_BY_VERSION[9] = _EDGES_COLUMNS_BY_VERSION[7]
_LINKS_COLUMNS = _EDGES_COLUMNS_BY_VERSION[7] | {"feed_partition"}


# What a table may declare before it is materialised: the on-disk ceiling
# bounds the file, not the memory a highly compressed file expands into.
_MAX_TABLE_ROWS = 20_000_000
_MAX_TABLE_ROW_GROUPS = 10_000
_MAX_TABLE_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024


def _load_table(read, data, path, table):
    """Read Parquet bytes into a frame, or refuse with a controlled error.

    The file's own metadata is checked first — rows, row groups and the
    uncompressed size it declares — so a small file that would expand far
    past the on-disk ceiling is refused before anything is allocated. A
    correctly-hashed but unreadable table — a duplicated column label, a
    truncated page — otherwise escapes as a raw Arrow exception.
    """
    import pyarrow.parquet

    try:
        metadata = pyarrow.parquet.ParquetFile(io.BytesIO(data)).metadata
        declared = sum(
            metadata.row_group(i).total_byte_size
            for i in range(metadata.num_row_groups)
        )
    except Exception as error:
        raise IncompatibleIndexError(f"{path}: not a readable {table} table ({error})")
    if (
        metadata.num_rows > _MAX_TABLE_ROWS
        or metadata.num_row_groups > _MAX_TABLE_ROW_GROUPS
        or declared > _MAX_TABLE_UNCOMPRESSED_BYTES
    ):
        raise IncompatibleIndexError(
            f"{path}: the {table} table declares more than this reader loads "
            f"({metadata.num_rows} rows, {metadata.num_row_groups} row groups, "
            f"{declared} uncompressed bytes)"
        )
    try:
        return read(io.BytesIO(data))
    except Exception as error:
        raise IncompatibleIndexError(f"{path}: not a readable {table} table ({error})")


def _check_columns(frame, expected, path, version, table):
    """Refuse a table whose column labels are not exactly ``expected``, each
    once: a duplicated label would reach callers as a two-column frame."""
    if not frame.columns.is_unique:
        raise IncompatibleIndexError(f"{path}: {table} has duplicate columns")
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
    # A null never equals the id, and ``all()`` must not skip it as missing.
    matches = frame["snapshot"] == snapshot["snapshot_id"]
    if matches.isna().any() or not bool(matches.all()):
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
    Windows lacks ``O_NOFOLLOW``, so there the name is checked for a reparse
    point (a symlink or junction) before the open; the window between that
    check and the open is the residual the build store also accepts.
    """
    if os.name == "nt":
        try:
            attributes = getattr(os.lstat(path), "st_file_attributes", 0)
        except OSError as error:
            raise IncompatibleIndexError(f"{path}: cannot read ({error.strerror})")
        if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise IncompatibleIndexError(f"{path}: not a regular file")
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
    """A resolved index: its manifest, feeds, and (if present) places and edges.

    A schema-7 index read whole joins every partition into the flat tables;
    read for one ``country`` it holds that partition alone. ``links`` is the
    cross-border edge table (with ``feed_partition``), None before schema 7.
    ``realtime`` is the GTFS-RT companion table of schema 8 (the whole
    index's, or the country's), None before it.
    """

    def __init__(
        self,
        snapshot,
        feeds,
        places=None,
        edges=None,
        *,
        links=None,
        country=None,
        path=None,
        realtime=None,
    ):
        self.snapshot = snapshot
        self.feeds = feeds
        self.places = places
        self.edges = edges
        self.links = links
        self.realtime = realtime
        self.country = country
        self._path = None if path is None else Path(path)
        self._partition_tables = {}

    @property
    def partitions(self):
        """The partition listing of a schema-7 manifest, else an empty dict."""
        return self.snapshot.get("partitions") or {}

    def _partition_table(self, partition, table):
        """One partition table read from disk once and kept for the index's
        lifetime; None when the partition lists no such table."""
        key = (partition, table)
        if key not in self._partition_tables:
            if self._path is None or table not in self.partitions.get(partition, {}):
                self._partition_tables[key] = None
            else:
                self._partition_tables[key] = _read_partition_table(
                    self._path, self.snapshot, partition, table
                )
        return self._partition_tables[key]

    def feeds_in(self, partition):
        """The feeds table of one partition of a schema-7 index read from
        disk — the feeds a link edge's ``feed_partition`` refers to — read
        once and kept for the index's lifetime."""
        feeds = self._partition_table(partition, "feeds")
        if feeds is None:
            raise IncompatibleIndexError(
                f"this index has no feeds partition {partition!r}"
            )
        return feeds

    def realtime_in(self, partition):
        """The realtime table of one partition of a schema-8 index (the
        companions of the feeds held there), read once; None when the
        partition has none."""
        return self._partition_table(partition, "realtime")

    def realtime_unlinked(self):
        """The GTFS-RT companions the index could not tie to a static feed:
        the rows of the ``international`` realtime table naming no feed, or a
        feed the index does not carry. A companion of a static feed sits in
        that feed's partition, so the check is against the partition's own
        feeds. None before schema 8."""
        if self.schema_version < 8:
            return None
        if self.country is None:
            table, feeds = self.realtime, self.feeds
        else:
            table = self.realtime_in(INTERNATIONAL_PARTITION)
            feeds = self._partition_table(INTERNATIONAL_PARTITION, "feeds")
        if table is None:
            return self.realtime.iloc[0:0]
        known = set() if feeds is None else set(feeds["feed_id"])
        static = table["static_feed_id"]
        return table[static.isna() | ~static.isin(known)].reset_index(drop=True)

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


def read_index(path, *, country=None):
    """Read the index at ``path``, or raise if it is unsupported or corrupt.

    A schema-7 index is a directory of partitions: without ``country`` every
    partition is read and joined into the flat feeds, places and edges tables
    (the cross-border edges included, so nothing a flat index carried is
    lost), and the ``links`` table is kept beside them; with ``country`` the
    result holds that partition's feeds, places and domestic edges, and the
    links whose place lies in it. ``country`` is refused for an older schema.
    A schema-8 index also carries its GTFS-RT companions in ``realtime``
    (joined, or the country's own).

    ``pandas`` (and its ``pyarrow`` Parquet engine, a required dependency) reads
    ``feeds.parquet``. The manifest is read first, so an incompatible snapshot
    is refused before the larger file is touched, and the Parquet's bytes are
    checked against the ``feeds_sha256`` the manifest records. That match proves
    the Parquet and manifest are the paired halves of one build — not that the
    build is authentic; :func:`refresh` additionally checks a downloaded archive
    against the digest its release manifest declares. Each file is read as a
    size-bounded regular
    file, so a symlinked or over-large one is refused rather than followed. The
    manifest's ``snapshot_id`` and the Parquet's columns are checked against the
    schema version, so a structurally wrong but correctly-hashed index is refused
    rather than misread downstream.
    """
    import pandas

    # Absolute, so the lazy partition reads of a country load stay put when
    # the working directory moves.
    path = Path(path).resolve()
    snapshot = _read_manifest(path)
    version = snapshot["schema_version"]
    if version >= 7:
        return _read_partitioned(path, snapshot, version, country)
    if country is not None:
        raise ValueError(f"schema_version {version} has no country partitions")
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
    _check_columns(feeds, _FEEDS_COLUMNS[version], path / FEEDS_FILE, version, "feeds")
    _check_snapshot_column(feeds, snapshot, path / FEEDS_FILE, "feeds")
    return Index(
        snapshot,
        feeds,
        _read_places(path, snapshot, version),
        _read_edges(path, snapshot, version),
    )


def _read_manifest(path):
    """The manifest at ``path``, refused unless it is a JSON object of a
    supported schema within this reader's range and names its snapshot."""
    try:
        snapshot = json.loads(
            _read_regular(path / SNAPSHOT_FILE, _MAX_SNAPSHOT_BYTES).decode("utf-8")
        )
    except ValueError as error:
        raise IncompatibleIndexError(
            f"{path / SNAPSHOT_FILE}: not a JSON manifest: {error}"
        ) from error
    if not isinstance(snapshot, dict):
        raise IncompatibleIndexError(f"{path / SNAPSHOT_FILE}: not a JSON object")
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
    return snapshot


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
    _check_columns(
        places, _PLACES_COLUMNS[version], path / PLACES_FILE, version, "places"
    )
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


def _partitions(snapshot, path):
    """The manifest's partition listing, checked for shape: partition names
    of the layout, each table an object with a digest."""
    listing = snapshot.get("partitions")
    if not isinstance(listing, dict) or not listing:
        raise IncompatibleIndexError(
            f"{path / SNAPSHOT_FILE}: manifest declares no partitions"
        )
    if len(listing) > _MAX_PARTITIONS:
        raise IncompatibleIndexError(
            f"{path / SNAPSHOT_FILE}: manifest lists more than {_MAX_PARTITIONS} "
            "partitions"
        )
    rows = {table: 0 for table in _TABLE_FILES}
    # The realtime table arrives with schema 8; an older snapshot listing
    # one is outside its layout.
    known = set(_TABLE_FILES)
    if snapshot["schema_version"] < 8:
        known.discard("realtime")
    for name, tables in listing.items():
        if not isinstance(name, str) or not _PARTITION_NAME.fullmatch(name):
            raise IncompatibleIndexError(
                f"{path / SNAPSHOT_FILE}: unexpected partition name {name!r}"
            )
        allowed = _PARTITION_TABLES.get(name, known) & known
        if (
            not isinstance(tables, dict)
            or not tables
            or not all(
                table in allowed
                and isinstance(entry, dict)
                and isinstance(entry.get("sha256"), str)
                and isinstance(entry.get("rows"), int)
                and not isinstance(entry.get("rows"), bool)
                and entry["rows"] >= 0
                for table, entry in tables.items()
            )
        ):
            raise IncompatibleIndexError(
                f"{path / SNAPSHOT_FILE}: partition {name!r} lists tables outside "
                "the layout"
            )
        for table, entry in tables.items():
            rows[table] += entry["rows"]
    if any(total > _MAX_TABLE_ROWS for total in rows.values()):
        raise IncompatibleIndexError(
            f"{path / SNAPSHOT_FILE}: the partitions list more than "
            f"{_MAX_TABLE_ROWS} rows of one table"
        )
    return listing


def _partition_directory(path, partition):
    """The partition's directory, refused when it is not a plain directory
    (a symlink or junction would lead the read outside the index)."""
    directory = path / partition
    try:
        info = os.lstat(directory)
    except OSError as error:
        raise IncompatibleIndexError(f"{directory}: cannot read ({error.strerror})")
    reparse = getattr(info, "st_file_attributes", 0) & getattr(
        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
    )
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or reparse:
        raise IncompatibleIndexError(f"{directory}: not a plain directory")
    return directory


def _read_partition_table(path, snapshot, partition, table):
    """One partition table, checked against its digest, columns and snapshot."""
    import pandas

    version = snapshot["schema_version"]
    listing = _partitions(snapshot, path)
    file = _partition_directory(path, partition) / _TABLE_FILES[table]
    entry = listing[partition][table]
    limits = {
        "feeds": _MAX_FEEDS_BYTES,
        "realtime": _MAX_FEEDS_BYTES,
        "places": _MAX_PLACES_BYTES,
    }
    data = _read_regular(file, limits.get(table, _MAX_EDGES_BYTES))
    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise IncompatibleIndexError(f"{file}: does not match the snapshot's sha256")
    if table == "places":
        import geopandas

        frame = _load_table(geopandas.read_parquet, data, file, table)
        columns = _PLACES_COLUMNS[version]
    else:
        frame = _load_table(pandas.read_parquet, data, file, table)
        if table == "feeds":
            columns = _FEEDS_COLUMNS[version]
        elif table == "realtime":
            columns = _REALTIME_COLUMNS
        elif partition == LINKS_PARTITION:
            columns = _LINKS_COLUMNS
        else:
            columns = _EDGES_COLUMNS_BY_VERSION[version]
    _check_columns(frame, columns, file, version, table)
    _check_snapshot_column(frame, snapshot, file, table)
    if len(frame) != entry["rows"]:
        raise IncompatibleIndexError(
            f"{file}: {len(frame)} rows where the snapshot lists {entry['rows']}"
        )
    return frame


def _concat(frames, geo=False):
    import pandas

    if not frames:
        return None
    joined = pandas.concat(frames, ignore_index=True)
    if geo:
        import geopandas

        return geopandas.GeoDataFrame(joined, geometry="geometry", crs=frames[0].crs)
    return joined


def _read_partitioned(path, snapshot, version, country):
    """A schema-7 index: every partition joined, or one country's."""
    listing = _partitions(snapshot, path)
    if country is not None:
        if country not in listing or country in (
            INTERNATIONAL_PARTITION,
            LINKS_PARTITION,
        ):
            raise IncompatibleIndexError(f"{path}: no country partition {country!r}")
        chosen = [country]
    else:
        chosen = [name for name in sorted(listing) if name != LINKS_PARTITION]
    tables = {"feeds": [], "realtime": [], "places": [], "edges": []}
    for name in chosen:
        for table in tables:
            if table in listing[name]:
                tables[table].append(_read_partition_table(path, snapshot, name, table))
    links = None
    if "edges" in listing.get(LINKS_PARTITION, {}):
        links = _read_partition_table(path, snapshot, LINKS_PARTITION, "edges")
        if country is not None:
            places = tables["places"]
            here = set(places[0]["place_id"]) if places else set()
            links = links[links["place_id"].isin(here)].reset_index(drop=True)
    feeds = _concat(tables["feeds"])
    if feeds is None:
        if country is None:
            raise IncompatibleIndexError(f"{path}: the index carries no feeds table")
        # A country served only through links has places but no home feeds.
        import pandas

        feeds = pandas.DataFrame(columns=sorted(_FEEDS_COLUMNS[version]))
    realtime = _concat(tables["realtime"])
    if realtime is None and version >= 8:
        import pandas

        # Schema 8 with no companions here: an empty table, not None.
        realtime = pandas.DataFrame(columns=sorted(_REALTIME_COLUMNS))
    edges = _concat(tables["edges"])
    if country is None and links is not None and len(links):
        # The flat view: the domestic edges and the cross-border ones together.
        flat_links = links.drop(columns=["feed_partition"])
        edges = _concat([edges, flat_links] if edges is not None else [flat_links])
    return Index(
        snapshot,
        feeds,
        _concat(tables["places"], geo=True),
        edges,
        links=links,
        country=country,
        path=path,
        realtime=realtime,
    )


def load(path, *, country=None):
    """:func:`read_index` under the plan's name: the whole index, or one
    country partition of a schema-7 index."""
    return read_index(path, country=country)


def links(path):
    """The cross-border edge table of the schema-7 index at ``path`` — every
    edge whose feed has no home country or whose place lies outside it, with
    ``feed_partition`` naming the partition holding the feed — or None when
    the index has none. Only the manifest and the links table are read."""
    path = Path(path).resolve()
    snapshot = _read_manifest(path)
    if snapshot["schema_version"] < 7:
        return None
    listing = _partitions(snapshot, path)
    if "edges" not in listing.get(LINKS_PARTITION, {}):
        return None
    return _read_partition_table(path, snapshot, LINKS_PARTITION, "edges")


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
    # An absent post segment sorts below every explicit one, ``.post0`` included.
    post = match.group("post")
    return (release, pre, (0, 0) if post is None else (1, int(post)))


def _check_reader_range(snapshot, path):
    """A supported-schema manifest names the discovery semantics it was built
    under and the transitio version that introduced its schema; a reader older
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
    """A place_id -> distinct-feed-count callable over the index's edges —
    the links into a country load included, so a name resolves as it does
    on the whole index."""
    tables = [t for t in (index.edges, index.links) if t is not None]
    if not tables:
        return None
    import pandas

    pairs = pandas.concat(
        [t[["place_id", "feed_id"]] for t in tables], ignore_index=True
    ).drop_duplicates()
    counts = pairs.groupby("place_id")["feed_id"].nunique().to_dict()
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

    ``query`` is a name, a QID, an own ``tp_`` id, or a :class:`Place`; an id
    resolves through the place's former ids and the QIDs it carries. A bare
    city name promotes to its default metro; ``kind`` pins the scope and
    suppresses promotion. Raises
    :class:`~transitio.exceptions.PlaceNotFoundError` or
    :class:`~transitio.exceptions.AmbiguousPlaceError`.
    """
    return _lookup_for(_coerce_index(index)).resolve(query, kind=kind)


def places(query, *, index=None):
    """The places matching ``query``, ranked best first (never promoted)."""
    return _lookup_for(_coerce_index(index)).search(query)


from transitio.index._refresh import installed, refresh, use  # noqa: E402
