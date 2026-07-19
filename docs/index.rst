transitio -- AOI-driven OSM and GTFS acquisition
==================================================

**transitio** is a Python library that selects and prepares the raw
ingredients of public-transport routing -- `OpenStreetMap
<https://www.openstreetmap.org/>`__ extracts and `GTFS <https://gtfs.org/>`__
timetables -- for an arbitrary **area of interest**. It is the companion to
`pyrosm <https://pyrosm.readthedocs.io/>`__ and
`cafein <https://github.com/cafein-py/cafein>`__: transitio moves the raw
ingredients of routing from the open data ecosystem to your area of interest,
ready for cafein to brew into routing results.

One call does the whole run:

.. code-block:: python

    import transitio

    result = transitio.fetch("Helsinki")   # geometry, bbox tuple or place name

    result.osm_pbf     # cropped OSM extract (path)
    result.feeds       # downloaded, cropped and validated GTFS feeds (paths)
    result.reports     # per-feed merged validation reports
    result.skipped     # (feed id, reason) for anything left out

    net = result.to_cafein()   # routable cafein.TransportNetwork
    osm = result.to_pyrosm()   # pyrosm.OSM reader over the extract

What can I do with transitio?
------------------------------

- discover the GTFS feeds overlapping an area through the `Mobility Database
  <https://mobilitydatabase.org/>`__ catalog, with or without an API token
- pick the dataset version whose service range covers a given day, and
  download it with checksum verification and a provenance sidecar
- resolve and download the smallest OpenStreetMap extract covering the area
  and crop it to the true AOI geometry (via pyrosm)
- validate GTFS feeds with a fast Rust core emitting the canonical
  `gtfs-validator <https://github.com/MobilityData/gtfs-validator>`__ notice
  codes, merged with the hosted canonical report where one exists
- repair fixable defects under the `gtfstidy
  <https://github.com/patrickbr/gtfstidy>`__ contract -- semantic equivalence
  from the passenger's perspective, every fix logged
- crop feeds spatially to the AOI and temporally to a service window, with
  referential consistency maintained across all tables
- hand the results straight to cafein or pyrosm

Validation runs in two mergeable tiers: the hosted canonical-validator
report (one HTTP request) and the local Rust core (the routing-critical
rule set under canonical notice codes). Every downloaded artefact carries a
provenance sidecar -- feed, dataset, URL, checksum, retrieval timestamp --
so a matrix computed downstream stays citable to the dataset version.

License
-------

transitio is licensed under the MIT license. Timetable and street data are
© their respective providers; all `OpenStreetMap
<https://www.openstreetmap.org>`__ data is licensed under the `Open Database
License <https://www.openstreetmap.org/copyright>`__.

.. toctree::
    :caption: Getting started
    :maxdepth: 1

    installation
    quickstart

.. toctree::
    :caption: API reference
    :maxdepth: 1

    reference

Indices and tables
==================

* :ref:`genindex`
* :ref:`search`
