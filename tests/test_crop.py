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
