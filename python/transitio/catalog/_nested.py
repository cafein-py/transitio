"""Feeds inside a larger archive, named by a URL fragment."""

from __future__ import annotations

import os
import shutil
import tempfile
import urllib.parse
import zipfile

from transitio import _http
from transitio.exceptions import DownloadError


def split_fragment(url):
    """``(url without its fragment, member)`` for a feed URL.

    The member is where the percent-decoded fragment, a trailing ``/``
    dropped, says the feed lives: ``("archive", path)`` for a nested zip (a
    name ending ``.zip``), ``("folder", path)`` otherwise. It is None without
    a fragment, or when the fragment is empty, starts with ``/`` or has a
    ``..`` part.
    """
    base, _, fragment = url.partition("#")
    raw = urllib.parse.unquote(fragment)
    path = raw.rstrip("/")
    if not path or raw.startswith("/") or ".." in path.split("/"):
        return base, None
    return base, ("archive" if path.lower().endswith(".zip") else "folder", path)


def _root(names, tables):
    """The prefix a nested zip's feed lives under: ``""`` when one of the
    ``tables`` is at its root, else the one top-level folder holding one
    directly; ``""`` otherwise."""
    if any(name in tables for name in names):
        return ""
    parts = (name.partition("/") for name in names)
    folders = {top for top, _, rest in parts if rest in tables}
    return folders.pop() + "/" if len(folders) == 1 else ""


def _unpack(archive, info, target):
    """Stream the member ``info`` of ``archive`` to a unique temporary file
    beside ``target``; return its path."""
    fd, partial = tempfile.mkstemp(
        dir=target.parent, prefix=target.name + ".", suffix=".part"
    )
    try:
        with os.fdopen(fd, "wb") as handle, archive.open(info) as member:
            shutil.copyfileobj(member, handle, 1 << 20)
    except BaseException:
        _http._discard(partial)
        raise
    return partial


def _repack(source, files, target):
    """Write the ``(info, name)`` members of the open zip ``source`` to a new
    zip at ``target``, each streamed under its ``name``; return its SHA-256
    hex."""
    with _http.replacing(target) as handle:
        with zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED) as out:
            for info, name in files:
                entry = zipfile.ZipInfo(name, info.date_time)
                entry.compress_type = zipfile.ZIP_DEFLATED
                # The declared size decides whether the entry needs ZIP64.
                entry.file_size = info.file_size
                with source.open(info) as reader, out.open(entry, "w") as writer:
                    shutil.copyfileobj(reader, writer, 1 << 20)
        handle.seek(0)
        return _http.sha256_stream(handle)


def _over(what, size, limit):
    return DownloadError(
        f"{what} declares {size} uncompressed bytes, over the {limit} budget "
        "(raise max_total_bytes to read it)"
    )


def extract_feed(archive, member, target, max_total_bytes=None):
    """Write the feed that ``member`` (from :func:`split_fragment`) names in
    the zip ``archive`` to ``target``; return its SHA-256 hex.

    A ``"folder"`` member is the feed's root. An ``"archive"`` member, a
    nested zip, is streamed to a temporary file beside ``target`` and read in
    the outer archive's place, its root being the zip's own when a GTFS file
    is there, else the one top-level folder holding a GTFS file directly. A
    nested zip with its files at its root becomes ``target`` as it is;
    otherwise ``target`` is a new zip of the files directly under the root,
    named without it. ``target`` is replaced only once complete.

    Raises :class:`~transitio.exceptions.DownloadError` when the nested zip
    is not in the archive, no GTFS file is directly under the root, or the
    nested zip or the files under the root declare more uncompressed bytes
    than ``max_total_bytes`` (default: the ``FeedEditor`` budget).
    """
    from transitio.edit._editor import _GTFS_TABLES, _MAX_TOTAL_BYTES

    limit = _MAX_TOTAL_BYTES if max_total_bytes is None else max_total_bytes
    kind, path = member
    target.parent.mkdir(parents=True, exist_ok=True)
    inner = None
    # zipfile reads no more of an entry than it declares, so the declared
    # sizes bound what is written.
    try:
        if kind == "archive":
            with zipfile.ZipFile(archive) as outer:
                try:
                    info = outer.getinfo(path)
                except KeyError:
                    raise DownloadError(
                        f"inner archive {path!r} not in the archive"
                    ) from None
                if info.file_size > limit:
                    raise _over(f"inner archive {path!r}", info.file_size, limit)
                inner = _unpack(outer, info, target)
        with zipfile.ZipFile(inner or archive) as source:
            infos = source.infolist()
            if kind == "folder":
                root = path + "/"
            else:
                root = _root([info.filename for info in infos], _GTFS_TABLES)
            under = [(i, i.filename[len(root) :]) for i in infos]
            files = [
                (info, name)
                for info, name in under
                if info.filename.startswith(root) and name and "/" not in name
            ]
            if not any(name in _GTFS_TABLES for _, name in files):
                raise DownloadError(f"no GTFS files under {path!r}")
            size = sum(info.file_size for info, _ in files)
            if size > limit:
                raise _over(f"the feed under {path!r}", size, limit)
            if inner is None or root:
                return _repack(source, files, target)
        digest = _http.sha256_file(inner)
        os.replace(inner, target)
        inner = None
        return digest
    finally:
        if inner is not None:
            _http._discard(inner)
