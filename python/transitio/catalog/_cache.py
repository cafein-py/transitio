"""The feed download cache: every downloaded version of a feed, stored once by
content.

A feed's versions live in ``<cache>/gtfs/<feed dir>/`` as ``<sha256>.zip``,
each beside a ``<sha256>.provenance.json`` sidecar. The sidecar keeps the
provenance fields a downloaded feed has always carried (``feed_id``,
``source_url``, ``sha256`` and the caller's own fields, from the version's
first acquisition), ``retrieved_at`` (the latest acquisition, which orders
versions), ``last_used_at`` (when a call last delivered the version) and the
cache's records under ``cache``:

- ``sources``: every acquisition of these bytes, oldest first, append-only;
- ``datasets``: the Mobility Database datasets the bytes represent, by id;
- ``served``: the requests of ``fetch`` the version served, by key, each with
  the dataset it was read as;
- ``index_proofs``: the index snapshots under which a probe proved the bytes
  to be the archive the index crawled, each with the URL probed;
- ``outputs``: what ``fetch`` made of the version, by key, each the name and
  SHA-256 of its file in the feed's ``outputs`` folder, beside the results
  stored as ``<key>.json``.

A delivered copy's sidecar describes the first acquisition. A version is
published only after its download completed and the file is a readable zip;
a version whose bytes no longer match its digest is linked again to an intact
blob, or else deleted, before use. On POSIX systems published versions are
read-only, and the bytes of a version
are stored once, in ``<cache>/gtfs/blobs/<sha256>.zip``, each feed's version
a hard link to that blob, so identical archives of two feeds take the space
once; where a link cannot be made, and on Windows, a version is a copy of its
own. Blobs are created, linked and removed under the lock
``<cache>/gtfs/.locks/blobs.lock``, always taken after a feed's lock. The
cache's own directories are never symlinks. Work on one feed runs under a
lock file in
``<cache>/gtfs/.locks/``, outside the feed's folder, and lock files are never
deleted.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import hashlib
import json
import os
import re
import shutil
import stat
import uuid
import zipfile
from pathlib import Path

from transitio import _http
from transitio.exceptions import DownloadError

_DIGEST = re.compile(r"[0-9a-f]{64}")
_SIDECAR = ".provenance.json"
# The steps whose output ``fetch`` stores, as ``<key>-<step>.zip``.
_OUTPUT_STEPS = ("cropped", "repaired")


def _feed_dir(feed_id):
    """The digest-keyed cache directory for a feed. Paths never key on the id
    itself: Onestop ids are Unicode, can exceed a filesystem's byte limit and
    can collide as filenames under normalisation. The whole digest is kept --
    ids are upstream-controlled, and a truncated hash would make chosen
    collisions feasible; the real id is recorded in the provenance sidecar."""
    return "id-" + hashlib.sha256(feed_id.encode("utf-8")).hexdigest()


def _write_provenance(path, data):
    """Write a provenance sidecar atomically and without following a symlink
    at the target: a partial write cannot leave truncated JSON beside the
    artefact it describes, and a symlink cannot redirect the write elsewhere.
    Portable -- the fresh unique temp name needs no ``O_NOFOLLOW``, and
    ``os.replace`` swaps it in without following a symlink at the target."""
    body = json.dumps(data, indent=2).encode("utf-8")
    with _http.replacing(path) as handle:
        handle.write(body)


def _regular(path):
    """Whether ``path`` is a regular file, not a symlink or a special file."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _well_formed(sidecar, feed_id, digest):
    """Whether a parsed sidecar describes ``digest`` of ``feed_id`` and holds
    the cache's records in the shape this module writes."""
    if not isinstance(sidecar, dict):
        return False
    cache = sidecar.get("cache")
    return (
        sidecar.get("feed_id") == feed_id
        and sidecar.get("sha256") == digest
        and isinstance(sidecar.get("retrieved_at"), str)
        and isinstance(cache, dict)
        and isinstance(cache.get("sources"), list)
        and len(cache["sources"]) > 0
        and all(
            isinstance(s, dict)
            and isinstance(s.get("source_url"), str)
            and isinstance(s.get("retrieved_at"), str)
            for s in cache["sources"]
        )
        and isinstance(cache.get("datasets"), dict)
        and all(
            isinstance(e, dict) and "service_date_range" in e
            for e in cache["datasets"].values()
        )
        and isinstance(cache.get("served", {}), dict)
        and isinstance(cache.get("index_proofs", {}), dict)
        and isinstance(cache.get("outputs", {}), dict)
        and all(_output_record(k, r) for k, r in cache.get("outputs", {}).items())
    )


def _output_record(key, record):
    """Whether ``record`` describes an output under ``key`` as ``fetch``
    stores one: no file (the version itself) or the step's file named by the
    key, with their SHA-256 digests."""
    if not (_DIGEST.fullmatch(key) and isinstance(record, dict)):
        return False
    file, digest = record.get("file"), record.get("sha256")
    if file is None:
        named = digest is None
    else:
        named = file in [f"{key}-{step}.zip" for step in _OUTPUT_STEPS]
        named = named and isinstance(digest, str) and bool(_DIGEST.fullmatch(digest))
    results = record.get("results_sha256")
    return named and isinstance(results, str) and bool(_DIGEST.fullmatch(results))


def _directory(path):
    """Create the directory ``path``; a symlink there is refused, so nothing
    is written or deleted through one."""
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise DownloadError(f"{path} is a symlink")


def _copy(path, target, provenance):
    """A writable copy of ``path`` at ``target`` beside a sidecar of
    ``provenance``; returns ``target``."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "rb") as source, _http.replacing(target) as handle:
        shutil.copyfileobj(source, handle)
    try:
        _write_provenance(target.with_suffix(_SIDECAR), provenance)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(target)
        raise
    return target


def _unshared(path):
    """The bytes of the files at or under ``path`` no hard link outside it
    shares, each counted once: what removing ``path`` frees."""
    if path.is_symlink() or not path.is_dir():
        files = [path]
    else:
        files = [Path(r) / n for r, _, names in os.walk(path) for n in names]
    inside = {}
    for file in files:
        with contextlib.suppress(OSError):
            info = os.lstat(file)
            key = (info.st_dev, info.st_ino)
            links, _, size = inside.get(key, (0, info.st_nlink, info.st_size))
            inside[key] = (links + 1, info.st_nlink, size)
    return sum(size for links, nlink, size in inside.values() if links >= nlink)


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _unlink(path):
    """Remove the file or symlink at ``path``; a directory squatting on a
    version's name is removed with its contents, without following links."""
    with contextlib.suppress(FileNotFoundError):
        if stat.S_ISDIR(os.lstat(path).st_mode):
            shutil.rmtree(path)
        else:
            os.unlink(path)


@dataclasses.dataclass
class Version:
    """One cached version of a feed: its archive and its sidecar's contents."""

    path: Path
    sidecar: dict

    @property
    def sha256(self):
        return self.path.stem

    @property
    def datasets(self):
        return self.sidecar.get("cache", {}).get("datasets", {})

    @property
    def retrieved_at(self):
        return self.sidecar.get("retrieved_at", "")

    @property
    def served(self):
        return self.sidecar.get("cache", {}).get("served", {})

    @property
    def index_proofs(self):
        return self.sidecar.get("cache", {}).get("index_proofs", {})

    @property
    def first_source(self):
        """The record of the first acquisition of these bytes."""
        return self.sidecar["cache"]["sources"][0]

    def acquired_as(self, fetched_from):
        """Whether any acquisition of these bytes came ``fetched_from``."""
        sources = self.sidecar.get("cache", {}).get("sources", [])
        return any(s.get("fetched_from") == fetched_from for s in sources)

    def provenance(self, dataset_id=None):
        """The flat provenance fields of the first acquisition, without the
        cache's records; dataset fields only for ``dataset_id``, one of the
        datasets these bytes represent."""
        dropped = ("cache", "last_used_at", "dataset_id", "service_date_range")
        flat = {k: v for k, v in self.sidecar.items() if k not in dropped}
        first = self.first_source
        flat.update(source_url=first["source_url"], retrieved_at=first["retrieved_at"])
        entry = self.datasets.get(dataset_id) if dataset_id else None
        if entry is not None:
            flat.update(
                dataset_id=dataset_id, service_date_range=entry["service_date_range"]
            )
        return flat


class FeedCache:
    """The versions of every feed cached under ``<cache_dir>/gtfs``."""

    def __init__(self, cache_dir):
        self.root = Path(cache_dir) / "gtfs"

    def folder(self, feed_id):
        return self.root / _feed_dir(feed_id)

    def lock(self, feed_id):
        """Hold the feed's lock for the block; another process waits."""
        return self.locked(_feed_dir(feed_id))

    @contextlib.contextmanager
    def locked(self, folder):
        """Hold the lock of the feed whose folder is named ``folder``."""
        with _http.locked(self.root / ".locks" / f"{folder}.lock"):
            yield

    def versions(self, feed_id):
        """The feed's versions -- a regular file beside a regular, well-formed
        sidecar naming this feed and the file's digest -- newest acquisition
        first (ties by digest); any other version is deleted."""
        folder = self.folder(feed_id)
        if self.root.is_symlink() or folder.is_symlink() or not folder.is_dir():
            return []
        strays = list(folder.glob(".*.link"))
        if strays:
            # Links an interrupted publication left behind, removed as a
            # feed's link is, with the blobs they held.
            with self._blob_lock():
                for stray in strays:
                    _unlink(stray)
                self._swept()
        found = []
        for path in folder.glob("*.zip"):
            if not _DIGEST.fullmatch(path.stem):
                continue
            sidecar = None
            if _regular(path) and _regular(path.with_suffix(_SIDECAR)):
                try:
                    sidecar = json.loads(path.with_suffix(_SIDECAR).read_text())
                except (OSError, ValueError):
                    pass
            if not _well_formed(sidecar, feed_id, path.stem):
                self.delete(Version(path, {}))
                continue
            found.append(Version(path, sidecar))
        found.sort(key=lambda v: (v.retrieved_at, v.sha256), reverse=True)
        self._sweep(folder, found)
        return found

    def _sweep(self, folder, versions):
        """Remove the outputs no version in ``versions`` lists, such as those
        of a version deleted for an unreadable sidecar."""
        outputs = folder / "outputs"
        if outputs.is_symlink() or not outputs.is_dir():
            return
        listed = set()
        for version in versions:
            for key, record in version.sidecar["cache"].get("outputs", {}).items():
                listed.update((f"{key}.json", record.get("file")))
        for path in outputs.iterdir():
            if path.name not in listed:
                _unlink(path)

    def newest(self, feed_id, accept=None):
        """The newest version whose bytes still match their digest and that
        ``accept(version)`` takes, or None. A version whose bytes no longer
        match is deleted; only versions tried are hashed."""
        for version in self.versions(feed_id):
            if (accept is None or accept(version)) and self.intact(version):
                return version
        return None

    def intact(self, version):
        """Whether ``version``'s bytes still match their digest. A version
        whose bytes do not is linked again to its blob when another feed has
        already replaced the blob with intact bytes, and is deleted
        otherwise."""
        if _http.sha256_file(version.path) == version.sha256:
            return True
        if self._relink(version):
            # The damaged file may have lost its last other link.
            self.sweep()
            return True
        self.delete(version)
        return False

    def _blob_lock(self):
        return _http.locked(self.root / ".locks" / "blobs.lock")

    @contextlib.contextmanager
    def _blobs(self):
        """The blob folder, held under the blob lock for the block."""
        with self._blob_lock():
            folder = self.root / "blobs"
            _directory(folder)
            yield folder

    def _store(self, staged, target, digest):
        """Move the staged archive to ``target``: a hard link to the blob of
        its bytes, the blob made from it when there is none or the blob no
        longer matches its name; a copy of its own where a link cannot be
        made."""
        if os.name == "nt":
            os.replace(staged, target)
            return
        with self._blobs() as folder:
            blob = folder / f"{digest}.zip"
            made = False
            if not (_regular(blob) and _http.sha256_file(blob) == digest):
                self._quarantine(blob)
                os.replace(staged, blob)
                os.chmod(blob, 0o444)
                made = True
            link = target.with_name(f".{uuid.uuid4().hex}.link")
            try:
                os.link(blob, link)
            except OSError:
                os.replace(blob if made else staged, target)
            else:
                try:
                    os.replace(link, target)
                finally:
                    _unlink(link)
            # A damaged blob put aside may have had its last link.
            self._swept()

    def _relink(self, version):
        """Link a damaged ``version`` to its blob again when the blob is
        another, intact file, or copy the blob where a link cannot be made; a
        blob that is the damaged file itself is put aside, to be removed once
        nothing links to it. Returns whether the version is intact again."""
        if os.name == "nt":
            return False
        with self._blobs() as folder:
            blob = folder / f"{version.sha256}.zip"
            try:
                shared = os.path.samefile(blob, version.path)
            except OSError:
                return False
            if shared:
                self._quarantine(blob)
                return False
            if not (_regular(blob) and _http.sha256_file(blob) == version.sha256):
                return False
            link = version.path.with_name(f".{uuid.uuid4().hex}.link")
            try:
                os.link(blob, link)
                os.replace(link, version.path)
            except OSError:
                # Where a link cannot be made, the version is a copy of its own.
                _unlink(link)
                with open(blob, "rb") as source, _http.replacing(version.path) as out:
                    shutil.copyfileobj(source, out)
                os.chmod(version.path, 0o444)
                return _http.sha256_file(version.path) == version.sha256
            return True

    def _quarantine(self, blob):
        """Rename a damaged blob out of the way of a new one."""
        if blob.exists() or blob.is_symlink():
            os.replace(blob, blob.with_name(f"{blob.stem}.{uuid.uuid4().hex}.damaged"))

    def sweep(self, prune=False):
        """Remove the blobs no feed's version links to any more, and with
        ``prune`` the blob folder when that leaves it empty; returns the
        bytes freed."""
        with self._blob_lock():
            freed = self._swept()
            if prune:
                with contextlib.suppress(OSError):
                    (self.root / "blobs").rmdir()
            return freed

    def _swept(self):
        """:meth:`sweep`, the blob lock held."""
        freed, folder = 0, self.root / "blobs"
        if folder.is_symlink() or not folder.is_dir():
            return freed
        for blob in folder.iterdir():
            with contextlib.suppress(OSError):
                info = os.lstat(blob)
                if not stat.S_ISREG(info.st_mode):
                    # No blob, whatever squats on its name.
                    size = _unshared(blob)
                    _unlink(blob)
                    freed += size
                elif info.st_nlink <= 1:
                    _unlink(blob)
                    freed += info.st_size
        return freed

    def remove(self, path):
        """Remove the file or folder ``path`` of the cache under the blob
        lock, with the blobs it held the last links to; returns the bytes
        freed."""
        with self._blob_lock():
            before = _unshared(path)
            _unlink(path)
            return before - _unshared(path) + self._swept()

    def download(self, client, url, feed_id, record, fetched_from, **options):
        """Download ``url`` with ``client`` into staging and publish it
        (:meth:`publish`); ``expected`` names a SHA-256 the bytes must have.
        Raises :class:`DownloadError` when the download fails, the bytes
        differ from ``expected`` or are not a zip; nothing is published or
        deleted then."""
        expected = options.pop("expected", None)
        with self.staging(feed_id) as staging:
            staged = staging / "download.zip"
            digest = _http.download(client, url, staged)
            if expected and digest != expected:
                raise DownloadError(
                    f"checksum mismatch for {url}: expected {expected}, got {digest}"
                )
            source = {
                "source_url": url,
                "fetched_from": fetched_from,
                "with_credentials": False,
                "download_errors": None,
                "index_snapshot": None,
                "retrieved_at": _now(),
            }
            return self.publish(feed_id, staged, digest, record, source, **options)

    @contextlib.contextmanager
    def staging(self, feed_id):
        """A fresh folder for the feed's downloads, removed after the block
        with whatever was not published from it."""
        staging = self.folder(feed_id) / ".staging"
        for directory in (self.root, staging.parent, staging):
            _directory(directory)
        folder = staging / uuid.uuid4().hex
        folder.mkdir()
        try:
            yield folder
        finally:
            shutil.rmtree(folder, ignore_errors=True)
            # Under the feed's lock no other download shares these folders.
            for empty in (staging, staging.parent):
                with contextlib.suppress(OSError):
                    empty.rmdir()

    def publish(
        self,
        feed_id,
        staged,
        digest,
        record,
        source,
        dataset=None,
        replace=False,
        used=True,
    ):
        """Add the staged file as a version of ``feed_id``: ``record`` gives
        the flat provenance fields of a first acquisition, ``source`` the
        acquisition record and ``dataset`` an optional ``{id: entry}`` of the
        MDB dataset the bytes represent. Identical bytes already cached keep
        their version and gain the record and the dataset. With ``replace``
        the feed's other versions are deleted afterwards; with ``used`` the
        version counts as delivered now. Raises :class:`DownloadError` when
        the staged file is not a zip."""
        if not zipfile.is_zipfile(staged):
            raise DownloadError(f"{source['source_url']}: not a zip archive")
        target = self.folder(feed_id) / f"{digest}.zip"
        current = [v for v in self.versions(feed_id) if v.sha256 == digest]
        if current and _http.sha256_file(target) == digest:
            sidecar = current[0].sidecar
        else:
            if current:
                self.delete(current[0])
            self._store(staged, target, digest)
            if os.name != "nt":
                os.chmod(target, 0o444)
            sidecar = {
                **record,
                "feed_id": feed_id,
                "source_url": source["source_url"],
                "sha256": digest,
                "cache": {"sources": [], "datasets": {}},
            }
            if dataset:
                ((dataset_id, entry),) = dataset.items()
                sidecar.update(
                    dataset_id=dataset_id,
                    service_date_range=entry["service_date_range"],
                )
        cache = sidecar["cache"]
        cache["sources"].append(source)
        for dataset_id, entry in (dataset or {}).items():
            # A dataset's first recorded context stays its context.
            cache["datasets"].setdefault(dataset_id, entry)
        sidecar["retrieved_at"] = source["retrieved_at"]
        if used:
            sidecar["last_used_at"] = source["retrieved_at"]
        _write_provenance(target.with_suffix(_SIDECAR), sidecar)
        version = Version(target, sidecar)
        if replace:
            for other in self.versions(feed_id):
                if other.sha256 != digest:
                    self.delete(other)
        return version

    def update(self, version, change):
        """Apply ``change`` to ``version``'s sidecar and write it. The
        sidecar is read again first, so a record added since is kept and a
        version deleted since stays deleted."""
        sidecar = version.path.with_suffix(_SIDECAR)
        try:
            current = json.loads(sidecar.read_text())
        except (OSError, ValueError):
            return
        change(current)
        _write_provenance(sidecar, current)
        version.sidecar = current

    def touch(self, version, served=None):
        """Record that a call delivered ``version``, and with ``served`` the
        ``(request key, dataset id)`` it served; a key keeps its first
        dataset."""

        def change(sidecar):
            sidecar["last_used_at"] = _now()
            if served is not None:
                key, dataset_id = served
                sidecar["cache"].setdefault("served", {}).setdefault(key, dataset_id)

        self.update(version, change)

    def delete(self, version):
        """Remove a version's archive, sidecar and the outputs made of it;
        returns the bytes the blobs it held the last links to freed."""
        outputs = version.path.parent / "outputs"
        records = version.sidecar.get("cache", {}).get("outputs", {})
        for key, record in records.items() if not outputs.is_symlink() else ():
            # Only names the cache gives its outputs, whatever the sidecar says.
            if not _DIGEST.fullmatch(key):
                continue
            if record.get("file") in [f"{key}-{step}.zip" for step in _OUTPUT_STEPS]:
                _unlink(outputs / record["file"])
            _unlink(outputs / f"{key}.json")
        _unlink(version.path.with_suffix(_SIDECAR))
        with self._blob_lock():
            _unlink(version.path)
            return self._swept()

    def deliver(self, version, target, provenance=None):
        """A writable copy of ``version`` at ``target`` beside a sidecar of
        ``provenance`` (default: :meth:`Version.provenance`); returns
        ``target``."""
        if provenance is None:
            provenance = version.provenance()
        return _copy(version.path, target, provenance)
