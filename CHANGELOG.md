# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Fixed

- `fetch` failed on Windows with "No such file or directory": a download's
  path in the default cache was about 262 characters long, past the
  260-character limit Windows sets unless long paths are switched on.
  Downloads are now staged in a short folder, `<cache_dir>/gtfs/.staging/`,
  about 90 characters shorter. A path that still reaches the limit on
  Windows raises an error saying so, with the two remedies: a shorter
  `cache_dir`, or switching on long paths.

## 0.21.2 — 2026-10-08

### Added

- `FeedEditor.drop_routes(route_ids)` removes many routes and everything that
  references them as one undoable action, each table in one pass, with a
  progress bar of the rows removed (`progress=False` turns it off):
  `editor.drop_routes(routes.loc[is_coach, "route_id"])`.

### Fixed

- `FeedEditor.delete_rows`, and so `drop_route` and every edit that removes
  rows, deletes all its rows in one step instead of copying the table once
  per row: 5,000 of 50,000 rows go in 0.02 s instead of 4 s. The change log
  and undo are unchanged.

## 0.21.1 — 2026-10-08

### Changed

- `fetch(feeds=place.recommend(day))` takes the place and the day from the
  recommendation; another `place`, any `aoi` or a `when` on another day
  raises `ValueError`.

### Fixed

- `recommend()` and the feed table no longer let a feed running the same
  lines with fewer departures stand in for all of another feed's
  departures: at each place a feed is credited with at most its own
  departures in each mode. Tallinn's own feed was left out for the
  national feed, which runs its lines with about half the departures.
- `recommend()` takes feeds only from the group the index measured
  together. A merged index measures overlap within each build, so two
  feeds of different builds never name each other; Munich's MVV feed and
  DELFI were both taken, each said to add half the departures. Feeds
  measured apart are now not compared, and the note counts them.

## 0.21.0 — 2026-10-07

### Added

- `Place.recommend(when)` and `Area.recommend(when)` say which of a place's
  feeds to use on a day and why the others are left out, as a
  `Recommendation` (`print()` it, or `to_dataframe()`). Feeds stale when
  indexed, whose indexed timetable does not run on the day or that need a
  paid account are left out first. On an index that records which feeds run
  the same lines, feeds are taken one at a time, each adding the most
  departures the taken ones lack, until they cover 95 % of the place's
  departures (`target=`) and 80 % of each mode, at most four
  (`max_feeds=`); among feeds adding about as much, the open one with the
  fewest stops is taken. An index without that evidence takes the feed with
  the most departures.
- `FeedList.to_dataframe()` lists a place's feeds one row each, with their
  tiers, relevance, share of the place, service, modes, timetable window,
  containment, access and stop count, the share of the place's departures
  each covers and the feeds it repeats, and a reason in words.
  `IndexedFeed` gains `share_of_place`, `stale_when_indexed`, `overlap` and
  `catalogue_name`.
- `fetch(place=..., feeds=...)` fetches only the feeds named, by id or as a
  `Recommendation`, from any relevance category; an id the place does not
  have raises `ValueError` before anything is downloaded.
- `fetch` shows its progress on stderr: a line per feed, a progress bar per
  download, a line per later step and a summary. `progress=False` turns it
  off. tqdm draws the bars; in Jupyter they are widgets when ipywidgets is
  installed, which the new `notebook` extra
  (`pip install "transitio[notebook]"`) adds.
- `transitio.index.area()` lists the feed-index places that cover an area,
  as whole or partly covered parts with the shares they cover, and the share
  of the area's land they cover together; `Area.feeds()` lists the parts'
  feeds as `Place.feeds()` does, each feed once.
- `fetch_pbf`'s provenance sidecar records `checked_bounds`, the bounds of
  the area the extracts were checked to cover, `extract_bounds`, the box in
  a single extract's PBF header, and each source's `bounds`. A crop cached
  before is given `checked_bounds` from its own sidecar, with the extract
  boxes unknown until `update=True`.

### Changed

- `fetch(aoi=...)` takes the feeds from the installed feed index when the
  index places cover at least half of the area's land, with the same
  `tiers`, `exclude`, `on_unknown`, selectors and containment as
  `fetch(place=...)`, and `FetchResult.places` lists those places.
  Otherwise it searches the Mobility Database catalogue by bounding box and
  warns why before anything is downloaded; options only the index honours
  are then refused. `index=False` searches the catalogue without a warning.
  `DISCOVERY_SEMANTICS_VERSION` is 5.
- Areas in km², such as those of the OSM area note, are measured in an
  equal-area projection.
- `transitio.index.refresh()` streams the index archive to disk instead of
  memory and shows its download as a progress bar on stderr
  (`progress=False` turns it off). The README and quickstart give the
  download size and the disk space the snapshots take.
- The Mobility Database catalogue export is downloaded through the same
  retrying download as feeds and replaces the cached copy only when
  complete.

### Fixed

- `MobilityDatabase.feed()` works without a refresh token: it reads the
  catalogue export, as `search_feeds` does.
- A feed matched by a whole-feed edge and an edge without a usable selector
  is selected whole instead of as unavailable.
- Stops at (0, 0) no longer count as located stops in the OSM area note.
- A crop drops the `areas.txt`, `location_groups.txt` and `networks.txt`
  rows that only the rows it removed named.
- The index download no longer holds the whole archive in memory.

## 0.20.0 — 2026-10-07

### Added

- Downloaded feeds are kept in a download cache, `<cache_dir>/gtfs` (by
  default the platform cache), one version per distinct archive, named by
  its SHA-256, beside a `.provenance.json` sidecar recording every
  acquisition. `fetch` serves a feed from a cached version that serves the
  request without downloading it: the version that served the same request
  before, else the newest. A day no cached version serves downloads the feed
  and keeps the older versions, so a repeated run delivers the same feeds
  and reports, offline too. The selection table gains a `cache` column
  (`downloaded`, `reused`, `refreshed` or `fallback`).
- `use_cache=False` on `fetch`, `MobilityDatabase.download`,
  `MobilityDatabase.download_latest` and `TransitlandAtlas.download`
  downloads again and replaces the feed's cached versions; when every
  attempt of a `fetch` refresh fails, the cached version is delivered with a
  `UserWarning`.
- What `fetch` makes of a cached version (crop, repair and validation
  results) is stored with it and read back by a later call making the same,
  and a dataset's hosted validation report is stored at its first use.
- `transitio.cache.info()` lists what the cache holds, with its sizes, and
  `transitio.cache.clear()` empties it, or removes versions unused for a time
  (`older_than`) or the versions of some feeds (`feeds`).
- Identical archives of several feeds are stored once, as hard links to one
  blob; on Windows, and where a link cannot be made, each feed keeps its own
  copy.
- `fetch` leaves out of each delivered feed the trips that a feed earlier in
  the selection record also runs (for a place, in the order of
  `place.feeds()`). Trips compare as `merge_feeds` compares them: the same
  route name and mode, stops and times, nearly (within 50 m and 3 minutes),
  pickup and drop-off behaviour, and frequencies, whatever the agency. With
  `when` the trips of that day are compared; without it, a trip is left out
  only when it is covered on every date it runs. A feed whose every trip in
  scope repeats is skipped, as is one left without a requested mode under
  `modes`; a trip that cannot be compared, such as one whose calendar cannot
  be read, is never left out, and a feed in another time zone, unreadable or
  over `max_total_bytes` is not compared. The results are stored with the
  cached versions.
- `duplicate_trips="drop"` (the default), `"exact"` or `"keep"` on `fetch`,
  and a `duplicate_trips` column in the selection table counting the trips
  left out of each feed (None for a feed not compared).
- `crop_feed(..., exclude_trips=...)` leaves out the named trips and what
  only they used.
- `FetchResult.paths`, `{feed id: path}` of the delivered feeds in the order
  of `FetchResult.feeds`.

### Changed

- `fetch(directory=...)` receives only the delivered feeds, the crop,
  repair or a copy of the cached version, beside their provenance sidecars;
  downloads always go to the cache, and a `directory` inside it is refused.
  Without `directory`, the catalog clients' download methods return the
  read-only cached file; with it, a writable copy at the same path as
  before.
- A report's provenance describes the first acquisition of the version
  delivered, so a reused version is reported as when it was downloaded.
- On the place path, a cached version counts as unchanged since indexed for
  `contained="drop"` only by a probe recorded under the index snapshot in
  use, and a key-protected feed without usable credentials uses only copies
  fetched without them.
- Trips and routes compare route types by basic mode (bus, tram, subway,
  rail or ferry), in `fetch`'s repeated trips and versions and in
  `merge_feeds`, so an extended type such as 704 (local bus) matches 3
  (bus).
- The selection record holds one entry per candidate feed. The notes it
  held in entries without a feed move to `FetchResult.osm_note` and
  `FetchResult.view_note`.
- A feed cut to a route selection is noted `"cut to <n> of <m> routes"`;
  the routes stay listed in `selections`, which is now documented. A feed
  delivered whole because its selector cannot be trusted is noted
  `"delivered whole: selector out of date"` or `"delivered whole: selector
  unavailable"`.
- `FetchResult.contained` maps each feed left out as contained to the
  delivered feeds carrying it, and the note `"kept: containment in <ids>
  not proven current"` names the containers.
- `fetch(directory=...)` names each delivered feed by its feed id,
  `<feed id>.zip` beside `<feed id>.provenance.json`, flat in the
  directory; an id that is not a safe file name becomes its ASCII form, `+`
  and its SHA-256. A later call delivering the same feed into the same
  directory replaces its files. The `id-<sha256>` folders earlier versions
  left in a directory are not removed.
- `ServiceLevel.departures_per_day`, `IndexedFeed.relevance` and
  `Place.service` are documented as the index now computes them: departures
  are averaged over the period a feed's timetable covers, a feed stale when
  indexed counts in no feed's share of the place nor in the place's sums,
  and a merged index sums the place over every build's feeds.
- `fetch`'s removal of repeated trips and `merge_feeds`' near-trip matching
  use less memory: they read only the columns they compare, match only the
  day's trips with `when`, and join candidate trips in parts. For the nine
  feeds of the Munich metro area the peak fell from about 4.2 to 2.7 GB with
  `when`, and from about 9.3 to 6.6 GB without it, where the comparison
  takes about 60 seconds instead of 50; the results are unchanged.

### Fixed

- `fetch` checks a feed's selector against a download whose members are
  up to 8 GiB, as large as the index build reads. A member over 2 GiB made
  every selector of the feed out of date, so a national aggregate whose
  stop_times.txt is that large was delivered uncut even when its selector
  matched the download.

## 0.19.1 — 2026-10-02

### Changed

- Without credentials, `fetch(place=...)` reads a key-protected feed from
  the Mobility Database's keyless hosted copy when there is one, and the
  feed's note gives the reason and its `access_instructions()`; a feed
  without a copy is skipped as before. Trafiklab's Swedish feeds and most
  feeds of Spain's national access point have such copies. When a download
  with credentials fails, the copy is read without them.

## 0.19.0 — 2026-10-02

### Added

- The reader reads schema-11 indexes. A feed carries its access details
  (`IndexedFeed.access`, `access_provider`, `auth_method`, `auth_params`,
  `access_url`, `registration_url`, `download_url` and
  `access_instructions()`), the
  index lists the access providers (`Index.access_provider(provider_id)`), and a
  place carries its centre (`Place.centre`, a point or None) and its
  population (`Place.population`, an int or None). Schema-10 indexes read
  as before; a schema-11 index needs transitio 0.19.0.
- `transitio.credentials` stores the credentials a key-protected feed
  needs, per access provider: `set`, `get`, `clear` and `configured`,
  read from `TRANSITIO_KEY_<PROVIDER>__<FIELD>` environment variables or
  a TOML file readable by the user alone. The file store is POSIX-only: on
  Windows `set` and `clear` raise `NotImplementedError` and only the
  environment variables are read.
- `fetch(place=...)` downloads key-protected feeds with their provider's
  credentials, from `transitio.credentials` or
  `credentials={"<provider>": {"<field>": "..."}}`. It sends them only to
  the origin of the feed's access URL and keeps them out of the reasons,
  paths, sidecars, reports and `httpx` log records it writes. A protected
  feed without credentials is skipped with a reason followed by its
  `access_instructions()`. On a schema-11 index a feed is downloaded from
  its `download_url` first and the Mobility Database's hosted copy second.

### Changed

- Place lookups:
  - In "Name, Qualifier" the qualifier is read as a containing region or
    country first, and any containing place only when none matches; the
    comma form is tried even when the whole string is another place's
    label in some language. "Copenhagen, Denmark" matched Copenhagen, New
    York, which lies in a town named Denmark, and "Halifax, Canada" gave
    the region through a Piedmontese label.
  - Plain lookups count a metro once across its definitions, as
    `kind="metro"` did, so "Cambridge, United Kingdom" resolves.
  - A place that carries a name only as a translation leaves the contest
    when another place carries it as its name or English label, unless it
    is far better known. "Bergen" gave Mons, whose Dutch name it is.
  - Where feeds do not decide, the place far better known by its
    recorded labels wins: "Moscow", "Delhi" and "Cairo" resolve instead
    of staying ambiguous.
  - In an index recording populations, a city of at least 200,000 people
    with more than twice the population of every other place of its name
    wins that name: "Lima" is Lima, Peru, not Lima, Ohio.
  - A record inside a far better-known city of the same name is set aside
    as that city's namesake: "Valencia" gave Valencia, Venezuela, because
    Valencia, Spain, contains a comarca of its name.
- `merge_feeds(check=True)` no longer refuses when an input's validation
  reached only a warning-severity cap, such as the block-overlap pair
  check's; the refusal names the validation, the file and the budget.
  Tampere, Boston, Toronto, Los Angeles and Chicago were refused with
  "cannot tell inherited errors from introduced ones".
- The crop drops stop_times rows naming a stop the feed lacks, trips
  naming a route it lacks, and a trip that drop leaves with fewer than two
  stop times, and the fetch report gains `summary.droppedRows`. Moscow's
  feed names three stops its stops.txt lacks, and cafein refused it.
- The crop drops exact repeats of a trips.txt row and reports them;
  repeats that differ still refuse. Delhi's feed repeats eight trips.
- `fetch_pbf` downloads the smallest set of OSM extracts that covers the
  area (pyrosm's `strategy="smallest_total"`), and `must_cover` names what
  the extracts must cover; the pipeline passes the delivered stops.
  Zermatt downloaded the 2.3 GB Alps extract and London all of England.
  Requires pyrosm 0.15.0.
- Rows holding U+FFFD, the replacement for an invalid character, are kept
  with one `invalid_character` notice per field. The reader skipped them
  while the crop kept the rows naming them: Istanbul's feed writes one in a
  stop name and a route name.

## 0.18.0 — 2026-10-01

### Changed

- `merge_feeds(check=True)` refuses only the error-severity notices the
  merge introduced, those its inputs do not carry; `check="strict"`
  refuses any, as `True` did. The inputs are validated only when the
  merged feed has an error, and the report gains `"inherited_errors"`
  (per input, by code) and `"introduced_errors"`. Toronto's merge was
  refused over errors its inputs already carried.
- `merge_feeds` defaults to `timezones="skip"`, and the common time zone
  is the one most feeds' stops confirm: a feed vouches for its
  `agency_timezone` when most of its stops lie in an equivalent zone, as
  located by `tzfpy`, a new required dependency. Leaving a feed out warns
  with a `UserWarning`, its `"skipped_feeds"` entry gains
  `"stop_timezone"`, and one feed left is merged alone. `fetch` notes a
  delivered feed whose `agency_timezone` its stops contradict. Honolulu's
  merge stopped on an airport shuttle feed declaring `America/New_York`.
- `fetch(place=...)` tries the Mobility Database direct download, the
  Transitland Atlas static feed, then the Mobility Database hosted copy,
  moving on after any failure, an answer that is not a zip archive
  included. `selection`, the provenance sidecar and the report's
  provenance record `fetched_from` and `download_errors`. Feeds of Mexico
  City, Moscow and Montréal were skipped as `"download failed"` though
  the index was built from their hosted copies.
- `fetch` skips a feed lacking a file GTFS requires, as `"missing required
  file agency.txt"` (`"missing required files ..."` for several), and one
  with neither calendar file as `"missing calendar.txt and
  calendar_dates.txt"`, which includes a crop that keeps no trip; it does
  not repair them. Toronto's TTC surface feed has no `agency.txt`, and
  cafein refused every feed delivered with it.
- `fetch`'s version pass and the merge's duplicate drop compare agency
  names with case, diacritics, punctuation and trailing legal forms
  (Ltd, GmbH, Oy, S.A. …) set aside, and a feed with one unnamed agency
  can pair as a version. Trip signatures leave the agency out, so copies
  of one network published under different agency names are dropped as
  duplicates. Glasgow's "Midland Bluebird Ltd" and the national feed's
  "Midland Bluebird" did not match.
- Headway-based trips count in `fetch`'s version pass and the merge's
  duplicate drop: a trip with `frequencies.txt` rows signs over its
  stops, its times relative to the first departure and its frequency
  rows, and matches only a trip with the same rows. Santiago's DTPM
  network came three times.
- `merge_feeds` and `merge_tables` also drop a later input's trip that
  nearly repeats an earlier input's: same route name and type, stops
  aligned in order within 50 m and 180 s, a mean time difference of at
  most 60 s, and agreeing pickup, drop-off and accessibility values.
  `duplicate_trips="exact"` keeps equal-signature matching only, and the
  report gains `"near_matches"` and `"unaligned_stops"`. Santiago's
  merged day fell from 16,890 trips to about 11,040.
- `fetch` treats a feed whose trips run under calendar rows spanning
  4,000 days or more as undated: it pairs with a dated feed on shared
  stops alone and is left out when a kept dated version starts later,
  and every such feed notes `placeholder calendar <start> to <end>`.
  Bogotá got TransMilenio's 2024 snapshot (2000 to 2099) beside the
  2026 feed.
- transitio's HTTP clients send `User-Agent: transitio/<version>
  (+https://github.com/cafein-py/transitio)` instead of httpx's default,
  which Metrolink's and Riverside Transit's hosts answered with 403.
- Feed downloads retry dropped connections, read timeouts, short bodies
  and retryable statuses (three failed attempts, ten requests at most)
  and resume with `Range` when the server pins the file with a strong
  ETag or a Last-Modified date; the read timeout is 60 s and the connect
  timeout 15 s.
- A region or country of at most 1,000 km² lists its primary feeds too
  by default, as a city does, so a town-sized municipality or small state
  offers its local feeds; `DISCOVERY_SEMANTICS_VERSION` is 3. When the
  default view of a place is empty although it has feeds, `fetch` warns
  and notes which tiers would fetch them. Monaco's default fetch returned
  no feeds and no warning.
- `place()` resolves exact names only: a name with only partial matches
  raises `PlaceNotFoundError`, whose `.candidates` lists them, and the
  feed margin does not decide when a better-known exact namesake (100 or
  more language labels, more than twice the leader's) lies in another
  country. "Nuuk" had resolved to Nuuksio, "Moscow" to Moscow, Idaho.
- `place()`'s city-first rule counts a city-level region or dependency
  when no exact match is a city, and places reaching the name only
  through an alias (a county, a neighbouring region, the country) no
  longer compete with a city that carries it as its name. Istanbul,
  Kuala Lumpur, Dubai, Hong Kong and Cape Town resolved as ambiguous,
  and Taipei resolved to its metro.
- `place()` counts as a place's own names its name and its labels in its
  country's languages (from Unicode CLDR 47) or English. A place that
  reaches a name only through a label in another language no longer
  competes with one carrying the name as its own, unless it is far better
  known, and the feed-lead veto compares own names. `suggest()` ranks a
  label in another language after aliases. "München" was ambiguous
  between Munich and a village of that name, and "Buenos Aires" matched
  127 places, Pinto in Spain among them through its Irish label.
- `place(kind="metro")` answers with one metro when several definitions
  delineate it (their metros share a member place), the first of
  functional urban area, metropolitan statistical area, metropolitan
  region and city-region (FAO). The new `definition=` argument picks a
  definition and implies `kind="metro"`; one no metro carries raises
  `ValueError` listing the definitions the index holds. Stockholm, Basel,
  Strasbourg, Helsinki and Paris tied among their definitions.
- `fetch` counts each delivered feed's located stops (those with usable
  coordinates) outside `osm_area` in a new `selection` field,
  `stops_outside_osm`, and the last selection entry notes the total, e.g.
  `"OSM area: 4970 of 10026 located stops outside it"`. The crop keeps
  each trip that serves the area whole, so such stops can get no
  footpaths in cafein. cafein gave 3,418 of Glasgow's 10,026 stops no
  footpaths, and fetch did not say why.
- `fetch_pbf`, and so `fetch`, downloads the smallest single OSM extract
  that contains the area among Geofabrik and BBBike extracts and
  Movisda's administrative areas and grid tiles, through
  `pyrosm.get_data_by_area` (pyrosm `>=0.14.0`). pyrosm downloads it,
  three attempts per extract, falling back to the next smallest. The
  provenance sidecar gains `provider`, `extract`, `extract_bytes` and
  `failed_extracts`, and its `retrieved_at` is when the extract was
  downloaded, for a crop and a reused extract too. `fetch_pbf` loses its
  `transport` argument and raises `ValueError` for an AOI without area.
  Full extracts are cached as `<provider>_<file>.osm.pbf`, so extracts
  cached before are downloaded again. Basel, Strasbourg, Baarle-Hertog,
  Frankfurt (Oder), Tornio and Haparanda got Geofabrik's whole Europe
  extract (35 GB); Basel now gets BBBike's 100 MB Basel extract.

### Fixed

- `transitio.index.fingerprint.from_feed` reads header names without
  surrounding whitespace, as the index build does (of two names that trim
  alike, the first column is read), so a feed with a padded header, such
  as Metra's, keeps a trusted route selector in `fetch`.
- The validator no longer checks `routes.network_id` against
  `networks.txt`, since GTFS makes it an id of its own: a feed without
  that file had every value reported as a `foreign_key_violation` error,
  and `repair_feed` cleared them all. A `network_id` column in
  `routes.txt` next to `route_networks.txt` or `networks.txt` is the
  canonical `route_networks_specified_in_more_than_one_file` error.
  Strasbourg's CTS feed carried 38 such errors and MBTA's 249.
- Validation, cropping and repair read header names and values without
  surrounding whitespace and write them trimmed; a file that had any
  carries one `leading_or_trailing_whitespaces` warning, with
  `trimmedCount`, instead of one per value, so a padded column no longer
  fills the notice cap. `repair_feed` logs a `trim_whitespace` fix per
  file, `crop_feed`'s report gains `"source_notices"`, and `FeedEditor`,
  `merge_feeds` (`"trimmed_values"`), `patch_feed` (`trim_values`) and
  `fetch`'s mode filter strip values too. Renfe pads every line with
  about 150 spaces, which left its feed without a service window.
- `crop_feed` drops the `fare_rules.txt` rows naming a route or zone it
  removed, and a fare whole when that leaves it without its route rules,
  its origin-destination rules or one of its `contains_id` zones, or when
  its agency goes, so no fare applies more widely than in the source. A
  fare without rules applies everywhere and stays. GO Transit's cropped
  feed carried 3,458 fare-rule `foreign_key_violation` errors.
- `merge_feeds` and `merge_tables` keep every input's default rider
  category instead of raising `ValueError` when several inputs declare
  one, as GTFS sets the default per fare product. `merge_feeds`' report
  lists them under `"rider_defaults"`, with the fare products a blank
  `rider_category_id` opens to every input's categories. Los Angeles's
  merge was refused.
- The scanner's delimiter guard counts only delimiters outside quotes, in
  every table, so Brockton Area Transit's two-column `areas.txt`, whose
  quoted polygons hold thousands of commas, no longer stops its crop as
  exceeding `max_columns`. A file that trips the guard keeps the rows
  before that line, and the refusal names the line and the guard:
  "areas.txt line 7 has more than 4096 delimiters outside quotes, the
  guard set by max_columns (1000)".
- A feed past the 2,000,000-day calendar expansion cap keeps its service
  window, its `moment` for the target day and its `expired_calendar`
  check, and carries a `service_expansion_truncated` warning; only
  `baselineTrips` is `None`, and the notices comparing against it are not
  raised. A temporal crop keeps every service that runs in its window.
  Basel's crop of the Swiss national feed had no service window.
- `fetch` reads a feed named by a URL fragment inside the downloaded
  archive (`…/gtfs.zip#1/google_transit.zip`, or a folder), downloading
  the outer archive once per place; members declaring more uncompressed
  bytes than `max_total_bytes` are refused. Melbourne's seven PTV feeds
  were skipped as having no usable `trips.txt`.
- A failed OSM extract download no longer loses the feeds `fetch` already
  fetched: they are delivered without an extract, with a `UserWarning`
  and a selection note. Baarle-Hertog's extract download timed out.
- `fetch_pbf` calls sharing a cache or output directory take turns, and a
  crop and its provenance sidecar replace their files only when complete,
  so a file and its sidecar describe the same extract, and a failed crop
  no longer leaves a truncated file that later calls return as cached.

## 0.17.0 — 2026-09-28

### Added

- `FetchResult.selection` records one entry per candidate feed, in
  candidate order, for area and place fetches: `feed_id`, `name`, the
  `decision` (`"delivered"` or `"skipped"`), the `reason` for a skip, a
  `note` about a delivered feed (the routes it was cut to), the index's
  service window (`index_window`, place fetches only), the computed window
  of every validated download (`feed_window`), the feeds a same-content or
  containment skip names (`same_as`, `contained_in`), `version_of` and the
  delivered `path`. `FetchResult.selection_table()` returns it as a pandas
  DataFrame; `skipped` lists the same skips.

### Changed

- `fetch` skips expired feeds by default (`expired="skip"`). Without `when`,
  a feed whose computed service window ended before today is skipped as
  `"service ended <end>"`; a feed that starts later or runs on other
  weekdays stays. On the place path, a feed whose index service window
  misses the day is skipped before download when a conditional `HEAD` to
  the URL the index crawled, carrying the recorded ETag or Last-Modified,
  answers 304 Not Modified (`"service ended <end>; unchanged since
  indexed"`); any other answer downloads it. `expired="keep"` sends no
  probe and, without `when`, delivers expired feeds as before.
- With `when`, a feed whose validation report for the day counts no active
  trip and carries the `no_service_on_reference_date` notice is skipped as
  `"no service on <day>"`, and a computed window that misses the day is
  skipped as `"service ended <end>"` or `"service starts <start>, after
  <day>"` instead of `"no service on the requested day (actual window
  ...)"`. A `reference_date` that disagrees with `when` raises
  `ValueError`.
- `fetch(place=...)` fetches the OSM extract after the feeds, for the
  place's parts that hold a stop of a delivered feed (the whole place when
  none does or a delivered feed's stops cannot be read), each part grown by
  1.6 km. Remote parts no delivered feed
  serves, such as Tokyo's Pacific islands, no longer widen the extract.
  `FetchResult.osm_area` holds the grown area, and the selection record
  notes the parts left out (`"OSM area: 1 of 47 parts (1783 of 2188
  km²)"`). Area fetches are unchanged.
- `fetch_pbf(..., buffer_m=0)` grows the AOI by `buffer_m` metres before
  the extract is picked and cropped, each part in the UTM zone of its
  centroid; the provenance sidecar's `aoi_bounds` are the grown AOI's. A
  part is not grown across the antimeridian, with a `UserWarning`. The
  docstring now states that the crop is pyrosm's envelope crop, not the
  true polygon.
- `merge_feeds` and `merge_tables` leave out an input's trips that repeat
  trips kept from the inputs before it (`duplicate_trips="drop"`, the
  default; `"keep"` keeps them). Trips repeat when their route key, stops,
  times and pickup and drop-off behaviour match, on dates the earlier trips
  run, one earlier trip per later trip and date; trips sharing a `block_id`
  go together. A dropped trip's stops still served by a kept trip are linked
  to the earlier trip's stops by transfers, and the report counts drops
  under `"duplicate_trips"`. A regional aggregate merged with an operator's
  own feed, as in London and Augsburg, carried the operator's trips twice.
- `fetch(place=...)` delivers one copy per service. `contained` defaults to
  `"drop"`, leaving out a contained feed when a container was delivered
  whole and `HEAD` probes prove both archives unchanged since indexed. A
  download equal to a delivered archive is skipped when its routes are
  within those delivered from it. With `when`, a feed whose route keys and
  stops largely match a kept one is left out as `"another version of <id>"`
  when kept feeds run every trip it runs on the day. `note` says why a
  feed was kept; several notes join with `"; "`.
- `merge_feeds` and `merge_tables` merge inputs whose `agency_timezone` names
  differ but denote the same clock, such as `CET` and `Europe/Paris` (Paris),
  or `America/Montreal` and `America/Toronto` (Montréal). Two names are
  equivalent when the tz database knows both and their UTC offsets agree at
  every quarter hour of the inputs' service, from the earliest service date
  to the latest plus its latest stop time, within 20 years before and 10
  years after today. `timezones="skip"` counts equivalent names as one zone.
  The merged `agency.txt` uses the name most inputs declare, and the report
  gains `"timezone_interval"` (the UTC instants compared) and
  `"timezone_aliases"` (each name replaced and the name used). A refusal now
  names each input with its zones, by position, prefix and path: New York's
  merge of 45 feeds stopped on `America/New_York` and `UTC` without saying
  which feed declared `UTC`.

### Fixed

- `crop_feed` refuses a feed only when a table the crop depends on is cut
  short, by a row, byte or column budget or an unreadable entry, in the
  source or in the cropped feed. A reached notice cap (`max_notices_per_file`
  or the block overlap check cap) no longer refuses it; the cropped feed's
  notices then include `notice_limit_reached`. São Paulo's SPTrans feed and
  Westchester's Bee-Line could not be cropped with the default budgets.
- The refusals of `crop_feed` and `repair_feed` name each file and the
  budget it exceeded with its value, such as "stops.txt exceeds max_rows
  (20000000); raise it to crop this feed", instead of "feed exceeds the scan
  or notice budgets". `repair_feed` still refuses a feed whose notices were
  sampled, and says so when the cap is one that no budget raises. An
  `unreadable_file` notice for a violated budget lists it under `budgets`.
- A zip with up to 64 KiB of bytes after its end-of-central-directory
  record is read instead of refused as "not a readable zip: no
  end-of-central-directory record found", so validation, cropping and
  repair accept it. The feeds of Réseau de transport de Longueuil and CRT
  Lanaudière carry one and two such bytes; Python's `zipfile` reads both.
- `FeedEditor` reads header names without surrounding whitespace, so
  `merge_feeds`, `patch_feed` and a saved feed write `agency_name` where the
  source header says ` agency_name`. Columns that then share a name fold into
  one, each row keeping the first non-blank value. A merge used to write both
  names, which cafein could not read, and a padded id column escaped the
  per-feed prefix. The merge report lists the changed names under
  `"header_fixes"`, and `patch_feed` logs them as `normalise_headers` actions.
  One of Greater London's feeds has such a header.

## 0.16.0 — 2026-09-27

### Added

- `transitio.index.fingerprint.identity(source)` computes a per-table content
  identity of a GTFS feed, from a zip or from a directory holding the member
  files: one digest each for stops, routes, trips, calendar, calendar_dates
  and stop_times. A digest does not change with column order, row order,
  whitespace around values, empty or absent optional columns, byte-order
  mark, line endings or zip packaging; stop coordinates are rounded to about
  1 m and single-digit stop-time hours zero-padded, and ids and all other
  values are kept verbatim. An unreadable source has no identity (`None`).
  Large tables are hashed with bounded memory.
- `fingerprint.identical_groups(identities)` groups the feeds whose
  identities match: equal stops, routes and trips, the same calendar
  tables, and equal stop_times where both feeds carry it.

## 0.15.0 — 2026-09-26

### Added

- The reader reads schema-10 indexes, whose feeds table records for each
  feed the larger feeds whose stops and routes contain its own
  (`contained_in`, exposed as `IndexedFeed.contained_in`; empty before
  schema 10). Schema 10 needs transitio 0.15.0.
- `fetch(place=..., contained="drop")` leaves a feed out when a feed
  containing it is delivered in the same call, before downloading it, and
  fetches containers first; the default, `contained="keep"`, delivers every
  feed and reports the delivered pairs in `FetchResult.contained`.
  Containment is a heuristic, not proof that every trip is carried.
- `merge_feeds(..., timezones="skip")` leaves out the feeds whose
  `agency_timezone` differs from the one most feeds declare (ties: the
  earliest feed's, then the first by name) and merges the rest, each keeping
  the prefix it had among all the inputs; the report lists them under
  `"skipped_feeds"`. The default, `timezones="refuse"`, raises as before.
  New York City's 41 feeds could not be merged because one intercity coach
  feed declares UTC.

### Changed

- `fetch` delivers a feed's data once when two catalogue entries serve the
  same files: a download whose content equals a feed already delivered in
  the call, cropped to the same routes, is recorded in `skipped` as
  `"same content as <feed id>"` instead of being cropped, validated and
  delivered again. Content is the same when the archives' SHA-256 digests
  match, or when every entry's name, CRC-32 and size match and the SHA-256
  digests of the decompressed entries confirm it.

### Fixed

- The default per-file budget of validation and cropping (`max_entry_bytes`)
  is 2 GiB, the whole total budget, instead of 1 GiB. Greater London cropped
  from Great Britain's national bus feed keeps a 1.4 GiB `stop_times.txt`
  (18.2 million rows, within the row budget), which the crop refused with
  "cropped feed exceeds the scan or notice budgets".
- `fetch` over an area with a `directory` writes each feed into its own
  folder. Without an API token every feed's download was named `latest.zip`
  in the one directory, so each feed overwrote the one before and the result
  listed the last feed several times.

## 0.14.0 — 2026-09-26

### Added

- `place()` and `places()` read a qualified name, "Name, Qualifier" with one
  or more qualifiers, as the places lying within a region or country each
  qualifier names: `"London, Ontario"`, `"London, Canada"`,
  `"City of London, UK"`. Qualifiers match the names, translations and
  aliases of a place's region and country, codes such as `UK` included; a
  label that itself holds a comma still matches as written.

### Changed

- The feed-count margin that breaks a name tie never favours a place reached
  only through an alias or a translation over one carrying the name as its
  own: "Sao Paulo" raises `AmbiguousPlaceError` instead of naming Saint Paul,
  Minnesota, whose aliases include São Paulo, and "Sao Paulo, Brazil"
  resolves.
- A bare place name resolves to the city rather than raising
  `AmbiguousPlaceError` when the other places sharing the name are named
  after it: the metros in its country (with several metro definitions a
  city and each of its metros share a name), a containing area that runs
  much the same service, and a place inside the city that lists its name
  only as an alias, such as a district of Bogotá. "Helsinki", "Berlin",
  "Augsburg" and "Bogotá" now resolve to the city. Other places sharing a
  name are decided as before, by the sole exact match or a lead of more
  than twice the runner-up's feeds. `DISCOVERY_SEMANTICS_VERSION` is 2.
- A bare city name is no longer promoted to the city's default metro. The
  city's metros are listed by `Place.metros` and `Place.delineations()`;
  `kind="metro"` restricts a name to metros.
- `crop_feed` leaves out an optional table the crop empties (for example
  frequencies.txt or shapes.txt when no retained trip uses them) instead
  of writing it as a header alone, which validators report as an empty
  file. Required files are still written.

### Removed

- `Place.promoted_from`, which only a promoted city carried.

## 0.13.0 — 2026-09-25

### Added

- `fetch` takes an `osm` flag (default True); with `osm=False` the OSM
  stage is skipped and the result's `osm_pbf` is None, for callers who want
  only the GTFS feeds.

### Changed

- `fetch` crops each feed before repairing it, so with the default crop
  `repair=True` works on the area's feed rather than on the whole source.
- `crop_feed` streams stop_times.txt, trips.txt and shapes.txt from the
  archive instead of parsing them whole, so a national feed crops to a
  city within the default budgets and in memory bounded by the area. The
  row and byte budgets keep applying to the other tables and to the
  cropped feed, whose validation the report describes; the source feed is
  no longer validated before the crop. A cropped feed the budgets cannot
  validate whole, and a feed repeating a `trip_id` in trips.txt, are
  refused.

## 0.12.0 — 2026-09-23

### Added

- The reader accepts index schema 7, a snapshot laid out as partitions:
  one directory per country (its feeds by home country, places and
  domestic edges) beside `international/feeds.parquet` and
  `links/edges.parquet`. `read_index(path)` joins every partition into
  the flat feeds, places and edges tables, cross-border edges included,
  and keeps the links table beside them; `read_index(path, country=...)`
  loads one country with the links into it, and `Index.feeds_in(partition)`
  reads a link's feed partition once. Every partition table is checked
  against the manifest's digest, row count, columns and snapshot id, and
  the release members and the refresh unpacker follow the partitions.
- Default views per place kind on a schema-7 index: a city or metro lists
  its primary and secondary feeds, a region its secondary and tertiary, a
  country its tertiary; `categories` names other relevance categories or
  `None` for all, `international=True` adds the cross-border feeds from
  the links, and results come back by category, then relevance, then id.
  Tier edges and feeds expose `relevance_category`, `relevance` and
  `cross_border`.
- Index schema 8: the feeds table is GTFS only and names its GTFS-RT
  companions in `realtime_feed_ids`. `Index.realtime`,
  `Index.realtime_in(partition)` and `Index.realtime_unlinked()` read the
  `realtime.parquet` companion table, and a feed returned for a place
  carries its companions as `IndexedFeed.realtime`, `RealtimeFeed` records
  with the static feed, link method, entity types and endpoints.
- Index schema 9: `IndexedFeed.service_start` and `service_end` are the
  feed's calendar span, and `Place.validity` is a `Validity` record with
  the dated and undated feed counts, the span, the `Window` records of
  constant valid-feed count, the best window and `on(date)`, the number of
  valid feeds on a day.
- `Place.subtype`, `Place.ancestors` and `Place.delineations()`: every
  delineation of a place — the place itself, its administrative ancestors
  and the metros it is a member of — as `Delineation` rows carrying the
  row's kind and subtype, so a caller can list the areal definitions a
  place has and pick one.
- `transitio.index.suggest()` (and `transitio.suggest`): type-ahead place
  suggestions for a typed prefix, matched against every name, translated
  name and alias a place carries, ranked by exact label, kind precedence,
  the label's source and feed count, with `kinds` and `country` filters
  and a `lang` for the label shown; an index's labels are sorted once, on
  the first call over it or ahead of it through `prepare_suggestions()`,
  so a query over the whole catalogue answers in milliseconds.

### Changed

- The feed-index build moved to its own repository,
  [transitio-dev/transitio-index](https://github.com/transitio-dev/transitio-index);
  transitio keeps the reader (`transitio.index`). The maintainer-only `build`
  extra went with it.

## 0.11.0 — 2026-09-08

### Added

- Curated FAO city-region metros publish: `set_statistical_area` accepts
  the `fao_city_region` scheme, and the metros stage resolves the code in
  the pinned FAO regions table, derives the metro's members again exactly
  as the suggestion report does (the eligible cities whose land areas lie
  in the region), judges the curator's confirmation against that list, and
  gives the metro the members and the region as its statistical identity
  only with the FAO city-regions and Overture divisions both allowlisted as
  derived inputs, credited in the derived inventory and the NOTICE; a stale
  confirmation or a closed gate is reported and the metro stays report-only.
  The suggestion report's pasteable pair is applicable as emitted.
- A named Overture division of the kept subtypes is a place even when no
  QID resolves: the skeleton keeps it (resolution method `overture_id`),
  the seed and the expand stage key it by `overture:<id>` and the registry
  identifies it by that concordance, its `wikidata_id` is null, and the
  resolution report holds only nameless divisions and conflicting signals.
- The publisher writes index schema 6: the places table carries
  `wikidata_id` (the QID beside the own id, null without one),
  `concordances` (every id the place carries per namespace — the registry's
  effective view over the place and the rows merged into it, or the QID
  alone without a registry) and `former_ids` (the ids merged into it), and
  the manifest records the schema-6 reader floor.
- The metros and expand stages resolve every QID a source names through
  the registry before joining metro rows, so a merged-away QID meets its
  survivor's row — and a different statistical code on it is a conflict,
  not a second row — and the expand stage enriches its discoveries with
  the labels of the QID each place carries after identification.
- Places are keyed by the index's own `tp_` id from the stages that mint
  them: the seed, the metros stage and the expand stage identify their rows
  through the registry and re-key them — links, placements, assignments
  and reports alike — with the QID beside the id (`wikidata_id`), and two
  QIDs the registry has merged become one place under the survivor's id.
  Override references resolve to own ids (the QID-keyed stages join on the
  canonical QID), a curated `add_place` may be keyed by another
  concordance and is minted from it, and the FAO report lists each city's
  QID beside its id. Without a registry, rows keep their QID keys.
- The expand stage is a registry transaction of its own: it identifies
  every place it discovers from crawled stops through the registry the
  gazetteer ran with, saves the registry before its generation is
  published, and records the digests loaded and saved in its manifest;
  later commands expect the registry expand saved, refuse one changed
  after it, and refuse expanded places not built on the run's registry;
  a registry-backed expansion needs a committed gazetteer run. The registry's lock file beside `overrides/` is ignored by git.
- The reader understands index schema 6, which keys places by the index's
  own `tp_` id: the places table gains `wikidata_id` (nullable),
  `concordances` (ids per namespace) and `former_ids`; `Place.wikidata_id`,
  `Place.concordances` and `Place.former_ids` expose them (a pre-6 index
  reports its QID key as both), and a place resolves by its own id, any QID
  it carries — a merged-away one included — or a former id. Nothing
  publishes schema 6 yet.
- Override references resolve through the place registry: `places.yaml`
  (`place`, `parent_id`, `member_ids`, `set_place_members`), `edges.yaml`
  places, `set_coverage` place ids and golden membership lists accept a
  `tp_` id, a bare QID or `namespace:value` for any registered place, each
  resolved to the registry's current key for that place; a bare QID no row
  carries yet still names a place to be minted, and any other unknown
  reference is an error.
- The gazetteer run is one transaction: its stages publish staged
  generations under the cache's run lock, the registry is saved once after
  the last stage, and a run manifest (`gazetteer/run.json`) published last
  names the generation each stage pointer stands for. The store resolves a
  pointer the run manifest names to that generation, so consumers see a
  complete set or the previous one, never a mixture; a failure before the
  save leaves the registry and the previous set untouched, a crash after it
  leaves rows a rerun reproduces, and a corrupt manifest or pointer is an
  error rather than an absence. Later commands refuse a run whose registry
  is no longer the file on disk until the gazetteer runs again.
- The gazetteer run opens one place-registry session (`--registry`,
  default `places_registry.jsonl` in the overrides directory;
  `--registry-read-only` refuses any mint or enrichment): the seed
  identifies every place by its QID, Overture id and OSM relation and
  records `tp_id` and `wikidata_id` on the row, the metros stage does the
  same for metro rows with their CBSA or Eurostat code, and the registry is
  saved once after the last stage. The QID stays the place key.

- The place registry identifies places: `Registry.identify` finds the one
  place a candidate's concordances name, records any it lacked, refuses a
  candidate naming several places, one carrying a value a curator detached
  from that place, or one without any concordance, and otherwise mints the
  next id from the header counter; a read-only session refuses a mint or
  an enrichment at the point of discovery. Rows carry `detached` entries
  (a value kept in history but dropped from every lookup, re-attachable
  elsewhere) and every lookup uses that effective view. The committed
  registry starts as the header-only `overrides/places_registry.jsonl`,
  and a CI job checks every pull request's change to it against the
  registry's history rules.

- The place registry gains its session and save: `registry.session` holds
  the writer lock beside the file for as long as the block runs (a second
  writer is refused), the registry it yields is the only one that can write
  and loses that ability when the session ends, and `save` replaces the
  file atomically in canonical order — only when something changed, refused
  when the file is no longer the one the session last saw, and never in a
  read-only session — recording the digest loaded beside the digest written
  in its manifest.

- `scripts/index_build/registry.py` reads the place registry, the index's
  own place ids and their concordances: a committed JSON Lines file with a
  header counter and one row per id, loaded under validation (exact header
  types, unique JSON keys, ordered ids below the counter, known namespaces,
  well-formed QIDs, merge targets live, a concordance value on one place
  only) and read through `resolve` — an own id, a bare QID or
  `namespace:value`, merges followed and retirements refused — with the
  first QID of a live place canonical.

- `store.publish(..., staged=True)` writes a generation that no pointer
  names, `store.resolve_generation` verifies one by name, and pruning keeps
  every generation a run manifest lists under `generations`, so a gazetteer
  run can stage its stages' outputs and make them visible together.

- The FAO suggestion report names each city-region after its urban centre's
  GHS-UCDB match: entries carry `name`, `name_ambiguous` and
  `name_candidates`, the pasteable `add_place` line is prefilled with the
  name, the UCDB credit joins the per-entry provenance and the manifest
  counts named entries; names attach only while `GHS-UCDB R2024A` is in the
  derived allowlist, and the report says so either way.

- `scripts/index_build/ucdb.py` pins the FAO urban centres and the GHS Urban
  Centre Database 2025 general-characteristics table (JRC, CC BY 4.0) and
  names each FAO centre after the UCDB centre covering the largest share of
  its area (at least a tenth; a runner-up within half of it flags the match
  ambiguous), matched once into `raw/fao-names.json`; `GHS-UCDB R2024A` joins
  the derived-source registry and allowlist. Nothing consumes the names yet.

- `pinned.derive` holds the compute-once step for artifacts derived from
  verified inputs (reused while the manifest's `sources` equal the inputs'
  digests), which the FAO patches conversion now uses; `fao.read_zipped` and
  `fao.integer_ids` are the shared zipped-geodata and exact-id readers.

- The gazetteer gains an FAO city-regions stage (`scripts/index_build/fao.py`):
  the pinned 1-hour patches and regions (Zenodo 10.5281/zenodo.11187634,
  CC BY 4.0) are converted once to GeoParquet, and cities with no metro and
  no known official assignment are grouped by the city-region of their
  patch's highest-tier centre into `suggested_metros_report.jsonl`, each
  entry with a ready-to-paste override pair; nothing is published.

- `scripts/index_build/fao.py` pins the FAO multi-tier city-regions inputs at
  the 1-hour cutoff (Zenodo 10.5281/zenodo.11187634, CC BY 4.0), converts
  the patches shapefile once to a verified GeoParquet generation, and reads
  patches and regions under the verified nested-region contract.

- `scripts/index_build/pinned.py` holds the pinned-input machinery (fetch or
  local files, SHA-256 verification, one verified `raw` generation, reuse,
  pin-checked resolve) that `eurostat.py` now delegates to, so further pinned
  sources share it.

- The geometry stage gives any metro without geometry the union of its
  member cities' shipped polygons (`geometry_source = "member_union"`),
  simplified and validated like every other place; a member without
  shipped geometry, or a metro without members, leaves it without one.

- `scripts/index_build/eurostat.py` pins Eurostat's metropolitan-regions
  composition table (NUTS 2021) and the GISCO NUTS-3 boundaries by checksum,
  publishes them as a verified `raw/eurostat.json` generation, reads them
  under an asserted input contract and derives each city's Eurostat metro
  assignment by containment of its Overture representative point.

- `places.yaml` gains the `set_statistical_area` operation — a curated
  crosswalk from a metro QID to a statistical scheme's code (`eurostat_metro`),
  bound by `evidence_hash` to the derived member list it confirms — and the
  Wikidata client a `metro_candidates` lookup (the P8138 metropolitan-area and
  functional-urban-area entities of a city) for curators to pick that QID from.

- The licence inventory gains a `use` column (`geometry` or `derived`) and
  a `DERIVED_SOURCE_ALLOWLIST` for sources that may contribute build-time
  derived data; an earlier stage's derived-input rows flow into the inventory
  and their credits into NOTICE. Metro report rows name their `branch`.

- The metros stage derives Eurostat metropolitan-region membership for every
  city of a country the pinned composition covers and records it in
  `metro_assignments.jsonl`; a Eurostat metro publishes only through a
  `set_statistical_area` crosswalk and only while its derived inputs are
  allowlisted, otherwise it is reported with the Wikidata candidates its
  member cities link to; the derived-input rows reach the licence inventory
  and NOTICE. Cities the expand stage discovers get their Eurostat membership
  on the next build.

- `IndexedFeed.files` exposes the GTFS files a feed's archive carries (the
  manifest the crawl records under index schema 5; empty for an older
  snapshot), with `has_shapes` and `has_fares` as capability hints over it, and
  `Place.feeds(requires=...)` keeps only feeds whose manifest carries the named
  files — a feed whose manifest is empty cannot satisfy a requirement. The
  tabular export gains a `files` column.

- `fetch(place=...)` validates each selector against the feed it is applied to
  before filtering: the edge's `classification_fingerprint` is recomputed from
  the download by its `fingerprint_kind` and compared, and every selected route
  id must be present. An untrustworthy selector -- unavailable at build time, a
  fingerprint that no longer matches, or a missing route -- is never silently
  filtered; `on_untrusted_selector=` (`"auto"` default, `"whole"`, `"drop"`,
  `"error"`) decides its fate, `"auto"` skipping the feed when an `exclude` was
  asked for and otherwise delivering it whole with its tier treated as unknown.
  `StaleSelectorError` is raised under `"error"`, and every outcome is recorded
  in `FetchResult.selections`.

- `crop_feed(routes=...)` keeps only trips whose route is in the given set, and
  `fetch(place=..., tiers=...)` uses it to crop each bundled feed to the routes
  its matched tiers select; every dropped route and the selecting edge's state
  are recorded in `FetchResult.selections`. A whole-feed or unavailable selector
  filters nothing.

- `fetch(place=...)` selects feeds from the index by place and tier rather than
  by bounding box: it takes `place=` (a name, QID or `Place`, mutually exclusive
  with `aoi=`), `tiers=`, `exclude=` and `on_unknown=`, uses the place geometry
  as the AOI for the OSM extract, and downloads each feed preferring its
  Mobility Database URL over its Transitland Atlas URL (decision I).
  `FetchResult` gains `selections` and `provenance`.

- `TransitlandAtlas`, a fallback download client for Transitland Atlas feeds,
  mirroring `MobilityDatabase` without a token: `download(feed)` streams the
  feed's static GTFS zip with a provenance sidecar recording the source URL,
  checksum and retrieval time. `AtlasFeed` carries a feed's Atlas identity and
  download URL, built from an Atlas record with `AtlasFeed.from_record`.

- `active_index()` falls back to an index bundled in the wheel when no
  snapshot is installed and none is pinned, after the `use()` selection,
  the `TRANSITIO_INDEX_SNAPSHOT` pin and the platformdirs cache. The
  wheel ships `LICENSE` and `NOTICE`, and the docs carry an attribution
  page for the index's upstream sources.

- The `license` build stage (stage 7) between prune and publish: it reads
  exactly what publication would read, records every contributing source in
  `licence_inventory.jsonl` (the geometry audit's rows and the feeds'
  declared licences), writes the `NOTICE` that ships with the index, and
  publishes the tables as `*_licensed.jsonl`. Publication reads those when
  the generation is current, ships the `NOTICE` beside the Parquet files and
  records `licensed` in `snapshot.json`; a release is made only from a
  licensed snapshot. The stage judges each feed's `redistribution_allowed`
  from its declared licence (or a known-permissive SPDX identifier), nulls
  the coverage hull of a feed whose licence disallows redistribution, and
  asserts that none survived; `IndexedFeed.redistribution_allowed` exposes
  the judgement. A place without a boundary of its own is given the buffered
  union of the redistributable coverage hulls of its feeds, labelled
  `geometry_source = "derived_from_feeds"`. A place still without one is not
  published: its edges are rehomed to the nearest published administrative
  ancestor, else the published country of its `country_code`, merging with
  an edge already there column by column (`rehomed_from`, `merged_evidence`
  and `curation_history` record the origin); a feed left with no edge is
  listed in the manifest's `feeds_without_edges`. The pruning closure is
  re-applied and every foreign key checked before the artifacts are written.

- `transitio.index.refresh()` installs the newest published index snapshot
  this transitio reads into the platformdirs cache — the archive is
  downloaded, checked against the digest its release manifest declares,
  unpacked defensively (expected members only, regular files, size
  ceilings), validated by the reader and activated by one atomic rename,
  with the three newest snapshots kept — and `transitio.index.use()` selects
  an installed snapshot for the current process. Which snapshot a query
  reads is resolved on first use: the `use()` selection, then the
  `TRANSITIO_INDEX_SNAPSHOT` environment variable, then the newest
  compatible installed snapshot. `place()`, `places()` and `Place.feeds()`
  read the active snapshot when no index is passed.

- The index release contract (`transitio.index.release`): each snapshot is
  its own GitHub release in the `transitio-dev/transitio-index` repository,
  tagged `index-<snapshot_id>` and holding the archive,
  its `.sha256` and an immutable `manifest.json`, and the rule for picking
  the newest release a reader supports. `scripts/publish_index.py` packs a
  built index deterministically, creates the release as a draft, uploads and
  verifies the assets, publishes, and confirms the round trip a client makes.
  `snapshot.json` records the stage generations the index was built from
  and the override-file digests it applied, and the publisher refuses an
  index they no longer describe.

- `Place.feeds()` and the `IndexedFeed` object — the feeds serving a place,
  joined with their membership edges. `tiers=`/`exclude=` filter by tier and
  `on_unknown` governs unclassified edges; the aggregate `needs_review` is the
  *or* across matched tiers and `selector` their union (always a `Selector`,
  with `unavailable` dominating). Membership is a fact — a feed is listed for
  a place because a route has a scheduled stop there — and each feed carries
  its `service` level in the place (stops, routes, departures per day);
  `Place.service` sums it over the feeds. `feeds().to_geodataframe()`
  tabulates the result.

- `transitio.place` / `transitio.places` and the `Place` object — name resolution
  over a published index's places. A query (name, QID or `Place`) is matched
  against every label and alias in every language by a defined ranking (exact
  beats prefix beats token-subset, then `metro > city > region > country`); a
  bare city name promotes to its default metro; anything unresolved raises
  `PlaceNotFoundError` or `AmbiguousPlaceError` (which carries the candidates).

- `transitio.index.fingerprint` — the classification fingerprint of a feed's
  evidence (route ids, agencies, types, served stops and rounded stop
  coordinates), computed the same way when an index is built and when a
  selector is validated against a downloaded feed.

- `transitio.index.read_index` — the read layer over a published index, loading
  a `feeds.parquet`, an optional `places.parquet` (a GeoParquet of the gazetteer
  places, with boundary geometry), an optional `edges.parquet` (one membership
  row per place/feed/tier) and its `snapshot.json` manifest into an `Index`
  exposing `.feeds`, `.places` and `.edges`. A schema version it does not
  understand is refused with `IncompatibleIndexError`.

### Changed

- Index schema version 4: `feeds.parquet` carries the crawl evidence
  (`coverage` hull, `stop_count`, `etag`, `last_modified`, `last_crawled`,
  `crawl_status`) and `redistribution_allowed`, and `snapshot.json` records the `discovery_semantics_version`
  the build used, the oldest transitio that reads the schema
  (`min_reader_version`) and the version that built it (`built_with`).
  Every `IndexedFeed` exposes its `provenance` (the
  snapshot id, the reader's discovery semantics version and the transitio
  version) so a reproduction can say whether it is exact. Older snapshots are
  refused with an upgrade message.

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
