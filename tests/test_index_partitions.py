"""Schema 7: a partitioned index read whole, per country, and its links."""

import hashlib
import json

import pytest

pytest.importorskip("geopandas")

import transitio  # noqa: E402
from transitio import index as reader  # noqa: E402
from transitio.exceptions import IncompatibleIndexError  # noqa: E402
from transitio.index import _refresh, release as contract  # noqa: E402
from index_fixture import (  # noqa: E402
    covered_feed,
    edge,
    pack,
    place,
    write_index,
    write_partitioned_index,
)

PLACES = [
    place("hel", "city", country_code="FI", name="Helsinki"),
    place("tku", "city", country_code="FI", name="Turku"),
    place("tll", "city", country_code="EE", name="Tallinn"),
]
FEEDS = [
    {**covered_feed("f-hsl"), "home_country": "FI", "scope": "domestic"},
    {**covered_feed("f-tlt"), "home_country": "EE", "scope": "domestic"},
    {**covered_feed("f-ferry"), "home_country": None, "scope": "international"},
]
EDGES = [
    edge(
        "hel",
        "f-hsl",
        tier="local",
        relevance_category="primary",
        relevance=0.9,
        cross_border=False,
    ),
    edge(
        "tku",
        "f-hsl",
        tier="regional",
        relevance_category="secondary",
        relevance=0.2,
        cross_border=False,
    ),
    edge(
        "tll",
        "f-tlt",
        tier="local",
        relevance_category="primary",
        relevance=0.8,
        cross_border=False,
    ),
    # The Estonian feed reaching Helsinki, and the ferry everywhere: links.
    edge(
        "hel",
        "f-tlt",
        tier="international",
        relevance_category="international",
        relevance=0.1,
        cross_border=True,
    ),
    edge(
        "hel",
        "f-ferry",
        tier="international",
        relevance_category="international",
        relevance=0.3,
        cross_border=True,
    ),
    edge(
        "tll",
        "f-ferry",
        tier="international",
        relevance_category="international",
        relevance=0.3,
        cross_border=True,
    ),
]


@pytest.fixture(autouse=True)
def _reader_reads_schema_7(monkeypatch):
    # The release carrying schema 7 is the reader floor the fixture records.
    monkeypatch.setattr(
        transitio, "__version__", reader.MIN_READER_VERSIONS[7], raising=False
    )


def _partitioned(tmp_path):
    return write_partitioned_index(
        tmp_path / "index", feeds=FEEDS, places=PLACES, edges=EDGES
    )


def test_a_partitioned_index_reads_whole_and_per_country(tmp_path):
    directory = _partitioned(tmp_path)
    listing = json.loads((directory / "snapshot.json").read_text())["partitions"]
    assert set(listing) == {"EE", "FI", "international", "links"}
    assert listing["links"]["edges"]["rows"] == 3 and "places" not in listing["links"]
    # Whole: the flat tables, cross-border edges included, and the links kept.
    index = reader.read_index(directory)
    assert index.schema_version == 7 and index.country is None
    assert sorted(index.feeds["feed_id"]) == ["f-ferry", "f-hsl", "f-tlt"]
    assert sorted(index.places["place_id"]) == ["hel", "tku", "tll"]
    assert len(index.edges) == 6 and "feed_partition" not in index.edges.columns
    assert list(index.links["feed_partition"]) == [
        "EE",
        "international",
        "international",
    ]
    assert index.places.crs is not None
    # One country: its feeds, places and domestic edges, and the links into it.
    finland = reader.read_index(directory, country="FI")
    assert list(finland.feeds["feed_id"]) == ["f-hsl"]
    assert sorted(finland.places["place_id"]) == ["hel", "tku"]
    assert sorted(finland.edges["tier"]) == ["local", "regional"]
    assert sorted(finland.links["feed_id"]) == ["f-ferry", "f-tlt"]
    assert finland.country == "FI"
    # The feeds a link refers to come from their own partition, read once.
    assert list(finland.feeds_in("EE")["feed_id"]) == ["f-tlt"]
    assert list(finland.feeds_in("international")["feed_id"]) == ["f-ferry"]
    assert finland.feeds_in("EE") is finland.feeds_in("EE")
    with pytest.raises(IncompatibleIndexError, match="no feeds partition"):
        finland.feeds_in("links")
    with pytest.raises(IncompatibleIndexError, match="no country partition"):
        reader.read_index(directory, country="SE")
    with pytest.raises(IncompatibleIndexError, match="no country partition"):
        reader.read_index(directory, country="links")
    assert reader.links(directory).equals(index.links)
    # A place resolves and its feeds come through the joined edges as before;
    # loaded for one country, the links' feeds are not among them.
    helsinki = reader.place("hel", index=index)
    everything = helsinki.feeds(spec=None, categories=None, international=True)
    assert {f.feed_id for f in everything} == {"f-hsl", "f-tlt", "f-ferry"}
    finnish = reader.place("hel", index=finland)
    assert [f.feed_id for f in finnish.feeds(spec=None, categories=None)] == ["f-hsl"]
    assert reader.load(directory, country="EE").country == "EE"


def test_a_partition_table_is_checked_like_a_flat_one(tmp_path):
    directory = _partitioned(tmp_path)
    snapshot_path = directory / "snapshot.json"
    original = snapshot_path.read_text()
    # A digest mismatch on one partition file.
    published = (directory / "EE" / "feeds.parquet").read_bytes()
    (directory / "EE" / "feeds.parquet").write_bytes(b"not the published parquet")
    with pytest.raises(IncompatibleIndexError, match="sha256"):
        reader.read_index(directory)
    (directory / "EE" / "feeds.parquet").write_bytes(published)
    # A links table without feed_partition, correctly hashed, is refused.
    import pyarrow.parquet as pq

    table = pq.read_table(directory / "links" / "edges.parquet").drop_columns(
        ["feed_partition"]
    )
    pq.write_table(table, directory / "links" / "edges.parquet")
    snapshot = json.loads(original)
    snapshot["partitions"]["links"]["edges"]["sha256"] = hashlib.sha256(
        (directory / "links" / "edges.parquet").read_bytes()
    ).hexdigest()
    snapshot_path.write_text(json.dumps(snapshot))
    with pytest.raises(IncompatibleIndexError, match="missing \\['feed_partition'\\]"):
        reader.read_index(directory)
    # A manifest with a partition name outside the layout, or none, is refused.
    snapshot = json.loads(original)
    snapshot["partitions"]["../x"] = {"feeds": {"sha256": "0" * 64}}
    snapshot_path.write_text(json.dumps(snapshot))
    with pytest.raises(IncompatibleIndexError, match="unexpected partition name"):
        reader.read_index(directory)
    snapshot = json.loads(original)
    del snapshot["partitions"]
    snapshot_path.write_text(json.dumps(snapshot))
    with pytest.raises(IncompatibleIndexError, match="declares no partitions"):
        reader.read_index(directory)
    # Tables outside a partition kind's layout, or more rows than one table
    # may hold, are refused before any file is read.
    snapshot = json.loads(original)
    snapshot["partitions"]["links"]["feeds"] = snapshot["partitions"]["FI"]["feeds"]
    snapshot_path.write_text(json.dumps(snapshot))
    with pytest.raises(IncompatibleIndexError, match="tables outside the layout"):
        reader.read_index(directory)
    snapshot = json.loads(original)
    snapshot["partitions"]["FI"]["edges"]["rows"] = reader._MAX_TABLE_ROWS
    snapshot_path.write_text(json.dumps(snapshot))
    with pytest.raises(IncompatibleIndexError, match="more than .* rows"):
        reader.read_index(directory)
    # A row count the table does not have, or a symlinked partition directory.
    snapshot = json.loads(original)
    snapshot["partitions"]["FI"]["edges"]["rows"] = 99
    snapshot_path.write_text(json.dumps(snapshot))
    with pytest.raises(IncompatibleIndexError, match="rows where the snapshot lists"):
        reader.read_index(directory)
    snapshot_path.write_text(original)
    real = (directory / "EE").rename(directory / "EE-real")
    try:
        (directory / "EE").symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this platform cannot create symlinks")
    with pytest.raises(IncompatibleIndexError, match="not a plain directory"):
        reader.read_index(directory)


def test_a_flat_schema_6_index_reads_through_the_same_reader(tmp_path):
    directory = write_index(tmp_path / "flat")
    index = reader.read_index(directory)
    assert index.schema_version == 6 and index.links is None
    assert index.country is None and index.partitions == {}
    with pytest.raises(ValueError, match="no country partitions"):
        reader.read_index(directory, country="FI")


def test_the_release_members_follow_the_snapshot(tmp_path):
    directory = _partitioned(tmp_path)
    snapshot = json.loads((directory / "snapshot.json").read_text())
    expected = [
        "snapshot.json",
        "EE/feeds.parquet",
        "EE/places.parquet",
        "EE/edges.parquet",
        "FI/feeds.parquet",
        "FI/places.parquet",
        "FI/edges.parquet",
        "international/feeds.parquet",
        "links/edges.parquet",
        "NOTICE",
    ]
    assert contract.members(snapshot) == expected
    assert contract.members({"schema_version": 6}) == list(contract.MEMBERS)
    with pytest.raises(ValueError, match="no places partition"):
        contract.members({"schema_version": 7, "partitions": {"FI": {"feeds": {}}}})
    with pytest.raises(ValueError, match="outside the layout"):
        contract.members({"schema_version": 7, "partitions": {"../x": {}}})
    with pytest.raises(ValueError, match="outside the layout"):
        contract.members(
            {"schema_version": 7, "partitions": {"links": {"feeds": {}, "edges": {}}}}
        )
    assert reader.links(directory) is not None
    assert reader.links(write_index(tmp_path / "flat")) is None
    # The packed archive unpacks into a staging directory that reads whole.
    assets = pack(directory)
    archive = assets[contract.archive_name(snapshot["snapshot_id"])]
    staging = tmp_path / "staging"
    staging.mkdir()
    _refresh._unpack(archive, staging)
    assert _refresh._whole_members(staging)
    assert reader.read_index(staging).snapshot_id == snapshot["snapshot_id"]
    # An archive missing a listed partition file, or holding an unlisted
    # member, is refused.
    members = [(name, (directory / name).read_bytes()) for name in expected]
    from index_fixture import _archive

    short = _archive([m for m in members if m[0] != "FI/edges.parquet"])
    (tmp_path / "s1").mkdir()
    with pytest.raises(Exception, match="lacks FI/edges.parquet"):
        _refresh._unpack(short, tmp_path / "s1")
    extra = _archive(members + [("SE/feeds.parquet", members[1][1])])
    (tmp_path / "s2").mkdir()
    with pytest.raises(Exception, match="does not list: SE/feeds.parquet"):
        _refresh._unpack(extra, tmp_path / "s2")
