"""Installing and selecting published index snapshots on this machine.

:func:`refresh` lists the index repository's releases, takes the newest one
whose manifest this reader supports, streams its archive into the cache,
verifies the size and digest the manifest declares, unpacks it defensively
into a private staging directory, validates the layout with the reader, and
only then activates it by one atomic rename into the platformdirs cache — a
failed or incompatible download leaves the previous snapshot in place. From
schema 12 the archive holds the core tables; each on-demand file is
downloaded into the snapshot the first time a query needs it, from the
release its ``release.json`` names.
:func:`use` selects among installed snapshots for this process only: it
writes nothing to disk.

Which snapshot a query reads is resolved lazily on first index access, never
at import: an active :func:`use` selection, then the ``TRANSITIO_INDEX_SNAPSHOT``
environment variable, then the newest compatible installed snapshot, then the
index bundled in the wheel. Nothing installed and nothing bundled is an error
that says to run :func:`refresh`.
"""

import contextlib
import gzip
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
import warnings
import weakref
from pathlib import Path

import platformdirs

from transitio import _http, _progress
from transitio.exceptions import DownloadError, IncompatibleIndexError, TransitioError
from transitio.index import release as contract

__all__ = ["refresh", "use", "installed", "active_index", "cache_root", "SNAPSHOT_ENV"]

SNAPSHOT_ENV = "TRANSITIO_INDEX_SNAPSHOT"
KEEP = 10
TIMEOUT = 60.0
# Beside snapshot.json from schema 12: the release the snapshot came from,
# whose assets its on-demand files are downloaded from.
RELEASE_FILE = "release.json"
DOWNLOAD_HOST = "https://github.com"
# A GitHub ``owner/name``, the only repository a release.json may name.
_REPOSITORY = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/(?!\.\.?$)[A-Za-z0-9._-]+"
)

# Per-member ceilings for an archive being unpacked, matching the reader's;
# a member over its ceiling is refused before a byte of it is written.
_MEMBER_LIMITS = {
    "snapshot.json": 8 * 1024 * 1024,
    "NOTICE": 8 * 1024 * 1024,
    "feeds.parquet": 512 * 1024 * 1024,
    "realtime.parquet": 512 * 1024 * 1024,
    "places.parquet": 512 * 1024 * 1024,
    "edges.parquet": 512 * 1024 * 1024,
    "boundaries.parquet": 512 * 1024 * 1024,
    "details.parquet": 512 * 1024 * 1024,
    "access_providers.parquet": 8 * 1024 * 1024,
}


def _member_limit(name):
    """The byte ceiling of a member, a partition table counting as its
    table."""
    return _MEMBER_LIMITS[name.rpartition("/")[2]]


def _is_member_name(name):
    return (
        name in contract.MEMBERS
        or name == contract.ACCESS_PROVIDERS_MEMBER
        or bool(contract.PARTITION_MEMBER.fullmatch(name))
    )


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


# Process-local: the selection use() made (with the cache it lives in and
# the shared lock that keeps it from pruning), the handle it resolves to,
# an index holding its own lock, and the transport the last refresh was
# given, which on-demand downloads use too.
_state = {
    "selection": None,
    "selection_root": None,
    "selection_lock": None,
    "handle": None,
    "handle_key": None,
    "transport": None,
}


def cache_root(cache_dir=None):
    """Where snapshots live: ``<user cache>/transitio/index`` unless given;
    always absolute, so a later change of directory moves nothing."""
    if cache_dir is not None:
        return Path(os.path.abspath(cache_dir))
    return Path(os.path.abspath(platformdirs.user_cache_dir("transitio"))) / "index"


def _snapshots(root):
    return root / "snapshots"


def _read_json(file):
    """The JSON object in ``file``, a bounded regular file, or None when it
    cannot be read as one."""
    from transitio.index import _MAX_SNAPSHOT_BYTES, _read_regular

    try:
        found = json.loads(_read_regular(file, _MAX_SNAPSHOT_BYTES).decode("utf-8"))
    except (OSError, ValueError, TransitioError):
        return None
    return found if isinstance(found, dict) else None


def _read_snapshot(path):
    """A snapshot's ``snapshot.json`` as a dict, or None when unreadable."""
    from transitio.index import SNAPSHOT_FILE

    return _read_json(path / SNAPSHOT_FILE)


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
    if not _whole_members(path):
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


def _whole_members(path, *, bundled=False):
    """Every release member present at ``path`` as a bounded regular file
    (never a symlink or an over-large one) -- the licence ``NOTICE`` among
    them, since it is authoritative for the snapshot. The members are the
    snapshot's own (a schema-7 snapshot lists its partitions), so the
    snapshot is read first. From schema 12 an installed snapshot also needs
    a ``release.json`` listing its on-demand files as released, which are
    downloaded when first used; a ``bundled`` one, which no release backs,
    every on-demand file instead."""
    snapshot = _read_snapshot(path)
    if snapshot is None:
        return False
    try:
        names = contract.members(snapshot)
        parts = contract.on_demand(snapshot)
    except ValueError:
        return False
    if bundled:
        names += parts
    elif parts and not _released(snapshot, _read_json(path / RELEASE_FILE)):
        return False
    for name in names:
        try:
            info = os.lstat(path / name)
        except OSError:
            return False
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= _member_limit(
            name
        ):
            return False
    return True


def _part_entry(snapshot, member, entry):
    """Whether ``entry`` describes the on-demand ``member`` of ``snapshot``
    as released: its asset's name, and the size and digest the snapshot
    lists."""
    partition, _, file = member.partition("/")
    table = file.removesuffix(".parquet")
    listed = snapshot["partitions"][partition][table]
    name = contract.part_asset_name(snapshot["snapshot_id"], partition, table)
    return (
        isinstance(entry, dict)
        and isinstance(listed, dict)
        and entry.get("name") == name
        and isinstance(entry.get("sha256"), str)
        and entry["sha256"] == listed.get("sha256")
        and type(entry.get("bytes")) is int
        and 0 < entry["bytes"] <= _member_limit(member)
        and entry["bytes"] == listed.get("bytes")
    )


def _lists_parts(snapshot, parts):
    """Whether ``parts`` (a release manifest's, or ``release.json``'s) lists
    exactly the on-demand files of ``snapshot``, each as released."""
    expected = contract.on_demand(snapshot)
    return (
        isinstance(parts, dict)
        and sorted(parts) == expected
        and all(_part_entry(snapshot, member, parts[member]) for member in expected)
    )


def _release_record(repository, snapshot_id, parts):
    """The ``release.json`` of a snapshot released by ``repository``: its
    tag, the URL its assets download from, and the on-demand ``parts``."""
    tag = contract.release_tag(snapshot_id)
    return {
        "repository": repository,
        "tag": tag,
        "url": f"{DOWNLOAD_HOST}/{repository}/releases/download/{tag}/",
        "parts": parts,
    }


def _released(snapshot, record):
    """Whether ``record`` is the ``release.json`` of ``snapshot``: a GitHub
    repository's release under the snapshot's own tag and download URL,
    listing exactly its on-demand files as released."""
    repository = record.get("repository") if isinstance(record, dict) else None
    return (
        isinstance(repository, str)
        and _REPOSITORY.fullmatch(repository) is not None
        and record
        == _release_record(repository, snapshot["snapshot_id"], record.get("parts"))
        and _lists_parts(snapshot, record["parts"])
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
    no longer) installed. The index holds the lock until it is collected,
    so nothing read from it loses its snapshot to a prune."""
    from transitio.index import read_index

    lock = _hold(root, snapshot_id)
    try:
        path = _snapshots(root) / snapshot_id
        if _installed_snapshot(root, snapshot_id) is None or not _whole_members(path):
            raise TransitioError("not installed")
        index = read_index(path)
        if index.snapshot_id != snapshot_id:
            raise TransitioError("names another snapshot")
        if index.places is None or index.edges is None:
            raise TransitioError("not whole")
    except (TransitioError, ValueError, OSError):
        # Not installed, or not whole: a candidate to pass over, and for a
        # pin the caller's error says to refresh.
        if lock is not None:
            lock.close()
        return None
    except BaseException:
        if lock is not None:
            lock.close()
        raise
    if lock is not None:
        index._lease = lock
        weakref.finalize(index, lock.close)
    return index


def _fetch_part(index, partition, table):
    """Download the on-demand ``table`` of ``partition`` that the directory
    of ``index`` lacks (:func:`_download_part`), with a progress bar; a
    failed download says how to fetch every on-demand file in advance."""
    from transitio.index import _TABLE_FILES

    member = f"{partition}/{_TABLE_FILES[table]}"
    try:
        _download_part(index._path, index.snapshot, member, _state["transport"])
    except DownloadError as error:
        raise TransitioError(
            f"{error}; transitio.index.refresh(everything=True) downloads every "
            "on-demand file in advance"
        ) from error


def _download_part(path, snapshot, member, transport, progress=True):
    """Download the on-demand ``member`` of the snapshot at ``path`` from
    the release its ``release.json`` names, through a private file beside
    it that replaces it only once its size and digest match what both that
    and the snapshot list. A directory without a ``release.json`` listing
    the file must carry its own, so this refuses."""
    from transitio.index import _partition_directory

    file = path / member
    record = _read_json(path / RELEASE_FILE) or {}
    parts = record.get("parts")
    entry = parts.get(member) if isinstance(parts, dict) else None
    if entry is None:
        raise TransitioError(f"{file}: missing; the index directory does not carry it")
    if not _released(snapshot, record):
        raise TransitioError(
            f"{file}: release.json does not describe the snapshot's release"
        )
    # Checked again before writing, so the file lands in the partition's own
    # directory and not through a link put there since the index was read.
    _partition_directory(path, member.partition("/")[0])
    report = None
    if progress:
        report = _progress.Download(
            f"Downloading {member} of feed index snapshot {snapshot['snapshot_id']}"
        )
    try:
        with (
            _http.client(
                timeout=TIMEOUT, follow_redirects=True, transport=transport
            ) as client,
            _http.staged(file) as partial,
        ):
            digest = _http.download(
                client,
                record["url"] + entry["name"],
                partial,
                limit=entry["bytes"],
                progress=report,
            )
            if partial.stat().st_size != entry["bytes"] or digest != entry["sha256"]:
                raise DownloadError(
                    "the download does not match the size and digest its snapshot "
                    "lists"
                )
    except DownloadError as error:
        raise DownloadError(f"{file}: {error}") from error
    except OSError:
        # Another download put the file in place first (Windows refuses to
        # replace it while it is open); that copy is checked when it is read.
        if not os.path.lexists(file):
            raise
    finally:
        if report is not None:
            report.close()


def _bundled_root():
    """The directory the wheel bundles the index into, beside this package.
    A wheel with a compiled extension is never zip-imported, so the package
    files are always a real directory on disk."""
    import importlib.resources

    return Path(str(importlib.resources.files("transitio.index")))


def _bundled():
    """The index bundled in the wheel, or None when none was shipped or it
    cannot be read or is incompatible. It carries no lock: it is read-only
    package data that no refresh prunes."""
    from transitio.index import read_index

    try:
        root = _bundled_root()
    except (ImportError, TypeError, OSError):
        return None
    # Whole like an installed snapshot -- every member present, the licence
    # NOTICE included, and every on-demand file, as nothing is downloaded
    # into package data -- with a readable places-and-edges index; a partial
    # bundle is no more use than an uninstalled one.
    if not _whole_members(root, bundled=True):
        return None
    try:
        index = read_index(root)
    except (TransitioError, ValueError, OSError):
        return None
    # Licensed like a released snapshot: a development build can be whole yet
    # carry ``licensed: false``, which the release publisher refuses, so the
    # wheel must not activate one either.
    if index.places is None or index.edges is None:
        return None
    if index.snapshot.get("licensed") is not True:
        return None
    return index


def active_index(*, cache_dir=None):
    """The :class:`~transitio.index.Index` queries read when none is passed,
    resolved lazily: the :func:`use` selection, then ``TRANSITIO_INDEX_SNAPSHOT``,
    then the newest compatible installed snapshot, then the index bundled in
    the wheel."""
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
        index = _load(root, pinned) or _bundled()
        if index is not None and index.snapshot_id != pinned:
            index = None
        if index is None:
            raise TransitioError(
                f"snapshot {pinned} is not installed; run transitio.index.refresh()"
            )
    else:
        index = None
        for snapshot_id, snapshot in installed(cache_dir=root):
            if not contract.compatible(snapshot)[0]:
                continue
            index = _load(root, snapshot_id)
            if index is not None:
                break
        if index is None:
            index = _bundled()
        if index is None:
            raise TransitioError(
                "no compatible feed index snapshot is installed; run "
                "transitio.index.refresh()"
            )
    _state["handle"] = index
    _state["handle_key"] = (str(root), index.snapshot_id, "pin" if pinned else "auto")
    return index


def _client(api_url, transport):
    return _http.client(
        base_url=api_url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=TIMEOUT,
        follow_redirects=False,
        transport=transport,
    )


def _unpack(archive, staging):
    """Write the members of the archive at ``archive`` into ``staging``: only
    the expected names, each once, regular files only, within their ceilings
    — a digest proves the archive is ours, not that extracting it is safe."""
    seen = set()
    try:
        with (
            gzip.open(archive, "rb") as compressed,
            tarfile.open(
                fileobj=_Bounded(compressed, _MAX_STREAM_BYTES), mode="r|"
            ) as tar,
        ):
            for member in tar:
                name = member.name
                if not _is_member_name(name):
                    raise DownloadError(
                        f"the archive holds an unexpected member {name!r}"
                    )
                if name in seen:
                    raise DownloadError(f"the archive holds {name!r} twice")
                if not member.isreg():
                    raise DownloadError(
                        f"the archive member {name!r} is not a regular file"
                    )
                if member.size > _member_limit(name):
                    raise DownloadError(f"the archive member {name!r} is too large")
                seen.add(name)
                handle = tar.extractfile(member)
                content = handle.read(member.size + 1) if handle is not None else b""
                if len(content) != member.size:
                    raise DownloadError(f"the archive member {name!r} is truncated")
                partition = name.rpartition("/")[0]
                if partition:
                    (staging / partition).mkdir(exist_ok=True)
                with open(staging / name, "xb") as out:
                    out.write(content)
    except (tarfile.TarError, EOFError, OSError) as error:
        raise DownloadError(f"the archive could not be read: {error}") from error
    # The members owed are the snapshot's own, read once the stream ends; an
    # unreadable snapshot owes the flat five and is refused by the reader.
    snapshot = _read_snapshot(staging) if "snapshot.json" in seen else None
    try:
        expected = (
            list(contract.MEMBERS) if snapshot is None else contract.members(snapshot)
        )
    except ValueError as error:
        raise DownloadError(f"the archive's snapshot {error}") from error
    missing = [name for name in expected if name not in seen]
    if missing:
        raise DownloadError(f"the archive lacks {', '.join(missing)}")
    extra = sorted(seen - set(expected))
    if extra:
        raise DownloadError(
            f"the archive holds members the snapshot does not list: {', '.join(extra)}"
        )


@contextlib.contextmanager
def _installing(root):
    """One refresh at a time per cache: an advisory lock on the snapshots
    directory, so two processes cannot race the activation."""
    with _http.locked(_snapshots(root) / ".lock"):
        yield


def _install(root, snapshot_id, archive, record=None):
    """Unpack the archive at ``archive``, validate and activate its snapshot
    under the cache lock; the previous one stays whole whatever fails. A
    target that another refresh completed meanwhile is accepted once it reads
    back whole; a damaged one is set aside and replaced. From schema 12
    ``record``, kept as ``release.json``, must list the snapshot's on-demand
    files as released."""
    from transitio.index import read_index

    snapshots = _snapshots(root)
    staging = Path(tempfile.mkdtemp(prefix=f".incoming-{snapshot_id}-", dir=snapshots))
    try:
        _unpack(archive, staging)
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
        if contract.on_demand(index.snapshot):
            if not _released(index.snapshot, record):
                raise DownloadError(
                    "the release's manifest does not list the snapshot's on-demand "
                    "files as released"
                )
            (staging / RELEASE_FILE).write_text(
                json.dumps(record, indent=2, sort_keys=True), encoding="utf-8"
            )
        # read_index never reads NOTICE, so an archive with an empty licence
        # NOTICE reads back fine; the wholeness check keeps it from activating.
        if not _whole_members(staging):
            raise DownloadError("the downloaded snapshot is not whole")
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
    """Remove all but the ``keep`` newest installed snapshots (none when
    ``keep`` is None), never one in ``protect`` nor one another process
    holds selected or loaded (its shared lock refuses the exclusive one
    taken here). Each removal is a rename to a private tombstone under that
    lock, so a failure to delete the tombstone's contents cannot leave a
    half-present snapshot. Returns the ids removed, the tombstones that
    could not be deleted and the bytes each removed snapshot took (None
    when they could not be read)."""
    try:
        import fcntl
    except ImportError:
        # Windows: no shared locks to protect other processes' selections,
        # so nothing is pruned there.
        return [], [], {}
    removed, leftover, sizes = [], [], {}
    if keep is None:
        return removed, leftover, sizes
    for position, (snapshot_id, _) in enumerate(installed(cache_dir=root)):
        if position < keep or snapshot_id in protect:
            continue
        # A snapshot that cannot be locked or moved aside stays installed, so
        # nothing removed goes unreported.
        try:
            lock = open(_lock_file(root, snapshot_id), "a+b")
        except OSError:
            continue
        try:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                tombstone = Path(
                    tempfile.mkdtemp(
                        prefix=f".removed-{snapshot_id}-", dir=_snapshots(root)
                    )
                )
            except OSError:
                continue
            try:
                os.replace(_snapshots(root) / snapshot_id, tombstone / "snapshot")
            except OSError:
                shutil.rmtree(tombstone, ignore_errors=True)
                continue
            with contextlib.suppress(OSError):
                os.unlink(_lock_file(root, snapshot_id))
            removed.append(snapshot_id)
            sizes[snapshot_id] = _tree_bytes(tombstone / "snapshot")
        finally:
            lock.close()
        shutil.rmtree(tombstone, ignore_errors=True)
        if tombstone.exists():
            leftover.append(str(tombstone))
    return removed, leftover, sizes


def _tree_bytes(path):
    """The bytes the regular files under ``path`` take, links not followed;
    None when a file cannot be read."""
    total = 0

    def failed(error):
        raise error

    try:
        for folder, _, files in os.walk(path, onerror=failed):
            for name in files:
                info = os.lstat(os.path.join(folder, name))
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size
    except OSError:
        return None
    return total


def _removed_note(removed, sizes, keep):
    """The warning naming the snapshots a refresh removed, their sizes and
    how to keep them."""

    def size(snapshot_id):
        taken = sizes.get(snapshot_id)
        return "size unknown" if taken is None else f"{taken / 1e6:,.0f} MB"

    named = ", ".join(f"{s} ({size(s)})" for s in removed)
    return (
        f"refresh removed {len(removed)} older snapshot"
        f"{'' if len(removed) == 1 else 's'} to keep the newest {keep}: {named}; "
        "refresh(keep=None) keeps every snapshot, and refresh(snapshot=...) "
        "installs one again"
    )


def _download(release, manifest, directory, transport, progress):
    """Stream the release's archive into ``directory`` and check it against
    the size and digest its manifest declares; return its path."""
    snapshot_id = manifest["snapshot_id"]
    name = contract.archive_name(snapshot_id)
    if manifest["schema_version"] >= 12:
        name = contract.core_archive_name(snapshot_id)
    assets = {a.get("name"): a for a in release.get("assets") or ()}
    url = (assets.get(name) or {}).get("browser_download_url")
    declared = manifest.get("archive") or {}
    expected = declared.get("sha256")
    size = declared.get("bytes")
    # The manifest bounds the download exactly: no size, no download.
    if (
        not isinstance(url, str)
        or not isinstance(expected, str)
        or type(size) is not int
        or not 0 < size <= contract.MAX_ASSET_BYTES
    ):
        raise DownloadError(
            f"release {release.get('tag_name')} does not declare a verifiable "
            "archive"
        )
    archive = directory / name
    report = None
    if progress:
        report = _progress.Download(f"Downloading feed index snapshot {snapshot_id}")
    # Not the API client: the asset URL is absolute and redirects to the
    # asset host.
    try:
        with _http.client(
            timeout=TIMEOUT, follow_redirects=True, transport=transport
        ) as client:
            digest = _http.download(client, url, archive, limit=size, progress=report)
    finally:
        if report is not None:
            report.close()
    if archive.stat().st_size != size or digest != expected:
        raise DownloadError(
            "the archive does not match the size and digest its manifest declares"
        )
    return archive


def _download_everything(root, snapshot_id, transport, progress):
    """Download every on-demand file the installed snapshot lacks, then read
    it whole (:func:`~transitio.index.check_index`)."""
    from transitio.index import check_index

    path = _snapshots(root) / snapshot_id
    snapshot = _read_snapshot(path)
    for member in contract.on_demand(snapshot):
        if not os.path.lexists(path / member):
            _download_part(path, snapshot, member, transport, progress)
    check_index(path)


def refresh(
    *,
    repository=contract.DEFAULT_REPOSITORY,
    api_url=contract.API_URL,
    cache_dir=None,
    transport=None,
    keep=KEEP,
    progress=True,
    everything=False,
    snapshot=None,
):
    """Install the newest published snapshot this reader supports, or with
    ``snapshot`` the published snapshot of that id.

    The archive is streamed to a private directory in the cache and
    unpacked from there. Before schema 12 it holds the whole snapshot:
    about 420 MB, which takes about 550 MB installed, so about 1 GB must be
    free while a refresh runs. From schema 12 it holds the core tables
    only; a partition's place boundaries and edge details are downloaded
    into the snapshot the first time a query needs them, each checked
    against the size and digest the snapshot lists, and kept for later and
    offline use. With ``everything=True`` every one of them the snapshot
    lacks is downloaded now, also when the snapshot was installed already,
    and the snapshot is then read whole with
    :func:`~transitio.index.check_index`. A progress bar for each download
    (a widget in Jupyter when ipywidgets is installed) and a line for the
    unpacking go to stderr; ``progress=False`` prints nothing.

    Returns a summary: the snapshot id, whether it was newly ``installed``
    (a damaged install is replaced), the releases ``skipped`` (newer but
    incompatible, or incomplete), the older snapshots ``removed`` to keep
    the newest ``keep`` (10 by default; ``keep=None`` removes none, and the
    snapshot installed and the pinned one are always kept) with the bytes
    each took (``removed_bytes``), and any ``leftover`` tombstone that could
    not be deleted. A ``UserWarning`` names every snapshot removed, with
    its size. Raises ``ValueError`` when ``snapshot`` is not a snapshot id,
    :class:`~transitio.exceptions.DownloadError` when the listing is
    unreachable, the named snapshot is not published, or a download fails
    verification, and
    :class:`~transitio.exceptions.IncompatibleIndexError` when no listed
    release is one this transitio reads or, with ``everything=True``, the
    snapshot fails a check. A failed install leaves the active snapshot in
    place; with ``everything=True`` the snapshot is installed before its
    on-demand files are downloaded, so it stays installed with those that
    arrived.
    """
    if snapshot is not None and not contract.is_snapshot_id(snapshot):
        raise ValueError(f"not a snapshot id: {snapshot!r}")
    root = cache_root(cache_dir)
    _state["transport"] = transport
    with _client(api_url, transport) as client:
        try:
            if snapshot is None:
                releases = contract.list_releases(client, repository)
            else:
                # Read by its tag, wherever the listing would page it.
                tag = contract.release_tag(snapshot)
                found = contract.release_by_tag(client, repository, tag)
                same = found is not None and found.get("tag_name") == tag
                releases = [found] if same else []
        except contract.ReleaseError as error:
            raise DownloadError(str(error)) from error
        if snapshot is not None and not releases:
            raise DownloadError(f"snapshot {snapshot} is not published")
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
    _snapshots(root).mkdir(parents=True, exist_ok=True)
    # The archive stays here until it is installed; the directory's name is
    # no snapshot id, so nothing reads it as an installed snapshot.
    with tempfile.TemporaryDirectory(
        dir=_snapshots(root), prefix=".download-", ignore_cleanup_errors=True
    ) as directory:
        archive = None
        if not _verified(root, snapshot_id):
            archive = _download(release, manifest, Path(directory), transport, progress)
        # Verified, installed and pruned as one step under the cache lock: the
        # download above is the only slow part, and nothing decided before it
        # is trusted once the lock is held.
        with _installing(root):
            already = _verified(root, snapshot_id)
            if not already:
                if archive is None:
                    raise DownloadError(
                        f"snapshot {snapshot_id} was removed while being verified; "
                        "run transitio.index.refresh() again"
                    )
                if progress:
                    _progress.say(f"Unpacking and checking snapshot {snapshot_id}")
                record = _release_record(repository, snapshot_id, manifest.get("parts"))
                _install(root, snapshot_id, archive, record)
            protect = {snapshot_id, _pinned(cache_dir)[1]}
            removed, leftover, sizes = _prune(root, keep, protect)
            # Held while the on-demand files arrive, so no other refresh
            # prunes the snapshot meanwhile.
            lease = _hold(root, snapshot_id) if everything else None
    if removed:
        warnings.warn(_removed_note(removed, sizes, keep), UserWarning, stacklevel=2)
    # The next query resolves afresh: a newer snapshot may now be active.
    _state["handle"] = None
    _state["handle_key"] = None
    if everything:
        try:
            _download_everything(root, snapshot_id, transport, progress)
        finally:
            if lease is not None:
                lease.close()
    return {
        "snapshot_id": snapshot_id,
        "installed": not already,
        "skipped": [{"tag": tag, "reason": reason} for tag, reason in skipped],
        "removed": removed,
        "removed_bytes": sizes,
        "leftover": leftover,
    }
