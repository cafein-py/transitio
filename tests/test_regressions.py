"""Regression tests, one per fixed defect."""

import csv
import io
import zipfile

import pytest

pytest.importorskip("transitio._core")

from transitio.gtfs import crop_feed  # noqa: E402
from transitio.repair import repair_feed  # noqa: E402
from transitio.validate import validate_feed  # noqa: E402

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


def test_route_network_ids_are_not_dangling_references(tmp_path):
    # routes.network_id was checked against networks.txt, so a feed without
    # that file had every value reported as a foreign_key_violation error,
    # and repair cleared them all.
    files = dict(FEED)
    files["routes.txt"] = (
        "route_id,agency_id,route_short_name,route_type,network_id\n"
        "r-in,hsl,1,3,CTS\nr-out,espoo,2,3,CTS\n"
    )
    source = write_zip(tmp_path / "feed.zip", files)
    repaired = tmp_path / "repaired.zip"
    result = repair_feed(source, repaired, reference_date="20260601")
    assert not any(
        n["code"] == "foreign_key_violation" for n in result["remaining_notices"]
    )
    routes = csv.DictReader(io.StringIO(read_entry(repaired, "routes.txt").decode()))
    assert [row["network_id"] for row in routes] == ["CTS", "CTS"]


def test_bytes_after_the_end_record_do_not_refuse_the_archive(tmp_path):
    # A zip with bytes after its end-of-central-directory record was "not a
    # readable zip", though Python's zipfile reads it.
    source = write_zip(tmp_path / "feed.zip", FEED)
    with open(source, "ab") as handle:
        handle.write(b"\0\0")
    report = validate_feed(source, reference_date="20260601")
    assert report["row_counts"]["stops.txt"] == 3


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


def test_area_search_ranks_local_feed_over_continental_aggregate(tmp_path):
    # an aggregate whose huge bounding rectangle sweeps over the searched
    # area must not outrank or crowd out the local feed (issue seen with a
    # Turku-area search returning German/Swedish/Norwegian aggregates)
    from transitio.catalog._csv import search_csv

    header = (
        "id,data_type,status,is_official,provider,"
        "location.country_code,location.subdivision_name,location.municipality,"
        "location.bounding_box.minimum_latitude,"
        "location.bounding_box.maximum_latitude,"
        "location.bounding_box.minimum_longitude,"
        "location.bounding_box.maximum_longitude,"
        "urls.direct_download,urls.latest,urls.license"
    )
    rows = [
        "mdb-1,gtfs,active,True,Aggregate,DE,,,39.0,79.0,0.0,159.0,,,",
        "mdb-2,gtfs,active,True,HSL,FI,Uusimaa,Helsinki,59.9,60.6,24.2,25.6,,,",
    ]
    path = tmp_path / "feeds_v2.csv"
    path.write_text("\n".join([header, *rows]) + "\n")
    helsinki = (24.6, 60.1, 25.2, 60.4)
    ranked = search_csv(path, bounds=helsinki)
    assert [feed.id for feed in ranked] == ["mdb-2", "mdb-1"]
    # the limit applies after ranking, not in catalogue file order
    assert [feed.id for feed in search_csv(path, bounds=helsinki, limit=1)] == ["mdb-2"]


def test_a_city_is_not_outranked_by_the_places_named_after_it():
    # Augsburg's name is shared by three metros in its country, one per metro
    # definition, and by a containing region with about the same service;
    # London's only exact-match city is in Canada, the metros sharing its name
    # in the UK; New York State carries far more service than the city; and
    # Hamilton's busier British metro must not win once the Canadian one, the
    # city's namesake, is set aside.
    pandas = pytest.importorskip("pandas")
    from transitio.exceptions import AmbiguousPlaceError
    from transitio.index.places import _PlaceLookup

    def place(place_id, kind, name, country, parent=None):
        return {
            "place_id": place_id,
            "kind": kind,
            "name": name,
            "names": {"en": name},
            "aliases": [],
            "parent_id": parent,
            "default_metro_id": None,
            "metro_ids": [],
            "member_ids": [],
            "country_code": country,
            "source_subtype": None,
        }

    feeds = {
        "c-aug": 30,
        "r-aug": 31,
        "c-ny": 68,
        "r-ny": 165,
        "c-ham": 5,
        "m-ham-ca": 40,
        "m-ham-gb": 60,
    }
    lookup = _PlaceLookup(
        pandas.DataFrame(
            [
                place("r-aug", "region", "Augsburg", "DE"),
                place("c-aug", "city", "Augsburg", "DE", parent="r-aug"),
                place("m-aug-1", "metro", "Augsburg", "DE"),
                place("m-aug-2", "metro", "Augsburg", "DE"),
                place("m-aug-3", "metro", "Augsburg", "DE"),
                place("c-lon", "city", "London", "CA"),
                place("m-lon-1", "metro", "London", "GB"),
                place("m-lon-2", "metro", "London", "GB"),
                place("r-ny", "region", "New York", "US"),
                place("c-ny", "city", "New York", "US", parent="r-ny"),
                place("c-ham", "city", "Hamilton", "CA"),
                place("m-ham-ca", "metro", "Hamilton", "CA"),
                place("m-ham-gb", "metro", "Hamilton", "GB"),
            ]
        ),
        feed_count=lambda place_id: feeds.get(place_id, 0),
    )
    assert lookup.resolve("Augsburg").id == "c-aug"
    for name in ("London", "New York", "Hamilton"):
        with pytest.raises(AmbiguousPlaceError):
            lookup.resolve(name)


def test_the_feed_margin_does_not_favour_an_alias_over_a_name():
    # Saint Paul, Minnesota carries "São Paulo" as an alias and more feeds than
    # São Paulo itself: the name stays ambiguous rather than naming Saint Paul,
    # and a qualifier picks São Paulo; a translation still reaches Vienna; and
    # a district listing its city's name as an alias is no rival to the city.
    pandas = pytest.importorskip("pandas")
    from transitio.exceptions import AmbiguousPlaceError
    from transitio.index.places import _PlaceLookup

    def place(place_id, kind, name, country, aliases=(), names=None, parent=None):
        return {
            "place_id": place_id,
            "kind": kind,
            "name": name,
            "names": names or {"en": name},
            "aliases": list(aliases),
            "parent_id": parent,
            "default_metro_id": None,
            "metro_ids": [],
            "member_ids": [],
            "country_code": country,
            "source_subtype": None,
        }

    # Kingston: an aliased city abroad would win the narrowed contest on feeds;
    # the veto must not hand the name to the busy metro set aside as a namesake.
    feeds = {"c-stp": 7, "c-sp": 2, "c-vie": 27, "m-wien": 30}
    feeds.update({"c-kin": 5, "c-kin-us": 30, "m-kin": 100})
    feeds.update({"c-bog": 6, "c-pa": 5, "m-bog": 6})
    lookup = _PlaceLookup(
        pandas.DataFrame(
            [
                place("br", "country", "Brazil", "BR"),
                place("c-sp", "city", "São Paulo", "BR"),
                place("c-stp", "city", "Saint Paul", "US", aliases=["São Paulo"]),
                place("c-vie", "city", "Vienna", "AT", names={"de": "Wien"}),
                place("m-wien", "metro", "Wien", "AT"),
                place("c-kin", "city", "Kingston", "JM"),
                place("m-kin", "metro", "Kingston", "JM"),
                place("c-kin-us", "city", "Port Kingston", "US", aliases=["Kingston"]),
                place("c-bog", "city", "Bogotá", "CO"),
                place("m-bog", "metro", "Bogotá", "CO"),
                place(
                    "c-pa", "city", "Puente Aranda", "CO", ["Bogotá"], parent="c-bog"
                ),
            ]
        ),
        feed_count=lambda place_id: feeds.get(place_id, 0),
    )
    with pytest.raises(AmbiguousPlaceError):
        lookup.resolve("Sao Paulo")
    assert lookup.resolve("Sao Paulo, Brazil").id == "c-sp"
    assert lookup.resolve("Wien").id == "c-vie"
    with pytest.raises(AmbiguousPlaceError):
        lookup.resolve("Kingston")
    assert lookup.resolve("Bogota").id == "c-bog"


def test_padded_header_names_merge_into_one_column(tmp_path):
    # A padded agency.txt header used to reach the merge verbatim, which
    # then wrote both agency_name and " agency_name", and the padded id
    # column escaped the per-feed prefix.
    from transitio.edit import FeedEditor
    from transitio.gtfs import merge_feeds

    padded = dict(FEED)
    padded["agency.txt"] = FEED["agency.txt"].replace(
        "agency_id,agency_name", " agency_id , agency_name", 1
    )
    feeds = [
        write_zip(tmp_path / "padded.zip", padded),
        write_zip(tmp_path / "clean.zip", FEED),
    ]
    output = tmp_path / "merged.zip"
    report = merge_feeds(feeds, output, reference_date="20260601")
    assert not any(n["severity"] == "ERROR" for n in report["notices"])
    header = read_entry(output, "agency.txt").decode().splitlines()[0]
    assert header == "agency_id,agency_name,agency_url,agency_timezone"
    merged = FeedEditor(output).tables
    agencies = ["f1:hsl", "f1:espoo", "f2:hsl", "f2:espoo"]
    assert list(merged["agency.txt"]["agency_id"]) == agencies
    assert set(merged["routes.txt"]["agency_id"]) == set(agencies)
    assert report["header_fixes"] == [
        {
            "feed": 0,
            "file": "agency.txt",
            "columns": [
                {"from": [" agency_id "], "to": "agency_id"},
                {"from": [" agency_name"], "to": "agency_name"},
            ],
        }
    ]
    clean = merge_feeds(
        [feeds[1], feeds[1]], tmp_path / "twice.zip", reference_date="20260601"
    )
    assert clean["header_fixes"] == []


@pytest.mark.parametrize(
    "form", ["clean", "padded-lines", "space-after-comma", "bom", "repeated-name"]
)
def test_padded_header_names_keep_the_selector_trusted(tmp_path, form):
    # A feed padding its header names, as Renfe pads every line and Metra each
    # header comma, recomputed a fingerprint at fetch that differed from the
    # build's, so its selector was judged stale.
    from types import SimpleNamespace

    from transitio.index import fingerprint
    from transitio.pipeline._fetch import _selector_trusted

    members = {
        "routes.txt": "route_id,agency_id,route_type\nr1,a,3\nr2,a,2\n",
        "stops.txt": (
            "stop_id,stop_lon,stop_lat\ns1,24.9,60.2\ns2,25.0,60.3\ns3,25.1,60.4\n"
        ),
        "trips.txt": "route_id,trip_id,service_id\nr1,t1,wk\nr2,t2,wk\n",
        "stop_times.txt": (
            "trip_id,stop_id,stop_sequence,pickup_type,drop_off_type\n"
            "t1,s1,1,0,0\nt1,s2,2,0,0\nt1,s3,3,1,1\nt2,s2,1,0,0\n"
        ),
    }
    for name, text in members.items():
        header, rows = text.split("\n", 1)
        if form == "padded-lines":
            members[name] = "".join(f"{line}  \n" for line in text.splitlines())
        elif form == "space-after-comma":
            members[name] = f"{header.replace(',', ', ')}\n{rows}"
        elif form == "bom":
            members[name] = "\N{BYTE ORDER MARK} " + text
    if form == "repeated-name":
        members["routes.txt"] = (
            "route_id,agency_id,route_type, route_type\nr1,a,3,700\nr2,a,2,700\n"
        )
    stored = fingerprint.compute(
        "route_stops",
        {
            "r1": {"route_type": 3, "agency_id": "a"},
            "r2": {"route_type": 2, "agency_id": "a"},
        },
        {"s1": (24.9, 60.2), "s2": (25.0, 60.3), "s3": (25.1, 60.4)},
        {"r1": {"s1", "s2"}, "r2": {"s2"}},
    )
    edge = SimpleNamespace(
        fingerprint_kind="route_stops", classification_fingerprint=stored
    )
    feed = SimpleNamespace(edges={"e1": edge})
    path = write_zip(tmp_path / "feed.zip", members)
    trusted = _selector_trusted(path, feed, SimpleNamespace(state="complete"))
    assert trusted == (True, None, {"r1", "r2"})


def test_padded_values_are_read_trimmed(tmp_path):
    # Values were read as written: a feed padding every line, as Renfe pads
    # its lines with about 150 spaces, had no calendar end_date, so fetch
    # skipped it as idle, and a parent_station of one space, as York Region
    # writes it, was a dangling reference.
    import datetime

    from transitio.gtfs import merge_feeds
    from transitio.pipeline._fetch import _process_feed, _service

    files = dict(
        FEED,
        **{
            "stops.txt": (
                "stop_id,stop_name,parent_station,stop_lat,stop_lon\n"
                "in1,Kamppi, ,60.169,24.931\n"
                "in2,Steissi,,60.171,24.941\n"
                "out1,Espoo,,60.205,24.655\n"
            )
        },
    )
    padded = {
        name: "".join(f"{line}{' ' * 150}\n" for line in text.splitlines())
        for name, text in files.items()
    }
    source = write_zip(tmp_path / "padded.zip", padded)

    def trimmed(notices):
        return sorted(
            n["context"]["filename"]
            for n in notices
            if n["code"] == "leading_or_trailing_whitespaces"
        )

    report = validate_feed(source, reference_date="20260601")
    assert report["service_window"] == ["20260101", "20261231"]
    assert report["moment"]["activeTrips"] == 2
    assert "foreign_key_violation" not in {n["code"] for n in report["notices"]}
    assert trimmed(report["notices"]) == sorted(padded)

    cropped = crop_feed(
        source, tmp_path / "cropped.zip", aoi=CITY_BBOX, reference_date="20260601"
    )
    assert trimmed(cropped["source_notices"]) == sorted(padded)

    day = datetime.date(2026, 6, 1)
    kept = _process_feed(
        source,
        geometry=CITY_BBOX,
        tag="study",
        repair=False,
        crop=True,
        modes={"bus"},
        day=day,
        study=True,
        hosted=None,
        budgets={"reference_date": "20260601"},
    )
    groups = {group["code"]: group for group in kept[1]["notices"]}
    assert groups["leading_or_trailing_whitespaces"]["totalNotices"] == len(padded)
    assert _service(source, day) is not None

    clean = write_zip(tmp_path / "clean.zip", FEED)
    merged = merge_feeds([source, clean], tmp_path / "merged.zip")
    assert not any(n["severity"] == "ERROR" for n in merged["notices"])
    assert [(entry["feed"], entry["file"]) for entry in merged["trimmed_values"]] == [
        (0, name) for name in sorted(padded)
    ]


def test_cropped_fares_never_apply_more_widely(tmp_path):
    # The crop kept fare_rules rows naming zones only removed stops carried,
    # kept fares of a pruned agency, and dropped fares that had no rules.
    files = dict(FEED)
    files["stops.txt"] = (
        "stop_id,stop_name,stop_lat,stop_lon,zone_id\n"
        "in1,Kamppi,60.169,24.931,A\n"
        "in2,Steissi,60.171,24.941,B\n"
        "out1,Espoo,60.205,24.655,C\n"
    )
    files["fare_attributes.txt"] = (
        "fare_id,price,currency_type,payment_method,transfers,agency_id\n"
        + "".join(f"f{n},3.10,EUR,0,,hsl\n" for n in range(1, 9))
        + "f9,2.00,EUR,0,,espoo\n"
    )
    files["fare_rules.txt"] = (
        "fare_id,route_id,origin_id,destination_id,contains_id\n"
        "f1,,A,B,\n"
        "f2,,A,C,\n"
        "f3,,,,A\nf3,,,,B\n"
        "f4,,,,A\nf4,,,,C\nf4,r-in,,,\n"
        "f5,r-in,,,\nf5,r-out,,,\n"
        "f6,r-out,,,\nf6,,A,B,\n"
        "f7,,Z,,\n"
    )
    source = write_zip(tmp_path / "feed.zip", files)
    output = tmp_path / "cropped.zip"
    result = crop_feed(source, output, aoi=CITY_BBOX, reference_date="20260601")

    def rows(name):
        return list(csv.DictReader(io.StringIO(read_entry(output, name).decode())))

    assert [tuple(row.values()) for row in rows("fare_rules.txt")] == [
        ("f1", "", "A", "B", ""),
        ("f3", "", "", "", "A"),
        ("f3", "", "", "", "B"),
        ("f5", "r-in", "", "", ""),
    ]
    fares = [row["fare_id"] for row in rows("fare_attributes.txt")]
    assert fares == ["f1", "f3", "f5", "f8"]
    assert not any(
        n["code"] == "foreign_key_violation" for n in result["remaining_notices"]
    )


INHERITED = [{"feed": 0, "errors": 1, "codes": {"foreign_key_violation": 1}}]


@pytest.mark.parametrize(
    "check, variant, refusal, inherited, introduced",
    [
        (True, None, None, INHERITED, {"errors": 0, "codes": {}}),
        ("strict", None, "1 of them inherited", INHERITED, {"errors": 0, "codes": {}}),
        (False, None, None, None, None),
        (
            True,
            "dangling-stop",
            r"introduced 1 .*\(foreign_key_violation 1\)",
            INHERITED,
            {"errors": 1, "codes": {"foreign_key_violation": 1}},
        ),
        (
            True,
            "input-changed-after-read",
            r"introduced 1 .*\(foreign_key_violation 1\)",
            INHERITED,
            {"errors": 1, "codes": {"foreign_key_violation": 1}},
        ),
        (True, "capped", None, INHERITED, {"errors": 0, "codes": {}}),
        (True, "sampled", "cannot tell", None, None),
    ],
    ids=[
        "inherited",
        "strict",
        "unchecked",
        "introduced",
        "input-changed-after-read",
        "capped",
        "sampled",
    ],
)
def test_merge_refuses_only_errors_it_introduced(
    tmp_path, monkeypatch, check, variant, refusal, inherited, introduced
):
    # A merge refused every error-severity notice of the merged feed, those
    # its inputs already carried included, so feeds merged clean only when
    # every input was clean.
    from transitio.edit import FeedBuilder
    from transitio.exceptions import InvalidFeedError
    from transitio.gtfs import _merge, merge_feeds

    def feed(start, end):
        builder = FeedBuilder()
        builder.add_agency("a", "Agency", "https://a.example", "Europe/Helsinki")
        builder.add_stop("s1", "First", 60.169, 24.931)
        builder.add_stop("s2", "Second", 60.171, 24.941)
        builder.add_route("r1", 3, "1", agency_id="a")
        builder.add_service("wk", "weekdays", "20260101", "20261231")
        builder.add_trip("r1", "wk", "t1", [("s1", start, start), ("s2", end, end)])
        return builder

    first = feed("08:00:00", "08:05:00")
    first.insert_rows(
        "attributions.txt",
        [{"route_id": "gone", "organization_name": "Org", "is_operator": "1"}],
    )
    second = feed("09:00:00", "09:05:00")
    if variant in ("dangling-stop", "input-changed-after-read"):
        merge_tables = _merge._merge_tables

        def dangling(*args, **kwargs):
            tables, dropped, details = merge_tables(*args, **kwargs)
            stops = tables["stops.txt"]
            tables["stops.txt"] = stops[stops["stop_id"] != "f2:s2"]
            if variant == "input-changed-after-read":
                # The caller's feed now carries the error the merge introduced.
                second.delete_rows("stops.txt", [1])
            return tables, dropped, details

        monkeypatch.setattr(_merge, "_merge_tables", dangling)
    if variant == "sampled":
        monkeypatch.setattr("transitio.validate._structure.CERTIFY_NOTICE_BUDGET", 0)
    feeds = [first, second]
    output = tmp_path / "merged.zip"
    budgets = {"reference_date": "20260601"}
    if variant == "capped":
        budgets["max_notices_per_file"] = 0
    if refusal is None:
        report = merge_feeds(feeds, output, check=check, **budgets)
    else:
        with pytest.raises(InvalidFeedError, match=refusal) as refused:
            merge_feeds(feeds, output, check=check, **budgets)
        report = refused.value.report
    assert output.exists()
    assert report["inherited_errors"] == inherited
    assert report["introduced_errors"] == introduced


def _rider_entry(position, wildcards=()):
    return {
        "feed": position,
        "defaults": [f"f{position + 1}:adult"],
        "wildcard_products": list(wildcards),
    }


@pytest.mark.parametrize(
    "defaults, open_products, expected",
    [
        (["1", "1"], [], [_rider_entry(0), _rider_entry(1)]),
        (["1", "1"], ["day"], [_rider_entry(0, ["f1:day"]), _rider_entry(1)]),
        (["", "1"], ["day"], []),
    ],
    ids=["a-default-each", "open-product", "one-default"],
)
def test_merge_keeps_each_inputs_default_rider_category(
    tmp_path, defaults, open_products, expected
):
    # A merge refused inputs that each declared a default rider category,
    # though GTFS sets the default per fare product, not per feed.
    from transitio.edit import FeedBuilder, FeedEditor
    from transitio.gtfs import merge_feeds

    feeds = []
    for default in defaults:
        builder = FeedBuilder()
        builder.insert_rows(
            "rider_categories.txt",
            [
                {"rider_category_id": "adult", "is_default_fare_category": default},
                {"rider_category_id": "child", "is_default_fare_category": "0"},
            ],
        )
        builder.insert_rows(
            "fare_products.txt",
            [
                {"fare_product_id": rider, "rider_category_id": rider}
                for rider in ("adult", "child")
            ],
        )
        feeds.append(builder)
    feeds[0].insert_rows(
        "fare_products.txt",
        [
            {"fare_product_id": product, "rider_category_id": ""}
            for product in open_products
        ],
    )
    output = tmp_path / "merged.zip"
    report = merge_feeds(feeds, output, check=False)
    riders = FeedEditor(output).tables["rider_categories.txt"]
    flagged = riders["is_default_fare_category"] == "1"
    assert list(riders.loc[flagged, "rider_category_id"]) == [
        f"f{position}:adult" for position, flag in enumerate(defaults, 1) if flag
    ]
    assert report["rider_defaults"] == expected


def test_merge_leaves_out_the_feed_whose_stops_lie_in_another_time_zone(tmp_path):
    # Two feeds declaring different time zones were refused, and skipping one
    # kept the earlier feed; RIO Limo declares America/New_York for its stops
    # at Honolulu's airport, beside TheBus in Pacific/Honolulu.
    import re

    from transitio.edit import FeedEditor
    from transitio.gtfs import merge_feeds

    stops = (
        "stop_id,stop_name,stop_lat,stop_lon\n"
        "in1,Terminal 1,21.332,-157.920\n"
        "in2,Terminal 2,21.334,-157.918\n"
        "out1,Lot,21.336,-157.915\n"
    )
    feeds = [
        write_zip(
            tmp_path / f"{index}.zip",
            {
                **FEED,
                "agency.txt": FEED["agency.txt"].replace("Europe/Helsinki", zone),
                "stops.txt": stops,
            },
        )
        for index, zone in enumerate(["America/New_York", "Pacific/Honolulu"])
    ]
    output = tmp_path / "merged.zip"
    left = f"left out feed 0 (f1, {feeds[0]}): America/New_York, "
    with pytest.warns(UserWarning, match=re.escape(left + "stops in Pacific/Honolulu")):
        report = merge_feeds(feeds, output, reference_date="20260601")
    assert report["skipped_feeds"] == [
        {
            "feed": 0,
            "timezones": ["America/New_York"],
            "stop_timezone": "Pacific/Honolulu",
        }
    ]
    agency = FeedEditor(output).tables["agency.txt"]
    assert list(agency["agency_id"]) == ["f2:hsl", "f2:espoo"]
    assert set(agency["agency_timezone"]) == {"Pacific/Honolulu"}


def test_quoted_commas_do_not_trip_the_delimiter_guard(tmp_path):
    # The delimiter guard counted every comma on a line, so a two-column
    # areas.txt whose quoted WKT polygon held over 4096 commas was refused as
    # exceeding max_columns and the feed could not be cropped.
    ring = ", ".join(f"24.{i:04d} 60.1" for i in range(4200))
    areas = f'area_id,wkt\na1,"POLYGON(({ring}))"\na2,"POINT (24.9 60.1)"\n'
    source = write_zip(tmp_path / "feed.zip", {**FEED, "areas.txt": areas})
    output = tmp_path / "cropped.zip"
    crop_feed(source, output, aoi=CITY_BBOX, reference_date="20260601")
    cropped = read_entry(output, "areas.txt").decode()
    assert list(csv.reader(io.StringIO(cropped))) == list(
        csv.reader(io.StringIO(areas))
    )


def test_a_capped_calendar_expansion_keeps_the_window_and_target_day(tmp_path):
    # Calendar rows past the 2,000,000-day expansion budget were skipped
    # without a notice: the report lost its service window and moment, and
    # a temporal crop dropped the trips of the skipped services.
    files = dict(FEED)
    daily, never = "1,1,1,1,1,1,1", "0,0,0,0,0,0,0"
    files["calendar.txt"] += "".join(
        f"s{i},{never if i == 0 else daily},20200101,20301212\n" for i in range(502)
    )
    files["trips.txt"] += "r-in,s501,t-last\n"
    files[
        "stop_times.txt"
    ] += "t-last,10:00:00,10:00:00,in1,1\nt-last,10:05:00,10:05:00,in2,2\n"
    source = write_zip(tmp_path / "feed.zip", files)
    # The row notice of s0, which runs on no weekday, fills the budget of one.
    report = validate_feed(source, reference_date="20280601", max_notices_per_file=1)
    capped = [
        n for n in report["notices"] if n["code"] == "service_expansion_truncated"
    ]
    assert [n["context"] for n in capped] == [
        {"maxServiceDays": 2_000_000, "calendarDays": 365 + 502 * 3999}
    ]
    assert report["service_window"] == ["20200101", "20301212"]
    assert report["moment"]["activeTrips"] == 1
    assert report["moment"]["baselineTrips"] is None

    output = tmp_path / "cropped.zip"
    crop_feed(
        source,
        output,
        start_date="20280601",
        end_date="20280601",
        reference_date="20280601",
    )
    assert "t-last" in read_entry(output, "trips.txt").decode()


def test_a_failed_producer_download_falls_back_to_the_hosted_copy(
    tmp_path, monkeypatch
):
    # A feed whose producer URL timed out or answered 404 was skipped as
    # "download failed", though its index row names the Mobility Database
    # hosted copy (urls.latest) the index itself was built from.
    import json

    import httpx

    import transitio.index as transitio_index
    from index_fixture import HULL, covered_feed, edge, write_index
    from transitio.catalog import MobilityDatabase
    from transitio.pipeline import fetch

    direct = "https://producer.example/gtfs.zip"
    latest = "https://files.example/mdb-9/latest.zip"
    feed = {
        **covered_feed("f-a", coverage_source="crawl"),
        "coverage": HULL,
        "mdb": {"urls": {"direct_download": direct, "latest": latest}},
    }
    edges = [edge("Q1757", "f-a", tier="local")]
    index = transitio_index.read_index(
        write_index(tmp_path / "index", feeds=[feed], edges=edges)
    )
    payload = write_zip(tmp_path / "feed.zip", FEED).read_bytes()

    def handler(request):
        if str(request.url) == latest:
            return httpx.Response(200, content=payload)
        return httpx.Response(404)

    class Served(MobilityDatabase):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.delenv("MOBILITY_API_REFRESH_TOKEN", raising=False)
    monkeypatch.setattr("transitio.catalog.MobilityDatabase", Served)
    result = fetch(
        place="Q1757",
        index=index,
        directory=tmp_path / "out",
        crop=False,
        osm=False,
        expired="keep",
    )
    (entry,) = result.selection
    assert (entry["decision"], entry["fetched_from"], entry["note"]) == (
        "delivered",
        "mdb_latest",
        "from the Mobility Database hosted copy",
    )
    errors = entry["download_errors"]
    assert errors.startswith("mdb: ") and "404 Not Found" in errors
    sidecar = result.feeds[0].with_suffix(".provenance.json")
    provenance = result.reports[0]["summary"]["provenance"]
    for record in (json.loads(sidecar.read_text()), provenance):
        assert (record["fetched_from"], record["download_errors"]) == (
            "mdb_latest",
            errors,
        )


_YEAR = ["2026-01-01", "2026-12-31"]


@pytest.mark.parametrize(
    "dropped, reason, window",
    [
        pytest.param(
            ["agency.txt"], "missing required file agency.txt", _YEAR, id="agency"
        ),
        pytest.param(
            ["stops.txt", "agency.txt"],
            "missing required files agency.txt, stops.txt",
            _YEAR,
            id="two-files",
        ),
        pytest.param(
            ["calendar.txt", "calendar_dates.txt"],
            "missing calendar.txt and calendar_dates.txt",
            None,
            id="calendars",
        ),
        pytest.param([], None, _YEAR, id="complete"),
    ],
)
def test_a_feed_missing_a_required_file_is_skipped(tmp_path, dropped, reason, window):
    # A feed without agency.txt was delivered, its report counting the
    # error, and cafein then refused every delivered feed.
    from transitio.pipeline._fetch import _process_feed, _SkipFeed

    files = {name: text for name, text in FEED.items() if name not in dropped}
    source = write_zip(tmp_path / "feed.zip", files)
    options = dict(
        geometry=None,
        tag="t",
        repair=False,
        crop=False,
        modes=None,
        day=None,
        study=False,
        hosted=None,
        # Feed-level notices bypass the per-file notice cap.
        budgets={"max_notices_per_file": 0},
    )
    if reason is None:
        path, report, *_, kept_window = _process_feed(source, **options)
        assert (path, kept_window) == (source, window) and report["summary"]
        return
    with pytest.raises(_SkipFeed) as caught:
        _process_feed(source, **options)
    assert (caught.value.reason, caught.value.window) == (reason, window)


MIDLAND = {
    "agency.txt": (
        "agency_id,agency_name,agency_url,agency_timezone\n"
        "1,Midland Bluebird,https://m.example,Europe/London\n"
    ),
    "stops.txt": (
        "stop_id,stop_name,stop_lat,stop_lon\na,A,55.86,-4.25\nb,B,55.87,-4.26\n"
    ),
    "routes.txt": "route_id,agency_id,route_short_name,route_type\nx36,1,X36,3\n",
    "trips.txt": "route_id,service_id,trip_id\nx36,wk,t1\n",
    "stop_times.txt": (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "t1,08:00:00,08:00:00,a,1\nt1,08:10:00,08:10:00,b,2\n"
    ),
    "calendar.txt": FEED["calendar.txt"],
}


def test_copies_of_a_trip_under_differently_named_agencies_are_one_service(tmp_path):
    # Copies of one network under "Midland Bluebird" and "Midland Bluebird
    # Ltd" were neither duplicates in the merge nor versions in fetch.
    import datetime

    from transitio.edit import FeedEditor
    from transitio.gtfs import merge_tables
    from transitio.pipeline._fetch import _service

    agency = MIDLAND["agency.txt"]
    renamed = {**MIDLAND, "agency.txt": agency.replace("Bluebird,", "Bluebird Ltd,")}
    tables = [
        FeedEditor(write_zip(tmp_path / f"{n}.zip", files)).tables
        for n, files in enumerate((MIDLAND, renamed))
    ]
    merged, _ = merge_tables(tables)
    assert list(merged["trips.txt"]["trip_id"]) == ["f1:t1"]
    # Routes naming two agency ids under one blank-named agency, or without
    # agency.txt, are not one unnamed agency.
    routes = MIDLAND["routes.txt"] + "x37,2,X37,3\n"
    blank = {**MIDLAND, "agency.txt": agency.replace("Midland Bluebird", "")}
    absent = {name: text for name, text in MIDLAND.items() if name != "agency.txt"}
    for n, files in enumerate((blank, absent)):
        two = write_zip(tmp_path / f"two{n}.zip", {**files, "routes.txt": routes})
        assert _service(two, datetime.date(2026, 6, 1)) is None


def test_copies_of_a_headway_network_are_merged_once(tmp_path):
    # Frequency-based trips were never compared, so a merge kept every copy
    # of a headway-only network.
    from transitio.edit import FeedEditor
    from transitio.gtfs import merge_feeds

    frequencies = "trip_id,start_time,end_time,headway_secs\nt1,08:00:00,10:00:00,600\n"
    copies = [
        write_zip(tmp_path / f"{n}.zip", {**MIDLAND, "frequencies.txt": frequencies})
        for n in range(2)
    ]
    report = merge_feeds(copies, tmp_path / "merged.zip", check=False)
    merged = FeedEditor(tmp_path / "merged.zip").tables
    for name in ("trips.txt", "frequencies.txt"):
        assert list(merged[name]["trip_id"]) == ["f1:t1"]
    assert report["duplicate_trips"]["dropped"] == 1


def _near_feed(moved, stop_times, extra):
    """A feed of trip ``g`` on route X36 and headway trip ``h`` on route
    506 over stops a to e, ``moved`` degrees north, and ``extra`` stops."""
    lats = dict(zip("abcde", (55.86, 55.862, 55.864, 55.866, 55.868)))
    stops = "".join(f"{s},{s},{lat + moved:.6f},-4.25\n" for s, lat in lats.items())
    return {
        "agency.txt": MIDLAND["agency.txt"],
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\n" + stops + extra,
        "routes.txt": (
            "route_id,agency_id,route_short_name,route_type\nx36,1,X36,3\n506,1,506,3\n"
        ),
        "trips.txt": "route_id,service_id,trip_id\nx36,wk,g\n506,wk,h\n",
        "stop_times.txt": (
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence,"
            "pickup_type,drop_off_type\n" + stop_times
        ),
        "frequencies.txt": (
            "trip_id,start_time,end_time,headway_secs\nh,06:00:00,10:00:00,600\n"
        ),
        "calendar.txt": FEED["calendar.txt"],
    }


def test_near_repeats_of_a_trip_are_merged_once(tmp_path):
    # A national feed timing an operator's bus between timing points to the
    # second, or a headway network's next version moving its last stop
    # 90 m, did not repeat the trip exactly, so the merge kept both copies.
    from transitio.edit import FeedEditor
    from transitio.gtfs import merge_feeds

    headway = "".join(
        f"h,06:{3 * n:02d}:00,06:{3 * n:02d}:00,{stop},{n},,\n"
        for n, stop in enumerate("abcd")
    )
    national = _near_feed(
        0,
        "g,08:00:00,08:00:00,a,1,,1\ng,08:02:30,08:02:30,b,2,,\n"
        "g,08:05:15,08:05:15,c,3,,\ng,08:07:40,08:07:40,d,4,,\n"
        "g,08:10:00,08:10:00,e,5,1,\n" + headway + "h,06:12:00,06:12:00,e,4,,\n",
        "",
    )
    operator = _near_feed(
        0.00002,
        "g,08:00:00,08:00:00,a,1,,\ng,08:02:00,08:02:00,b,2,,\n"
        "g,08:04:00,08:04:00,x,3,,\ng,08:05:00,08:05:00,c,4,,\n"
        "g,08:08:00,08:08:00,d,5,,\ng,08:10:00,08:10:00,e,6,,\n"
        + headway
        + "h,06:12:00,06:12:00,f,4,,\n",
        "x,x,55.863,-4.25\nf,f,55.8688,-4.25\n",
    )
    feeds = [
        write_zip(tmp_path / f"{n}.zip", files)
        for n, files in enumerate((national, operator))
    ]
    report = merge_feeds(feeds, tmp_path / "merged.zip", check=False)
    merged = FeedEditor(tmp_path / "merged.zip").tables
    assert sorted(merged["trips.txt"]["trip_id"]) == ["f1:g", "f1:h"]
    counts = report["duplicate_trips"]
    assert counts["dropped"] == counts["near_matches"] == counts["unaligned_stops"] == 2


def test_a_placeholder_calendar_is_an_older_version_of_the_dated_network(tmp_path):
    # A snapshot on a 2025 to 2099 calendar passed every date check, and as
    # its route names had drifted it never paired with the dated network
    # serving the same stops, so both were delivered. Its first running day
    # comes after the dated network's start.
    import datetime

    from transitio.pipeline._fetch import _entry, _service, _settle_versions

    dated = write_zip(tmp_path / "dated.zip", FEED)
    snapshot = {
        **FEED,
        "routes.txt": FEED["routes.txt"].replace(",1,", ",x,").replace(",2,", ",y,"),
        "calendar.txt": FEED["calendar.txt"].replace(
            "20260101,20261231", "20251231,20990101"
        ),
        "calendar_dates.txt": FEED["calendar_dates.txt"]
        + "wk,20251231,2\nwk,20260101,2\n",
        "transfers.txt": "from_stop_id,to_stop_id,transfer_type\nin1,in2,0\n",
    }
    snapshot = write_zip(tmp_path / "snapshot.zip", snapshot)
    day = datetime.date(2026, 6, 1)
    record = [_entry("snapshot", None), _entry("dated", None)]
    windows = (["2026-01-05", "2099-01-01"], ["2026-01-01", "2026-12-31"])
    for entry, window in zip(record, windows):
        entry.update(decision="delivered", feed_window=window)
    services = {"dated": _service(dated, day), "snapshot": _service(snapshot, day)}
    assert _settle_versions(record, services, set(), day) == {"snapshot"}
    assert record[1]["note"] is None
    assert (record[0]["reason"], record[0]["note"], record[0]["version_of"]) == (
        "another version of dated",
        "placeholder calendar 2025-12-31 to 2099-01-01",
        {"feed_id": "dated", "route_overlap": 0.0, "stop_overlap": 1.0},
    )


@pytest.mark.parametrize("client", ["mdb", "atlas", "osm", "index"])
def test_http_clients_identify_themselves_as_transitio(client, tmp_path):
    # Hosts that filter by user agent answered httpx's default
    # "python-httpx/<version>" with 403 Forbidden, so their feeds never
    # downloaded.
    import re

    import httpx

    from transitio import _http
    from transitio.catalog import AtlasFeed, MobilityDatabase, TransitlandAtlas
    from transitio.catalog._models import Feed
    from transitio.index import _refresh
    from transitio.osm._fetch import _download

    url = "https://feeds.example/gtfs.zip"
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, content=b"PK\x03\x04")

    transport = httpx.MockTransport(handler)
    if client == "mdb":
        feed = Feed.from_api({"id": "mdb-1", "latest_dataset": {"hosted_url": url}})
        with MobilityDatabase(None, cache_dir=tmp_path, transport=transport) as db:
            db.download_latest(feed)
    elif client == "atlas":
        feed = AtlasFeed.from_record(
            {"onestop_id": "f-x", "urls": {"static_current": url}}
        )
        with TransitlandAtlas(cache_dir=tmp_path, transport=transport) as atlas:
            atlas.download(feed)
    elif client == "osm":
        _download(url, tmp_path / "extract.osm.pbf", False, transport)
    else:
        with _refresh._client("https://api.github.example", transport) as http:
            http.get("/repos/x/y/releases")
    (request,) = sent
    assert request.headers["User-Agent"] == _http.USER_AGENT
    if client == "index":
        assert request.headers["Accept"] == "application/vnd.github+json"
    assert re.fullmatch(
        r"transitio/\S+ \(\+https://github\.com/cafein-py/transitio\)",
        _http.USER_AGENT,
    )
