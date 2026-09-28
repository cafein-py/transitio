"""AOI-driven OSM extract download and cropping, built on pyrosm."""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import warnings
from pathlib import Path

import httpx
import numpy as np
import platformdirs
import shapely
from shapely.geometry import box
from shapely.geometry.base import BaseGeometry

from transitio.exceptions import ExtractNotFoundError


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


def _resolve_url(geometry, update):
    """Return the PBF URL of the smallest covering Geofabrik extract."""
    from pyrosm import get_data_by_bbox

    try:
        return get_data_by_bbox(geometry, download=False, update=update)
    except ValueError as error:
        raise ExtractNotFoundError(str(error)) from error


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url, path, update, transport=None):
    if path.exists() and not update:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.parent / (path.name + ".part")
    client = httpx.Client(follow_redirects=True, timeout=60.0, transport=transport)
    with client:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            with open(partial, "wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)
    partial.replace(path)


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


def _write_provenance(path, *, geometry, url, extract_sha256, cropped):
    record = {
        "source_url": url,
        "extract_sha256": extract_sha256,
        "file_sha256": _sha256(path) if cropped else extract_sha256,
        "cropped": cropped,
        "aoi_bounds": list(geometry.bounds),
        "retrieved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    path.with_suffix(".provenance.json").write_text(json.dumps(record, indent=2))


def fetch_pbf(
    aoi,
    *,
    crop=True,
    buffer_m=0,
    directory=None,
    cache_dir=None,
    update=False,
    transport=None,
):
    """Download (and by default crop) the OSM extract covering an AOI.

    Resolution and cropping build on pyrosm: the AOI is grown by ``buffer_m``,
    the smallest Geofabrik extract whose extent covers the grown AOI is picked
    from pyrosm's bundled extract index, its ``.osm.pbf`` is downloaded into
    the transitio cache, and by default the result is cropped to the envelope
    of the grown AOI (pyrosm crops a polygon by its bounding box). A
    ``.provenance.json`` sidecar records the source extract URL, checksums,
    the grown AOI's bounds and the retrieval timestamp.

    Parameters
    ----------
    aoi : geometry, GeoDataFrame/GeoSeries, tuple or str
        Area of interest: a shapely geometry, a GeoDataFrame/GeoSeries, a
        ``(minx, miny, maxx, maxy)`` tuple in WGS84, or a place name to
        geocode via Nominatim.
    crop : bool, default True
        Crop the downloaded extract to the envelope of the grown AOI;
        ``False`` returns the full covering extract.
    buffer_m : float, default 0
        Metres to grow the AOI by before the extract is picked and cropped.
        Each part of the geometry is buffered in the UTM zone of its centroid
        and the results are unioned, so far-apart parts each grow by the full
        distance; 0 leaves the AOI unchanged. A part does not grow across
        the antimeridian, which one extract envelope cannot span; a
        ``UserWarning`` says when a part is clipped there.
    directory : str or pathlib.Path, optional
        Directory for the returned file; defaults to the transitio cache.
        Full extracts backing a crop always stay in the cache.
    cache_dir : str or pathlib.Path, optional
        Cache directory for full extracts. Defaults to the platform user
        cache directory for transitio.
    update : bool, default False
        Re-download the extract (and refresh the extract index) even when a
        cached copy exists.
    transport : httpx.BaseTransport, optional
        Custom transport, mainly for testing.

    Returns
    -------
    pathlib.Path
        Path of the ``.osm.pbf`` file.
    """
    geometry = _buffered(_as_geometry(aoi), buffer_m)
    cache = (
        Path(cache_dir) if cache_dir else Path(platformdirs.user_cache_dir("transitio"))
    )
    extract_dir = cache / "osm"
    out_dir = Path(directory) if directory else extract_dir

    url = _resolve_url(geometry, update)
    filename = url.rsplit("/", 1)[-1]

    if crop:
        # A grown place name is named by its geometry, not by the name alone.
        target = out_dir / _crop_filename(geometry if buffer_m else aoi, geometry)
        extract_path = extract_dir / filename
    else:
        target = out_dir / filename
        extract_path = target
    if target.exists() and not update:
        return target

    _download(url, extract_path, update, transport)
    extract_sha256 = _sha256(extract_path)

    if crop:
        from pyrosm import OSM

        out_dir.mkdir(parents=True, exist_ok=True)
        OSM(str(extract_path), bounding_box=geometry).to_pbf(output_path=str(target))

    _write_provenance(
        target,
        geometry=geometry,
        url=url,
        extract_sha256=extract_sha256,
        cropped=crop,
    )
    return target
