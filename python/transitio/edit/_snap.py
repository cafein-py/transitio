"""Snapping route geometries to the OSM street network."""

from __future__ import annotations

import os


def snap_to_network(
    waypoints, pbf, *, network_type="driving", custom_filter=None, filter_type="keep"
):
    """Route a waypoint sequence along an OSM network.

    Loads the network from an OSM extract (as fetched by
    :func:`transitio.fetch_pbf`), snaps each waypoint to its nearest
    network node, and connects consecutive waypoints with the shortest
    network path — the mechanism behind snapped route drawing for bus,
    tram and rail alignments.

    Parameters
    ----------
    waypoints : sequence of (lat, lon)
        At least two points, in visit order.
    pbf : str or pathlib.Path
        OSM ``.osm.pbf`` extract covering the waypoints.
    network_type : str, default "driving"
        pyrosm network type (``walking``, ``cycling``, ``driving``,
        ``driving+service``, ``all``). Ignored when ``custom_filter`` is
        given.
    custom_filter : dict, optional
        A pyrosm Overpass-style tag filter selecting which OSM ways form
        the routing network, e.g. ``{"railway": ["tram"]}`` to snap tram
        alignments to their rails or ``{"railway": ["rail",
        "light_rail"]}`` for heavy rail. When given, the network is
        restricted to exactly the matching ways (pyrosm ``network_type``
        ``"all"`` with ``filter_type``); when ``None``, the plain
        ``network_type`` network is used.
    filter_type : str, default "keep"
        pyrosm filter mode for ``custom_filter`` (``"keep"`` or
        ``"exclude"``).

    Returns
    -------
    shapely.LineString
        The snapped alignment in WGS84 (x = longitude, y = latitude).

    Notes
    -----
    Requires the ``networkx`` package (install ``transitio[snap]``).
    Snapping tram or rail routes needs an extract that actually contains
    the ``railway`` ways (some cropped extracts drop them); without a
    ``custom_filter`` the driving network carries street-running trams
    but not dedicated rails.
    """
    try:
        import networkx as nx
    except ImportError as error:
        raise ImportError(
            "snap_to_network requires networkx; install transitio[snap]"
        ) from error
    from pyrosm import OSM
    from shapely.geometry import LineString, Point
    from shapely.strtree import STRtree

    waypoints = list(waypoints)
    if len(waypoints) < 2:
        raise ValueError("need at least two waypoints")

    from pyproj import Transformer

    osm = OSM(os.fspath(pbf))
    if custom_filter is not None:
        # network_type "all" + a keep-filter restricts the network to
        # exactly the matching ways (unlike a bare custom_filter, which
        # pyrosm unions with the network_type base).
        nodes, edges = osm.get_network(
            nodes=True,
            network_type="all",
            custom_filter=custom_filter,
            filter_type=filter_type,
        )
        described = f"custom_filter {custom_filter}"
    else:
        nodes, edges = osm.get_network(nodes=True, network_type=network_type)
        described = f"{network_type} network"
    if nodes is None or edges is None or nodes.empty or edges.empty:
        raise ValueError(f"no {described} in {pbf}")
    graph = osm.to_graph(nodes, edges, graph_type="networkx")

    # Nearest-node search in a locally metric, antimeridian-safe frame:
    # an azimuthal equidistant projection centered on the network.
    center_lon = float(nodes["lon"].iloc[0])
    center_lat = float(nodes["lat"].iloc[0])
    project = Transformer.from_crs(
        "EPSG:4326",
        f"+proj=aeqd +lat_0={center_lat} +lon_0={center_lon} +datum=WGS84",
        always_xy=True,
    ).transform
    xs, ys = project(nodes["lon"].to_numpy(), nodes["lat"].to_numpy())
    tree = STRtree([Point(x, y) for x, y in zip(xs, ys)])
    ids = nodes["id"].to_numpy()

    snapped = [ids[tree.nearest(Point(*project(lon, lat)))] for lat, lon in waypoints]

    def edge_coordinates(source, target):
        """The full geometry of one traversed edge, oriented source→target."""
        data = graph.get_edge_data(source, target) or {}
        if data and "geometry" not in data:  # multigraph: {key: attrs}
            data = min(data.values(), key=lambda attrs: attrs.get("length", 0.0))
        geometry = data.get("geometry")
        head = (graph.nodes[source]["x"], graph.nodes[source]["y"])
        tail = (graph.nodes[target]["x"], graph.nodes[target]["y"])
        if geometry is None:
            return [head, tail]
        segment = list(geometry.coords)
        starts_at_tail = (segment[0][0] - tail[0]) ** 2 + (
            segment[0][1] - tail[1]
        ) ** 2 < (segment[0][0] - head[0]) ** 2 + (segment[0][1] - head[1]) ** 2
        if starts_at_tail:
            segment.reverse()
        return segment

    coordinates = []
    for source, target in zip(snapped, snapped[1:]):
        try:
            path = nx.shortest_path(graph, source, target, weight="length")
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            raise ValueError(
                f"no network path between waypoints near nodes "
                f"{source} and {target}"
            ) from None
        for hop_source, hop_target in zip(path, path[1:]):
            for coordinate in edge_coordinates(hop_source, hop_target):
                if not coordinates or coordinates[-1] != coordinate:
                    coordinates.append(coordinate)
    if len(coordinates) < 2:
        raise ValueError("snapped path collapsed to a single point")
    return LineString(coordinates)
