"""The read layer over a published feed index.

An index is a directory of ``feeds.parquet`` (one row per feed), an optional
``places.parquet`` (one row per place, with boundary geometry) and a
``snapshot.json`` manifest. :func:`read_index` loads one and returns an
:class:`Index` exposing the manifest, the feeds as a DataFrame and the places as
a GeoDataFrame (or ``None`` when the index predates the gazetteer). Building an
index is a maintainer step (``scripts/build_index.py --stage publish``);
discovering and refreshing bundled snapshots is a later addition.

Edges come later, and only schema versions this transitio understands are
accepted, so a newer index is refused with a clear upgrade message rather than
misread.
"""

import hashlib
import io
import json
import os
import stat
from pathlib import Path

from transitio.exceptions import (
    IncompatibleIndexError,
    PlaceNotFoundError,
    TransitioError,
)
from transitio.index.places import Place, _PlaceLookup

__all__ = [
    "Index",
    "Place",
    "read_index",
    "place",
    "places",
    "SUPPORTED_SCHEMA_VERSIONS",
]

# The index schema versions this reader understands. A snapshot outside the set
# is refused rather than read against columns that may have moved.
SUPPORTED_SCHEMA_VERSIONS = frozenset({1})

FEEDS_FILE = "feeds.parquet"
PLACES_FILE = "places.parquet"
SNAPSHOT_FILE = "snapshot.json"

# Ceilings on what one index file may be, so a swapped-in or damaged file cannot
# read an unbounded amount into memory. A real index is a few MB.
_MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
_MAX_FEEDS_BYTES = 512 * 1024 * 1024
_MAX_PLACES_BYTES = 512 * 1024 * 1024

# The columns a schema_version 1 feeds table carries. A correctly-hashed but
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
        "snapshot",
    }
)

# The columns a schema_version 1 places table carries, geometry included.
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
        "snapshot",
        "geometry",
    }
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
    """A resolved index: its manifest, its feeds and (if present) its places."""

    def __init__(self, snapshot, feeds, places=None):
        self.snapshot = snapshot
        self.feeds = feeds
        self.places = places

    @property
    def snapshot_id(self):
        return self.snapshot["snapshot_id"]

    @property
    def schema_version(self):
        return self.snapshot["schema_version"]

    def __repr__(self):
        places = "None" if self.places is None else len(self.places)
        return (
            f"<Index snapshot_id={self.snapshot_id!r} feeds={len(self.feeds)} "
            f"places={places} schema_version={self.schema_version}>"
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
    feeds = pandas.read_parquet(io.BytesIO(data))
    columns = set(feeds.columns)
    if columns != _SCHEMA_COLUMNS:
        raise IncompatibleIndexError(
            f"{path / FEEDS_FILE}: feeds columns do not match schema_version "
            f"{version} (missing {sorted(_SCHEMA_COLUMNS - columns)}, "
            f"unexpected {sorted(columns - _SCHEMA_COLUMNS)})"
        )
    return Index(snapshot, feeds, _read_places(path, snapshot, version))


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
    places = geopandas.read_parquet(io.BytesIO(data))
    columns = set(places.columns)
    if columns != _PLACES_COLUMNS:
        raise IncompatibleIndexError(
            f"{path / PLACES_FILE}: places columns do not match schema_version "
            f"{version} (missing {sorted(_PLACES_COLUMNS - columns)}, "
            f"unexpected {sorted(columns - _PLACES_COLUMNS)})"
        )
    return places


def _coerce_index(index):
    if index is None:
        raise TransitioError("no active feed index; pass index=<path to a built index>")
    if isinstance(index, Index):
        return index
    return read_index(index)


def _lookup_for(index):
    lookup = getattr(index, "_place_lookup", None)
    if lookup is None:
        if index.places is None:
            raise PlaceNotFoundError("this index carries no places")
        lookup = _PlaceLookup(index.places)
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
