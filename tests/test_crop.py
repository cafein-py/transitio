import zipfile

import pytest

pytest.importorskip("transitio._core")

from transitio.gtfs import crop_feed  # noqa: E402
from transitio.validate import validate_feed  # noqa: E402

FEED = {
    "agency.txt": (
        "agency_id,agency_name,agency_url,agency_timezone\n"
        "hsl,HSL,https://hsl.fi,Europe/Helsinki\n"
    ),
    "stops.txt": (
        "stop_id,stop_name,stop_lat,stop_lon\n"
        "in1,Kamppi,60.169,24.931\n"
        "in2,Steissi,60.171,24.941\n"
        "out1,Espoo,60.205,24.655\n"
    ),
    "routes.txt": (
        "route_id,agency_id,route_short_name,route_type\n"
        "r-in,hsl,1,3\nr-out,hsl,2,3\n"
    ),
    "trips.txt": (
        "route_id,service_id,trip_id\n" "r-in,wk,t-in\nr-out,wk,t-out\nr-in,old,t-old\n"
    ),
    "stop_times.txt": (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "t-in,08:00:00,08:00:00,in1,1\n"
        "t-in,08:05:00,08:05:00,in2,2\n"
        "t-out,09:00:00,09:00:00,out1,1\n"
        "t-out,09:30:00,09:30:00,out1,2\n"
        "t-old,10:00:00,10:00:00,in1,1\n"
        "t-old,10:05:00,10:05:00,in2,2\n"
    ),
    "calendar.txt": (
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
        "start_date,end_date\n"
        "wk,1,1,1,1,1,0,0,20260101,20261231\n"
        "old,1,1,1,1,1,0,0,20250101,20250630\n"
    ),
}

CITY_BBOX = (24.9, 60.1, 25.0, 60.2)


def write_zip(path, files):
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return path


def test_spatial_crop(tmp_path):
    source = write_zip(tmp_path / "feed.zip", FEED)
    output = tmp_path / "cropped.zip"
    result = crop_feed(source, output, aoi=CITY_BBOX, reference_date="20260601")
    assert result["row_counts"]["trips.txt"] == 2  # t-in and t-old
    assert result["row_counts"]["stops.txt"] == 2
    check = validate_feed(output, reference_date="20260601")
    assert {n["code"] for n in check["notices"]}.isdisjoint(
        {"foreign_key_violation", "unusable_trip"}
    )


def test_temporal_crop(tmp_path):
    source = write_zip(tmp_path / "feed.zip", FEED)
    output = tmp_path / "cropped.zip"
    result = crop_feed(
        source,
        output,
        start_date="20260101",
        end_date="20261231",
        reference_date="20260601",
    )
    # the old service and its trip are gone; both 2026 trips remain
    assert result["row_counts"]["trips.txt"] == 2
    assert result["row_counts"]["calendar.txt"] == 1
    assert result["service_window"] == ["20260101", "20261231"]


def test_combined_crop_and_guards(tmp_path):
    source = write_zip(tmp_path / "feed.zip", FEED)
    output = tmp_path / "cropped.zip"
    result = crop_feed(
        source,
        output,
        aoi=CITY_BBOX,
        start_date="20260101",
        end_date="20261231",
        reference_date="20260601",
    )
    assert result["row_counts"]["trips.txt"] == 1  # only t-in
    with pytest.raises(ValueError, match="nothing to crop"):
        crop_feed(source, tmp_path / "x.zip")


def test_crop_helsinki_to_inner_city(tmp_path, helsinki_gtfs):
    output = tmp_path / "inner.zip"
    budget = {"max_notices_per_file": 1_000_000}
    result = crop_feed(
        helsinki_gtfs,
        output,
        aoi=(24.90, 60.15, 24.98, 60.20),
        reference_date="20220222",
        **budget,
    )
    full = validate_feed(helsinki_gtfs, reference_date="20220222", **budget)
    assert 0 < result["row_counts"]["stops.txt"] < full["row_counts"]["stops.txt"]
    assert result["row_counts"]["stop_times.txt"] > 0


# A grid of one-stop trips, so the retained stops are exactly the stops
# inside the area (no crossing trip drags an outside stop along).
GRID_STOPS = {
    # (lon, lat), against the diagonal y = x of the triangle below
    "inside1": (0.6, 0.2),
    "inside2": (0.9, 0.4),
    "edge": (0.5, 0.5),  # exactly on the diagonal
    "outside": (0.2, 0.8),  # above it, but inside its bounding box
    "far": (5.0, 5.0),
}
GRID = {
    "agency.txt": (
        "agency_id,agency_name,agency_url,agency_timezone\n"
        "a,A,https://a.example,Europe/Helsinki\n"
    ),
    "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\n"
    + "".join(
        f"{name},{name},{lat},{lon}\n" for name, (lon, lat) in GRID_STOPS.items()
    ),
    "routes.txt": "route_id,agency_id,route_short_name,route_type\n"
    + "".join(f"r-{name},a,{name},3\n" for name in GRID_STOPS),
    "trips.txt": "route_id,service_id,trip_id,shape_id\n"
    + "".join(
        # the far trip runs on its own service, so cropping it away must
        # take that calendar row with it
        f"r-{name},{'far-svc' if name == 'far' else 'wk'},t-{name},sh-{name}\n"
        for name in GRID_STOPS
    ),
    "shapes.txt": "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n"
    + "".join(
        f"sh-{name},{lat},{lon},1\nsh-{name},{lat + 0.01},{lon + 0.01},2\n"
        for name, (lon, lat) in GRID_STOPS.items()
    ),
    "stop_times.txt": (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        + "".join(
            f"t-{name},08:00:00,08:00:00,{name},1\n"
            f"t-{name},08:10:00,08:10:00,{name},2\n"
            for name in GRID_STOPS
        )
    ),
    "calendar.txt": (
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
        "start_date,end_date\n"
        "wk,1,1,1,1,1,0,0,20260101,20261231\n"
        "far-svc,1,1,1,1,1,0,0,20260101,20261231\n"
    ),
}

# The lower-right triangle of the unit square (everything below the
# diagonal): "inside1"/"inside2" are inside it, "edge" sits on the
# diagonal, "outside" is above it — but inside the triangle's bounding
# box, which is the whole square.
TRIANGLE = {
    "type": "Polygon",
    "coordinates": [[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 0.0)]],
}


def cropped_stops(tmp_path, name, **kwargs):
    source = write_zip(tmp_path / "grid.zip", GRID)
    output = tmp_path / f"{name}.zip"
    crop_feed(source, output, reference_date="20260601", **kwargs)
    with zipfile.ZipFile(output) as archive:
        rows = archive.read("stops.txt").decode().strip().splitlines()[1:]
    return {row.split(",")[0] for row in rows}


def test_polygon_crop_is_not_its_bounding_box(tmp_path):
    inside = cropped_stops(tmp_path, "polygon", aoi=TRIANGLE)
    assert inside == {"inside1", "inside2", "edge"}  # boundary counts as in

    box = cropped_stops(tmp_path, "bbox", aoi=(0.0, 0.0, 1.0, 1.0))
    assert inside < box  # strictly fewer: the box also keeps "outside"


def test_polygon_crop_stays_referentially_consistent(tmp_path):
    source = write_zip(tmp_path / "grid.zip", GRID)
    output = tmp_path / "cropped.zip"
    report = crop_feed(source, output, aoi=TRIANGLE, reference_date="20260601")
    assert report["row_counts"]["trips.txt"] == 3
    assert report["row_counts"]["stops.txt"] == 3

    inside = {"inside1", "inside2", "edge"}
    with zipfile.ZipFile(output) as archive:

        def column(name, index):
            rows = archive.read(name).decode().strip().splitlines()[1:]
            return {row.split(",")[index] for row in rows}

        # everything the dropped trips referenced cascades away with them
        assert column("routes.txt", 0) == {f"r-{name}" for name in inside}
        assert column("shapes.txt", 0) == {f"sh-{name}" for name in inside}
        assert column("calendar.txt", 0) == {"wk"}  # far-svc is gone
    check = validate_feed(output, reference_date="20260601")
    assert not [n for n in check["notices"] if n["severity"] == "ERROR"]


def test_polygon_hole_excludes_its_interior(tmp_path):
    with_hole = {
        "type": "Polygon",
        "coordinates": [
            [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (0.0, 0.0)],
            [(0.4, 0.4), (0.6, 0.4), (0.6, 0.6), (0.4, 0.6), (0.4, 0.4)],
        ],
    }
    # "edge" sits in the hole; "far" is outside the square altogether
    assert cropped_stops(tmp_path, "hole", aoi=with_hole) == {
        "inside1",
        "inside2",
        "outside",
    }


def test_multipolygon_keeps_stops_in_either_part(tmp_path):
    two_parts = {
        "type": "MultiPolygon",
        "coordinates": [
            [[(0.0, 0.0), (0.7, 0.0), (0.7, 0.3), (0.0, 0.3), (0.0, 0.0)]],
            # a second outer ring, not a hole of the first
            [[(4.5, 4.5), (5.5, 4.5), (5.5, 5.5), (4.5, 5.5), (4.5, 4.5)]],
        ],
    }
    assert cropped_stops(tmp_path, "multi", aoi=two_parts) == {"inside1", "far"}


def test_shapely_and_geojson_agree(tmp_path):
    shapely = pytest.importorskip("shapely.geometry")
    geometry = shapely.Polygon(TRIANGLE["coordinates"][0])
    assert cropped_stops(tmp_path, "geom", aoi=geometry) == cropped_stops(
        tmp_path, "mapping", aoi=TRIANGLE
    )


def test_polygon_with_full_trips_only(tmp_path):
    source = write_zip(tmp_path / "feed.zip", FEED)
    output = tmp_path / "cropped.zip"
    # a triangle covering in1 but not in2, so the trip serving both is
    # kept without full_trips_only and dropped with it
    half = {
        "type": "Polygon",
        "coordinates": [
            [
                (24.90, 60.10),
                (24.935, 60.10),
                (24.935, 60.20),
                (24.90, 60.20),
                (24.90, 60.10),
            ]
        ],
    }
    loose = crop_feed(source, output, aoi=half, reference_date="20260601")
    assert loose["row_counts"]["trips.txt"] == 2
    # the crossing trip keeps its whole sequence, outside stop included
    with zipfile.ZipFile(output) as archive:
        times = archive.read("stop_times.txt").decode().strip().splitlines()[1:]
        stops = archive.read("stops.txt").decode().strip().splitlines()[1:]
    kept = [row.split(",")[3] for row in times if row.startswith("t-in,")]
    assert kept == ["in1", "in2"]  # in2 is outside the area
    assert {row.split(",")[0] for row in stops} == {"in1", "in2"}
    check = validate_feed(output, reference_date="20260601")
    assert not [n for n in check["notices"] if n["severity"] == "ERROR"]
    strict = crop_feed(
        source,
        tmp_path / "strict.zip",
        aoi=half,
        full_trips_only=True,
        reference_date="20260601",
    )
    assert strict["row_counts"]["trips.txt"] == 0


def test_polygon_errors(tmp_path):
    source = write_zip(tmp_path / "feed.zip", FEED)
    output = tmp_path / "cropped.zip"
    with pytest.raises(ValueError, match="three distinct points"):
        crop_feed(
            source,
            output,
            aoi={"type": "Polygon", "coordinates": [[(0.0, 0.0), (1.0, 1.0)]]},
        )
    with pytest.raises(ValueError, match="non-finite"):
        crop_feed(
            source,
            output,
            aoi={
                "type": "Polygon",
                "coordinates": [[(0.0, 0.0), (1.0, 0.0), (float("nan"), 1.0)]],
            },
        )
    with pytest.raises(ValueError, match="out of range"):
        crop_feed(
            source,
            output,
            aoi={
                "type": "Polygon",
                "coordinates": [[(0.0, 0.0), (200.0, 0.0), (1.0, 1.0)]],
            },
        )
    # only one spatial predicate at a time (the wrapper picks one; the
    # core call underneath refuses both)
    from transitio import _core

    with pytest.raises(ValueError, match="not both"):
        _core.crop_feed(
            str(source),
            str(output),
            bbox=(0.0, 0.0, 1.0, 1.0),
            polygon=[[[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]]],
        )


def test_unclosed_polygon_ring_is_closed(tmp_path):
    # the same triangle without its repeated first point
    unclosed = {
        "type": "Polygon",
        "coordinates": [[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]],
    }
    assert cropped_stops(tmp_path, "unclosed", aoi=unclosed) == cropped_stops(
        tmp_path, "closed", aoi=TRIANGLE
    )


def test_stop_associations_follow_their_stops(tmp_path):
    feed = dict(GRID)
    feed["areas.txt"] = "area_id,area_name\nzone,Zone\n"
    feed["stop_areas.txt"] = "area_id,stop_id\n" + "".join(
        f"zone,{name}\n" for name in GRID_STOPS
    )
    feed["location_groups.txt"] = "location_group_id,location_group_name\n" "lg,Group\n"
    feed["location_group_stops.txt"] = "location_group_id,stop_id\n" + "".join(
        f"lg,{name}\n" for name in GRID_STOPS
    )
    source = write_zip(tmp_path / "grid.zip", feed)
    output = tmp_path / "cropped.zip"
    crop_feed(source, output, aoi=TRIANGLE, reference_date="20260601")
    inside = {"inside1", "inside2", "edge"}
    with zipfile.ZipFile(output) as archive:
        for name in ("stop_areas.txt", "location_group_stops.txt"):
            rows = archive.read(name).decode().strip().splitlines()[1:]
            assert {row.split(",")[1] for row in rows} == inside, name
    check = validate_feed(output, reference_date="20260601")
    assert not [n for n in check["notices"] if n["severity"] == "ERROR"]


def test_route_crop_keeps_only_selected_routes(tmp_path):
    source = write_zip(tmp_path / "in.zip", FEED)
    output = tmp_path / "out.zip"
    crop_feed(source, output, routes=["r-in"])
    with zipfile.ZipFile(output) as archive:
        routes = archive.read("routes.txt").decode()
        trips = archive.read("trips.txt").decode()
        stops = archive.read("stops.txt").decode()
    # Only r-in's routes/trips survive, and the cascade drops r-out's stop.
    assert "r-in" in routes and "r-out" not in routes
    assert "t-in" in trips and "t-out" not in trips and "t-old" in trips
    assert "out1" not in stops and "in1" in stops


def test_route_crop_combines_with_the_area(tmp_path):
    source = write_zip(tmp_path / "in.zip", FEED)
    output = tmp_path / "out.zip"
    # r-out is selected but serves no stop in the city, so nothing survives it.
    crop_feed(source, output, aoi=CITY_BBOX, routes=["r-out"])
    with zipfile.ZipFile(output) as archive:
        trips = archive.read("trips.txt").decode()
    assert "t-out" not in trips and "t-in" not in trips


def test_nothing_to_crop_is_refused(tmp_path):
    source = write_zip(tmp_path / "in.zip", FEED)
    with pytest.raises(ValueError, match="nothing to crop"):
        crop_feed(source, tmp_path / "x.zip")


def test_route_crop_accepts_a_single_string(tmp_path):
    source = write_zip(tmp_path / "in.zip", FEED)
    output = tmp_path / "out.zip"
    crop_feed(source, output, routes="r-in")
    with zipfile.ZipFile(output) as archive:
        routes = archive.read("routes.txt").decode()
    # A bare string is one route id, not an iterable of characters.
    assert "r-in" in routes and "r-out" not in routes


def test_route_crop_source_routes_distinguishes_absent_from_empty(tmp_path):
    # With routes.txt the report lists its route ids; without it, source_routes
    # is None (undetermined), never an empty list standing in for absent.
    source = write_zip(tmp_path / "in.zip", FEED)
    report = crop_feed(source, tmp_path / "out.zip", routes=["r-in"])
    assert sorted(report["source_routes"]) == ["r-in", "r-out"]

    no_routes = {k: v for k, v in FEED.items() if k != "routes.txt"}
    source2 = write_zip(tmp_path / "in2.zip", no_routes)
    report2 = crop_feed(source2, tmp_path / "out2.zip", routes=["r-in"])
    assert report2["source_routes"] is None
