"""Inspect and clear the feed download cache.

Every feed transitio downloads is kept under ``<cache_dir>/gtfs``, by default
in the platform cache, as versions named by their SHA-256 beside a
``.provenance.json`` sidecar, together with what :func:`transitio.fetch` made
of them (:mod:`transitio.catalog._cache`). :func:`info` lists what the cache
holds and :func:`clear` removes it.
"""

from __future__ import annotations

import datetime
import os
import shutil
from pathlib import Path

__all__ = ["clear", "info"]


def _feed_cache(cache_dir):
    import platformdirs

    from transitio.catalog._cache import FeedCache

    return FeedCache(cache_dir or platformdirs.user_cache_dir("transitio"))


def _bytes(path):
    """The bytes of the file at ``path``, or of every file under it, symlinks
    not followed."""
    if path.is_symlink() or not path.is_dir():
        try:
            return os.lstat(path).st_size
        except FileNotFoundError:
            return 0
    return sum(
        os.lstat(Path(root) / name).st_size
        for root, _, names in os.walk(path)
        for name in names
    )


def _remove(path):
    """Remove the file, symlink or folder at ``path``; returns the bytes
    freed, nothing for what could not be removed."""
    size = _bytes(path)
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path, ignore_errors=True)
    return size - _bytes(path)


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
    """``(path, is_feed)`` of what the cache's root holds besides its locks:
    each feed's folder, and anything an older transitio left there."""
    from transitio.catalog._cache import _DIGEST

    if not cache.root.is_dir() or cache.root.is_symlink():
        return []
    entries = []
    for path in sorted(cache.root.iterdir()):
        if path.name == ".locks":
            continue
        is_feed = path.name.startswith("id-") and bool(_DIGEST.fullmatch(path.name[3:]))
        entries.append((path, is_feed and path.is_dir() and not path.is_symlink()))
    return entries


def info(cache_dir=None):
    """What the feed download cache holds, as a ``pandas.DataFrame``.

    One row per cached version: ``feed_id``, ``sha256``, ``source_url`` (of
    its first acquisition), ``archive_bytes``, ``outputs_bytes`` (its sidecar
    and what :func:`~transitio.fetch` made of it), ``served`` (the number of
    requests it served), ``retrieved_at`` (its last acquisition) and
    ``last_used_at`` (when a call last delivered it, None when none did).
    Anything else in the cache, such as the folders of older transitio
    versions or a version without a readable sidecar, is one row with
    ``feed_id`` None and its bytes in ``outputs_bytes``.
    ``attrs["logical_bytes"]`` is the sum of both byte columns. Nothing is
    changed.

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
    ]
    rows = []
    for path, is_feed in _entries(_feed_cache(cache_dir)):
        feed_id = _feed_id(path) if is_feed else None
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
                }
            )
            rest -= rows[-1]["archive_bytes"] + outputs_bytes
        if rest > 0:
            rows.append(dict.fromkeys(columns) | {"outputs_bytes": rest})
            rows[-1].update(archive_bytes=0, served=0)
    table = pd.DataFrame(rows, columns=columns)
    table.attrs["logical_bytes"] = int(
        table["archive_bytes"].sum() + table["outputs_bytes"].sum()
    )
    return table


def clear(cache_dir=None, older_than=None, feeds=None):
    """Remove what the feed download cache holds; returns the bytes freed.

    Without arguments the cache is emptied: every version, sidecar and output,
    and the folders older transitio versions left. ``older_than`` (a
    ``datetime.timedelta``) removes only the versions not delivered within
    that time, or never, with what was made of them; ``feeds`` (feed ids)
    limits the removal to those feeds. The lock files stay, so a fetch running
    meanwhile keeps its own lock.

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
    freed = 0
    for path, is_feed in _entries(cache):
        if not is_feed:
            # Left by an older transitio, it belongs to no feed's lock.
            if wanted is None and cutoff is None:
                freed += _remove(path)
            continue
        if wanted is not None and path.name not in wanted:
            continue
        with cache.locked(path.name):
            feed_id = _feed_id(path)
            if cutoff is None or feed_id is None:
                freed += _remove(path)
                continue
            before = _bytes(path)
            for version in cache.versions(feed_id):
                if not _used_since(version.sidecar.get("last_used_at"), cutoff):
                    cache.delete(version)
            freed += before - _bytes(path)
    return freed


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
