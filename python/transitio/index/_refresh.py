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

import hashlib
import io
import os
import shutil
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

# Process-local: the selection use() made, and the handle it resolves to.
_state = {"selection": None, "handle": None, "handle_key": None}


def cache_root(cache_dir=None):
    """Where snapshots live: ``<user cache>/transitio/index`` unless given."""
    if cache_dir is not None:
        return Path(cache_dir)
    return Path(platformdirs.user_cache_dir("transitio")) / "index"


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


def installed(*, cache_dir=None):
    """The installed snapshots as ``(snapshot_id, snapshot manifest)``,
    newest build first."""
    root = _snapshots(cache_root(cache_dir))
    found = []
    if root.is_dir():
        for entry in root.iterdir():
            if not contract.is_snapshot_id(entry.name) or entry.is_symlink():
                continue
            snapshot = _read_snapshot(entry)
            if snapshot is not None and snapshot.get("snapshot_id") == entry.name:
                found.append((entry.name, snapshot))
    found.sort(
        key=lambda item: (str(item[1].get("built_at") or ""), item[0]), reverse=True
    )
    return found


def use(snapshot=None, *, cache_dir=None):
    """Select an installed snapshot for this process; ``None`` clears the
    selection. Selects only, never downloads, and writes nothing to disk."""
    if snapshot is not None:
        if not contract.is_snapshot_id(snapshot):
            raise ValueError(f"not a snapshot id: {snapshot!r}")
        if _read_snapshot(_snapshots(cache_root(cache_dir)) / snapshot) is None:
            raise TransitioError(
                f"snapshot {snapshot} is not installed; run transitio.index.refresh()"
            )
    _state["selection"] = snapshot
    _state["handle"] = None
    _state["handle_key"] = None


def _pinned():
    """The snapshot pinned for this process: use() first, then the environment."""
    return _state["selection"] or os.environ.get(SNAPSHOT_ENV) or None


def active_index(*, cache_dir=None):
    """The :class:`~transitio.index.Index` queries read when none is passed,
    resolved lazily: the :func:`use` selection, then ``TRANSITIO_INDEX_SNAPSHOT``,
    then the newest compatible installed snapshot."""
    from transitio.index import read_index

    root = cache_root(cache_dir)
    pinned = _pinned()
    if pinned is not None:
        if not contract.is_snapshot_id(pinned):
            raise TransitioError(f"{SNAPSHOT_ENV}: not a snapshot id: {pinned!r}")
        if _read_snapshot(_snapshots(root) / pinned) is None:
            raise TransitioError(
                f"snapshot {pinned} is not installed; run transitio.index.refresh()"
            )
        chosen = pinned
    else:
        chosen = None
        for snapshot_id, snapshot in installed(cache_dir=cache_dir):
            if contract.compatible(snapshot)[0]:
                chosen = snapshot_id
                break
        if chosen is None:
            raise TransitioError(
                "no compatible feed index snapshot is installed; run "
                "transitio.index.refresh()"
            )
    key = (str(root), chosen)
    if _state["handle_key"] != key:
        _state["handle"] = read_index(_snapshots(root) / chosen)
        _state["handle_key"] = key
    return _state["handle"]


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
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
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


def _install(root, snapshot_id, data):
    """Unpack, validate and activate one snapshot; the previous one stays
    whole whatever fails."""
    from transitio.index import read_index

    snapshots = _snapshots(root)
    snapshots.mkdir(parents=True, exist_ok=True)
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
        target = snapshots / snapshot_id
        if not target.exists():
            os.replace(staging, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _prune(root, keep, protect):
    """Remove all but the ``keep`` newest installed snapshots, never one in
    ``protect``; returns the ids removed."""
    removed = []
    for position, (snapshot_id, _) in enumerate(installed(cache_dir=root)):
        if position >= keep and snapshot_id not in protect:
            shutil.rmtree(_snapshots(root) / snapshot_id, ignore_errors=True)
            removed.append(snapshot_id)
    return removed


def refresh(
    *,
    repository=contract.DEFAULT_REPOSITORY,
    api_url=contract.API_URL,
    cache_dir=None,
    transport=None,
    keep=KEEP,
):
    """Install the newest published snapshot this reader supports.

    Returns a summary: the snapshot id, whether it was newly ``installed``,
    the releases ``skipped`` (newer but incompatible, or incomplete) and the
    older snapshots ``removed`` to keep the cache at ``keep``. Raises
    :class:`~transitio.exceptions.DownloadError` when the listing is
    unreachable or the archive fails verification, and
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
        already = _read_snapshot(_snapshots(root) / snapshot_id) is not None
        if not already:
            assets = {a.get("name"): a for a in release.get("assets") or ()}
            asset = assets.get(contract.archive_name(snapshot_id))
            declared = manifest.get("archive") or {}
            expected = declared.get("sha256")
            size = declared.get("bytes")
            if asset is None or not isinstance(expected, str):
                raise DownloadError(
                    f"release {release.get('tag_name')} names no verifiable archive"
                )
            limit = (
                size
                if isinstance(size, int) and 0 < size <= contract.MAX_ASSET_BYTES
                else contract.MAX_ASSET_BYTES
            )
            try:
                data = contract.download(
                    client, asset["browser_download_url"], "archive", limit
                )
            except (contract.ReleaseError, KeyError) as error:
                raise DownloadError(str(error)) from error
            if hashlib.sha256(data).hexdigest() != expected:
                raise DownloadError(
                    "the archive does not match the digest its manifest declares"
                )
            _install(root, snapshot_id, data)
    protect = {snapshot_id, _pinned()}
    removed = _prune(root, keep, protect)
    # The next query resolves afresh: a newer snapshot may now be active.
    _state["handle"] = None
    _state["handle_key"] = None
    return {
        "snapshot_id": snapshot_id,
        "installed": not already,
        "skipped": [{"tag": tag, "reason": reason} for tag, reason in skipped],
        "removed": removed,
    }
