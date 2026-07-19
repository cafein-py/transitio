# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- Real-feed integration harness: ``scripts/fetch_test_data.py`` downloads
  the r5py Helsinki sample data (GTFS + OSM extract, pinned by release tag
  and SHA-256, resume-capable) into the gitignored ``tests/data/``;
  session fixtures gate on ``BEANPICKER_REQUIRE_TEST_DATA``; integration
  tests validate the production Helsinki feed end-to-end and render its
  merged report. CI caches and fetches the datasets.

- Report module (``beanpicker.report``): ``build_report`` groups local
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
- Rust GTFS core foundation: the ``beanpicker-gtfs`` crate parses a feed zip
  into raw tables while collecting notices (never failing hard on data
  defects), covering the structural rule tier — file presence including the
  calendar pair, column shape, row shape, primary-key uniqueness, nested,
  duplicated and unknown files — with notice codes and severities following
  the canonical gtfs-validator naming, configurable decompression, row and
  column budgets enforced while reading (hostile-archive defense; per-file
  violations reported as notices, not aborts), duplicate archive entries
  detected via a direct central-directory walk, and the GIL released for
  the whole scan.
  ``beanpicker.validate_feed(path)`` exposes the flat notice report; the
  canonical grouped report rendering lands with the report module.

- Repository scaffold: maturin build with a stub ``beanpicker._core`` Rust
  crate, CI for lint/tests and release wheels.
- ``beanpicker.exceptions`` module with ``BeanpickerError``,
  ``MissingTokenError``, ``DownloadError`` and ``ExtractNotFoundError``.
- No-token fallback for the catalog: without a refresh token,
  ``search_feeds`` now searches the Mobility Database CSV catalogue export
  (with a ``UserWarning``) instead of failing; ``Feed`` carries
  ``latest_dataset_url`` and ``download_latest`` fetches the hosted latest
  dataset zip in both modes.
- OSM module (``beanpicker.fetch_pbf``): AOI-driven extract acquisition on
  top of pyrosm — smallest-covering-extract resolution from pyrosm's bundled
  Geofabrik index, cached download, polygon-true cropping via
  ``pyrosm.OSM(...).to_pbf``, place-name AOIs via Nominatim geocoding, and a
  provenance sidecar per file.
- Mobility Database catalog client (``beanpicker.MobilityDatabase``):
  token-refresh authentication, feed search by AOI bounding box, country,
  subdivision and municipality, historical dataset listing with
  date-coverage selection, cached checksum-verified dataset download with a
  provenance sidecar, and hosted validation-report retrieval.
