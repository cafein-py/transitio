"""A published feed index and its release, written in the reader's shape.

The reader's tests and the pipeline's need an index to read, install and
refresh. This module writes one directly -- the members of a release in the
layout ``transitio.index`` reads and ``transitio.index.release`` describes --
so those tests depend on the reader alone, never on the build that produces
real indexes. The row shapes mirror the build's publish stage column for
column; a test in the build's suite checks the two stay in step.
"""

import gzip
import hashlib
import io
import json
import re
import tarfile

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import shapely

import transitio
import transitio.index as reader
from transitio.index import release as contract

API = "https://api.example"
UPLOADS = "https://uploads.example"
DOWNLOADS = "https://objects.example"

# The tables below declare the schema-6 columns, so the fixture is pinned to
# that version rather than to whatever a newer reader supports.
SCHEMA_VERSION = 6
SNAPSHOT_ID = "0123456789abcdef"
NOTICE = b"Boundary geometry from Overture.\n"
HULL = "0101000000" + "00" * 16  # a WKB point
GEOM_HEX = shapely.to_wkb(shapely.box(24.9, 60.1, 25.1, 60.3)).hex()
_QID = re.compile(r"Q[1-9][0-9]*$")


def covered_feed(feed_id, **kw):
    return {
        "feed_id": feed_id,
        "onestop_id": kw.get("onestop_id"),
        "mdb_id": None,
        "id_minted": kw.get("id_minted", False),
        "source": "atlas",
        "spec": kw.get("spec", "gtfs"),
        "name": kw.get("name", feed_id),
        "aliases": [],
        "crosswalk_method": "url_exact",
        "crosswalk_confidence": 1.0,
        "crawlable": kw.get("crawlable", True),
        "uncrawlable_reason": None,
        "coverage_source": kw.get("coverage_source", "declared"),
    }


def edge(place_id, feed_id, **kw):
    return {
        "place_id": place_id,
        "feed_id": feed_id,
        "tier": kw.get("tier", "unknown"),
        "service": kw.get("service"),
        "tier_confidence": 0.0,
        "method": "inferred",
        "rehomed_from": [],
        "evidence": {"declared_level": "municipality", "declared_place_id": place_id},
        "curation": None,
        "merged_evidence": [],
        "curation_history": [],
        "classification_fingerprint": None,
        "fingerprint_kind": "none",
        "selector_state": "unavailable",
        "selector": None,
        "needs_review": kw.get("needs_review", True),
        # Schema 7's relevance, when a partitioned fixture sets it.
        "relevance_category": kw.get("relevance_category"),
        "relevance": kw.get("relevance"),
        "cross_border": kw.get("cross_border"),
    }


def place(place_id, kind, *, geometry=None, **kw):
    return {
        "place_id": place_id,
        "kind": kind,
        "source_subtype": kw.get("source_subtype", kind),
        "name": kw.get("name", place_id),
        "names": kw.get("names", {"en": place_id}),
        "aliases": kw.get("aliases", []),
        "resolution_method": kw.get("resolution_method", "overture_wikidata"),
        "default_metro_id": kw.get("default_metro_id"),
        "parent_id": kw.get("parent_id"),
        "country_code": kw.get("country_code", "FI"),
        "overture_id": kw.get("overture_id"),
        "osm_relation_id": None,
        "statistical_area_id": kw.get("statistical_area_id"),
        "metro_ids": kw.get("metro_ids", []),
        "member_ids": kw.get("member_ids", []),
        "geometry": geometry,
        "geometry_source": "overture" if geometry else None,
    }


PLACES = [
    place(
        "Q1757",
        "city",
        geometry=GEOM_HEX,
        names={"en": "Helsinki", "sv": "Helsingfors"},
        aliases=["Stadi"],
        metro_ids=["Q-metro"],
    ),
    place("Q-metro", "metro", member_ids=["Q1757"]),  # no geometry
]

FEEDS_SCHEMA = pa.schema(
    [
        ("feed_id", pa.string()),
        ("onestop_id", pa.string()),
        ("mdb_id", pa.string()),
        ("id_minted", pa.bool_()),
        ("source", pa.string()),
        ("spec", pa.string()),
        ("name", pa.string()),
        ("aliases", pa.list_(pa.string())),
        ("crosswalk_method", pa.string()),
        ("crosswalk_confidence", pa.float64()),
        ("static_feed_id", pa.string()),
        ("static_link_method", pa.string()),
        ("atlas", pa.string()),
        ("mdb", pa.string()),
        ("gbfs", pa.string()),
        ("crawlable", pa.bool_()),
        ("uncrawlable_reason", pa.string()),
        ("coverage_source", pa.string()),
        ("coverage", pa.binary()),
        ("stop_count", pa.int64()),
        ("etag", pa.string()),
        ("last_modified", pa.string()),
        ("last_crawled", pa.string()),
        ("crawl_status", pa.string()),
        ("files", pa.list_(pa.string())),
        ("redistribution_allowed", pa.bool_()),
        ("snapshot", pa.string()),
    ]
)

PLACES_SCHEMA = pa.schema(
    [
        ("place_id", pa.string()),
        ("kind", pa.string()),
        ("source_subtype", pa.string()),
        ("name", pa.string()),
        ("names", pa.map_(pa.string(), pa.string())),
        ("aliases", pa.list_(pa.string())),
        ("default_metro_id", pa.string()),
        ("resolution_method", pa.string()),
        ("curated", pa.bool_()),
        ("parent_id", pa.string()),
        ("metro_ids", pa.list_(pa.string())),
        ("member_ids", pa.list_(pa.string())),
        ("country_code", pa.string()),
        ("overture_id", pa.string()),
        ("osm_relation_id", pa.string()),
        ("statistical_area_id", pa.string()),
        ("geonames_id", pa.string()),
        ("geometry_source", pa.string()),
        ("service", pa.string()),
        ("snapshot", pa.string()),
        ("wikidata_id", pa.string()),
        ("concordances", pa.string()),
        ("former_ids", pa.list_(pa.string())),
        ("geometry", pa.binary()),
    ]
)

EDGES_SCHEMA = pa.schema(
    [
        ("place_id", pa.string()),
        ("feed_id", pa.string()),
        ("tier", pa.string()),
        ("service", pa.string()),
        ("tier_confidence", pa.float64()),
        ("method", pa.string()),
        ("rehomed_from", pa.list_(pa.string())),
        ("evidence", pa.string()),
        ("curation", pa.string()),
        ("merged_evidence", pa.string()),
        ("curation_history", pa.string()),
        ("classification_fingerprint", pa.string()),
        ("fingerprint_kind", pa.string()),
        ("selector_state", pa.string()),
        ("selector", pa.string()),
        ("needs_review", pa.bool_()),
        ("snapshot", pa.string()),
    ]
)


def _json_block(block):
    if block is None:
        return None
    return json.dumps(block, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _feed_row(record, snapshot_id):
    coverage = record.get("coverage")
    return {
        "feed_id": record["feed_id"],
        "onestop_id": record.get("onestop_id"),
        "mdb_id": record.get("mdb_id"),
        "id_minted": record["id_minted"],
        "source": record["source"],
        "spec": record["spec"],
        "name": record.get("name"),
        "aliases": record.get("aliases") or [],
        "crosswalk_method": record["crosswalk_method"],
        "crosswalk_confidence": record["crosswalk_confidence"],
        "static_feed_id": record.get("static_feed_id"),
        "static_link_method": record.get("static_link_method"),
        "atlas": _json_block(record.get("atlas")),
        "mdb": _json_block(record.get("mdb")),
        "gbfs": _json_block(record.get("gbfs")),
        "crawlable": record.get("crawlable"),
        "uncrawlable_reason": record.get("uncrawlable_reason"),
        "coverage_source": record.get("coverage_source"),
        "coverage": None if coverage is None else bytes.fromhex(coverage),
        "stop_count": record.get("stop_count"),
        "etag": record.get("etag"),
        "last_modified": record.get("last_modified"),
        "last_crawled": record.get("last_crawled"),
        "crawl_status": record.get("crawl_status"),
        "files": record.get("files") or [],
        "redistribution_allowed": record.get("redistribution_allowed"),
        "snapshot": snapshot_id,
    }


def _service_by_place(edges):
    """Each place's service summed over the feeds serving it, pairs counted
    once; a number stays null while no feed reports it. Mirrors the publish
    stage's helper so the fixture's place rows match what the build emits."""
    per_pair = {}
    for record in edges:
        per_pair.setdefault(
            (record["place_id"], record["feed_id"]), record.get("service")
        )
    totals = {}
    for (place_id, _), service in per_pair.items():
        total = totals.setdefault(
            place_id,
            {"feeds": 0, "stops": None, "routes": None, "departures_per_day": None},
        )
        total["feeds"] += 1
        for field in ("stops", "routes", "departures_per_day"):
            if (service or {}).get(field) is not None:
                total[field] = (total[field] or 0) + service[field]
    return totals


def _place_row(record, snapshot_id, service=None):
    # A row keyed by its QID states its identity; any other key is a
    # fixture's and states none.
    qid = record["place_id"] if _QID.match(record["place_id"]) else None
    metro_ids = record.get("metro_ids") or []
    geometry = record.get("geometry")
    return {
        "place_id": record["place_id"],
        "kind": record["kind"],
        "source_subtype": record.get("source_subtype"),
        "name": record.get("name"),
        "names": dict(sorted((record.get("names") or {}).items())),
        "aliases": record.get("aliases") or [],
        "default_metro_id": record.get("default_metro_id")
        or (metro_ids[0] if len(metro_ids) == 1 else None),
        "resolution_method": record.get("resolution_method"),
        "curated": bool(record.get("curated", False)),
        "parent_id": record.get("parent_id"),
        "metro_ids": metro_ids,
        "member_ids": record.get("member_ids") or [],
        "country_code": record.get("country_code"),
        "overture_id": record.get("overture_id"),
        "osm_relation_id": record.get("osm_relation_id"),
        "statistical_area_id": record.get("statistical_area_id"),
        "geonames_id": record.get("geonames_id"),
        "geometry_source": record.get("geometry_source"),
        "service": _json_block(service),
        "snapshot": snapshot_id,
        "wikidata_id": qid,
        "concordances": json.dumps({"wikidata": [qid]} if qid else {}, sort_keys=True),
        "former_ids": [],
        "geometry": None if geometry is None else bytes.fromhex(geometry),
    }


def _edge_row(record, snapshot_id):
    return {
        "place_id": record["place_id"],
        "feed_id": record["feed_id"],
        "tier": record["tier"],
        "service": _json_block(record.get("service")),
        "tier_confidence": record["tier_confidence"],
        "method": record["method"],
        "rehomed_from": record.get("rehomed_from") or [],
        "evidence": _json_block(record.get("evidence")),
        "curation": _json_block(record.get("curation")),
        "merged_evidence": _json_block(record.get("merged_evidence") or []),
        "curation_history": _json_block(record.get("curation_history") or []),
        "classification_fingerprint": record.get("classification_fingerprint"),
        "fingerprint_kind": record["fingerprint_kind"],
        "selector_state": record["selector_state"],
        "selector": _json_block(record.get("selector")),
        "needs_review": record["needs_review"],
        "snapshot": snapshot_id,
    }


def _geo_metadata():
    import pyproj

    crs = json.loads(pyproj.CRS.from_epsg(4326).to_json())
    return json.dumps(
        {
            "version": "1.0.0",
            "primary_column": "geometry",
            "columns": {
                "geometry": {"encoding": "WKB", "geometry_types": [], "crs": crs}
            },
        }
    ).encode("utf-8")


def _parquet(rows, schema):
    sink = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), sink)
    return sink.getvalue()


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def write_index(
    directory,
    *,
    feeds=None,
    edges=None,
    places=None,
    snapshot_id=SNAPSHOT_ID,
    notice=NOTICE,
    feeds_only=False,
):
    """Write a published index under ``directory`` and return it.

    The defaults are one declared feed serving Helsinki and its metro, all
    five release members present and the index licensed. With ``feeds_only``
    the index carries neither places nor edges, as a build without a
    gazetteer publishes; ``notice=None`` publishes it unlicensed.
    """
    feeds = [covered_feed("f-a")] if feeds is None else feeds
    edges = [edge("Q1757", "f-a")] if edges is None else edges
    places = PLACES if places is None else places
    directory.mkdir(parents=True, exist_ok=True)
    feeds_data = _parquet([_feed_row(r, snapshot_id) for r in feeds], FEEDS_SCHEMA)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "discovery_semantics_version": reader.DISCOVERY_SEMANTICS_VERSION,
        "min_reader_version": reader.MIN_READER_VERSIONS[SCHEMA_VERSION],
        "built_with": transitio.__version__,
        "snapshot_id": snapshot_id,
        "built_at": "2026-09-01T00:00:00+00:00",
        "counts": {"feeds": len(feeds)},
        "feeds_sha256": _sha256(feeds_data),
        "licensed": notice is not None,
        "notice_sha256": None if notice is None else _sha256(notice),
    }
    (directory / reader.FEEDS_FILE).write_bytes(feeds_data)
    if not feeds_only:
        service = _service_by_place(edges)
        places_data = _parquet(
            [_place_row(r, snapshot_id, service.get(r["place_id"])) for r in places],
            PLACES_SCHEMA.with_metadata({b"geo": _geo_metadata()}),
        )
        edges_data = _parquet([_edge_row(r, snapshot_id) for r in edges], EDGES_SCHEMA)
        manifest["places_sha256"] = _sha256(places_data)
        manifest["edges_sha256"] = _sha256(edges_data)
        manifest["counts"].update(places=len(places), edges=len(edges))
        (directory / reader.PLACES_FILE).write_bytes(places_data)
        (directory / reader.EDGES_FILE).write_bytes(edges_data)
    if notice is not None:
        (directory / "NOTICE").write_bytes(notice)
    (directory / reader.SNAPSHOT_FILE).write_text(
        json.dumps(manifest, indent=2, sort_keys=True)
    )
    return directory


def index(tmp_path):
    """The default index, under ``tmp_path / "index"``."""
    return write_index(tmp_path / "index")


PARTITIONED_SCHEMA_VERSION = 7
FEEDS_SCHEMA_7 = FEEDS_SCHEMA.remove(FEEDS_SCHEMA.get_field_index("snapshot")).append(
    pa.field("home_country", pa.string())
)
for _name, _type in (
    ("country_shares", pa.string()),
    ("scope", pa.string()),
    ("declared_countries", pa.list_(pa.string())),
    ("snapshot", pa.string()),
):
    FEEDS_SCHEMA_7 = FEEDS_SCHEMA_7.append(pa.field(_name, _type))
EDGES_SCHEMA_7 = EDGES_SCHEMA
for _name, _type in (
    ("relevance_category", pa.string()),
    ("relevance", pa.float64()),
    ("cross_border", pa.bool_()),
):
    EDGES_SCHEMA_7 = EDGES_SCHEMA_7.append(pa.field(_name, _type))
LINKS_SCHEMA_7 = EDGES_SCHEMA_7.append(pa.field("feed_partition", pa.string()))
# Schema 8: GTFS only (no GBFS block), each static feed naming its GTFS-RT
# companions, which ride in a realtime table beside the feeds.
FEEDS_SCHEMA_8 = FEEDS_SCHEMA_7.remove(FEEDS_SCHEMA_7.get_field_index("gbfs"))
FEEDS_SCHEMA_8 = FEEDS_SCHEMA_8.insert(
    FEEDS_SCHEMA_8.get_field_index("snapshot"),
    pa.field("realtime_feed_ids", pa.list_(pa.string())),
)
REALTIME_SCHEMA = pa.schema(
    [
        ("feed_id", pa.string()),
        ("onestop_id", pa.string()),
        ("mdb_id", pa.string()),
        ("id_minted", pa.bool_()),
        ("source", pa.string()),
        ("name", pa.string()),
        ("aliases", pa.list_(pa.string())),
        ("crosswalk_method", pa.string()),
        ("crosswalk_confidence", pa.float64()),
        ("static_feed_id", pa.string()),
        ("static_link_method", pa.string()),
        ("urls", pa.string()),
        ("entity_types", pa.list_(pa.string())),
        ("atlas", pa.string()),
        ("mdb", pa.string()),
        ("redistribution_allowed", pa.bool_()),
        ("snapshot", pa.string()),
    ]
)


def realtime_feed(feed_id, static_feed_id, urls=None, **kw):
    """A GTFS-RT companion record for :func:`write_partitioned_index`."""
    urls = (
        {"realtime_trip_updates": f"https://rt.example/{feed_id}"}
        if urls is None
        else urls
    )
    return {
        "feed_id": feed_id,
        "onestop_id": feed_id,
        "id_minted": False,
        "source": kw.get("source", "atlas"),
        "name": kw.get("name"),
        "crosswalk_method": "none",
        "crosswalk_confidence": 0.0,
        "static_feed_id": static_feed_id,
        "static_link_method": kw.get(
            "method", "declared" if static_feed_id else "none"
        ),
        "urls": urls,
        "entity_types": sorted(k.removeprefix("realtime_") for k in urls),
        "redistribution_allowed": kw.get("redistribution_allowed"),
    }


def _realtime_row(record, snapshot_id):
    return {
        "feed_id": record["feed_id"],
        "onestop_id": record.get("onestop_id"),
        "mdb_id": record.get("mdb_id"),
        "id_minted": record.get("id_minted", False),
        "source": record.get("source", "atlas"),
        "name": record.get("name"),
        "aliases": record.get("aliases") or [],
        "crosswalk_method": record.get("crosswalk_method", "none"),
        "crosswalk_confidence": record.get("crosswalk_confidence", 0.0),
        "static_feed_id": record.get("static_feed_id"),
        "static_link_method": record.get("static_link_method"),
        "urls": _json_block(record.get("urls") or {}),
        "entity_types": list(record.get("entity_types") or []),
        "atlas": _json_block(record.get("atlas")),
        "mdb": _json_block(record.get("mdb")),
        "redistribution_allowed": record.get("redistribution_allowed"),
        "snapshot": snapshot_id,
    }


def _feed_row_7(record, snapshot_id):
    row = _feed_row(record, snapshot_id)
    row.update(
        home_country=record.get("home_country"),
        country_shares=_json_block(record.get("country_shares") or {}),
        scope=record.get("scope", "declared"),
        declared_countries=list(record.get("declared_countries") or []),
    )
    return row


def _edge_row_7(record, snapshot_id, partition=None):
    row = _edge_row(record, snapshot_id)
    row.update(
        relevance_category=record.get("relevance_category"),
        relevance=record.get("relevance"),
        cross_border=record.get("cross_border"),
    )
    if partition is not None:
        row["feed_partition"] = partition
    return row


def _counts(feeds, places, edges, realtime, home):
    """The manifest counts: from schema 8 the companions too, linked when
    their static feed is one of the index's."""
    counts = {"feeds": len(feeds), "places": len(places), "edges": len(edges)}
    if realtime is not None:
        linked = sum(1 for r in realtime if r.get("static_feed_id") in home)
        counts.update(
            realtime=len(realtime),
            realtime_linked=linked,
            realtime_unlinked=len(realtime) - linked,
        )
    return counts


def write_partitioned_index(
    directory,
    *,
    feeds,
    places,
    edges,
    snapshot_id=SNAPSHOT_ID,
    notice=NOTICE,
    realtime=None,
):
    """Write a schema-7 index under ``directory``: feeds by ``home_country``
    (``international`` without one), places by ``country_code``, edges under
    the feed's home country when the place lies there, else in ``links`` with
    ``feed_partition``; the manifest lists every table's rows and digest.
    With ``realtime`` (GTFS-RT companion records) a schema-8 index: each
    companion under its static feed's partition (``international`` without
    one in the index), each static feed naming its companions."""
    directory.mkdir(parents=True, exist_ok=True)
    version = PARTITIONED_SCHEMA_VERSION if realtime is None else 8
    home = {feed["feed_id"]: feed.get("home_country") for feed in feeds}
    country = {place["place_id"]: place["country_code"] for place in places}
    companions = {}
    for record in realtime or ():
        companions.setdefault(record.get("static_feed_id"), []).append(
            record["feed_id"]
        )
    parts = {}
    for feed in feeds:
        row = _feed_row_7(feed, snapshot_id)
        if version >= 8:
            del row["gbfs"]
            row["realtime_feed_ids"] = sorted(companions.get(feed["feed_id"], ()))
        parts.setdefault(home[feed["feed_id"]] or "international", {}).setdefault(
            "feeds", []
        ).append(row)
    for record in realtime or ():
        static = record.get("static_feed_id")
        partition = (home.get(static) if static in home else None) or "international"
        parts.setdefault(partition, {}).setdefault("realtime", []).append(
            _realtime_row(record, snapshot_id)
        )
    service = _service_by_place(edges)
    for place in places:
        parts.setdefault(country[place["place_id"]], {}).setdefault(
            "places", []
        ).append(_place_row(place, snapshot_id, service.get(place["place_id"])))
    for record in edges:
        feed_home = home[record["feed_id"]]
        if feed_home is None or feed_home != country[record["place_id"]]:
            row = _edge_row_7(record, snapshot_id, feed_home or "international")
            parts.setdefault("links", {}).setdefault("edges", []).append(row)
        else:
            parts.setdefault(feed_home, {}).setdefault("edges", []).append(
                _edge_row_7(record, snapshot_id)
            )
    listing = {}
    for partition, tables in sorted(parts.items()):
        (directory / partition).mkdir(exist_ok=True)
        listing[partition] = {}
        for table, rows in tables.items():
            if table == "feeds":
                data = _parquet(
                    rows, FEEDS_SCHEMA_8 if version >= 8 else FEEDS_SCHEMA_7
                )
            elif table == "realtime":
                data = _parquet(rows, REALTIME_SCHEMA)
            elif table == "places":
                data = _parquet(
                    rows, PLACES_SCHEMA.with_metadata({b"geo": _geo_metadata()})
                )
            else:
                schema = LINKS_SCHEMA_7 if partition == "links" else EDGES_SCHEMA_7
                data = _parquet(rows, schema)
            (directory / partition / f"{table}.parquet").write_bytes(data)
            listing[partition][table] = {"rows": len(rows), "sha256": _sha256(data)}
    manifest = {
        "schema_version": version,
        "discovery_semantics_version": reader.DISCOVERY_SEMANTICS_VERSION,
        "min_reader_version": reader.MIN_READER_VERSIONS[version],
        "built_with": transitio.__version__,
        "snapshot_id": snapshot_id,
        "built_at": "2026-09-01T00:00:00+00:00",
        "counts": _counts(feeds, places, edges, realtime, home),
        "partitions": listing,
        "licensed": notice is not None,
        "notice_sha256": None if notice is None else _sha256(notice),
    }
    if notice is not None:
        (directory / "NOTICE").write_bytes(notice)
    (directory / reader.SNAPSHOT_FILE).write_text(
        json.dumps(manifest, indent=2, sort_keys=True)
    )
    return directory


def manifest_bytes(**fields):
    manifest = {
        "snapshot_id": SNAPSHOT_ID,
        "schema_version": 4,
        "min_reader_version": "0.11.0",
        **fields,
    }
    return json.dumps(manifest).encode()


def pack(directory):
    """The release assets of the index at ``directory``, in the contract's
    shape: the archive of its members, the archive's checksum and the
    manifest a client reads before downloading anything."""
    snapshot = json.loads((directory / reader.SNAPSHOT_FILE).read_bytes())
    members = [
        (name, (directory / name).read_bytes()) for name in contract.members(snapshot)
    ]
    snapshot_id = snapshot["snapshot_id"]
    name = contract.archive_name(snapshot_id)
    archive = _archive(members)
    manifest = {
        "snapshot_id": snapshot_id,
        "schema_version": snapshot["schema_version"],
        "discovery_semantics_version": snapshot.get("discovery_semantics_version"),
        "min_reader_version": snapshot.get("min_reader_version"),
        "built_with": snapshot.get("built_with"),
        "built_at": snapshot.get("built_at"),
        "counts": snapshot.get("counts"),
        "archive": {"name": name, "sha256": _sha256(archive), "bytes": len(archive)},
        "members": {member: _sha256(data) for member, data in members},
    }
    return {
        name: archive,
        name + contract.CHECKSUM_SUFFIX: f"{_sha256(archive)}  {name}\n".encode(),
        contract.MANIFEST_NAME: (
            json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        ).encode(),
    }


def _archive(members):
    stream = io.BytesIO()
    with gzip.GzipFile(fileobj=stream, mode="wb", mtime=0) as compressed:
        with tarfile.open(
            fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT
        ) as archive:
            for name, data in members:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o644
                info.uname = info.gname = ""
                archive.addfile(info, io.BytesIO(data))
    return stream.getvalue()


def release(fake, directory):
    """Publish the index at ``directory`` into ``fake`` the way a release is
    published, and return its snapshot id."""
    assets = pack(directory)
    snapshot_id = json.loads(assets[contract.MANIFEST_NAME])["snapshot_id"]
    fake.seed(contract.release_tag(snapshot_id), assets)
    return snapshot_id


class FakeGitHub:
    """The slice of the Releases API the publisher and a client touch, with
    GitHub's visibility rules: drafts and their assets need the token."""

    def __init__(self, *, corrupt=None, lose_publish_response=False, on_upload=None):
        self.releases = {}
        self.assets = {}
        self.blobs = {}
        self.corrupt = corrupt
        self.lose_publish_response = lose_publish_response
        self.on_upload = on_upload
        self.clock = 0
        self.published = 0
        self.latest = None

    def transport(self):
        return httpx.MockTransport(self.handle)

    def seed(self, tag, assets, *, draft=False):
        """A release that exists before the publisher runs; index releases
        were published the way the publisher does, never as latest."""
        make_latest = "false" if tag.startswith(contract.TAG_PREFIX) else "true"
        release = self._create(tag, draft, make_latest)
        for name, data in assets.items():
            self._upload(release, name, data)
        return release

    def _create(self, tag, draft, make_latest="true"):
        self.clock += 1
        release_id = 100 + self.clock
        if not draft and make_latest != "false":
            self.latest = release_id
        if not draft:
            self.published += 1
        release = {
            "id": release_id,
            "tag_name": tag,
            "draft": draft,
            "prerelease": False,
            "created_at": f"2026-09-{self.clock:02d}T00:00:00Z",
            "published_at": (
                None if draft else f"2026-10-{self.published:02d}T00:00:00Z"
            ),
            "upload_url": f"{UPLOADS}/repos/o/r/releases/{release_id}/assets{{?name,label}}",
            "assets": [],
        }
        self.releases[release_id] = release
        return release

    def _upload(self, release, name, data):
        asset_id = 1000 + len(self.assets) + 1
        stored = data[:-1] + b"?" if self.corrupt == name else data
        self.blobs[asset_id] = stored
        asset = {
            "id": asset_id,
            "name": name,
            "size": len(stored),
            "url": f"{API}/repos/o/r/releases/assets/{asset_id}",
            "browser_download_url": f"{DOWNLOADS}/{release['tag_name']}/{name}",
        }
        self.assets[asset_id] = (release["id"], asset)
        release["assets"].append(asset)
        return asset

    def handle(self, request):
        authed = "Authorization" in request.headers
        path = request.url.path
        if path.startswith("/repos/old/r/"):
            # A transferred repository: GitHub answers with its new home.
            location = f"{API}/repos/o/r/" + path[len("/repos/old/r/") :]
            return httpx.Response(301, headers={"Location": location})
        # The token reaches the API and upload hosts only: never the object
        # store the asset redirects lead to.
        assert authed == (request.url.host != "objects.example") or (
            request.url.host == "api.example" and not authed
        )
        if request.url.host == "objects.example":
            # The API redirect is a signed capability URL; the browser URL
            # of a draft's asset is not served to anyone.
            if path.startswith("/signed/"):
                return httpx.Response(
                    200, content=self.blobs[int(path.rsplit("/", 1)[1])]
                )
            _, tag, name = path.split("/", 2)
            for release_id, asset in self.assets.values():
                release = self.releases[release_id]
                if release["tag_name"] == tag and asset["name"] == name:
                    if release["draft"]:
                        return httpx.Response(404)
                    return httpx.Response(200, content=self.blobs[asset["id"]])
            return httpx.Response(404)
        if request.url.host == "uploads.example" and request.method == "POST":
            assert authed
            if self.on_upload is not None:
                self.on_upload()
            release_id = int(path.split("/")[-2])
            name = request.url.params["name"]
            asset = self._upload(self.releases[release_id], name, request.content)
            return httpx.Response(201, json=asset)
        if request.method == "GET" and path.startswith("/repos/o/r/releases/tags/"):
            tag = path.rsplit("/", 1)[1]
            for release in self.releases.values():
                if release["tag_name"] == tag and (authed or not release["draft"]):
                    return httpx.Response(200, json=release)
            return httpx.Response(404)
        if request.method == "GET" and path.startswith("/repos/o/r/releases/assets/"):
            asset_id = int(path.rsplit("/", 1)[1])
            release_id, asset = self.assets[asset_id]
            if self.releases[release_id]["draft"] and not authed:
                return httpx.Response(404)
            return httpx.Response(
                302, headers={"Location": f"{DOWNLOADS}/signed/{asset_id}"}
            )
        if request.method == "POST" and path == "/repos/o/r/releases":
            body = json.loads(request.content)
            release = self._create(body["tag_name"], body["draft"])
            return httpx.Response(201, json=release)
        if request.method == "PATCH" and path.startswith("/repos/o/r/releases/"):
            release = self.releases[int(path.rsplit("/", 1)[1])]
            body = json.loads(request.content)
            release["draft"] = body["draft"]
            if not release["draft"] and body.get("make_latest") != "false":
                self.latest = release["id"]
            if not release["draft"] and release.get("published_at") is None:
                self.published += 1
                release["published_at"] = f"2026-10-{self.published:02d}T00:00:00Z"
            if self.lose_publish_response:
                # Applied, but the response never arrived.
                return httpx.Response(502)
            return httpx.Response(200, json=release)
        if request.method == "GET" and path.startswith("/repos/o/r/releases/"):
            release = self.releases.get(int(path.rsplit("/", 1)[1]))
            if release is None or (release["draft"] and not authed):
                return httpx.Response(404)
            return httpx.Response(200, json=release)
        if request.method == "GET" and path == "/repos/o/r/releases":
            listing = [r for r in self.releases.values() if authed or not r["draft"]]
            listing.sort(key=lambda r: r["created_at"], reverse=True)
            return httpx.Response(200, json=listing)
        return httpx.Response(404)
