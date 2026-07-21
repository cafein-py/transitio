import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point

pytest.importorskip("transitio._core")

from transitio.edit import FeedEditor, build_feed  # noqa: E402

LINE = LineString([(24.90, 60.16), (24.93, 60.17), (24.96, 60.18)])


def routes_frame(**overrides):
    attributes = {
        "route_id": "sc1",
        "route_short_name": "S1",
        "mode": "tram",
        "headway_min": 10,
        "speed_kmh": 30,
        "days": "weekdays",
    }
    attributes.update(overrides)
    return gpd.GeoDataFrame([attributes], geometry=[LINE], crs="EPSG:4326")


def test_build_feed_generated_stops(tmp_path):
    output = tmp_path / "scenario.zip"
    report = build_feed(
        routes_frame(),
        output,
        timezone="Europe/Helsinki",
        reference_date="20260601",
    )
    assert not any(n["severity"] == "ERROR" for n in report["notices"])

    editor = FeedEditor(output)
    routes = editor.tables["routes.txt"]
    assert list(routes["route_type"]) == ["0"]  # tram
    trips = editor.tables["trips.txt"]
    assert sorted(trips["direction_id"]) == ["0", "1"]  # both directions
    assert set(trips["shape_id"]) == {"sc1-shape", "sc1-shape-r"}
    frequencies = editor.tables["frequencies.txt"]
    assert list(frequencies["headway_secs"]) == ["600", "600"]
    assert list(frequencies["start_time"]) == ["06:00:00", "06:00:00"]
    stops = editor.tables["stops.txt"]
    assert len(stops) >= 8  # ~3.9 km at 400 m spacing
    calendar = editor.tables["calendar.txt"].iloc[0]
    assert calendar["monday"] == "1" and calendar["saturday"] == "0"
    # last stop's arrival equals the full run time from distance/speed
    times = editor.tables["stop_times.txt"]
    outbound = times[times["trip_id"] == "sc1-day-0"]
    assert outbound.iloc[-1]["arrival_time"] > "06:06:00"


def test_build_feed_with_stop_layer_and_periods(tmp_path):
    stops = gpd.GeoDataFrame(
        [
            {"stop_id": "a", "stop_name": "Alku"},
            {"stop_id": "b", "stop_name": "Keskusta"},
            {"stop_id": "c", "stop_name": "Loppu"},
            {"stop_id": "far", "stop_name": "Kaukana"},
        ],
        geometry=[
            Point(24.90, 60.16),
            Point(24.93, 60.17),
            Point(24.96, 60.18),
            Point(25.20, 60.30),  # beyond snap distance
        ],
        crs="EPSG:4326",
    )
    frame = routes_frame(headway_min=None, headway_peak=5, headway_offpeak=15)
    output = tmp_path / "scenario.zip"
    report = build_feed(
        frame,
        output,
        stops_layer=stops,
        periods={
            "peak": ("07:00:00", "09:00:00"),
            "offpeak": ("09:00:00", "15:00:00"),
        },
        timezone="Europe/Helsinki",
        check=False,
        reference_date="20260601",
    )
    assert not any(n["severity"] == "ERROR" for n in report["notices"])

    editor = FeedEditor(output)
    times = editor.tables["stop_times.txt"]
    outbound = times[times["trip_id"] == "sc1-peak-0"]
    assert list(outbound["stop_id"]) == ["a", "b", "c"]  # far stop excluded
    inbound = times[times["trip_id"] == "sc1-peak-1"]
    assert list(inbound["stop_id"]) == ["c", "b", "a"]
    frequencies = editor.tables["frequencies.txt"]
    assert set(frequencies["headway_secs"]) == {"300", "900"}
    names = set(editor.tables["stops.txt"]["stop_name"])
    assert "Keskusta" in names


def test_build_feed_reads_geopackage(tmp_path):
    source = tmp_path / "network.gpkg"
    routes_frame().to_file(source, layer="routes", driver="GPKG")
    output = tmp_path / "scenario.zip"
    report = build_feed(
        source,
        output,
        routes_layer="routes",
        timezone="Europe/Helsinki",
        reference_date="20260601",
    )
    assert not any(n["severity"] == "ERROR" for n in report["notices"])


def test_build_feed_reprojects_and_validates_inputs(tmp_path):
    projected = routes_frame().to_crs("EPSG:3067")
    output = tmp_path / "scenario.zip"
    report = build_feed(
        projected,
        output,
        timezone="Europe/Helsinki",
        reference_date="20260601",
    )
    assert not any(n["severity"] == "ERROR" for n in report["notices"])

    with pytest.raises(ValueError, match="no headway"):
        build_feed(
            routes_frame(headway_min=None),
            tmp_path / "x.zip",
            timezone="Europe/Helsinki",
        )
    with pytest.raises(ValueError, match="unknown mode"):
        build_feed(
            routes_frame(mode="zeppelin"),
            tmp_path / "y.zip",
            timezone="Europe/Helsinki",
        )


def test_build_feed_unidirectional(tmp_path):
    output = tmp_path / "scenario.zip"
    build_feed(
        routes_frame(bidirectional="false"),
        output,
        timezone="Europe/Helsinki",
        reference_date="20260601",
    )
    editor = FeedEditor(output)
    assert list(editor.tables["trips.txt"]["direction_id"]) == ["0"]


def test_build_feed_exact_times(tmp_path):
    output = tmp_path / "scenario.zip"
    frame = routes_frame(bidirectional="false")
    report = build_feed(
        frame,
        output,
        timezone="Europe/Helsinki",
        exact_times=True,
        reference_date="20260601",
    )
    assert not any(n["severity"] == "ERROR" for n in report["notices"])
    editor = FeedEditor(output)
    assert "frequencies.txt" not in editor.tables
    trips = editor.tables["trips.txt"]
    # 06:00-22:00 at 10 min headway = 96 departures
    assert len(trips) == 96
    times = editor.tables["stop_times.txt"]
    first = times[times["trip_id"] == "sc1-day-0-1"]
    assert first.iloc[0]["departure_time"] == "06:00:00"


def test_build_feed_weekday_flag_columns(tmp_path):
    frame = routes_frame(days=None, monday=1, tuesday=1, saturday=0)
    output = tmp_path / "scenario.zip"
    build_feed(frame, output, timezone="Europe/Helsinki", reference_date="20260601")
    calendar = FeedEditor(output).tables["calendar.txt"].iloc[0]
    assert calendar["monday"] == "1"
    assert calendar["tuesday"] == "1"
    assert calendar["wednesday"] == "0"


def test_build_feed_rejects_conflicting_headways(tmp_path):
    frame = routes_frame(headway_peak=5)
    with pytest.raises(ValueError, match="not both"):
        build_feed(
            frame,
            tmp_path / "x.zip",
            periods={"peak": ("07:00:00", "09:00:00")},
            timezone="Europe/Helsinki",
        )


def test_build_feed_warns_without_crs(tmp_path):
    frame = routes_frame()
    frame = frame.set_crs(None, allow_override=True)
    with pytest.warns(UserWarning, match="no CRS"):
        build_feed(
            frame,
            tmp_path / "scenario.zip",
            timezone="Europe/Helsinki",
            reference_date="20260601",
        )


def test_build_feed_snapped(tmp_path, kantakaupunki_pbf):
    pytest.importorskip("networkx")
    frame = gpd.GeoDataFrame(
        [
            {
                "route_id": "snapped",
                "route_short_name": "S",
                "mode": "bus",
                "headway_min": 10,
            }
        ],
        geometry=[LineString([(24.9310, 60.1699), (24.9414, 60.1719)])],
        crs="EPSG:4326",
    )
    output = tmp_path / "scenario.zip"
    report = build_feed(
        frame,
        output,
        timezone="Europe/Helsinki",
        snap_to=kantakaupunki_pbf,
        reference_date="20260601",
    )
    assert not any(n["severity"] == "ERROR" for n in report["notices"])
    shapes = FeedEditor(output).shapes
    outbound = shapes[shapes["shape_id"] == "snapped-shape"].geometry.iloc[0]
    assert len(outbound.coords) > 2  # follows streets


def test_build_feed_snapped_custom_filter(tmp_path, kantakaupunki_pbf):
    pytest.importorskip("networkx")
    frame = gpd.GeoDataFrame(
        [
            {
                "route_id": "snapped",
                "route_short_name": "S",
                "mode": "bus",
                "headway_min": 10,
            }
        ],
        geometry=[LineString([(24.9310, 60.1699), (24.9414, 60.1719)])],
        crs="EPSG:4326",
    )
    report = build_feed(
        frame,
        tmp_path / "scenario.zip",
        timezone="Europe/Helsinki",
        snap_to=kantakaupunki_pbf,
        snap_custom_filter={
            "highway": [
                "primary",
                "secondary",
                "tertiary",
                "residential",
                "service",
                "unclassified",
                "living_street",
            ]
        },
        reference_date="20260601",
    )
    assert not any(n["severity"] == "ERROR" for n in report["notices"])


def test_layer_stop_offsets_rebase_to_window_start(tmp_path):
    # A stop layer that starts mid-line must still depart the first
    # served stop exactly at the window start.
    stops = gpd.GeoDataFrame(
        [
            {"stop_id": "mid", "stop_name": "Mid"},
            {"stop_id": "end", "stop_name": "End"},
        ],
        geometry=[Point(24.93, 60.17), Point(24.96, 60.18)],
        crs="EPSG:4326",
    )
    output = tmp_path / "scenario.zip"
    build_feed(
        routes_frame(bidirectional="false"),
        output,
        stops_layer=stops,
        timezone="Europe/Helsinki",
        check=False,
        reference_date="20260601",
    )
    times = FeedEditor(output).tables["stop_times.txt"]
    assert times.iloc[0]["departure_time"] == "06:00:00"


def test_build_feed_rejects_bad_windows_and_types(tmp_path):
    with pytest.raises(ValueError, match="whole number"):
        build_feed(
            routes_frame(mode=None, route_type=2.9),
            tmp_path / "a.zip",
            timezone="Europe/Helsinki",
        )
    with pytest.raises(ValueError, match="end\\n?.*after|after its start"):
        build_feed(
            routes_frame(first_departure="10:00:00", last_departure="08:00:00"),
            tmp_path / "b.zip",
            timezone="Europe/Helsinki",
        )
    with pytest.raises(ValueError, match="would expand"):
        build_feed(
            routes_frame(headway_min=0.01),
            tmp_path / "c.zip",
            timezone="Europe/Helsinki",
            exact_times=True,
        )


def test_build_feed_shapefile_safe_aliases(tmp_path):
    frame = gpd.GeoDataFrame(
        [
            {
                "route_id": "al1",
                "short_name": "A",
                "mode": "bus",
                "headway": 12,
                "first_dep": "07:00:00",
                "last_dep": "10:00:00",
                "bidir": "false",
            }
        ],
        geometry=[LINE],
        crs="EPSG:4326",
    )
    output = tmp_path / "scenario.zip"
    build_feed(frame, output, timezone="Europe/Helsinki", reference_date="20260601")
    editor = FeedEditor(output)
    assert list(editor.tables["routes.txt"]["route_short_name"]) == ["A"]
    frequencies = editor.tables["frequencies.txt"]
    assert list(frequencies["headway_secs"]) == ["720"]
    assert list(frequencies["start_time"]) == ["07:00:00"]
    assert list(editor.tables["trips.txt"]["direction_id"]) == ["0"]


def test_build_feed_renamed_geometry_and_3d(tmp_path):
    line_3d = LineString(
        [(24.90, 60.16, 5.0), (24.93, 60.17, 6.0), (24.96, 60.18, 7.0)]
    )
    frame = gpd.GeoDataFrame(
        [{"route_id": "z1", "route_short_name": "Z", "mode": "bus", "headway_min": 10}],
        geometry=[line_3d],
        crs="EPSG:4326",
    )
    frame = frame.rename_geometry("alignment")
    output = tmp_path / "scenario.zip"
    report = build_feed(
        frame, output, timezone="Europe/Helsinki", reference_date="20260601"
    )
    assert not any(n["severity"] == "ERROR" for n in report["notices"])


def test_build_feed_all_false_weekdays_raise(tmp_path):
    frame = routes_frame(days=None, monday=0, tuesday=0)
    with pytest.raises(ValueError, match="no day is enabled"):
        build_feed(frame, tmp_path / "x.zip", timezone="Europe/Helsinki")


def test_build_feed_validates_stop_spacing(tmp_path):
    with pytest.raises(ValueError, match="stop_spacing"):
        build_feed(
            routes_frame(),
            tmp_path / "x.zip",
            timezone="Europe/Helsinki",
            stop_spacing=-5,
        )
    with pytest.raises(ValueError, match="coarser spacing"):
        build_feed(
            routes_frame(),
            tmp_path / "y.zip",
            timezone="Europe/Helsinki",
            stop_spacing=0.1,
        )
