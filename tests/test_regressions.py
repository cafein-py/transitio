"""Regression tests, one per fixed defect."""

import csv
import io
import sys
import zipfile

import pytest

pytest.importorskip("transitio._core")

from transitio.exceptions import DownloadError, ExtractNotFoundError  # noqa: E402
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


def _place(place_id, kind, name, country, aliases=(), names=None, parent=None):
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


def _lookup(rows, feeds):
    """A resolver over the place ``rows``, with the feed counts ``feeds``."""
    pandas = pytest.importorskip("pandas")
    from transitio.index.places import _PlaceLookup

    return _PlaceLookup(
        pandas.DataFrame(rows), feed_count=lambda place_id: feeds.get(place_id, 0)
    )


def test_a_city_is_not_outranked_by_the_places_named_after_it():
    # Augsburg's name is shared by three metros in its country, one per metro
    # definition, and by a containing region with about the same service;
    # London's only exact-match city is in Canada, the metros sharing its name
    # in the UK; New York State carries far more service than the city; and
    # Hamilton's busier British metro must not win once the Canadian one, the
    # city's namesake, is set aside. Where no city matches, Istanbul's province
    # stands as the city against its metro; Lagos's Portuguese town keeps the
    # Nigerian state from standing as one, so the Nigerian metro stays a rival.
    # Valencia's comarca, inside the far better-known city with as many feeds,
    # is the city's namesake, not the other way round: set against the comarca
    # alone, Venezuela's better-known Valencia won. Antwerp, inside a same-named
    # city that is not far better known, still sets that city aside.
    from transitio.exceptions import AmbiguousPlaceError

    def labels(count):
        return {f"l{n}": f"label {n}" for n in range(count)}

    feeds = {
        "c-aug": 30,
        "r-aug": 31,
        "c-ny": 68,
        "r-ny": 165,
        "c-ham": 5,
        "m-ham-ca": 40,
        "m-ham-gb": 60,
        "r-ist": 6,
        "m-ist": 4,
        "m-lag": 1,
        "r-lag": 1,
        "c-lag": 5,
        "r-val": 7,
        "c-val": 7,
        "c-val-com": 7,
        "c-ant": 23,
        "c-ant-in": 23,
    }
    lookup = _lookup(
        [
            _place("r-aug", "region", "Augsburg", "DE"),
            _place("c-aug", "city", "Augsburg", "DE", parent="r-aug"),
            _place("m-aug-1", "metro", "Augsburg", "DE"),
            _place("m-aug-2", "metro", "Augsburg", "DE"),
            _place("m-aug-3", "metro", "Augsburg", "DE"),
            _place("c-lon", "city", "London", "CA"),
            _place("m-lon-1", "metro", "London", "GB"),
            _place("m-lon-2", "metro", "London", "GB"),
            _place("r-ny", "region", "New York", "US"),
            _place("c-ny", "city", "New York", "US", parent="r-ny"),
            _place("c-ham", "city", "Hamilton", "CA"),
            _place("m-ham-ca", "metro", "Hamilton", "CA"),
            _place("m-ham-gb", "metro", "Hamilton", "GB"),
            _place("r-ist", "region", "Istanbul", "TR"),
            _place("m-ist", "metro", "Istanbul", "TR"),
            _place("m-lag", "metro", "Lagos", "NG"),
            _place("r-lag", "region", "Lagos", "NG"),
            _place("c-lag", "city", "Lagos", "PT"),
            _place("r-val", "region", "Valencia", "ES"),
            _place(
                "c-val", "city", "Valencia", "ES", names=labels(170), parent="r-val"
            ),
            _place(
                "c-val-com", "city", "Valencia", "ES", names=labels(48), parent="c-val"
            ),
            _place("c-val-ve", "city", "Valencia", "VE", names=labels(100)),
            _place("c-ant", "city", "Antwerp", "BE", names=labels(59)),
            _place(
                "c-ant-in", "city", "Antwerp", "BE", names=labels(170), parent="c-ant"
            ),
        ],
        feeds,
    )
    assert lookup.resolve("Augsburg").id == "c-aug"
    assert lookup.resolve("Istanbul").id == "r-ist"
    assert lookup.resolve("Valencia").id == "c-val"
    assert lookup.resolve("Antwerp").id == "c-ant-in"
    for name in ("London", "New York", "Hamilton", "Lagos"):
        with pytest.raises(AmbiguousPlaceError):
            lookup.resolve(name)


def test_the_feed_margin_does_not_favour_an_alias_over_a_name():
    # Saint Paul, Minnesota carries "São Paulo" as an alias and more feeds than
    # São Paulo itself: the name stays ambiguous rather than naming Saint Paul,
    # and a qualifier picks São Paulo; a translation still reaches Vienna; and
    # a district listing its city's name as an alias is no rival to the city,
    # nor are a region and a country listing Taipei, or a county listing Los
    # Angeles as an alias and in Croatian and Nahuatl, whose metros would
    # otherwise win on feeds.
    from transitio.exceptions import AmbiguousPlaceError

    # Kingston: an aliased city abroad would win the narrowed contest on feeds;
    # the veto must not hand the name to the busy metro set aside as a namesake.
    feeds = {"c-stp": 7, "c-sp": 2, "c-vie": 27, "m-wien": 30}
    feeds.update({"c-kin": 5, "c-kin-us": 30, "m-kin": 100})
    feeds.update({"c-bog": 6, "c-pa": 5, "m-bog": 6})
    feeds.update({"c-tpe": 1, "m-tpe": 4, "r-ntpe": 2, "tw": 9})
    feeds.update({"c-la": 54, "r-la": 120, "m-la": 142})
    lookup = _lookup(
        [
            _place("br", "country", "Brazil", "BR"),
            _place("c-sp", "city", "São Paulo", "BR"),
            _place("c-stp", "city", "Saint Paul", "US", aliases=["São Paulo"]),
            _place("c-vie", "city", "Vienna", "AT", names={"de": "Wien"}),
            _place("m-wien", "metro", "Wien", "AT"),
            _place("c-kin", "city", "Kingston", "JM"),
            _place("m-kin", "metro", "Kingston", "JM"),
            _place("c-kin-us", "city", "Port Kingston", "US", aliases=["Kingston"]),
            _place("c-bog", "city", "Bogotá", "CO"),
            _place("m-bog", "metro", "Bogotá", "CO"),
            _place("c-pa", "city", "Puente Aranda", "CO", ["Bogotá"], parent="c-bog"),
            _place("c-tpe", "city", "Taipei", "TW"),
            _place("m-tpe", "metro", "Taipei", "TW"),
            _place("r-ntpe", "region", "New Taipei", "TW", aliases=["Taipei"]),
            _place("tw", "country", "Taiwan", "TW", aliases=["Taipei"]),
            _place(
                "r-la",
                "region",
                "Los Angeles County",
                "US",
                ["Los Angeles"],
                {"en": "Los Angeles County", "hr": "Los Angeles", "nah": "Los Angeles"},
            ),
            _place("c-la", "city", "Los Angeles", "US", parent="r-la"),
            _place("m-la", "metro", "Los Angeles", "US"),
        ],
        feeds,
    )
    with pytest.raises(AmbiguousPlaceError):
        lookup.resolve("Sao Paulo")
    assert lookup.resolve("Sao Paulo, Brazil").id == "c-sp"
    assert lookup.resolve("Wien").id == "c-vie"
    with pytest.raises(AmbiguousPlaceError):
        lookup.resolve("Kingston")
    assert lookup.resolve("Bogota").id == "c-bog"
    assert lookup.resolve("Taipei").id == "c-tpe"
    assert lookup.resolve("Los Angeles").id == "c-la"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # Pinto, Spain, lists the name only in Irish.
        ("Buenos Aires", "c-ba"),
        # Munich carries the name in German, a village as its name.
        ("München", "c-muc"),
        # Paris, known far more widely, stays a rival through its Finnish label;
        # so does Mexico through its Polish one against a town of that name in
        # Mexico, where a country reaching the name by an alias would be the
        # town's namesake.
        ("Pariisi", None),
        ("Meksyk", None),
    ],
)
def test_a_label_in_another_language_does_not_compete_with_an_own_name(name, expected):
    # Every label counted as a place's own name, so a place abroad carrying a
    # capital's name in a language not its own kept the capital ambiguous, and
    # a feed lead on a German label was vetoed in favour of a village's name.
    from transitio.exceptions import AmbiguousPlaceError

    known = {f"l{n}": f"label {n}" for n in range(150)}
    lookup = _lookup(
        [
            _place("c-ba", "city", "Buenos Aires", "AR"),
            _place("c-pinto", "city", "Pinto", "ES", names={"ga": "Buenos Aires"}),
            _place("c-muc", "city", "Munich", "DE", names={**known, "de": "München"}),
            _place("c-mue", "city", "München", "DE"),
            _place("c-par", "city", "Paris", "FR", names={**known, "fi": "Pariisi"}),
            _place("c-pariisi", "city", "Pariisi", "EE"),
            _place("mx", "country", "Mexico", "MX", names={**known, "pl": "Meksyk"}),
            _place("c-mek", "city", "Meksyk", "MX"),
        ],
        {
            "c-ba": 30,
            "c-pinto": 20,
            "c-muc": 39,
            "c-mue": 3,
            "c-par": 55,
            "c-pariisi": 1,
            "mx": 2,
            "c-mek": 2,
        },
    )
    if expected is None:
        with pytest.raises(AmbiguousPlaceError):
            lookup.resolve(name)
    else:
        assert lookup.resolve(name).id == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # Mons carries "Bergen" in Dutch and German and as an alias.
        ("Bergen", None),
        # An English label is a primary name.
        ("Halle", "c-halle-saale"),
        # Loison-sous-Lens, listing "Lens" as an alias, stays a namesake of the
        # arrondissement it lies in once the arrondissement leaves.
        ("Lens", "c-lens"),
    ],
)
def test_a_primary_name_outranks_a_label_in_another_own_language(name, expected):
    # A place carrying a name only in another of its own languages competed
    # like the places named so: Mons, Dutch "Bergen", outran the German towns
    # of that name on feeds, and the arrondissement of Lens outran its city.
    from transitio.exceptions import AmbiguousPlaceError

    lookup = _lookup(
        [
            _place(
                "c-mons",
                "city",
                "Mons",
                "BE",
                ["Bergen"],
                {"nl": "Bergen", "de": "Bergen"},
            ),
            _place("c-bergen-1", "city", "Bergen", "DE"),
            _place("c-bergen-2", "city", "Bergen", "DE"),
            _place(
                "c-halle-saale", "city", "Halle (Saale)", "DE", names={"en-ca": "Halle"}
            ),
            _place("c-halle", "city", "Halle", "BE"),
            _place(
                "c-arr", "city", "Arrondissement of Lens", "FR", names={"fr": "Lens"}
            ),
            _place("c-lens", "city", "Lens", "FR", parent="c-arr"),
            _place(
                "c-loison", "city", "Loison-sous-Lens", "FR", ["Lens"], parent="c-arr"
            ),
            _place("m-lens", "metro", "Lens", "FR"),
        ],
        {
            "c-mons": 15,
            "c-bergen-1": 3,
            "c-bergen-2": 2,
            "c-halle-saale": 19,
            "c-halle": 4,
            "c-arr": 30,
            "c-lens": 12,
            "c-loison": 7,
            "m-lens": 16,
        },
    )
    if expected is None:
        with pytest.raises(AmbiguousPlaceError):
            lookup.resolve(name)
    else:
        assert lookup.resolve(name).id == expected


def test_suggestions_rank_a_label_in_another_language_after_aliases():
    # An American Vienna listing "Wien" in Low German outranked Vienna, whose
    # German label came after its own Low German one, on feeds alone.
    lookup = _lookup(
        [
            _place(
                "c-vie", "city", "Vienna", "AT", names={"nds": "Wien", "de": "Wien"}
            ),
            _place("c-al", "city", "Neudorf", "AT", aliases=["Wien"]),
            _place("c-vie-us", "city", "Vienna", "US", names={"nds": "Wien"}),
        ],
        {"c-vie": 24, "c-al": 27, "c-vie-us": 30},
    )
    hits = lookup.suggest("wien", limit=3)
    assert [(hit.place.id, hit.source) for hit in hits] == [
        ("c-vie", "de"),
        ("c-al", "alias"),
        ("c-vie-us", "nds"),
    ]


@pytest.mark.parametrize(
    ("name", "rows", "expected"),
    [
        # Each row: the place id, its kind, country, feeds and language labels.
        pytest.param(
            "Moscow",
            [("us", "city", "US", 3, 77), ("ru", "city", "RU", 0, 336)],
            "ru",
            id="moscow",
        ),
        # Delhi, India, a region, ranks below two American townships.
        pytest.param(
            "Delhi",
            [
                ("us", "city", "US", 5, 31),
                ("us-1", "city", "US", 1, 1),
                ("us-2", "city", "US", 1, 1),
                ("in", "region", "IN", 2, 101),
            ],
            "in",
            id="delhi",
        ),
        # An American metro leads and counts the labels of its country's city,
        # too few against Russia's, and enough in the next case.
        pytest.param(
            "Saint Petersburg",
            [
                ("us-m", "metro", "US", 12, 1),
                ("us", "city", "US", 0, 117),
                ("ru", "region", "RU", 0, 264),
            ],
            "ru",
            id="metro-leader",
        ),
        pytest.param(
            "Saint Petersburg",
            [
                ("us-m", "metro", "US", 12, 1),
                ("us", "city", "US", 0, 150),
                ("ru", "region", "RU", 0, 264),
            ],
            "us-m",
            id="metro-leader-known",
        ),
        pytest.param(
            "Paris",
            [("fr", "city", "FR", 55, 344), ("us", "city", "US", 1, 60)],
            "fr",
            id="paris",
        ),
        # Abroad below the floor; a better-known place at home is no rival.
        pytest.param(
            "Springfield",
            [
                ("us", "city", "US", 5, 30),
                ("gb", "city", "GB", 0, 90),
                ("us-2", "city", "US", 0, 336),
            ],
            "us",
            id="floor",
        ),
        # Twice the leader's labels is not more than twice.
        pytest.param(
            "Springfield",
            [("us", "city", "US", 5, 80), ("gb", "city", "GB", 0, 160)],
            "us",
            id="twice",
        ),
        # Neither well-known place abroad is far better known than the other.
        pytest.param(
            "Moscow",
            [
                ("us", "city", "US", 3, 77),
                ("ru", "city", "RU", 0, 336),
                ("ca", "city", "CA", 0, 200),
            ],
            None,
            id="two-known-rivals",
        ),
        pytest.param(
            "Cali",
            [
                ("co-m", "metro", "CO", 1, 1),
                ("co", "city", "CO", 1, 129),
                ("co-2", "city", "CO", 1, 5),
            ],
            "co",
            id="within-country",
        ),
    ],
)
def test_a_far_better_known_place_wins_where_feeds_do_not_decide(name, rows, expected):
    # Feed counts measure how well each country's feeds are catalogued: Moscow,
    # Idaho, with three feeds, won over Moscow, Russia, with none. The labels a
    # place carries in many languages mark it as known far beyond its country,
    # and where feeds do not decide, such a place wins, at home or abroad: of
    # Colombia's two cities named Cali, one feed each, the one with 129 labels.
    from transitio.exceptions import AmbiguousPlaceError

    places = [
        _place(pid, kind, name, country, names={f"l{n}": name for n in range(labels)})
        for pid, kind, country, _, labels in rows
    ]
    lookup = _lookup(places, {row[0]: row[3] for row in rows})
    if expected is None:
        with pytest.raises(AmbiguousPlaceError):
            lookup.resolve(name)
    else:
        assert lookup.resolve(name).id == expected


@pytest.mark.parametrize(
    ("name", "rows", "expected"),
    [
        # Each row: the id, kind, country, feeds, labels, population and parent.
        pytest.param(
            "Lima",
            [
                ("pe", "city", "PE", 0, 90, 200_000, None),
                ("us", "city", "US", 3, 63, None, None),
            ],
            "pe",
            id="lima",
        ),
        pytest.param(
            "Lima",
            [
                ("pe", "city", "PE", 0, 90, 199_999, None),
                ("us", "city", "US", 3, 63, None, None),
            ],
            "us",
            id="below",
        ),
        # Japan's region of the name contains its city, so it is no rival.
        pytest.param(
            "Kochi",
            [
                ("in", "city", "IN", 0, 125, 5_069_022, None),
                ("jp", "city", "JP", 10, 100, 216_999, "jp-r"),
                ("jp-r", "region", "JP", 10, 50, None, None),
            ],
            "in",
            id="kochi",
        ),
        # Within twice the population the feed margin decides.
        pytest.param(
            "Valencia",
            [
                ("ve", "city", "VE", 0, 100, 1_601_249, None),
                ("es", "city", "ES", 7, 170, 1_404_208, None),
            ],
            "es",
            id="valencia",
        ),
        pytest.param(
            "Istanbul",
            [
                ("c", "city", "TR", 0, 139, 14_210_222, "r"),
                ("r", "region", "TR", 6, 114, None, None),
                ("m", "metro", "TR", 4, 1, None, None),
            ],
            "c",
            id="istanbul",
        ),
        # A state of the name containing no city of it is not compared.
        pytest.param(
            "Victoria",
            [
                ("ca", "city", "CA", 6, 129, 250_760, None),
                ("au", "region", "AU", 17, 155, None, None),
            ],
            None,
            id="victoria",
        ),
        # Mexico City carries "Meksyk" only as a label in other languages.
        pytest.param(
            "Meksyk",
            [
                ("mx", "city", "MX", 30, 150, 21_000_000, None),
                ("pl", "city", "PL", 0, 1, None, None),
            ],
            None,
            id="meksyk",
        ),
        # A city without a population, known as widely, keeps the rule out.
        pytest.param(
            "San Jose",
            [
                ("cr", "city", "CR", 0, 168, 2_272_572, None),
                ("us", "city", "US", 11, 168, None, None),
            ],
            "us",
            id="san-jose",
        ),
        pytest.param(
            "Medan",
            [
                ("id", "city", "ID", 0, 128, 4_350_624, None),
                ("fr", "city", "FR", 3, 108, None, None),
            ],
            "id",
            id="medan",
        ),
    ],
)
def test_a_city_far_larger_than_its_namesakes_wins(name, rows, expected):
    # Lima, Peru, with no feeds and fewer labels than the label rule needs, lost
    # its name to Lima, Ohio, on feeds; a city of 200,000 people or more with
    # over twice the population of every other place of the name wins.
    from transitio.exceptions import AmbiguousPlaceError

    own = {"mx": "Mexico City"}
    places = [
        {
            **_place(
                pid,
                kind,
                own.get(pid, name),
                country,
                names={f"l{n}": name for n in range(labels)},
                parent=parent,
            ),
            "population": population,
        }
        for pid, kind, country, _, labels, population, parent in rows
    ]
    lookup = _lookup(places, {row[0]: row[3] for row in rows})
    if expected is None:
        with pytest.raises(AmbiguousPlaceError):
            lookup.resolve(name)
    else:
        assert lookup.resolve(name).id == expected


@pytest.mark.parametrize(
    ("name", "options", "expected"),
    [
        ("Stockholm", {"kind": "metro"}, "m-fua"),
        ("Stockholm", {"definition": "metropolitan region"}, "m-mr"),
        ("Stockholm", {"definition": "city-region (FAO)"}, "m-fao"),
        ("Athens", {"kind": "metro"}, None),
        # The definition that answers carries the name only in another language.
        ("Kansas City", {"kind": "metro"}, "m-kc-msa"),
        ("Stockholm", {}, "m-fua"),
        # A city of the name abroad leaves the British metros one metro.
        ("Ipswich", {}, "m-ips-mr"),
        # The city's own metros keep both definitions, so neither outruns it.
        ("Firenze", {}, None),
    ],
)
def test_one_definition_of_a_metro_answers(name, options, expected):
    # Each metro definition names its metro after the core city, so Stockholm's
    # three metros tied, and without kind="metro" so did Cambridge's two in the
    # UK. The FAO region shares a member only with the metropolitan region, and
    # that one with the FUA; Athens, US and Greece, share none and stay rivals.
    # Florence's metros are the city's namesakes: with one definition dropped,
    # its FUA would beat the city by the margin.
    from transitio.exceptions import AmbiguousPlaceError

    metros = [
        # Each row: the id, name, country, definition, members and feeds.
        ("m-fao", "Stockholm", "SE", "city-region (FAO)", ["a", "b"], 3),
        ("m-mr", "Stockholm", "SE", "metropolitan region", ["b", "c"], 3),
        ("m-fua", "Stockholm", "SE", "functional urban area", ["c", "d"], 3),
        ("m-ath-us", "Athens", "US", "city-region (FAO)", ["e"], 3),
        ("m-ath-gr", "Athens", "GR", "city-region (FAO)", ["f"], 3),
        ("m-kc-fao", "Kansas City", "US", "city-region (FAO)", ["j"], 14),
        ("m-ips-mr", "Ipswich", "GB", "metropolitan region", ["g"], 6),
        ("m-ips-fao", "Ipswich", "GB", "city-region (FAO)", ["g", "h"], 6),
        ("m-flr-fua", "Firenze", "IT", "functional urban area", ["i"], 26),
        ("m-flr-mr", "Firenze", "IT", "metropolitan region", ["i"], 25),
    ]
    florence = {"en": "Florence", "it": "Firenze"}
    places = [
        {
            **_place(pid, "metro", label, country),
            "source_subtype": subtype,
            "member_ids": members,
        }
        for pid, label, country, subtype, members, _ in metros
    ] + [
        _place("c-ips", "city", "Ipswich", "AU"),
        _place("r-flr", "region", "Florence", "IT", names=florence),
        _place("c-flr", "city", "Florence", "IT", names=florence, parent="r-flr"),
        {
            **_place("m-kc-msa", "metro", "KC area", "US", names={"da": "Kansas City"}),
            "source_subtype": "metropolitan statistical area",
            "member_ids": ["j"],
        },
    ]
    feeds = {row[0]: row[-1] for row in metros}
    feeds.update({"c-ips": 1, "r-flr": 26, "c-flr": 11, "m-kc-msa": 7})
    lookup = _lookup(places, feeds)
    if expected is None:
        with pytest.raises(AmbiguousPlaceError):
            lookup.resolve(name, **options)
    else:
        assert lookup.resolve(name, **options).id == expected


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Copenhagen, Denmark", "r-cph"),
        # A town qualifies when no region or country does.
        ("Copenhagen, Town of Denmark", "c-cph"),
        ("Halifax, Canada", "c-hfx"),
        # A label in another language answers when the qualified name is
        # ambiguous or matches nothing.
        ("Ashington, Anglija", "c-ash-1"),
        ("Ashington, Anglia", "c-ash-1"),
        # An alias holding the comma is read as written.
        ("Andover, USA", "c-and-1"),
    ],
)
def test_a_qualifier_names_a_containing_region_or_country(query, expected):
    # Any ancestor qualified a name, so a town called Denmark in New York kept
    # "Copenhagen, Denmark" ambiguous; and a label equal to the whole query in
    # a language not the place's own (Piedmontese "Halifax (Canadà)" for the
    # region) skipped the qualifier.
    lookup = _lookup(
        [
            _place("dk", "country", "Denmark", "DK"),
            _place(
                "r-cph",
                "region",
                "Copenhagen Municipality",
                "DK",
                aliases=["Copenhagen"],
                parent="dk",
            ),
            _place("us", "country", "United States", "US", ["USA"]),
            _place("r-ny", "region", "New York", "US", parent="us"),
            _place("c-dk", "city", "Denmark", "US", ["Town of Denmark"], parent="r-ny"),
            _place("c-cph", "city", "Copenhagen", "US", parent="c-dk"),
            _place("ca", "country", "Canada", "CA"),
            _place(
                "r-hfx",
                "region",
                "Halifax",
                "CA",
                names={"pms": "Halifax (Canadà)"},
                parent="ca",
            ),
            _place("c-hfx", "city", "Halifax", "CA", parent="r-hfx"),
            _place("r-eng", "region", "England", "GB", names={"lv": "Anglija"}),
            _place(
                "c-ash-1",
                "city",
                "Ashington",
                "GB",
                names={"lt": "Ashington, Anglija", "ro": "Ashington, Anglia"},
                parent="r-eng",
            ),
            _place("c-ash-2", "city", "Ashington", "GB", parent="r-eng"),
            _place("c-and-1", "city", "Andover", "US", ["Andover, USA"], parent="r-ny"),
            _place("c-and-2", "city", "Andover", "US", parent="r-ny"),
        ],
        {
            "r-cph": 13,
            "c-cph": 1,
            "r-hfx": 3,
            "c-hfx": 3,
            "c-ash-1": 1,
            "c-ash-2": 2,
            "c-and-1": 1,
            "c-and-2": 4,
        },
    )
    assert lookup.resolve(query).id == expected


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
        (
            True,
            "sampled",
            r"input 0 \(f1\) left out .*attributions.txt exceeds max_notices_per_file",
            None,
            None,
        ),
        (True, "blocked", None, INHERITED, {"errors": 0, "codes": {}}),
        (
            True,
            "blocked-introduced",
            r"introduced 1 .*\(foreign_key_violation 1\)",
            INHERITED,
            {"errors": 1, "codes": {"foreign_key_violation": 1}},
        ),
    ],
    ids=[
        "inherited",
        "strict",
        "unchecked",
        "introduced",
        "input-changed-after-read",
        "capped",
        "sampled",
        "blocked",
        "blocked-introduced",
    ],
)
def test_merge_refuses_only_errors_it_introduced(
    tmp_path, monkeypatch, check, variant, refusal, inherited, introduced
):
    # A merge refused every error-severity notice of the merged feed, those
    # its inputs already carried included, so feeds merged clean only when
    # every input was clean; and an input whose block reached the overlap
    # check's pair cap counted as sampled, so its merge was refused.
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
    if variant in ("blocked", "blocked-introduced"):
        # 142 trips in one block: 10,011 pairs, over the overlap check's cap.
        for n in range(142):
            start = 36_000 + 120 * n
            stops = [("s1", start, start), ("s2", start + 60, start + 60)]
            first.add_trip("r1", "wk", f"b{n}", stops, block_id="b")
    second = feed("09:00:00", "09:05:00")
    if variant in ("dangling-stop", "input-changed-after-read", "blocked-introduced"):
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
    if variant in ("capped", "blocked-introduced"):
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
        assert (path, kept_window) == (source, window)
        assert report["summary"]["droppedRows"] is None  # not cropped
        return
    with pytest.raises(_SkipFeed) as caught:
        _process_feed(source, **options)
    assert (caught.value.reason, caught.value.window) == (reason, window)


FK = "foreign_key_violation"


@pytest.mark.parametrize(
    "changes, dropped, counts, note",
    [
        pytest.param(
            {
                "stop_times.txt": FEED["stop_times.txt"]
                + "t-in,08:10:00,08:10:00,ghost,3\n"
            },
            [(FK, "stop_times.txt", "stop_id", "stops.txt", "ghost")],
            (1, 2),
            "dropped 1 stop_times.txt rows whose stop_id is not in stops.txt",
            id="stop",
        ),
        pytest.param(
            {
                "trips.txt": FEED["trips.txt"] + "r-ghost,wk,t-ghost\n",
                "stop_times.txt": FEED["stop_times.txt"]
                + "t-ghost,10:00:00,10:00:00,in1,1\nt-ghost,10:05:00,10:05:00,in2,2\n",
            },
            [(FK, "trips.txt", "route_id", "routes.txt", "r-ghost")],
            (1, 2),
            "dropped 1 trips.txt rows whose route_id is not in routes.txt",
            id="route",
        ),
        pytest.param(
            {
                "trips.txt": FEED["trips.txt"] + "r-in,wk,t-short\nr-in,wk,t-one\n",
                "stop_times.txt": FEED["stop_times.txt"]
                + "t-short,10:00:00,10:00:00,in1,1\nt-short,10:05:00,10:05:00,ghost,2\n"
                + "t-one,11:00:00,11:00:00,in2,1\n",
            },
            [
                (FK, "stop_times.txt", "stop_id", "stops.txt", "ghost"),
                ("unusable_trip", "trips.txt", "trip_id", None, "t-short"),
            ],
            (2, 3),  # t-one had a single stop_time in the source and stays
            "dropped 1 stop_times.txt rows whose stop_id is not in stops.txt, "
            "dropped 1 trips.txt rows left with fewer than two stop_times",
            id="short-trip",
        ),
        pytest.param({}, [], (1, 2), None, id="consistent"),
        pytest.param(
            {"routes.txt": "agency_id,route_short_name,route_type\nhsl,1,3\n"},
            "routes.txt has no route_id column; cannot crop this feed",
            None,
            None,
            id="no-route-id",
        ),
    ],
)
def test_the_crop_drops_rows_naming_a_missing_stop_or_route(
    tmp_path, changes, dropped, counts, note
):
    # Moscow's cropped feed kept stop_times rows naming stops its stops.txt
    # lacked, and cafein refused the whole feed.
    from transitio.pipeline._fetch import _dropped_note, _process_feed

    source = write_zip(tmp_path / "feed.zip", {**FEED, **changes})
    output = tmp_path / "cropped.zip"
    if isinstance(dropped, str):
        with pytest.raises(OSError, match=dropped):
            crop_feed(source, output, aoi=CITY_BBOX)
        assert not output.exists()
        return
    result = crop_feed(source, output, aoi=CITY_BBOX)
    expected = [
        {
            "code": code,
            "filename": filename,
            "fieldName": field,
            "parentFilename": parent,
            "rowCount": 1,
            "valueCount": 1,
            "sampleValues": [value],
        }
        for code, filename, field, parent, value in dropped
    ]
    assert result["dropped_rows"] == expected
    row_counts = result["row_counts"]
    assert (row_counts["trips.txt"], row_counts["stop_times.txt"]) == counts
    codes = {n["code"] for n in validate_feed(output)["notices"]}
    assert "foreign_key_violation" not in codes
    _, report, *_ = _process_feed(
        source,
        geometry=CITY_BBOX,
        tag="t",
        repair=False,
        crop=True,
        modes=None,
        day=None,
        study=False,
        hosted=None,
        budgets={},
    )
    assert report["summary"]["droppedRows"] == expected
    assert _dropped_note(report) == note


@pytest.mark.parametrize(
    "repeats, count",
    [
        pytest.param("r-in,wk,t-in\n" * 3, 3, id="exact"),
        pytest.param(" r-in ,wk, t-in\n", 1, id="padded"),
        pytest.param("r-out,wk,t-in\n", None, id="differing"),
    ],
)
def test_the_crop_drops_exact_repeats_of_a_trip(tmp_path, repeats, count):
    # Delhi's feed repeats eight trips.txt rows exactly, and the crop
    # refused the feed as ambiguous.
    from transitio.pipeline._fetch import _dropped_note, _process_feed

    trips = FEED["trips.txt"] + repeats
    source = write_zip(tmp_path / "feed.zip", {**FEED, "trips.txt": trips})
    output = tmp_path / "cropped.zip"
    if count is None:
        with pytest.raises(OSError, match='repeats trip_id "t-in"'):
            crop_feed(source, output, aoi=CITY_BBOX)
        assert not output.exists()
        return
    result = crop_feed(source, output, aoi=CITY_BBOX)
    assert result["dropped_rows"] == [
        {
            "code": "duplicate_key",
            "filename": "trips.txt",
            "fieldName": "trip_id",
            "parentFilename": None,
            "rowCount": count,
            "valueCount": 1,
            "sampleValues": ["t-in"],
        }
    ]
    assert result["row_counts"]["trips.txt"] == 1
    codes = {n["code"] for n in validate_feed(output)["notices"]}
    assert "duplicate_key" not in codes
    _, report, *_ = _process_feed(
        source,
        geometry=CITY_BBOX,
        tag="t",
        repair=False,
        crop=True,
        modes=None,
        day=None,
        study=False,
        hosted=None,
        budgets={},
    )
    assert _dropped_note(report) == f"dropped {count} exact duplicate trips.txt rows"


@pytest.mark.parametrize(
    "replaced, fields",
    [
        pytest.param(
            {
                "stops.txt": (b"in1,Kamppi", "in1,Kamp\ufffdi".encode()),
                "routes.txt": (b"r-in,hsl,1", "r-in,hsl,\ufffd1".encode()),
            },
            {("stops.txt", "stop_name"), ("routes.txt", "route_short_name")},
            id="text",
        ),
        pytest.param(
            {
                "stops.txt": (b"in1,", b"in\xff1,"),
                "stop_times.txt": (b",in1,", b",in\xff1,"),
            },
            {("stops.txt", "stop_id"), ("stop_times.txt", "stop_id")},
            id="id",
        ),
    ],
)
def test_rows_holding_an_invalid_character_are_kept(tmp_path, replaced, fields):
    # Istanbul's feed writes U+FFFD in a stop name and a route name; the
    # reader skipped both rows, and the crop kept the rows naming them.
    files = dict(FEED)
    for name, (old, new) in replaced.items():
        files[name] = FEED[name].encode().replace(old, new)
    source = write_zip(tmp_path / "feed.zip", files)

    def check(notices):
        codes = {n["code"] for n in notices}
        assert "foreign_key_violation" not in codes
        found = {
            (n["context"]["filename"], n["context"]["fieldName"])
            for n in notices
            if n["code"] == "invalid_character"
        }
        assert found == fields

    check(validate_feed(source)["notices"])
    output = tmp_path / "cropped.zip"
    result = crop_feed(source, output, aoi=CITY_BBOX)
    assert result["dropped_rows"] == []
    assert result["row_counts"]["stop_times.txt"] == 2
    stop = replaced["stops.txt"][1].decode(errors="replace")
    assert stop in read_entry(output, "stops.txt").decode()
    check(result["remaining_notices"])
    assert repair_feed(source, tmp_path / "repaired.zip")["fixes"] == []


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


def test_copies_of_a_trip_coded_as_bus_and_local_bus_are_merged_once(tmp_path):
    # Munich's city operator codes its buses 704 (local bus) where the
    # regional feed codes them 3, so a merge kept both copies of each trip.
    from transitio.edit import FeedEditor
    from transitio.gtfs import merge_feeds

    local = {**MIDLAND, "routes.txt": MIDLAND["routes.txt"].replace(",3\n", ",704\n")}
    feeds = [
        write_zip(tmp_path / f"{n}.zip", files)
        for n, files in enumerate((MIDLAND, local))
    ]
    report = merge_feeds(feeds, tmp_path / "merged.zip", check=False)
    merged = FeedEditor(tmp_path / "merged.zip").tables
    assert list(merged["trips.txt"]["trip_id"]) == ["f1:t1"]
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


def test_delivered_feeds_do_not_repeat_each_others_trips(tmp_path, monkeypatch):
    # In Munich the city operator's feed repeated most trips of the regional
    # feed under its own agency name, a minute and some metres off, and
    # fetch delivered both copies to be routed together.
    import httpx

    import transitio.index as transitio_index
    from index_fixture import HULL, covered_feed, edge, write_index
    from transitio.catalog import TransitlandAtlas
    from transitio.pipeline import fetch

    agency = MIDLAND["agency.txt"].replace("Midland Bluebird", "First Glasgow")
    operator = {
        **MIDLAND,
        "agency.txt": agency,
        # 20 m north of the regional feed's stops.
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\n"
        "a,A,55.86018,-4.25\nb,B,55.87018,-4.26\n",
        "trips.txt": MIDLAND["trips.txt"] + "x36,wk,t2\n",
        "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "t1,08:01:00,08:01:00,a,1\nt1,08:11:00,08:11:00,b,2\n"
        "t2,10:00:00,10:00:00,a,1\nt2,10:10:00,10:10:00,b,2\n",
    }
    payloads = {
        feed_id: write_zip(tmp_path / f"{feed_id}.zip", files).read_bytes()
        for feed_id, files in (("f-a", MIDLAND), ("f-b", operator))
    }
    feeds = [
        {
            **covered_feed(feed_id, coverage_source="crawl"),
            "coverage": HULL,
            "atlas": {"urls": {"static_current": f"https://feeds.example/{feed_id}"}},
        }
        for feed_id in payloads
    ]
    edges = [edge("Q1757", f["feed_id"], tier="local") for f in feeds]
    index = transitio_index.read_index(
        write_index(tmp_path / "index", feeds=feeds, edges=edges)
    )

    def handler(request):
        return httpx.Response(200, content=payloads[request.url.path.strip("/")])

    class Served(TransitlandAtlas):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.delenv("MOBILITY_API_REFRESH_TOKEN", raising=False)
    monkeypatch.setattr("transitio.catalog.TransitlandAtlas", Served)
    result = fetch(place="Q1757", index=index, crop=False, osm=False, expired="keep")
    trips = [read_entry(path, "trips.txt").decode().split() for path in result.feeds]
    assert [rows[1:] for rows in trips] == [["x36,wk,t1"], ["x36,wk,t2"]]
    assert result.selection[1]["note"] == "1 repeated trips of f-a left out"


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


@pytest.mark.parametrize("client", ["mdb", "atlas", "index"])
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

    url = "https://feeds.example/gtfs.zip"
    sent = []

    def handler(request):
        sent.append(request)
        # The smallest valid zip: an empty archive's end record.
        return httpx.Response(200, content=b"PK\x05\x06" + bytes(18))

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


def test_an_area_across_a_border_gets_the_smallest_extract_containing_it(
    tmp_path, monkeypatch
):
    # A place grown across a national border was given Geofabrik's
    # whole-continent extract, since only Geofabrik extracts were ranked and
    # one had to cover the area's envelope.
    import json
    import pathlib
    import shutil
    import types

    from pyrosm import get_data
    from shapely.geometry import box

    from transitio.osm import fetch_pbf
    from transitio.osm._fetch import _buffered

    url = "https://download.bbbike.org/osm/bbbike/Basel/Basel.osm.pbf"
    areas = []

    def get_data_by_area(area, directory=None, **kwargs):
        areas.append(area)
        path = pathlib.Path(directory) / "bbbike_Basel.osm.pbf"
        shutil.copyfile(get_data("test_pbf"), path)
        fields = dict(provider="BBBike", extract="Basel", url=url, bytes=100138363)
        source = types.SimpleNamespace(
            path=str(path), sha256="0" * 64, snapshot=None, **fields
        )
        return types.SimpleNamespace(
            path=str(path),
            failed=[],
            sources=[source],
            sha256=source.sha256,
            snapshot=None,
            **fields,
        )

    def get_data_by_bbox(*args, **kwargs):
        pytest.fail("the extract was ranked among Geofabrik extracts only")

    monkeypatch.setattr("pyrosm.get_data_by_area", get_data_by_area)
    monkeypatch.setattr("pyrosm.get_data_by_bbox", get_data_by_bbox)
    polygon = box(7.55, 47.52, 7.65, 47.60)
    path = fetch_pbf(polygon, crop=False, buffer_m=1600, cache_dir=tmp_path)
    (area,) = areas
    assert area.equals(_buffered(polygon, 1600))
    sidecar = json.loads(path.with_suffix(".provenance.json").read_text())
    assert (sidecar["provider"], sidecar["extract"], sidecar["source_url"]) == (
        "BBBike",
        "Basel",
        url,
    )


def test_extracts_need_cover_only_the_stops_within_the_buffer(tmp_path, monkeypatch):
    # A place grown across a border took the one extract containing all of
    # it (Geofabrik's Alps for Zermatt), though its stops needed far less.
    import math
    import pathlib

    import shapely

    from transitio.osm import fetch_pbf
    from transitio.osm._fetch import _area_km2, _buffered

    calls = []

    def get_data_by_area(area, directory=None, output_path=None, **kwargs):
        calls.append(kwargs)
        extract = pathlib.Path(directory) / "geofabrik_finland-latest.osm.pbf"
        return _finland_extract(extract, False, output_path)

    monkeypatch.setattr("pyrosm.get_data_by_area", get_data_by_area)
    aoi = shapely.box(7.70, 45.95, 7.80, 46.05)
    east = 111_320 * math.cos(math.radians(46.0))
    # Well inside; in the grown margin, 200 m from its edge; 20 km outside.
    points = [(7.75, 46.0), (7.80 + 1400 / east, 46.0), (7.80 + 20_000 / east, 46.0)]
    stops = shapely.multipoints(points)
    path = fetch_pbf(aoi, buffer_m=1600, must_cover=stops, cache_dir=tmp_path)
    plain = fetch_pbf(aoi, buffer_m=1600, cache_dir=tmp_path)

    call, without = calls
    assert call["strategy"] == "smallest_total" and without["must_cover"] is None
    must_cover = call["must_cover"]
    discs = [_buffered(shapely.Point(point), 1600) for point in points]
    inner, margin = (_area_km2(must_cover.intersection(disc)) for disc in discs[:2])
    assert inner == pytest.approx(_area_km2(discs[0]), rel=1e-6)
    assert 0 < margin < 0.9 * _area_km2(discs[1])
    assert not must_cover.intersects(discs[2])
    assert must_cover.equals(
        _buffered(shapely.multipoints(points[:2]), 1600).intersection(
            _buffered(aoi, 1600)
        )
    )
    assert path.name != plain.name


def _finland_extract(path, update, output_path=None, crop=None):
    """An ``AreaExtract`` stand-in for Geofabrik's Finland extract at ``path``,
    written as pyrosm would: when missing, pyrosm's ``test_pbf``, on update
    its ``helsinki_pbf``, then cropped to ``output_path`` when given by
    ``crop(path, output_path)``, by default ``b"crop of "`` and the
    extract's bytes."""
    import hashlib
    import pathlib
    import shutil
    import types

    from pyrosm import get_data

    def crop_of(source, target):
        target.write_bytes(b"crop of " + source.read_bytes())

    if update or not path.exists():
        shutil.copyfile(get_data("helsinki_pbf" if update else "test_pbf"), path)
    written = path
    if output_path is not None:
        written = pathlib.Path(output_path)
        (crop or crop_of)(path, written)
    fields = dict(
        provider="Geofabrik",
        extract="finland",
        url="https://download.geofabrik.de/europe/finland-latest.osm.pbf",
        bytes=None,
    )
    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    source = types.SimpleNamespace(
        path=str(path), sha256=sha256, snapshot=None, **fields
    )
    return types.SimpleNamespace(
        path=str(written),
        failed=[],
        sources=[source],
        sha256=hashlib.sha256(written.read_bytes()).hexdigest(),
        snapshot=None,
        **fields,
    )


def test_fetches_sharing_a_cache_take_turns(tmp_path, monkeypatch):
    # A fetch could replace the cached extract while another cropped it and
    # took its checksum, or overwrite the other's sidecar, so a file's
    # provenance could name bytes it was not made from.
    import concurrent.futures
    import hashlib
    import json
    import pathlib
    import threading

    from pyrosm import get_data

    from transitio.osm import fetch_pbf

    extract = tmp_path / "osm" / "geofabrik_finland-latest.osm.pbf"
    updates = []
    cropping, release = threading.Event(), threading.Event()

    def slow_crop(source, target):
        content = source.read_bytes()
        cropping.set()
        release.wait(30)
        target.write_bytes(b"crop of " + content)

    def get_data_by_area(area, update=False, output_path=None, **kwargs):
        updates.append(update)
        return _finland_extract(extract, update, output_path, slow_crop)

    monkeypatch.setattr("pyrosm.get_data_by_area", get_data_by_area)
    bbox = (24.6, 60.1, 25.2, 60.4)
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        try:
            crop = pool.submit(fetch_pbf, bbox, cache_dir=tmp_path)
            assert cropping.wait(30)
            full = pool.submit(
                fetch_pbf, bbox, crop=False, update=True, cache_dir=tmp_path
            )
            assert not concurrent.futures.wait([full], timeout=0.5).done
            assert updates == [False]
        finally:
            release.set()
        paths = crop.result(30), full.result(30)
    crop_sidecar, full_sidecar = (
        json.loads(path.with_suffix(".provenance.json").read_text()) for path in paths
    )
    old, new = (
        pathlib.Path(get_data(name)).read_bytes()
        for name in ("test_pbf", "helsinki_pbf")
    )
    assert paths[0].read_bytes() == b"crop of " + old
    assert crop_sidecar["extract_sha256"] == hashlib.sha256(old).hexdigest()
    assert full_sidecar["extract_sha256"] == hashlib.sha256(new).hexdigest()


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges")
def test_a_crop_and_its_sidecar_replace_what_is_at_their_names(tmp_path, monkeypatch):
    # The crop and its sidecar were written straight to their names, so a
    # failed crop left a truncated file that later calls returned as cached,
    # and a symlink at either name had its target overwritten. A failed
    # update also left the replaced extract's sidecar describing old bytes.
    import os
    import pathlib

    from transitio.osm import fetch_pbf

    fail = []

    def crop(source, target):
        target.write_bytes(b"\x00crop")
        if fail:
            raise RuntimeError("crop failed")

    def get_data_by_area(area, update=False, directory=None, **kwargs):
        extract = pathlib.Path(directory) / "geofabrik_finland-latest.osm.pbf"
        return _finland_extract(extract, update, kwargs.get("output_path"), crop)

    monkeypatch.setattr("pyrosm.get_data_by_area", get_data_by_area)
    bbox = (24.6, 60.1, 25.2, 60.4)
    extract = fetch_pbf(bbox, crop=False, cache_dir=tmp_path)
    extract_sidecar = extract.with_suffix(".provenance.json")
    os.utime(extract, (0, 0))
    os.utime(extract_sidecar, (86400, 86400))
    path = fetch_pbf(bbox, cache_dir=tmp_path)
    names = path, path.with_suffix(".provenance.json")
    for name in names:
        outside = tmp_path / f"outside-{name.suffix}"
        outside.write_text("do not clobber")
        name.unlink()
        name.symlink_to(outside)

    fail.append(True)
    with pytest.raises(RuntimeError, match="crop failed"):
        fetch_pbf(bbox, cache_dir=tmp_path, update=True)
    assert all(name.is_symlink() for name in names)
    assert not extract_sidecar.exists()
    assert [p for p in path.parent.iterdir() if p.is_dir()] == []
    fail.clear()
    assert fetch_pbf(bbox, cache_dir=tmp_path, update=True) == path
    assert not any(name.is_symlink() for name in names)
    assert path.read_bytes() == b"\x00crop"
    outside = sorted(tmp_path.glob("outside-*"))
    assert [p.read_text() for p in outside] == ["do not clobber"] * 2


@pytest.mark.parametrize("status", [200, 404])
def test_feeds_nested_in_one_archive_are_read_from_it_once(
    tmp_path, monkeypatch, status
):
    # Feeds whose URL fragment names a zip inside a larger archive were each
    # delivered as the whole archive, downloaded once per feed, and skipped as
    # "feed has no usable trips.txt".
    import json

    import httpx

    import transitio.index as transitio_index
    from index_fixture import HULL, covered_feed, edge, write_index
    from transitio.catalog import TransitlandAtlas
    from transitio.pipeline import fetch

    outer = "https://data.example/outer.zip"
    urls = {f"f-{n}": f"{outer}#{n}/google_transit.zip" for n in (1, 2)}
    feeds = [
        {
            **covered_feed(feed_id, coverage_source="crawl"),
            "coverage": HULL,
            "atlas": {"urls": {"static_current": url}},
        }
        for feed_id, url in urls.items()
    ]
    edges = [edge("Q1757", feed_id, tier="local") for feed_id in urls]
    index = transitio_index.read_index(
        write_index(tmp_path / "index", feeds=feeds, edges=edges)
    )
    stops = {"f-1": FEED["stops.txt"], "f-2": FEED["stops.txt"].replace("60.", "61.")}
    inner = {
        f"{n}/google_transit.zip": write_zip(
            tmp_path / f"{n}.zip", {**FEED, "stops.txt": stops[f"f-{n}"]}
        ).read_bytes()
        for n in (1, 2)
    }
    payload = write_zip(tmp_path / "outer.zip", inner).read_bytes()
    requests = []

    def handler(request):
        requests.append((request.method, request.url.path))
        return httpx.Response(status, content=payload if status == 200 else b"")

    class Served(TransitlandAtlas):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.delenv("MOBILITY_API_REFRESH_TOKEN", raising=False)
    monkeypatch.setattr("transitio.catalog.TransitlandAtlas", Served)
    out = tmp_path / "out"
    result = fetch(
        place="Q1757", index=index, directory=out, crop=False, osm=False, expired="keep"
    )
    assert requests == [("GET", "/outer.zip")]
    # The directory holds the feeds' own files, not the archive.
    assert {path.name.split(".")[0] for path in out.glob("*")} <= set(urls)
    if status == 404:
        reason = f"download failed: atlas: {outer}: HTTP 404 Not Found"
        assert sorted(result.skipped) == [("f-1", reason), ("f-2", reason)]
        return
    assert {entry["feed_id"]: entry["decision"] for entry in result.selection} == {
        "f-1": "delivered",
        "f-2": "delivered",
    }
    for entry in result.selection:
        path = entry["path"]
        assert read_entry(path, "stops.txt").decode() == stops[entry["feed_id"]]
        sidecar = json.loads(path.with_suffix(".provenance.json").read_text())
        assert (sidecar["source_url"], sidecar["archive_url"]) == (
            urls[entry["feed_id"]],
            outer,
        )


_EXTRACT_TIMEOUT = (
    "https://download.geofabrik.de/europe-latest.osm.pbf: ReadTimeout: timed out"
    " (3 requests)"
)


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(DownloadError(_EXTRACT_TIMEOUT), id="download"),
        pytest.param(
            ExtractNotFoundError("no extract covers the area"), id="no-extract"
        ),
        pytest.param(ValueError("invalid area"), id="value"),
    ],
)
def test_a_failed_extract_download_keeps_the_fetched_feeds(
    tmp_path, monkeypatch, error
):
    # A read timeout on the OSM extract, fetched after the place's feeds,
    # raised from fetch and lost the feeds already downloaded and processed.
    import pathlib

    import transitio.index as transitio_index
    from index_fixture import HULL, covered_feed, edge, write_index
    from transitio.pipeline import fetch

    feed = {
        **covered_feed("f-a", coverage_source="crawl"),
        "coverage": HULL,
        "atlas": {"urls": {"static_current": "https://feeds.example/a.zip"}},
    }
    edges = [edge("Q1757", "f-a", tier="local")]
    index = transitio_index.read_index(
        write_index(tmp_path / "index", feeds=[feed], edges=edges)
    )

    def download(self, feed, directory=None):
        return write_zip(pathlib.Path(directory) / "latest.zip", FEED)

    def fetch_pbf(*args, **kwargs):
        raise error

    monkeypatch.delenv("MOBILITY_API_REFRESH_TOKEN", raising=False)
    monkeypatch.setattr("transitio.catalog.TransitlandAtlas._fetch_static", download)
    monkeypatch.setattr("transitio.osm.fetch_pbf", fetch_pbf)
    options = dict(
        place="Q1757",
        index=index,
        directory=tmp_path / "out",
        crop=False,
        expired="keep",
    )
    if not isinstance(error, DownloadError):
        with pytest.raises(type(error)) as caught:
            fetch(**options)
        assert caught.value is error
        return
    with pytest.warns(UserWarning, match="OSM extract not fetched"):
        result = fetch(**options)
    (delivered,) = result.selection
    assert delivered["decision"] == "delivered"
    assert result.feeds == [delivered["path"]]
    assert (result.osm_pbf, result.osm_area) == (None, None)
    assert result.osm_note == f"OSM extract not fetched: {_EXTRACT_TIMEOUT}"


_HIDDEN_NOTE = (
    "default view (region: secondary, tertiary) holds none of the place's 1 feed:"
    " f-bus (primary); tiers=['local'] fetches it"
)


@pytest.mark.parametrize(
    "bbox, note",
    [
        pytest.param(CITY_BBOX, None, id="town-sized"),
        pytest.param(WIDE_BBOX, _HIDDEN_NOTE, id="wide"),
    ],
)
def test_an_empty_default_view_is_not_fetched_silently(
    tmp_path, monkeypatch, bbox, note
):
    # A town-sized region or country (Monaco, San Juan) left its local-only
    # feeds out of the default view, and fetch(place=...) returned no feeds,
    # an empty selection record and no warning.
    import datetime
    import pathlib
    import warnings

    import shapely

    import transitio
    import transitio.index as transitio_index
    from index_fixture import covered_feed, edge, place, write_partitioned_index
    from transitio.pipeline import fetch

    monkeypatch.setattr(
        transitio, "__version__", transitio_index.MIN_READER_VERSIONS[7], raising=False
    )
    monkeypatch.setattr(
        "transitio.pipeline._fetch._today", lambda: datetime.date(2026, 6, 1)
    )
    feed = {
        **covered_feed("f-bus"),
        "atlas": {"urls": {"static_current": "https://feeds.example/bus.zip"}},
        "home_country": "FI",
        "scope": "domestic",
    }
    region = place("r", "region", geometry=shapely.to_wkb(shapely.box(*bbox)).hex())
    local = edge(
        "r", "f-bus", tier="local", relevance_category="primary", relevance=0.9
    )
    index = transitio_index.read_index(
        write_partitioned_index(
            tmp_path / "index", feeds=[feed], places=[region], edges=[local]
        )
    )
    calls = []

    def download(self, feed, directory=None):
        calls.append(feed.feed_id)
        return write_zip(pathlib.Path(directory) / "latest.zip", FEED)

    monkeypatch.delenv("MOBILITY_API_REFRESH_TOKEN", raising=False)
    monkeypatch.setattr("transitio.catalog.TransitlandAtlas._fetch_static", download)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = fetch(
            place="r", index=index, directory=tmp_path / "out", crop=False, osm=False
        )
    warned = [str(w.message) for w in caught if issubclass(w.category, UserWarning)]
    if note is None:
        (entry,) = result.selection
        assert (warned, calls, entry["decision"]) == ([], ["f-bus"], "delivered")
        return
    assert (warned, calls, result.feeds) == ([note], [], [])
    assert (result.selection, result.view_note) == ([], note)


@pytest.mark.parametrize("osm", [True, False])
def test_stops_beyond_the_osm_area_are_counted(tmp_path, monkeypatch, osm):
    # The crop keeps whole trips, so the stops of a kept trip beyond the place
    # lay outside the OSM area, where cafein gives them no footpaths, and
    # fetch neither counted nor noted them.
    import datetime
    import pathlib

    import shapely

    import transitio.index as transitio_index
    from index_fixture import covered_feed, edge, place, write_index
    from transitio.pipeline import fetch

    monkeypatch.setattr(
        "transitio.pipeline._fetch._today", lambda: datetime.date(2026, 6, 1)
    )
    feed = {
        **covered_feed("f-a"),
        "atlas": {"urls": {"static_current": "https://feeds.example/a.zip"}},
    }
    city = place("c", "city", geometry=shapely.to_wkb(shapely.box(*CITY_BBOX)).hex())
    index = transitio_index.read_index(
        write_index(
            tmp_path / "index",
            feeds=[feed],
            places=[city],
            edges=[edge("c", "f-a", tier="local")],
        )
    )
    # Trip t-in runs on to Espoo, beyond the place grown by 1.6 km.
    tables = dict(FEED)
    tables["stop_times.txt"] += "t-in,08:30:00,08:30:00,out1,3\n"

    def download(self, feed, directory=None):
        return write_zip(pathlib.Path(directory) / "latest.zip", tables)

    monkeypatch.delenv("MOBILITY_API_REFRESH_TOKEN", raising=False)
    monkeypatch.setattr("transitio.catalog.TransitlandAtlas._fetch_static", download)
    monkeypatch.setattr("transitio.osm.fetch_pbf", lambda *a, **k: tmp_path / "a.pbf")
    result = fetch(place="c", index=index, directory=tmp_path / "out", osm=osm)
    (entry,) = result.selection
    assert entry["decision"] == "delivered"
    if not osm:
        assert (entry["stops_outside_osm"], result.osm_note) == (None, None)
        return
    assert (entry["stops_outside_osm"], result.osm_note) == (
        1,
        "OSM area: 1 of 3 located stops outside it",
    )


def test_selector_fingerprints_read_members_as_large_as_the_build():
    # The build fingerprints members of a whole download up to 8 GiB; with a
    # smaller ceiling here, every selector of a national aggregate whose
    # stop_times.txt runs to several GiB is stale and the feed is delivered whole.
    from transitio.index import fingerprint

    assert fingerprint._MAX_MEMBER_BYTES == 8 * 1024**3


def test_delivered_feeds_are_named_by_feed_id(tmp_path, monkeypatch):
    # Feeds delivered into a directory each sat in an id-<sha256> folder, so
    # the feeds of a Munich fetch could be told apart only by their sidecars.
    import hashlib
    import json
    import pathlib

    import transitio.index as transitio_index
    from index_fixture import HULL, covered_feed, edge, write_index
    from transitio.pipeline import fetch

    ids = ["f-u281z9-mvv", "f-nvbw~ding", "f-u2f-pražskáintegrovanádoprava"]
    feeds = [
        {
            **covered_feed(feed_id, coverage_source="crawl"),
            "coverage": HULL,
            "atlas": {"urls": {"static_current": f"https://feeds.example/{n}.zip"}},
        }
        for n, feed_id in enumerate(ids)
    ]
    edges = [edge("Q1757", feed_id, tier="local") for feed_id in ids]
    index = transitio_index.read_index(
        write_index(tmp_path / "index", feeds=feeds, edges=edges)
    )

    def download(self, feed, directory=None):
        # Each feed's trips run hours apart from the others', so none repeats.
        n = ids.index(feed.feed_id)
        times = FEED["stop_times.txt"].replace("08:", f"1{n}:").replace("09:", f"2{n}:")
        tables = {**FEED, "stop_times.txt": times}
        return write_zip(pathlib.Path(directory) / "latest.zip", tables)

    monkeypatch.delenv("MOBILITY_API_REFRESH_TOKEN", raising=False)
    monkeypatch.setattr("transitio.catalog.TransitlandAtlas._fetch_static", download)
    out = tmp_path / "out"
    result = fetch(
        place="Q1757", index=index, directory=out, crop=False, osm=False, expired="keep"
    )
    sha = hashlib.sha256(ids[2].encode("utf-8")).hexdigest()
    names = [*ids[:2], f"f-u2f-prazskaintegrovanadoprava+{sha}"]
    assert result.paths == {i: out / f"{name}.zip" for i, name in zip(ids, names)}
    assert list(result.paths.values()) == result.feeds
    for feed_id, path in result.paths.items():
        sidecar = json.loads(path.with_suffix(".provenance.json").read_text())
        assert sidecar["feed_id"] == feed_id
    files = [name + end for name in names for end in (".zip", ".provenance.json")]
    assert sorted(path.name for path in out.iterdir()) == sorted(files)


def test_a_feed_is_read_from_the_csv_export_without_a_token(tmp_path, monkeypatch):
    # MobilityDatabase.feed() raised MissingTokenError without a token, while
    # search_feeds() read the same feeds from the catalogue export.
    import httpx

    from transitio.catalog import MobilityDatabase

    body = (
        "id,data_type,status,provider,location.country_code,urls.latest\n"
        "mdb-1,gtfs,active,HSL,FI,https://files.example/mdb-1/latest.zip\n"
        "mdb-2,gtfs_rt,active,HSL RT,FI,\n"
    )
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, text=body)

    monkeypatch.delenv("MOBILITY_API_REFRESH_TOKEN", raising=False)
    transport = httpx.MockTransport(handler)
    with MobilityDatabase(None, cache_dir=tmp_path, transport=transport) as db:
        with pytest.warns(UserWarning, match="CSV catalogue export") as caught:
            feed = db.feed("mdb-1")
            assert db.search_feeds(country_code="FI") == [feed]
            for feed_id in ("mdb-2", "mdb-9"):
                with pytest.raises(LookupError, match=f"no GTFS feed '{feed_id}'"):
                    db.feed(feed_id)
    # Every warning points at the caller, and no API request was made.
    assert {warning.filename for warning in caught} == {__file__}
    assert [request.url.path for request in sent] == ["/feeds_v2.csv"]


def test_an_area_fetch_selects_the_feeds_of_the_index_places(tmp_path, monkeypatch):
    # fetch(aoi=...) searched the catalogue by bounding box and downloaded
    # every feed whose box met the area's, continental aggregates included.
    import datetime
    import pathlib

    import shapely

    import transitio
    import transitio.index as transitio_index
    from index_fixture import covered_feed, edge, place, write_partitioned_index
    from transitio.catalog import MobilityDatabase
    from transitio.pipeline import fetch

    monkeypatch.setattr(
        transitio, "__version__", transitio_index.MIN_READER_VERSIONS[7], raising=False
    )
    monkeypatch.setattr(
        "transitio.pipeline._fetch._today", lambda: datetime.date(2026, 6, 1)
    )

    def box(*bounds):
        return shapely.to_wkb(shapely.box(*bounds)).hex()

    places = [
        place("fi", "country", geometry=box(24, 60, 26, 61)),
        place("ee", "country", country_code="EE", geometry=box(24, 59, 26, 60)),
        place("c", "city", parent_id="fi", geometry=box(*CITY_BBOX)),
    ]
    tiers = {"local": "primary", "regional": "secondary", "national": "tertiary"}
    feeds = [
        {
            **covered_feed(f"f-{tier}"),
            "atlas": {"urls": {"static_current": f"https://feeds.example/{tier}"}},
            "home_country": "FI",
            "scope": "domestic",
        }
        for tier in tiers
    ]
    edges = [
        edge(where, f"f-{tier}", tier=tier, relevance_category=category, relevance=1)
        for tier, category in tiers.items()
        for where in ("c", "fi")
    ]
    index = transitio_index.read_index(
        write_partitioned_index(
            tmp_path / "index", feeds=feeds, places=places, edges=edges
        )
    )
    downloads, searches, extracts = [], [], []

    def download(self, feed, directory=None):
        downloads.append(feed.feed_id)
        return write_zip(pathlib.Path(directory) / "latest.zip", FEED)

    def search(self, *args, **kwargs):
        searches.append(kwargs["aoi"].bounds)
        return []

    def extract(area, **options):
        extracts.append((area.bounds, sorted(options)))
        return tmp_path / "area.osm.pbf"

    monkeypatch.delenv("MOBILITY_API_REFRESH_TOKEN", raising=False)
    monkeypatch.setattr("transitio.catalog.TransitlandAtlas._fetch_static", download)
    monkeypatch.setattr(MobilityDatabase, "search_feeds", search)
    monkeypatch.setattr("transitio.osm.fetch_pbf", extract)
    options = dict(index=index, directory=tmp_path / "out", crop=False)
    result = fetch(CITY_BBOX, **options)
    assert (downloads, searches) == (["f-local", "f-regional"], [])
    assert [p.id for p in result.places] == ["c"]
    assert result.snapshot == index.snapshot_id
    # The extract covers the area itself, not grown.
    assert extracts == [(CITY_BBOX, ["cache_dir", "directory", "progress"])]
    # Mostly in a country without feeds: the catalogue, with a warning.
    mostly_ee = (24.9, 59.7, 25.0, 60.2)
    with pytest.warns(UserWarning) as caught:
        result = fetch(mostly_ee, **options)
    assert (
        "the feed index's places cover 19% of the area; 0 feeds from the "
        "Mobility Database catalogue by bounding box"
    ) in [str(warning.message) for warning in caught]
    assert (searches, result.places, result.snapshot) == ([mostly_ee], [], None)


def test_stops_at_the_origin_are_not_located(tmp_path):
    # A stop at (0, 0), which stands for a missing position, was counted as a
    # located stop outside the OSM area; the fingerprint still reads it, as the
    # build's does.
    import shapely

    from transitio.index import fingerprint
    from transitio.pipeline._fetch import _count_outside, _stop_coords

    stops = FEED["stops.txt"] + "zero,Null Island,0.0,0.0\n"
    path = write_zip(tmp_path / "feed.zip", {**FEED, "stops.txt": stops})
    record = [{"decision": "delivered", "path": path}]
    counts = _count_outside(record, shapely.box(*CITY_BBOX), {path: _stop_coords(path)})
    assert (record[0]["stops_outside_osm"], counts) == (1, (1, 3, 0))
    with zipfile.ZipFile(path) as archive:
        assert fingerprint._member_coords(archive)["zero"] == (0.0, 0.0)


def test_a_crop_drops_the_areas_groups_and_networks_it_orphans(tmp_path):
    # A crop pruned stop_areas.txt, location_group_stops.txt and
    # route_networks.txt but kept every area, location group and network,
    # so the cropped feed defined those of the stops and routes it removed.
    files = {
        **FEED,
        "areas.txt": "area_id\na-in\na-out\n",
        "stop_areas.txt": "area_id,stop_id\na-in,in1\na-out,out1\n",
        "location_groups.txt": "location_group_id\nlg-in\nlg-out\n",
        "location_group_stops.txt": (
            "location_group_id,stop_id\nlg-in,in1\nlg-out,out1\n"
        ),
        "networks.txt": "network_id\nn-in\nn-out\n",
        "route_networks.txt": "network_id,route_id\nn-in,r-in\nn-out,r-out\n",
    }
    source = write_zip(tmp_path / "feed.zip", files)
    output = tmp_path / "cropped.zip"
    crop_feed(source, output, aoi=CITY_BBOX, reference_date="20260601")
    for name, kept in [
        ("areas.txt", "a-in"),
        ("location_groups.txt", "lg-in"),
        ("networks.txt", "n-in"),
    ]:
        rows = csv.reader(io.StringIO(read_entry(output, name).decode()))
        assert [row[0] for row in rows][1:] == [kept]
    report = validate_feed(output, reference_date="20260601")
    assert not any(n["severity"] == "ERROR" for n in report["notices"])


def test_the_index_download_shows_its_progress(tmp_path, monkeypatch, capsys):
    # A refresh held the whole archive, about 420 MB, in memory and printed
    # nothing while it downloaded.
    import httpx

    from index_fixture import API, DOWNLOADS, FakeGitHub, index, release
    from transitio.index import _refresh
    from transitio.index import release as contract

    monkeypatch.setattr(_refresh, "_state", {key: None for key in _refresh._state})
    fake = FakeGitHub()
    snapshot_id = release(fake, index(tmp_path))
    (asset,) = [
        asset
        for _, asset in fake.assets.values()
        if asset["name"] == contract.archive_name(snapshot_id)
    ]
    # As on GitHub, the archive's URL redirects to the asset host.
    path = asset["browser_download_url"].removeprefix(DOWNLOADS)
    asset["browser_download_url"] = "https://github.example" + path

    def handle(request):
        if request.url.host == "github.example":
            return httpx.Response(302, headers={"Location": DOWNLOADS + path})
        return fake.handle(request)

    transport = httpx.MockTransport(handle)
    for progress in (True, False):
        cache = tmp_path / f"cache-{progress}"
        summary = _refresh.refresh(
            repository="o/r",
            api_url=API,
            cache_dir=cache,
            transport=transport,
            progress=progress,
        )
        assert summary["installed"] and summary["snapshot_id"] == snapshot_id
        # Off a terminal the download's bar is a line.
        expected = (
            f"Downloading feed index snapshot {snapshot_id}\n"
            f"Unpacking and checking snapshot {snapshot_id}\n"
        )
        assert capsys.readouterr() == ("", expected if progress else "")


def test_the_munich_feeds_are_told_apart(tmp_path, monkeypatch):
    # Munich's feeds showed shares of about a quarter each and nothing said
    # that MVV, DELFI and gtfs.de urban each run nearly all of its service.
    import transitio
    import transitio.index as transitio_index
    from test_index_views import MUNICH, munich_index

    monkeypatch.setattr(
        transitio, "__version__", transitio_index.MIN_READER_VERSIONS[11]
    )
    three = {feed_id: MUNICH[feed_id] for feed_id in ("f-mvv", "f-delfi", "f-urban")}
    munich = transitio_index.place("muc", index=munich_index(tmp_path, three))
    table = munich.feeds(categories=None).to_dataframe().set_index("feed_id")
    assert table["covers"].round(2).to_dict() == {
        "f-mvv": 0.99,
        "f-delfi": 1.0,
        "f-urban": 0.94,
    }
    assert table.loc["f-urban", "repeats"] == "f-delfi 100 %, f-mvv 99 %"


def test_munich_takes_one_feed_and_says_why_it_leaves_out_the_rest(
    tmp_path, monkeypatch
):
    # Munich's view listed ten feeds and nothing said which to use: MVV alone
    # runs nearly all of the city's service, its S-Bahn typed as tram.
    import transitio
    import transitio.index as transitio_index
    from test_index_views import munich_index

    monkeypatch.setattr(
        transitio, "__version__", transitio_index.MIN_READER_VERSIONS[11]
    )
    munich = transitio_index.place("muc", index=munich_index(tmp_path))
    found = munich.recommend("2026-10-13")
    assert found.feed_ids == ["f-mvv"]
    assert str(found).splitlines() == [
        "Munich (city), 2026-10-13: take 1 feed, covering about 98 % of the "
        "departures the index records there, each counted once",
        "  + f-mvv: covers 98 % of the place's departures (98 % of bus; 98 % of "
        "rail, subway and tram)",
        "  - f-delfi (DELFI): repeats f-mvv (98 % of its departures); 550,396 "
        "stops against 28,330",
        "  - f-urban (Public Transport Germany): repeats f-mvv (99 % of its "
        "departures); 674,929 stops against 28,330",
        "  - f-mvg (MVG): repeats f-mvv (99 % of its departures); needs a free "
        "account with MVG API",
        "  - f-rail (Regional Rail): repeats f-mvv (100 % of its departures); "
        "needs credentials the index has no details for",
        "  - f-bw (BW aggregate): adds too little: 0.021 % of the place's "
        "departures",
        "  - f-tiny (Tiny): contained in f-bw",
        "  - f-old (MVV (old)): stale when indexed: its timetable ended 2026-07-31",
    ]


def test_a_feed_running_the_same_lines_less_often_does_not_replace_it(
    tmp_path, monkeypatch
):
    # Tallinn's own feed was left out: the national feed runs all its lines,
    # though with about half its departures, and stood in for all of them.
    import transitio
    import transitio.index as transitio_index
    from test_index_views import munich_index

    monkeypatch.setattr(
        transitio, "__version__", transitio_index.MIN_READER_VERSIONS[11]
    )
    feeds = {
        "f-city": {
            "name": "City",
            "modes": {"bus": 1000},
            "stops": 500,
            "with": {"f-nat": {"bus": 1.0}},
        },
        "f-nat": {
            "name": "National",
            "modes": {"bus": 500, "rail": 100},
            "stops": 400,
            "with": {"f-city": {"bus": 1.0}},
        },
    }
    place = transitio_index.place("muc", index=munich_index(tmp_path, feeds))
    assert place.recommend("2026-10-13").feed_ids == ["f-city", "f-nat"]
    table = place.feeds(categories=None).to_dataframe().set_index("feed_id")
    assert table.loc["f-city", "repeats"] == "f-nat 50 %"

    # In an area the cap holds place by place: an aggregate's many
    # departures elsewhere do not stand in for a city feed's where they meet.
    from index_fixture import covered_feed, write_partitioned_index
    from test_index_views import AREA_PLACES, _edge

    def served(place_id, feed_id, modes, shares):
        service = {"stops": 10, "routes": 1, "departures_per_day": sum(modes.values())}
        record = _edge(place_id, feed_id, "local", "primary", 0.5, service=service)
        overlap = {"departures": modes, "with": shares}
        return {**record, "evidence": {"overlap": overlap}}

    edges = [
        served("c1", "f-city", {"bus": 1000}, {"f-agg": {"bus": 1.0}}),
        served("c1", "f-agg", {"bus": 100}, {"f-city": {"bus": 1.0}}),
        served("k2", "f-agg", {"bus": 5000}, {}),
    ]
    feeds = [
        {**covered_feed(feed_id), "home_country": "AA"}
        for feed_id in ("f-city", "f-agg")
    ]
    path = write_partitioned_index(
        tmp_path / "area", feeds=feeds, places=AREA_PLACES, edges=edges, access={}
    )
    index = transitio_index.read_index(path)
    area = transitio_index.area((2, 0, 2.8, 0.4), index=index)
    assert sorted(area.recommend("2026-10-13").feed_ids) == ["f-agg", "f-city"]


def test_a_feed_measured_apart_is_not_counted_as_new_service(tmp_path, monkeypatch):
    # The merged index measured MVV's feed in another build than DELFI's, so
    # neither named the other and MVV was taken as service DELFI lacks; a
    # tram feed measured with DELFI and sharing no line still adds service.
    import transitio
    import transitio.index as transitio_index
    from test_index_views import munich_index

    monkeypatch.setattr(
        transitio, "__version__", transitio_index.MIN_READER_VERSIONS[11]
    )

    def spec(name, modes, stops, compared, shares=None):
        overlap = {"departures": modes, "with": shares or {}, "compared": compared}
        return {
            "name": name,
            "modes": modes,
            "stops": stops,
            "evidence": {"overlap": overlap},
        }

    feeds = {
        "f-delfi": spec(
            "DELFI",
            {"bus": 900, "rail": 500},
            5000,
            ["f-mvg", "f-tram"],
            {"f-mvg": {"bus": 1.0}},
        ),
        "f-mvg": spec(
            "MVG", {"bus": 400}, 300, ["f-delfi", "f-tram"], {"f-delfi": {"bus": 1.0}}
        ),
        "f-tram": spec("Tram", {"tram": 100}, 50, ["f-delfi", "f-mvg"]),
        "f-mvv": spec("MVV", {"bus": 900, "rail": 500}, 1000, []),
        # Apart too, but contained in DELFI: left out for that.
        "f-sub": {**spec("Sub", {"bus": 20}, 10, []), "contained": ["f-delfi"]},
    }
    found = transitio_index.place("muc", index=munich_index(tmp_path, feeds)).recommend(
        "2026-10-13"
    )
    assert found.feed_ids == ["f-delfi", "f-tram"]
    reasons = {c.feed.feed_id: c.reason for c in found.left_out}
    assert reasons["f-mvv"] == (
        "not compared: the index measured it apart from the feeds taken"
    )
    assert reasons["f-sub"] == "contained in f-delfi"
    assert found.note == (
        "the coverage leaves out 1 feed not compared with the others "
        "(1,400 departures a day)"
    )


def test_delete_rows_drops_its_rows_at_once_and_logs_them_one_by_one(monkeypatch):
    # delete_rows copied the whole table once per deleted row, so dropping a
    # route's stop_times from a large feed took minutes.
    import json

    from transitio.edit import FeedBuilder
    from transitio.edit import _changes

    builder = FeedBuilder()
    rows = [{"stop_id": f"s{i}", "stop_name": f"Stop {i}"} for i in range(6)]
    builder.insert_rows("stops.txt", rows)
    before = builder.tables["stops.txt"].copy()
    logged = len(builder.changes)

    def failing(*args, **kwargs):
        if len(builder.changes) > logged:
            raise RuntimeError("log full")
        original(*args, **kwargs)

    # A failure while logging leaves the table and the log as they were.
    original = builder._record
    monkeypatch.setattr(builder, "_record", failing)
    with pytest.raises(RuntimeError, match="log full"):
        builder.delete_rows("stops.txt", [5, 0, 2])
    monkeypatch.undo()
    assert builder.tables["stops.txt"].equals(before)
    assert len(builder.changes) == logged

    builder.delete_rows("stops.txt", [5, 0, 2])
    assert list(builder.tables["stops.txt"]["stop_id"]) == ["s1", "s3", "s4"]
    deleted = [c for c in builder.changes if c.kind == "delete"]
    assert [(c.row, c.row_count) for c in deleted] == [(5, 5), (2, 4), (0, 3)]
    assert [c.old for c in deleted] == [
        json.dumps(_changes._row_payload(before, row)) for row in (5, 2, 0)
    ]
    assert builder.undo() == "delete_rows"
    assert builder.tables["stops.txt"].equals(before)
    assert builder.redo() == "delete_rows"
    assert list(builder.tables["stops.txt"]["stop_id"]) == ["s1", "s3", "s4"]


def test_a_download_path_stays_within_windows_limit(tmp_path, monkeypatch):
    # On Windows a fetch failed with "No such file or directory": a download
    # nested the feed's 67-character folder twice under the default cache, a
    # 262-character path, past Windows' 260-character limit.
    import tempfile
    from pathlib import Path

    from transitio import _http, cache as feed_cache
    from transitio.catalog._cache import STAGING, FeedCache, _feed_dir

    store = FeedCache(tmp_path)
    with store.staging("f-mdb-2904") as folder:
        assert folder.parent == store.root / STAGING
        inner = folder / _feed_dir("f-mdb-2904") / "latest.zip.12345678.part"
        # 114 characters: about 175 under a default Windows cache root.
        assert len(str(inner.relative_to(store.root))) <= 120
        # Listing and clearing the cache leave the staging folder alone.
        assert feed_cache.clear(tmp_path) == 0 and folder.is_dir()
        assert feed_cache.info(tmp_path).empty
    assert not any((store.root / STAGING).iterdir())

    def missing(**kwargs):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(_http, "_windows", lambda: True)
    monkeypatch.setattr(tempfile, "mkstemp", missing)
    # Windows counts UTF-16 units: 125 astral characters are 250 of them.
    for name, too_long in (("d" * 300, True), ("\U0001f600" * 125, True), ("d", False)):
        with pytest.raises(OSError) as caught:
            with _http.replacing(Path("/" + name) / "latest.zip"):
                pass
        if too_long:
            assert "260-character limit" in str(caught.value)
            assert "cache_dir=Path.home() / 'tc'" in str(caught.value)
        else:
            assert type(caught.value) is FileNotFoundError
    # A writer that opens the staged path by name fails with its own error.
    monkeypatch.setattr(_http, "WINDOWS_MAX_PATH", len(str(tmp_path)) + 5)
    with pytest.raises(OSError, match="character limit") as caught:
        with _http.staged(tmp_path / "x.osm.pbf") as partial:
            raise RuntimeError(f"Open failed for '{partial}'")
    assert isinstance(caught.value.__cause__, RuntimeError)


def test_undo_and_redo_replay_a_run_of_rows_in_one_step(monkeypatch):
    # Undo and redo replayed the rows of delete_rows and insert_rows one at a
    # time, copying the table per row, so undoing drop_routes on a city's
    # feed ran for many minutes.
    from transitio.edit import FeedBuilder
    from transitio.edit import _changes
    from transitio.exceptions import ChangeLogDesyncError

    builder = FeedBuilder()
    builder.insert_rows("stops.txt", [{"stop_id": "first", "stop_name": "First"}])
    first = builder.tables["stops.txt"].copy()
    rows = [{"stop_id": f"s{i}", "stop_name": f"Stop {i}"} for i in range(50)]
    builder.insert_rows("stops.txt", rows)
    inserted = builder.tables["stops.txt"].copy()
    builder.delete_rows("stops.txt", range(0, 51, 3))
    deleted = builder.tables["stops.txt"].copy()

    puts = []
    put = _changes._TableView.put
    monkeypatch.setattr(
        _changes._TableView,
        "put",
        lambda view, filename, table: puts.append(filename)
        or put(view, filename, table),
    )
    # Each step writes the table once: restore, remove, restore, remove, restore.
    for step, label, expected in [
        (builder.undo, "delete_rows", inserted),
        (builder.redo, "delete_rows", deleted),
        (builder.undo, "delete_rows", inserted),
        (builder.undo, "insert_rows", first),
        (builder.redo, "insert_rows", inserted),
    ]:
        puts.clear()
        assert step() == label
        assert builder.tables["stops.txt"].equals(expected)
        assert puts == ["stops.txt"]

    # A row changed outside the log still refuses, naming that row, and
    # leaves the table as it was.
    builder.tables["stops.txt"].iat[3, 1] = "changed"
    edited = builder.tables["stops.txt"].copy()
    with pytest.raises(ChangeLogDesyncError, match=r"row 3 \(row to delete changed\)"):
        builder.redo()
    assert builder.tables["stops.txt"].equals(edited)


def test_a_recommendation_near_full_coverage_says_over_not_about_over():
    # Turku's summary read "covering about over 99 % of the departures".
    from types import SimpleNamespace

    from transitio.index.recommend import Recommendation

    area = SimpleNamespace(parts=())
    found = Recommendation(area, None, [], [], 0.997, {}, "overlap", None)
    assert str(found).splitlines()[0] == (
        "The area's 0 places: take 0 feeds, covering over 99 % of the departures "
        "the index records there, each counted once"
    )


def test_a_row_bar_counts_whole_rows(monkeypatch, capsys):
    # drop_routes' bar read "50.0/100": every bar scaled its counts as bytes.
    from tqdm import std

    from transitio import _progress

    monkeypatch.setattr(_progress, "_bar_class", lambda: (std.tqdm, False))
    for unit, shown in (("row", "1200/2000"), ("B", "1.20k/2.00k")):
        made = _progress.bar("Dropping", 2000, unit=unit)
        made.update(1200)
        made.close()
        assert shown in capsys.readouterr().err


def test_places_and_suggestions_list_the_resolved_city_first():
    # places("Augsburg") and suggest("augs") listed Augsburg's metros before
    # the city place("Augsburg") answers.
    import transitio.index as transitio_index
    from test_index_resolve import _index, _p

    idx = _index(
        [
            _p("Q-aug-m", "metro", "Augsburg", member_ids=["Q-aug"], country_code="DE"),
            _p("Q-aug", "city", "Augsburg", metro_ids=["Q-aug-m"], country_code="DE"),
        ]
    )
    assert transitio_index.place("Augsburg", index=idx).id == "Q-aug"
    assert [p.id for p in transitio_index.places("Augsburg", index=idx)] == [
        "Q-aug",
        "Q-aug-m",
    ]
    assert [s.place.id for s in transitio_index.suggest("augs", index=idx)] == [
        "Q-aug",
        "Q-aug-m",
    ]
    assert [
        s.place.id for s in transitio_index.suggest("augs", limit=1, index=idx)
    ] == ["Q-aug"]
    # Two cities of the name in the metro's country both go before it.
    twins = _index(
        [
            _p("Q-s-m", "metro", "Sburg", member_ids=["Q-s1"], country_code="US"),
            _p("Q-s1", "city", "Sburg", metro_ids=["Q-s-m"], country_code="US"),
            _p("Q-s2", "city", "Sburg", country_code="US"),
        ]
    )
    found = [s.place.id for s in transitio_index.suggest("sbu", index=twins)]
    assert found[2] == "Q-s-m" and sorted(found[:2]) == ["Q-s1", "Q-s2"]
    # Places without a country are no namesakes: the metro keeps its rank.
    nowhere = _index(
        [
            _p("Q-x-m", "metro", "Xburg", member_ids=["Q-x"], country_code=None),
            _p("Q-x", "city", "Xburg", metro_ids=["Q-x-m"], country_code=None),
        ]
    )
    assert [s.place.id for s in transitio_index.suggest("xbu", index=nowhere)] == [
        "Q-x-m",
        "Q-x",
    ]


def test_a_repair_goes_ahead_past_a_notice_limit_of_kinds_it_does_not_fix(tmp_path):
    # Turku's feed was refused: its shapes.txt had over 10,000 notices of a
    # kind the repair never touches.
    import io

    import transitio

    files = {
        "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\n"
        "a,A,https://a.example,Europe/Helsinki\n",
        "stops.txt": 'stop_id,stop_name,stop_lat,stop_lon\ns1,"Ka\npi",60.169,24.931\n'
        's2,"Ste\nsi",60.171,24.941\n',
        "routes.txt": "route_id,agency_id,route_short_name,route_type\nr1,a,1,3\n",
        "trips.txt": "route_id,service_id,trip_id\nr1,wk,t1\n",
        "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "t1,08:00:00,08:00:00,s1,1\nt1,08:05:00,08:05:00,s2,2\n",
        "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,"
        "saturday,sunday,start_date,end_date\nwk,1,1,1,1,1,0,0,20260101,20261231\n",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    source = tmp_path / "feed.zip"
    source.write_bytes(buffer.getvalue())
    capped = [
        n
        for n in transitio.validate_feed(source, max_notices_per_file=1)["notices"]
        if n["code"] == "notice_limit_reached"
    ]
    assert [n["context"]["suppressedCodes"] for n in capped] == [["new_line_in_value"]]
    result = transitio.repair_feed(source, tmp_path / "out.zip", max_notices_per_file=1)
    assert result["fixes"] == [] and (tmp_path / "out.zip").exists()


def test_validation_without_a_study_day_reports_no_expired_service(tmp_path):
    # Without a reference date, validation and repair judged calendars
    # against the day they ran, so a feed's notices changed from day to day.
    files = {name: text.replace("2026", "2020") for name, text in FEED.items()}
    source = write_zip(tmp_path / "feed.zip", files)

    def expired(notices):
        return [n for n in notices if n["code"] == "expired_calendar"]

    assert expired(validate_feed(source)["notices"]) == []
    repaired = repair_feed(source, tmp_path / "repaired.zip")
    assert expired(repaired["remaining_notices"]) == []
    assert expired(validate_feed(source, reference_date="20210101")["notices"])


def test_shapes_trips_and_built_stops_are_added_in_one_insert(tmp_path, monkeypatch):
    # add_shape, add_trip and build_feed inserted their rows one at a time,
    # each copying the whole table: a 4,000-point shape took 25 s.
    import geopandas as gpd
    from shapely.geometry import LineString

    from transitio.edit import FeedBuilder, build_feed

    inserts = []
    insert_rows = FeedBuilder.insert_rows

    def counting(self, filename, rows, **kwargs):
        rows = list(rows)
        inserts.append((filename, len(rows)))
        return insert_rows(self, filename, rows, **kwargs)

    monkeypatch.setattr(FeedBuilder, "insert_rows", counting)
    builder = FeedBuilder()
    builder.add_shape("s", [(60.0, 24.0), (60.1, 24.1), (60.2, 24.2)])
    builder.add_trip("r", "wk", "t", [("a", 0, 0), ("b", 600, 600), ("c", 1200, 1200)])
    assert inserts == [("shapes.txt", 3), ("trips.txt", 1), ("stop_times.txt", 3)]
    inserts.clear()
    line = LineString([(24.90, 60.16), (24.93, 60.17), (24.96, 60.18)])
    routes = gpd.GeoDataFrame(
        [{"route_id": "r1", "headway_min": 10}], geometry=[line], crs="EPSG:4326"
    )
    build_feed(routes, tmp_path / "built.zip", timezone="Europe/Helsinki", check=False)
    stops = [count for filename, count in inserts if filename == "stops.txt"]
    assert len(stops) == 1 and stops[0] > 2
