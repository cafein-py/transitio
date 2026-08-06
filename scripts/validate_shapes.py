"""Measure inferred shapes against a feed's own shapes.

Takes a feed that already has ``shapes.txt``, withholds it, infers
shapes from an OSM extract at each strictness level, and scores the
result against the withheld truth. That gives the numbers behind the
levels: how many shapes each writes, and how far they sit from what
the operator published.

Accuracy per inferred shape: relative total-length error, and the
sampled symmetric Hausdorff distance between the inferred alignment
and the true one (sampled every 25 m, the maximum of both directed
distances, so an omitted excursion scores as badly as a wrong detour).

Manual, benchmark-style:

    python scripts/validate_shapes.py \\
        --gtfs tests/data/helsinki_gtfs.zip \\
        --pbf tests/data/helsinki-transit.osm.pbf \\
        --modes tram
"""

import argparse
import collections
import io
import statistics
import tempfile
import zipfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyproj
import shapely

from transitio.shapes import LEVELS, infer_shapes

SAMPLE_METERS = 25.0
WRONG_CORRIDOR_METERS = 250.0


def strip_shapes(gtfs, destination, modes_routes=None):
    """The feed without shapes: no ``shapes.txt``, no references."""
    with zipfile.ZipFile(gtfs) as source:
        names = source.namelist()
        trips = pd.read_csv(io.BytesIO(source.read("trips.txt")), dtype=str)
        stop_times = pd.read_csv(io.BytesIO(source.read("stop_times.txt")), dtype=str)
        payload = {
            name: source.read(name)
            for name in names
            if name not in ("trips.txt", "stop_times.txt", "shapes.txt")
        }
    if modes_routes is not None:
        trips = trips[trips["route_id"].isin(modes_routes)]
        stop_times = stop_times[stop_times["trip_id"].isin(trips["trip_id"])]
    trips = trips.drop(columns=[c for c in ("shape_id",) if c in trips])
    stop_times = stop_times.drop(
        columns=[c for c in ("shape_dist_traveled",) if c in stop_times]
    )
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as out:
        for name, blob in payload.items():
            out.writestr(name, blob)
        out.writestr("trips.txt", trips.to_csv(index=False))
        out.writestr("stop_times.txt", stop_times.to_csv(index=False))
    return destination


def shape_lines(feed):
    """shape_id → lon/lat vertex array, in shape order."""
    with zipfile.ZipFile(feed) as archive:
        if "shapes.txt" not in archive.namelist():
            return {}
        shapes = pd.read_csv(io.BytesIO(archive.read("shapes.txt")), dtype=str)
    if shapes.empty:
        return {}
    for column in ("shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"):
        shapes[column] = pd.to_numeric(shapes[column], errors="coerce")
    shapes = shapes.dropna(subset=["shape_pt_lat", "shape_pt_lon"])
    lines = {}
    for shape_id, group in shapes.sort_values(
        ["shape_id", "shape_pt_sequence"], kind="stable"
    ).groupby("shape_id", sort=False):
        if len(group) >= 2:
            lines[shape_id] = group[["shape_pt_lon", "shape_pt_lat"]].to_numpy()
    return lines


def trip_shapes(feed):
    """trip_id → shape_id, for trips that carry one."""
    with zipfile.ZipFile(feed) as archive:
        trips = pd.read_csv(io.BytesIO(archive.read("trips.txt")), dtype=str)
    if "shape_id" not in trips:
        return {}
    have = trips.dropna(subset=["shape_id"])
    return dict(zip(have["trip_id"], have["shape_id"]))


def offset_meters(a, b, transformer):
    """Sampled symmetric Hausdorff distance between two lon/lat lines."""
    lines = []
    for coordinates in (a, b):
        x, y = transformer.transform(coordinates[:, 0], coordinates[:, 1])
        lines.append(shapely.LineString(np.column_stack([x, y])))
    directed = []
    for line, other in ((lines[0], lines[1]), (lines[1], lines[0])):
        length = max(line.length, SAMPLE_METERS)
        samples = shapely.line_interpolate_point(
            line, np.arange(0.0, length + 1, SAMPLE_METERS)
        )
        directed.append(float(shapely.distance(other, samples).max()))
    return max(directed)


def line_length(coordinates, transformer):
    x, y = transformer.transform(coordinates[:, 0], coordinates[:, 1])
    return float(shapely.LineString(np.column_stack([x, y])).length)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gtfs", required=True)
    parser.add_argument("--pbf", required=True)
    parser.add_argument("--modes", nargs="+", default=None)
    parser.add_argument("--levels", nargs="+", default=sorted(LEVELS))
    arguments = parser.parse_args()

    with zipfile.ZipFile(arguments.gtfs) as archive:
        routes = pd.read_csv(io.BytesIO(archive.read("routes.txt")), dtype=str)
    keep = None
    if arguments.modes:
        from transitio.shapes._match import mode_of

        kinds = routes["route_type"].astype(int).map(mode_of)
        keep = set(routes.loc[kinds.isin(arguments.modes), "route_id"])
        print(f"modes {arguments.modes}: {len(keep)} routes")

    truth_lines = shape_lines(arguments.gtfs)
    truth_of_trip = trip_shapes(arguments.gtfs)
    print(f"feed carries {len(truth_lines)} shapes as ground truth")

    with tempfile.TemporaryDirectory() as directory:
        stripped = strip_shapes(arguments.gtfs, Path(directory) / "stripped.zip", keep)
        transformer = None
        for name in arguments.levels:
            output = Path(directory) / f"{name}.zip"
            report = infer_shapes(
                stripped,
                output,
                arguments.pbf,
                strictness=name,
                modes=arguments.modes,
            )
            inferred_lines = shape_lines(output)
            inferred_of_trip = trip_shapes(output)
            if transformer is None and truth_lines:
                first = next(iter(truth_lines.values()))
                crs = gpd.GeoSeries(
                    gpd.points_from_xy(first[:1, 0], first[:1, 1]), crs="EPSG:4326"
                ).estimate_utm_crs()
                transformer = pyproj.Transformer.from_crs(
                    "EPSG:4326", crs, always_xy=True
                )

            # One inferred shape can serve trips that referenced
            # several published shapes. Scoring against whichever
            # mapping happened to come last would make the numbers
            # depend on iteration order, so those are excluded and
            # counted instead.
            candidates = collections.defaultdict(set)
            for trip_id, shape_id in inferred_of_trip.items():
                truth_id = truth_of_trip.get(trip_id)
                if truth_id in truth_lines and shape_id in inferred_lines:
                    candidates[shape_id].add(truth_id)
            ambiguous = {s for s, ids in candidates.items() if len(ids) > 1}
            pairs = {
                shape_id: next(iter(ids))
                for shape_id, ids in candidates.items()
                if len(ids) == 1
            }
            errors, offsets = [], []
            for shape_id, truth_id in pairs.items():
                inferred = inferred_lines[shape_id]
                truth = truth_lines[truth_id]
                true_length = line_length(truth, transformer)
                if true_length <= 0:
                    continue
                errors.append(
                    abs(line_length(inferred, transformer) - true_length) / true_length
                )
                offsets.append(offset_meters(inferred, truth, transformer))
            methods = collections.Counter(entry["method"] for entry in report["shapes"])
            print(f"\n[{name}] wrote {report['written']}/{report['patterns']} patterns")
            print(f"  methods: {dict(methods)}")
            print(f"  scored against truth: {len(errors)}")
            if ambiguous:
                print(
                    f"  excluded as ambiguous (several published shapes): "
                    f"{len(ambiguous)}"
                )
            if errors:
                print(
                    f"  length error: median {statistics.median(errors):.2%}, "
                    f"max {max(errors):.2%}"
                )
                print(
                    f"  offset: median {statistics.median(offsets):.0f} m, "
                    f"max {max(offsets):.0f} m, "
                    f">{WRONG_CORRIDOR_METERS:.0f} m: "
                    f"{sum(1 for v in offsets if v > WRONG_CORRIDOR_METERS)}"
                )
            stages = collections.Counter(entry["stage"] for entry in report["skipped"])
            if stages:
                print(f"  skipped: {dict(stages)}")


if __name__ == "__main__":
    main()
