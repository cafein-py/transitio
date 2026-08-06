import csv
import hashlib
import json
import zipfile

import pytest

pytest.importorskip("transitio._core")

from transitio.exceptions import PatchError  # noqa: E402
from transitio.gtfs import patch_feed  # noqa: E402

BASE = {
    "agency.txt": (
        "agency_id,agency_name,agency_url,agency_timezone\n"
        "b1,City Transit,https://city.example,Europe/Helsinki\n"
    ),
    "stops.txt": (
        "stop_id,stop_name,stop_lat,stop_lon\n"
        "bs1,Kamppi,60.169,24.931\n"
        "bs2,Steissi,60.171,24.941\n"
    ),
    "routes.txt": "route_id,agency_id,route_short_name,route_type\nbr1,b1,1,3\n",
    "calendar.txt": (
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
        "start_date,end_date\n"
        "wk,1,1,1,1,1,1,1,20260101,20261231\n"
    ),
    "trips.txt": "route_id,service_id,trip_id\nbr1,wk,t1\n",
    # The second arrival precedes the first departure: an ERROR
    # anchored to t1 selects it for replacement.
    "stop_times.txt": (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "t1,08:00:00,08:00:00,bs1,1\n"
        "t1,07:00:00,07:00:00,bs2,2\n"
    ),
}

DONOR = {
    "agency.txt": (
        "agency_id,agency_name,agency_url,agency_timezone\n"
        "d9,City Transit,https://city.example,Europe/Helsinki\n"
    ),
    "stops.txt": (
        "stop_id,stop_name,stop_lat,stop_lon\n"
        "ds1,Kamppi,60.169,24.931\n"
        "ds2,Steissi,60.171,24.941\n"
    ),
    "routes.txt": "route_id,agency_id,route_short_name,route_type\ndr1,d9,1,3\n",
    "calendar.txt": (
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
        "start_date,end_date\n"
        "dk,1,1,1,1,1,1,1,20260101,20261231\n"
    ),
    "trips.txt": "route_id,service_id,trip_id\ndr1,dk,dt1\n",
    "stop_times.txt": (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "dt1,08:00:30,08:00:30,ds1,1\n"
        "dt1,08:05:00,08:05:00,ds2,2\n"
    ),
}


def write_zip(path, files, extra=None):
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
        for name, content in (extra or {}).items():
            archive.writestr(name, content)
    return path


def read_member(path, name):
    with zipfile.ZipFile(path) as archive:
        return archive.read(name).decode()


def trip_ids(path):
    rows = csv.DictReader(read_member(path, "trips.txt").splitlines())
    return {row["trip_id"] for row in rows}


def test_replaces_broken_trip_with_donor(tmp_path):
    base = write_zip(tmp_path / "base.zip", BASE)
    donor = write_zip(tmp_path / "donor.zip", DONOR)
    output = tmp_path / "patched.zip"
    report = patch_feed(base, donor, output)
    (entry,) = [p for p in report["patches"] if p["action"] == "replace_trip"]
    assert entry["tripId"] == "t1"
    assert entry["donorTripId"] == "dt1"
    assert entry["newTripId"] == "donor:dt1"
    assert entry["similarity"] == 1.0
    assert "stop_time_with_arrival_before_previous_departure_time" in (
        entry["triggered_by"]
    )
    assert report["semantic_equivalence"] is False
    assert report["thresholds"]["stopMatchShare"] == 0.8
    assert not any(
        notice["severity"] == "ERROR" for notice in report["remaining_notices"]
    )
    assert trip_ids(output) == {"donor:dt1"}
    # The base file was never touched.
    assert read_member(base, "trips.txt") == BASE["trips.txt"]


def test_no_match_raises_with_report_and_written_output(tmp_path):
    base = write_zip(tmp_path / "base.zip", BASE)
    lonely = dict(
        DONOR,
        **{
            "routes.txt": "route_id,agency_id,route_short_name,route_type\ndr1,d9,99,3\n"
        },
    )
    donor = write_zip(tmp_path / "donor.zip", lonely)
    output = tmp_path / "patched.zip"
    with pytest.raises(PatchError) as excinfo:
        patch_feed(base, donor, output)
    report = excinfo.value.report
    assert [p["action"] for p in report["patches"]] == ["no_donor_match"]
    assert output.exists()
    # check=False returns the same shape instead of raising.
    report = patch_feed(base, donor, tmp_path / "again.zip", check=False)
    assert report["patches"][0]["action"] == "no_donor_match"


def test_unhealthy_donor_is_never_used(tmp_path):
    base = write_zip(tmp_path / "base.zip", BASE)
    sick = dict(
        DONOR,
        **{
            "stop_times.txt": (
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "dt1,08:00:30,08:00:30,ds1,1\n"
                "dt1,07:30:00,07:30:00,ds2,2\n"
            )
        },
    )
    donor = write_zip(tmp_path / "donor.zip", sick)
    report = patch_feed(base, donor, tmp_path / "patched.zip", check=False)
    assert report["patches"][0]["action"] == "no_healthy_donor"


def test_two_base_trips_compete_for_one_donor(tmp_path):
    contested = dict(
        BASE,
        **{
            "trips.txt": "route_id,service_id,trip_id\nbr1,wk,t1\nbr1,wk,t2\n",
            "stop_times.txt": (
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "t1,08:00:00,08:00:00,bs1,1\n"
                "t1,07:00:00,07:00:00,bs2,2\n"
                "t2,08:00:10,08:00:10,bs1,1\n"
                "t2,07:00:00,07:00:00,bs2,2\n"
            ),
        },
    )
    base = write_zip(tmp_path / "base.zip", contested)
    donor = write_zip(tmp_path / "donor.zip", DONOR)
    report = patch_feed(base, donor, tmp_path / "patched.zip", check=False)
    actions = {p["tripId"]: p["action"] for p in report["patches"] if "tripId" in p}
    assert actions["t1"] == "replace_trip"  # sorted order wins
    assert actions["t2"] == "donor_taken"


def test_dependent_rows_dropped_and_logged(tmp_path):
    with_deps = dict(
        BASE,
        **{
            "frequencies.txt": (
                "trip_id,start_time,end_time,headway_secs\nt1,07:00:00,09:00:00,600\n"
            ),
            "transfers.txt": (
                "from_stop_id,to_stop_id,from_trip_id,transfer_type\n" "bs1,bs2,t1,0\n"
            ),
        },
    )
    base = write_zip(tmp_path / "base.zip", with_deps)
    donor = write_zip(tmp_path / "donor.zip", DONOR)
    output = tmp_path / "patched.zip"
    report = patch_feed(base, donor, output)
    dropped = [p for p in report["patches"] if p["action"] == "drop_dependent"]
    assert [d["filename"] for d in dropped] == [
        "frequencies.txt",
        "transfers.txt",
    ]
    with zipfile.ZipFile(output) as archive:
        # Both tables lost their only row; empty files must not survive.
        assert "frequencies.txt" not in archive.namelist()
        assert "transfers.txt" not in archive.namelist()


def test_closure_imports_hierarchy_and_networks(tmp_path):
    deep = dict(
        DONOR,
        **{
            "stops.txt": (
                "stop_id,stop_name,stop_lat,stop_lon,location_type,"
                "parent_station,level_id\n"
                "ds1,Kamppi,60.169,24.931,0,dsp,dl1\n"
                "ds2,Steissi,60.171,24.941,0,,\n"
                "dsp,Kamppi station,60.1691,24.9311,1,,\n"
            ),
            "levels.txt": "level_id,level_index\ndl1,0\n",
            "route_networks.txt": "network_id,route_id\ndnw,dr1\n",
            "networks.txt": "network_id,network_name\ndnw,City network\n",
        },
    )
    base = write_zip(tmp_path / "base.zip", BASE)
    donor = write_zip(tmp_path / "donor.zip", deep)
    output = tmp_path / "patched.zip"
    patch_feed(base, donor, output)
    assert "donor:dsp" in read_member(output, "stops.txt")
    assert "donor:dl1" in read_member(output, "levels.txt")
    assert "donor:dnw" in read_member(output, "networks.txt")
    assert "donor:dr1" in read_member(output, "route_networks.txt")


def test_stale_base_translations_dropped_and_donor_fares_ignored(tmp_path):
    translated = dict(
        BASE,
        **{
            "translations.txt": (
                "table_name,field_name,language,translation,record_id\n"
                "trips,trip_headsign,fi,Keskusta,t1\n"
            )
        },
    )
    faring = dict(
        DONOR,
        **{
            "fare_attributes.txt": (
                "fare_id,price,currency_type,payment_method\nf1,2.80,EUR,0\n"
            ),
            "translations.txt": (
                "table_name,field_name,language,translation,record_id\n"
                "trips,trip_headsign,fi,Lahtoasema,dt1\n"
            ),
        },
    )
    base = write_zip(tmp_path / "base.zip", translated)
    donor = write_zip(tmp_path / "donor.zip", faring)
    output = tmp_path / "patched.zip"
    report = patch_feed(base, donor, output)
    dropped = [p for p in report["patches"] if p["action"] == "drop_dependent"]
    assert any(d["filename"] == "translations.txt" for d in dropped)
    with zipfile.ZipFile(output) as archive:
        # Donor fares and translations never travel with the closure,
        # and the base's only translation targeted the replaced trip.
        assert "fare_attributes.txt" not in archive.namelist()
        assert "translations.txt" not in archive.namelist()


def test_refusals(tmp_path):
    base = write_zip(tmp_path / "base.zip", BASE)
    # Same-path output.
    donor = write_zip(tmp_path / "donor.zip", DONOR)
    with pytest.raises(ValueError):
        patch_feed(base, donor, base)
    # Timezone conflict.
    tallinn = dict(
        DONOR,
        **{
            "agency.txt": (
                "agency_id,agency_name,agency_url,agency_timezone\n"
                "d9,City Transit,https://city.example,Europe/Tallinn\n"
            )
        },
    )
    conflicting = write_zip(tmp_path / "tallinn.zip", tallinn)
    with pytest.raises(ValueError):
        patch_feed(base, conflicting, tmp_path / "out1.zip")
    # Flex donor.
    flex = write_zip(tmp_path / "flex.zip", DONOR, extra={"locations.geojson": "{}"})
    with pytest.raises(ValueError):
        patch_feed(base, flex, tmp_path / "out2.zip")


def test_reliability_refusal_beats_check_false(tmp_path):
    sampled = dict(
        BASE,
        **{
            "stops.txt": (
                "stop_id,stop_name,stop_lat,stop_lon\n"
                "bs1, Kamppi,60.169,24.931\n"
                "bs2, Steissi,60.171,24.941\n"
            )
        },
    )
    base = write_zip(tmp_path / "base.zip", sampled)
    donor = write_zip(tmp_path / "donor.zip", DONOR)
    with pytest.raises(PatchError, match="sampled or truncated"):
        patch_feed(
            base, donor, tmp_path / "out.zip", check=False, max_notices_per_file=1
        )


def test_when_window_enforced_and_moment_reported(tmp_path):
    base = write_zip(tmp_path / "base.zip", BASE)
    donor = write_zip(tmp_path / "donor.zip", DONOR)
    report = patch_feed(base, donor, tmp_path / "out.zip", when="20260601")
    assert report["moment"] is not None
    assert report["moment"]["referenceDate"] == "20260601"
    with pytest.raises(PatchError, match="does not cover"):
        patch_feed(base, donor, tmp_path / "out2.zip", when="20270601")


def test_donor_provenance_sidecar_verified(tmp_path):
    base = write_zip(tmp_path / "base.zip", BASE)
    donor = write_zip(tmp_path / "donor.zip", DONOR)
    digest = hashlib.sha256(donor.read_bytes()).hexdigest()
    sidecar = donor.with_suffix(".provenance.json")
    sidecar.write_text(
        json.dumps({"sha256": digest, "feed_id": "mdb-1", "dataset_id": "mdb-1-x"})
    )
    report = patch_feed(base, donor, tmp_path / "out.zip")
    assert report["donor"] == {
        "sha256": digest,
        "feed_id": "mdb-1",
        "dataset_id": "mdb-1-x",
    }
    # A stale sidecar is rejected: computed hash only.
    sidecar.write_text(json.dumps({"sha256": "0" * 64, "feed_id": "mdb-1"}))
    report = patch_feed(base, donor, tmp_path / "out2.zip")
    assert report["donor"]["sha256"] == digest
    assert report["donor"]["catalog"] == "unavailable"
    assert "feed_id" not in report["donor"]


def test_unpairable_duplicate_agency_names(tmp_path):
    twins = dict(
        BASE,
        **{
            "agency.txt": (
                "agency_id,agency_name,agency_url,agency_timezone\n"
                "b1,City Transit,https://city.example,Europe/Helsinki\n"
                "b2,City Transit,https://city2.example,Europe/Helsinki\n"
            )
        },
    )
    base = write_zip(tmp_path / "base.zip", twins)
    donor = write_zip(tmp_path / "donor.zip", DONOR)
    report = patch_feed(base, donor, tmp_path / "out.zip", check=False)
    actions = [p["action"] for p in report["patches"]]
    assert "unpairable_agency" in actions
    assert "replace_trip" not in actions


def test_prefix_collision_picks_numbered_prefix(tmp_path):
    occupied = dict(
        BASE,
        **{
            "stops.txt": BASE["stops.txt"] + "donor:bs9,Elsewhere,60.2,24.9\n",
        },
    )
    base = write_zip(tmp_path / "base.zip", occupied)
    donor = write_zip(tmp_path / "donor.zip", DONOR)
    report = patch_feed(base, donor, tmp_path / "out.zip")
    (entry,) = [p for p in report["patches"] if p["action"] == "replace_trip"]
    assert entry["newTripId"] == "donor2:dt1"


def test_similarity_below_threshold_is_no_match(tmp_path):
    stops = (
        "stop_id,stop_name,stop_lat,stop_lon\n"
        "%s1,Kamppi,60.169,24.931\n"
        "%s2,Steissi,60.171,24.941\n"
        "%s3,Hakaniemi,60.179,24.951\n"
        "%s4,Sornainen,60.187,24.961\n"
        "%s5,Kalasatama,60.185,24.975\n"
    )
    long_base = dict(
        BASE,
        **{
            "stops.txt": stops.replace("%s", "bs"),
            "stop_times.txt": (
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "t1,08:00:00,08:00:00,bs1,1\n"
                "t1,07:00:00,07:00:00,bs2,2\n"
                "t1,08:04:00,08:04:00,bs3,3\n"
                "t1,08:06:00,08:06:00,bs4,4\n"
                "t1,08:08:00,08:08:00,bs5,5\n"
            ),
        },
    )
    short_donor = dict(
        DONOR,
        **{
            "stops.txt": stops.replace("%s", "ds"),
            "stop_times.txt": (
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "dt1,08:00:30,08:00:30,ds1,1\n"
                "dt1,08:02:00,08:02:00,ds2,2\n"
                "dt1,08:04:00,08:04:00,ds3,3\n"
            ),
        },
    )
    base = write_zip(tmp_path / "base.zip", long_base)
    donor = write_zip(tmp_path / "donor.zip", short_donor)
    # 3 of 5 stops shared: similarity 0.6 sits under the 0.8 floor.
    report = patch_feed(base, donor, tmp_path / "out.zip", check=False)
    assert report["patches"][0]["action"] == "no_donor_match"


def test_shared_stop_ids_do_not_imply_a_match(tmp_path):
    imposter = dict(
        DONOR,
        **{
            "stops.txt": (
                "stop_id,stop_name,stop_lat,stop_lon\n"
                "bs1,Erottaja,61.169,25.931\n"
                "bs2,Kauppatori,61.171,25.941\n"
            ),
            "stop_times.txt": (
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "dt1,08:00:30,08:00:30,bs1,1\n"
                "dt1,08:05:00,08:05:00,bs2,2\n"
            ),
        },
    )
    base = write_zip(tmp_path / "base.zip", BASE)
    donor = write_zip(tmp_path / "donor.zip", imposter)
    report = patch_feed(base, donor, tmp_path / "out.zip", check=False)
    assert report["patches"][0]["action"] == "no_donor_match"


def test_blank_agency_ids_backfilled_on_both_sides(tmp_path):
    bare_base = dict(
        BASE,
        **{
            "agency.txt": (
                "agency_name,agency_url,agency_timezone\n"
                "City Transit,https://city.example,Europe/Helsinki\n"
            ),
            "routes.txt": "route_id,route_short_name,route_type\nbr1,1,3\n",
        },
    )
    bare_donor = dict(
        DONOR,
        **{
            "agency.txt": (
                "agency_name,agency_url,agency_timezone\n"
                "City Transit,https://city.example,Europe/Helsinki\n"
            ),
            "routes.txt": "route_id,route_short_name,route_type\ndr1,1,3\n",
        },
    )
    base = write_zip(tmp_path / "base.zip", bare_base)
    donor = write_zip(tmp_path / "donor.zip", bare_donor)
    output = tmp_path / "out.zip"
    report = patch_feed(base, donor, output)
    assert any(p["action"] == "replace_trip" for p in report["patches"])
    agencies = read_member(output, "agency.txt")
    assert "agency_id" in agencies.splitlines()[0]


def test_reliability_refusal_covers_donor_and_final_stages(tmp_path):
    base = write_zip(tmp_path / "base.zip", BASE)
    padded = dict(
        DONOR,
        **{
            "stops.txt": (
                "stop_id,stop_name,stop_lat,stop_lon\n"
                "ds1, Kamppi,60.169,24.931\n"
                "ds2, Steissi,60.171,24.941\n"
            )
        },
    )
    donor = write_zip(tmp_path / "donor.zip", padded)
    with pytest.raises(PatchError, match="donor validation"):
        patch_feed(
            base, donor, tmp_path / "out.zip", check=False, max_notices_per_file=1
        )
    # Final-stage truncation: each input fits max_rows, but the merged
    # stops table (base + imported donor stops) crosses it.
    clean_donor = write_zip(tmp_path / "donor2.zip", DONOR)
    with pytest.raises(PatchError, match="patched output"):
        patch_feed(base, clean_donor, tmp_path / "out2.zip", check=False, max_rows=3)


def test_closure_dedup_frequencies_and_associative_filter(tmp_path):
    contested = dict(
        BASE,
        **{
            "trips.txt": "route_id,service_id,trip_id\nbr1,wk,t1\nbr1,wk,t2\n",
            "stop_times.txt": (
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "t1,08:00:00,08:00:00,bs1,1\n"
                "t1,07:00:00,07:00:00,bs2,2\n"
                "t2,08:00:10,08:00:10,bs1,1\n"
                "t2,07:00:00,07:00:00,bs2,2\n"
            ),
        },
    )
    rich_donor = dict(
        DONOR,
        **{
            "stops.txt": (
                "stop_id,stop_name,stop_lat,stop_lon\n"
                "ds1,Kamppi,60.169,24.931\n"
                "ds2,Steissi,60.171,24.941\n"
                "ds9,Ulkopuoli,60.2,24.99\n"
            ),
            "trips.txt": "route_id,service_id,trip_id\ndr1,dk,dt1\ndr1,dk,dt2\n",
            "stop_times.txt": (
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "dt1,08:00:30,08:00:30,ds1,1\n"
                "dt1,08:05:00,08:05:00,ds2,2\n"
                "dt2,08:00:40,08:00:40,ds1,1\n"
                "dt2,08:05:10,08:05:10,ds2,2\n"
            ),
            "frequencies.txt": (
                "trip_id,start_time,end_time,headway_secs\n"
                "dt1,09:00:00,10:00:00,600\n"
            ),
            "routes.txt": (
                "route_id,agency_id,route_short_name,route_type\n"
                "dr1,d9,1,3\n"
                "dr9,d9,9,3\n"
            ),
            "transfers.txt": (
                "from_stop_id,to_stop_id,from_route_id,to_route_id,transfer_type\n"
                "ds1,ds2,dr1,dr1,0\n"
                "ds1,ds9,dr1,dr1,0\n"
                "ds1,ds2,dr1,dr9,0\n"
            ),
        },
    )
    base = write_zip(tmp_path / "base.zip", contested)
    donor = write_zip(tmp_path / "donor.zip", rich_donor)
    output = tmp_path / "out.zip"
    report = patch_feed(base, donor, output)
    replaced = {
        p["tripId"]: p["donorTripId"]
        for p in report["patches"]
        if p["action"] == "replace_trip"
    }
    assert replaced == {"t1": "dt1", "t2": "dt2"}
    stops = read_member(output, "stops.txt")
    # Shared closure stops are imported once, and only closure members.
    assert stops.count("donor:ds1,") == 1
    assert "ds9" not in stops
    assert "donor:dt1" in read_member(output, "frequencies.txt")
    transfers = read_member(output, "transfers.txt")
    assert "donor:ds1,donor:ds2,donor:dr1,donor:dr1" in transfers
    # Rows referencing a stop or a route outside the closure stay out.
    assert "ds9" not in transfers
    assert "dr9" not in transfers


FIVE_STOPS = (
    "stop_id,stop_name,stop_lat,stop_lon\n"
    "%s1,Kamppi,60.169,24.931\n"
    "%s2,Steissi,60.171,24.941\n"
    "%s3,Hakaniemi,60.179,24.951\n"
    "%s4,Sornainen,60.187,24.961\n"
    "%s5,Kalasatama,60.185,24.975\n"
)

LONG_BROKEN_BASE = {
    **BASE,
    "stops.txt": FIVE_STOPS.replace("%s", "bs"),
    "stop_times.txt": (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "t1,08:00:00,08:00:00,bs1,1\n"
        "t1,07:00:00,07:00:00,bs2,2\n"
        "t1,08:04:00,08:04:00,bs3,3\n"
        "t1,08:06:00,08:06:00,bs4,4\n"
        "t1,08:08:00,08:08:00,bs5,5\n"
    ),
}


def test_similarity_accepted_exactly_at_the_boundary(tmp_path):
    four_of_five = dict(
        DONOR,
        **{
            "stops.txt": FIVE_STOPS.replace("%s", "ds"),
            "stop_times.txt": (
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "dt1,08:00:30,08:00:30,ds1,1\n"
                "dt1,08:02:00,08:02:00,ds2,2\n"
                "dt1,08:04:00,08:04:00,ds3,3\n"
                "dt1,08:06:00,08:06:00,ds4,4\n"
            ),
        },
    )
    base = write_zip(tmp_path / "base.zip", LONG_BROKEN_BASE)
    donor = write_zip(tmp_path / "donor.zip", four_of_five)
    report = patch_feed(base, donor, tmp_path / "out.zip")
    (entry,) = [p for p in report["patches"] if p["action"] == "replace_trip"]
    # 4 shared stops over max(5, 4): exactly the 0.8 floor.
    assert entry["similarity"] == 0.8


def test_duplicate_donor_routes_pool_and_rank_by_similarity(tmp_path):
    twin_routes = dict(
        DONOR,
        **{
            "stops.txt": FIVE_STOPS.replace("%s", "ds"),
            "routes.txt": (
                "route_id,agency_id,route_short_name,route_type\n"
                "dr1,d9,1,3\n"
                "dr2,d9,1,3\n"
            ),
            "trips.txt": "route_id,service_id,trip_id\ndr1,dk,dtA\ndr2,dk,dtB\n",
            "stop_times.txt": (
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "dtA,08:00:30,08:00:30,ds1,1\n"
                "dtA,08:02:00,08:02:00,ds2,2\n"
                "dtA,08:04:00,08:04:00,ds3,3\n"
                "dtA,08:06:00,08:06:00,ds4,4\n"
                "dtB,08:00:20,08:00:20,ds1,1\n"
                "dtB,08:02:00,08:02:00,ds2,2\n"
                "dtB,08:04:00,08:04:00,ds3,3\n"
                "dtB,08:06:00,08:06:00,ds4,4\n"
                "dtB,08:08:00,08:08:00,ds5,5\n"
            ),
        },
    )
    base = write_zip(tmp_path / "base.zip", LONG_BROKEN_BASE)
    donor = write_zip(tmp_path / "donor.zip", twin_routes)
    report = patch_feed(base, donor, tmp_path / "out.zip")
    (entry,) = [p for p in report["patches"] if p["action"] == "replace_trip"]
    # Trips of both same-name routes pool; dtB's 1.0 beats dtA's 0.8.
    assert entry["donorTripId"] == "dtB"
    assert entry["similarity"] == 1.0


def test_failed_patch_report_keeps_moment(tmp_path):
    base = write_zip(tmp_path / "base.zip", BASE)
    other_route = dict(
        DONOR,
        **{
            "routes.txt": (
                "route_id,agency_id,route_short_name,route_type\ndr1,d9,99,3\n"
            )
        },
    )
    donor = write_zip(tmp_path / "donor.zip", other_route)
    with pytest.raises(PatchError) as excinfo:
        patch_feed(base, donor, tmp_path / "out.zip", when="20260601")
    moment = excinfo.value.report["moment"]
    assert moment is not None
    assert moment["referenceDate"] == "20260601"


def test_scalar_or_list_sidecar_is_ignored(tmp_path):
    base = write_zip(tmp_path / "base.zip", BASE)
    donor = write_zip(tmp_path / "donor.zip", DONOR)
    sidecar = donor.with_suffix(".provenance.json")
    for payload in ("42", '["not", "a", "mapping"]'):
        sidecar.write_text(payload)
        report = patch_feed(base, donor, tmp_path / "out.zip", check=False)
        assert report["donor"]["catalog"] == "unavailable"


def test_conflicting_reference_date_rejected(tmp_path):
    base = write_zip(tmp_path / "base.zip", BASE)
    donor = write_zip(tmp_path / "donor.zip", DONOR)
    with pytest.raises(ValueError, match="disagree"):
        patch_feed(
            base,
            donor,
            tmp_path / "out.zip",
            when="20260601",
            reference_date="20260701",
        )


def test_both_catalog_sidecar_shapes_accepted(tmp_path):
    base = write_zip(tmp_path / "base.zip", BASE)
    donor = write_zip(tmp_path / "donor.zip", DONOR)
    digest = hashlib.sha256(donor.read_bytes()).hexdigest()
    sidecar = donor.with_suffix(".provenance.json")
    # The dataset-download shape carries a dataset id.
    sidecar.write_text(
        json.dumps(
            {
                "feed_id": "mdb-1",
                "dataset_id": "mdb-1-x",
                "source_url": "https://host.example/feed.zip",
                "sha256": digest,
                "service_date_range": ["2026-01-01", "2026-12-31"],
                "retrieved_at": "2026-08-01T00:00:00+00:00",
            }
        )
    )
    report = patch_feed(base, donor, tmp_path / "out1.zip")
    assert report["donor"]["feed_id"] == "mdb-1"
    assert report["donor"]["dataset_id"] == "mdb-1-x"
    assert "catalog" not in report["donor"]
    # The latest-download shape has no dataset id.
    sidecar.write_text(
        json.dumps(
            {
                "feed_id": "mdb-1",
                "source_url": "https://host.example/latest.zip",
                "sha256": digest,
                "retrieved_at": "2026-08-01T00:00:00+00:00",
            }
        )
    )
    report = patch_feed(base, donor, tmp_path / "out2.zip")
    assert report["donor"]["feed_id"] == "mdb-1"
    assert "dataset_id" not in report["donor"]
    assert "catalog" not in report["donor"]


def test_symlinked_or_oversized_sidecar_is_ignored(tmp_path):
    base = write_zip(tmp_path / "base.zip", BASE)
    donor = write_zip(tmp_path / "donor.zip", DONOR)
    digest = hashlib.sha256(donor.read_bytes()).hexdigest()
    payload = json.dumps({"sha256": digest, "feed_id": "mdb-1"})
    sidecar = donor.with_suffix(".provenance.json")
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(payload)
    sidecar.symlink_to(elsewhere)
    report = patch_feed(base, donor, tmp_path / "out1.zip")
    assert report["donor"]["catalog"] == "unavailable"
    sidecar.unlink()
    sidecar.write_text(payload + " " * (2 << 20))
    report = patch_feed(base, donor, tmp_path / "out2.zip")
    assert report["donor"]["catalog"] == "unavailable"


def test_lcs_budget_refusal_is_a_caveat_not_a_verdict(tmp_path, monkeypatch):
    import transitio.gtfs._patch as patch_module

    monkeypatch.setattr(patch_module, "_LCS_CELL_BUDGET", 3)
    base = write_zip(tmp_path / "base.zip", BASE)
    donor = write_zip(tmp_path / "donor.zip", DONOR)
    report = patch_feed(base, donor, tmp_path / "out.zip", check=False)
    entry = report["patches"][0]
    assert entry["action"] == "no_donor_match"
    assert entry["lcsBudgetExhaustedAt"] == "dt1"
    # The echo states the value that was actually applied.
    assert report["thresholds"]["lcsCellBudget"] == 3


def test_stop_match_distance_thresholds():
    from transitio.gtfs._patch import _stops_match

    # One degree of latitude spans ~111,195 m on the editor's sphere,
    # so these offsets sit just inside and outside the two radii.
    meters = 1.0 / 111194.93
    here = (60.170, 24.940)

    def shifted(distance_m):
        return (here[0] + distance_m * meters, here[1])

    named = "kamppi"
    assert _stops_match((named, here), (named, shifted(95))) is True
    assert _stops_match((named, here), (named, shifted(105))) is False
    assert _stops_match(("", here), ("", shifted(20))) is True
    assert _stops_match(("", here), ("", shifted(30))) is False
    # One blank name: the tighter proximity-only rule applies.
    assert _stops_match((named, here), ("", shifted(20))) is True
    assert _stops_match((named, here), ("", shifted(30))) is False


def test_whitespace_bearing_donor_ids_survive_the_closure(tmp_path):
    spaced = dict(
        DONOR,
        **{
            "routes.txt": (
                "route_id,agency_id,route_short_name,route_type\ndr1 ,d9,1,3\n"
            ),
            "calendar.txt": (
                "service_id,monday,tuesday,wednesday,thursday,friday,saturday,"
                "sunday,start_date,end_date\n"
                "dk ,1,1,1,1,1,1,1,20260101,20261231\n"
            ),
            "trips.txt": "route_id,service_id,trip_id\ndr1 ,dk ,dt1\n",
            "route_networks.txt": "network_id,route_id\ndnw,dr1 \n",
            "networks.txt": "network_id,network_name\ndnw,City network\n",
        },
    )
    base = write_zip(tmp_path / "base.zip", BASE)
    donor = write_zip(tmp_path / "donor.zip", spaced)
    output = tmp_path / "out.zip"
    # Ids are opaque: internally consistent whitespace must neither
    # break the closure nor the revalidation of the patched output.
    report = patch_feed(base, donor, output)
    assert any(p["action"] == "replace_trip" for p in report["patches"])
    assert "donor:dr1 " in read_member(output, "routes.txt")
    # The associative row references the same whitespace-bearing id.
    assert "donor:dr1 " in read_member(output, "route_networks.txt")


def test_malformed_base_departure_is_never_normalised(tmp_path):
    bogus = dict(
        BASE,
        **{
            "stops.txt": (
                "stop_id,stop_name,stop_lat,stop_lon\n"
                "bs1,Kamppi,60.169,24.931\n"
                "bs2,Steissi,60.171,24.941\n"
                "bs3,Hakaniemi,60.179,24.951\n"
            ),
            "stop_times.txt": (
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "t1,07:60:20,07:60:20,bs1,1\n"
                "t1,08:05:00,08:05:00,bs2,2\n"
                "t1,08:02:00,08:02:00,bs3,3\n"
            ),
        },
    )
    near_miss = dict(
        DONOR,
        **{
            "stops.txt": (
                "stop_id,stop_name,stop_lat,stop_lon\n"
                "ds1,Kamppi,60.169,24.931\n"
                "ds2,Steissi,60.171,24.941\n"
                "ds3,Hakaniemi,60.179,24.951\n"
            ),
            "stop_times.txt": (
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "dt1,07:59:50,07:59:50,ds1,1\n"
                "dt1,08:05:00,08:05:00,ds2,2\n"
                "dt1,08:07:00,08:07:00,ds3,3\n"
            ),
        },
    )
    base = write_zip(tmp_path / "base.zip", bogus)
    donor = write_zip(tmp_path / "donor.zip", near_miss)
    # Read leniently, 07:60:20 lands 30 s from the donor's departure
    # and the trips would match; strictly, it is simply unavailable.
    report = patch_feed(base, donor, tmp_path / "out.zip", check=False)
    assert [p["action"] for p in report["patches"]] == ["no_donor_match"]


def test_final_reliability_refusal_carries_the_report(tmp_path):
    base = write_zip(tmp_path / "base.zip", BASE)
    donor = write_zip(tmp_path / "donor.zip", DONOR)
    with pytest.raises(PatchError) as excinfo:
        patch_feed(base, donor, tmp_path / "out.zip", check=False, max_rows=3)
    report = getattr(excinfo.value, "report", None)
    assert report is not None
    assert any(p["action"] == "replace_trip" for p in report["patches"])


def test_aggregate_lcs_budget_stops_before_partial_evidence(tmp_path, monkeypatch):
    import transitio.gtfs._patch as patch_module

    # Each candidate fits the per-candidate cap; together they do not.
    monkeypatch.setattr(patch_module, "_LCS_CELL_BUDGET", 4)
    monkeypatch.setattr(patch_module, "_LCS_TOTAL_CELL_BUDGET", 4)
    contested = dict(
        BASE,
        **{
            "trips.txt": "route_id,service_id,trip_id\nbr1,wk,t1\nbr1,wk,t2\n",
            "stop_times.txt": (
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "t1,08:00:00,08:00:00,bs1,1\n"
                "t1,07:00:00,07:00:00,bs2,2\n"
                "t2,08:00:10,08:00:10,bs1,1\n"
                "t2,07:00:00,07:00:00,bs2,2\n"
            ),
        },
    )
    base = write_zip(tmp_path / "base.zip", contested)
    donor = write_zip(tmp_path / "donor.zip", DONOR)
    report = patch_feed(base, donor, tmp_path / "out.zip", check=False)
    actions = {p["tripId"]: p for p in report["patches"] if "tripId" in p}
    assert actions["t1"]["action"] == "replace_trip"
    assert actions["t2"]["action"] == "no_donor_match"
    assert actions["t2"]["lcsBudgetExhaustedAt"] == "dt1"
    assert report["thresholds"]["lcsTotalCellBudget"] == 4


def test_equal_similarity_ties_break_on_donor_trip_id(tmp_path):
    twins = dict(
        DONOR,
        **{
            # Listed with the lexicographically larger id first, so a
            # stable tie-break cannot come from insertion order.
            "trips.txt": "route_id,service_id,trip_id\ndr1,dk,dtZ\ndr1,dk,dtA\n",
            "stop_times.txt": (
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "dtZ,08:00:30,08:00:30,ds1,1\n"
                "dtZ,08:05:00,08:05:00,ds2,2\n"
                "dtA,08:00:30,08:00:30,ds1,1\n"
                "dtA,08:05:00,08:05:00,ds2,2\n"
            ),
        },
    )
    base = write_zip(tmp_path / "base.zip", BASE)
    donor = write_zip(tmp_path / "donor.zip", twins)
    report = patch_feed(base, donor, tmp_path / "out.zip")
    (entry,) = [p for p in report["patches"] if p["action"] == "replace_trip"]
    assert entry["similarity"] == 1.0
    assert entry["donorTripId"] == "dtA"


def test_unicode_digit_times_are_rejected(tmp_path):
    # Devanagari digits: int() would accept them, GTFS does not.
    exotic = dict(
        BASE,
        **{
            "stops.txt": (
                "stop_id,stop_name,stop_lat,stop_lon\n"
                "bs1,Kamppi,60.169,24.931\n"
                "bs2,Steissi,60.171,24.941\n"
                "bs3,Hakaniemi,60.179,24.951\n"
            ),
            "stop_times.txt": (
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "t1,\u0966\u096d:\u0965\u0969:\u0968\u0966,"
                "\u0966\u096d:\u0965\u0969:\u0968\u0966,bs1,1\n"
                "t1,08:05:00,08:05:00,bs2,2\n"
                "t1,08:02:00,08:02:00,bs3,3\n"
            ),
        },
    )
    near_miss = dict(
        DONOR,
        **{
            "stops.txt": (
                "stop_id,stop_name,stop_lat,stop_lon\n"
                "ds1,Kamppi,60.169,24.931\n"
                "ds2,Steissi,60.171,24.941\n"
                "ds3,Hakaniemi,60.179,24.951\n"
            ),
            "stop_times.txt": (
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "dt1,07:59:50,07:59:50,ds1,1\n"
                "dt1,08:05:00,08:05:00,ds2,2\n"
                "dt1,08:07:00,08:07:00,ds3,3\n"
            ),
        },
    )
    base = write_zip(tmp_path / "base.zip", exotic)
    donor = write_zip(tmp_path / "donor.zip", near_miss)
    report = patch_feed(base, donor, tmp_path / "out.zip", check=False)
    assert [p["action"] for p in report["patches"]] == ["no_donor_match"]


FIVE_STOP_DONOR = {
    **DONOR,
    "stops.txt": (
        "stop_id,stop_name,stop_lat,stop_lon\n"
        "ds1,Kamppi,60.169,24.931\n"
        "ds2,Steissi,60.171,24.941\n"
        "ds3,Hakaniemi,60.179,24.951\n"
        "ds4,Sornainen,60.187,24.961\n"
        "ds5,Kalasatama,60.185,24.975\n"
    ),
    "stop_times.txt": (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "dt1,08:00:30,08:00:30,ds1,1\n"
        "dt1,08:02:00,08:02:00,ds2,2\n"
        "dt1,08:04:00,08:04:00,ds3,3\n"
        "dt1,08:06:00,08:06:00,ds4,4\n"
        "dt1,08:08:00,08:08:00,ds5,5\n"
    ),
}


def test_unorderable_stop_sequence_makes_a_trip_unmatchable(tmp_path):
    # Only two of the base trip's five stops match the donor. Dropping
    # the three unparseable rows would leave 2/2 — a false 1.0.
    ragged = dict(
        BASE,
        **{
            "stops.txt": (
                "stop_id,stop_name,stop_lat,stop_lon\n"
                "bs1,Kamppi,60.169,24.931\n"
                "bs2,Steissi,60.171,24.941\n"
                "bs3,Munkkiniemi,60.199,24.871\n"
                "bs4,Otaniemi,60.185,24.829\n"
                "bs5,Tapiola,60.176,24.804\n"
            ),
            "stop_times.txt": (
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "t1,08:00:00,08:00:00,bs1,1\n"
                "t1,07:00:00,07:00:00,bs2,2\n"
                "t1,08:04:00,08:04:00,bs3,x\n"
                "t1,08:06:00,08:06:00,bs4,y\n"
                "t1,08:08:00,08:08:00,bs5,z\n"
            ),
        },
    )
    base = write_zip(tmp_path / "base.zip", ragged)
    donor = write_zip(tmp_path / "donor.zip", FIVE_STOP_DONOR)
    report = patch_feed(base, donor, tmp_path / "out.zip", check=False)
    assert [p["action"] for p in report["patches"]] == ["no_donor_match"]


def test_duplicate_stop_sequence_makes_a_trip_unmatchable(tmp_path):
    duped = dict(
        BASE,
        **{
            "stop_times.txt": (
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "t1,08:00:00,08:00:00,bs1,1\n"
                "t1,07:00:00,07:00:00,bs2,1\n"
            )
        },
    )
    base = write_zip(tmp_path / "base.zip", duped)
    donor = write_zip(tmp_path / "donor.zip", DONOR)
    report = patch_feed(base, donor, tmp_path / "out.zip", check=False)
    assert [p["action"] for p in report["patches"]] == ["no_donor_match"]


def test_first_departure_never_falls_through_to_a_later_stop(tmp_path):
    # The first stop has no times at all; the second sits 10 s from the
    # donor's departure and must not stand in for the trip's start.
    headless = dict(
        BASE,
        **{
            "stop_times.txt": (
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "t1,,,bs1,1\n"
                "t1,08:00:40,08:00:40,bs2,2\n"
            )
        },
    )
    base = write_zip(tmp_path / "base.zip", headless)
    donor = write_zip(tmp_path / "donor.zip", DONOR)
    report = patch_feed(base, donor, tmp_path / "out.zip", check=False)
    assert [p["action"] for p in report["patches"]] == ["no_donor_match"]


def test_arrival_time_does_not_stand_in_for_departure(tmp_path):
    # The first stop states an arrival 10 s from the donor's departure
    # but no departure of its own; dwell is unknown, so no match.
    arrival_only = dict(
        BASE,
        **{
            "stop_times.txt": (
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "t1,08:00:40,,bs1,1\n"
                "t1,07:00:00,07:00:00,bs2,2\n"
            )
        },
    )
    base = write_zip(tmp_path / "base.zip", arrival_only)
    donor = write_zip(tmp_path / "donor.zip", DONOR)
    report = patch_feed(base, donor, tmp_path / "out.zip", check=False)
    assert [p["action"] for p in report["patches"]] == ["no_donor_match"]


@pytest.mark.parametrize("side", ["base", "donor"])
def test_oversized_stop_sequence_does_not_crash(tmp_path, side):
    huge = "9" * 5000
    feeds = {"base": dict(BASE), "donor": dict(DONOR)}
    if side == "base":
        feeds["base"]["stop_times.txt"] = (
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "t1,08:00:00,08:00:00,bs1,1\n"
            f"t1,07:00:00,07:00:00,bs2,{huge}\n"
        )
    else:
        feeds["donor"]["stop_times.txt"] = (
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "dt1,08:00:30,08:00:30,ds1,1\n"
            f"dt1,08:05:00,08:05:00,ds2,{huge}\n"
        )
    base = write_zip(tmp_path / "base.zip", feeds["base"])
    donor = write_zip(tmp_path / "donor.zip", feeds["donor"])
    report = patch_feed(base, donor, tmp_path / "out.zip", check=False)
    assert [p["action"] for p in report["patches"]] == ["no_donor_match"]


@pytest.mark.parametrize("bad_time", ["08:0:00", "08:000:00", "0008:00:00"])
def test_time_component_widths_are_enforced(bad_time):
    from transitio.gtfs._patch import _seconds

    assert _seconds(bad_time) is None
    assert _seconds("08:00:00") == 28800
    assert _seconds("25:00:00") == 90000  # after-midnight service is valid


def test_sequentially_occupied_prefixes_pick_the_first_free_one(tmp_path):
    occupied = dict(
        BASE,
        **{
            # donor plus donor2..donor11 are all in use already.
            "stops.txt": BASE["stops.txt"]
            + "donor:bs0,Elsewhere,60.2,24.9\n"
            + "".join(f"donor{n}:bs{n},Elsewhere,60.2,24.9\n" for n in range(2, 12)),
        },
    )
    base = write_zip(tmp_path / "base.zip", occupied)
    donor = write_zip(tmp_path / "donor.zip", DONOR)
    report = patch_feed(base, donor, tmp_path / "out.zip")
    (entry,) = [p for p in report["patches"] if p["action"] == "replace_trip"]
    assert entry["newTripId"] == "donor12:dt1"
