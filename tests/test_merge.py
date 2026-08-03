import pandas as pd
import pytest

pytest.importorskip("transitio._core")

from transitio.edit import FeedBuilder, FeedEditor  # noqa: E402
from transitio.gtfs import merge_feeds, merge_tables  # noqa: E402


def build_city(agency_id="hsl"):
    builder = FeedBuilder()
    builder.add_agency(agency_id, "Agency", "https://a.example", "Europe/Helsinki")
    builder.add_stop("s1", "First", 60.169, 24.931)
    builder.add_stop("s2", "Second", 60.171, 24.941)
    builder.add_route("r1", 0, "1", agency_id=agency_id)
    builder.add_service("wk", "weekdays", "20260101", "20261231")
    builder.add_trip(
        "r1",
        "wk",
        "t1",
        [("s1", "08:00:00", "08:00:00"), ("s2", "08:05:00", "08:05:30")],
    )
    return builder


def frame(**columns):
    return pd.DataFrame({key: list(values) for key, values in columns.items()})


def test_merge_colliding_ids(tmp_path):
    output = tmp_path / "merged.zip"
    report = merge_feeds(
        [build_city(), build_city()], output, reference_date="20260601"
    )
    assert not any(n["severity"] == "ERROR" for n in report["notices"])
    assert report["dropped_files"] == []

    merged = FeedEditor(output)
    stops = merged.tables["stops.txt"]
    assert len(stops) == 4
    assert set(stops["stop_id"]) == {"f1:s1", "f1:s2", "f2:s1", "f2:s2"}
    trips = merged.tables["trips.txt"]
    assert set(trips["trip_id"]) == {"f1:t1", "f2:t1"}
    assert set(trips["route_id"]) == {"f1:r1", "f2:r1"}
    times = merged.tables["stop_times.txt"]
    assert set(times["stop_id"]) <= set(stops["stop_id"])
    assert set(merged.tables["calendar.txt"]["service_id"]) == {"f1:wk", "f2:wk"}
    assert set(merged.tables["agency.txt"]["agency_id"]) == {"f1:hsl", "f2:hsl"}


def test_single_agency_backfill_and_colon_ids(tmp_path):
    first = build_city(agency_id="")
    second = build_city(agency_id="")
    first.add_stop("a:b", "Colon", 60.17, 24.93)
    second.add_stop("a:b", "Colon", 60.17, 24.93)
    output = tmp_path / "merged.zip"
    report = merge_feeds([first, second], output, reference_date="20260601")
    assert not any(n["severity"] == "ERROR" for n in report["notices"])

    merged = FeedEditor(output)
    assert set(merged.tables["agency.txt"]["agency_id"]) == {"f1", "f2"}
    assert set(merged.tables["routes.txt"]["agency_id"]) == {"f1", "f2"}
    assert {"f1:a:b", "f2:a:b"} <= set(merged.tables["stops.txt"]["stop_id"])


def test_single_agency_backfill_creates_missing_column():
    first = {
        "agency.txt": frame(agency_id=["a1"], agency_timezone=["Europe/Helsinki"]),
        "routes.txt": frame(route_id=["r1"]),
        "fare_attributes.txt": frame(fare_id=["fa"]),
    }
    second = {
        "agency.txt": frame(agency_id=["a2"], agency_timezone=["Europe/Helsinki"]),
        "routes.txt": frame(route_id=["r1"], agency_id=["a2"]),
    }
    tables, _ = merge_tables([first, second])
    assert list(tables["routes.txt"]["agency_id"]) == ["f1:a1", "f2:a2"]
    assert list(tables["fare_attributes.txt"]["agency_id"]) == ["f1:a1"]


def test_dropped_files_and_extra_columns():
    first = {
        "stops.txt": frame(stop_id=["1"], custom_note=["keep"]),
        "feed_info.txt": frame(feed_publisher_name=["pub"]),
        "translations.txt": frame(table_name=["stops"]),
    }
    second = {"stops.txt": frame(stop_id=["1"])}
    tables, dropped = merge_tables([first, second], extra_entries=[["readme.txt"], []])
    assert dropped == ["feed_info.txt", "readme.txt", "translations.txt"]
    assert "feed_info.txt" not in tables
    stops = tables["stops.txt"]
    assert list(stops["stop_id"]) == ["f1:1", "f2:1"]
    assert list(stops["custom_note"]) == ["keep", ""]


def test_path_and_memory_inputs_equal(tmp_path):
    builders = [build_city(), build_city(agency_id="tkl")]
    paths = []
    for index, builder in enumerate(builders):
        path = tmp_path / f"in{index}.zip"
        builder.save(path, reference_date="20260601")
        paths.append(path)
    from_paths = tmp_path / "from-paths.zip"
    from_memory = tmp_path / "from-memory.zip"
    merge_feeds(paths, from_paths, reference_date="20260601")
    merge_feeds(builders, from_memory, reference_date="20260601")
    left = FeedEditor(from_paths)
    right = FeedEditor(from_memory)
    assert set(left.tables) == set(right.tables)
    for name in left.tables:
        pd.testing.assert_frame_equal(left.tables[name], right.tables[name])


def test_fares_v2_reference_prefixing():
    def fares(default):
        return {
            "fare_media.txt": frame(fare_media_id=["m"]),
            "rider_categories.txt": frame(
                rider_category_id=["rc"], is_default_fare_category=[default]
            ),
            "fare_products.txt": frame(
                fare_product_id=["p"], fare_media_id=["m"], rider_category_id=["rc"]
            ),
            "networks.txt": frame(network_id=["n"]),
            "route_networks.txt": frame(network_id=["n"], route_id=["r"]),
            "areas.txt": frame(area_id=["a"]),
            "stop_areas.txt": frame(area_id=["a"], stop_id=["s"]),
            "timeframes.txt": frame(timeframe_group_id=["tf"], service_id=["sv"]),
            "fare_leg_rules.txt": frame(
                leg_group_id=["lg"],
                network_id=["n"],
                from_area_id=["a"],
                to_area_id=["a"],
                from_timeframe_group_id=["tf"],
                to_timeframe_group_id=["tf"],
                fare_product_id=["p"],
            ),
            "fare_transfer_rules.txt": frame(
                from_leg_group_id=["lg"], to_leg_group_id=["lg"], fare_product_id=["p"]
            ),
            "fare_leg_join_rules.txt": frame(
                from_network_id=["n"],
                to_network_id=["n"],
                from_stop_id=["s"],
                to_stop_id=["s"],
            ),
        }

    tables, _ = merge_tables([fares("1"), fares("")])
    for filename, table in tables.items():
        for column in table.columns:
            if column == "is_default_fare_category":
                continue
            for prefix, value in zip(["f1", "f2"], table[column]):
                assert value.startswith(prefix + ":"), (filename, column, value)


def test_network_representations_normalised():
    first = {"routes.txt": frame(route_id=["r1"], network_id=["n1"])}
    second = {
        "routes.txt": frame(route_id=["r2"]),
        "route_networks.txt": frame(network_id=["n2"], route_id=["r2"]),
    }
    tables, _ = merge_tables([first, second])
    assert "network_id" not in tables["routes.txt"].columns
    pairs = set(
        zip(
            tables["route_networks.txt"]["route_id"],
            tables["route_networks.txt"]["network_id"],
        )
    )
    assert pairs == {("f2:r2", "f2:n2"), ("f1:r1", "f1:n1")}


def test_feed_wide_attribution_passes_through():
    attribution = frame(
        organization_name=["Producer"], agency_id=[""], route_id=[""], trip_id=[""]
    )
    tables, _ = merge_tables(
        [{"attributions.txt": attribution}, {"stops.txt": frame(stop_id=["1"])}]
    )
    row = tables["attributions.txt"].iloc[0]
    assert row["organization_name"] == "Producer"
    assert row["agency_id"] == "" and row["route_id"] == "" and row["trip_id"] == ""


def test_conflicting_agency_timezones():
    first = {"agency.txt": frame(agency_id=["a"], agency_timezone=["Europe/Helsinki"])}
    second = {
        "agency.txt": frame(agency_id=["b"], agency_timezone=["Europe/Stockholm"])
    }
    with pytest.raises(ValueError, match="timezones differ"):
        merge_tables([first, second])


def test_conflicting_default_rider_categories():
    def feed():
        return {
            "rider_categories.txt": frame(
                rider_category_id=["rc"], is_default_fare_category=["1"]
            )
        }

    with pytest.raises(ValueError, match="default rider category"):
        merge_tables([feed(), feed()])


def test_flex_feeds_are_refused():
    plain = {"stops.txt": frame(stop_id=["1"])}
    with_geojson = {"locations.geojson": frame(anything=["x"]), **plain}
    with pytest.raises(ValueError, match="locations.geojson"):
        merge_tables([with_geojson, plain])
    with pytest.raises(ValueError, match="locations.geojson"):
        merge_tables([plain, plain], extra_entries=[["locations.geojson"], []])
    with_location_ids = {"stop_times.txt": frame(trip_id=["t"], location_id=["loc"])}
    with pytest.raises(ValueError, match="location_id"):
        merge_tables([with_location_ids, plain])


def test_argument_errors():
    feed = {"stops.txt": frame(stop_id=["1"])}
    with pytest.raises(ValueError, match="at least two"):
        merge_tables([feed])
    with pytest.raises(ValueError, match="at least two"):
        merge_feeds([feed], "out.zip")
    with pytest.raises(ValueError, match="unique"):
        merge_tables([feed, feed], prefixes=["x", "x"])
    with pytest.raises(ValueError, match="unique"):
        merge_tables([feed, feed], prefixes=["x", " x "])
    with pytest.raises(ValueError, match="non-empty"):
        merge_tables([feed, feed], prefixes=["x", " "])
    with pytest.raises(ValueError, match="':'"):
        merge_tables([feed, feed], prefixes=["x", "y:z"])
    with pytest.raises(ValueError, match="prefixes"):
        merge_tables([feed, feed], prefixes=["x"])
