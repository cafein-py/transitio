.. _reference:

API reference
=============

:func:`~transitio.fetch` is the main entry point: it runs the whole
acquisition pipeline for an area of interest or an indexed place and returns
a :class:`~transitio.FetchResult`. Every stage is also available on its own —
the feed index, the catalog clients, the OSM fetcher, and the
validate/repair/crop functions.

The pipeline
------------

.. currentmodule:: transitio

.. autosummary::
   :toctree: api/

   fetch
   FetchResult
   FetchResult.to_cafein
   FetchResult.to_pyrosm

The feed index
--------------

The index lists the feeds serving each place, by tier. ``place``, ``places``,
``suggest``, ``Place`` and ``IndexedFeed`` are also importable from
``transitio`` itself.

.. currentmodule:: transitio.index

.. autosummary::
   :toctree: api/

   refresh
   installed
   use
   place
   places
   suggest
   prepare_suggestions
   read_index
   load
   links
   Place
   Place.feeds
   Place.delineations
   Place.subtype
   Place.parent
   Place.children
   Place.ancestors
   Place.metros
   Place.members
   Place.service
   Place.validity
   Place.wikidata_id
   Place.concordances
   Place.former_ids
   IndexedFeed
   IndexedFeed.service
   IndexedFeed.service_start
   IndexedFeed.service_end
   IndexedFeed.relevance
   IndexedFeed.relevance_category
   IndexedFeed.selector
   IndexedFeed.coverage
   IndexedFeed.files
   IndexedFeed.has_shapes
   IndexedFeed.has_fares
   IndexedFeed.license
   IndexedFeed.redistribution_allowed
   IndexedFeed.provenance
   IndexedFeed.snapshot
   Delineation
   Suggestion
   Selector
   Index
   Index.partitions
   Index.feeds_in
   Index.realtime_in
   Index.realtime_unlinked
   Index.discovery_semantics_version

.. currentmodule:: transitio

The feed catalogs
-----------------

.. autosummary::
   :toctree: api/

   MobilityDatabase
   MobilityDatabase.search_feeds
   MobilityDatabase.feed
   MobilityDatabase.datasets
   MobilityDatabase.dataset_for
   MobilityDatabase.download
   MobilityDatabase.download_latest
   MobilityDatabase.validation_report
   MobilityDatabase.close
   Feed
   Dataset
   TransitlandAtlas
   TransitlandAtlas.download
   TransitlandAtlas.close
   AtlasFeed

OSM extracts and route shapes
-----------------------------

.. autosummary::
   :toctree: api/

   fetch_pbf
   infer_shapes

Editing and building feeds
--------------------------

.. autosummary::
   :toctree: api/

   FeedBuilder
   FeedBuilder.add_agency
   FeedBuilder.add_stop
   FeedBuilder.add_route
   FeedBuilder.add_service
   FeedBuilder.add_shape
   FeedBuilder.add_trip
   FeedBuilder.add_frequency_trip
   FeedBuilder.stops
   FeedBuilder.set_stops
   FeedBuilder.shapes
   FeedBuilder.save
   FeedEditor
   FeedEditor.update_stop
   FeedEditor.update_route
   FeedEditor.set_headway
   FeedEditor.shift_trip
   FeedEditor.drop_route
   OsmEditor
   OsmEditor.nodes
   OsmEditor.ways
   OsmEditor.add_node
   OsmEditor.move_node
   OsmEditor.retag_node
   OsmEditor.delete_node
   OsmEditor.add_way
   OsmEditor.reshape_way
   OsmEditor.retag_way
   OsmEditor.delete_way
   OsmEditor.snap
   OsmEditor.save

.. currentmodule:: transitio.edit

.. autosummary::
   :toctree: api/

   build_feed
   snap_to_network


.. currentmodule:: transitio

Validation, repair and cropping
-------------------------------

.. autosummary::
   :toctree: api/

   validate_feed
   repair_feed
   crop_feed
   patch_feed

Merging and comparing feeds
---------------------------

.. autosummary::
   :toctree: api/

   merge_feeds
   merge_tables
   compare_feeds
   compare_feed_history

Reporting
---------

.. currentmodule:: transitio.report

.. autosummary::
   :toctree: api/

   build_report
   parity_summary
   render_markdown
   render_html

Exceptions
----------

.. currentmodule:: transitio.exceptions

.. autosummary::
   :toctree: api/

   TransitioError
   InvalidFeedError
   PatchError
   MissingTokenError
   DownloadError
   ExtractNotFoundError
   IncompatibleIndexError
   PlaceNotFoundError
   AmbiguousPlaceError
   StaleSelectorError
   ShapeInferenceError
   ChangeLogDesyncError
