"""Inspect and clear the feed download cache.

Every feed transitio downloads is kept under ``<cache_dir>/gtfs``, by default
in the platform cache, as versions named by their SHA-256 beside a
``.provenance.json`` sidecar, together with what :func:`transitio.fetch` made
of them (:mod:`transitio.catalog._cache`). :func:`info` lists what the cache
holds and :func:`clear` removes it.
"""

from __future__ import annotations

import collections
import contextlib
import datetime
import os
import shutil
import stat
from pathlib import Path

__all__ = ["clear", "info"]


def _feed_cache(cache_dir):
    import platformdirs

    from transitio.catalog._cache import FeedCache

    return FeedCache(cache_dir or platformdirs.user_cache_dir("transitio"))


def _size(path):
    """The ``os.lstat`` of ``path``, None when it is gone."""
    try:
        return os.lstat(path)
    except OSError:
        return None


def _bytes(path):
    """The bytes of the file at ``path``, or of every file under it, symlinks
    not followed and files gone meanwhile left out."""
    if path.is_symlink() or not path.is_dir():
        files = [path]
    else:
        files = [Path(r) / n for r, _, names in os.walk(path) for n in names]
    return sum(info.st_size for info in map(_size, files) if info is not None)


def _physical(root):
    """The bytes the files under ``root`` take on disk, a file with several
    hard links counted once; the lock files left out."""
    seen, total = set(), 0
    for folder, names, files in os.walk(root):
        if Path(folder) == root and ".locks" in names:
            names.remove(".locks")
        for name in files:
            info = _size(Path(folder) / name)
            if info is not None and (info.st_dev, info.st_ino) not in seen:
                seen.add((info.st_dev, info.st_ino))
                total += info.st_size
    return total


def _remove(path):
    """Remove the file, symlink or folder at ``path``; returns the bytes
    freed, nothing for what could not be removed or is still linked
    elsewhere."""
    from transitio.catalog._cache import _unshared

    before = _unshared(path)
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path, ignore_errors=True)
    return before - _unshared(path)


def _feed_id(folder):
    """The id of the feed cached in ``folder``, read from a sidecar whose id
    names that folder; None when none does."""
    import json

    from transitio.catalog._cache import _SIDECAR, _feed_dir, _regular

    for sidecar in folder.glob(f"*{_SIDECAR}"):
        if not _regular(sidecar):
            continue
        try:
            feed_id = json.loads(sidecar.read_text()).get("feed_id")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(feed_id, str) and _feed_dir(feed_id) == folder.name:
            return feed_id
    return None


def _entries(cache):
    """``(path, is_feed)`` of what the cache's root holds besides its locks
    and the downloads being staged:
    each feed's folder, and anything an older transitio left there."""
    from transitio.catalog._cache import _DIGEST, STAGING

    if not cache.root.is_dir() or cache.root.is_symlink():
        return []
    entries = []
    for path in sorted(cache.root.iterdir()):
        if path.name in (".locks", STAGING):
            continue
        is_feed = path.name.startswith("id-") and bool(_DIGEST.fullmatch(path.name[3:]))
        entries.append((path, is_feed and path.is_dir() and not path.is_symlink()))
    return entries


def info(cache_dir=None):
    """What the feed download cache holds, as a ``pandas.DataFrame``.

    One row per cached version: ``feed_id``, ``sha256``, ``source_url`` (of
    its first acquisition), ``archive_bytes``, ``outputs_bytes`` (its sidecar
    and what :func:`~transitio.fetch` made of it), ``served`` (the number of
    requests it served), ``retrieved_at`` (its last acquisition),
    ``last_used_at`` (when a call last delivered it, None when none did),
    ``blob_sha256`` (the stored blob its archive is a hard link to, None for
    a copy of its own) and ``shared_with`` (how many other versions share
    that blob). Anything else in the cache besides its locks and the downloads
    being staged, such as the folders of older
    transitio versions, a version without a readable sidecar or a blob no
    version links to, is one row with ``feed_id`` None and its bytes in
    ``outputs_bytes``. ``attrs["logical_bytes"]`` is the sum of both byte
    columns, ``attrs["physical_bytes"]`` what the cache takes on disk, a
    shared blob counted once, and ``attrs["saved_bytes"]`` their difference.
    Nothing is changed and no lock taken, so a fetch or :func:`clear` running
    meanwhile can make the numbers disagree.

    Parameters
    ----------
    cache_dir : str or pathlib.Path, optional
        The cache's directory, as given to :func:`~transitio.fetch`; defaults
        to the platform cache.
    """
    import json

    import pandas as pd

    from transitio.catalog._cache import _DIGEST, _SIDECAR, _regular, _well_formed

    columns = [
        "feed_id",
        "sha256",
        "source_url",
        "archive_bytes",
        "outputs_bytes",
        "served",
        "retrieved_at",
        "last_used_at",
        "blob_sha256",
        "shared_with",
    ]
    cache = _feed_cache(cache_dir)
    rows, blob_folder = [], None
    for path, is_feed in _entries(cache):
        feed_id = _feed_id(path) if is_feed else None
        if path.name == "blobs" and path.is_dir() and not path.is_symlink():
            # Counted once the versions are known: a blob one of them links
            # to is counted with it.
            blob_folder = path
            continue
        rest = _bytes(path)
        for archive in sorted(path.glob("*.zip")) if feed_id else ():
            sidecar_path = archive.with_suffix(_SIDECAR)
            if not (_regular(archive) and _regular(sidecar_path)):
                continue
            try:
                sidecar = json.loads(sidecar_path.read_text())
            except (OSError, ValueError):
                continue
            if not (
                _DIGEST.fullmatch(archive.stem)
                and _well_formed(sidecar, feed_id, archive.stem)
            ):
                continue
            outputs = path / "outputs"
            records = sidecar["cache"].get("outputs", {})
            made = [outputs / f"{key}.json" for key in records]
            made += [outputs / r["file"] for r in records.values() if r.get("file")]
            outputs_bytes = _bytes(sidecar_path) + sum(map(_bytes, made))
            blob, inode = cache.root / "blobs" / archive.name, None
            with contextlib.suppress(OSError):
                if os.path.samefile(blob, archive):
                    info = os.lstat(archive)
                    inode = (info.st_dev, info.st_ino)
            rows.append(
                {
                    "feed_id": feed_id,
                    "sha256": archive.stem,
                    "source_url": sidecar["cache"]["sources"][0]["source_url"],
                    "archive_bytes": _bytes(archive),
                    "outputs_bytes": outputs_bytes,
                    "served": len(sidecar["cache"].get("served", {})),
                    "retrieved_at": sidecar["retrieved_at"],
                    "last_used_at": sidecar.get("last_used_at"),
                    "blob_sha256": None if inode is None else archive.stem,
                    "shared_with": inode,
                }
            )
            rest -= rows[-1]["archive_bytes"] + outputs_bytes
        if rest > 0:
            rows.append(dict.fromkeys(columns) | {"outputs_bytes": rest})
            rows[-1].update(archive_bytes=0, served=0)
    listed = {row["shared_with"] for row in rows} - {None}
    entries = []
    with contextlib.suppress(OSError):
        entries = list(blob_folder.iterdir()) if blob_folder else []
    rest = 0
    for entry in entries:
        info = _size(entry)
        if info is None:
            continue
        linked = stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) in listed
        rest += 0 if linked else _bytes(entry)
    if rest > 0:
        rows.append(dict.fromkeys(columns) | {"outputs_bytes": rest})
        rows[-1].update(archive_bytes=0, served=0)
    # Shared with the other listed versions linked to the same blob.
    blobs = collections.Counter(
        row["shared_with"] for row in rows if row["shared_with"] is not None
    )
    for row in rows:
        inode = row["shared_with"]
        row["shared_with"] = 0 if inode is None else blobs[inode] - 1
    table = pd.DataFrame(rows, columns=columns)
    logical = int(table["archive_bytes"].sum() + table["outputs_bytes"].sum())
    real = cache.root.is_dir() and not cache.root.is_symlink()
    physical = _physical(cache.root) if real else 0
    table.attrs.update(
        logical_bytes=logical, physical_bytes=physical, saved_bytes=logical - physical
    )
    return table


def clear(cache_dir=None, older_than=None, feeds=None):
    """Remove what the feed download cache holds; returns the bytes freed.

    Without arguments the cache is emptied: every version, sidecar and output,
    and the folders older transitio versions left. ``older_than`` (a
    ``datetime.timedelta``) removes only the versions not delivered within
    that time, or never, with what was made of them; ``feeds`` (feed ids)
    limits the removal to those feeds. The lock files and the folder downloads
    are staged in stay, so a fetch running meanwhile keeps its lock and its
    download.

    What the cache holds when the call starts is listed first, and each feed
    is removed under its lock, so a fetch holding one is waited for. A feed a
    fetch adds after the listing is left for the next call.

    Parameters
    ----------
    cache_dir : str or pathlib.Path, optional
        The cache's directory, as given to :func:`~transitio.fetch`; defaults
        to the platform cache.
    older_than : datetime.timedelta, optional
    feeds : iterable of str, optional
    """
    from transitio.catalog._cache import _feed_dir

    if older_than is not None and not isinstance(older_than, datetime.timedelta):
        raise TypeError("older_than= must be a datetime.timedelta")
    cache = _feed_cache(cache_dir)
    wanted = None if feeds is None else {_feed_dir(feed_id) for feed_id in feeds}
    cutoff = None
    if older_than is not None:
        cutoff = datetime.datetime.now(datetime.timezone.utc) - older_than
    from transitio.catalog._cache import _unshared

    freed = 0
    if not cache.root.is_dir() or cache.root.is_symlink():
        return freed
    for path, is_feed in _entries(cache):
        if not is_feed:
            # The blobs and what an older transitio left belong to no feed.
            if wanted is None and cutoff is None and path.name != "blobs":
                freed += _remove(path)
            continue
        if wanted is not None and path.name not in wanted:
            continue
        with cache.locked(path.name):
            feed_id = _feed_id(path)
            if cutoff is None or feed_id is None:
                freed += cache.remove(path)
                continue
            before = _unshared(path)
            for version in cache.versions(feed_id):
                if not _used_since(version.sidecar.get("last_used_at"), cutoff):
                    freed += cache.delete(version)
            freed += before - _unshared(path)
    blobs = cache.root / "blobs"
    if (
        wanted is None
        and cutoff is None
        and (blobs.is_symlink() or (blobs.exists() and not blobs.is_dir()))
    ):
        # Not the cache's blob folder, so nothing links to it.
        return freed + cache.remove(blobs)
    # A blob goes with its last link; one a fetch linked meanwhile stays.
    return freed + cache.sweep(prune=wanted is None and cutoff is None)


def _used_since(used, cutoff):
    """Whether the time ``used`` (an ISO 8601 string, or None) is at or
    after ``cutoff``; an unreadable time is not."""
    try:
        when = datetime.datetime.fromisoformat(used)
    except (TypeError, ValueError):
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return when >= cutoff
