"""Inferring GTFS route alignments from OpenStreetMap.

A feed without ``shapes.txt`` leaves every consumer drawing straight
lines between stops. :func:`infer_shapes` fills that gap from an OSM
extract — matching OSM route relations where they exist, map matching
over mode graphs where they do not — and writes a feed carrying the
alignments as real shapes.

How much uncertainty the result may rest on is the caller's choice
through ``strictness``: ``"strict"`` writes only unambiguous matches,
``"permissive"`` prefers an informed guess to a straight line. The
returned report says which strategy produced every shape.
"""

from transitio.shapes._infer import infer_shapes
from transitio.shapes._levels import LEVELS, PERMISSIVE, RELAXED, STRICT, Level

__all__ = ["Level", "LEVELS", "PERMISSIVE", "RELAXED", "STRICT", "infer_shapes"]
