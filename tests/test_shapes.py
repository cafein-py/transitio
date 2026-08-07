"""Inferring shapes into a feed: levels, validation, what gets written."""

import io
import zipfile

import numpy as np
import pandas as pd
import pytest

from transitio.shapes import LEVELS, PERMISSIVE, STRICT, infer_shapes
from transitio.shapes import _levels


def read_table(feed, name, coerce_na=True):
    """A table from a written feed.

    ``coerce_na=False`` reads literally, which is what a test of value
    preservation needs: pandas' default NA parsing would itself turn a
    legal ``NA``/``N/A`` id into a missing value.
    """
    with zipfile.ZipFile(feed) as archive:
        if name not in archive.namelist():
            return None
        options = {} if coerce_na else {"keep_default_na": False, "na_values": []}
        return pd.read_csv(io.BytesIO(archive.read(name)), dtype=str, **options)


def tram_feed(source, destination, *, keep_shape_refs=False, trips_per_route=40):
    """A small shapeless tram-only feed cut from the Helsinki fixture."""
    with zipfile.ZipFile(source) as archive:
        routes = pd.read_csv(io.BytesIO(archive.read("routes.txt")), dtype=str)
        trips = pd.read_csv(io.BytesIO(archive.read("trips.txt")), dtype=str)
        stop_times = pd.read_csv(io.BytesIO(archive.read("stop_times.txt")), dtype=str)
        stops = archive.read("stops.txt")
        agency = archive.read("agency.txt")
    tram_routes = routes[routes["route_type"] == "0"]
    trips = trips[trips["route_id"].isin(tram_routes["route_id"])]
    trips = trips.groupby("route_id", sort=False).head(trips_per_route)
    stop_times = stop_times[stop_times["trip_id"].isin(trips["trip_id"])]
    stop_times = stop_times.drop(
        columns=[c for c in ("shape_dist_traveled",) if c in stop_times]
    )
    if not keep_shape_refs:
        trips = trips.drop(columns=[c for c in ("shape_id",) if c in trips])
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as out:
        out.writestr("agency.txt", agency)
        out.writestr("stops.txt", stops)
        out.writestr("routes.txt", tram_routes.to_csv(index=False))
        out.writestr("trips.txt", trips.to_csv(index=False))
        out.writestr("stop_times.txt", stop_times.to_csv(index=False))
    return destination


@pytest.fixture(scope="module")
def shapeless(helsinki_gtfs, tmp_path_factory):
    directory = tmp_path_factory.mktemp("shapes")
    return tram_feed(helsinki_gtfs, directory / "shapeless.zip")


@pytest.fixture(scope="module")
def strict_run(shapeless, transit_pbf, tmp_path_factory):
    output = tmp_path_factory.mktemp("strict") / "shaped.zip"
    report = infer_shapes(
        shapeless, output, transit_pbf, strictness="strict", modes=["tram"]
    )
    return output, report


def test_unknown_strictness_rejects(shapeless, transit_pbf, tmp_path):
    with pytest.raises(ValueError, match="unknown strictness"):
        infer_shapes(shapeless, tmp_path / "x.zip", transit_pbf, strictness="loose")


def test_unknown_mode_rejects(shapeless, transit_pbf, tmp_path):
    with pytest.raises(ValueError, match="segway"):
        infer_shapes(shapeless, tmp_path / "x.zip", transit_pbf, modes=["segway"])


def test_writes_a_usable_shapes_table(strict_run):
    output, report = strict_run
    assert report["level"] == "strict"
    assert report["written"] >= 10
    shapes = read_table(output, "shapes.txt")
    assert shapes is not None and not shapes.empty
    # Every written shape is a drawable line with an ordered sequence.
    for _shape_id, group in shapes.groupby("shape_id"):
        sequence = group["shape_pt_sequence"].astype(int).to_numpy()
        assert len(group) >= 2
        assert (np.diff(sequence) > 0).all()
        assert group["shape_pt_lat"].astype(float).between(59.0, 61.0).all()
        assert group["shape_pt_lon"].astype(float).between(24.0, 26.0).all()


def test_trips_and_stop_times_reference_the_shapes(strict_run):
    output, report = strict_run
    trips = read_table(output, "trips.txt")
    shapes = read_table(output, "shapes.txt")
    written = set(shapes["shape_id"])
    referenced = set(trips["shape_id"].dropna())
    assert referenced and referenced <= written
    stop_times = read_table(output, "stop_times.txt")
    shaped_trips = set(trips.loc[trips["shape_id"].notna(), "trip_id"])
    of_shaped = stop_times[stop_times["trip_id"].isin(shaped_trips)]
    distances = of_shaped["shape_dist_traveled"].dropna().astype(float)
    assert len(distances) == len(of_shaped)
    # Distances start at zero for each trip and never decrease.
    for _, group in of_shaped.groupby("trip_id"):
        values = group["shape_dist_traveled"].astype(float).to_numpy()
        assert values[0] == pytest.approx(0.0)
        assert (np.diff(values) >= 0).all()


def test_report_names_the_method_behind_every_shape(strict_run):
    _, report = strict_run
    methods = {entry["method"] for entry in report["shapes"]}
    assert methods <= {"osm_relation", "map_matched", "existing"}
    for entry in report["shapes"]:
        if entry["method"] == "osm_relation":
            assert entry["relation"] is not None
            assert entry["score"] is not None
        assert entry["trips"] >= 1
    for entry in report["skipped"]:
        assert entry["stage"]  # every refusal names its stage


def test_levels_trade_coverage_for_certainty(shapeless, transit_pbf, tmp_path):
    written = {}
    for name in ("strict", "relaxed", "permissive"):
        report = infer_shapes(
            shapeless,
            tmp_path / f"{name}.zip",
            transit_pbf,
            strictness=name,
            modes=["tram"],
        )
        written[name] = report["written"]
    assert written["strict"] <= written["relaxed"] <= written["permissive"]
    assert written["permissive"] > written["strict"]


def test_a_level_object_is_accepted(shapeless, transit_pbf, tmp_path):
    report = infer_shapes(
        shapeless,
        tmp_path / "custom.zip",
        transit_pbf,
        strictness=PERMISSIVE,
        modes=["tram"],
    )
    assert report["level"] == "permissive"


def test_inferred_shapes_match_the_feeds_own(helsinki_gtfs, transit_pbf, tmp_path):
    # The accuracy claim behind the default level: shapes inferred for
    # a shapeless feed sit within a few percent of what the operator
    # published, and never in a different corridor.
    import geopandas as gpd
    import pyproj
    import shapely

    shapeless = tram_feed(helsinki_gtfs, tmp_path / "in.zip", trips_per_route=25)
    output = tmp_path / "out.zip"
    infer_shapes(shapeless, output, transit_pbf, strictness="strict", modes=["tram"])

    truth_trips = read_table(helsinki_gtfs, "trips.txt").set_index("trip_id")
    truth_shapes = read_table(helsinki_gtfs, "shapes.txt")
    inferred_trips = read_table(output, "trips.txt")
    inferred_shapes = read_table(output, "shapes.txt")

    def lines(table):
        table = table.copy()
        for column in ("shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"):
            table[column] = pd.to_numeric(table[column], errors="coerce")
        table = table.dropna(subset=["shape_pt_lat", "shape_pt_lon"])
        return {
            shape_id: group.sort_values("shape_pt_sequence")[
                ["shape_pt_lon", "shape_pt_lat"]
            ].to_numpy()
            for shape_id, group in table.groupby("shape_id")
        }

    truth = lines(truth_shapes)
    got = lines(inferred_shapes)
    crs = gpd.GeoSeries(
        gpd.points_from_xy([24.94], [60.17]), crs="EPSG:4326"
    ).estimate_utm_crs()
    transformer = pyproj.Transformer.from_crs("EPSG:4326", crs, always_xy=True)

    def projected(coordinates):
        x, y = transformer.transform(coordinates[:, 0], coordinates[:, 1])
        return shapely.LineString(np.column_stack([x, y]))

    errors, offsets = [], []
    for row in inferred_trips.dropna(subset=["shape_id"]).itertuples():
        truth_id = truth_trips.loc[row.trip_id, "shape_id"]
        if pd.isna(truth_id) or truth_id not in truth or row.shape_id not in got:
            continue
        a, b = projected(got[row.shape_id]), projected(truth[truth_id])
        if b.length <= 0:
            continue
        errors.append(abs(a.length - b.length) / b.length)
        samples = shapely.line_interpolate_point(
            a, np.arange(0.0, max(a.length, 25.0) + 1, 25.0)
        )
        offsets.append(float(shapely.distance(b, samples).max()))
    assert len(errors) >= 10
    assert float(np.median(errors)) <= 0.05
    assert max(offsets) <= 250.0  # never a different corridor


def test_dangling_shape_references_count_as_shapeless(
    helsinki_gtfs, transit_pbf, tmp_path
):
    # trips.shape_id survives but shapes.txt does not: the feed is
    # shapeless in practice and must be treated that way.
    dangling = tram_feed(helsinki_gtfs, tmp_path / "dangling.zip", keep_shape_refs=True)
    report = infer_shapes(
        dangling,
        tmp_path / "fixed.zip",
        transit_pbf,
        strictness="strict",
        modes=["tram"],
    )
    assert report["written"] >= 10
    assert not any(entry["method"] == "existing" for entry in report["shapes"])


def test_existing_shapes_are_kept(helsinki_gtfs, transit_pbf, tmp_path):
    # A feed that already has usable shapes keeps them untouched.
    with zipfile.ZipFile(helsinki_gtfs) as archive:
        names = set(archive.namelist())
    assert "shapes.txt" in names
    report = infer_shapes(
        helsinki_gtfs,
        tmp_path / "kept.zip",
        transit_pbf,
        strictness="strict",
        modes=["ferry"],
    )
    assert report["written"] == 0
    assert all(entry["method"] == "existing" for entry in report["shapes"])
    before = read_table(helsinki_gtfs, "shapes.txt")
    after = read_table(tmp_path / "kept.zip", "shapes.txt")
    assert len(after) == len(before)


def test_levels_are_ordered_by_tolerance():
    assert STRICT.accept < LEVELS["relaxed"].accept < PERMISSIVE.accept
    assert STRICT.containment > PERMISSIVE.containment
    assert STRICT.ref_required and not PERMISSIVE.ref_required
    assert STRICT.operator_mismatch_disqualifies
    assert not PERMISSIVE.operator_mismatch_disqualifies


def test_resolve_rejects_nonsense():
    with pytest.raises(ValueError):
        _levels.resolve(object())
    assert _levels.resolve(STRICT) is STRICT
    assert _levels.resolve("permissive") is PERMISSIVE


def test_output_must_differ_from_the_input(shapeless, transit_pbf):
    with pytest.raises(ValueError, match="must differ"):
        infer_shapes(shapeless, shapeless, transit_pbf)


def test_provenance_sidecar_records_the_run(strict_run):
    import json

    output, report = strict_run
    sidecar = output.with_suffix(".provenance.json")
    assert sidecar.exists()
    record = json.loads(sidecar.read_text())
    # GTFS cannot say a shape was inferred; the sidecar must.
    assert record["level"] == "strict"
    assert record["written"] == report["written"]
    assert record["osm_pbf"].endswith(".osm.pbf")
    assert record["written_at"]
    methods = {entry["method"] for entry in record["shapes"]}
    assert methods and methods <= {"osm_relation", "map_matched", "existing"}


def certified(monkeypatch, before, after, swap=False, **kwargs):
    """Run `_certify` against synthetic validation reports.

    ``swap`` gives the output's notices different contexts, modelling
    one occurrence fixed and another introduced under the same code.
    """
    from transitio.shapes import _infer

    def fake(path, *a, **k):
        is_output = str(path).endswith("out.zip")
        codes = after if is_output else before
        offset = 100 if (is_output and swap) else 0
        return {
            "notices": [
                {"code": code, "severity": "ERROR", "context": {"n": i + offset}}
                for i, code in enumerate(codes)
            ],
            "incomplete": [],
        }

    monkeypatch.setattr("transitio.validate.validate_feed", fake)
    report = {}
    _infer._certify("in.zip", "out.zip", report, kwargs.get("check", True))
    return report


def test_certification_refuses_a_feed_it_made_worse(monkeypatch):
    from transitio.exceptions import ShapeInferenceError

    with pytest.raises(ShapeInferenceError, match="decreasing_shape_distance"):
        certified(monkeypatch, [], ["decreasing_shape_distance"])


def test_certification_ignores_pre_existing_errors(monkeypatch):
    # The input's own problems are not this function's doing.
    report = certified(
        monkeypatch, ["foreign_key_violation"], ["foreign_key_violation"]
    )
    assert report["introduced_notices"] == []


def test_certification_can_be_recorded_without_raising(monkeypatch):
    report = certified(monkeypatch, [], ["decreasing_shape_distance"], check=False)
    assert [n["code"] for n in report["introduced_notices"]] == [
        "decreasing_shape_distance"
    ]


def test_generated_ids_never_collide(helsinki_gtfs, transit_pbf, tmp_path):
    # A feed already using the generated id pattern must not have its
    # shapes merged into an inferred one.
    source = tram_feed(helsinki_gtfs, tmp_path / "in.zip", trips_per_route=10)
    with zipfile.ZipFile(source) as archive:
        payload = {n: archive.read(n) for n in archive.namelist()}
    trips = pd.read_csv(io.BytesIO(payload["trips.txt"]), dtype=str)
    trips["shape_id"] = "transitio-0"  # dangling, and squats the id
    squatted = tmp_path / "squatted.zip"
    with zipfile.ZipFile(squatted, "w", zipfile.ZIP_DEFLATED) as out:
        for name, blob in payload.items():
            out.writestr(
                name, trips.to_csv(index=False) if name == "trips.txt" else blob
            )
    report = infer_shapes(
        squatted, tmp_path / "out.zip", transit_pbf, modes=["tram"], check=False
    )
    written = {e["shape_id"] for e in report["shapes"] if e["method"] != "existing"}
    assert written and "transitio-0" not in written


def test_existing_shapes_keep_their_schema(transit_pbf, tmp_path):
    # Blank optional distances stay blank (never the string "nan") and
    # extension columns survive a run that infers nothing.
    feed = tmp_path / "extended.zip"
    shapes = pd.DataFrame(
        {
            "shape_id": ["s1", "s1"],
            "shape_pt_lat": ["60.17", "60.18"],
            "shape_pt_lon": ["24.94", "24.95"],
            "shape_pt_sequence": ["0", "1"],
            "shape_dist_traveled": ["", ""],
            "shape_bearing": ["90", "91"],  # an extension column
        }
    )
    with zipfile.ZipFile(feed, "w") as out:
        out.writestr("agency.txt", "agency_id,agency_name\nA,Agency\n")
        out.writestr("routes.txt", "route_id,agency_id,route_type\nR,A,3\n")
        out.writestr("trips.txt", "route_id,service_id,trip_id,shape_id\nR,S,T,s1\n")
        out.writestr("stop_times.txt", "trip_id,stop_id,stop_sequence\nT,X,1\nT,Y,2\n")
        out.writestr(
            "stops.txt",
            "stop_id,stop_lat,stop_lon\nX,60.17,24.94\nY,60.18,24.95\n",
        )
        out.writestr("shapes.txt", shapes.to_csv(index=False))
    output = tmp_path / "out.zip"
    infer_shapes(feed, output, transit_pbf, modes=["bus"], check=False)
    # Read the raw bytes: pandas would render a correctly blank field
    # as the string "nan" too, so a parsed check cannot tell them apart.
    with zipfile.ZipFile(output) as archive:
        raw = archive.read("shapes.txt").decode()
    header, *rows = [line for line in raw.splitlines() if line]
    assert "shape_bearing" in header.split(",")
    assert "nan" not in raw
    distance = header.split(",").index("shape_dist_traveled")
    assert all(row.split(",")[distance] == "" for row in rows)
    bearing = header.split(",").index("shape_bearing")
    assert [row.split(",")[bearing] for row in rows] == ["90", "91"]


def test_output_must_differ_from_the_extract(shapeless, transit_pbf, tmp_path):
    with pytest.raises(ValueError, match="the extract"):
        infer_shapes(shapeless, transit_pbf, transit_pbf)


def test_intake_budget_refuses_an_oversized_entry(shapeless, transit_pbf, tmp_path):
    from transitio.shapes import _infer

    with pytest.raises(OSError, match="budget"):
        _infer._read_tables(shapeless, max_entry_bytes=16)


def test_certification_counts_repeat_notices(monkeypatch):
    # A second error under a code the input already had is still worse.
    from transitio.exceptions import ShapeInferenceError

    with pytest.raises(ShapeInferenceError, match="foreign_key_violation"):
        certified(
            monkeypatch,
            ["foreign_key_violation"],
            ["foreign_key_violation", "foreign_key_violation"],
        )


def test_map_matched_shapes_carry_evidence(strict_run):
    _, report = strict_run
    matched = [e for e in report["shapes"] if e["method"] == "map_matched"]
    if matched:  # the fixture resolves some patterns by graph
        assert all(e["score"] is not None for e in matched)


def test_skipped_entries_identify_their_pattern(strict_run):
    _, report = strict_run
    for entry in report["skipped"]:
        assert entry["first_stop"] and entry["last_stop"]
        assert isinstance(entry["trips"], list) and entry["trips"]


def test_legal_na_strings_survive(transit_pbf, tmp_path):
    # "NA" and "NULL" are legal GTFS ids; pandas' default NA parsing
    # would blank them and break every reference that uses them.
    feed = tmp_path / "na.zip"
    with zipfile.ZipFile(feed, "w") as out:
        out.writestr("agency.txt", "agency_id,agency_name\nNA,Agency NULL\n")
        out.writestr("routes.txt", "route_id,agency_id,route_type\nNULL,NA,3\n")
        out.writestr("trips.txt", "route_id,service_id,trip_id\nNULL,NA,N/A\n")
        out.writestr(
            "stop_times.txt", "trip_id,stop_id,stop_sequence\nN/A,NA,1\nN/A,NULL,2\n"
        )
        out.writestr(
            "stops.txt",
            "stop_id,stop_lat,stop_lon\nNA,60.17,24.94\nNULL,60.18,24.95\n",
        )
    output = tmp_path / "out.zip"
    infer_shapes(feed, output, transit_pbf, modes=["bus"], check=False)
    trips = read_table(output, "trips.txt", coerce_na=False)
    assert list(trips["trip_id"]) == ["N/A"]
    assert list(trips["route_id"]) == ["NULL"]
    stop_times = read_table(output, "stop_times.txt", coerce_na=False)
    assert set(stop_times["stop_id"]) == {"NA", "NULL"}


def test_certification_refuses_a_sampled_validation(monkeypatch):
    # A truncated validation cannot prove the output is sound, so it
    # must refuse rather than certify on partial evidence.
    from transitio.exceptions import ShapeInferenceError
    from transitio.shapes import _infer

    def fake(path, *a, **k):
        return {
            "notices": [{"code": "notice_limit_reached", "severity": "WARNING"}],
            "incomplete": [],
        }

    monkeypatch.setattr("transitio.validate.validate_feed", fake)
    with pytest.raises(ShapeInferenceError, match="sampled or truncated"):
        _infer._certify("in.zip", "out.zip", {}, True)


def test_certification_sees_a_swapped_occurrence(monkeypatch):
    # One occurrence fixed, a different one introduced under the same
    # code: the counts match but the feed is not the same.
    from transitio.exceptions import ShapeInferenceError

    with pytest.raises(ShapeInferenceError):
        certified(
            monkeypatch,
            ["foreign_key_violation"],
            ["foreign_key_violation"] * 1,
            swap=True,
        )


def test_provenance_lineage_is_inherited(shapeless, transit_pbf, tmp_path):
    # A feed inferred twice must not look operator-published the
    # second time round.
    first = tmp_path / "first.zip"
    infer_shapes(shapeless, first, transit_pbf, modes=["tram"], check=False)
    second = tmp_path / "second.zip"
    report = infer_shapes(first, second, transit_pbf, modes=["tram"], check=False)
    assert report["inherited"] is not None
    assert report["inherited"]["level"] == "strict"


def test_provenance_records_the_effective_thresholds(strict_run):
    import json

    output, _ = strict_run
    record = json.loads(output.with_suffix(".provenance.json").read_text())
    # A name alone cannot survive a recalibration or a custom Level.
    assert record["thresholds"]["accept"] == STRICT.accept
    assert record["thresholds"]["ref_required"] is True
    assert record["osm_pbf_sha256"]


def test_entry_count_budget_refuses_a_swarm(tmp_path):
    from transitio.shapes import _infer

    swarm = tmp_path / "swarm.zip"
    with zipfile.ZipFile(swarm, "w") as out:
        for index in range(5):
            out.writestr(f"pad{index}.bin", b"x")
    monkey = _infer.MAX_ENTRIES
    try:
        _infer.MAX_ENTRIES = 3
        with pytest.raises(OSError, match="archive entries"):
            _infer._check_budgets(swarm)
    finally:
        _infer.MAX_ENTRIES = monkey


def test_mixed_shaped_and_shapeless_trips(helsinki_gtfs, transit_pbf, tmp_path):
    # One trip of a pattern keeps its published shape; the others get
    # an inferred one, and the report counts each honestly.
    source = tram_feed(helsinki_gtfs, tmp_path / "mixed.zip", trips_per_route=6)
    with zipfile.ZipFile(source) as archive:
        payload = {n: archive.read(n) for n in archive.namelist()}
    trips = pd.read_csv(io.BytesIO(payload["trips.txt"]), dtype=str)
    trips["shape_id"] = ""
    trips.loc[trips.index[0], "shape_id"] = "published"
    shapes = pd.DataFrame(
        {
            "shape_id": ["published"] * 2,
            "shape_pt_lat": ["60.17", "60.18"],
            "shape_pt_lon": ["24.94", "24.95"],
            "shape_pt_sequence": ["0", "1"],
        }
    )
    mixed = tmp_path / "mixed-in.zip"
    with zipfile.ZipFile(mixed, "w", zipfile.ZIP_DEFLATED) as out:
        for name, blob in payload.items():
            out.writestr(
                name, trips.to_csv(index=False) if name == "trips.txt" else blob
            )
        out.writestr("shapes.txt", shapes.to_csv(index=False))
    output = tmp_path / "mixed-out.zip"
    report = infer_shapes(mixed, output, transit_pbf, modes=["tram"], check=False)
    kept = [e for e in report["shapes"] if e["shape_id"] == "published"]
    assert kept and kept[0]["trips"] == 1
    after = read_table(output, "trips.txt")
    assert (after["shape_id"] == "published").sum() == 1
    # The published shape survives untouched.
    assert "published" in set(read_table(output, "shapes.txt")["shape_id"])


def test_empty_shapes_member_is_not_a_crash(transit_pbf, tmp_path):
    # Zero-byte and header-only shapes.txt are empty tables, not
    # malformed feeds — and a run that infers nothing must still write.
    for label, body in (("zero", b""), ("header", b"shape_id,shape_pt_lat\n")):
        feed = tmp_path / f"{label}.zip"
        with zipfile.ZipFile(feed, "w") as out:
            out.writestr("agency.txt", "agency_id,agency_name\nA,Ag\n")
            out.writestr("routes.txt", "route_id,agency_id,route_type\nR,A,3\n")
            out.writestr("trips.txt", "route_id,service_id,trip_id\nR,S,T\n")
            out.writestr(
                "stop_times.txt", "trip_id,stop_id,stop_sequence\nT,X,1\nT,Y,2\n"
            )
            out.writestr(
                "stops.txt",
                "stop_id,stop_lat,stop_lon\nX,60.17,24.94\nY,60.18,24.95\n",
            )
            out.writestr("shapes.txt", body)
        output = tmp_path / f"{label}-out.zip"
        # The point is that an empty member parses as an empty table
        # rather than raising; whether this tiny feed happens to match
        # a relation is beside it.
        report = infer_shapes(feed, output, transit_pbf, modes=["bus"], check=False)
        assert output.exists()
        assert isinstance(report["written"], int)
        assert read_table(output, "shapes.txt") is not None


def test_second_run_keeps_shapes_labelled_inferred(shapeless, transit_pbf, tmp_path):
    # A shape inferred by an earlier run must not be laundered into an
    # operator-published one by a later pass.
    first = tmp_path / "first.zip"
    infer_shapes(shapeless, first, transit_pbf, modes=["tram"], check=False)
    second = tmp_path / "second.zip"
    report = infer_shapes(first, second, transit_pbf, modes=["tram"], check=False)
    carried = [e for e in report["shapes"] if e.get("inferred_by")]
    assert carried, "previously inferred shapes should keep their evidence"
    assert all(e["method"] in ("osm_relation", "map_matched") for e in carried)
    assert report["inherited"]["thresholds"]["accept"] == STRICT.accept


def test_output_named_like_a_sidecar_is_not_destroyed(shapeless, transit_pbf, tmp_path):
    # An output already called *.provenance.json must not resolve to
    # its own sidecar and be overwritten by it.
    output = tmp_path / "feed.provenance.json"
    infer_shapes(shapeless, output, transit_pbf, modes=["tram"], check=False)
    assert zipfile.is_zipfile(output)
    assert (tmp_path / "feed.provenance.json.provenance.json").exists()


def test_blank_stop_coordinates_skip_not_crash(transit_pbf, tmp_path):
    # Blank coordinates are legal GTFS for some location types.
    feed = tmp_path / "blank.zip"
    with zipfile.ZipFile(feed, "w") as out:
        out.writestr("agency.txt", "agency_id,agency_name\nA,Ag\n")
        out.writestr("routes.txt", "route_id,agency_id,route_type\nR,A,3\n")
        out.writestr("trips.txt", "route_id,service_id,trip_id\nR,S,T\n")
        out.writestr("stop_times.txt", "trip_id,stop_id,stop_sequence\nT,X,1\nT,Y,2\n")
        out.writestr("stops.txt", "stop_id,stop_lat,stop_lon\nX,,\nY,60.18,24.95\n")
    report = infer_shapes(
        feed, tmp_path / "out.zip", transit_pbf, modes=["bus"], check=False
    )
    assert report["written"] == 0


def test_unorderable_stop_times_refuse_the_whole_trip(transit_pbf, tmp_path):
    # Shaping from only the rows that parsed would leave the dropped
    # stop unchecked against the alignment.
    feed = tmp_path / "unordered.zip"
    with zipfile.ZipFile(feed, "w") as out:
        out.writestr("agency.txt", "agency_id,agency_name\nA,Ag\n")
        out.writestr("routes.txt", "route_id,agency_id,route_type\nR,A,3\n")
        out.writestr("trips.txt", "route_id,service_id,trip_id\nR,S,T\n")
        out.writestr(
            "stop_times.txt",
            "trip_id,stop_id,stop_sequence\nT,X,1\nT,Y,oops\nT,Z,3\n",
        )
        out.writestr(
            "stops.txt",
            "stop_id,stop_lat,stop_lon\nX,60.17,24.94\nY,60.18,24.95\nZ,60.19,24.96\n",
        )
    report = infer_shapes(
        feed, tmp_path / "out.zip", transit_pbf, modes=["bus"], check=False
    )
    assert report["patterns"] == 0
    assert report["written"] == 0


def test_duplicate_archive_entries_refuse(tmp_path, transit_pbf):
    from transitio.shapes import _infer

    duplicated = tmp_path / "dupe.zip"
    with zipfile.ZipFile(duplicated, "w") as out:
        out.writestr("stops.txt", "a")
        out.writestr("stops.txt", "b")
    with pytest.raises(OSError, match="duplicate archive entries"):
        _infer._check_budgets(duplicated)


def test_inferred_shapes_record_their_own_strictness(strict_run):
    _, report = strict_run
    inferred = [e for e in report["shapes"] if e["method"] != "existing"]
    assert inferred
    for entry in inferred:
        assert entry["level"] == "strict"
        assert entry["thresholds"]["accept"] == STRICT.accept
        assert entry["osm_pbf_sha256"]


def test_a_foreign_sidecar_is_not_inherited(shapeless, transit_pbf, tmp_path):
    # A sidecar naming different bytes must not lend its provenance.
    import json

    first = tmp_path / "first.zip"
    infer_shapes(shapeless, first, transit_pbf, modes=["tram"], check=False)
    sidecar = tmp_path / "first.provenance.json"
    record = json.loads(sidecar.read_text())
    record["feed_sha256"] = "0" * 64  # describes some other feed
    sidecar.write_text(json.dumps(record))
    report = infer_shapes(
        first, tmp_path / "second.zip", transit_pbf, modes=["tram"], check=False
    )
    assert report["inherited"] is None


def test_short_pattern_on_a_long_relation_is_not_circular(transit_pbf, tmp_path):
    # A pattern that merely turns back near its origin must not be read
    # as a completed loop and given the relation's unserved tail.
    from transitio.shapes._geometry import locate_on_shape

    import numpy as np
    import pyproj
    import shapely

    transformer = pyproj.Transformer.from_crs("EPSG:4326", 32635, always_xy=True)
    # An open line running 3 km east; the stops use only its first 300 m.
    xs = np.linspace(0.0, 3000.0, 200)
    line = shapely.LineString(np.column_stack([xs, np.zeros_like(xs)]))
    lonlat = np.asarray(
        [transformer.transform(x, 0.0, direction="INVERSE") for x in (0.0, 150.0, 60.0)]
    )
    latlon = np.column_stack([lonlat[:, 1], lonlat[:, 0]])
    # Last stop is back near the first: not a loop, because the LINE is
    # not closed — so this must refuse rather than jump to line.length.
    assert locate_on_shape(line, latlon, transformer) is None


def test_intake_refusals_reach_the_report(transit_pbf, tmp_path):
    feed = tmp_path / "refusals.zip"
    with zipfile.ZipFile(feed, "w") as out:
        out.writestr("agency.txt", "agency_id,agency_name\nA,Ag\n")
        out.writestr("routes.txt", "route_id,agency_id,route_type\nR,A,3\n")
        out.writestr("trips.txt", "route_id,service_id,trip_id\nR,S,T\n")
        out.writestr(
            "stop_times.txt",
            "trip_id,stop_id,stop_sequence\nT,X,1\nT,Y,bad\n",
        )
        out.writestr(
            "stops.txt",
            "stop_id,stop_lat,stop_lon\nX,60.17,24.94\nY,60.18,24.95\n",
        )
    report = infer_shapes(
        feed, tmp_path / "out.zip", transit_pbf, modes=["bus"], check=False
    )
    stages = {entry["stage"] for entry in report["skipped"]}
    assert "unorderable-stop-times" in stages


def test_a_hostile_sidecar_is_ignored(shapeless, transit_pbf, tmp_path):
    first = tmp_path / "first.zip"
    infer_shapes(shapeless, first, transit_pbf, modes=["tram"], check=False)
    # A JSON array, not an object: must be ignored, not crash.
    (tmp_path / "first.provenance.json").write_text("[1, 2, 3]")
    report = infer_shapes(
        first, tmp_path / "second.zip", transit_pbf, modes=["tram"], check=False
    )
    assert report["inherited"] is None
