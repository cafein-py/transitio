"""The release contract a published index snapshot follows.

Each snapshot is its own GitHub release in the index repository
(``transitio-dev/transitio-index``, the home of the build, never the code
repository), tagged ``index-<snapshot_id>``, holding the archive
``transitio-index-<snapshot_id>.tar.gz``, its ``.sha256`` and an immutable
``manifest.json``. The publisher (``scripts/publish_index.py``) writes them;
the refresh side lists releases and takes the newest one whose manifest
declares a schema this reader supports. Both sides share the names, the
selection rule and the HTTP helpers here, so they cannot drift apart.
"""

import json
import re

import httpx

__all__ = [
    "API_URL",
    "DEFAULT_REPOSITORY",
    "MANIFEST_NAME",
    "CHECKSUM_SUFFIX",
    "TAG_PREFIX",
    "MEMBERS",
    "archive_name",
    "release_tag",
    "is_snapshot_id",
    "compatible",
    "candidates",
    "newest_compatible",
    "list_releases",
    "download",
    "read_manifest",
    "MAX_MANIFEST_BYTES",
    "MAX_ASSET_BYTES",
]

API_URL = "https://api.github.com"
DEFAULT_REPOSITORY = "transitio-dev/transitio-index"
# GitHub caps release assets at 2 GiB; an index is a few MB, a manifest a
# few hundred bytes.
MAX_ASSET_BYTES = 2 * 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
TAG_PREFIX = "index-"
MANIFEST_NAME = "manifest.json"
CHECKSUM_SUFFIX = ".sha256"

# What a released archive holds, every one required: a snapshot without
# places or edges is a local build, not something a client should adopt.
MEMBERS = (
    "snapshot.json",
    "feeds.parquet",
    "places.parquet",
    "edges.parquet",
    "NOTICE",
)

_SNAPSHOT_ID = re.compile(r"[0-9a-f]{16}")


def is_snapshot_id(value):
    """A snapshot id as the publish stage mints it: sixteen hex digits."""
    return isinstance(value, str) and _SNAPSHOT_ID.fullmatch(value) is not None


def archive_name(snapshot_id):
    return f"transitio-index-{snapshot_id}.tar.gz"


def release_tag(snapshot_id):
    return TAG_PREFIX + snapshot_id


def compatible(manifest):
    """Whether this reader can read the snapshot a release manifest describes:
    ``(True, None)`` or ``(False, reason)``."""
    from transitio import __version__
    from transitio.index import SUPPORTED_SCHEMA_VERSIONS, _version_key

    if not isinstance(manifest, dict):
        return False, "manifest is not an object"
    if not is_snapshot_id(manifest.get("snapshot_id")):
        return False, "manifest names no snapshot id"
    version = manifest.get("schema_version")
    if type(version) is not int or version not in SUPPORTED_SCHEMA_VERSIONS:
        return False, f"schema_version {version!r} is not one this transitio reads"
    minimum = manifest.get("min_reader_version")
    floor = _version_key(minimum) if isinstance(minimum, str) else None
    if floor is None:
        return False, "manifest names no min_reader_version"
    current = _version_key(__version__)
    if current is None or current < floor:
        return False, f"needs transitio >= {minimum}"
    return True, None


def candidates(releases):
    """The index releases a client may take, newest first: published (never
    a draft or a pre-release), tagged for a snapshot, each as ``(release,
    assets by name)``."""
    found = []
    for release in releases:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        if release.get("prerelease"):
            continue
        tag = release.get("tag_name")
        if not isinstance(tag, str) or not tag.startswith(TAG_PREFIX):
            continue
        if not is_snapshot_id(tag[len(TAG_PREFIX) :]):
            continue
        assets = {}
        for asset in release.get("assets") or ():
            if isinstance(asset, dict) and isinstance(asset.get("name"), str):
                assets[asset["name"]] = asset
        found.append((release, assets))

    # Newest by publication time (created_at is the tagged commit's time,
    # which can tie or run backwards), the release id breaking ties.
    def key(item):
        release = item[0]
        identifier = release.get("id")
        return (
            str(release.get("published_at") or ""),
            identifier if isinstance(identifier, int) else -1,
        )

    found.sort(key=key, reverse=True)
    return found


def newest_compatible(releases, read_manifest):
    """The newest release whose manifest this reader supports, as
    ``(release, manifest, skipped)``; ``read_manifest(asset)`` returns the
    parsed manifest or None when the asset cannot be read. A release without
    a readable manifest is incomplete and skipped, as is an incompatible
    one; ``skipped`` lists ``(tag, reason)`` so a caller can say what it
    passed over. ``(None, None, skipped)`` when nothing qualifies."""
    skipped = []
    for release, assets in candidates(releases):
        tag = release["tag_name"]
        asset = assets.get(MANIFEST_NAME)
        manifest = None if asset is None else read_manifest(asset)
        if manifest is None:
            skipped.append((tag, "no readable manifest"))
            continue
        ok, reason = compatible(manifest)
        if not ok:
            skipped.append((tag, reason))
            continue
        if release_tag(manifest["snapshot_id"]) != tag:
            skipped.append((tag, "manifest names another snapshot"))
            continue
        return release, manifest, skipped
    return None, None, skipped


class ReleaseError(RuntimeError):
    """A release could not be listed or an asset read."""


def _check(response, what):
    if response.status_code // 100 != 2:
        raise ReleaseError(f"{what}: HTTP {response.status_code}")
    return response


def list_releases(client, repository):
    """The repository's releases as the API lists them, following the
    redirect GitHub keeps for a moved repository (the client drops any
    token across hosts)."""
    try:
        response = client.get(
            f"/repos/{repository}/releases",
            params={"per_page": 100},
            follow_redirects=True,
        )
        return _check(response, "listing the releases").json()
    except (httpx.HTTPError, ValueError) as error:
        raise ReleaseError(f"listing the releases: {error}") from error


def download(client, url, what, limit=MAX_ASSET_BYTES):
    """An asset's bytes, streamed up to ``limit`` and refused beyond it,
    following GitHub's redirect to its object store."""
    chunks = []
    size = 0
    try:
        with client.stream(
            "GET",
            url,
            headers={"Accept": "application/octet-stream"},
            follow_redirects=True,
        ) as response:
            _check(response, what)
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > limit:
                    raise ReleaseError(f"{what}: larger than {limit} bytes")
                chunks.append(chunk)
    except httpx.HTTPError as error:
        raise ReleaseError(f"{what}: {error}") from error
    return b"".join(chunks)


def read_manifest(client, asset):
    """A release manifest read as a client would, or None when unreadable."""
    try:
        data = download(
            client, asset["browser_download_url"], "manifest", MAX_MANIFEST_BYTES
        )
        return json.loads(data.decode("utf-8"))
    except (ReleaseError, KeyError, ValueError):
        return None
