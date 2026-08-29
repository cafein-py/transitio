# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- `transitio.index.read_index` — the read layer over a published feed index,
  loading a `feeds.parquet` and its `snapshot.json` manifest. Feeds only for
  now; a schema version it does not understand is refused with
  `IncompatibleIndexError`.

### Changed

- `pyarrow` is now a required runtime dependency; it backs the Parquet feed
  index.

## 0.10.0 — 2026-08-08

### Added

- **`transitio.infer_shapes`** — write a feed's missing `shapes.txt`
  from an OSM extract. Two strategies per distinct stop pattern,
  best first: a matched OSM `type=route` relation (the operator's own
  alignment, stitched from its member ways and cut to the span the
  pattern serves) and, failing that, map matching over a mode graph —
  tram/light-rail/subway/rail tracks, or a bus-drivable street network
  resolved per way through the full PSV access hierarchy
  (`access → vehicle → motor_vehicle → psv → bus`), with the graph
  split at every barrier node whose bus access is not an explicit
  allow. Matching is deterministic: mode compatibility (never
  relaxed), route-ref agreement, corridor containment measured as the
  pattern's stops covered by the relation, an operator/network filter,
  then an approximate-subsequence stop-sequence distance that scores a
  short working on the stops it actually serves. Every candidate
  alignment is validated against the pattern's own stops — each within
  snap tolerance, positions monotone along the line, total length
  plausible — before anything is written, and the returned report names
  the method, matched relation and score behind every shape and the
  stage behind every refusal.
- **Strictness levels** (`strictness="strict" | "relaxed" |
  "permissive"`, or a `Level`): how much uncertainty the caller will
  accept. The levels move the judgement thresholds only — the mode
  filter, one-way and ring-direction legality, and barrier access never
  relax, because those produce alignments that are impossible rather
  than merely uncertain. Measured on the Helsinki tram fixture with the
  feed's own shapes withheld (`scripts/validate_shapes.py`): strict
  writes 35 of 80 patterns, relaxed 40, permissive 43, all at ~0.9%
  median length error with no shape in a different corridor.
- Feeds whose `trips.shape_id` points at a missing or empty
  `shapes.txt` are treated as shapeless rather than shaped, so a feed
  that lost its shapes is repaired instead of passed through. A pattern
  whose trips mix published and missing shapes has only the shapeless
  ones assigned; an operator-published shape is never overwritten.

- The written feed is **certified** against the input: any
  error-severity notice inference introduced raises
  `transitio.exceptions.ShapeInferenceError` (the file is still
  written, like `InvalidFeedError`), notices compare by full identity
  with multiplicity, and a sampled or truncated validation refuses
  rather than certifying on partial evidence. `check=False` records the
  outcome without raising.

- A **`<output>.provenance.json` sidecar** records the run beside the
  feed, because GTFS itself cannot say that a shape was inferred rather
  than published. Every inferred shape carries its method, matched
  relation, score, the effective strictness thresholds and the OSM
  extract digest; a prior run's sidecar is inherited per shape — bound
  to the input feed's checksum — so a twice-inferred feed never passes
  as operator-published.

- Mode coverage follows GTFS's extended route types, so feeds using the
  Hierarchical Vehicle Type ranges are handled: coach (200s) and
  taxi-bus (1500s) with bus, suburban rail (300s) with train,
  urban/metro/underground (400s–600s) with subway, and ferry (1200s)
  with water transport.

- Circular patterns are supported: a route whose last stop returns to
  its first is a completed loop rather than a monotonicity failure,
  recognised only when the alignment itself closes.


## 0.9.0 — 2026-08-06

### Added

- ``validate_feed`` exposes the date-targeted MEASUREMENTS the moment
  checks already computed internally: a ``moment`` block (with an
  explicit ``reference_date``) carrying ``activeTrips``,
  ``activeRoutes``, ``stopsServed``, the feed's own ``baselineTrips``
  mean and ``windowDays`` — judgement stays with the notices — plus an
  ``incomplete`` list naming truncated/unreadable files (row counts for
  them are lower bounds) and ``stop_bounds``, the feed's stop bounding
  box from the already-budgeted scan (``None`` when stops.txt is
  incomplete: partial bounds would mislead). Groundwork for
  ``compare_feeds``.

- ``transitio.compare_feeds``: rank candidate GTFS feeds for a
  user-specified date (and optional time of day). Each candidate is
  validated with the date as the target moment and tabulated —
  activity at the moment, ERROR/WARNING counts, cafein-readiness
  verdicts, transfer counts, service-window margin, stop-bounds
  agreement with the other candidates — then ranked by a documented
  deterministic scoring tuple in which unusable-at-the-target and
  unreliable-counts (sampling or truncation) always dominate, so
  incomplete evidence can never flatter a candidate. The winner is a
  recommendation: the full metric table, every score component, the
  caveats (e.g. poor area overlap) and the applied thresholds are all
  in the result, and ``render_comparison_markdown`` /
  ``render_comparison_html`` produce shareable pages.

- ``transitio.compare_feed_history`` and
  ``MobilityDatabase.datasets_for``: enumerate every Mobility Database
  dataset version whose published service range covers a target day
  (published ranges are optimistic — the comparison then verifies
  reality against the computed calendars), download each version with
  the existing checksum and provenance machinery, and rank them with
  ``compare_feeds``, dataset ids as labels and per-candidate dataset
  provenance attached. Catalog history requires an API token; zero or
  one covering version raises with the concrete situation named.

- ``transitio.patch_feed``: heal a feed by replacing the trips its own
  ERROR notices implicate with matched counterparts from a sibling
  (donor) feed of the same area and period. Matching never trusts
  cross-feed ids: agencies pair by name, routes by type and name within
  the paired agency, trips by first-departure proximity (60 s) and a
  stop-sequence similarity of at least 0.8 over name-and-proximity stop
  identity. Replacements import the donor subgraph under a
  collision-free id prefix with full referential closure (stops with
  parent stations and levels, routes with agencies and networks,
  frequencies; donor fares and translations stay out), dependent base
  rows that referenced a replaced trip are dropped and logged, and the
  output is revalidated. Every action lands in a patch report with
  donor provenance (checksum-verified sidecar) and the applied
  thresholds; ``semantic_equivalence`` is explicitly ``false`` — the
  donor timetable may genuinely differ. Because the base feed is by
  definition broken, matching reads it strictly rather than leniently:
  a trip whose stop_times cannot be ordered with confidence, or whose
  first stop has no valid departure, is unmatchable instead of being
  matched on the rows that happen to parse. Matching work is bounded
  per candidate and per call; a candidate left unscored by that bound
  is logged as a resource caveat and never resolved into a match, so a
  donor is never chosen from partial evidence. Sampled or truncated
  validation at any stage raises ``PatchError`` (new exception)
  regardless of ``check``; with ``check=True`` remaining ERRORs raise
  too, with the report attached and the file written for inspection.

## 0.8.0 — 2026-08-05

### Added

- Cafein-readiness (distances): ``validate_feed`` now returns a
  ``readiness`` block predicting, per trip, which tier of cafein 0.10.0's
  distance ladder will accept the feed's data — validated
  ``shape_dist_traveled`` (non-NaN, non-decreasing, detour ratio within
  cafein's meter or kilometer bands against stop-to-stop great-circle
  distances), stops linear-referenced onto the shape (a real UTM
  projection with the tier decided only when every candidate zone
  agrees), or the crow-fly fallback — with a
  ``full``/``partial``/``straight_line`` verdict, and ``null`` for the
  whole section when truncated input would make it guesswork. Five
  advisory transitio-specific notices accompany it:
  ``shape_dist_ratio_implausible`` (WARNING),
  ``shape_dist_in_kilometers`` (INFO), ``chord_only_shape`` (WARNING,
  shapes with no more vertices than the trip has stops),
  ``stop_far_from_shape`` (WARNING, a stop beyond cafein's 100 m snap
  tolerance) and ``trips_without_shapes`` (INFO, aggregate). The report
  renderers show the summary as a one-line readiness block.

- Cafein-readiness (fares): the ``readiness`` block gains a ``fares``
  section predicting whether cafein can price journeys. GTFS v1 fares
  are counted as priceable when the price parses to a finite
  non-negative number and the currency is a three-letter code, and a
  coarse route-compatibility share is computed under cafein's own
  fare-rule grant model (contains rows contribute their zone alone,
  origin/destination rows form clauses with exactly their present
  fields, route-only rows grant the route, a fare with no grants is
  unrestricted, and agency scope bounds every grant). The verdict is
  ``computable``/``partial``/``absent``/``blocked`` — ``blocked`` when
  a multi-agency feed carries a fare without ``agency_id``, which
  cafein rejects outright; Fares v2 presence is reported separately
  since cafein does not read v2. Transfer pricing is reported present
  when a priceable fare carries an explicit ``transfers`` value or a
  ``transfer_duration``. Four advisory notices:
  ``no_fare_information`` (INFO), ``fare_attribute_not_priceable``
  (WARNING), ``partial_fare_coverage`` (WARNING, below a 20 %
  route-compatibility share) and ``fare_without_agency_id`` (WARNING).

### Fixed

- The date-time-targeted checks' over-midnight lookback is no longer
  clamped at seven days: a trip legally completing more than a week
  after its service day (GTFS times allow 3-digit hours) is now
  attributed to the target moment instead of producing a false
  ``no_trips_at_reference_time``.
- ``reference_time`` now rejects single-digit hours (``8:00``),
  matching its documented ``HH:MM``/``HH:MM:SS`` format.
- Frequency rows with a reversed or empty window or a non-positive
  ``headway_secs`` no longer count as service in the date-time-targeted
  checks, and a trip whose frequency rows are all unusable is excluded
  from the timed checks entirely rather than silently falling back to
  its stop-time span.

## 0.7.0 — 2026-08-05

### Added

- Date-time-targeted validation: passing ``reference_date`` to
  ``validate_feed`` (and ``repair_feed`` / ``crop_feed``) now also checks
  that the feed is in working order on that day, and the new
  ``reference_time`` keyword (``HH:MM`` or ``HH:MM:SS``) narrows the
  check to a moment. Four transitio-specific WARNING notices:
  ``no_service_on_reference_date`` (nothing runs on the day),
  ``no_trips_at_reference_time`` (services active but no trip operating
  at the time — frequency-window departures and over-midnight trips from
  the previous service day are accounted for),
  ``service_level_below_baseline`` (the moment's active-trip count falls
  under half of the feed's own per-day or per-clock-time average, the
  threshold explicit in the notice), and
  ``route_inactive_on_reference_date`` (a route active on at least half
  of the service-window days has no service on the target day).

- An invertible change log with undo/redo on ``FeedBuilder`` /
  ``FeedEditor``: every helper mutation is recorded (grouped so one
  helper call is one undo step), three public logged primitives —
  ``set_value``, ``insert_rows``, ``delete_rows`` — plus a public
  ``action(label)`` context group multi-step operations, and ``undo()``
  / ``redo()`` revert or replay whole actions atomically, verifying the
  tables still match the log first (``ChangeLogDesyncError`` otherwise;
  direct edits through ``tables`` remain outside the log). ``save``
  writes the applied history to ``<name>.changes.txt`` beside the feed —
  a plain CSV whose final ``meta`` row carries the source and result
  checksums — and removes a stale sidecar when the log is empty or
  ``change_log=False``.

## 0.6.0 — 2026-08-04

### Changed

- ``crop_feed`` (and ``fetch``, which forwards its ``aoi``) crops to a
  polygon itself rather than to its bounding box: a Polygon or
  MultiPolygon — a shapely geometry, a GeoDataFrame/GeoSeries of them, or
  a GeoJSON-style mapping — now selects the stops inside the area, holes
  excluded, with points on a boundary counted as inside. Bounding-box
  tuples and non-polygon geometries are unaffected. Callers that already
  passed a polygon get a tighter crop than before, which is what the
  argument always described; the previous behaviour was documented as a
  limitation.

## 0.5.0 — 2026-08-03

### Added

- ``merge_feeds`` / ``merge_tables`` (``transitio.gtfs``): merge several
  GTFS feeds into one referentially consistent feed. Every id (and every
  standard reference to it, including Fares v2, networks, areas and
  location-group tables) is namespaced with a per-feed prefix so ids from
  different feeds never collide; single-agency feeds with blank agency
  ids get them backfilled; mixed ``routes.network_id`` /
  ``route_networks.txt`` representations are normalised to the latter.
  ``feed_info.txt`` and ``translations.txt`` are dropped and reported;
  GTFS-Flex feeds and conflicting agency timezones or fare defaults are
  refused. ``merge_feeds`` writes the zip atomically, validates it, and
  returns the validation report with a ``dropped_files`` key.

### Fixed

- Area-filtered catalogue searches in the CSV fallback now rank results by
  the share of each feed's bounding box inside the searched area (with the
  result limit applied after ranking), so local feeds are no longer
  outranked or crowded out by continental aggregates whose bounding
  rectangles merely sweep over the area.

## 0.4.0 — 2026-07-22

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
- ``OsmEditor.snap``: route a waypoint sequence along the *current edited*
  network — the edited network is materialized to a temporary PBF (reused
  until the next edit) and routed with ``snap_to_network``, so a shape
  follows edits and new ways. Defaults to the loaded network; a
  ``custom_filter`` narrows within it (e.g. to tram rails).

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
