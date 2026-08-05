import sys
import zipfile

import pytest

pytest.importorskip("transitio._core")

from transitio.validate import validate_feed  # noqa: E402

MINIMAL = {
    "agency.txt": (
        "agency_id,agency_name,agency_url,agency_timezone\n"
        "hsl,HSL,https://hsl.fi,Europe/Helsinki\n"
    ),
    "stops.txt": (
        "stop_id,stop_name,stop_lat,stop_lon\n"
        "s1,Kamppi,60.169,24.931\n"
        "s2,Steissi,60.171,24.941\n"
    ),
    "routes.txt": "route_id,agency_id,route_short_name,route_type\nr1,hsl,1,3\n",
    "trips.txt": "route_id,service_id,trip_id\nr1,wk,t1\n",
    "stop_times.txt": (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "t1,08:00:00,08:00:00,s1,1\n"
        "t1,08:05:00,08:05:00,s2,2\n"
    ),
    "calendar.txt": (
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
        "start_date,end_date\n"
        "wk,1,1,1,1,1,0,0,20260101,20261231\n"
    ),
}


def write_zip(path, files):
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return path


def codes(report):
    return {notice["code"] for notice in report["notices"]}


def errors(report):
    return [n for n in report["notices"] if n["severity"] == "ERROR"]


def test_minimal_feed_is_error_free(tmp_path):
    report = validate_feed(write_zip(tmp_path / "feed.zip", MINIMAL))
    assert errors(report) == []
    assert report["row_counts"]["stop_times.txt"] == 2
    assert "missing_recommended_file" in codes(report)


def test_missing_required_file(tmp_path):
    files = {k: v for k, v in MINIMAL.items() if k != "stops.txt"}
    report = validate_feed(write_zip(tmp_path / "feed.zip", files))
    missing = [n for n in report["notices"] if n["code"] == "missing_required_file"]
    assert [n["context"]["filename"] for n in missing] == ["stops.txt"]


def test_missing_calendar_pair(tmp_path):
    files = {k: v for k, v in MINIMAL.items() if k != "calendar.txt"}
    report = validate_feed(write_zip(tmp_path / "feed.zip", files))
    assert "missing_calendar_and_calendar_date_files" in codes(report)


def test_duplicate_key(tmp_path):
    files = dict(
        MINIMAL, **{"trips.txt": "route_id,service_id,trip_id\nr1,wk,t1\nr1,wk,t1\n"}
    )
    report = validate_feed(write_zip(tmp_path / "feed.zip", files))
    (dup,) = [n for n in report["notices"] if n["code"] == "duplicate_key"]
    assert dup["context"]["filename"] == "trips.txt"
    assert dup["context"]["oldCsvRowNumber"] == 2
    assert dup["context"]["csvRowNumber"] == 3


def test_structural_column_and_row_notices(tmp_path):
    files = dict(
        MINIMAL,
        **{"routes.txt": "route_id,route_id,,route_short_name\nr1,r1,x,1,EXTRA\n,,,\n"},
    )
    report = validate_feed(write_zip(tmp_path / "feed.zip", files))
    found = codes(report)
    for expected in (
        "duplicated_column",
        "empty_column_name",
        "missing_required_column",
        "invalid_row_length",
        "empty_row",
        "empty_file",  # every routes row was dropped
    ):
        assert expected in found


def test_header_only_required_file_is_not_clean(tmp_path):
    files = dict(MINIMAL, **{"stops.txt": "stop_id,stop_name,stop_lat,stop_lon\n"})
    report = validate_feed(write_zip(tmp_path / "feed.zip", files))
    empty = [n for n in report["notices"] if n["code"] == "empty_file"]
    assert [n["context"]["filename"] for n in empty] == ["stops.txt"]
    assert "stops.txt" not in report["row_counts"]


def test_padded_header_not_silently_repaired(tmp_path):
    files = dict(MINIMAL, **{"trips.txt": " route_id,service_id,trip_id\nr1,wk,t1\n"})
    report = validate_feed(write_zip(tmp_path / "feed.zip", files))
    assert "leading_or_trailing_whitespaces" in codes(report)
    assert "missing_required_column" in codes(report)


def test_duplicate_zip_entries_are_refused(tmp_path):
    # Zip readers disagree on which occurrence of a duplicated entry wins,
    # so the ambiguous table is refused with a notice instead of validated.
    path = tmp_path / "feed.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in MINIMAL.items():
            archive.writestr(name, content)
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("trips.txt", "route_id,service_id,trip_id\nr9,wk,t9\n")
    report = validate_feed(path)
    dup = [n for n in report["notices"] if n["code"] == "duplicate_zip_entry"]
    assert [n["context"]["filename"] for n in dup] == ["trips.txt"]
    assert "trips.txt" not in report["row_counts"]
    # present but ambiguous is not "missing"
    assert "missing_required_file" not in codes(report)


def test_undecodable_row_skipped_table_kept(tmp_path):
    path = tmp_path / "feed.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in MINIMAL.items():
            if name != "stops.txt":
                archive.writestr(name, content)
        archive.writestr(
            "stops.txt", b"stop_id,stop_name\ns1,Kamppi\ns2,\xff\xfe\ns3,Steissi\n"
        )
    report = validate_feed(path)
    assert "invalid_character" in codes(report)
    assert report["row_counts"]["stops.txt"] == 2


def test_unknown_and_nested_files(tmp_path):
    files = dict(MINIMAL, **{"notes.txt": "hello\n", "nested/agency.txt": "x\ny\n"})
    report = validate_feed(write_zip(tmp_path / "feed.zip", files))
    assert "unknown_file" in codes(report)
    assert "invalid_input_files_in_subfolder" in codes(report)


def test_empty_file(tmp_path):
    files = dict(MINIMAL, **{"shapes.txt": ""})
    report = validate_feed(write_zip(tmp_path / "feed.zip", files))
    assert "empty_file" in codes(report)


def test_field_rules_reach_python(tmp_path):
    files = dict(
        MINIMAL,
        **{
            "trips.txt": "route_id,service_id,trip_id\nr1,ghost-service,t1\n",
            "calendar.txt": (
                "service_id,monday,tuesday,wednesday,thursday,friday,saturday,"
                "sunday,start_date,end_date\nwk,1,1,1,1,1,0,0,20260101,notadate\n"
            ),
        },
    )
    report = validate_feed(write_zip(tmp_path / "feed.zip", files))
    found = codes(report)
    assert "invalid_date" in found
    assert "foreign_key_violation" in found


def test_minimal_feed_clean_across_all_tiers(tmp_path):
    report = validate_feed(
        write_zip(tmp_path / "feed.zip", MINIMAL), reference_date="20260601"
    )
    assert errors(report) == []


def test_semantic_tier_and_service_window(tmp_path):
    files = dict(
        MINIMAL,
        **{
            "stop_times.txt": (
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "t1,08:10:00,08:10:00,s1,1\n"
                "t1,08:05:00,08:05:00,s2,2\n"
            )
        },
    )
    report = validate_feed(
        write_zip(tmp_path / "feed.zip", files), reference_date="20260601"
    )
    assert "stop_time_with_arrival_before_previous_departure_time" in codes(report)
    assert report["service_window"] == ["20260101", "20261231"]


def test_expired_calendar_uses_reference_date(tmp_path):
    report = validate_feed(
        write_zip(tmp_path / "feed.zip", MINIMAL), reference_date="20270601"
    )
    assert "expired_calendar" in codes(report)


def test_reference_date_targets_service_checks(tmp_path):
    report = validate_feed(
        write_zip(tmp_path / "feed.zip", MINIMAL), reference_date="20270601"
    )
    assert "no_service_on_reference_date" in codes(report)


def test_reference_time_targets_moment(tmp_path):
    feed = write_zip(tmp_path / "feed.zip", MINIMAL)
    report = validate_feed(feed, reference_date="20260601", reference_time="22:00")
    assert "no_trips_at_reference_time" in codes(report)
    report = validate_feed(feed, reference_date="20260601", reference_time="08:03")
    assert "no_trips_at_reference_time" not in codes(report)


def test_reference_time_requires_date(tmp_path):
    feed = write_zip(tmp_path / "feed.zip", MINIMAL)
    with pytest.raises(ValueError):
        validate_feed(feed, reference_time="08:00")


def test_invalid_reference_time(tmp_path):
    feed = write_zip(tmp_path / "feed.zip", MINIMAL)
    for bad in ("25:00", "8am", "08:60", "080000", "8:0", "8:00"):
        with pytest.raises(ValueError):
            validate_feed(feed, reference_date="20260601", reference_time=bad)


def test_readiness_predicts_distance_tiers(tmp_path):
    files = dict(
        MINIMAL,
        **{
            "trips.txt": "route_id,service_id,trip_id,shape_id\nr1,wk,t1,sh1\n",
            "shapes.txt": (
                "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n"
                "sh1,60.169,24.931,1\n"
                "sh1,60.170,24.936,2\n"
                "sh1,60.171,24.941,3\n"
            ),
        },
    )
    report = validate_feed(write_zip(tmp_path / "feed.zip", files))
    distances = report["readiness"]["distances"]
    assert distances["predicted"]["shape_linref"] == 1
    assert distances["verdict"] == "full"
    assert distances["cafein"] == "0.10.0"


def test_readiness_reports_fares(tmp_path):
    files = dict(
        MINIMAL,
        **{
            "fare_attributes.txt": (
                "fare_id,price,currency_type,payment_method,transfers\n"
                "f1,2.80,EUR,0,0\n"
            )
        },
    )
    report = validate_feed(write_zip(tmp_path / "feed.zip", files))
    fares = report["readiness"]["fares"]
    assert fares["v1"] == {
        "fares": 1,
        "priceable": 1,
        "routeCompatibility": 1.0,
        "agencyAmbiguous": False,
    }
    assert fares["transferPricing"] == "present"
    assert fares["verdict"] == "computable"
    report = validate_feed(write_zip(tmp_path / "bare.zip", MINIMAL))
    assert report["readiness"]["fares"]["verdict"] == "absent"
    assert "no_fare_information" in codes(report)


def test_omitted_reference_date_runs_no_moment_checks(tmp_path):
    report = validate_feed(write_zip(tmp_path / "feed.zip", MINIMAL))
    moment_codes = {
        "no_service_on_reference_date",
        "no_trips_at_reference_time",
        "service_level_below_baseline",
        "route_inactive_on_reference_date",
    }
    assert not (codes(report) & moment_codes)


def test_row_cap_is_configurable(tmp_path):
    report = validate_feed(write_zip(tmp_path / "feed.zip", MINIMAL), max_rows=1)
    assert "too_many_rows" in codes(report)
    assert report["row_counts"]["stop_times.txt"] == 1


def test_not_a_zip_raises(tmp_path):
    bogus = tmp_path / "feed.zip"
    bogus.write_text("plain text")
    with pytest.raises(OSError, match="zip"):
        validate_feed(bogus)


def test_non_utf8_filename(tmp_path):
    if sys.platform == "win32":
        pytest.skip("lone-surrogate names hit a PyO3 str->PathBuf panic on Windows")
    name = b"feed-\xe4.zip".decode("utf-8", "surrogateescape")
    path = tmp_path / name
    try:
        write_zip(path, MINIMAL)
    except OSError:
        pytest.skip("filesystem rejects non-UTF-8 filenames")
    report = validate_feed(path)
    assert report["row_counts"]["stop_times.txt"] == 2
