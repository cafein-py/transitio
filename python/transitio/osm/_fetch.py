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


def _extract(geometry, update, directory, must_cover, output_path=None):
    """pyrosm's smallest extract, or smaller set of extracts merged into one,
    that covers ``must_cover`` (else ``geometry``), downloaded into
    ``directory``; with ``output_path`` it is cropped there to the envelope
    of ``geometry``."""
    from pyrosm import get_data_by_area
    from pyrosm.exceptions import ExtractDownloadError
    from pyrosm.exceptions import ExtractNotFoundError as NoExtract

    directory.mkdir(parents=True, exist_ok=True)
    try:
        return get_data_by_area(
            geometry,
            crop=output_path is not None,
            output_path=output_path,
            update=update,
            directory=str(directory),
            strategy="smallest_total",
            must_cover=must_cover,
        )
    except NoExtract as error:
        raise ExtractNotFoundError(str(error)) from error
    except ExtractDownloadError as error:
        raise DownloadError(str(error)) from error


def _fmt_coord(value):
    return f"{value:.5f}".rstrip("0").rstrip(".")


def _crop_filename(aoi, geometry, must_cover=None):
    if isinstance(aoi, str) and must_cover is None:
        slug = re.sub(r"[^a-z0-9]+", "-", aoi.lower()).strip("-") or "place"
        # Distinct place names can normalize to one slug (non-ASCII names
        # especially); the digest keeps their cache entries apart.
        digest = hashlib.sha256(aoi.encode("utf-8")).hexdigest()[:8]
        return f"{slug}-{digest}.osm.pbf"
    coords = "_".join(_fmt_coord(v) for v in geometry.bounds)
    if must_cover is None and geometry.equals(box(*geometry.bounds)):
        return f"bbox_{coords}.osm.pbf"
    # True polygons need more than their envelope in the cache key, or
    # different AOIs sharing a bounding box would reuse the first crop;
    # another must_cover can choose other extracts.
    key = geometry.wkb if must_cover is None else geometry.wkb + must_cover.wkb
    digest = hashlib.sha256(key).hexdigest()[:12]
    return f"aoi_{coords}_{digest}.osm.pbf"


def _written_at(*paths):
    """The UTC time the oldest of the files at ``paths`` was last written, as
    ISO 8601."""
    written = min(os.stat(path).st_mtime for path in paths)
    return datetime.datetime.fromtimestamp(written, datetime.timezone.utc).isoformat()


def _drop_stale_sidecars(directory):
    """Remove each extract sidecar in ``directory`` older than the
    ``.osm.pbf`` it describes, which was replaced after it was written."""
    for sidecar in Path(directory).glob("*.osm.provenance.json"):
        pbf = sidecar.with_name(sidecar.name.removesuffix(".provenance.json") + ".pbf")
        with contextlib.suppress(FileNotFoundError):
            if sidecar.stat().st_mtime < pbf.stat().st_mtime:
                sidecar.unlink()


def _isoformat(moment):
    return None if moment is None else moment.isoformat()


def _write_provenance(path, *, geometry, extract, must_cover, cropped):
    sources = [
        {
            "url": source.url,
            "provider": source.provider,
            "extract": source.extract,
            "bytes": source.bytes,
            "sha256": source.sha256,
            "snapshot": _isoformat(source.snapshot),
            "retrieved_at": _written_at(source.path),
        }
        for source in extract.sources
    ]
    record = {
        "source_url": extract.url,
        "provider": extract.provider,
        "extract": extract.extract,
        "extract_bytes": extract.bytes,
        "failed_extracts": [
            {"url": url, "error": error} for url, error in extract.failed
        ],
        "extract_sha256": sources[0]["sha256"] if len(sources) == 1 else None,
        "file_sha256": extract.sha256,
        "cropped": cropped,
        "aoi_bounds": list(geometry.bounds),
        "retrieved_at": _written_at(*(source.path for source in extract.sources)),
        "sources": sources,
        "snapshot": _isoformat(extract.snapshot),
        "must_cover_bounds": None if must_cover is None else list(must_cover.bounds),
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
    must_cover=None,
    directory=None,
    cache_dir=None,
    update=False,
):
    """Download (and by default crop) the smallest OSM data covering an AOI.

    The AOI is grown by ``buffer_m``, then pyrosm's ``get_data_by_area``
    compares Geofabrik and BBBike extracts and Movisda's administrative areas
    and 1° and 10° grid tiles. It picks the smallest single extract that
    contains the grown AOI, or a set of extracts that together cover it and
    are smaller in total, merged into one file; a set holds at most one
    Movisda area and no grid tile. With ``must_cover`` only that part of the
    AOI has to be covered. pyrosm downloads each extract, three attempts
    each, resuming a dropped download where the server allows, and chooses
    again without an extract that fails. By default the result is cropped to
    the envelope of the grown AOI. The extracts cover the grown AOI (or
    ``must_cover``), though not always its whole envelope, and Movisda cuts
    ways at its edges; either way only data outside the AOI (or
    ``must_cover``) can be missing from the crop.

    Ranking needs the network. pyrosm fetches Movisda's index (kept for a
    day) and asks Geofabrik and BBBike for download sizes (kept for a week);
    what it cannot fetch is skipped or ranked last, with a ``UserWarning``.
    A ``.provenance.json`` sidecar records the source extract's URL,
    provider, id and size, the smaller extracts whose download failed, the
    checksums, the grown AOI's bounds and ``retrieved_at``, the time the
    extract was downloaded (its file's modification time). ``sources`` lists
    each downloaded extract in merge order (``url``, ``provider``,
    ``extract``, ``bytes``, ``sha256``, ``snapshot`` and ``retrieved_at``),
    ``snapshot`` is when the data was taken (from the PBF header; None when
    it has none) and ``must_cover_bounds`` the bounds of the clipped
    ``must_cover`` (None without it). For a merged set ``source_url`` and
    ``extract_sha256`` are None, ``provider`` and ``extract`` join the
    sources' with ``+``, ``extract_bytes`` is their total (None when one is
    unknown) and ``retrieved_at`` and ``snapshot`` are the oldest source's.
    A crop is reused while it and its sidecar exist. Calls sharing the cache
    or ``directory`` take turns (a ``.fetch_pbf.lock`` file there), so a
    file and its sidecar always describe the same extracts.

    Parameters
    ----------
    aoi : geometry, GeoDataFrame/GeoSeries, tuple or str
        Area of interest: a shapely geometry, a GeoDataFrame/GeoSeries, a
        ``(minx, miny, maxx, maxy)`` tuple in WGS84, or a place name to
        geocode via Nominatim.
    crop : bool, default True
        Crop the downloaded extract to the envelope of the grown AOI;
        ``False`` returns the full extract, or for a set the merged file,
        ``merged_<hash>.osm.pbf`` beside its sources.
    buffer_m : float, default 0
        Metres to grow the AOI (and ``must_cover``) by before the extract is
        picked and cropped. Each part of the geometry is buffered in the UTM
        zone of its centroid and the results are unioned, so far-apart parts
        each grow by the full distance; 0 leaves the AOI unchanged. A part
        does not grow across the antimeridian, which one extract envelope
        cannot span; a ``UserWarning`` says when a part is clipped there.
    must_cover : geometry or GeoDataFrame/GeoSeries, optional
        What the extracts must cover in place of the whole AOI, in WGS84,
        such as the transit stops routing needs, so sea or unserved land in
        the AOI does not force a larger extract. It is grown by ``buffer_m``
        and clipped to the grown AOI: ``fetch_pbf(area, buffer_m=1600,
        must_cover=stops)`` covers the grown area within 1.6 km of each
        stop. The crop still follows the AOI, so its other parts may lack
        data.
    directory : str or pathlib.Path, optional
        Directory for the returned file; defaults to the transitio cache.
        Full extracts backing a crop always stay in the cache. With
        ``crop=False`` the extracts are downloaded there, and pyrosm keeps
        its index and size caches beside them.
    cache_dir : str or pathlib.Path, optional
        Cache directory for full extracts. Defaults to the platform user
        cache directory for transitio.
    update : bool, default False
        Re-download the extracts and refresh the provider indexes and sizes,
        even when a cached copy exists.

    Returns
    -------
    pathlib.Path
        Path of the ``.osm.pbf`` file.

    Raises
    ------
    ExtractNotFoundError
        When no Geofabrik, BBBike or Movisda extract, nor a set of them,
        covers the grown AOI (or ``must_cover``).
    DownloadError
        When every extract that would cover it fails to download.
    ValueError
        When the grown AOI has no area, or ``must_cover`` lies outside it.
    """
    geometry = _buffered(_as_geometry(aoi), buffer_m)
    minx, miny, maxx, maxy = geometry.bounds
    if geometry.is_empty or not (minx < maxx and miny < maxy):
        raise ValueError("the AOI has no area; grow a point or line with buffer_m")
    if must_cover is not None:
        must_cover = _buffered(_as_geometry(must_cover), buffer_m).intersection(
            geometry
        )
        if must_cover.is_empty:
            raise ValueError("must_cover lies outside the grown AOI")
    cache = (
        Path(cache_dir) if cache_dir else Path(platformdirs.user_cache_dir("transitio"))
    )
    extract_dir = cache / "osm"
    out_dir = Path(directory) if directory else extract_dir

    # A crop's full extracts stay in the cache; full extracts go to out_dir.
    source_dir = extract_dir if crop else out_dir
    if crop:
        # A grown place name is named by its geometry, not by the name alone.
        name = _crop_filename(geometry if buffer_m else aoi, geometry, must_cover)
        target = out_dir / name
        # The sidecar marks a whole crop: it is removed before the crop is
        # replaced and written again after.
        sidecar = target.with_suffix(".provenance.json")
        if target.exists() and sidecar.exists() and not update:
            return target
    with _taking_turns(source_dir, out_dir):
        # Another call may have made the crop while this one waited.
        if crop and target.exists() and sidecar.exists() and not update:
            return target
        try:
            if crop:
                with _http.staged(target) as partial:
                    extract = _extract(
                        geometry, update, source_dir, must_cover, str(partial)
                    )
                    sidecar.unlink(missing_ok=True)
            else:
                extract = _extract(geometry, update, source_dir, must_cover)
                target = Path(extract.path)
        finally:
            if update or not crop:
                # pyrosm may have replaced extracts even when it then failed.
                _drop_stale_sidecars(source_dir)
        _write_provenance(
            target,
            geometry=geometry,
            extract=extract,
            must_cover=must_cover,
            cropped=crop,
        )
    return target
