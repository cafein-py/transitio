"""The one-call acquisition pipeline."""

from __future__ import annotations

import csv
import dataclasses
import hashlib
import io
import json
import os
import pathlib
import warnings
import zipfile

# Coarse mode names over GTFS route types, including the extended blocks:
# railway 100s and suburban railway 300s are rail; urban railway 400s,
# metro 500s, underground 600s and monorail join subway; coach 200s,
# bus 700s and trolleybus 800s join bus; tram 900s; water 1000s and
# ferry 1200s are ferry. Aerial, funicular, taxi and air map to no mode.
_MODE_TYPES = {
    "tram": {0, 5} | set(range(900, 1000)),
    "subway": {1, 12} | set(range(400, 700)),
    "rail": {2} | set(range(100, 200)) | set(range(300, 400)),
    "bus": {3, 11} | set(range(200, 300)) | set(range(700, 900)),
    "ferry": {4} | set(range(1000, 1100)) | set(range(1200, 1300)),
}

# Decompressed budget for the pre-validation routes.txt peek; any real
# routes.txt is far smaller, and validation applies the full budgets later.
_MODES_BYTE_CAP = 64 * 1024 * 1024


@dataclasses.dataclass
class FetchResult:
    """What the pipeline produced for one AOI."""

    osm_pbf: pathlib.Path
    feeds: list
    reports: list
    repairs: list
    skipped: list

    def __iter__(self):  # convenient (pbf, feeds) unpacking
        return iter((self.osm_pbf, self.feeds))

    def to_cafein(self, **options):
        """Build a routable ``cafein.TransportNetwork`` from this result.

        The validated feeds and the OSM extract are handed to
        ``cafein.TransportNetwork.from_gtfs``; keyword arguments pass
        through (``walking_speed_kmph``, ``bounding_box``, ``ultra``,
        ...), and ``osm_pbf=None`` builds without the walking network.

        Requires the ``cafein`` package.
        """
        if not self.feeds:
            raise ValueError("no feeds to build a network from")
        try:
            import cafein
        except ImportError as error:
            raise ImportError(
                "the cafein package is required for to_cafein()"
            ) from error
        options.setdefault("osm_pbf", os.fspath(self.osm_pbf))
        paths = [os.fspath(path) for path in self.feeds]
        return cafein.TransportNetwork.from_gtfs(paths, **options)

    def to_pyrosm(self, **options):
        """Open the OSM extract as a ``pyrosm.OSM`` reader.

        Keyword arguments pass through to ``pyrosm.OSM`` (for example
        ``bounding_box`` to read a sub-area of the cropped extract).
        """
        from pyrosm import OSM

        return OSM(os.fspath(self.osm_pbf), **options)


def _feed_modes(path):
    """Coarse modes served by a feed, from its routes.txt.

    Returns ``None`` when routes.txt cannot be read (missing, over the
    byte budget, or malformed) so the caller can report the feed as
    undeterminable rather than silently unfiltered.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            with archive.open("routes.txt") as handle:
                data = handle.read(_MODES_BYTE_CAP + 1)
        if len(data) > _MODES_BYTE_CAP:
            return None
        text = data.decode("utf-8-sig", errors="replace")
        types = set()
        for row in csv.DictReader(io.StringIO(text)):
            value = (row.get("route_type") or "").strip()
            if value.lstrip("-").isdigit():
                types.add(int(value))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile, csv.Error):
        return None
    return {mode for mode, accepted in _MODE_TYPES.items() if types & accepted}


def _bbox_area(feed):
    """Bounding-box area of a feed's data, or ``None`` when unknown."""
    raw = feed.raw or {}
    bounding_box = (raw.get("latest_dataset") or {}).get("bounding_box") or {}
    values = []
    for key in (
        "minimum_longitude",
        "maximum_longitude",
        "minimum_latitude",
        "maximum_latitude",
    ):
        value = bounding_box.get(key, raw.get(f"location.bounding_box.{key}"))
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            return None
    min_lon, max_lon, min_lat, max_lat = values
    return abs(max_lon - min_lon) * abs(max_lat - min_lat)


def _rank(feed):
    """The plan's documented deterministic preference for overlapping feeds.

    Official before unofficial, active status before anything else, then
    spatial specificity (smaller data bounding box first, unknown extent
    last), with the feed ID as a stable tie-breaker.
    """
    area = _bbox_area(feed)
    return (
        not feed.official,
        feed.status != "active",
        area is None,
        area or 0.0,
        feed.id,
    )


def _covers(service_window, ymd):
    """Whether a validation service window covers a YYYYMMDD day.

    An unknown window (``None``: unreliable calendars or a truncated
    scan) counts as covering — absence of service cannot be proven.
    """
    if not service_window:
        return True
    start, end = service_window
    return start <= ymd <= end


def fetch(
    aoi,
    when=None,
    *,
    modes=None,
    repair=False,
    crop=True,
    refresh_token=None,
    cache_dir=None,
    directory=None,
    country_code=None,
    **budgets,
):
    """Fetch everything cafein needs for an AOI in one call.

    Resolves and crops the OSM extract, discovers all GTFS feeds
    overlapping the AOI, downloads each feed, validates it, optionally
    repairs and spatially crops it, and builds a merged report per feed.
    With an API token, downloads come from catalogued dataset versions
    (checksum-verified, with the hosted canonical-validator report);
    without one, the unversioned latest hosted zip is fetched — a moving
    target with no upstream checksum, documented in its provenance
    sidecar as such. Every overlapping feed is processed, in a
    deterministic order with official feeds first; one broken feed never
    aborts the others — it lands in ``skipped`` with its reason.

    Parameters
    ----------
    aoi : geometry, GeoDataFrame/GeoSeries, tuple or str
        Area of interest (place names are geocoded via Nominatim once,
        and the resulting geometry drives every stage).
    when : str or datetime.date, optional
        Service day the feeds must cover, ``YYYY-MM-DD``. Dataset-version
        selection needs an API token; with or without one, feeds whose
        computed service window (the outer bounds of actual calendar
        activity, not the published range) does not include the day are
        skipped. Exact-day activity is not checked yet.
    modes : str or list of str, optional
        Keep only feeds serving at least one of ``tram``, ``subway``,
        ``rail``, ``bus``, ``ferry`` — decided from the delivered
        (post-crop) feed's routes.txt, since the catalog carries no mode
        metadata. Unknown mode names raise ``ValueError``.
    repair : bool, default False
        Repair each feed (gtfstidy contract) before use; conservative
        default leaves feeds untouched.
    crop : bool, default True
        Spatially crop each feed to the AOI's bounding box.
    refresh_token, cache_dir, directory, country_code
        Passed to the catalog and OSM layers.
    **budgets
        The ``validate_feed`` keyword arguments.

    Returns
    -------
    FetchResult
        ``osm_pbf``, validated ``feeds`` (paths), merged ``reports`` and
        repair ``repairs`` (fix logs, empty without ``repair=True``) per
        kept feed, and ``skipped`` (feed id, reason) pairs. Reports merge
        the local validation of the delivered feed with the hosted report
        of the published dataset, so after cropping or repair the hosted
        side describes the pre-transform original.
    """
    from transitio.catalog import MobilityDatabase
    from transitio.gtfs import crop_feed
    from transitio.osm import fetch_pbf
    from transitio.osm._fetch import _as_geometry
    from transitio.repair import repair_feed
    from transitio.report import build_report
    from transitio.validate import validate_feed

    geometry = _as_geometry(aoi)

    if modes is not None:
        if isinstance(modes, str):
            modes = [modes]
        modes = {str(mode).lower() for mode in modes}
        unknown = modes - set(_MODE_TYPES)
        if unknown:
            raise ValueError(
                f"unknown modes {sorted(unknown)}; "
                f"valid modes are {sorted(_MODE_TYPES)}"
            )

    when_ymd = None
    if when is not None:
        from transitio.catalog._models import as_date

        when_ymd = as_date(when).strftime("%Y%m%d")
    budgets.setdefault("reference_date", when_ymd)

    # Transformed outputs carry a parameter digest so calls for different
    # AOIs or reference dates never overwrite each other's artefacts.
    tag = hashlib.sha256(
        json.dumps(
            {
                "bounds": [round(v, 6) for v in geometry.bounds],
                "reference_date": budgets.get("reference_date"),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()[:16]

    osm_pbf = fetch_pbf(geometry, cache_dir=cache_dir, directory=directory)

    feeds, reports, repairs, skipped = [], [], [], []
    with MobilityDatabase(refresh_token, cache_dir=cache_dir) as db:
        if when is not None and not db._refresh_token:
            warnings.warn(
                "no Mobility Database API token: 'when' cannot select "
                "historical datasets, using the latest hosted datasets",
                UserWarning,
                stacklevel=2,
            )
        candidates = sorted(
            db.search_feeds(aoi=geometry, country_code=country_code), key=_rank
        )
        for feed in candidates:
            dataset = None
            if db._refresh_token:
                try:
                    if when is not None:
                        dataset = db.dataset_for(feed, when)
                        if dataset is None:
                            skipped.append(
                                (feed.id, "no dataset covers the requested day")
                            )
                            continue
                    else:
                        # Prefer a versioned dataset (checksum, hosted
                        # report) over the unversioned moving target.
                        versions = db.datasets(feed)
                        dataset = versions[0] if versions else None
                except Exception as error:  # noqa: B902
                    skipped.append((feed.id, f"dataset selection failed: {error}"))
                    continue
            try:
                if dataset is not None:
                    path = db.download(dataset, directory=directory)
                else:
                    path = db.download_latest(feed, directory=directory)
            except Exception as error:  # noqa: B902
                skipped.append((feed.id, f"download failed: {error}"))
                continue
            hosted = None
            if dataset is not None:
                try:
                    hosted = db.validation_report(dataset)
                except Exception:  # noqa: B902 — the hosted report is optional
                    hosted = None

            try:
                provenance = None
                sidecar = path.with_suffix(".provenance.json")
                if sidecar.exists():
                    provenance = json.loads(sidecar.read_text())

                fixes = []
                if repair:
                    repaired = path.with_name(f"{path.stem}-repaired-{tag}.zip")
                    fixes = repair_feed(path, repaired, **budgets)["fixes"]
                    path = repaired
                if crop:
                    cropped = path.with_name(f"{path.stem}-cropped-{tag}.zip")
                    crop_feed(path, cropped, aoi=geometry, **budgets)
                    path = cropped

                # Modes are read from the delivered feed, after cropping,
                # so an aggregate serving buses only outside the AOI does
                # not pass a bus filter.
                if modes is not None:
                    served = _feed_modes(path)
                    if served is None:
                        skipped.append(
                            (feed.id, "could not read routes.txt for mode filtering")
                        )
                        continue
                    if not served & modes:
                        skipped.append(
                            (feed.id, f"serves {sorted(served)}, not {sorted(modes)}")
                        )
                        continue

                validation = validate_feed(path, **budgets)
                if when_ymd and not _covers(validation["service_window"], when_ymd):
                    window = validation["service_window"]
                    skipped.append(
                        (
                            feed.id,
                            "no service on the requested day (actual window "
                            f"{window[0]}..{window[1]})",
                        )
                    )
                    continue
                report = build_report(validation, hosted=hosted, provenance=provenance)
            except Exception as error:  # noqa: B902 — isolate per-feed failures
                skipped.append((feed.id, f"processing failed: {error}"))
                continue
            reports.append(report)
            repairs.append(fixes)
            feeds.append(path)

    return FetchResult(
        osm_pbf=osm_pbf,
        feeds=feeds,
        reports=reports,
        repairs=repairs,
        skipped=skipped,
    )
