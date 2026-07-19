import zipfile

import pytest

pytest.importorskip("transitio._core")

from transitio.repair import repair_feed  # noqa: E402
from transitio.validate import validate_feed  # noqa: E402

BROKEN = {
    "agency.txt": (
        "agency_id,agency_name,agency_url,agency_timezone\n"
        "hsl,HSL,https://hsl.fi,Europe/Helsinki\n"
    ),
    "stops.txt": (
        "stop_id,stop_name,stop_lat,stop_lon,wheelchair_boarding\n"
        "s1,Kamppi,60.169,24.931,9\n"
        "s2,Steissi,60.171,24.941,1\n"
        "s3,NoCoords,,,0\n"
    ),
    "routes.txt": "route_id,agency_id,route_short_name,route_type\nr1,hsl,1,3\n",
    "trips.txt": (
        "route_id,service_id,trip_id,shape_id\n"
        "r1,wk,t1,ghost-shape\n"
        "r1,wk,t2,\n"
        "ghost-route,wk,t3,\n"
    ),
    "stop_times.txt": (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "t1,08:00:00,08:00:00,s1,1\n"
        "t1,08:05:00,08:05:00,s2,2\n"
        "t2,09:00:00,09:00:00,s1,1\n"
        "t2,09:04:00,09:04:00,s3,2\n"
        "t3,10:00:00,10:00:00,s1,1\n"
        "t3,10:05:00,10:05:00,s2,2\n"
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


def test_repair_fixes_and_cascades(tmp_path):
    source = write_zip(tmp_path / "broken.zip", BROKEN)
    output = tmp_path / "repaired.zip"
    result = repair_feed(source, output, reference_date="20260601")

    actions = {(f["action"], f["filename"], f.get("field")) for f in result["fixes"]}
    # invalid wheelchair_boarding 9 -> default 0
    assert ("default_value", "stops.txt", "wheelchair_boarding") in actions
    # dangling optional shape reference cleared, trip survives
    assert ("clear_reference", "trips.txt", "shape_id") in actions
    # stop without coordinates dropped; t3 (ghost route) dropped
    dropped_files = {
        f["filename"] for f in result["fixes"] if f["action"] == "drop_entity"
    }
    assert "stops.txt" in dropped_files
    assert "trips.txt" in dropped_files

    remaining = {n["code"] for n in result["remaining_notices"]}
    assert "foreign_key_violation" not in remaining
    assert "stop_without_location" not in remaining
    assert "unexpected_enum_value" not in remaining
    check = validate_feed(output, reference_date="20260601")
    # t1 survived intact; t2 lost its s3 stop and became unusable
    assert check["row_counts"]["trips.txt"] == 1
    assert check["row_counts"]["stop_times.txt"] == 2


def test_repair_is_idempotent_on_clean_feeds(tmp_path):
    source = write_zip(tmp_path / "broken.zip", BROKEN)
    first = tmp_path / "first.zip"
    repair_feed(source, first, reference_date="20260601")
    second = tmp_path / "second.zip"
    result = repair_feed(first, second, reference_date="20260601")
    assert result["fixes"] == []


def test_repair_helsinki_roundtrip(tmp_path, helsinki_gtfs):
    output = tmp_path / "helsinki-repaired.zip"
    # The production feed exceeds the default notice cap, which repair
    # correctly refuses; raise the budget as the error message instructs.
    budget = {"max_notices_per_file": 1_000_000}
    before = validate_feed(helsinki_gtfs, reference_date="20220222", **budget)
    result = repair_feed(helsinki_gtfs, output, reference_date="20220222", **budget)
    assert result["service_window"] is not None
    check = validate_feed(output, reference_date="20220222")
    assert check["row_counts"]["stop_times.txt"] > 1000
    # the repaired feed must not be worse than the original
    errors_after = sum(1 for n in check["notices"] if n["severity"] == "ERROR")
    errors_before = sum(1 for n in before["notices"] if n["severity"] == "ERROR")
    assert errors_after <= errors_before


def test_repair_refuses_truncated_snapshots(tmp_path):
    source = write_zip(tmp_path / "broken.zip", BROKEN)
    with pytest.raises(OSError, match="budget"):
        repair_feed(source, tmp_path / "out.zip", max_rows=1)
    with pytest.raises(OSError, match="budget"):
        repair_feed(source, tmp_path / "out.zip", max_notices_per_file=1)


def test_repair_refuses_source_aliasing_output(tmp_path):
    source = write_zip(tmp_path / "broken.zip", BROKEN)
    with pytest.raises(OSError, match="aliases"):
        repair_feed(source, source)


def test_dangling_fare_selector_drops_rule(tmp_path):
    files = dict(
        BROKEN,
        **{
            "fare_attributes.txt": (
                "fare_id,price,currency_type,payment_method,transfers\n"
                "single,3.20,EUR,1,0\n"
            ),
            "fare_rules.txt": "fare_id,route_id\nsingle,ghost-route\n",
        },
    )
    source = write_zip(tmp_path / "broken.zip", files)
    result = repair_feed(source, tmp_path / "out.zip", reference_date="20260601")
    dropped = [
        f
        for f in result["fixes"]
        if f["action"] == "drop_entity" and f["filename"] == "fare_rules.txt"
    ]
    # Clearing the selector would broaden the fare; the rule is dropped.
    assert len(dropped) == 1
