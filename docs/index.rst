transitio: Transit feeds in and out
===================================

**Find the right public transport feeds for any city, validated and ready for
editing.**

Give transitio a city, and it searches its feed index for the GTFS feeds that
serve that city. The index is a catalogue of the world's public transport
feeds and the places they serve. transitio recommends which feeds to use and
explains why it leaves out the others. It then downloads those feeds, crops
them to the city and validates them. It also downloads the `OpenStreetMap
<https://www.openstreetmap.org/>`__ data used for routing. The feeds and the
extract can be passed directly to `cafein
<https://github.com/cafein-py/cafein>`__ for routing. The extract can also be
opened in `pyrosm <https://pyrosm.readthedocs.io/>`__ for the street network.

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

What transitio does
-------------------

- **Finds places and their feeds.** Look up a city, metro area, region or
  country by name and list the feeds that serve it, from local to
  international. See :doc:`finding_places`.
- **Chooses the feeds to use.** ``recommend()`` considers the feeds that serve
  a place, selects which to use and explains why it leaves the others out.
  See :doc:`choosing_feeds`.
- **Fetches the data.** Download the feeds for a place, a box or any polygon
  and crop them to that area. Also download an OpenStreetMap extract of the
  area. See :doc:`fetching_data`.
- **Reuses its downloads.** Every feed that ``fetch`` downloads is kept in a
  cache. This lets the same analysis use the same feeds again, even offline. See
  :doc:`download_cache`.
- **Checks, repairs and edits feeds.** Validate any GTFS feed with the notice
  codes of the canonical GTFS validator. Repair defects that can be fixed
  without changing the trips riders see. Edit a feed with undo and redo. See
  :doc:`working_with_feeds`.
- **Crops and merges feeds.** Cut a feed to an area or a date range, merge
  several feeds into one, or replace a feed's broken trips with those of
  another. See :doc:`cropping_and_merging`.
- **Builds scenario feeds.** Turn routes drawn in a GIS tool and their
  headways (how often they run) into a GTFS feed. See :doc:`building_feeds`.
- **Searches the catalogues.** Query the Mobility Database and download
  OpenStreetMap extracts directly. See :doc:`catalogues`.
- **Draws missing route shapes.** ``infer_shapes`` draws the shapes a feed
  lacks from OpenStreetMap. See :doc:`reference`.

License
-------

transitio is licensed under the MIT license. The feeds and OpenStreetMap data
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
