import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

pytest.importorskip("geopandas")
import geopandas  # noqa: E402
import shapely  # noqa: E402

import transitio  # noqa: E402
from transitio import index as transitio_index  # noqa: E402
from transitio.exceptions import (  # noqa: E402
    AmbiguousPlaceError,
    PlaceNotFoundError,
)
from transitio.index.places import _as_dict, _as_list, _as_str  # noqa: E402


def _p(
    place_id,
    kind,
    name,
    *,
    names=None,
    aliases=None,
    default_metro_id=None,
    parent_id=None,
    metro_ids=None,
    member_ids=None,
    country_code="US",
    geometry=None,
):
    return {
        "place_id": place_id,
        "kind": kind,
        "name": name,
        "names": names if names is not None else {"en": name},
        "aliases": aliases or [],
        "default_metro_id": default_metro_id,
        "parent_id": parent_id,
        "metro_ids": metro_ids or [],
        "member_ids": member_ids or [],
        "country_code": country_code,
        "geometry": geometry,
    }


BOX = shapely.box(-74.1, 40.6, -73.9, 40.9)
RECORDS = [
    _p(
        "Q60",
        "city",
        "New York City",
        names={"en": "New York City", "fi": "New Yorkin kaupunki"},
        aliases=["New York", "NYC"],
        default_metro_id="Q1109190",
        parent_id="Q1384",
        metro_ids=["Q1109190", "Q-msa"],
        geometry=BOX,
    ),
    _p(
        "Q1109190",
        "metro",
        "New York metropolitan area",
        member_ids=["Q60"],
        geometry=BOX,
    ),
    _p("Q-msa", "metro", "Tri-State Metro"),
    _p("Q1384", "region", "New York"),
    _p("Q-sp-il", "city", "Springfield", country_code="US"),
    _p("Q-sp-ma", "city", "Springfield", country_code="US"),
    _p("Q-cam-uk", "city", "Cambridge", country_code="GB"),
    _p("Q-cam-us", "city", "Cambridge", country_code="US"),
    _p("Q-ffm", "city", "Frankfurt am Main", aliases=["Frankfurt"], country_code="DE"),
    _p("Q-ffo", "city", "Frankfurt (Oder)", aliases=["Frankfurt"], country_code="DE"),
    _p(
        "Q-ncl-uk",
        "city",
        "Newcastle upon Tyne",
        aliases=["Newcastle"],
        country_code="GB",
    ),
    _p("Q-ncl-au", "city", "Newcastle", country_code="AU"),
    _p(
        "Q-twin",
        "city",
        "Twinsburg",
        metro_ids=["Q1109190", "Q-msa", "Q-ghost"],
        country_code="US",
    ),
    _p("Q-stj", "city", "St. John's", country_code="CA"),
    _p("Q-whb", "city", "Wilkes-Barre", country_code="US"),
    _p("Q1757", "city", "Helsinki", names={"en": "Helsinki"}, country_code="FI"),
    _p("Q-zur", "city", "Zürich", names={"de": "Zürich"}, country_code="CH"),
]


def _index(records):
    frame = geopandas.GeoDataFrame(records, geometry="geometry", crs="EPSG:4326")
    return transitio_index.Index({}, None, frame)


@pytest.fixture
def idx():
    return _index(RECORDS)


def test_a_bare_city_name_promotes_to_its_default_metro(idx):
    place = transitio_index.place("New York City", index=idx)
    assert place.id == "Q1109190"
    assert place.kind == "metro"
    assert place.promoted_from == "Q60"


def test_kind_pins_the_scope_and_suppresses_promotion(idx):
    place = transitio_index.place("New York City", kind="city", index=idx)
    assert place.id == "Q60"
    assert place.kind == "city"
    assert place.promoted_from is None


def test_a_qid_resolves_directly_without_promotion(idx):
    assert transitio_index.place("Q60", index=idx).id == "Q60"
    assert transitio_index.place("Q1109190", index=idx).id == "Q1109190"


def test_passing_a_place_returns_it(idx):
    city = transitio_index.place("New York City", kind="city", index=idx)
    assert transitio_index.place(city, index=idx) is city


def test_identical_city_names_are_ambiguous(idx):
    with pytest.raises(AmbiguousPlaceError) as caught:
        transitio_index.place("Springfield", index=idx)
    assert {c.id for c in caught.value.candidates} == {"Q-sp-il", "Q-sp-ma"}


def test_cambridge_uk_and_us_are_ambiguous(idx):
    with pytest.raises(AmbiguousPlaceError):
        transitio_index.place("Cambridge", index=idx)


def test_frankfurt_and_newcastle_short_names_are_ambiguous(idx):
    with pytest.raises(AmbiguousPlaceError) as frankfurt:
        transitio_index.place("Frankfurt", index=idx)
    assert {c.id for c in frankfurt.value.candidates} == {"Q-ffm", "Q-ffo"}
    with pytest.raises(AmbiguousPlaceError) as newcastle:
        transitio_index.place("Newcastle", index=idx)
    assert {c.id for c in newcastle.value.candidates} == {"Q-ncl-uk", "Q-ncl-au"}


def test_a_multi_metro_city_without_a_default_is_not_promoted(idx):
    place = transitio_index.place("Twinsburg", index=idx)
    assert place.id == "Q-twin"
    assert place.kind == "city"
    assert place.promoted_from is None


def test_metro_and_member_traversals_resolve_to_places(idx):
    city = transitio_index.place("New York City", kind="city", index=idx)
    assert {m.id for m in city.metros} == {"Q1109190", "Q-msa"}
    metro = transitio_index.place("Q1109190", index=idx)
    assert [m.id for m in metro.members] == ["Q60"]


def test_missing_metro_ids_are_silently_omitted(idx):
    # Q-twin lists Q-ghost, which is not in the index; it drops out.
    twin = transitio_index.place("Twinsburg", index=idx)
    assert {m.id for m in twin.metros} == {"Q1109190", "Q-msa"}


def test_intra_word_punctuation_folds_away(idx):
    assert transitio_index.place("St Johns", index=idx).id == "Q-stj"
    assert transitio_index.place("Wilkes Barre", index=idx).id == "Q-whb"
    # A non-breaking hyphen (U+2011) joins words like the ASCII one.
    assert transitio_index.place("Wilkes‑Barre", index=idx).id == "Q-whb"


def test_new_york_alone_is_ambiguous_across_kinds(idx):
    with pytest.raises(AmbiguousPlaceError) as caught:
        transitio_index.place("new york", index=idx)
    assert {c.id for c in caught.value.candidates} == {"Q60", "Q1384", "Q1109190"}


def test_no_match_raises_place_not_found(idx):
    with pytest.raises(PlaceNotFoundError):
        transitio_index.place("Atlantis", index=idx)


def test_places_lists_candidates_ranked_by_match_then_kind(idx):
    found = transitio_index.places("new york", index=idx)
    # Exact matches (city, then region by kind precedence) before the token-subset
    # metro match.
    assert [p.id for p in found] == ["Q60", "Q1384", "Q1109190"]


def test_place_exposes_identity_hierarchy_and_geometry(idx):
    city = transitio_index.place("New York City", kind="city", index=idx)
    assert city.names["fi"] == "New Yorkin kaupunki"
    assert "New York" in city.aliases
    assert city.country_code == "US"
    assert city.default_metro_id == "Q1109190"
    assert city.metro_ids == ["Q1109190", "Q-msa"]
    assert city.geometry.area > 0
    assert city.parent.id == "Q1384"
    assert [c.id for c in transitio_index.place("Q1384", index=idx).children] == ["Q60"]


def test_a_prefix_and_a_diacritic_insensitive_match_resolve(idx):
    assert transitio_index.place("Helsi", index=idx).id == "Q1757"
    assert transitio_index.place("zurich", index=idx).id == "Q-zur"


def test_a_non_english_label_resolves(idx):
    assert (
        transitio_index.place("New Yorkin kaupunki", kind="city", index=idx).id == "Q60"
    )


def test_a_feeds_only_index_has_no_places_to_resolve():
    feeds_only = transitio_index.Index({}, None, None)
    with pytest.raises(PlaceNotFoundError, match="no places"):
        transitio_index.place("anywhere", index=feeds_only)


def test_the_top_level_alias_is_wired(idx):
    assert transitio.place("New York City", index=idx).id == "Q1109190"
    assert transitio.Place is transitio_index.Place


def test_the_coercion_helpers_handle_parquet_shapes():
    numpy = pytest.importorskip("numpy")
    assert _as_list(numpy.array(["a", "b"])) == ["a", "b"]
    assert _as_list(None) == []
    assert _as_list(float("nan")) == []
    assert _as_dict([("en", "X")]) == {"en": "X"}
    assert _as_dict(None) == {}
    assert _as_str(float("nan")) is None
    assert _as_str("Q1") == "Q1"
