# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Fixed

- Repair and crop now copy through verbatim every archive entry they never
  parsed — ``locations.geojson`` (GTFS-Flex), unknown files and nested
  entries were previously dropped by the rewrite. A duplicated unparsed
  name is copied once, and hostile entries (path traversal, aliases of the
  rewritten tables, symlinks) are excluded from the output.
- Cropping with a one-sided date window clamps calendars and prunes
  calendar_dates exceptions on the bounded side; previously clamping only
  happened when both bounds were given.
- Cropping drops attributions whose only reference is a pruned agency,
  closing a dangling ``agency_id`` foreign key.
- Repair and crop refuse to run when the staging path (``<output>.part``)
  aliases the source archive, which previously deleted the input.
- Cropped OSM extracts of true polygon AOIs now carry a geometry digest in
  their cache filename; polygons sharing a bounding envelope previously
  reused the first cached crop.

### Changed

- Renamed the project from ``beanpicker`` to ``transitio`` ahead of the
  first release: the Python package, the Rust crates, the
  ``TRANSITIO_REQUIRE_TEST_DATA`` test gate, the platform cache directory
  and the ``TransitioError`` exception base all follow the new name.

### Added

- Sphinx documentation site (``docs/``, sphinx-book-theme): landing page,
  installation and quickstart guides, and an autosummary API reference over
  the public surface; ``.readthedocs.yml`` builds it on Read the Docs with
  the compiled package installed.

- Benchmark suite: ``transitio.report.parity_summary`` buckets a merged
  report's notice codes into agreeing, count-disagreeing, local-only and
  canonical-only sets, and ``scripts/benchmark_validator.py`` times
  ``validate_feed`` over a corpus of feed zips and prints the parity
  breakdown against a ``<feed stem>.canonical.json`` canonical-validator
  report when present.

- Handoff helpers on ``FetchResult``: ``to_cafein()`` builds a routable
  ``cafein.TransportNetwork`` from the validated feeds and OSM extract
  (keyword arguments pass through to ``TransportNetwork.from_gtfs``), and
  ``to_pyrosm()`` opens the extract as a ``pyrosm.OSM`` reader.

- One-call pipeline (``transitio.fetch``): resolves the OSM extract for an
  AOI, discovers every overlapping GTFS feed (ordered by the documented
  preference: official, active, most spatially specific), selects the
  dataset version covering a requested service day (or the latest
  versioned dataset) when a token is available, downloads with checksum
  verification (token mode; the tokenless fallback fetches the latest
  hosted zips unverified), optionally repairs, crops each feed to the AOI
  by default, filters by coarse transport modes read from the delivered
  feed's ``routes.txt``, validates and verifies the service window, and
  returns
  the paths with per-feed merged reports, repair logs and skip reasons in
  a ``FetchResult``. Per-feed failures are recorded as skips, never
  aborting the remaining feeds.

- Feed cropping (``transitio.crop_feed``): spatial cropping to an AOI
  bounding box (trips serving the area with full stop sequences, or
  strictly inside with ``full_trips_only``) and temporal cropping to a
  service-date window, cascading stops, routes, shapes, calendars,
  frequencies, transfers, pathways, fares and agencies to a referentially
  consistent feed; retained trips keep their times and attributes
  untouched. Same fail-closed budget, symlink and atomic-write behavior
  as repair.

- Feed repair (``transitio.repair_feed``) under the gtfstidy contract:
  fixable optional fields reset to spec defaults, dangling optional
  references cleared in place, entities with unfixable errors dropped with
  cascading removals to referential consistency, the repaired feed
  rewritten as a fresh zip, and every action logged as a structured fix
  record naming its trigger. Calling ``repair_feed`` is the opt-in;
  validation never modifies feeds.

- Real-feed integration harness: ``scripts/fetch_test_data.py`` downloads
  the r5py Helsinki sample data (GTFS + OSM extract, pinned by release tag
  and SHA-256, resume-capable) into the gitignored ``tests/data/``;
  session fixtures gate on ``TRANSITIO_REQUIRE_TEST_DATA``; integration
  tests validate the production Helsinki feed end-to-end and render its
  merged report. CI caches and fetches the datasets.

- Report module (``transitio.report``): ``build_report`` groups local
  notices by code in the canonical grouped convention and merges them with
  a hosted canonical-validator report (per-code ``source`` local/hosted/
  both), embeds the provenance block, computed service window and row
  counts; ``render_markdown`` and ``render_html`` produce human-readable
  renderings.

- Semantic rule tier: stop-time progression and trip usability (including
  arrival/departure ordering, trip edges and travelled-distance
  monotonicity), calendar activity with ``expired_calendar`` against a
  configurable reference date, block-overlap detection with true
  service-day intersection, frequency-window overlaps, and shape distance,
  usage and single-point checks — all codes and severities verified against
  the canonical validator source. ``validate_feed`` reports the computed
  ``service_window`` so catalog-published dataset ranges can be verified
  against actual calendars.

- Field-format and referential-integrity rule tier: typed per-column
  validation (dates, GTFS over-midnight times, integers/floats with ranges,
  enumerations, IANA timezones, coordinates with near-origin/near-pole
  sanity), required and conditionally required fields
  (``stop_without_location``, ``route_both_short_and_long_name_missing``,
  agency_id with multiple agencies), calendar/frequency range order, agency
  timezone consistency, parent-station location-type relations, unknown
  columns, and cross-table ``foreign_key_violation`` checks — all under
  canonical notice codes, with the same per-file severity-aware notice
  sampling as the structural tier.
- Rust GTFS core foundation: the ``transitio-gtfs`` crate parses a feed zip
  into raw tables while collecting notices (never failing hard on data
  defects), covering the structural rule tier — file presence including the
  calendar pair, column shape, row shape, primary-key uniqueness, nested,
  duplicated and unknown files — with notice codes and severities following
  the canonical gtfs-validator naming, configurable decompression, row and
  column budgets enforced while reading (hostile-archive defense; per-file
  violations reported as notices, not aborts), duplicate archive entries
  detected via a direct central-directory walk, and the GIL released for
  the whole scan.
  ``transitio.validate_feed(path)`` exposes the flat notice report; the
  canonical grouped report rendering lands with the report module.

- Repository scaffold: maturin build with a stub ``transitio._core`` Rust
  crate, CI for lint/tests and release wheels.
- ``transitio.exceptions`` module with ``TransitioError``,
  ``MissingTokenError``, ``DownloadError`` and ``ExtractNotFoundError``.
- No-token fallback for the catalog: without a refresh token,
  ``search_feeds`` now searches the Mobility Database CSV catalogue export
  (with a ``UserWarning``) instead of failing; ``Feed`` carries
  ``latest_dataset_url`` and ``download_latest`` fetches the hosted latest
  dataset zip in both modes.
- OSM module (``transitio.fetch_pbf``): AOI-driven extract acquisition on
  top of pyrosm — smallest-covering-extract resolution from pyrosm's bundled
  Geofabrik index, cached download, polygon-true cropping via
  ``pyrosm.OSM(...).to_pbf``, place-name AOIs via Nominatim geocoding, and a
  provenance sidecar per file.
- Mobility Database catalog client (``transitio.MobilityDatabase``):
  token-refresh authentication, feed search by AOI bounding box, country,
  subdivision and municipality, historical dataset listing with
  date-coverage selection, cached checksum-verified dataset download with a
  provenance sidecar, and hosted validation-report retrieval.
