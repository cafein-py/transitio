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
import operator
import os
import threading
import re
import stat
from pathlib import Path

from transitio.exceptions import IncompatibleIndexError, PlaceNotFoundError
from transitio.index import _boundaries
from transitio.index.feeds import (
    AccessProvider,
    IndexedFeed,
    Selector,
    _access_provider,
)
from transitio.index.places import (
    Area,
    AreaPart,
    AreaUnused,
    Delineation,
    Place,
    Suggestion,
    _PlaceLookup,
)
from transitio.index.recommend import Recommendation

__all__ = [
    "AccessProvider",
    "Area",
    "AreaPart",
    "AreaUnused",
    "Delineation",
    "Index",
    "IndexedFeed",
    "Place",
    "Recommendation",
    "Selector",
    "Suggestion",
    "read_index",
    "check_index",
    "load",
    "links",
    "area",
    "place",
    "places",
    "prepare_suggestions",
    "suggest",
    "refresh",
    "use",
    "installed",
    "SUPPORTED_SCHEMA_VERSIONS",
    "MIN_READER_VERSIONS",
    "DISCOVERY_SEMANTICS_VERSION",
]

# The index schema versions this reader understands. A snapshot outside the set
# is refused rather than read against columns that may have moved.
SUPPORTED_SCHEMA_VERSIONS = frozenset({4, 5, 6, 7, 8, 9, 10, 11, 12})

# The oldest transitio that reads each schema version: what a snapshot records
# as its reader floor, fixed per schema rather than taken from the build.
# Schema 5 adds the per-feed ``files`` manifest and schema 6 keys places by
# their own id, with the QID beside it; all three ship first in 0.11.0.
# Schema 7 (partitions), 8 (the GTFS-RT companion table) and 9 (feed service
# spans and place validity) ship together; schema 10 (the feeds a feed
# lies within) first in 0.15.0; schema 11 (feed access details and place
# centres) first in 0.19.0; schema 12 (core and on-demand tables) first in
# 0.24.0.
MIN_READER_VERSIONS = {
    4: "0.11.0",
    5: "0.11.0",
    6: "0.11.0",
    7: "0.12.0",
    8: "0.12.0",
    9: "0.12.0",
    10: "0.15.0",
    11: "0.19.0",
    12: "0.24.0",
}

# Bumped whenever name resolution, ranking or filtering changes: the snapshot
# pins the data, this pins how the reader interprets it, and a result that
# records both (with the transitio version) is reproducible.
DISCOVERY_SEMANTICS_VERSION = 5

FEEDS_FILE = "feeds.parquet"
REALTIME_FILE = "realtime.parquet"
PLACES_FILE = "places.parquet"
EDGES_FILE = "edges.parquet"
SNAPSHOT_FILE = "snapshot.json"
# Schema 11 adds the providers of feed credentials, one table at the root.
ACCESS_PROVIDERS_FILE = "access_providers.parquet"
# Schema 7 is a directory of partitions: one per country code (its feeds by
# home country, its places, their domestic edges), ``international`` (the
# feeds without a home country) and ``links`` (every cross-border edge, with
# ``feed_partition`` naming the partition holding the feed).
# Schema 8 adds ``realtime.parquet`` beside a partition's feeds: the GTFS-RT
# companions of its static feeds, keyed by ``static_feed_id``; the
# ``international`` partition also holds the companions without one.
# Schema 12 moves the place boundaries into ``boundaries.parquet`` and the
# edges' full evidence, selectors and curation into ``details.parquet``,
# tables read on demand beside the core ones.
BOUNDARIES_FILE = "boundaries.parquet"
DETAILS_FILE = "details.parquet"
INTERNATIONAL_PARTITION = "international"
LINKS_PARTITION = "links"
_TABLE_FILES = {
    "feeds": FEEDS_FILE,
    "realtime": REALTIME_FILE,
    "places": PLACES_FILE,
    "edges": EDGES_FILE,
    "boundaries": BOUNDARIES_FILE,
    "details": DETAILS_FILE,
}
_PARTITION_NAME = re.compile(r"[A-Z]{2}|international|links")
# The tables each partition kind may carry; a country partition any of
# them. More partitions than countries, or more listed rows than one table
# may hold, is refused before anything is read.
_PARTITION_TABLES = {
    INTERNATIONAL_PARTITION: {"feeds", "realtime"},
    LINKS_PARTITION: {"edges", "details"},
}
_MAX_PARTITIONS = 300

# Ceilings on what one index file may be, so a swapped-in or damaged file cannot
# read an unbounded amount into memory. A real index's largest table is about
# 50 MB.
_MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
_MAX_FEEDS_BYTES = 512 * 1024 * 1024
_MAX_PLACES_BYTES = 512 * 1024 * 1024
_MAX_EDGES_BYTES = 512 * 1024 * 1024
_MAX_ACCESS_PROVIDERS_BYTES = 8 * 1024 * 1024

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
# Schema 10: the larger feeds whose stops and routes contain the feed's.
_FEEDS_COLUMNS[10] = _FEEDS_COLUMNS[9] | {"contained_in"}
# Schema 11: the URL the build crawls and how to get the feed's credentials.
_FEEDS_COLUMNS[11] = _FEEDS_COLUMNS[10] | {
    "download_url",
    "access",
    "access_provider",
    "auth_method",
    "auth_params",
    "registration_url",
}
_ACCESS_PROVIDERS_COLUMNS = frozenset(
    {
        "provider_id",
        "name",
        "registration_url",
        "docs_url",
        "terms_url",
        "credential_fields",
        "free",
    }
)
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
_PLACES_COLUMNS[10] = _PLACES_COLUMNS[9]
# Schema 11: the place's centre point and its urban centre's population.
_PLACES_COLUMNS[11] = _PLACES_COLUMNS[10] | {"centre", "population"}
# Schema 7 edges carry the rank stage's relevance; the links table also names
# the partition holding each edge's feed.
_RELEVANCE_COLUMNS = frozenset({"relevance_category", "relevance", "cross_border"})
_EDGES_COLUMNS_BY_VERSION = {
    version: _EDGES_COLUMNS for version in SUPPORTED_SCHEMA_VERSIONS if version < 7
}
_EDGES_COLUMNS_BY_VERSION[7] = _EDGES_COLUMNS | _RELEVANCE_COLUMNS
_EDGES_COLUMNS_BY_VERSION[8] = _EDGES_COLUMNS_BY_VERSION[7]
_EDGES_COLUMNS_BY_VERSION[9] = _EDGES_COLUMNS_BY_VERSION[7]
_EDGES_COLUMNS_BY_VERSION[10] = _EDGES_COLUMNS_BY_VERSION[7]
_EDGES_COLUMNS_BY_VERSION[11] = _EDGES_COLUMNS_BY_VERSION[7]
# Schema 12: places carry their boundary's bounding box in place of the
# boundary, and edges a ``summary`` of the evidence keys the reader ranks by
# in place of the details.
_BBOX_COLUMNS = ("xmin", "ymin", "xmax", "ymax")
_DETAIL_COLUMNS = (
    "evidence",
    "selector",
    "merged_evidence",
    "curation",
    "curation_history",
    "rehomed_from",
)
_FEEDS_COLUMNS[12] = _FEEDS_COLUMNS[11]
_PLACES_COLUMNS[12] = (_PLACES_COLUMNS[11] - {"geometry"}) | set(_BBOX_COLUMNS)
_EDGES_COLUMNS_BY_VERSION[12] = (
    _EDGES_COLUMNS_BY_VERSION[11] - set(_DETAIL_COLUMNS)
) | {"summary"}
_BOUNDARIES_COLUMNS = frozenset({"place_id", "geometry", "polygon", "snapshot"})
_DETAILS_COLUMNS = frozenset(
    {"place_id", "feed_id", "tier", *_DETAIL_COLUMNS, "snapshot"}
)
# Each core table extended on demand: its partner, the join keys, the
# columns taken from the partner and those the core alone carries.
_ON_DEMAND = {
    "places": ("boundaries", ["place_id"], ["geometry"], list(_BBOX_COLUMNS)),
    "edges": (
        "details",
        ["place_id", "feed_id", "tier"],
        list(_DETAIL_COLUMNS),
        ["summary"],
    ),
}
_ON_DEMAND_TABLES = frozenset(extra for extra, *_ in _ON_DEMAND.values())
# The Arrow types of the columns schema 11 and 12 add, and of the keys that
# join a schema-12 table with its on-demand partner; an all-null column
# passes.
_EDGE_KEYS = dict.fromkeys(("place_id", "feed_id", "tier"), "string")
_COLUMN_TYPES = {
    "feeds": dict.fromkeys(_FEEDS_COLUMNS[11] - _FEEDS_COLUMNS[10], "string"),
    "places": {
        "place_id": "string",
        "centre": "binary",
        "population": "integer",
        **dict.fromkeys(_BBOX_COLUMNS, "float64"),
    },
    "boundaries": {"place_id": "string", "polygon": "bool"},
    "edges": {**_EDGE_KEYS, "summary": "string"},
    "details": {
        **_EDGE_KEYS,
        **dict.fromkeys(_DETAIL_COLUMNS, "string"),
        "rehomed_from": "list of strings",
    },
    "access_providers": {
        **dict.fromkeys(_ACCESS_PROVIDERS_COLUMNS, "string"),
        "credential_fields": "list of strings",
        "free": "bool",
    },
}


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
        parquet = pyarrow.parquet.ParquetFile(io.BytesIO(data))
        metadata = parquet.metadata
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
    _check_types(parquet.schema_arrow, path, table)
    try:
        return read(io.BytesIO(data))
    except Exception as error:
        raise IncompatibleIndexError(f"{path}: not a readable {table} table ({error})")


def _check_types(schema, path, table):
    """Refuse a column schema 11 or 12 adds stored as another Arrow type."""
    import pyarrow as pa

    accepts = {
        "string": (pa.types.is_string, pa.types.is_large_string),
        "binary": (pa.types.is_binary, pa.types.is_large_binary),
        "bool": (pa.types.is_boolean,),
        "float64": (pa.types.is_float64,),
        "integer": (pa.types.is_integer,),
        "list of strings": (pa.types.is_list, pa.types.is_large_list),
    }
    for name, expected in _COLUMN_TYPES.get(table, {}).items():
        index = schema.get_field_index(name)  # -1 when absent or repeated
        found = None if index < 0 else schema.field(index).type
        if found is None or pa.types.is_null(found):
            continue
        valid = any(test(found) for test in accepts[expected])
        if valid and expected == "list of strings":
            valid = any(test(found.value_type) for test in accepts["string"])
        if not valid:
            raise IncompatibleIndexError(
                f"{path}: {table} column {name!r} is {found}, not {expected}"
            )


def _check_centres(places, path):
    """The schema-11 place centres as points, refused unless each is a WKB
    Point."""
    import geopandas

    centres = places["centre"]
    try:
        if centres.dtype.name != "geometry":
            centres = geopandas.GeoSeries.from_wkb(centres, crs="EPSG:4326")
    except Exception as error:
        raise IncompatibleIndexError(f"{path}: a place centre is not WKB ({error})")
    kinds = geopandas.GeoSeries(centres).geom_type
    if (kinds.notna() & (kinds != "Point")).any():
        raise IncompatibleIndexError(f"{path}: a place centre is not a Point")
    return centres


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


_UNREAD = object()  # a partition table not yet read


class Index:
    """A resolved index: its manifest, feeds, and (if present) places and edges.

    A schema-7 index read whole joins every partition into the flat tables;
    read for one ``country`` it holds that partition alone. ``links`` is the
    cross-border edge table (with ``feed_partition``), None before schema 7.
    ``realtime`` is the GTFS-RT companion table of schema 8 (the whole
    index's, or the country's), None before it. ``access_providers`` is the
    schema-11 table of the providers that issue feed credentials, None
    before it. A schema-12 index read from disk holds the core tables:
    places with their bounding boxes and edges with their ``summary``; a
    partition's boundaries and details are read on first use
    (:meth:`boundaries_in`, :meth:`details_in`) and kept.
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
        access_providers=None,
    ):
        self.snapshot = snapshot
        self.feeds = feeds
        self.places = places
        self.edges = edges
        self.links = links
        self.realtime = realtime
        self.access_providers = access_providers
        self.country = country
        self._path = None if path is None else Path(path)
        self._partition_tables = {}
        self._tables_lock = threading.Lock()
        self._providers = None
        # Schema 12: the partition each row of ``edges`` was read from.
        self._edge_partitions = None
        # The shared lock on an installed snapshot, held while the index lives.
        self._lease = None

    @property
    def partitions(self):
        """The partition listing of a schema-7 manifest, else an empty dict."""
        return self.snapshot.get("partitions") or {}

    def _partition_table(self, partition, table):
        """One partition table read from disk once, whichever thread asks
        first, and kept for the index's lifetime; None when the partition
        lists no such table."""
        key = (partition, table)
        found = self._partition_tables.get(key, _UNREAD)
        if found is _UNREAD:
            with self._tables_lock:
                if key not in self._partition_tables:
                    self._partition_tables[key] = self._read_table(partition, table)
                found = self._partition_tables[key]
        return found

    def _read_table(self, partition, table):
        """One partition table from disk; an on-demand table the directory
        lacks is asked for first. Boundaries come as a GeoSeries by place
        id, details indexed by place id, feed id and tier."""
        if self._path is None or table not in self.partitions.get(partition, {}):
            return None
        file = self._path / partition / _TABLE_FILES[table]
        if table in _ON_DEMAND_TABLES and not os.path.lexists(file):
            from transitio.index._refresh import _fetch_part

            _fetch_part(self, partition, table)
        frame = _read_partition_table(self._path, self.snapshot, partition, table)
        if table not in _ON_DEMAND_TABLES:
            return frame
        keys = _ON_DEMAND["edges"][1] if table == "details" else "place_id"
        frame = frame.set_index(keys)
        if not frame.index.is_unique:
            raise IncompatibleIndexError(f"{file}: a key repeats")
        if table == "boundaries":
            import geopandas

            return geopandas.GeoSeries(frame["geometry"], crs="EPSG:4326")
        return frame

    def boundaries_in(self, partition):
        """The place boundaries of one country partition of a schema-12
        index, a GeoSeries indexed by place id, read once and kept; None
        when the partition lists none (before schema 12 the boundaries are
        the places table's ``geometry``)."""
        return self._partition_table(partition, "boundaries")

    def details_in(self, partition):
        """The full evidence, selectors and curation of the edges of one
        partition of a schema-12 index (``links`` for the cross-border
        ones), indexed by place id, feed id and tier, read once and kept;
        None when the partition lists none."""
        return self._partition_table(partition, "details")

    def _boundaries(self, place_ids, partitions):
        """The boundaries of ``place_ids``, each read from its partition in
        ``partitions``, as an object array; refused when a partition holds
        no boundary for one of them."""
        import numpy as np

        place_ids = np.asarray(place_ids, dtype=object)
        partitions = np.asarray(partitions, dtype=object)
        shapes = np.empty(len(place_ids), dtype=object)
        for partition in dict.fromkeys(partitions.tolist()):
            here = partitions == partition
            table = self.boundaries_in(partition)
            rows = np.full(here.sum(), -1)
            if table is not None:
                rows = table.index.get_indexer(place_ids[here])
            if (rows < 0).any():
                missing = place_ids[here][rows < 0][0]
                raise IncompatibleIndexError(
                    f"partition {partition!r} holds no boundary for place {missing!r}"
                )
            shapes[here] = table.to_numpy()[rows]
        return shapes

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

    def access_provider(self, provider_id):
        """The :class:`AccessProvider` with this id, or None when the index
        has none of that id or predates schema 11."""
        if self.access_providers is None:
            return None
        if self._providers is None:
            providers = {}
            for record in self.access_providers.to_dict("records"):
                provider = _access_provider(record)
                providers.setdefault(provider.provider_id, provider)
            self._providers = providers
        return self._providers.get(provider_id)

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
    (joined, or the country's own). A schema-12 index keeps place boundaries
    and the edges' evidence, selectors and curation in on-demand tables:
    only the core tables are read here, and a partition's on-demand ones
    when first needed (:attr:`Place.geometry`, :func:`area`, an edge's
    ``evidence`` or ``selector``); :func:`check_index` reads them all.

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


def _read_root_table(path, snapshot, file, limit, table):
    """The table ``file`` at the snapshot root, or None when the manifest
    declares no ``<table>_sha256`` for it.

    The Parquet is then a size-bounded regular file whose bytes must match
    that digest; places are read as a GeoDataFrame, any other table as a
    DataFrame.
    """
    key = f"{table}_sha256"
    expected = snapshot.get(key)
    if expected is None:
        return None
    if not isinstance(expected, str):
        raise IncompatibleIndexError(f"{path / SNAPSHOT_FILE}: {key} is not a string")
    if table == "places":
        from geopandas import read_parquet
    else:
        from pandas import read_parquet

    data = _read_regular(path / file, limit)
    if hashlib.sha256(data).hexdigest() != expected:
        raise IncompatibleIndexError(
            f"{path / file}: does not match the snapshot's {key}"
        )
    return _load_table(read_parquet, data, path / file, table)


def _read_places(path, snapshot, version):
    """The places GeoDataFrame, or None when the index carries no places;
    its columns checked against the schema before it is returned."""
    places = _read_root_table(path, snapshot, PLACES_FILE, _MAX_PLACES_BYTES, "places")
    if places is not None:
        _check_columns(
            places, _PLACES_COLUMNS[version], path / PLACES_FILE, version, "places"
        )
        _check_snapshot_column(places, snapshot, path / PLACES_FILE, "places")
    return places


def _read_edges(path, snapshot, version):
    """The edges DataFrame, or None when the index carries no edges; its
    columns checked against the schema before it is returned."""
    edges = _read_root_table(path, snapshot, EDGES_FILE, _MAX_EDGES_BYTES, "edges")
    if edges is not None:
        _check_columns(edges, _EDGES_COLUMNS, path / EDGES_FILE, version, "edges")
        _check_snapshot_column(edges, snapshot, path / EDGES_FILE, "edges")
    return edges


def _read_access_providers(path, snapshot, version):
    """The schema-11 table of credential providers, which the manifest must
    declare; None before schema 11."""
    if version < 11:
        return None
    providers = _read_root_table(
        path,
        snapshot,
        ACCESS_PROVIDERS_FILE,
        _MAX_ACCESS_PROVIDERS_BYTES,
        "access_providers",
    )
    if providers is None:
        raise IncompatibleIndexError(
            f"{path / SNAPSHOT_FILE}: manifest declares no access_providers_sha256"
        )
    _check_columns(
        providers,
        _ACCESS_PROVIDERS_COLUMNS,
        path / ACCESS_PROVIDERS_FILE,
        version,
        "access_providers",
    )
    return providers


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
    # The realtime table arrives with schema 8 and the on-demand tables, with
    # each table's size, with schema 12; an older snapshot listing one is
    # outside its layout.
    version = snapshot["schema_version"]
    known = set(_TABLE_FILES)
    if version < 8:
        known.discard("realtime")
    if version < 12:
        known -= _ON_DEMAND_TABLES
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
                and (version < 12 or _positive_int(entry.get("bytes")))
                for table, entry in tables.items()
            )
        ):
            raise IncompatibleIndexError(
                f"{path / SNAPSHOT_FILE}: partition {name!r} lists tables outside "
                "the layout"
            )
        for core, (extra, *_) in _ON_DEMAND.items() if version >= 12 else ():
            if (core in tables) != (extra in tables):
                raise IncompatibleIndexError(
                    f"{path / SNAPSHOT_FILE}: partition {name!r} lists {core} and "
                    f"{extra} apart"
                )
        for table, entry in tables.items():
            rows[table] += entry["rows"]
    if any(total > _MAX_TABLE_ROWS for total in rows.values()):
        raise IncompatibleIndexError(
            f"{path / SNAPSHOT_FILE}: the partitions list more than "
            f"{_MAX_TABLE_ROWS} rows of one table"
        )
    return listing


def _positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


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


def _read_partition_table(path, snapshot, partition, table, whole=False):
    """One partition table, checked against its digest, columns and snapshot.
    From schema 12, with ``whole``, places and edges are joined with their
    on-demand boundaries and details, one row each, into the frames of
    schema 11."""
    frame = _read_partition_file(path, snapshot, partition, table)
    if not whole or snapshot["schema_version"] < 12 or table not in _ON_DEMAND:
        return frame
    import pandas

    extra, keys, taken, dropped = _ON_DEMAND[table]
    file = path / partition / _TABLE_FILES[extra]
    partner = _read_partition_file(path, snapshot, partition, extra)
    # The key columns are typed as strings when read; a null key names nothing.
    if frame[keys].isna().any().any() or partner[keys].isna().any().any():
        raise IncompatibleIndexError(f"{file}: a {table} or {extra} key is null")
    try:
        joined = frame.drop(columns=dropped).merge(
            partner[keys + taken],
            on=keys,
            how="left",
            validate="one_to_one",
            indicator=True,
        )
    except pandas.errors.MergeError:
        raise IncompatibleIndexError(f"{file}: a key repeats in {table} or {extra}")
    if len(partner) != len(frame) or (joined.pop("_merge") != "both").any():
        raise IncompatibleIndexError(
            f"{file}: its rows do not match the {table} rows one to one"
        )
    if table == "places":
        import geopandas

        joined = geopandas.GeoDataFrame(joined, geometry="geometry", crs="EPSG:4326")
    return joined


def _read_partition_file(path, snapshot, partition, table):
    """One partition file, checked against its size, digest, columns and
    snapshot."""
    import pandas

    version = snapshot["schema_version"]
    listing = _partitions(snapshot, path)
    file = _partition_directory(path, partition) / _TABLE_FILES[table]
    entry = listing[partition][table]
    limits = {
        "feeds": _MAX_FEEDS_BYTES,
        "realtime": _MAX_FEEDS_BYTES,
        "places": _MAX_PLACES_BYTES,
        "boundaries": _MAX_PLACES_BYTES,
    }
    data = _read_regular(file, limits.get(table, _MAX_EDGES_BYTES))
    if version >= 12 and len(data) != entry["bytes"]:
        raise IncompatibleIndexError(
            f"{file}: {len(data)} bytes where the snapshot lists {entry['bytes']}"
        )
    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise IncompatibleIndexError(f"{file}: does not match the snapshot's sha256")
    if table == "places" and version < 12:
        import geopandas

        frame = _load_table(geopandas.read_parquet, data, file, table)
    elif table == "boundaries":
        import pyarrow.parquet

        def read(source):
            return _boundaries.decode(pyarrow.parquet.read_table(source))

        frame = _load_table(read, data, file, table)
    else:
        frame = _load_table(pandas.read_parquet, data, file, table)
    if table == "places":
        columns = _PLACES_COLUMNS[version]
        if version >= 11 and "centre" in frame.columns:
            frame["centre"] = _check_centres(frame, file)
    elif table == "feeds":
        columns = _FEEDS_COLUMNS[version]
    elif table == "realtime":
        columns = _REALTIME_COLUMNS
    elif table == "boundaries":
        columns = _BOUNDARIES_COLUMNS
    elif table == "details":
        columns = _DETAILS_COLUMNS
    elif partition == LINKS_PARTITION:
        columns = _EDGES_COLUMNS_BY_VERSION[version] | {"feed_partition"}
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


def _read_partitioned(path, snapshot, version, country, whole=False):
    """A schema-7 index: every partition joined, or one country's; from
    schema 12 the core tables, with ``whole`` their on-demand partners
    joined in."""
    import pandas

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
    counts = {}  # the edges rows of each partition, in order
    for name in chosen:
        for table in tables:
            if table in listing[name]:
                frame = _read_partition_table(path, snapshot, name, table, whole)
                tables[table].append(frame)
                if table == "edges":
                    counts[name] = len(frame)
    links = None
    if "edges" in listing.get(LINKS_PARTITION, {}):
        links = _read_partition_table(path, snapshot, LINKS_PARTITION, "edges", whole)
        if country is not None:
            places = tables["places"]
            here = set(places[0]["place_id"]) if places else set()
            links = links[links["place_id"].isin(here)].reset_index(drop=True)
    feeds = _concat(tables["feeds"])
    if feeds is None:
        if country is None:
            raise IncompatibleIndexError(f"{path}: the index carries no feeds table")
        # A country served only through links has places but no home feeds.
        feeds = pandas.DataFrame(columns=sorted(_FEEDS_COLUMNS[version]))
    realtime = _concat(tables["realtime"])
    if realtime is None and version >= 8:
        # Schema 8 with no companions here: an empty table, not None.
        realtime = pandas.DataFrame(columns=sorted(_REALTIME_COLUMNS))
    edges = _concat(tables["edges"])
    if country is None and links is not None and len(links):
        # The flat view: the domestic edges and the cross-border ones together.
        flat_links = links.drop(columns=["feed_partition"])
        edges = _concat([edges, flat_links] if edges is not None else [flat_links])
        counts[LINKS_PARTITION] = len(flat_links)
    index = Index(
        snapshot,
        feeds,
        _concat(tables["places"], geo=version < 12 or whole),
        edges,
        links=links,
        country=country,
        path=path,
        realtime=realtime,
        access_providers=_read_access_providers(path, snapshot, version),
    )
    if version >= 12 and not whole and edges is not None:
        import numpy

        codes = numpy.repeat(numpy.arange(len(counts)), list(counts.values()))
        index._edge_partitions = pandas.Categorical.from_codes(codes, list(counts))
    return index


def load(path, *, country=None):
    """:func:`read_index` under a shorter name: the whole index, or one
    country partition of a schema-7 index."""
    return read_index(path, country=country)


def links(path):
    """The cross-border edge table of the schema-7 index at ``path`` — every
    edge whose feed has no home country or whose place lies outside it, with
    ``feed_partition`` naming the partition holding the feed — or None when
    the index has none. Only the manifest and the links table (from schema
    12 with its details) are read."""
    path = Path(path).resolve()
    snapshot = _read_manifest(path)
    if snapshot["schema_version"] < 7:
        return None
    listing = _partitions(snapshot, path)
    if "edges" not in listing.get(LINKS_PARTITION, {}):
        return None
    return _read_partition_table(path, snapshot, LINKS_PARTITION, "edges", True)


def check_index(path):
    """Read every table of the index at ``path`` with every check, or raise
    :class:`~transitio.exceptions.IncompatibleIndexError` at the first
    problem; returns None.

    Beyond what :func:`read_index` reads, a schema-12 index's on-demand
    tables are read too: each file's size, digest, columns, rows and
    snapshot are checked, each place must have one boundaries row and each
    edge one details row, with none left over, and every boundary is
    decoded."""
    path = Path(path).resolve()
    snapshot = _read_manifest(path)
    version = snapshot["schema_version"]
    if version < 12:
        read_index(path)
    else:
        _read_partitioned(path, snapshot, version, None, whole=True)


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
    else the active installed snapshot, resolved lazily. An index read from
    a path is kept for the next query of that path, until its manifest
    changes."""
    if index is None:
        from transitio.index._refresh import active_index

        return active_index()
    if isinstance(index, Index):
        return index
    path = Path(index).resolve()

    def identity():
        # The manifest's bytes name the snapshot, its tables' digests included;
        # one that cannot be read safely leaves the refusal to read_index.
        try:
            data = _read_regular(path / SNAPSHOT_FILE, _MAX_SNAPSHOT_BYTES)
            manifest = json.loads(data.decode("utf-8"))
        except (IncompatibleIndexError, ValueError):
            return None, None
        return (str(path), hashlib.sha256(data).hexdigest()), manifest

    key, manifest = identity()
    if key is None:
        return read_index(path)
    with _LOOKUP_LOCK:
        if _READ_INDEXES.get("key") == key:
            return _READ_INDEXES["index"]
    read = read_index(path)
    # Kept only when it was read from that very manifest, still in place.
    if read.snapshot == manifest and identity()[0] == key:
        with _LOOKUP_LOCK:
            _READ_INDEXES.update(key=key, index=read)
    return read


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


# One lookup per index, whichever thread asks first.
_LOOKUP_LOCK = threading.Lock()
_READ_INDEXES = {}  # the index last read from a path, with its manifest key


def _lookup_for(index):
    with _LOOKUP_LOCK:
        lookup = getattr(index, "_place_lookup", None)
        if lookup is None:
            if index.places is None:
                raise PlaceNotFoundError("this index carries no places")
            lookup = _PlaceLookup(
                index.places, feed_count=_feed_count_for(index), index=index
            )
            index._place_lookup = lookup
    return lookup


def place(query, *, kind=None, definition=None, index=None):
    """Resolve ``query`` to a single :class:`Place`, or raise.

    ``query`` is a name, a QID, an own ``tp_`` id, or a :class:`Place`; an id
    resolves through the place's former ids and the QIDs it carries. Only a
    name matching a label in full resolves: a partial name ("Helsi") raises
    :class:`~transitio.exceptions.PlaceNotFoundError` with the places it
    partly matches as ``candidates``, and :func:`suggest` completes names. A
    name shared by a city and the metros named after it in its country, an
    area containing it with much the same service, or places inside it or
    areas in its country listing its name only as an alias, resolves to the
    city ("Taipei" is the city, not New Taipei or Taiwan); where no city
    matches, a region or country of the name stands as the city ("Istanbul"
    is the province, not its metro);
    other shared names go to the place with more than twice the runner-up's
    feeds, unless a place of the name in another country is far better
    known (by the languages its name is recorded in), since feed counts
    measure how well each country's feeds are catalogued. Where feeds do not
    decide, a place of the name known far more widely than every other, in
    its country or abroad, wins: "Moscow" is Moscow, Russia, and "Cali,
    Colombia" the city rather than a lesser-known place there. A place's own
    names are its name and its labels in its country's languages or
    English; where a place carries the name as its own, one reaching it
    only through a label in another language does not compete unless it is
    far better known: "München" is Munich, and "Meksyk", Polish for Mexico,
    stays ambiguous rather than naming a place in Poland called Meksyk.
    Where a place carries the name as its name or in English, one carrying
    it only in another of its own languages does not compete either unless
    far better known: "Bergen", Dutch for Mons, is no rival to the towns
    named Bergen. Before any of these, a city of at least 200,000 people
    (schema 11, :attr:`Place.population`) carrying the name as its name or
    in English wins when it has more than twice the population of every
    other place of the name recording one, whatever their feeds or labels:
    "Lima" is Lima, Peru, not Lima, Ohio. It does not where a region or
    country of the name contains no city of the name ("Victoria": the city
    in British Columbia and the Australian state), or where a city of the
    name without a recorded population is known at least as widely, as San
    Jose, California, a city of the San Francisco urban centre, is.
    ``kind`` pins the scope. A qualified name, "Name, Qualifier" with one or
    more qualifiers, keeps the places lying within a region or country each
    qualifier names ("London, Ontario", "City of London, UK"), or within any
    containing place, such as a town, when that leaves no place of the name:
    "Copenhagen, Denmark" is in Denmark, not in the town of Denmark, New
    York. A name or alias that itself holds a comma still matches as
    written; a label in another language equal to the whole query answers
    only when the qualified name names no single place (no exact match, or
    several that nothing decides between).

    A metro's definition is its ``subtype``. Metros of the name sharing a
    member place are one metro under several definitions, and the first in
    this order answers:
    ``"functional urban area"`` (Eurostat Urban Audit),
    ``"metropolitan statistical area"`` (US Census),
    ``"metropolitan region"`` (Eurostat's NUTS-3 approximation),
    ``"city-region (FAO)"``, then any other; "Stockholm" with
    ``kind="metro"`` is its functional urban area. A city's own metros,
    those in its country, keep every definition, so that dropping the
    others cannot hand the city's name to one of them; "Cambridge, United
    Kingdom", with no city of the name in the UK, is its metropolitan
    region. ``definition`` names one instead and implies
    ``kind="metro"``; it raises :class:`ValueError` when ``kind`` names
    another kind or no metro of the index carries it. US metropolitan
    statistical areas are named after their principal cities ("Los
    Angeles–Long Beach–Anaheim metropolitan area"), so a city's name
    matches one only in part and raises ``PlaceNotFoundError`` listing it;
    the city's :attr:`Place.metros` holds it. Raises
    :class:`~transitio.exceptions.PlaceNotFoundError` or
    :class:`~transitio.exceptions.AmbiguousPlaceError`.
    """
    lookup = _lookup_for(_coerce_index(index))
    return lookup.resolve(query, kind=kind, definition=definition)


def area(geometry, *, country=None, index=None):
    """The index places that cover an area, the share they cover and their
    feeds, as an :class:`Area`.

    ``geometry`` is a shapely geometry, a GeoDataFrame/GeoSeries or a
    ``(minx, miny, maxx, maxy)`` tuple, in WGS84, and ``country`` an ISO
    code keeping only that country's places. The candidates are the cities,
    regions and countries with at least one feed whose boundary intersects
    the area; metros are left out. Each is measured on its part inside the
    area: ``inside``, the share of the place, and ``holds``, the share of the
    area it stands for. Every city at least half inside (a whole part), or
    holding at least 1 % of the area, is a part. A region or a country stands
    only for the area's land that no city part and no narrower place with
    feeds covers, and is a part when that land is at least 1 % of the area:
    a box around Los Angeles gives its cities, then Los Angeles County for
    the land between them, not California; a box inside one district gives
    that district alone. ``unused`` lists the other candidates with why
    (``"holds less than 1 % of the area"``, ``"narrower places cover its
    land in the area"``, or ``"the area has no extent"`` for a point or a
    line). ``coverage`` is the area the parts cover, divided
    by the area's land: its part within the index's country places, with or
    without feeds, or with ``country`` within that country's, plus the
    parts. Land in a country without feeds counts as uncovered, sea and land
    outside every country place of the index are left out, and an area with
    no land has a coverage of 0. Areas are measured in km² in an equal-area
    projection (EPSG:6933). :meth:`Area.feeds` lists the parts' feeds.
    """
    from transitio.osm._fetch import _as_geometry

    lookup = _lookup_for(_coerce_index(index))
    return lookup.area(_as_geometry(geometry), country)


def places(query, *, index=None):
    """The places matching ``query``, ranked best first; a qualified name
    lists only the places within its qualifiers, as :func:`place` first
    reads it."""
    return _lookup_for(_coerce_index(index)).search(query)


def suggest(prefix, *, limit=10, kinds=None, country=None, lang=None, index=None):
    """Type-ahead: the places whose labels start with ``prefix``, ranked.

    Every label a place carries counts — its name, its names in other
    languages and its aliases — normalised as :func:`place` normalises a
    query, so ``hels`` and ``helsingf`` both reach Helsinki. Places rank by
    an exact label first, then kind precedence as :func:`places` ranks it,
    the label's source (the name, a name in the place's own languages or
    English, an alias, a name in another language), more feeds, the label
    and the id; one :class:`Suggestion` per place, at most ``limit``.
    ``kinds`` keeps only those kinds, ``country`` one ISO code or several,
    and ``lang`` picks the label to show (the place's name in that language
    when it has one, else its name). An empty prefix suggests nothing. The
    index's labels are sorted once, on the first call over it.
    """
    limit = operator.index(limit)  # a float or NaN limit is a TypeError
    if limit < 1:
        raise ValueError("limit must be at least 1")
    lookup = _lookup_for(_coerce_index(index))
    return lookup.suggest(prefix, limit=limit, kinds=kinds, country=country, lang=lang)


def prepare_suggestions(*, index=None):
    """Build the sorted labels :func:`suggest` reads, ahead of the first call.

    The build takes seconds over a whole catalogue, so a server pays it at
    startup rather than on a user's first keystroke; a later call is free.
    Returns the index prepared (the active one when none is given).
    """
    prepared = _coerce_index(index)
    _lookup_for(prepared).prepare()
    return prepared


from transitio.index._refresh import installed, refresh, use  # noqa: E402
