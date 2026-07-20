"""Regression tests, one per fixed defect."""

import zipfile

import pytest

pytest.importorskip("transitio._core")

from transitio.gtfs import crop_feed  # noqa: E402
from transitio.repair import repair_feed  # noqa: E402

FEED = {
    "agency.txt": (
        "agency_id,agency_name,agency_url,agency_timezone\n"
        "hsl,HSL,https://hsl.fi,Europe/Helsinki\n"
        "espoo,Espoo,https://espoo.fi,Europe/Helsinki\n"
    ),
    "stops.txt": (
        "stop_id,stop_name,stop_lat,stop_lon\n"
        "in1,Kamppi,60.169,24.931\n"
        "in2,Steissi,60.171,24.941\n"
        "out1,Espoo,60.205,24.655\n"
    ),
    "routes.txt": (
        "route_id,agency_id,route_short_name,route_type\n"
        "r-in,hsl,1,3\nr-out,espoo,2,3\n"
    ),
    "trips.txt": "route_id,service_id,trip_id\nr-in,wk,t-in\nr-out,wk,t-out\n",
    "stop_times.txt": (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "t-in,08:00:00,08:00:00,in1,1\n"
        "t-in,08:05:00,08:05:00,in2,2\n"
        "t-out,09:00:00,09:00:00,out1,1\n"
        "t-out,09:30:00,09:30:00,out1,2\n"
    ),
    "calendar.txt": (
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
        "start_date,end_date\n"
        "wk,1,1,1,1,1,0,0,20260101,20261231\n"
    ),
    "calendar_dates.txt": (
        "service_id,date,exception_type\nwk,20260102,2\nwk,20260704,1\n"
    ),
}

CITY_BBOX = (24.9, 60.1, 25.0, 60.2)
WIDE_BBOX = (24.0, 60.0, 26.0, 61.0)


def write_zip(path, files):
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return path


def read_entry(path, name):
    with zipfile.ZipFile(path) as archive:
        return archive.read(name)


def test_polygon_and_envelope_get_distinct_crop_cache_names():
    # A polygon AOI and its bounding envelope used to share a cached
    # crop filename, so the first crop was silently reused for both.
    from shapely.geometry import Polygon, box

    from transitio.osm._fetch import _crop_filename

    triangle = Polygon([(24.6, 60.1), (25.2, 60.1), (25.2, 60.4)])
    envelope = box(*triangle.bounds)
    assert _crop_filename(triangle, triangle) != _crop_filename(envelope, envelope)
    assert _crop_filename(envelope, envelope).startswith("bbox_")
    assert _crop_filename((24.6, 60.1, 25.2, 60.4), envelope).startswith("bbox_")
    # Distinct place names normalizing to one slug stay distinct too.
    assert _crop_filename("Berlin!", envelope) != _crop_filename("Berlin?", envelope)


def test_single_bound_temporal_crop_clamps_calendars(tmp_path):
    # A start-only (or end-only) window used to skip calendar clamping
    # entirely, so the feed still advertised service outside the window.
    source = write_zip(tmp_path / "feed.zip", FEED)
    output = tmp_path / "cropped.zip"
    crop_feed(source, output, start_date="20260601", reference_date="20260601")
    calendar = read_entry(output, "calendar.txt").decode()
    row = calendar.splitlines()[1].split(",")
    assert row[-2] == "20260601"  # start_date clamped up
    assert row[-1] == "20261231"  # open end bound untouched
    dates = read_entry(output, "calendar_dates.txt").decode()
    assert "20260102" not in dates  # exception before the window dropped
    assert "20260704" in dates


def test_calendar_outside_one_sided_window_is_dropped(tmp_path):
    # A calendar wholly before a start-only window, kept alive by a
    # calendar_dates addition inside it, used to clamp into an invalid
    # start_date > end_date interval.
    files = dict(FEED)
    files["trips.txt"] += "r-in,old,t-old\n"
    files[
        "stop_times.txt"
    ] += "t-old,10:00:00,10:00:00,in1,1\nt-old,10:05:00,10:05:00,in2,2\n"
    files["calendar.txt"] += "old,1,1,1,1,1,0,0,20250101,20250630\n"
    files["calendar_dates.txt"] += "old,20260710,1\n"
    source = write_zip(tmp_path / "feed.zip", files)
    output = tmp_path / "cropped.zip"
    result = crop_feed(source, output, start_date="20260601", reference_date="20260601")
    assert result["row_counts"]["trips.txt"] == 3  # t-old retained
    calendar = read_entry(output, "calendar.txt").decode()
    assert "old" not in calendar  # empty clamped interval dropped
    dates = read_entry(output, "calendar_dates.txt").decode()
    assert "20260710" in dates


def test_attribution_of_pruned_agency_is_dropped(tmp_path):
    # An attribution referencing only a cropped-away agency survived and
    # left a dangling agency_id foreign key.
    files = dict(FEED)
    files["attributions.txt"] = (
        "attribution_id,agency_id,organization_name,is_producer\n"
        "a1,espoo,Espoo Data,1\na2,hsl,HSL Data,1\n"
    )
    source = write_zip(tmp_path / "feed.zip", files)
    output = tmp_path / "cropped.zip"
    result = crop_feed(source, output, aoi=CITY_BBOX, reference_date="20260601")
    attributions = read_entry(output, "attributions.txt").decode()
    assert "espoo" not in attributions
    assert "hsl" in attributions
    assert not any(
        n["code"] == "foreign_key_violation" for n in result["remaining_notices"]
    )


def test_repair_refuses_staging_that_aliases_source(tmp_path):
    # With a source literally named `<output>.part`, the staging cleanup
    # used to delete the input archive before writing.
    source = write_zip(tmp_path / "feed.zip.part", FEED)
    original = source.read_bytes()
    with pytest.raises(OSError, match="aliases the source"):
        repair_feed(source, tmp_path / "feed.zip", reference_date="20260601")
    assert source.read_bytes() == original


def test_unparsed_entries_survive_rewrites(tmp_path):
    # locations.geojson (GTFS-Flex) and unknown files were dropped by the
    # repair/crop archive rewrite because only parsed tables were written.
    geojson = b'{"type":"FeatureCollection","features":[]}'
    notes = b"hand-written operator notes\n"
    files = dict(FEED)
    source = tmp_path / "feed.zip"
    with zipfile.ZipFile(source, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
        archive.writestr("locations.geojson", geojson)
        archive.writestr("notes.md", notes)
        archive.writestr("extras/nested-junk.bin", b"nested")

    repaired = tmp_path / "repaired.zip"
    repair_feed(source, repaired, reference_date="20260601")
    assert read_entry(repaired, "locations.geojson") == geojson
    assert read_entry(repaired, "notes.md") == notes
    assert read_entry(repaired, "extras/nested-junk.bin") == b"nested"

    cropped = tmp_path / "cropped.zip"
    crop_feed(source, cropped, aoi=WIDE_BBOX, reference_date="20260601")
    assert read_entry(cropped, "locations.geojson") == geojson
    assert read_entry(cropped, "notes.md") == notes


def test_hostile_passthrough_names_are_not_copied(tmp_path):
    # Passthrough must not propagate Zip-Slip names, aliases of the
    # rewritten tables, or symlink entries into the repaired archive.
    source = tmp_path / "feed.zip"
    with zipfile.ZipFile(source, "w") as archive:
        for name, content in FEED.items():
            archive.writestr(name, content)
        archive.writestr("../evil.txt", b"escape")
        archive.writestr("STOPS.TXT", b"shadow")
        link = zipfile.ZipInfo("innocent-link")
        link.external_attr = 0o120777 << 16
        archive.writestr(link, b"/etc/passwd")
        archive.writestr("notes.md", b"kept")

    repaired = tmp_path / "repaired.zip"
    repair_feed(source, repaired, reference_date="20260601")
    with zipfile.ZipFile(repaired) as archive:
        names = archive.namelist()
    assert "notes.md" in names
    assert "../evil.txt" not in names
    assert "STOPS.TXT" not in names
    assert "innocent-link" not in names
