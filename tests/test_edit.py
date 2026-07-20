import zipfile

import pytest

pytest.importorskip("transitio._core")

from transitio.edit import FeedBuilder, FeedEditor  # noqa: E402
from transitio.edit._editor import format_gtfs_time, parse_gtfs_time  # noqa: E402
from transitio.exceptions import InvalidFeedError  # noqa: E402


def build_minimal(builder=None):
    builder = builder or FeedBuilder()
    builder.add_agency("hsl", "HSL", "https://hsl.fi", "Europe/Helsinki")
    builder.add_stop("s1", "Kamppi", 60.169, 24.931)
    builder.add_stop("s2", "Steissi", 60.171, 24.941)
    builder.add_route("r1", 0, "1", agency_id="hsl")
    builder.add_service("wk", "weekdays", "20260101", "20261231")
    return builder


def test_builder_roundtrip_scheduled_trip(tmp_path):
    builder = build_minimal()
    builder.add_trip(
        "r1",
        "wk",
        "t1",
        [("s1", "08:00:00", "08:00:00"), ("s2", "08:05:00", "08:05:30")],
    )
    path = tmp_path / "built.zip"
    report = builder.save(path, reference_date="20260601")
    assert report["row_counts"]["stop_times.txt"] == 2
    assert not any(n["severity"] == "ERROR" for n in report["notices"])

    editor = FeedEditor(path)
    assert list(editor.tables["trips.txt"]["trip_id"]) == ["t1"]
    assert list(editor.tables["stop_times.txt"]["departure_time"]) == [
        "08:00:00",
        "08:05:30",
    ]


def test_builder_frequency_trip(tmp_path):
    builder = build_minimal()
    builder.add_frequency_trip(
        "r1",
        "wk",
        "t1",
        [("s1", 0), ("s2", 300)],
        start="06:00:00",
        end="09:00:00",
        headway=600,
    )
    path = tmp_path / "built.zip"
    report = builder.save(path, reference_date="20260601")
    assert not any(n["severity"] == "ERROR" for n in report["notices"])

    editor = FeedEditor(path)
    frequencies = editor.tables["frequencies.txt"]
    assert list(frequencies["headway_secs"]) == ["600"]
    assert list(editor.tables["stop_times.txt"]["arrival_time"]) == [
        "06:00:00",
        "06:05:00",
    ]


def test_save_check_gate(tmp_path):
    builder = FeedBuilder()
    builder.add_agency("a", "A", "https://a.example", "Europe/Helsinki")
    path = tmp_path / "broken.zip"
    with pytest.raises(InvalidFeedError) as caught:
        builder.save(path)
    assert path.exists()  # the file is written; only the gate raises
    assert any(
        n["code"] == "missing_required_file" for n in caught.value.report["notices"]
    )
    report = builder.save(path, check=False)
    assert any(n["severity"] == "ERROR" for n in report["notices"])


def test_editor_mutations(tmp_path):
    builder = build_minimal()
    builder.add_frequency_trip(
        "r1",
        "wk",
        "t1",
        [("s1", 0), ("s2", 300)],
        start="06:00:00",
        end="09:00:00",
        headway=600,
    )
    builder.add_route("r2", 3, "2", agency_id="hsl")
    builder.add_trip(
        "r2",
        "wk",
        "t2",
        [("s2", "10:00:00", "10:00:00"), ("s1", "10:07:00", "10:07:00")],
    )
    source = tmp_path / "source.zip"
    builder.save(source, reference_date="20260601")

    editor = FeedEditor(source)
    editor.set_headway("t1", 300, end="10:00:00")
    editor.shift_trip("t2", 3600)
    editor.update_stop("s1", stop_name="Kamppi M")
    output = tmp_path / "edited.zip"
    report = editor.save(output, reference_date="20260601")
    assert not any(n["severity"] == "ERROR" for n in report["notices"])

    reread = FeedEditor(output)
    frequencies = reread.tables["frequencies.txt"]
    assert list(frequencies["headway_secs"]) == ["300"]
    assert list(frequencies["end_time"]) == ["10:00:00"]
    times = reread.tables["stop_times.txt"]
    assert list(times.loc[times["trip_id"] == "t2", "departure_time"]) == [
        "11:00:00",
        "11:07:00",
    ]
    assert "Kamppi M" in set(reread.tables["stops.txt"]["stop_name"])


def test_editor_drop_route_cascades(tmp_path):
    builder = build_minimal()
    builder.add_frequency_trip(
        "r1",
        "wk",
        "t1",
        [("s1", 0), ("s2", 300)],
        start="06:00:00",
        end="09:00:00",
        headway=600,
    )
    source = tmp_path / "source.zip"
    builder.save(source, reference_date="20260601")

    editor = FeedEditor(source)
    editor.drop_route("r1")
    assert editor.tables["routes.txt"].empty
    assert editor.tables["trips.txt"].empty
    assert editor.tables["stop_times.txt"].empty
    assert editor.tables["frequencies.txt"].empty
    with pytest.raises(ValueError, match="no route_id"):
        editor.update_route("r1", route_short_name="x")


def test_editor_preserves_extra_entries(tmp_path):
    builder = build_minimal()
    builder.add_trip(
        "r1", "wk", "t1", [("s1", "08:00:00", "08:00:00"), ("s2", 29100, 29100)]
    )
    source = tmp_path / "source.zip"
    builder.save(source, reference_date="20260601")
    geojson = b'{"type":"FeatureCollection","features":[]}'
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr("locations.geojson", geojson)

    editor = FeedEditor(source)
    output = tmp_path / "edited.zip"
    editor.save(output, reference_date="20260601")
    with zipfile.ZipFile(output) as archive:
        assert archive.read("locations.geojson") == geojson


def test_stops_geodataframe():
    editor = build_minimal()
    editor.add_trip(
        "r1", "wk", "t1", [("s1", "08:00:00", "08:00:00"), ("s2", 29100, 29100)]
    )
    import geopandas as gpd

    frame = editor.stops
    assert isinstance(frame, gpd.GeoDataFrame)
    assert frame.crs.to_epsg() == 4326
    assert frame.geometry.iloc[0].x == pytest.approx(24.931)


def test_gtfs_time_helpers():
    assert parse_gtfs_time("25:10:00") == 90600
    assert format_gtfs_time(90600) == "25:10:00"
    assert format_gtfs_time(parse_gtfs_time(300)) == "00:05:00"
    with pytest.raises(ValueError):
        parse_gtfs_time("8h30")
    with pytest.raises(ValueError):
        format_gtfs_time(-1)


def test_save_refuses_symlink_staging(tmp_path):
    import os as _os

    builder = build_minimal()
    builder.add_trip(
        "r1", "wk", "t1", [("s1", "08:00:00", "08:00:00"), ("s2", 29100, 29100)]
    )
    target = tmp_path / "out.zip"
    decoy = tmp_path / "decoy"
    decoy.write_bytes(b"")
    _os.symlink(decoy, tmp_path / "out.zip.part")
    with pytest.raises(ValueError, match="symlink"):
        builder.save(target, reference_date="20260601")


def test_editor_load_budget(tmp_path):
    builder = build_minimal()
    builder.add_trip(
        "r1", "wk", "t1", [("s1", "08:00:00", "08:00:00"), ("s2", 29100, 29100)]
    )
    source = tmp_path / "feed.zip"
    builder.save(source, reference_date="20260601")
    with pytest.raises(ValueError, match="budget"):
        FeedEditor(source, max_total_bytes=10)


def test_unknown_txt_preserved_verbatim(tmp_path):
    builder = build_minimal()
    builder.add_trip(
        "r1", "wk", "t1", [("s1", "08:00:00", "08:00:00"), ("s2", 29100, 29100)]
    )
    source = tmp_path / "feed.zip"
    builder.save(source, reference_date="20260601")
    weird = b"not,really\ncsv \xc3\xa4\n\n\n"
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr("license_notes.txt", weird)

    editor = FeedEditor(source)
    assert "license_notes.txt" not in editor.tables
    output = tmp_path / "out.zip"
    editor.save(output, reference_date="20260601")
    with zipfile.ZipFile(output) as archive:
        assert archive.read("license_notes.txt") == weird


def test_set_headway_requires_window_selector(tmp_path):
    builder = build_minimal()
    builder.add_frequency_trip(
        "r1",
        "wk",
        "t1",
        [("s1", 0), ("s2", 300)],
        start="06:00:00",
        end="09:00:00",
        headway=600,
    )
    builder._append(
        "frequencies.txt",
        {
            "trip_id": "t1",
            "start_time": "15:00:00",
            "end_time": "18:00:00",
            "headway_secs": "300",
        },
    )
    source = tmp_path / "feed.zip"
    builder.save(source, reference_date="20260601")

    editor = FeedEditor(source)
    with pytest.raises(ValueError, match="frequency windows"):
        editor.set_headway("t1", 120)
    editor.set_headway("t1", 120, window="15:00:00")
    frequencies = editor.tables["frequencies.txt"]
    assert sorted(frequencies["headway_secs"]) == ["120", "600"]


def test_add_trip_is_atomic():
    builder = build_minimal()
    with pytest.raises(ValueError):
        builder.add_trip(
            "r1",
            "wk",
            "bad",
            [("s1", "08:00:00", "08:00:00"), ("s2", "08:70:00", "08:70:00")],
        )
    assert "trips.txt" not in builder.tables
    assert "stop_times.txt" not in builder.tables


def test_set_stops_writeback():
    editor = build_minimal()
    frame = editor.stops
    frame = frame.set_geometry(frame.geometry.translate(xoff=0.01))
    editor.set_stops(frame)
    table = editor.tables["stops.txt"]
    assert float(table.loc[table["stop_id"] == "s1", "stop_lon"].iloc[0]) == (
        pytest.approx(24.941)
    )
    assert "geometry" not in table.columns


def test_time_parser_rejects_out_of_range():
    for bad in ("08:70:00", "08:00:60", "-1:00:00", -5, 1.5, True):
        with pytest.raises(ValueError):
            parse_gtfs_time(bad)


def test_save_rejects_unsafe_table_names(tmp_path):
    builder = build_minimal()
    builder.tables["../evil.txt"] = builder.tables["agency.txt"].copy()
    with pytest.raises(ValueError, match="unsafe table names"):
        builder.save(tmp_path / "out.zip")


def test_set_stops_reprojects_to_wgs84():
    editor = build_minimal()
    frame = editor.stops.to_crs("EPSG:3067")
    editor.set_stops(frame)
    table = editor.tables["stops.txt"]
    lon = float(table.loc[table["stop_id"] == "s1", "stop_lon"].iloc[0])
    assert lon == pytest.approx(24.931, abs=1e-6)


def test_add_frequency_trip_rejects_bad_headway():
    builder = build_minimal()
    for bad in (0, -300, 1.5, True):
        with pytest.raises(ValueError):
            builder.add_frequency_trip(
                "r1",
                "wk",
                "t1",
                [("s1", 0), ("s2", 300)],
                start="06:00:00",
                end="09:00:00",
                headway=bad,
            )
    assert "trips.txt" not in builder.tables


def test_drop_route_clears_route_transfers_and_networks(tmp_path):
    builder = build_minimal()
    builder.add_frequency_trip(
        "r1",
        "wk",
        "t1",
        [("s1", 0), ("s2", 300)],
        start="06:00:00",
        end="09:00:00",
        headway=600,
    )
    builder._append("route_networks.txt", {"network_id": "n1", "route_id": "r1"})
    builder._append(
        "transfers.txt",
        {
            "from_stop_id": "",
            "to_stop_id": "",
            "from_route_id": "r1",
            "to_route_id": "",
            "transfer_type": "0",
        },
    )
    source = tmp_path / "feed.zip"
    builder.save(source, check=False, reference_date="20260601")
    editor = FeedEditor(source)
    editor.drop_route("r1")
    assert editor.tables["route_networks.txt"].empty
    assert editor.tables["transfers.txt"].empty


def test_add_shape_and_shapes_view(tmp_path):
    from shapely.geometry import LineString

    builder = build_minimal()
    builder.add_shape(
        "sh1", LineString([(24.931, 60.169), (24.936, 60.170), (24.941, 60.171)])
    )
    builder.add_trip(
        "r1",
        "wk",
        "t1",
        [("s1", "08:00:00", "08:00:00"), ("s2", "08:05:00", "08:05:00")],
        shape_id="sh1",
    )
    path = tmp_path / "with-shape.zip"
    report = builder.save(path, reference_date="20260601")
    assert not any(n["severity"] == "ERROR" for n in report["notices"])
    assert report["row_counts"]["shapes.txt"] == 3

    editor = FeedEditor(path)
    assert list(editor.tables["trips.txt"]["shape_id"]) == ["sh1"]
    distances = [float(v) for v in editor.tables["shapes.txt"]["shape_dist_traveled"]]
    assert distances[0] == 0.0
    assert distances == sorted(distances)
    assert 500 < distances[-1] < 1500  # plausible meters for ~1 km

    view = editor.shapes
    assert list(view["shape_id"]) == ["sh1"]
    assert view.geometry.iloc[0].coords[0] == (24.931, 60.169)


def test_add_shape_from_latlon_pairs():
    builder = build_minimal()
    builder.add_shape("sh1", [(60.169, 24.931), (60.171, 24.941)], distances=False)
    table = builder.tables["shapes.txt"]
    assert "shape_dist_traveled" not in table.columns
    assert list(table["shape_pt_lat"]) == ["60.169", "60.171"]
    with pytest.raises(ValueError, match="at least two"):
        builder.add_shape("sh2", [(60.169, 24.931)])


def test_snap_to_network(kantakaupunki_pbf):
    pytest.importorskip("networkx")
    from transitio.edit import snap_to_network

    waypoints = [(60.1699, 24.9310), (60.1719, 24.9414)]
    line = snap_to_network(waypoints, kantakaupunki_pbf)
    assert line.geom_type == "LineString"
    assert len(line.coords) > 2  # follows streets, not a straight segment
    start = line.coords[0]
    assert start[0] == pytest.approx(24.9310, abs=0.005)
    assert start[1] == pytest.approx(60.1699, abs=0.005)
    with pytest.raises(ValueError, match="at least two"):
        snap_to_network([waypoints[0]], kantakaupunki_pbf)
