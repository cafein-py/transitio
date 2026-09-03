"""Installing and selecting published index snapshots on this machine.

:func:`refresh` lists the index repository's releases, takes the newest one
whose manifest this reader supports, downloads its archive, verifies the
digest the manifest declares, unpacks it defensively into a private staging
directory, validates the layout with the reader, and only then activates it
by one atomic rename into the platformdirs cache — a failed or incompatible
download leaves the previous snapshot in place. :func:`use` selects among
installed snapshots for this process only: it writes nothing to disk.

Which snapshot a query reads is resolved lazily on first index access, never
at import: an active :func:`use` selection, then the ``TRANSITIO_INDEX_SNAPSHOT``
environment variable, then the newest compatible installed snapshot. Nothing
installed is an error that says to run :func:`refresh`, since no snapshot is
bundled with the wheel yet.
"""

import contextlib
import gzip
import hashlib
import io
import os
import shutil
import stat
import tarfile
import tempfile
from pathlib import Path

import httpx
import platformdirs

from transitio.exceptions import DownloadError, IncompatibleIndexError, TransitioError
from transitio.index import release as contract

__all__ = ["refresh", "use", "installed", "active_index", "cache_root", "SNAPSHOT_ENV"]

SNAPSHOT_ENV = "TRANSITIO_INDEX_SNAPSHOT"
KEEP = 3
TIMEOUT = 60.0
USER_AGENT = "transitio-index"

# Per-member ceilings for an archive being unpacked, matching the reader's;
# a member over its ceiling is refused before a byte of it is written.
_MEMBER_LIMITS = {
    "snapshot.json": 8 * 1024 * 1024,
    "NOTICE": 8 * 1024 * 1024,
    "feeds.parquet": 512 * 1024 * 1024,
    "places.parquet": 512 * 1024 * 1024,
    "edges.parquet": 512 * 1024 * 1024,
}

# The most a decompressed archive may stream, extension headers included:
# every member at its ceiling plus header slack. tarfile materialises PAX
# and GNU long-name headers before yielding a member, so the bound sits on
# the stream, not on the members.
_HEADER_SLACK = 1024 * 1024
_MAX_STREAM_BYTES = sum(_MEMBER_LIMITS.values()) + _HEADER_SLACK


class _Bounded:
    """A read-only file object that refuses to yield more than ``limit``."""

    def __init__(self, raw, limit):
        self._raw = raw
        self._left = limit

    def read(self, size=-1):
        data = self._raw.read(size)
        self._left -= len(data)
        if self._left < 0:
            raise DownloadError("the archive expands beyond what a snapshot can hold")
        return data


# Process-local: the selection use() made (with the cache it lives in), the
# handle it resolves to, and the shared locks that keep both from pruning.
_state = {
    "selection": None,
    "selection_root": None,
    "selection_lock": None,
    "handle": None,
    "handle_key": None,
    "handle_lock": None,
}


def cache_root(cache_dir=None):
    """Where snapshots live: ``<user cache>/transitio/index`` unless given;
    always absolute, so a later change of directory moves nothing."""
    if cache_dir is not None:
        return Path(os.path.abspath(cache_dir))
    return Path(os.path.abspath(platformdirs.user_cache_dir("transitio"))) / "index"


def _snapshots(root):
    return root / "snapshots"


def _read_snapshot(path):
    """A snapshot's ``snapshot.json`` as a dict, or None when unreadable."""
    from transitio.index import SNAPSHOT_FILE, _MAX_SNAPSHOT_BYTES, _read_regular
    import json

    try:
        data = _read_regular(path / SNAPSHOT_FILE, _MAX_SNAPSHOT_BYTES)
        snapshot = json.loads(data.decode("utf-8"))
    except (OSError, ValueError, TransitioError):
        return None
    return snapshot if isinstance(snapshot, dict) else None


def _reparse_point(path):
    """A Windows junction or symlink, which ``is_symlink`` alone misses."""
    if os.name != "nt":
        return False
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _installed_snapshot(root, snapshot_id):
    """The manifest of an installed snapshot, or None: the entry must be a
    real directory (never a symlink) whose manifest names that very id."""
    path = _snapshots(root) / snapshot_id
    if path.is_symlink() or not path.is_dir() or _reparse_point(path):
        return None
    snapshot = _read_snapshot(path)
    if snapshot is None or snapshot.get("snapshot_id") != snapshot_id:
        return None
    return snapshot


def _verified(root, snapshot_id):
    """Whether the installed snapshot is whole: every release member a
    bounded regular file, and the reader reading it back under the same id
    with its places and edges."""
    from transitio.index import read_index

    if _installed_snapshot(root, snapshot_id) is None:
        return False
    path = _snapshots(root) / snapshot_id
    for name in contract.MEMBERS:
        try:
            info = os.lstat(path / name)
        except OSError:
            return False
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MEMBER_LIMITS[name]:
            return False
    try:
        index = read_index(path)
    except (TransitioError, ValueError, OSError):
        return False
    return (
        index.snapshot_id == snapshot_id
        and index.places is not None
        and index.edges is not None
    )


def installed(*, cache_dir=None):
    """The installed snapshots as ``(snapshot_id, snapshot manifest)``,
    newest build first."""
    root = cache_root(cache_dir)
    found = []
    if _snapshots(root).is_dir():
        for entry in _snapshots(root).iterdir():
            if not contract.is_snapshot_id(entry.name):
                continue
            snapshot = _installed_snapshot(root, entry.name)
            if snapshot is not None:
                found.append((entry.name, snapshot))
    found.sort(
        key=lambda item: (str(item[1].get("built_at") or ""), item[0]), reverse=True
    )
    return found


def _lock_file(root, snapshot_id):
    return _snapshots(root) / f"{snapshot_id}.lock"


def _hold(root, snapshot_id):
    """A shared advisory lock on the snapshot, held while it is selected or
    loaded, so a refresh in another process cannot prune it away; None
    where the platform has no shared locks (nothing is pruned there)."""
    try:
        import fcntl
    except ImportError:  # Windows
        return None
    _snapshots(root).mkdir(parents=True, exist_ok=True)
    handle = open(_lock_file(root, snapshot_id), "a+b")
    fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
    return handle


def _release(key):
    handle = _state.get(key)
    if handle is not None:
        handle.close()
    _state[key] = None


def use(snapshot=None, *, cache_dir=None):
    """Select an installed snapshot for this process; ``None`` clears the
    selection. Selects only, never downloads, and writes nothing to disk
    but the advisory lock that keeps the selection from being pruned."""
    root = cache_root(cache_dir)
    lock = None
    if snapshot is not None:
        if not contract.is_snapshot_id(snapshot):
            raise ValueError(f"not a snapshot id: {snapshot!r}")
        # Held before the check: a pruner cannot take it away in between.
        lock = _hold(root, snapshot)
        if not _verified(root, snapshot):
            if lock is not None:
                lock.close()
            raise TransitioError(
                f"snapshot {snapshot} is not installed whole; run "
                "transitio.index.refresh()"
            )
    _release("selection_lock")
    _release("handle_lock")
    _state["selection"] = snapshot
    _state["selection_root"] = None if snapshot is None else root
    _state["selection_lock"] = lock
    _state["handle"] = None
    _state["handle_key"] = None


def _pinned(cache_dir=None):
    """``(cache root, snapshot id or None)``: the use() selection in the
    cache it was made in, else the environment pin in the given cache."""
    if _state["selection"] is not None:
        return _state["selection_root"], _state["selection"]
    return cache_root(cache_dir), os.environ.get(SNAPSHOT_ENV) or None


def _load(root, snapshot_id):
    """The snapshot read under its shared lock, or None when it is not (or
    no longer) installed; the lock travels with the handle."""
    from transitio.index import read_index

    lock = _hold(root, snapshot_id)
    try:
        if _installed_snapshot(root, snapshot_id) is None:
            raise TransitioError("not installed")
        index = read_index(_snapshots(root) / snapshot_id)
        if index.snapshot_id != snapshot_id:
            raise TransitioError("names another snapshot")
    except (TransitioError, ValueError, OSError):
        # Not installed, or not whole: a candidate to pass over, and for a
        # pin the caller's error says to refresh.
        if lock is not None:
            lock.close()
        return None, None
    except BaseException:
        if lock is not None:
            lock.close()
        raise
    return index, lock


def active_index(*, cache_dir=None):
    """The :class:`~transitio.index.Index` queries read when none is passed,
    resolved lazily: the :func:`use` selection, then ``TRANSITIO_INDEX_SNAPSHOT``,
    then the newest compatible installed snapshot."""
    root, pinned = _pinned(cache_dir)
    if pinned is not None and not contract.is_snapshot_id(pinned):
        raise TransitioError(f"{SNAPSHOT_ENV}: not a snapshot id: {pinned!r}")
    # A handle is reused only for the resolution that produced it: a pin
    # for the same id, or an automatic pick in the same cache.
    key = _state["handle_key"]
    if key is not None and key[0] == str(root):
        if pinned is not None and key[1:] == (pinned, "pin"):
            return _state["handle"]
        if pinned is None and key[2] == "auto":
            return _state["handle"]
    if pinned is not None:
        index, lock = _load(root, pinned)
        if index is None:
            raise TransitioError(
                f"snapshot {pinned} is not installed; run transitio.index.refresh()"
            )
    else:
        index = lock = None
        for snapshot_id, snapshot in installed(cache_dir=root):
            if not contract.compatible(snapshot)[0]:
                continue
            index, lock = _load(root, snapshot_id)
            if index is not None:
                break
        if index is None:
            raise TransitioError(
                "no compatible feed index snapshot is installed; run "
                "transitio.index.refresh()"
            )
    _release("handle_lock")
    _state["handle"] = index
    _state["handle_key"] = (str(root), index.snapshot_id, "pin" if pinned else "auto")
    _state["handle_lock"] = lock
    return index


def _client(api_url, transport):
    return httpx.Client(
        base_url=api_url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        },
        timeout=TIMEOUT,
        follow_redirects=False,
        transport=transport,
    )


def _unpack(data, staging):
    """Write the archive's members into ``staging``: only the expected names,
    each once, regular files only, within their ceilings — a digest proves
    the archive is ours, not that extracting it is safe."""
    seen = set()
    try:
        stream = _Bounded(gzip.GzipFile(fileobj=io.BytesIO(data)), _MAX_STREAM_BYTES)
        with tarfile.open(fileobj=stream, mode="r|") as tar:
            for member in tar:
                name = member.name
                if name not in contract.MEMBERS:
                    raise DownloadError(
                        f"the archive holds an unexpected member {name!r}"
                    )
                if name in seen:
                    raise DownloadError(f"the archive holds {name!r} twice")
                if not member.isreg():
                    raise DownloadError(
                        f"the archive member {name!r} is not a regular file"
                    )
                if member.size > _MEMBER_LIMITS[name]:
                    raise DownloadError(f"the archive member {name!r} is too large")
                seen.add(name)
                handle = tar.extractfile(member)
                content = handle.read(member.size + 1) if handle is not None else b""
                if len(content) != member.size:
                    raise DownloadError(f"the archive member {name!r} is truncated")
                with open(staging / name, "xb") as out:
                    out.write(content)
    except (tarfile.TarError, EOFError, OSError) as error:
        raise DownloadError(f"the archive could not be read: {error}") from error
    missing = [name for name in contract.MEMBERS if name not in seen]
    if missing:
        raise DownloadError(f"the archive lacks {', '.join(missing)}")


@contextlib.contextmanager
def _installing(root):
    """One refresh at a time per cache: an advisory lock on the snapshots
    directory, so two processes cannot race the activation."""
    snapshots = _snapshots(root)
    snapshots.mkdir(parents=True, exist_ok=True)
    handle = open(snapshots / ".lock", "a+b")
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except ImportError:  # Windows
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        yield
    finally:
        handle.close()


def _install(root, snapshot_id, data):
    """Unpack, validate and activate one snapshot under the cache lock; the
    previous one stays whole whatever fails. A target that another refresh
    completed meanwhile is accepted once it reads back whole; a damaged one
    is set aside and replaced."""
    from transitio.index import read_index

    snapshots = _snapshots(root)
    staging = Path(tempfile.mkdtemp(prefix=f".incoming-{snapshot_id}-", dir=snapshots))
    try:
        _unpack(data, staging)
        try:
            index = read_index(staging)
        except (TransitioError, ValueError, OSError) as error:
            raise DownloadError(
                f"the downloaded snapshot is unreadable: {error}"
            ) from error
        if index.snapshot_id != snapshot_id:
            raise DownloadError(
                f"the archive holds snapshot {index.snapshot_id!r}, not {snapshot_id!r}"
            )
        if index.places is None or index.edges is None:
            raise DownloadError("the snapshot lacks its places or edges table")
        target = snapshots / snapshot_id
        if target.exists() or target.is_symlink():
            if _verified(root, snapshot_id):
                return
            aside = Path(
                tempfile.mkdtemp(prefix=f".damaged-{snapshot_id}-", dir=snapshots)
            )
            os.replace(target, aside / "snapshot")
            shutil.rmtree(aside, ignore_errors=True)
        os.replace(staging, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _prune(root, keep, protect):
    """Remove all but the ``keep`` newest installed snapshots, never one in
    ``protect`` nor one another process holds selected or loaded (its
    shared lock refuses the exclusive one taken here). Each removal is a
    rename to a private tombstone under that lock, so a failure to delete
    the tombstone's contents cannot leave a half-present snapshot. Returns
    the ids removed and the tombstones that could not be deleted."""
    try:
        import fcntl
    except ImportError:
        # Windows: no shared locks to protect other processes' selections,
        # so nothing is pruned there.
        return [], []
    removed = []
    leftover = []
    for position, (snapshot_id, _) in enumerate(installed(cache_dir=root)):
        if position < keep or snapshot_id in protect:
            continue
        lock = open(_lock_file(root, snapshot_id), "a+b")
        try:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                continue
            tombstone = Path(
                tempfile.mkdtemp(
                    prefix=f".removed-{snapshot_id}-", dir=_snapshots(root)
                )
            )
            os.replace(_snapshots(root) / snapshot_id, tombstone / "snapshot")
            with contextlib.suppress(OSError):
                os.unlink(_lock_file(root, snapshot_id))
            removed.append(snapshot_id)
        finally:
            lock.close()
        shutil.rmtree(tombstone, ignore_errors=True)
        if tombstone.exists():
            leftover.append(str(tombstone))
    return removed, leftover


def refresh(
    *,
    repository=contract.DEFAULT_REPOSITORY,
    api_url=contract.API_URL,
    cache_dir=None,
    transport=None,
    keep=KEEP,
):
    """Install the newest published snapshot this reader supports.

    Returns a summary: the snapshot id, whether it was newly ``installed``
    (a damaged install is replaced), the releases ``skipped`` (newer but
    incompatible, or incomplete), the older snapshots ``removed`` to keep
    the cache at ``keep`` and any ``leftover`` tombstone that could not be
    deleted. Raises :class:`~transitio.exceptions.DownloadError`
    when the listing is unreachable or the archive fails verification, and
    :class:`~transitio.exceptions.IncompatibleIndexError` when no listed
    release is one this transitio reads; the active snapshot stays.
    """
    root = cache_root(cache_dir)
    with _client(api_url, transport) as client:
        try:
            releases = contract.list_releases(client, repository)
        except contract.ReleaseError as error:
            raise DownloadError(str(error)) from error
        release, manifest, skipped = contract.newest_compatible(
            releases, lambda asset: contract.read_manifest(client, asset)
        )
        if manifest is None:
            reasons = "; ".join(f"{tag}: {reason}" for tag, reason in skipped)
            raise IncompatibleIndexError(
                "no published index release is one this transitio reads"
                + (f" ({reasons})" if reasons else "")
            )
        snapshot_id = manifest["snapshot_id"]
        data = None
        if not _verified(root, snapshot_id):
            assets = {a.get("name"): a for a in release.get("assets") or ()}
            asset = assets.get(contract.archive_name(snapshot_id))
            declared = manifest.get("archive") or {}
            expected = declared.get("sha256")
            size = declared.get("bytes")
            # The manifest bounds the download exactly: no size, no download.
            if (
                asset is None
                or not isinstance(expected, str)
                or type(size) is not int
                or not 0 < size <= contract.MAX_ASSET_BYTES
            ):
                raise DownloadError(
                    f"release {release.get('tag_name')} does not declare a verifiable "
                    "archive"
                )
            try:
                data = contract.download(
                    client, asset["browser_download_url"], "archive", size
                )
            except (contract.ReleaseError, KeyError) as error:
                raise DownloadError(str(error)) from error
            if len(data) != size or hashlib.sha256(data).hexdigest() != expected:
                raise DownloadError(
                    "the archive does not match the size and digest its manifest "
                    "declares"
                )
    # Verified, installed and pruned as one step under the cache lock: the
    # download above is the only slow part, and nothing decided before it
    # is trusted once the lock is held.
    with _installing(root):
        already = _verified(root, snapshot_id)
        if not already:
            if data is None:
                raise DownloadError(
                    f"snapshot {snapshot_id} was removed while being verified; run "
                    "transitio.index.refresh() again"
                )
            _install(root, snapshot_id, data)
        removed, leftover = _prune(root, keep, {snapshot_id, _pinned(cache_dir)[1]})
    # The next query resolves afresh: a newer snapshot may now be active.
    _release("handle_lock")
    _state["handle"] = None
    _state["handle_key"] = None
    return {
        "snapshot_id": snapshot_id,
        "installed": not already,
        "skipped": [{"tag": tag, "reason": reason} for tag, reason in skipped],
        "removed": removed,
        "leftover": leftover,
    }
