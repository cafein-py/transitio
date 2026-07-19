"""Synchronous client for the Mobility Database REST API."""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import time
import warnings
from pathlib import Path

import httpx
import platformdirs

from transitio.catalog._csv import fetch_catalog_csv, search_csv
from transitio.catalog._models import Dataset, Feed, as_date
from transitio.exceptions import DownloadError, MissingTokenError

API_URL = "https://api.mobilitydatabase.org/v1"
TOKEN_ENV_VAR = "MOBILITY_API_REFRESH_TOKEN"

_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_PAGE_SIZE = 100
_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _safe_id(value):
    """Validate a catalog ID before it is used as a path component."""
    if not value or not _SAFE_ID.match(value):
        raise DownloadError(f"catalog id {value!r} is not safe for filesystem use")
    return value


def _bounds(aoi):
    """Return (minx, miny, maxx, maxy) from a geometry-like AOI."""
    if hasattr(aoi, "total_bounds"):  # GeoDataFrame / GeoSeries
        return tuple(float(v) for v in aoi.total_bounds)
    bounds = getattr(aoi, "bounds", None)
    if bounds is not None and not callable(bounds):  # shapely geometry
        return tuple(float(v) for v in bounds)
    try:
        values = tuple(float(v) for v in aoi)
    except (TypeError, ValueError):
        values = ()
    if len(values) != 4:
        raise ValueError(
            "aoi must be a geometry, GeoDataFrame/GeoSeries or a "
            "(minx, miny, maxx, maxy) tuple"
        )
    return values


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stream_download(client, url, path):
    """Stream a URL to ``path`` via a partial file; return the SHA-256 hex."""
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    partial = path.parent / (path.name + ".part")
    with client.stream("GET", url) as response:
        response.raise_for_status()
        with open(partial, "wb") as handle:
            for chunk in response.iter_bytes():
                digest.update(chunk)
                handle.write(chunk)
    partial.replace(path)
    return digest.hexdigest()


class MobilityDatabase:
    """Synchronous client for the Mobility Database catalog API.

    Parameters
    ----------
    refresh_token : str, optional
        Mobility Database refresh token. Falls back to the
        ``MOBILITY_API_REFRESH_TOKEN`` environment variable.
    cache_dir : str or pathlib.Path, optional
        Directory for downloaded datasets. Defaults to the platform user
        cache directory for transitio.
    timeout : float, default 30.0
        Per-request timeout in seconds.
    transport : httpx.BaseTransport, optional
        Custom transport, mainly for testing.
    """

    def __init__(
        self, refresh_token=None, *, cache_dir=None, timeout=30.0, transport=None
    ):
        self._refresh_token = refresh_token or os.environ.get(TOKEN_ENV_VAR)
        self._cache_dir = (
            Path(cache_dir)
            if cache_dir
            else Path(platformdirs.user_cache_dir("transitio"))
        )
        self._http = httpx.Client(
            timeout=timeout, transport=transport, follow_redirects=True
        )
        self._access_token = None
        self._token_expiry = 0.0
        self._retry_wait = 1.0

    def close(self):
        """Close the underlying HTTP session."""
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def _token(self):
        if self._access_token and time.monotonic() < self._token_expiry:
            return self._access_token
        if not self._refresh_token:
            raise MissingTokenError(
                "a Mobility Database refresh token is required; pass "
                f"refresh_token or set {TOKEN_ENV_VAR}"
            )
        response = self._http.post(
            f"{API_URL}/tokens", json={"refresh_token": self._refresh_token}
        )
        response.raise_for_status()
        payload = response.json()
        self._access_token = payload["access_token"]
        self._token_expiry = time.monotonic() + payload.get("expires_in", 3600) - 60
        return self._access_token

    def _get_json(self, path, params=None):
        url = f"{API_URL}{path}"
        refreshed = False
        attempt = 0
        while True:
            headers = {"Authorization": f"Bearer {self._token()}"}
            response = self._http.get(url, params=params, headers=headers)
            if response.status_code == 401 and not refreshed:
                self._access_token = None
                refreshed = True
                continue
            if response.status_code in _RETRY_STATUSES and attempt < 3:
                time.sleep(self._retry_wait * 2**attempt)
                attempt += 1
                continue
            response.raise_for_status()
            return response.json()

    def _paginated(self, path, params, limit, predicate=None):
        """Collect up to ``limit`` records matching ``predicate``, paginating
        past non-matching records until the API is exhausted. ``limit=None``
        fetches every record."""
        results = []
        offset = 0
        while limit is None or len(results) < limit:
            page = dict(params)
            page["limit"] = (
                _PAGE_SIZE
                if predicate or limit is None
                else min(_PAGE_SIZE, limit - len(results))
            )
            page["offset"] = offset
            batch = self._get_json(path, params=page)
            if not batch:
                break
            results.extend(
                record for record in batch if predicate is None or predicate(record)
            )
            if len(batch) < page["limit"]:
                break
            offset += len(batch)
        return results if limit is None else results[:limit]

    def search_feeds(
        self,
        aoi=None,
        *,
        country_code=None,
        subdivision=None,
        municipality=None,
        status="active",
        official_only=False,
        enclosure="partially_enclosed",
        limit=100,
    ):
        """Search GTFS feeds in the Mobility Database catalog.

        Parameters
        ----------
        aoi : geometry or tuple, optional
            Area of interest: a shapely geometry, a GeoDataFrame/GeoSeries,
            or a (minx, miny, maxx, maxy) tuple in WGS84. Reduced to its
            bounding box for the API query.
        country_code : str, optional
            ISO 3166-1 alpha-2 country code filter.
        subdivision : str, optional
            Subdivision (state/region) name filter.
        municipality : str, optional
            Municipality name filter.
        status : str or None, default "active"
            Keep only feeds with this catalog status; ``None`` disables the
            filter.
        official_only : bool, default False
            Keep only feeds marked official by the operator.
        enclosure : {"partially_enclosed", "completely_enclosed"}
            How feed extents must relate to the AOI bounding box.
        limit : int, default 100
            Maximum number of matching feeds to return.

        Returns
        -------
        list of Feed

        Notes
        -----
        Without a refresh token this method falls back to the Mobility
        Database CSV catalogue export (with a ``UserWarning``): feed
        discovery still works, but historical dataset versions and hosted
        validation reports need the API and therefore a token.
        """
        if enclosure not in ("partially_enclosed", "completely_enclosed"):
            raise ValueError(
                "enclosure must be 'partially_enclosed' or 'completely_enclosed'"
            )
        bounds = _bounds(aoi) if aoi is not None else None
        if not self._refresh_token:
            warnings.warn(
                "no Mobility Database refresh token configured; falling back "
                "to the CSV catalogue export (no historical datasets or "
                "hosted validation reports)",
                UserWarning,
                stacklevel=2,
            )
            path = fetch_catalog_csv(self._cache_dir, self._http)
            return search_csv(
                path,
                bounds=bounds,
                country_code=country_code,
                subdivision=subdivision,
                municipality=municipality,
                status=status,
                official_only=official_only,
                enclosure=enclosure,
                limit=limit,
            )
        params = {}
        if bounds is not None:
            minx, miny, maxx, maxy = bounds
            params["dataset_latitudes"] = f"{miny},{maxy}"
            params["dataset_longitudes"] = f"{minx},{maxx}"
            params["bounding_filter_method"] = enclosure
        if country_code:
            params["country_code"] = country_code
        if subdivision:
            params["subdivision_name"] = subdivision
        if municipality:
            params["municipality"] = municipality
        if official_only:
            params["is_official"] = True
        if status is None:
            predicate = None
        else:

            def predicate(record):
                return record.get("status") == status

        records = self._paginated("/gtfs_feeds", params, limit, predicate)
        return [Feed.from_api(record) for record in records]

    def feed(self, feed_id):
        """Fetch a single feed by its catalog ID.

        Returns
        -------
        Feed
        """
        return Feed.from_api(self._get_json(f"/gtfs_feeds/{feed_id}"))

    def datasets(self, feed, *, limit=100):
        """List catalogued dataset versions of a feed, newest first.

        Parameters
        ----------
        feed : Feed or str
            The feed, or its catalog ID.
        limit : int or None, default 100
            Maximum number of datasets to return; ``None`` fetches every
            catalogued version.

        Returns
        -------
        list of Dataset
        """
        feed_id = feed.id if isinstance(feed, Feed) else feed
        records = self._paginated(f"/gtfs_feeds/{feed_id}/datasets", {}, limit)
        datasets = [Dataset.from_api(record) for record in records]
        datasets.sort(key=lambda d: d.downloaded_at or _EPOCH, reverse=True)
        return datasets

    def dataset_for(self, feed, when):
        """Pick the dataset whose published service range covers a date.

        The most recently downloaded covering dataset wins; ``None`` is
        returned when no catalogued dataset covers ``when``. Published
        service ranges are frequently optimistic, so coverage should be
        verified against the actual calendar files after download.

        Parameters
        ----------
        feed : Feed or str
            The feed, or its catalog ID.
        when : datetime.date, datetime.datetime or str
            The service day; any time-of-day component is ignored.

        Returns
        -------
        Dataset or None
        """
        when = as_date(when)
        for dataset in self.datasets(feed, limit=None):
            if dataset.covers(when):
                return dataset
        return None

    def download(self, dataset, directory=None):
        """Download a dataset zip with checksum verification and caching.

        A cached copy whose SHA-256 matches the catalogued hash is reused
        without a network request. A ``<dataset id>.provenance.json`` sidecar
        records feed and dataset IDs, source URL, checksum and retrieval
        timestamp for reproducibility.

        Parameters
        ----------
        dataset : Dataset
        directory : str or pathlib.Path, optional
            Target directory; defaults to the transitio cache.

        Returns
        -------
        pathlib.Path
            Path of the downloaded zip.
        """
        if not dataset.hosted_url:
            raise DownloadError(f"dataset {dataset.id} has no hosted download url")
        target_dir = (
            Path(directory)
            if directory
            else self._cache_dir / "gtfs" / _safe_id(dataset.feed_id)
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{_safe_id(dataset.id)}.zip"
        if path.exists() and dataset.hash and _sha256(path) == dataset.hash:
            return path
        # The catalog token is never sent to download hosts.
        digest = _stream_download(self._http, dataset.hosted_url, path)
        if dataset.hash and digest != dataset.hash:
            path.unlink()
            raise DownloadError(
                f"checksum mismatch for dataset {dataset.id}: "
                f"expected {dataset.hash}, got {digest}"
            )
        provenance = {
            "feed_id": dataset.feed_id,
            "dataset_id": dataset.id,
            "source_url": dataset.hosted_url,
            "sha256": digest,
            "service_date_range": [
                str(dataset.service_start) if dataset.service_start else None,
                str(dataset.service_end) if dataset.service_end else None,
            ],
            "retrieved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        path.with_suffix(".provenance.json").write_text(
            json.dumps(provenance, indent=2)
        )
        return path

    def download_latest(self, feed, directory=None):
        """Download the latest hosted dataset zip of a feed.

        Works without an API token: the URL comes from the catalogue entry.
        The latest dataset is a moving target, so the file is re-downloaded
        on every call; no upstream checksum is available, and the provenance
        sidecar records the computed SHA-256 only. With a token,
        :meth:`dataset_for` plus :meth:`download` give checksum-verified,
        version-pinned downloads instead.

        Parameters
        ----------
        feed : Feed
        directory : str or pathlib.Path, optional
            Target directory; defaults to the transitio cache.

        Returns
        -------
        pathlib.Path
            Path of the downloaded zip.
        """
        if not feed.latest_dataset_url:
            raise DownloadError(f"feed {feed.id} has no hosted latest-dataset url")
        target_dir = (
            Path(directory)
            if directory
            else self._cache_dir / "gtfs" / _safe_id(feed.id)
        )
        path = target_dir / "latest.zip"
        digest = _stream_download(self._http, feed.latest_dataset_url, path)
        provenance = {
            "feed_id": feed.id,
            "source_url": feed.latest_dataset_url,
            "sha256": digest,
            "retrieved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        path.with_suffix(".provenance.json").write_text(
            json.dumps(provenance, indent=2)
        )
        return path

    def validation_report(self, dataset):
        """Fetch the hosted canonical-validator JSON report for a dataset.

        Returns
        -------
        dict or None
            The report, or ``None`` when the catalog holds no report for
            this dataset version.
        """
        if not dataset.validation_report_url:
            return None
        response = self._http.get(dataset.validation_report_url)
        response.raise_for_status()
        return response.json()
