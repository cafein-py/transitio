"""No-token fallback over the Mobility Database CSV catalogue export."""

from __future__ import annotations

import csv
import time
from pathlib import Path

from shapely.geometry import box

from beanpicker.catalog._models import Feed

CSV_CATALOG_URL = "https://files.mobilitydatabase.org/feeds_v2.csv"

_MAX_AGE_SECONDS = 24 * 3600

# The live export schema is not verifiable from every environment, so every
# field lookup tolerates naming variants through these alias tuples.
_ALIASES = {
    "country_code": ("location.country_code", "country_code"),
    "subdivision": ("location.subdivision_name", "subdivision_name"),
    "municipality": ("location.municipality", "municipality"),
    "min_lat": ("location.bounding_box.minimum_latitude", "minimum_latitude"),
    "max_lat": ("location.bounding_box.maximum_latitude", "maximum_latitude"),
    "min_lon": ("location.bounding_box.minimum_longitude", "minimum_longitude"),
    "max_lon": ("location.bounding_box.maximum_longitude", "maximum_longitude"),
}


def _first(row, *names):
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


def _feed_from_row(row):
    official = _first(row, "is_official", "official")
    location = {
        "country_code": _first(row, *_ALIASES["country_code"]),
        "subdivision_name": _first(row, *_ALIASES["subdivision"]),
        "municipality": _first(row, *_ALIASES["municipality"]),
    }
    return Feed(
        id=row["id"],
        provider=_first(row, "provider"),
        status=_first(row, "status"),
        official=(
            official.strip().lower() in ("true", "1", "yes")
            if official is not None
            else None
        ),
        producer_url=_first(row, "urls.direct_download", "urls.direct_download_url"),
        license_url=_first(row, "urls.license", "urls.license_url"),
        latest_dataset_url=_first(row, "urls.latest", "urls.latest_url"),
        locations=(location,),
        raw=dict(row),
    )


def _row_box(row):
    values = []
    for key in ("min_lon", "min_lat", "max_lon", "max_lat"):
        raw = _first(row, *_ALIASES[key])
        if raw is None:
            return None
        try:
            values.append(float(raw))
        except ValueError:
            return None
    return box(*values)


def fetch_catalog_csv(cache_dir, client, *, update=False):
    """Download the CSV catalogue export, reusing a cached copy under 24h old."""
    path = Path(cache_dir) / "catalog" / "feeds_v2.csv"
    fresh = path.exists() and time.time() - path.stat().st_mtime < _MAX_AGE_SECONDS
    if update or not fresh:
        response = client.get(CSV_CATALOG_URL)
        response.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(response.content)
    return path


def search_csv(
    path,
    *,
    bounds=None,
    country_code=None,
    subdivision=None,
    municipality=None,
    status="active",
    official_only=False,
    enclosure="partially_enclosed",
    limit=100,
):
    """Filter the CSV catalogue with the same semantics as the API search."""
    aoi_box = box(*bounds) if bounds is not None else None
    feeds = []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            if _first(row, "data_type") != "gtfs":
                continue
            if country_code:
                value = _first(row, *_ALIASES["country_code"]) or ""
                if value.upper() != country_code.upper():
                    continue
            if subdivision:
                value = _first(row, *_ALIASES["subdivision"]) or ""
                if value.lower() != subdivision.lower():
                    continue
            if municipality:
                value = _first(row, *_ALIASES["municipality"]) or ""
                if value.lower() != municipality.lower():
                    continue
            if aoi_box is not None:
                feed_box = _row_box(row)
                if feed_box is None:
                    continue
                if enclosure == "completely_enclosed":
                    if not aoi_box.contains(feed_box):
                        continue
                elif not aoi_box.intersects(feed_box):
                    continue
            feed = _feed_from_row(row)
            if status is not None and feed.status != status:
                continue
            if official_only and not feed.official:
                continue
            feeds.append(feed)
            if len(feeds) >= limit:
                break
    return feeds
