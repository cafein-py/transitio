"""Fallback download client for Transitland Atlas feeds."""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
from pathlib import Path

import httpx
import platformdirs

from transitio.catalog._client import _stream_download, _write_provenance
from transitio.exceptions import DownloadError

# The Atlas ``urls`` key that names a feed's current static GTFS download.
STATIC_URL = "static_current"


def _feed_dir(feed_id):
    """The digest-keyed cache directory for a feed. Paths never key on the id
    itself: Onestop ids are Unicode, can exceed a filesystem's byte limit and
    can collide as filenames under normalisation. The whole digest is kept --
    ids are upstream-controlled, and a truncated hash would make chosen
    collisions feasible; the real id is recorded in the provenance sidecar."""
    return "id-" + hashlib.sha256(feed_id.encode("utf-8")).hexdigest()


@dataclasses.dataclass(frozen=True)
class AtlasFeed:
    """A Transitland Atlas feed, enough of it to download and attribute.

    Attributes mirror the fields the download path relies on; the complete
    Atlas record is kept in ``raw``.
    """

    feed_id: str
    onestop_id: str | None
    name: str | None
    spec: str | None
    static_url: str | None
    requires_auth: bool
    raw: dict = dataclasses.field(repr=False, default_factory=dict)

    @classmethod
    def from_record(cls, record, feed_id=None):
        """Build a feed from an Atlas record block, as the index carries it.

        ``feed_id`` names the feed for the cache path and provenance; it
        defaults to the record's own onestop id when not given separately.
        """
        record = record or {}
        urls = record.get("urls") or {}
        onestop_id = record.get("onestop_id")
        identifier = feed_id or onestop_id
        if not identifier:
            raise DownloadError("atlas feed record has no feed id or onestop id")
        return cls(
            feed_id=identifier,
            onestop_id=onestop_id,
            name=record.get("name"),
            spec=record.get("spec"),
            static_url=urls.get(STATIC_URL),
            requires_auth=bool(
                record.get("requires_auth") or record.get("authorization")
            ),
            raw=record,
        )


class TransitlandAtlas:
    """Synchronous fallback download client for Transitland Atlas feeds.

    Atlas is a fallback download source: the Mobility Database is preferred
    where a feed exists in both. No token is needed -- a feed's download URL
    comes from its Atlas record, which the built index carries -- so this
    mirrors :class:`~transitio.catalog.MobilityDatabase` without its
    authentication.

    Parameters
    ----------
    cache_dir : str or pathlib.Path, optional
        Directory for downloaded feeds. Defaults to the platform user cache
        directory for transitio.
    timeout : float, default 30.0
        Per-request timeout in seconds.
    transport : httpx.BaseTransport, optional
        Custom transport, mainly for testing.
    """

    def __init__(self, *, cache_dir=None, timeout=30.0, transport=None):
        self._cache_dir = (
            Path(cache_dir)
            if cache_dir
            else Path(platformdirs.user_cache_dir("transitio"))
        )
        self._http = httpx.Client(
            timeout=timeout, transport=transport, follow_redirects=True
        )

    def close(self):
        """Close the underlying HTTP session."""
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def download(self, feed, directory=None):
        """Download an Atlas feed's static GTFS zip with a provenance sidecar.

        The Atlas static URL is a moving target with no upstream checksum, so
        the file is re-downloaded on every call and a
        ``<feed id>.provenance.json`` sidecar records the source URL, the
        computed SHA-256 and the retrieval time.

        Parameters
        ----------
        feed : AtlasFeed
        directory : str or pathlib.Path, optional
            Base directory; the feed is placed in its own digest-named
            subdirectory of it. Defaults to the transitio cache.

        Returns
        -------
        pathlib.Path
            Path of the downloaded zip.
        """
        if not feed.static_url:
            raise DownloadError(f"atlas feed {feed.feed_id} has no static download url")
        base = Path(directory) if directory else self._cache_dir / "gtfs"
        # Namespaced by the feed even under a caller's directory, so several
        # feeds downloaded into one directory never share ``latest.zip``.
        path = base / _feed_dir(feed.feed_id) / "latest.zip"
        digest = _stream_download(self._http, feed.static_url, path)
        provenance = {
            "feed_id": feed.feed_id,
            "onestop_id": feed.onestop_id,
            "source": "atlas",
            "source_url": feed.static_url,
            "sha256": digest,
            "retrieved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        _write_provenance(path.with_suffix(".provenance.json"), provenance)
        return path
