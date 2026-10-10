"""Place boundaries stored as GeoArrow multipolygons, read back bit for bit."""

import io
import json

import pytest

pytest.importorskip("geopandas")

import numpy as np  # noqa: E402
import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402
import shapely  # noqa: E402
from transitio.index import _boundaries  # noqa: E402
from index_fixture import SNAPSHOT_ID  # noqa: E402

HOLED = shapely.Polygon(
    [(24.0, 60.0), (25.0, 60.0), (25.0, 61.0), (24.0, 61.0)],
    [[(24.2, 60.2), (24.4, 60.2), (24.4, 60.4)]],
)
# Each kind of boundary: a Polygon, a one-part and a multipart MultiPolygon
# (one part with a hole), none.
BOUNDARIES = [
    shapely.box(24.9, 60.1, 25.1, 60.3),
    shapely.MultiPolygon([shapely.box(22.2, 60.4, 22.3, 60.5)]),
    shapely.MultiPolygon([HOLED, shapely.box(26.0, 60.0, 26.1, 60.1)]),
    None,
]


def test_boundaries_round_trip_bit_for_bit():
    data = _boundaries.encode(["a", "b", "c", "d"], BOUNDARIES, SNAPSHOT_ID)
    table = pq.read_table(io.BytesIO(data))
    # Two chunks, as a file of several row groups reads.
    frame = _boundaries.decode(pa.concat_tables([table, table]))
    expected = shapely.to_wkb(np.array(BOUNDARIES * 2, dtype=object)).tolist()
    assert shapely.to_wkb(frame["geometry"].to_numpy()).tolist() == expected
    assert list(frame["polygon"]) == [True, False, False, False] * 2
    metadata = pq.ParquetFile(io.BytesIO(data)).metadata
    geo = json.loads(metadata.metadata[b"geo"])["columns"]["geometry"]
    assert geo["encoding"] == "multipolygon"
    row_group = metadata.row_group(0)
    leaves = {
        column.path_in_schema: column
        for column in map(row_group.column, range(row_group.num_columns))
    }
    for axis in "xy":
        leaf = leaves[f"geometry.list.element.list.element.list.element.{axis}"]
        assert "BYTE_STREAM_SPLIT" in leaf.encodings and leaf.compression == "ZSTD"
        assert not {"PLAIN_DICTIONARY", "RLE_DICTIONARY"} & set(leaf.encodings)
    refused = ["POINT (0 0)", "POLYGON Z ((0 0 1, 1 0 1, 1 1 1, 0 0 1))"]
    try:  # M ordinates need shapely 2.1 and GEOS 3.12
        shapely.from_wkt("POLYGON M ((0 0 1, 1 0 1, 1 1 1, 0 0 1))")
        refused.append("POLYGON M ((0 0 1, 1 0 1, 1 1 1, 0 0 1))")
    except shapely.errors.ShapelyError:
        pass
    for wkt in refused:
        with pytest.raises(ValueError, match="not a 2D Polygon"):
            _boundaries.encode(["a"], [shapely.from_wkt(wkt)], SNAPSHOT_ID)
