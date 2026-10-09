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
   FetchResult.paths
   FetchResult.selection_table
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
   area
   Area
   Area.feeds
   Area.recommend
   AreaPart
   Place
   Place.feeds
   Place.recommend
   Place.delineations
   Place.subtype
   Place.parent
   Place.children
   Place.ancestors
   Place.metros
   Place.members
   Place.service
   Place.validity
   Place.centre
   Place.population
   Place.wikidata_id
   Place.concordances
   Place.former_ids
   IndexedFeed
   IndexedFeed.service
   IndexedFeed.service_start
   IndexedFeed.service_end
   IndexedFeed.relevance
   IndexedFeed.relevance_category
   IndexedFeed.share_of_place
   IndexedFeed.stale_when_indexed
   IndexedFeed.overlap
   IndexedFeed.catalogue_name
   IndexedFeed.selector
   IndexedFeed.coverage
   IndexedFeed.files
   IndexedFeed.has_shapes
   IndexedFeed.has_fares
   IndexedFeed.license
   IndexedFeed.redistribution_allowed
   IndexedFeed.provenance
   IndexedFeed.snapshot
   IndexedFeed.access_instructions
   AccessProvider
   Recommendation
   Delineation
   Suggestion
   Selector
   Index
   Index.partitions
   Index.feeds_in
   Index.realtime_in
   Index.realtime_unlinked
   Index.discovery_semantics_version
   Index.access_provider

A query's feeds come as a ``FeedList``, a list with tabular exports.

.. currentmodule:: transitio.index.feeds

.. autosummary::
   :toctree: api/

   FeedList.to_dataframe
   FeedList.to_geodataframe

Feed credentials
----------------

The credentials of feeds that need an account with their provider: one
environment variable per field, or a private file in the user config
directory.

.. currentmodule:: transitio.credentials

.. autosummary::
   :toctree: api/

   set
   get
   clear
   configured

.. currentmodule:: transitio

The download cache
------------------

Every downloaded feed is kept as a version in the cache and reused while it
serves the request (see :func:`~transitio.fetch`); these list and remove what
the cache holds.

.. currentmodule:: transitio.cache

.. autosummary::
   :toctree: api/

   info
   clear

The feed catalogs
-----------------

.. currentmodule:: transitio

.. autosummary::
   :toctree: api/

   MobilityDatabase
   MobilityDatabase.search_feeds
   MobilityDatabase.feed
   MobilityDatabase.datasets
   MobilityDatabase.dataset_for
   MobilityDatabase.datasets_for
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
   FeedBuilder.changes
   FeedBuilder.undo
   FeedBuilder.redo
   FeedEditor
   FeedEditor.update_stop
   FeedEditor.update_route
   FeedEditor.set_headway
   FeedEditor.shift_trip
   FeedEditor.drop_route
   FeedEditor.drop_routes
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
