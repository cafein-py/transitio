"""AOI-driven OSM extract download and cropping, built on pyrosm."""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import os
import re
import warnings
from pathlib import Path

import numpy as np
import platformdirs
import shapely
from shapely.geometry import box
from shapely.geometry.base import BaseGeometry

from transitio import _http
from transitio.exceptions import DownloadError, ExtractNotFoundError


def _as_geometry(aoi):
    """Normalise an AOI to a shapely geometry.

    Accepts a shapely geometry, a GeoDataFrame/GeoSeries, a
    (minx, miny, maxx, maxy) tuple, or a place name geocoded via Nominatim.
    """
    if isinstance(aoi, str):
        from pyrosm import geocode

        return geocode(aoi)
    if hasattr(aoi, "total_bounds"):  # GeoDataFrame / GeoSeries
        geoms = getattr(aoi, "geometry", aoi)
        if hasattr(geoms, "union_all"):
            return geoms.union_all()
        return geoms.unary_union
    if isinstance(aoi, BaseGeometry):
        return aoi
    try:
        values = tuple(float(v) for v in aoi)
    except (TypeError, ValueError):
        values = ()
    if len(values) != 4:
        raise ValueError(
            "aoi must be a geometry, GeoDataFrame/GeoSeries, a "
            "(minx, miny, maxx, maxy) tuple or a place name"
        )
    minx, miny, maxx, maxy = values
    if not (minx <= maxx and miny <= maxy):
        raise ValueError("invalid bounding box: expected minx <= maxx and miny <= maxy")
    return box(minx, miny, maxx, maxy)


def _utm_groups(geoms):
    """Yield ``(central meridian, projected)`` per UTM zone: the geometries
    whose centroid falls in the zone, as a GeoSeries in that zone's CRS."""
    import geopandas as gpd

    centroids = shapely.centroid(geoms)
    lon, lat = shapely.get_x(centroids), shapely.get_y(centroids)
    zones = np.floor((lon + 180.0) / 6.0).astype(int) % 60 + 1
    codes = np.where(lat < 0, 32700, 32600) + zones
    for code in np.unique(codes):
        chosen = gpd.GeoSeries(geoms[codes == code], crs="EPSG:4326")
        yield 6 * (int(code) % 100) - 183, chosen.to_crs(int(code))


def _buffered(geometry, buffer_m):
    """``geometry`` grown by ``buffer_m`` metres; unchanged at 0.

    Each part is buffered in the UTM zone of its centroid and the results are
    unioned in WGS84, so far-apart parts each grow by the full distance.
    """
    if not buffer_m:
        return geometry
    world = box(-180.0, -90.0, 180.0, 90.0)
    grown, crossed = [], False
    for meridian, projected in _utm_groups(shapely.get_parts(geometry)):
        # Longitudes wrap around the zone's meridian, so a part grown across
        # the antimeridian is clipped there instead of spanning the globe:
        # one extract envelope cannot span it.
        wgs84 = f"+proj=longlat +datum=WGS84 +lon_wrap={meridian}"
        back = projected.buffer(buffer_m).to_crs(wgs84).to_numpy()
        crossed = crossed or not shapely.covered_by(back, world).all()
        grown.extend(shapely.intersection(back, world))
    if crossed:
        warnings.warn(
            "the AOI is not grown across the antimeridian", UserWarning, stacklevel=3
        )
    return shapely.union_all(grown)


def _area_km2(geometry):
    """The area of ``geometry`` in km², each part measured in the UTM zone of
    its centroid."""
    parts = shapely.get_parts(geometry)
    return sum(projected.area.sum() for _, projected in _utm_groups(parts)) / 1e6


def _extract(geometry, update, directory):
    """The smallest single extract that contains ``geometry``, downloaded by
    pyrosm into ``directory``."""
    from pyrosm import get_data_by_area
    from pyrosm.exceptions import ExtractDownloadError

    directory.mkdir(parents=True, exist_ok=True)
    try:
        return get_data_by_area(
            geometry, crop=False, update=update, directory=str(directory)
        )
    except ValueError as error:
        raise ExtractNotFoundError(str(error)) from error
    except ExtractDownloadError as error:
        raise DownloadError(str(error)) from error


def _fmt_coord(value):
    return f"{value:.5f}".rstrip("0").rstrip(".")


def _crop_filename(aoi, geometry):
    if isinstance(aoi, str):
        slug = re.sub(r"[^a-z0-9]+", "-", aoi.lower()).strip("-") or "place"
        # Distinct place names can normalize to one slug (non-ASCII names
        # especially); the digest keeps their cache entries apart.
        digest = hashlib.sha256(aoi.encode("utf-8")).hexdigest()[:8]
        return f"{slug}-{digest}.osm.pbf"
    coords = "_".join(_fmt_coord(v) for v in geometry.bounds)
    if geometry.equals(box(*geometry.bounds)):
        return f"bbox_{coords}.osm.pbf"
    # True polygons need more than their envelope in the cache key, or
    # different AOIs sharing a bounding box would reuse the first crop.
    digest = hashlib.sha256(geometry.wkb).hexdigest()[:12]
    return f"aoi_{coords}_{digest}.osm.pbf"


def _checksum_and_written(path):
    """The SHA-256 hex of the file at ``path`` and the UTC time it was last
    written, read from one open file."""
    with open(path, "rb") as handle:
        written = os.fstat(handle.fileno()).st_mtime
        digest = _http.sha256_stream(handle)
    utc = datetime.datetime.fromtimestamp(written, datetime.timezone.utc)
    return digest, utc.isoformat()


def _write_provenance(
    path, *, geometry, extract, extract_sha256, retrieved_at, cropped
):
    record = {
        "source_url": extract.url,
        "provider": extract.provider,
        "extract": extract.extract,
        "extract_bytes": extract.bytes,
        "failed_extracts": [
            {"url": url, "error": error} for url, error in extract.failed
        ],
        "extract_sha256": extract_sha256,
        "file_sha256": _http.sha256_file(path) if cropped else extract_sha256,
        "cropped": cropped,
        "aoi_bounds": list(geometry.bounds),
        "retrieved_at": retrieved_at,
    }
    from transitio.catalog._client import _write_provenance as write_sidecar

    write_sidecar(path.with_suffix(".provenance.json"), record)


@contextlib.contextmanager
def _taking_turns(*directories):
    """Hold the fetch lock of each directory, taken in one order so that
    calls sharing directories never wait on each other in a cycle."""
    with contextlib.ExitStack() as stack:
        for directory in sorted({Path(d).resolve() for d in directories}):
            stack.enter_context(_http.locked(directory / ".fetch_pbf.lock"))
        yield


def fetch_pbf(
    aoi,
    *,
    crop=True,
    buffer_m=0,
    directory=None,
    cache_dir=None,
    update=False,
):
    """Download (and by default crop) the smallest OSM extract containing an AOI.

    The AOI is grown by ``buffer_m``, then pyrosm's ``get_data_by_area``
    picks the smallest single extract that contains it, among Geofabrik and
    BBBike extracts and Movisda's administrative areas and 1° and 10° grid
    tiles. pyrosm downloads it, three attempts per extract, falling back to
    the next smallest. By default the result is cropped to the envelope of
    the grown AOI. The extract contains the grown AOI, though not always its
    whole envelope, and Movisda cuts ways at its tile edges; either way only
    data outside the AOI can be missing from the crop.

    Ranking needs the network. pyrosm fetches Movisda's index (kept for a
    day) and asks Geofabrik and BBBike for download sizes (kept for a week);
    what it cannot fetch is skipped or ranked last, with a ``UserWarning``.
    A ``.provenance.json`` sidecar records the source extract's URL,
    provider, id and size, the smaller extracts whose download failed, the
    checksums, the grown AOI's bounds and ``retrieved_at``, the time the
    extract was downloaded (its file's modification time). A crop is reused
    while it and its sidecar exist. Calls sharing the cache or
    ``directory`` take turns (a ``.fetch_pbf.lock`` file there), so a file
    and its sidecar always describe the same extract.

    Parameters
    ----------
    aoi : geometry, GeoDataFrame/GeoSeries, tuple or str
        Area of interest: a shapely geometry, a GeoDataFrame/GeoSeries, a
        ``(minx, miny, maxx, maxy)`` tuple in WGS84, or a place name to
        geocode via Nominatim.
    crop : bool, default True
        Crop the downloaded extract to the envelope of the grown AOI;
        ``False`` returns the full extract.
    buffer_m : float, default 0
        Metres to grow the AOI by before the extract is picked and cropped.
        Each part of the geometry is buffered in the UTM zone of its centroid
        and the results are unioned, so far-apart parts each grow by the full
        distance; 0 leaves the AOI unchanged. A part does not grow across
        the antimeridian, which one extract envelope cannot span; a
        ``UserWarning`` says when a part is clipped there.
    directory : str or pathlib.Path, optional
        Directory for the returned file; defaults to the transitio cache.
        Full extracts backing a crop always stay in the cache. With
        ``crop=False`` the extract is downloaded there, and pyrosm keeps its
        index and size caches beside it.
    cache_dir : str or pathlib.Path, optional
        Cache directory for full extracts. Defaults to the platform user
        cache directory for transitio.
    update : bool, default False
        Re-download the extract and refresh the provider indexes and sizes,
        even when a cached copy exists.

    Returns
    -------
    pathlib.Path
        Path of the ``.osm.pbf`` file.

    Raises
    ------
    ExtractNotFoundError
        When no Geofabrik, BBBike or Movisda extract contains the grown AOI.
    DownloadError
        When every extract that contains the grown AOI fails to download.
    ValueError
        When the grown AOI has no area.
    """
    geometry = _buffered(_as_geometry(aoi), buffer_m)
    minx, miny, maxx, maxy = geometry.bounds
    if geometry.is_empty or not (minx < maxx and miny < maxy):
        raise ValueError("the AOI has no area; grow a point or line with buffer_m")
    cache = (
        Path(cache_dir) if cache_dir else Path(platformdirs.user_cache_dir("transitio"))
    )
    extract_dir = cache / "osm"
    out_dir = Path(directory) if directory else extract_dir

    # A crop's full extract stays in the cache; a full extract goes to out_dir.
    source_dir = extract_dir if crop else out_dir
    if crop:
        # A grown place name is named by its geometry, not by the name alone.
        target = out_dir / _crop_filename(geometry if buffer_m else aoi, geometry)
        # The sidecar marks a whole crop: it is removed before the crop is
        # replaced and written again after.
        sidecar = target.with_suffix(".provenance.json")
        if target.exists() and sidecar.exists() and not update:
            return target
    with _taking_turns(source_dir, out_dir):
        # Another call may have made the crop while this one waited.
        if crop and target.exists() and sidecar.exists() and not update:
            return target
        extract = _extract(geometry, update, source_dir)
        if update or not crop:
            # pyrosm may have replaced the extract, so its sidecar goes before
            # anything else can fail; a full extract's is written again below.
            Path(extract.path).with_suffix(".provenance.json").unlink(missing_ok=True)
        extract_sha256, retrieved_at = _checksum_and_written(extract.path)
        if crop:
            from pyrosm import OSM

            with _http.staged(target) as partial:
                OSM(extract.path, bounding_box=geometry).to_pbf(
                    output_path=str(partial)
                )
                sidecar.unlink(missing_ok=True)
        else:
            target = Path(extract.path)
        _write_provenance(
            target,
            geometry=geometry,
            extract=extract,
            extract_sha256=extract_sha256,
            retrieved_at=retrieved_at,
            cropped=crop,
        )
    return target
