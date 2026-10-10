transitio: Transit feeds in and out, ready for routing
======================================================

**Find the right public transport feeds for any city, validated and ready for
routing or editing.**

``transitio`` is a Python library that helps you to find public transport
feeds serving a given city and prepares them for routing. ``transitio``'s feed
index catalogues the world's public transport feeds in GTFS format and the
places they serve (currently listing approximately 150,000 places). The
General Transit Feed Specification (GTFS) is the standard format for public
transport timetables, used by thousands of transport authorities across the
world. With ``transitio``, you can get recommendations on which feeds to use
for a given city (and time) and an explanation of why certain feeds should be
left out. ``transitio`` then helps you to download the recommended feeds, crop
them to the given area and validate them to avoid using broken or defective
feeds. With ``transitio``, you can also download `OpenStreetMap
<https://www.openstreetmap.org/>`__ data for the same area (using `pyrosm
<https://pyrosm.readthedocs.io/>`__ under the hood) if you want to do
multimodal routing that combines public transport and walking.

.. code-block:: python

    import transitio

    transitio.index.refresh()             # once: install the feed index

    turku = transitio.place("Turku")
    rec = turku.recommend()               # which feeds to use, and why
    result = transitio.fetch(feeds=rec)   # download, crop and validate

    result.paths                          # the feed files
    result.osm_pbf                        # the OpenStreetMap extract
    network = result.to_cafein()          # a network to route on (needs cafein)

Start with :doc:`installation` and the :doc:`quickstart`.

What can you do with transitio?
-------------------------------

- **Find places and their feeds.** Look up a city, metro area, region or
  country by name and list the feeds that serve it, from local to
  international. See :doc:`finding_places`.
- **Choose the feeds to use.** ``recommend()`` considers the feeds that serve
  a place, selects which to use and explains why it leaves the others out.
  See :doc:`choosing_feeds`.
- **Fetch the data.** Download the feeds for a place, a box or any polygon
  and crop them to that area. Also download an OpenStreetMap extract of the
  area. See :doc:`fetching_data`.
- **Reuse downloads.** Every feed that ``fetch`` downloads is kept in a
  cache. This lets the same analysis use the same feeds again, even offline.
  See :doc:`download_cache`.
- **Check, repair and edit feeds.** Validate any GTFS feed with the notice
  codes of the canonical GTFS validator. Repair defects that can be fixed
  without changing the trips riders see. Edit a feed with undo and redo. See
  :doc:`working_with_feeds`.
- **Crop and merge feeds.** Cut a feed to an area or a date range, merge
  several feeds into one, or replace a feed's broken trips with those of
  another. See :doc:`cropping_and_merging`.
- **Build scenario feeds.** Turn routes drawn in a GIS tool and their
  headways (how often they run) into a GTFS feed. See :doc:`building_feeds`.
- **Search the catalogues.** Query the Mobility Database and download
  OpenStreetMap extracts directly. See :doc:`catalogues`.
- **Draw missing route shapes.** ``infer_shapes`` draws the shapes a feed
  lacks from OpenStreetMap. See :doc:`reference`.

License
-------

``transitio`` is licensed under the MIT license. The feeds and OpenStreetMap data
it downloads keep their own licenses. OpenStreetMap data is available under
the `Open Database License <https://www.openstreetmap.org/copyright>`__. See
:doc:`attribution`.

.. toctree::
    :caption: Getting started
    :maxdepth: 1
    :hidden:

    installation
    quickstart.ipynb

.. toctree::
    :caption: User guide
    :maxdepth: 1
    :hidden:

    finding_places.ipynb
    choosing_feeds.ipynb
    fetching_data.ipynb
    download_cache.ipynb
    working_with_feeds.ipynb
    cropping_and_merging.ipynb
    building_feeds.ipynb
    catalogues.ipynb

.. toctree::
    :caption: API reference
    :maxdepth: 1
    :hidden:

    reference

.. toctree::
    :caption: About
    :maxdepth: 1
    :hidden:

    attribution
