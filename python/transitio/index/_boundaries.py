"""The schema-12 boundaries table: place boundaries as GeoArrow multipolygons.

A boundary is stored in GeoParquet 1.1.0's native ``multipolygon`` encoding
(polygons, rings, then ``x``/``y`` coordinates as nested lists), a Polygon as
a one-part MultiPolygon with ``polygon`` true. The coordinate columns use
Parquet's byte-stream split without a dictionary, and the file zstd, so the
float64 coordinates come back bit for bit. The index publisher writes the
table with :func:`encode` and the reader reads it with :func:`decode`.
"""

import io
import json

import numpy as np
import shapely

_COORDINATES = "geometry.list.element.list.element.list.element."


def encode(place_ids, geometries, snapshot):
    """The Parquet bytes of a boundaries table: one row per place id, its
    boundary a Polygon, a MultiPolygon or None, and the snapshot id."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    import pyproj

    geometries = np.asarray(geometries, dtype=object)
    missing = shapely.is_missing(geometries)
    shapes = geometries[~missing]
    if (
        not np.isin(shapely.get_type_id(shapes), (3, 6)).all()
        or shapely.is_empty(shapes).any()
        # Z or M values would not survive the x/y layout.
        or (shapely.get_coordinate_dimension(shapes) != 2).any()
    ):
        raise ValueError("a boundary is not a 2D Polygon, MultiPolygon or None")
    # A trailing empty MultiPolygon keeps the layout multipolygon when every
    # boundary is a Polygon, or there are none.
    filled = np.where(missing, shapely.MultiPolygon(), geometries)
    filled = np.append(filled, shapely.MultiPolygon())
    _, coords, (rings, parts, outer) = shapely.to_ragged_array(filled)
    outer = outer[:-1]
    vertices = pa.StructArray.from_arrays(
        [pa.array(coords[:, 0]), pa.array(coords[:, 1])], names=["x", "y"]
    )
    for offsets in (rings, parts):
        vertices = pa.ListArray.from_arrays(pa.array(offsets, pa.int32()), vertices)
    # A null outer offset marks a place without a boundary.
    outer = pa.array(outer, pa.int32(), mask=np.append(missing, False))
    crs = json.loads(pyproj.CRS.from_epsg(4326).to_json())
    geo = {
        "version": "1.1.0",
        "primary_column": "geometry",
        "columns": {
            "geometry": {
                "encoding": "multipolygon",
                "geometry_types": ["MultiPolygon"],
                "crs": crs,
            }
        },
    }
    table = pa.table(
        {
            "place_id": pa.array(place_ids, pa.string()),
            "geometry": pa.ListArray.from_arrays(outer, vertices),
            "polygon": pa.array(shapely.get_type_id(geometries) == 3),
            "snapshot": pa.array([snapshot] * len(geometries), pa.string()),
        }
    ).replace_schema_metadata({"geo": json.dumps(geo)})
    sink = io.BytesIO()
    pq.write_table(
        table,
        sink,
        compression="zstd",
        use_dictionary=["place_id", "snapshot"],
        column_encoding={
            _COORDINATES + "x": "BYTE_STREAM_SPLIT",
            _COORDINATES + "y": "BYTE_STREAM_SPLIT",
        },
    )
    return sink.getvalue()


def _level(array):
    """A list array's offsets from zero and the values they index."""
    offsets = array.offsets.to_numpy()
    values = array.values.slice(offsets[0], offsets[-1] - offsets[0])
    return offsets - offsets[0], values


def _layout(field_type):
    """Whether an Arrow type is the multipolygon layout, x and y float64."""
    import pyarrow as pa

    for _ in range(3):
        if not pa.types.is_list(field_type):
            return False
        field_type = field_type.value_type
    fields = list(field_type) if pa.types.is_struct(field_type) else []
    return [field.name for field in fields] == ["x", "y"] and all(
        pa.types.is_float64(field.type) for field in fields
    )


def _check_metadata(schema):
    """Refuse a table whose GeoParquet metadata does not declare the
    multipolygon encoding in longitude and latitude: EPSG:4326, OGC:CRS84,
    or no ``crs`` key, which GeoParquet reads as OGC:CRS84."""
    try:
        column = json.loads((schema.metadata or {})[b"geo"])["columns"]["geometry"]
    except (KeyError, TypeError, ValueError):
        raise ValueError("the table carries no GeoParquet metadata for geometry")
    if not isinstance(column, dict) or column.get("encoding") != "multipolygon":
        raise ValueError("geometry is not declared as the multipolygon encoding")
    if "crs" in column:
        crs = column["crs"]
        ident = crs.get("id") if isinstance(crs, dict) else None
        known = {("EPSG", 4326), ("OGC", "CRS84")}
        if (
            not isinstance(ident, dict)
            or (
                ident.get("authority"),
                ident.get("code"),
            )
            not in known
        ):
            raise ValueError("the boundaries are not in EPSG:4326")


def decode(table):
    """A boundaries table read with pyarrow as a DataFrame, ``geometry`` the
    shapely boundaries (None where a place has none), those flagged
    ``polygon`` unwrapped to their single Polygon."""
    _check_metadata(table.schema)
    if not _layout(table.schema.field("geometry").type):
        raise ValueError("geometry is not the GeoArrow multipolygon layout")
    if table.column("polygon").null_count:
        raise ValueError("a boundary's polygon flag is null")
    decoded = [np.empty(0, dtype=object)]
    for chunk in table.column("geometry").chunks:
        geometry_offsets, polygons = _level(chunk)
        polygon_offsets, rings = _level(polygons)
        ring_offsets, vertices = _level(rings)
        axes = vertices.flatten()
        # Only a whole boundary may be null: a null part, ring or coordinate
        # would be read as offsets or NaN.
        if any(level.null_count for level in (polygons, rings, vertices, *axes)):
            raise ValueError("a boundary holds a null part, ring or coordinate")
        x, y = (axis.to_numpy(zero_copy_only=False) for axis in axes)
        shapes = shapely.from_ragged_array(
            shapely.GeometryType.MULTIPOLYGON,
            np.column_stack([x, y]),
            (ring_offsets, polygon_offsets, geometry_offsets),
        )
        shapes[chunk.is_null().to_numpy(zero_copy_only=False)] = None
        decoded.append(shapes)
    frame = table.select([n for n in table.column_names if n != "geometry"])
    frame = frame.to_pandas()
    geometries = np.concatenate(decoded)
    single = frame["polygon"].to_numpy(dtype=bool)
    if (single & shapely.is_missing(geometries)).any():
        raise ValueError("a boundary flagged polygon is null")
    if (shapely.get_num_geometries(geometries[single]) != 1).any():
        raise ValueError("a boundary flagged polygon has more than one part")
    geometries[single] = shapely.get_geometry(geometries[single], 0)
    frame["geometry"] = geometries
    return frame
