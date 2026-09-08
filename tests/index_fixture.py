"""A published feed index, written in the reader's shape.

The reader's tests and the pipeline's need an index to read. This module
writes one directly -- the members of a release in the layout
``transitio.index`` reads -- so those tests depend on the reader alone,
never on the build that produces real indexes. The row shapes mirror the
build's publish stage column for column; a test in the build's suite checks
the two stay in step.
"""

import hashlib
import io
import json
import re

import pyarrow as pa
import pyarrow.parquet as pq
import shapely

import transitio
import transitio.index as reader

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
        "needs_review": True,
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


def write_index(directory, *, feeds=None, edges=None, places=None):
    """Write a published index under ``directory`` and return it.

    The defaults are one declared feed serving Helsinki and its metro, all
    five release members present and the index licensed.
    """
    feeds = [covered_feed("f-a")] if feeds is None else feeds
    edges = [edge("Q1757", "f-a")] if edges is None else edges
    places = PLACES if places is None else places
    directory.mkdir(parents=True, exist_ok=True)
    feeds_data = _parquet([_feed_row(r, SNAPSHOT_ID) for r in feeds], FEEDS_SCHEMA)
    places_data = _parquet(
        [_place_row(r, SNAPSHOT_ID) for r in places],
        PLACES_SCHEMA.with_metadata({b"geo": _geo_metadata()}),
    )
    edges_data = _parquet([_edge_row(r, SNAPSHOT_ID) for r in edges], EDGES_SCHEMA)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "discovery_semantics_version": reader.DISCOVERY_SEMANTICS_VERSION,
        "min_reader_version": reader.MIN_READER_VERSIONS[SCHEMA_VERSION],
        "built_with": transitio.__version__,
        "snapshot_id": SNAPSHOT_ID,
        "built_at": "2026-09-01T00:00:00+00:00",
        "counts": {"feeds": len(feeds), "places": len(places), "edges": len(edges)},
        "feeds_sha256": _sha256(feeds_data),
        "places_sha256": _sha256(places_data),
        "edges_sha256": _sha256(edges_data),
        "licensed": True,
        "notice_sha256": _sha256(NOTICE),
    }
    (directory / reader.FEEDS_FILE).write_bytes(feeds_data)
    (directory / reader.PLACES_FILE).write_bytes(places_data)
    (directory / reader.EDGES_FILE).write_bytes(edges_data)
    (directory / "NOTICE").write_bytes(NOTICE)
    (directory / reader.SNAPSHOT_FILE).write_text(
        json.dumps(manifest, indent=2, sort_keys=True)
    )
    return directory
