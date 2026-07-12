# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- Repository scaffold: maturin build with a stub ``beanpicker._core`` Rust
  crate, CI for lint/tests and release wheels.
- ``beanpicker.exceptions`` module with ``BeanpickerError``,
  ``MissingTokenError``, ``DownloadError`` and ``ExtractNotFoundError``.
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
