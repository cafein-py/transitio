import datetime
import re
import struct

import pandas as pd
import pytest

pytest.importorskip("transitio._core")

from transitio.edit import FeedBuilder, FeedEditor  # noqa: E402
from transitio.exceptions import InvalidFeedError  # noqa: E402
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


def _city_in(zone, agency_id="hsl"):
    builder = build_city(agency_id)
    builder.tables["agency.txt"]["agency_timezone"] = zone
    return builder


HEL, UTC, OSLO = "Europe/Helsinki", "UTC", "Europe/Oslo"
CET, PARIS, NYC = "CET", "Europe/Paris", "America/New_York"


@pytest.mark.parametrize(
    "zones, timezones, error, skipped",
    [
        ([HEL, UTC, HEL], "refuse", "differ", None),
        ([HEL, UTC, HEL], "skip", None, [{"feed": 1, "timezones": [UTC]}]),
        (
            [UTC, UTC, HEL, HEL],
            "skip",
            None,
            [{"feed": 2, "timezones": [HEL]}, {"feed": 3, "timezones": [HEL]}],
        ),
        ([HEL, UTC, OSLO], "skip", "fewer than two", None),
        ([HEL, HEL], "maybe", "must be", None),
        (
            [UTC, NYC],
            "refuse",
            re.escape(f"feed 0 (f1): {UTC}; feed 1 (f2): {NYC}"),
            None,
        ),
        ([UTC, CET, PARIS], "skip", None, [{"feed": 0, "timezones": [UTC]}]),
    ],
    ids=[
        "refused",
        "outlier-left-out",
        "tie-earliest",
        "too-few-left",
        "bad-option",
        "refusal-names-inputs",
        "equivalent-class-wins",
    ],
)
def test_feeds_of_another_time_zone(tmp_path, zones, timezones, error, skipped):
    feeds = [_city_in(zone, f"a{i}") for i, zone in enumerate(zones)]
    output = tmp_path / "merged.zip"
    if error:
        with pytest.raises(ValueError, match=error):
            merge_feeds(feeds, output, timezones=timezones, reference_date="20260601")
        return
    report = merge_feeds(feeds, output, timezones=timezones, reference_date="20260601")
    assert report["skipped_feeds"] == skipped
    # The feeds kept keep the prefixes they had among all the inputs.
    left = {entry["feed"] for entry in skipped}
    agency = FeedEditor(output).tables["agency.txt"]
    assert set(agency["agency_id"]) == {
        f"f{i + 1}:a{i}" for i in range(len(zones)) if i not in left
    }
    assert agency["agency_timezone"].nunique() == 1


def test_equivalent_zone_names_merge_under_the_earliest(tmp_path):
    output = tmp_path / "merged.zip"
    feeds = [_city_in(f" {PARIS} ", "a0"), _city_in(CET, "a1")]
    report = merge_feeds(feeds, output, reference_date="20260601")
    assert report["timezone_aliases"] == {CET: PARIS}
    assert set(FeedEditor(output).tables["agency.txt"]["agency_timezone"]) == {PARIS}


def _utc(*fields):
    return datetime.datetime(*fields, tzinfo=datetime.timezone.utc)


@pytest.mark.parametrize(
    "first, second, year, equivalent",
    [
        (CET, PARIS, 2026, True),
        ("America/Montreal", "America/Toronto", 2026, True),
        (UTC, NYC, 2026, False),
        ("Mars/Olympus", "Mars/Olympus", 2026, True),
        ("Mars/Olympus", UTC, 2026, False),
        ("America/Indiana/Indianapolis", NYC, 2005, False),
    ],
    ids=["alias", "link", "other-offset", "unknown-self", "unknown-known", "history"],
)
def test_zone_equivalence(first, second, year, equivalent):
    from transitio.gtfs._merge import _zone_classes

    classes = _zone_classes({first, second}, (_utc(year, 1, 1), _utc(year + 1, 1, 1)))
    assert (classes[first] == classes[second]) is equivalent


def _tzif(changes):
    """TZif (version 1) bytes of a zone taking each UTC offset from its UTC
    instant on: ``[(instant, offset seconds), ...]``."""
    offsets = sorted({offset for _, offset in changes})
    counts = (0, 0, 0, len(changes), len(offsets), 4)
    data = [struct.pack(">4sc15x6l", b"TZif", b"\0", *counts)]
    data += [struct.pack(">l", int(instant.timestamp())) for instant, _ in changes]
    data.append(bytes(offsets.index(offset) for _, offset in changes))
    data += [struct.pack(">lBB", offset, 0, 0) for offset in offsets]
    data.append(b"TST\0")
    return b"".join(data)


@pytest.fixture
def test_zones(tmp_path):
    import zoneinfo

    epoch = _utc(1970, 1, 1)
    summer = [(_utc(2000, 10, 29, 1), 3600)]
    zones = {
        "One": [(epoch, 3600)],
        "OneUntil2012": [(epoch, 3600), (_utc(2012, 1, 1), 7200)],
        "Early": [(epoch, 3600), (_utc(2000, 3, 26, 1), 7200), *summer],
        "Late": [(epoch, 3600), (_utc(2000, 3, 26, 1, 30), 7200), *summer],
        "At0105": [(epoch, 3600), (_utc(2000, 3, 26, 1, 5), 7200), *summer],
        "At0110": [(epoch, 3600), (_utc(2000, 3, 26, 1, 10), 7200), *summer],
    }
    root = tmp_path / "zoneinfo"
    (root / "Test").mkdir(parents=True)
    for name, changes in zones.items():
        (root / "Test" / name).write_bytes(_tzif(changes))
    original = zoneinfo.TZPATH
    zoneinfo.reset_tzpath(to=[str(root), *original])
    zoneinfo.ZoneInfo.clear_cache()
    yield
    zoneinfo.reset_tzpath(to=original)
    zoneinfo.ZoneInfo.clear_cache()


def _timed_city(index, zone, dates, last, frequency_end, days, removed):
    builder = FeedBuilder()
    builder.add_agency(f"a{index}", "Agency", "https://a.example", zone)
    builder.add_stop("s1", "First", 60.169, 24.931)
    builder.add_stop("s2", "Second", 60.171, 24.941)
    builder.add_route("r1", 0, "1", agency_id=f"a{index}")
    builder.add_service("wk", days, *dates)
    if removed:
        row = {"service_id": "wk", "date": removed, "exception_type": "2"}
        builder.insert_rows("calendar_dates.txt", [row])
    if frequency_end:
        stops = [("s1", 0), ("s2", "01:30:00")]
        builder.add_frequency_trip(
            "r1", "wk", "t1", stops, start=0, end=frequency_end, headway=600
        )
    else:
        stops = [("s1", "08:00:00", "08:00:00"), ("s2", last, last)]
        builder.add_trip("r1", "wk", "t1", stops)
    return builder


Y2000, H1_2000 = ("20000101", "20001231"), ("20000101", "20000630")
FROM_2000 = "1999-12-31T10:00:00+00:00"


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            {
                "dates": [("20000101", "20201231")] * 2,
                "interval": [FROM_2000, "2010-01-01T00:00:00+00:00"],
            },
            id="differs-after-clip",
        ),
        pytest.param({"zones": ["Test/Early", "Test/Late"]}, id="changes-30-min-apart"),
        pytest.param(
            {
                "zones": ["Test/At0105", "Test/At0110"],
                "interval": [FROM_2000, "2001-01-01T12:00:00+00:00"],
            },
            id="differs-between-quarters",
        ),
        pytest.param(
            {
                "dates": [H1_2000] * 2,
                "last": "49:10:00",
                "interval": [FROM_2000, "2000-07-03T12:00:00+00:00"],
            },
            id="late-stop-time",
        ),
        pytest.param(
            {
                "dates": [H1_2000] * 2,
                "last": "1234567:00:00",
                "interval": [FROM_2000, "2010-01-01T00:00:00+00:00"],
            },
            id="seven-digit-hour",
        ),
        pytest.param(
            {
                "dates": [H1_2000] * 2,
                "frequency_end": "47:00:00",
                "interval": [FROM_2000, "2000-07-03T12:00:00+00:00"],
            },
            id="late-frequency",
        ),
        pytest.param(
            {
                "days": "weekdays",
                "removed": "20000103",
                "interval": ["2000-01-03T10:00:00+00:00", "2000-12-30T12:00:00+00:00"],
            },
            id="first-and-last-running-days",
        ),
        pytest.param(
            {
                "dates": [("19700101", "19751231")] * 2,
                "interval": ["2000-01-01T00:00:00+00:00", "2001-01-01T00:00:00+00:00"],
            },
            id="service-before-clip",
        ),
        pytest.param(
            {
                "dates": [("19700101", "19751231"), ("20150101", "20151231")],
                "interval": ["2000-01-01T00:00:00+00:00", "2001-01-01T00:00:00+00:00"],
            },
            id="service-around-clip",
        ),
    ],
)
def test_timezone_interval(tmp_path, monkeypatch, test_zones, case):
    case = {
        "zones": ["Test/One", "Test/OneUntil2012"],
        "dates": [Y2000] * 2,
        "last": "08:05:00",
        "frequency_end": None,
        "days": "daily",
        "removed": None,
        "interval": None,
        **case,
    }
    monkeypatch.setattr(
        "transitio.gtfs._merge._today", lambda: datetime.date(2000, 1, 1)
    )
    paths = []
    for index, (zone, dates) in enumerate(zip(case["zones"], case["dates"])):
        builder = _timed_city(
            index,
            zone,
            dates,
            case["last"],
            case["frequency_end"],
            case["days"],
            case["removed"],
        )
        paths.append(tmp_path / f"in{index}.zip")
        builder.save(paths[-1], check=False)
    output = tmp_path / "merged.zip"
    if case["interval"] is None:
        names = re.escape(f"feed 0 (f1, {paths[0]}): {case['zones'][0]}")
        with pytest.raises(InvalidFeedError, match=names):
            merge_feeds(paths, output, check=False)
        return
    report = merge_feeds(paths, output, check=False)
    assert report["timezone_interval"] == case["interval"]


def test_zones_tied_in_one_feed_resolve_by_name():
    from transitio.gtfs._merge import _timezone_outliers

    both = {"agency.txt": frame(agency_id=["a", "b"], agency_timezone=[UTC, HEL])}
    tables = [both, {"agency.txt": frame(agency_timezone=[UTC])}]
    tables.append({"agency.txt": frame(agency_timezone=[HEL])})
    assert _timezone_outliers(tables) == {0: [HEL, UTC], 1: [UTC]}


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


SERVICES = {
    "jan": ("20260101", "20260131"),
    "jan1": ("20260101", "20260115"),
    "jan2": ("20260116", "20260131"),
    "mid": ("20260105", "20260120"),
    "late": ("20260120", "20260210"),
    "long": ("20260101", "20370101"),
    "huge": ("19000101", "20991231"),
}
LATER = ("09:00:00", "09:10:00")


def _trips(*specs):
    return [{"trip_id": f"t{i}", **spec} for i, spec in enumerate(specs, start=1)]


def _repeats(trips, rows=(), continuous=""):
    builder = FeedBuilder()
    builder.add_agency("a", "Agency", "https://a.example", HEL)
    stops = {"s1": 60.1, "s2": 60.11, "s3": 60.12, "s4": 60.10001, "s5": 60.100001}
    for stop, lat in stops.items():
        builder.add_stop(stop, stop, lat, 24.9)
    builder.add_route("r1", 3, "1", agency_id="a", continuous_pickup=continuous)
    builder.add_route("r2", 3, "2", agency_id="a")
    builder.add_route("r3", 3, "1", agency_id="a", continuous_pickup="0")
    for service in sorted({trip.get("service", "jan") for trip in trips}):
        builder.add_service(service, "daily", *SERVICES[service])
    for trip in trips:
        names = ("block_id", "shape_id", "trip_headsign")
        fields = {key: trip[key] for key in names if key in trip}
        if "shape_id" in fields:
            builder.add_shape(fields["shape_id"], [(60.10, 24.9), (60.11, 24.9)])
        times = trip.get("times", ("08:00:00", "08:10:00"))
        stops = list(zip(trip.get("stops", ("s1", "s2")), times, times))
        route, service = trip.get("route", "r1"), trip.get("service", "jan")
        builder.add_trip(route, service, trip["trip_id"], stops, **fields)
        first = len(builder.tables["stop_times.txt"]) - len(stops)
        for column, values in trip.get("stop_fields", {}).items():
            for offset, value in enumerate(values):
                builder.set_value("stop_times.txt", first + offset, column, value)
    for name, row in rows:
        builder.insert_rows(name, [row])
    return builder


@pytest.mark.parametrize(
    "variant, expected",
    [
        ({}, "equal"),
        ({"times": ("08:00:00", "08:11:00")}, "differs"),
        ({"stop_fields": {"pickup_type": ("1", "")}}, "differs"),
        ({"route": "r2"}, "differs"),
        ({"stops": ("s4", "s2")}, "differs"),
        ({"stops": ("s5", "s2")}, "equal"),
        ({"route": "r3"}, "differs"),
        ({"shape_id": "sa"}, "equal"),
        ({"trip_headsign": "Centre"}, "equal"),
        ({"stops": ("s9", "s2")}, "absent"),
        ({"stop_fields": {"stop_sequence": ("1", "1")}}, "absent"),
        ({"route": "r9"}, "absent"),
        ({"trip_id": "t1"}, "absent"),
        ({"stop_fields": {"stop_sequence": ("0" * 19 + "1", "2")}}, "equal"),
    ],
    ids=(
        "identical other-time other-pickup other-route-key moved-stop "
        "moved-within-rounding route-continuous-pickup other-shape "
        "other-headsign unknown-stop repeated-sequence unknown-route "
        "listed-twice padded-sequence"
    ).split(),
)
def test_trip_signatures(variant, expected):
    from transitio.gtfs._schedule import trip_signatures

    signed = trip_signatures(_repeats(_trips({}, variant)).tables)
    signatures = dict(zip(signed["trip_id"], signed["signature"]))
    if expected == "absent":
        assert "t2" not in signatures
        # A feed of such trips alone has no signatures at all.
        assert trip_signatures(_repeats(_trips(variant, variant)).tables).empty
        return
    assert set(signed["service_id"]) == {"jan"}
    assert len(signatures["t1"]) == 32  # 128 bits in hex
    assert (signatures["t2"] == signatures["t1"]) is (expected == "equal")


def _calendar(*rows):
    """calendar.txt from ``(service_id, weekday flags from Monday, start,
    end)`` rows."""
    columns = ["service_id", "flags", "start_date", "end_date"]
    table = pd.DataFrame(list(rows), columns=columns, dtype=str)
    for position, day in enumerate(
        ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    ):
        table[day] = table["flags"].str[position]
    return table.drop(columns="flags")


MON_TUE = ("wk", "1100000", "20260105", "20260113")
JAN_1_TO_5 = ["20260101", "20260102", "20260103", "20260104", "20260105"]


@pytest.mark.parametrize(
    "calendar, exceptions, budget, expected, unexpanded",
    [
        (
            [MON_TUE],
            [],
            None,
            {"wk": ["20260105", "20260106", "20260112", "20260113"]},
            set(),
        ),
        (
            [MON_TUE],
            [("wk", "20260106", "2"), ("wk", "20260301", "1")],
            None,
            {"wk": ["20260105", "20260112", "20260113", "20260301"]},
            set(),
        ),
        ([], [("x", "20260401", "1")], None, {"x": ["20260401"]}, set()),
        ([("huge", "1111111", "19000101", "20991231")], [], None, {}, {"huge"}),
        (
            [("bad", "1111111", "2026x", "20260110")],
            [("wk", "2026-01-01", "1")],
            None,
            {},
            {"bad", "wk"},
        ),
        ([MON_TUE], [("wk", "20260106", None)], None, {}, {"wk"}),
        (
            [("bad", "11x0000", "20260105", "20260113"), MON_TUE],
            [("x", "20260401", "3")],
            None,
            {"wk": ["20260105", "20260106", "20260112", "20260113"]},
            {"bad", "x"},
        ),
        (
            [
                ("bad", "x111111", "20260101", "20260103"),
                ("short", "1111111", "20260101", "20260105"),
                ("long", *MON_TUE[1:]),
            ],
            [],
            5,
            {"short": JAN_1_TO_5},
            {"bad", "long"},
        ),
    ],
    ids=(
        "weekdays exceptions dates-only over-40000-days unparseable "
        "missing-column bad-values budget"
    ).split(),
)
def test_service_dates(monkeypatch, calendar, exceptions, budget, expected, unexpanded):
    from transitio.gtfs import _schedule

    if budget:
        monkeypatch.setattr(_schedule, "MAX_EXPANDED_DAYS", budget)
    tables = {}
    if calendar:
        tables["calendar.txt"] = _calendar(*calendar)
    if exceptions:
        columns = ("service_id", "date", "exception_type")
        table = pd.DataFrame(exceptions, columns=columns)
        tables["calendar_dates.txt"] = table.dropna(axis="columns")
    dates, left_out = _schedule.service_dates(tables)
    days = dates["date"].dt.strftime("%Y%m%d").groupby(dates["service_id"])
    assert {service: sorted(found) for service, found in days} == expected
    assert left_out == unexpanded
