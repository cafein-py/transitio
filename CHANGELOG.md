# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- ``OsmEditor`` (``transitio.edit.OsmEditor``): edit the routable network
  of a local OSM extract and write it back to a re-readable
  ``*.osm.pbf``. Loads nodes and whole ways with pyrosm (now ``>=0.12.0``),
  exposes them as GeoDataFrames, and edits them in the OSM data model —
  coordinates on nodes, a way as an ordered member-node list — via
  ``move_node``, ``add_node``, ``delete_node``, ``add_way`` (referencing
  existing and/or new nodes), ``reshape_way``, ``delete_way`` and the
  ``retag_*`` helpers. ``save`` writes a network-only file by default
  (``subset_only``) so editing a shared node cannot deform a feature that
  was not loaded.

### Changed

- ``pyrosm`` requirement raised to ``>=0.12.0`` for its geometry-editing
  ``write_pbf``.

## 0.3.0 — 2026-07-21

### Added

- Custom-filter snapping: ``snap_to_network`` and ``build_feed``
  (``snap_custom_filter=``) now accept a pyrosm Overpass-style tag
  filter selecting which OSM ways form the routing network — e.g.
  ``custom_filter={"railway": ["tram"]}`` to snap alignments to tram
  rails, or ``{"railway": ["rail", "light_rail"]}`` for heavy rail —
  instead of only the fixed ``network_type``. When given, the network
  is restricted to exactly the matching ways.

## 0.2.0 — 2026-07-21

### Added

- The map-based feed editor, as a companion package:
  `transitio-editor <https://github.com/cafein-py/transitio-editor>`_
  serves a local MapLibre GUI over the editing API below, and
  ``transitio edit feed.zip`` delegates to it when installed (with a
  clear error otherwise). The core library carries no GUI code (the
  interim ``transitio.gui`` module and ``[gui]`` extra existed only on
  the development branch and never shipped in a release).

- Scenario feeds from geodata (``transitio.build_feed``): reads route
  alignments from a GeoPackage/Shapefile or GeoDataFrame under a small
  attribute convention (mode, ``headway_min`` or per-period
  ``headway_<name>`` columns, ``speed_kmh``/``duration_min``, operating
  window, service days, ``bidirectional``) and writes a validated
  frequency-based GTFS feed — geometries become shapes with metric
  distances, stops come from an optional point layer snapped to each
  route or are interpolated at a spacing, and trips are generated per
  direction and period. Projected inputs are reprojected to WGS84.

- Feed editing and building (``transitio.FeedBuilder`` /
  ``transitio.FeedEditor``): build a GTFS feed entity by entity
  (agencies, stops, routes, calendars, scheduled and frequency-based
  trips) or load an existing feed into pandas tables, mutate it
  (``update_stop``, ``set_headway``, ``shift_trip``, ``drop_route``,
  or direct DataFrame access), view stops as a WGS84 GeoDataFrame, and
  save atomically with transitio's validator (canonical notice codes,
  routing-oriented rule subset) run on every save —
  error-severity notices raise ``InvalidFeedError`` (carrying the
  report) unless ``check=False``. Unparsed archive entries survive the
  round trip. Shapes are first-class: ``add_shape`` writes polylines
  with cumulative metric ``shape_dist_traveled`` (cafein's travel
  distances build on them), trips reference them via ``shape_id=``, the
  ``shapes`` view returns per-shape LineStrings, and
  ``transitio.edit.snap_to_network`` routes a waypoint sequence along
  the pyrosm-loaded OSM street network (``transitio[snap]`` extra) —
  the primitive behind snapped route drawing for bus and tram
  alignments.

## 0.1.0 — 2026-07-20

The first release. Developed pre-release under the working name
``beanpicker``.

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
