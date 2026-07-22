"""Editing a routable OSM network and writing it back to a ``*.osm.pbf``.

:class:`OsmEditor` loads the routable network of a local OSM extract with
pyrosm (>= 0.12.0), exposes its nodes and whole ways as GeoDataFrames, edits
them in the OSM data model — coordinates live on nodes, a way is an ordered
list of member node ids — and writes the result back to a re-readable PBF.
"""

from __future__ import annotations

import os
from pathlib import Path

# Columns that carry identity, geometry, topology or OSM metadata, never
# tags; refused as tag keyword arguments so a stray kwarg can't rewrite them.
_RESERVED_COLUMNS = frozenset(
    {
        "id",
        "osm_type",
        "nodes",
        "geometry",
        "lon",
        "lat",
        "u",
        "v",
        "length",
        "tags",
        "timestamp",
        "version",
        "changeset",
        "visible",
    }
)

# The editable network by default: every ``highway`` way (so footways and
# paths are present for walk-path editing) plus the common railways.
_DEFAULT_NETWORK_FILTER = {
    "highway": True,
    "railway": ["tram", "rail", "light_rail", "subway"],
}


class OsmEditor:
    """Edit the routable network of an OSM extract and save it as a PBF.

    Geometry follows the OSM model: node coordinates are authoritative and a
    way is an ordered list of member node ids, so moving a node moves every
    way through it and a way is reshaped through its member list, not its
    (derived) LineString. New elements get provisional negative ids that are
    stable for the life of the editor and become the ids in the written file.

    Parameters
    ----------
    path : str or pathlib.Path
        The source ``*.osm.pbf`` extract.
    network_type : str, optional
        pyrosm network type (``walking``, ``driving``, ``all``, ...). Give
        this to load a single mode instead of the default broad network.
    custom_filter : dict, optional
        pyrosm Overpass-style tag filter selecting the editable network
        (e.g. ``{"highway": True, "railway": ["tram", "rail"]}``), applied
        with ``network_type="all"`` and ``filter_type`` — matching
        :func:`~transitio.edit.snap_to_network`. When neither
        ``custom_filter`` nor ``network_type`` is given, the default is a
        broad network of all ``highway`` ways plus tram/rail/light_rail/
        subway railways, so walk-, road- and rail-editing all have their
        features.
    filter_type : str, default "keep"
        pyrosm filter mode for ``custom_filter``: ``"keep"`` restricts the
        network to the matching ways, ``"remove"`` drops them and keeps the
        rest.

    Notes
    -----
    Ways whose members extend beyond the extract keep their full member list;
    the out-of-extract members have no coordinates here and are re-emitted
    from the source on save. :meth:`save` writes a network-only file
    (``subset_only``), so editing a shared node can never silently deform a
    feature that was not loaded.
    """

    def __init__(
        self, path, *, network_type=None, custom_filter=None, filter_type="keep"
    ):
        from pyrosm import OSM

        self.source = Path(path)
        self._osm = OSM(os.fspath(self.source), keep_node_info=True)
        if custom_filter is None and network_type is None:
            custom_filter = _DEFAULT_NETWORK_FILTER
        if custom_filter is not None:
            # network_type "all" + a keep-filter restricts the network to
            # exactly the matching ways (the idiom used by snap_to_network).
            nodes, edges = self._osm.get_network(
                nodes=True,
                network_type="all",
                custom_filter=custom_filter,
                filter_type=filter_type,
            )
        else:
            nodes, edges = self._osm.get_network(nodes=True, network_type=network_type)
        if nodes is None or edges is None or len(edges) == 0:
            raise ValueError("no routable network in the extract")

        nodes = nodes.copy()
        nodes["osm_type"] = "node"
        # Whole ways: one row per way id, carrying its full ordered member
        # list in `nodes`. The per-segment geometry/u/v are dropped; way
        # geometry is derived from the members on access.
        ways = edges.drop_duplicates(subset="id").copy()
        ways["osm_type"] = "way"
        ways = ways.drop(columns=["u", "v", "length"], errors="ignore").reset_index(
            drop=True
        )
        self._nodes = nodes.reset_index(drop=True)
        self._ways = ways
        self._deletions = set()
        # Ids added in *this* session, tracked explicitly: reopening a file
        # this editor wrote turns its negative ids into ordinary source
        # elements, so the sign alone cannot say what is new.
        self._provisional = set()
        # New elements get provisional negative ids. Start below the lowest
        # id present, so reopening a file this editor wrote never re-issues an
        # existing id.
        lowest = min(
            [0]
            + ([int(self._nodes["id"].min())] if len(self._nodes) else [])
            + ([int(self._ways["id"].min())] if len(self._ways) else [])
        )
        self._next_new_id = min(-1, lowest - 1)

    # -- ids ---------------------------------------------------------------

    def _alloc_id(self):
        new_id = self._next_new_id
        self._next_new_id -= 1
        self._provisional.add(new_id)
        return new_id

    @staticmethod
    def _check_tags(tags):
        reserved = _RESERVED_COLUMNS & set(tags)
        if reserved:
            raise ValueError(
                f"cannot set reserved column(s) as tags: {sorted(reserved)}"
            )

    def _node_coords(self):
        return {
            int(row.id): (row.geometry.x, row.geometry.y)
            for row in self._nodes.itertuples()
            if row.geometry is not None
        }

    # -- views -------------------------------------------------------------

    @property
    def nodes(self):
        """Network nodes as a WGS84 GeoDataFrame copy (``id``, geometry, tags)."""
        frame = self._nodes.copy()
        if "tags" in frame.columns:
            # .copy() shares the mutable tag dicts; give the caller its own.
            frame["tags"] = [
                dict(tag) if isinstance(tag, dict) else tag for tag in frame["tags"]
            ]
        return frame

    @property
    def ways(self):
        """Whole ways as a WGS84 GeoDataFrame copy.

        Geometry is derived from the current member-node coordinates; where a
        way's members leave the extract the line is split at the gap (a
        MultiLineString) rather than bridging non-adjacent nodes. Edit through
        the mutation methods, not this view.
        """
        import geopandas as gpd

        coords = self._node_coords()
        geometry = [
            self._geometry_from_members(members, coords)
            for members in self._ways["nodes"]
        ]
        frame = self._ways.drop(columns=["geometry"], errors="ignore").copy()
        # .copy() shares the mutable member lists and tag dicts; own them.
        frame["nodes"] = [list(members) for members in frame["nodes"]]
        if "tags" in frame.columns:
            frame["tags"] = [
                dict(tag) if isinstance(tag, dict) else tag for tag in frame["tags"]
            ]
        return gpd.GeoDataFrame(frame, geometry=geometry, crs="EPSG:4326")

    @staticmethod
    def _geometry_from_members(members, coords):
        # Contiguous runs of in-extract members become the geometry; a gap
        # (a member with no coordinate) breaks the run so non-adjacent nodes
        # are never bridged into a fabricated segment.
        from shapely.geometry import LineString, MultiLineString

        runs, current = [], []
        for node_id in members:
            if node_id in coords:
                current.append(coords[node_id])
                continue
            if len(current) > 1:
                runs.append(current)
            current = []
        if len(current) > 1:
            runs.append(current)
        if not runs:
            return None
        if len(runs) == 1:
            return LineString(runs[0])
        return MultiLineString([LineString(run) for run in runs])

    def _known_node_ids(self):
        # Every id the network can legitimately reference: loaded nodes plus
        # members already carried by ways (including out-of-extract ones).
        known = {int(node_id) for node_id in self._nodes["id"]}
        for members in self._ways["nodes"]:
            known.update(members)
        return known

    # -- node edits --------------------------------------------------------

    def _node_index(self, node_id):
        index = self._nodes.index[self._nodes["id"] == node_id]
        if len(index) == 0:
            raise ValueError(f"no node {node_id}")
        return index[0]

    def move_node(self, node_id, lon, lat):
        """Move an existing node; every way through it follows."""
        from shapely.geometry import Point

        index = self._node_index(node_id)
        self._nodes.at[index, "geometry"] = Point(lon, lat)
        for column, value in (("lon", lon), ("lat", lat)):
            if column in self._nodes.columns:
                self._nodes.at[index, column] = value
        return self

    def add_node(self, lon, lat, **tags):
        """Add a new node at ``(lon, lat)``; returns its (negative) id."""
        import geopandas as gpd
        import pandas as pd
        from shapely.geometry import Point

        self._check_tags(tags)
        node_id = self._alloc_id()
        row = {"id": node_id, "osm_type": "node", **tags}
        new = gpd.GeoDataFrame([row], geometry=[Point(lon, lat)], crs="EPSG:4326")
        self._nodes = pd.concat([self._nodes, new], ignore_index=True)
        return node_id

    def delete_node(self, node_id):
        """Remove a node and strip it from every way that referenced it."""
        index = self._node_index(node_id)
        self._nodes = self._nodes.drop(index=index).reset_index(drop=True)
        self._record_delete("node", node_id)
        self._strip_node_from_ways(node_id)
        return self

    def _record_delete(self, osm_type, element_id):
        # A provisional element is not in the source, so it only leaves the
        # in-memory state; a source element must be listed for write_pbf.
        if element_id in self._provisional:
            self._provisional.discard(element_id)
        else:
            self._deletions.add((osm_type, int(element_id)))

    def _strip_node_from_ways(self, node_id):
        # Keep the in-memory topology consistent: no way may keep a reference
        # to a removed node (a dangling provisional id would break the save).
        drop = []
        for index in self._ways.index:
            members = self._ways.at[index, "nodes"]
            if node_id not in members:
                continue
            remaining = [n for n in members if n != node_id]
            if len(remaining) < 2 and self._ways.at[index, "id"] in self._provisional:
                drop.append(index)  # a provisional way cannot survive
            else:
                self._ways.at[index, "nodes"] = remaining
        if drop:
            self._ways = self._ways.drop(index=drop).reset_index(drop=True)

    def retag_node(self, node_id, **tags):
        """Set tag columns on a node."""
        self._check_tags(tags)
        index = self._node_index(node_id)
        for key, value in tags.items():
            self._nodes.at[index, key] = value
        return self

    # -- way edits ---------------------------------------------------------

    def _way_index(self, way_id):
        index = self._ways.index[self._ways["id"] == way_id]
        if len(index) == 0:
            raise ValueError(f"no way {way_id}")
        return index[0]

    def add_way(self, node_ids, **tags):
        """Add a new way over ``node_ids`` (existing and/or new); returns its id."""
        import geopandas as gpd
        import pandas as pd

        self._check_tags(tags)
        members = self._checked_members(node_ids)
        way_id = self._alloc_id()
        row = {"id": way_id, "osm_type": "way", "nodes": members, **tags}
        new = gpd.GeoDataFrame([row], geometry=[None], crs="EPSG:4326")
        self._ways = pd.concat([self._ways, new], ignore_index=True)
        return way_id

    def reshape_way(self, way_id, node_ids):
        """Replace a way's ordered member-node list (insert/remove/reorder)."""
        index = self._way_index(way_id)
        self._ways.at[index, "nodes"] = self._checked_members(node_ids)
        return self

    def _checked_members(self, node_ids):
        members = [int(n) for n in node_ids]
        if len(members) < 2:
            raise ValueError("a way needs at least two nodes")
        unknown = set(members) - self._known_node_ids()
        if unknown:
            raise ValueError(f"unknown node id(s): {sorted(unknown)}")
        return members

    def delete_way(self, way_id):
        """Remove a way. Its orphaned nodes are dropped on save."""
        index = self._way_index(way_id)
        self._ways = self._ways.drop(index=index).reset_index(drop=True)
        self._record_delete("way", way_id)
        return self

    def retag_way(self, way_id, **tags):
        """Set tag columns on a way."""
        self._check_tags(tags)
        index = self._way_index(way_id)
        for key, value in tags.items():
            self._ways.at[index, key] = value
        return self

    # -- save --------------------------------------------------------------

    def save(self, path):
        """Write the edited network to a re-readable ``*.osm.pbf``.

        The output is network-only (``write_pbf`` ``subset_only``): just the
        edited network and the nodes it references, not the whole source
        extract. This keeps the file a routing network and ensures editing a
        node shared with a feature that was not loaded can never deform it.

        Parameters
        ----------
        path : str or pathlib.Path
            Output PBF.

        Returns
        -------
        pathlib.Path
            The written path.
        """
        path = Path(path)
        staging = path.with_name(path.name + ".part")
        for target in (path, staging):
            if target.is_symlink():
                raise ValueError(f"{target} is a symlink; refusing to follow it")
        # Write to a sibling then rename, so a failed write never truncates an
        # existing file and saving over the source stays safe.
        try:
            self._osm.write_pbf(
                [self._nodes, self._ways_for_write()],
                os.fspath(staging),
                apply_geometry=True,
                delete=sorted(self._deletions),
                subset_only=True,
            )
        except BaseException:
            staging.unlink(missing_ok=True)
            raise
        os.replace(staging, path)
        return path

    def _ways_for_write(self):
        # write_pbf derives way geometry from the `nodes` member lists (and
        # the node Points), so the ways frame carries topology, not geometry.
        import geopandas as gpd

        frame = self._ways.drop(columns=["geometry"], errors="ignore").copy()
        return gpd.GeoDataFrame(frame, geometry=[None] * len(frame), crs="EPSG:4326")
