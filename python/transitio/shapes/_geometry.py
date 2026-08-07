"""Shared geometry helpers for shape inference."""

import numpy as np
import shapely

SNAP_TOLERANCE = 100.0
"""Maximum stop-to-geometry distance, in meters — a stop farther than
this from a candidate alignment does not lie on it."""


def measures(line):
    """Cumulative meters at each vertex of a projected LineString."""
    coordinates = shapely.get_coordinates(line)
    hops = np.hypot(*np.diff(coordinates, axis=0).T)
    return np.concatenate([[0.0], np.cumsum(hops)]).tolist()


def locate_on_shape(line, latlon, transformer):
    """The stops' absolute positions along a projected shape, validated:
    every stop must lie near the shape, and the positions must be
    monotone (guards against self-intersecting shapes assigning stops
    out of order)."""
    x, y = transformer.transform(latlon[:, 1], latlon[:, 0])
    points = shapely.points(x, y)
    offsets = shapely.distance(line, points)
    if not np.isfinite(offsets).all() or (offsets > SNAP_TOLERANCE).any():
        return None
    along = shapely.line_locate_point(line, points)
    if not np.isfinite(along).all():
        return None
    # A loop route ends where it began: its final stop locates at the
    # line's start, which is the end of a completed circuit rather than
    # a backwards jump. This only applies when the LINE itself closes
    # and its end is that same place — otherwise the pattern simply
    # turns back near its origin, and moving its last stop to the far
    # end would write a tail the trip never serves.
    ends = shapely.get_coordinates(line)[[0, -1]]
    closed = float(np.hypot(*(ends[0] - ends[1]))) <= SNAP_TOLERANCE
    circular = (
        closed
        and len(along) > 2
        and abs(along[-1] - along[0]) <= SNAP_TOLERANCE
        and along[-2] > along[0]
    )
    if circular:
        along = np.append(along[:-1], line.length)
    if (np.diff(along) < 0).any() or along[-1] <= along[0]:
        return None
    return along
