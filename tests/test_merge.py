import collections
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
        [build_city(), build_city()],
        output,
        duplicate_trips="keep",
        reference_date="20260601",
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


def _city_in(zone, agency_id="hsl", at=None):
    """``build_city`` declaring ``zone``, its stops moved to ``at`` (lat, lon)."""
    builder = build_city(agency_id)
    builder.tables["agency.txt"]["agency_timezone"] = zone
    if at is not None:
        builder.tables["stops.txt"]["stop_lat"] = [str(at[0]), str(at[0] + 0.002)]
        builder.tables["stops.txt"]["stop_lon"] = [str(at[1]), str(at[1] + 0.01)]
    return builder


HEL, UTC, OSLO = "Europe/Helsinki", "UTC", "Europe/Oslo"
CET, PARIS, NYC = "CET", "Europe/Paris", "America/New_York"
HONOLULU = "Pacific/Honolulu"
IN_OSLO = (59.911, 10.750)


@pytest.mark.parametrize(
    "zones, in_oslo, timezones, error, skipped",
    [
        ([HEL, UTC, HEL], (), "refuse", "differ", None),
        ([HEL, UTC, HEL], (), None, None, [(1, UTC, HEL)]),
        ([UTC, UTC, HEL, HEL], (), "skip", None, [(0, UTC, HEL), (1, UTC, HEL)]),
        ([HEL, OSLO], (1,), "skip", None, [(1, OSLO, OSLO)]),
        ([HEL, UTC, OSLO], (), "skip", None, [(1, UTC, HEL), (2, OSLO, HEL)]),
        ([HEL, HEL], (), "maybe", "must be", None),
        (
            [UTC, NYC],
            (),
            "refuse",
            re.escape(f"feed 0 (f1): {UTC}; feed 1 (f2): {NYC}"),
            None,
        ),
        ([UTC, CET, PARIS], (), "skip", None, [(0, UTC, HEL)]),
    ],
    ids=[
        "refused",
        "outlier-left-out",
        "stops-outweigh-count",
        "both-vouch-earliest",
        "one-left",
        "bad-option",
        "refusal-names-inputs",
        "equivalent-class-wins",
    ],
)
def test_feeds_of_another_time_zone(
    tmp_path, zones, in_oslo, timezones, error, skipped
):
    feeds = [
        _city_in(zone, f"a{i}", IN_OSLO if i in in_oslo else None)
        for i, zone in enumerate(zones)
    ]
    output = tmp_path / "merged.zip"
    options = {"reference_date": "20260601"}
    if timezones is not None:
        options["timezones"] = timezones
    if error:
        with pytest.raises(ValueError, match=error):
            merge_feeds(feeds, output, **options)
        return
    with pytest.warns(UserWarning, match="^left out feed "):
        report = merge_feeds(feeds, output, **options)
    assert report["skipped_feeds"] == [
        {"feed": feed, "timezones": [zone], "stop_timezone": located}
        for feed, zone, located in skipped
    ]
    # The feeds kept keep the prefixes they had among all the inputs.
    left = {feed for feed, _, _ in skipped}
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
            merge_feeds(paths, output, check=False, timezones="refuse")
        return
    report = merge_feeds(paths, output, check=False)
    assert report["timezone_interval"] == case["interval"]


@pytest.mark.parametrize(
    "points, expected",
    [
        ([("21.332", "-157.920")], HONOLULU),
        ([("60.169", "24.931"), ("21.332", "-157.920"), ("60.171", "24.941")], HEL),
        ([("30.0", "-30.0"), ("30.5", "-30.0"), ("21.332", "-157.920")], HONOLULU),
        ([("", "24.931"), ("x", "24.931"), ("95.0", "24.931")], None),
    ],
    ids=["one-zone", "most-stops", "sea-not-counted", "unlocated"],
)
def test_stop_zone(points, expected):
    from transitio.gtfs._merge import _stop_zone

    lat, lon = zip(*points)
    tables = {"stops.txt": frame(stop_lat=lat, stop_lon=lon)}
    assert _stop_zone(tables) == expected


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
    with pytest.raises(ValueError, match="duplicate_trips"):
        merge_tables([feed, feed], duplicate_trips="maybe")
    for check in ("maybe", 1):
        with pytest.raises(ValueError, match="check"):
            merge_feeds([feed, feed], "out.zip", check=check)


def _error(code, **context):
    return {"code": code, "severity": "ERROR", "context": context}


STOP_REF = {"childFilename": "stop_times.txt", "childFieldName": "stop_id"}
STOP_KEY = {"filename": "stops.txt", "fieldNames": "stop_id"}
ARRIVAL = {"filename": "stop_times.txt", "fieldName": "arrival_time"}


@pytest.mark.parametrize(
    "merged, first, second, inherited, introduced",
    [
        pytest.param(
            [
                _error(
                    "foreign_key_violation",
                    **STOP_REF,
                    fieldValue="f1:s9",
                    csvRowNumber=7,
                )
            ],
            [
                _error(
                    "foreign_key_violation", **STOP_REF, fieldValue="s9", csvRowNumber=3
                )
            ],
            [],
            [{"foreign_key_violation": 1}, {}],
            {},
            id="prefixed-id",
        ),
        pytest.param(
            [
                _error(
                    "block_trips_with_overlapping_stop_times",
                    blockId="f1:b",
                    tripIdA="f1:t1",
                    tripIdB="f2:t1",
                ),
                _error("duplicate_key", **STOP_KEY, oldCsvRowNumber=2, csvRowNumber=5),
                _error("duplicate_key", **STOP_KEY, oldCsvRowNumber=3, csvRowNumber=9),
            ],
            [
                _error(
                    "block_trips_with_overlapping_stop_times",
                    blockId="b",
                    tripIdA="t1",
                    tripIdB="t1",
                )
            ],
            [_error("duplicate_key", **STOP_KEY, oldCsvRowNumber=2, csvRowNumber=3)],
            [{}, {"duplicate_key": 1}],
            {"block_trips_with_overlapping_stop_times": 1, "duplicate_key": 1},
            id="several-or-no-inputs",
        ),
        pytest.param(
            [_error("invalid_time", **ARRIVAL, fieldValue="f1:30", csvRowNumber=2)],
            [_error("invalid_time", **ARRIVAL, fieldValue="f1:30", csvRowNumber=2)],
            [_error("invalid_time", **ARRIVAL, fieldValue="f1:30", csvRowNumber=2)],
            [{"invalid_time": 1}, {}],
            {},
            id="not-an-id",
        ),
    ],
)
def test_split_errors(merged, first, second, inherited, introduced):
    from transitio.gtfs._merge import _split_errors

    validations = [{"notices": notices} for notices in (merged, first, second)]
    found = _split_errors(validations[0], validations[1:], ["f1", "f2"])
    assert found == (inherited, introduced)


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
    builder.add_agency("b", "Other", "https://b.example", HEL)
    stops = {"s1": 60.1, "s2": 60.11, "s3": 60.12, "s4": 60.10001, "s5": 60.100001}
    for stop, lat in stops.items():
        builder.add_stop(stop, stop, lat, 24.9)
    builder.add_route("r1", 3, "1", agency_id="a", continuous_pickup=continuous)
    builder.add_route("r2", 3, "2", agency_id="a")
    builder.add_route("r3", 3, "1", agency_id="a", continuous_pickup="0")
    builder.add_route("r4", 3, "1", agency_id="b")
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
        ({"route": "r4"}, "equal"),
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
        "moved-within-rounding route-continuous-pickup other-agency other-shape "
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


def test_agency_keys():
    from transitio.gtfs._schedule import agency_keys

    cases = [
        ("Midland Bluebird Ltd", "midland bluebird"),
        ("MIDLAND BLUEBIRD Ltd.", "midland bluebird"),
        ("Transportes S.A.", "transportes"),
        ("Arriva B.V.", "arriva"),
        ("Craig of Campbeltown Limited", "craig of campbeltown"),
        ("Oy Pohjolan Liikenne Ab", "oy pohjolan liikenne"),
        ("AB", "ab"),
        ("Société", "societe"),
        ("Societe", "societe"),
        ("", ""),
    ]
    names, expected = zip(*cases)
    assert list(agency_keys(pd.Series(names))) == list(expected)


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


def _links(*pairs):
    return {(*ends, "", "") for pair in pairs for ends in (pair, pair[::-1])}


ONE = _trips({})
PAIR = _trips({"block_id": "b"}, {"times": LATER, "block_id": "b"})
LINKED = _trips({}, {"stops": ("s1", "s3"), "times": LATER})
FREQUENCY = (
    "frequencies.txt",
    dict(trip_id="t1", start_time="08:00:00", end_time="10:00:00", headway_secs="600"),
)
TRANSFER = (
    "transfers.txt",
    dict(from_stop_id="s2", to_stop_id="s2", from_trip_id="t1", to_trip_id="t2"),
)
EXTRA_DAY = (
    "calendar_dates.txt",
    dict(service_id="long", date="20370105", exception_type="1"),
)
SPLIT = _trips(
    {"block_id": "x", "service": "jan1"},
    {"times": LATER, "block_id": "x", "service": "jan1"},
    {"service": "jan2"},
    {"times": LATER, "service": "jan2"},
)

# Block b's trips come first by trip id, block a's first by block_id.
BLOCKS = _trips(
    {"block_id": "b", "stops": ("s5", "s2")},
    {"block_id": "b", "stops": ("s5", "s2"), "times": LATER},
    {"block_id": "a"},
    {"block_id": "a", "times": LATER},
)


@pytest.mark.parametrize(
    "inputs, dropped, expected",
    [
        ([ONE, ONE], {"f2:t1"}, {}),
        (
            [ONE, _trips({}, {})],
            {"f2:t1"},
            {"transfers": _links(("f2:s1", "f1:s1"), ("f2:s2", "f1:s2"))},
        ),
        (
            [ONE, _trips({"service": "jan1"}, {"service": "jan2"})],
            {"f2:t1", "f2:t2"},
            {},
        ),
        ([_trips({}, {"times": LATER}), PAIR], set(), {}),
        ([ONE, _trips({"service": "mid"})], {"f2:t1"}, {}),
        ([ONE, _trips({"service": "late"})], set(), {}),
        ([ONE, _trips({"times": LATER})], set(), {}),
        ([ONE, _trips({"route": "r2"})], set(), {}),
        ([ONE, {"trips": ONE, "rows": [FREQUENCY]}], set(), {}),
        ([{"trips": ONE, "rows": [FREQUENCY]}, ONE], set(), {}),
        (
            [ONE, {"trips": _trips({}, {"times": LATER}), "rows": [TRANSFER]}],
            set(),
            {"transfers": {("f2:s2", "f2:s2", "f2:t1", "f2:t2")}},
        ),
        ([ONE, PAIR], set(), {}),
        ([[{**trip, "block_id": "x"} for trip in PAIR], PAIR], {"f2:t1", "f2:t2"}, {}),
        ([SPLIT, PAIR], set(), {}),
        (
            [BLOCKS, [*PAIR, {"trip_id": "t3", "stops": ("s1", "s3")}]],
            {"f2:t1", "f2:t2"},
            {"transfers": _links(("f2:s1", "f1:s5"))},
        ),
        (
            [ONE, ONE, LINKED],
            {"f2:t1", "f3:t1"},
            {"transfers": _links(("f3:s1", "f1:s1"))},
        ),
        (
            [{"trips": ONE, "continuous": "0"}, {"trips": ONE, "continuous": "2"}],
            set(),
            {},
        ),
        ([ONE, LINKED], {"f2:t1"}, {"transfers": _links(("f2:s1", "f1:s1"))}),
        ([ONE, _trips({"stop_fields": {"pickup_type": ("1", "")}})], set(), {}),
        ([_trips({"shape_id": "sa"}), _trips({"shape_id": "sb"})], {"f2:t1"}, {}),
        (
            [
                _trips({"service": "long"}),
                {"trips": _trips({"service": "long"}), "rows": [EXTRA_DAY]},
            ],
            set(),
            {},
        ),
        ([_trips({"service": "huge"})] * 2, set(), {"unexpanded": 2}),
        ([ONE, ONE], set(), {"mode": "keep"}),
    ],
    ids=(
        "identical one-for-one disjoint-days block-vs-unblocked subset-days "
        "partial-overlap other-times other-route later-frequency "
        "earlier-frequency trip-transfer half-a-block block-for-block "
        "block-completed-elsewhere candidate-block-order three-inputs "
        "route-continuous-pickup "
        "stop-link other-pickup other-shape extra-day-past-4000 unexpanded keep"
    ).split(),
)
def test_duplicate_trips(tmp_path, inputs, dropped, expected):
    feeds = [
        _repeats(**spec if isinstance(spec, dict) else {"trips": spec})
        for spec in inputs
    ]
    output = tmp_path / "merged.zip"
    mode = expected.get("mode", "drop")
    report = merge_feeds(feeds, output, check=False, duplicate_trips=mode)
    merged = FeedEditor(output).tables
    every = {
        f"f{position + 1}:{trip_id}"
        for position, feed in enumerate(feeds)
        for trip_id in feed.tables["trips.txt"]["trip_id"]
    }
    assert set(merged["trips.txt"]["trip_id"]) == every - dropped
    assert set(merged["stop_times.txt"]["trip_id"]) == every - dropped
    # Kept trips keep their shapes, and unused shapes stay.
    shapes = set(merged.get("shapes.txt", frame(shape_id=[]))["shape_id"])
    assert set(merged["trips.txt"].get("shape_id", [""])) - {""} <= shapes
    columns = ["from_stop_id", "to_stop_id", "from_trip_id", "to_trip_id"]
    transfers = merged.get("transfers.txt", pd.DataFrame(columns=columns))
    rows = transfers.reindex(columns=columns).fillna("").itertuples(index=False)
    assert set(rows) == expected.get("transfers", set())
    by_feed = collections.Counter(int(trip[1]) - 1 for trip in dropped)
    assert report["duplicate_trips"] == {
        "dropped": len(dropped),
        "by_feed": dict(by_feed),
        "unexpanded_services": expected.get("unexpanded", 0),
        "stop_links": sum(not row[2] for row in expected.get("transfers", ())) // 2,
    }


def test_rows_of_dropped_trips_go():
    from transitio.gtfs._patch import _drop_trip_rows

    tables = {
        "trips.txt": frame(trip_id=["t1", "t2"]),
        "stop_times.txt": frame(trip_id=["t1", "t2"]),
        "transfers.txt": frame(from_trip_id=["t1", "t2"], to_trip_id=["t2", ""]),
        "attributions.txt": frame(trip_id=["t1", "t2"]),
    }
    _drop_trip_rows(tables, {"t1"}, [])
    for name in ("trips.txt", "stop_times.txt", "attributions.txt"):
        assert list(tables[name]["trip_id"]) == ["t2"]
    assert list(tables["transfers.txt"]["from_trip_id"]) == ["t2"]


def test_block_matching_takes_long_augmenting_paths():
    from transitio.gtfs._duplicates import _one_to_one

    # The last entry's only option is the first's, which moves every other
    # entry along, deeper than Python's recursion limit.
    count = 3000
    options = [[i, i + 1] for i in range(count - 1)] + [[0]]
    assert _one_to_one(options) == [*range(1, count), 0]
    assert _one_to_one([[0], [0]]) is None
