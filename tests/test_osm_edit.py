import pytest

pytest.importorskip("pyrosm")

from pyrosm import OSM, get_data  # noqa: E402

from transitio.edit import OsmEditor  # noqa: E402


@pytest.fixture
def pbf():
    return get_data("test_pbf")


def _editor(pbf):
    return OsmEditor(pbf, network_type="driving")


def _reread(path):
    """Nodes and whole ways of a saved PBF, deduped by way id."""
    osm = OSM(str(path), keep_node_info=True)
    nodes, edges = osm.get_network("all", nodes=True)
    return nodes, edges.drop_duplicates(subset="id")


def test_load_exposes_nodes_and_ways(pbf):
    editor = _editor(pbf)
    assert len(editor.nodes) > 0
    ways = editor.ways
    assert len(ways) > 0
    assert "nodes" in ways.columns
    line = ways[ways.geometry.notna()].iloc[0].geometry
    assert line.geom_type == "LineString"


def test_move_node_roundtrip(pbf, tmp_path):
    editor = _editor(pbf)
    node_id = int(editor.nodes.iloc[0]["id"])
    editor.move_node(node_id, 24.9400, 60.1700)
    nodes, _ = _reread(editor.save(tmp_path / "moved.osm.pbf"))
    point = nodes.loc[nodes["id"] == node_id, "geometry"].iloc[0]
    assert point.x == pytest.approx(24.9400) and point.y == pytest.approx(60.1700)


def test_add_node_and_way_roundtrip(pbf, tmp_path):
    editor = _editor(pbf)
    a, b = int(editor.nodes.iloc[0]["id"]), int(editor.nodes.iloc[1]["id"])
    new_node = editor.add_node(24.9500, 60.1800)
    way_id = editor.add_way([a, new_node, b], highway="footway")
    assert new_node < 0 and way_id < 0

    nodes, ways = _reread(editor.save(tmp_path / "added.osm.pbf"))
    # the new node is written as an actual node row at its coordinates
    point = nodes.loc[nodes["id"] == new_node, "geometry"]
    assert len(point) == 1 and point.iloc[0].x == pytest.approx(24.9500)
    row = ways[ways["id"] == way_id]
    assert len(row) == 1
    members = list(row.iloc[0]["nodes"])
    assert a in members and b in members and new_node in members  # existing ids reused
    assert row.iloc[0].get("highway") == "footway"


def test_reshape_way_inserts_vertex(pbf, tmp_path):
    editor = _editor(pbf)
    way_id = int(editor.ways.iloc[0]["id"])
    members = list(editor.ways[editor.ways["id"] == way_id].iloc[0]["nodes"])
    new_node = editor.add_node(24.9600, 60.1900)
    editor.reshape_way(way_id, members[:1] + [new_node] + members[1:])

    _, ways = _reread(editor.save(tmp_path / "reshaped.osm.pbf"))
    after = list(ways[ways["id"] == way_id].iloc[0]["nodes"])
    assert new_node in after
    assert len(after) == len(members) + 1


def test_delete_way_roundtrip(pbf, tmp_path):
    editor = _editor(pbf)
    way_id = int(editor.ways.iloc[1]["id"])
    editor.delete_way(way_id)
    _, ways = _reread(editor.save(tmp_path / "deleted.osm.pbf"))
    assert not (ways["id"] == way_id).any()


def test_delete_way_drops_orphans_keeps_shared(pbf):
    editor = _editor(pbf)
    a = editor.add_node(24.9500, 60.1800)  # exclusive to way 1
    shared = editor.add_node(24.9600, 60.1900)
    b = editor.add_node(24.9700, 60.2000)  # exclusive to way 2
    way1 = editor.add_way([a, shared], highway="footway")
    editor.add_way([shared, b], highway="footway")  # way 2 also uses `shared`
    editor.delete_way(way1)
    ids = set(editor.nodes["id"])
    assert a not in ids  # orphaned by the deleted way -> dropped in memory
    assert shared in ids and b in ids  # still referenced by way 2 -> kept


def test_retag_way_roundtrip(pbf, tmp_path):
    editor = _editor(pbf)
    way_id = int(editor.ways.iloc[0]["id"])
    editor.retag_way(way_id, maxspeed="30")
    _, ways = _reread(editor.save(tmp_path / "retagged.osm.pbf"))
    assert ways.loc[ways["id"] == way_id, "maxspeed"].iloc[0] == "30"


def test_default_network_is_broad(pbf):
    editor = OsmEditor(pbf)  # no network_type -> broad highways + railways
    highways = set(editor.ways["highway"].dropna())
    assert {"footway", "path"} & highways  # walking ways are present


def test_reserved_tag_keys_rejected(pbf):
    editor = _editor(pbf)
    way_id = int(editor.ways.iloc[0]["id"])
    node_id = int(editor.nodes.iloc[0]["id"])
    with pytest.raises(ValueError, match="reserved column"):
        editor.add_node(24.9, 60.1, id=5)
    with pytest.raises(ValueError, match="reserved column"):
        editor.add_way([node_id, node_id], nodes=[1, 2])
    with pytest.raises(ValueError, match="reserved column"):
        editor.retag_way(way_id, geometry="x")
    with pytest.raises(ValueError, match="reserved column"):
        editor.retag_node(node_id, timestamp="x")  # metadata, not a tag


def test_delete_reopened_new_element_roundtrips(pbf, tmp_path):
    # A negative id written by the editor becomes a source id on reopen;
    # deleting it must still record a deletion so write_pbf removes it.
    editor = _editor(pbf)
    a, b = int(editor.nodes.iloc[0]["id"]), int(editor.nodes.iloc[1]["id"])
    mid = editor.add_node(24.9500, 60.1800)
    way_id = editor.add_way([a, mid, b], highway="footway")
    out = editor.save(tmp_path / "v1.osm.pbf")

    reopened = OsmEditor(out, network_type="all")
    assert (reopened.ways["id"] == way_id).any()  # persisted negative id
    reopened.delete_way(way_id)
    _, ways = _reread(reopened.save(tmp_path / "v2.osm.pbf"))
    assert not (ways["id"] == way_id).any()  # actually deleted


def test_delete_node_strips_incident_ways(pbf, tmp_path):
    editor = _editor(pbf)
    a, b = int(editor.nodes.iloc[0]["id"]), int(editor.nodes.iloc[1]["id"])
    mid = editor.add_node(24.9500, 60.1800)
    way_id = editor.add_way([a, mid, b], highway="footway")
    editor.delete_node(mid)
    # the mid node is gone from the way's member list in memory
    members = list(editor.ways[editor.ways["id"] == way_id].iloc[0]["nodes"])
    assert mid not in members and members == [a, b]
    # and the save (which would otherwise dangle on the provisional id) works
    _, ways = _reread(editor.save(tmp_path / "stripped.osm.pbf"))
    assert mid not in list(ways[ways["id"] == way_id].iloc[0]["nodes"])


def test_reopen_allocates_below_existing_negative_ids(pbf, tmp_path):
    editor = _editor(pbf)
    a, b = int(editor.nodes.iloc[0]["id"]), int(editor.nodes.iloc[1]["id"])
    first = editor.add_node(24.9500, 60.1800)
    editor.add_way([a, first, b], highway="footway")
    out = editor.save(tmp_path / "with_new.osm.pbf")

    reopened = OsmEditor(out, network_type="all")
    existing = set(reopened.nodes["id"]) | set(reopened.ways["id"])
    assert first in existing  # the provisional id persisted
    new_id = reopened.add_node(24.9600, 60.1900)
    assert new_id not in existing  # never re-issues an existing id


def test_delete_existing_node_roundtrip(pbf, tmp_path):
    editor = _editor(pbf)
    node_id = int(editor.nodes.iloc[0]["id"])  # a positive, source id
    editor.delete_node(node_id)
    nodes, ways = _reread(editor.save(tmp_path / "node_deleted.osm.pbf"))
    assert not (nodes["id"] == node_id).any()
    assert all(node_id not in members for members in ways["nodes"])


def test_retag_node_roundtrip(pbf, tmp_path):
    editor = _editor(pbf)
    node_id = int(editor.nodes.iloc[0]["id"])
    editor.retag_node(node_id, highway="crossing")
    nodes, _ = _reread(editor.save(tmp_path / "node_retag.osm.pbf"))
    # get_network keeps node tags in the `tags` dict, not a promoted column.
    tags = nodes.loc[nodes["id"] == node_id, "tags"].iloc[0]
    assert tags.get("highway") == "crossing"


def test_add_and_reshape_reject_unknown_nodes(pbf):
    editor = _editor(pbf)
    a = int(editor.nodes.iloc[0]["id"])
    way_id = int(editor.ways.iloc[0]["id"])
    with pytest.raises(ValueError, match="unknown node id"):
        editor.add_way([a, 999999999])
    with pytest.raises(ValueError, match="unknown node id"):
        editor.reshape_way(way_id, [a, 999999999])


def test_ways_view_is_an_independent_copy(pbf):
    editor = _editor(pbf)
    way_id = int(editor.ways.iloc[0]["id"])
    members = editor.ways[editor.ways["id"] == way_id].iloc[0]["nodes"]
    members.append(-99999)  # mutate the returned copy
    internal = editor.ways[editor.ways["id"] == way_id].iloc[0]["nodes"]
    assert -99999 not in internal  # internal topology untouched


def test_unknown_ids_and_short_ways_raise(pbf):
    editor = _editor(pbf)
    with pytest.raises(ValueError, match="no node"):
        editor.move_node(999999999, 24.9, 60.1)
    with pytest.raises(ValueError, match="no way"):
        editor.delete_way(999999999)
    with pytest.raises(ValueError, match="at least two nodes"):
        editor.add_way([int(editor.nodes.iloc[0]["id"])])


def test_empty_network_raises(pbf):
    with pytest.raises(ValueError, match="no routable network"):
        OsmEditor(pbf, custom_filter={"aerialway": ["zip_line"]})


def _two_waypoints(editor):
    a = editor.nodes.iloc[0].geometry
    b = editor.nodes.iloc[5].geometry
    return [(a.y, a.x), (b.y, b.x)]


def test_snap_routes_along_edited_network(pbf):
    pytest.importorskip("networkx")
    editor = _editor(pbf)
    line = editor.snap(_two_waypoints(editor))
    assert line.geom_type == "LineString"
    assert len(line.coords) >= 2


def test_snap_cache_invalidates_on_edit(pbf):
    pytest.importorskip("networkx")
    editor = _editor(pbf)
    waypoints = _two_waypoints(editor)
    editor.snap(waypoints)
    assert editor._network_dirty is False  # materialized and cached
    editor.snap(waypoints)
    assert editor._network_dirty is False  # reused, not rebuilt
    node = editor.nodes.iloc[0]
    editor.move_node(int(node["id"]), node.geometry.x + 1e-4, node.geometry.y + 1e-4)
    assert editor._network_dirty is True  # an edit invalidates the cache
    editor.snap(waypoints)
    assert editor._network_dirty is False  # rebuilt from the edited network


def test_snap_custom_filter_narrows(pbf):
    pytest.importorskip("networkx")
    editor = _editor(pbf)
    # narrowing to a subset absent from the network yields no path
    with pytest.raises(ValueError):
        editor.snap(_two_waypoints(editor), custom_filter={"aerialway": ["zip_line"]})
