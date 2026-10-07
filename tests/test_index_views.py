"""Schema 7: a place's default view by kind, cross-border feeds on request,
and ordering by relevance."""

import pytest

pytest.importorskip("geopandas")

import shapely  # noqa: E402

import transitio  # noqa: E402
from transitio import index as reader  # noqa: E402
from index_fixture import (  # noqa: E402
    GEOM_HEX,
    covered_feed,
    edge,
    place,
    write_partitioned_index,
)


def _box(*bounds):
    return shapely.to_wkb(shapely.box(*bounds)).hex()


PLACES = [
    place("fi", "country", country_code="FI", name="Finland"),
    place("uus", "region", country_code="FI", name="Uusimaa", parent_id="fi"),
    place("hel", "city", country_code="FI", name="Helsinki", parent_id="uus"),
    place("tll", "city", country_code="EE", name="Tallinn"),
    # About 10 km², 250 km² and 12,000 km².
    place("mc", "country", country_code="MC", geometry=_box(7.40, 43.72, 7.44, 43.75)),
    place("town", "region", country_code="FI", parent_id="fi", geometry=GEOM_HEX),
    place("wide", "region", country_code="FI", geometry=_box(24.0, 60.0, 26.0, 61.0)),
]
FEEDS = [
    {**covered_feed("f-hsl"), "home_country": "FI", "scope": "domestic"},
    {**covered_feed("f-coach"), "home_country": "FI", "scope": "domestic"},
    {**covered_feed("f-vr"), "home_country": "FI", "scope": "domestic"},
    {**covered_feed("f-tlt"), "home_country": "EE", "scope": "domestic"},
    {**covered_feed("f-ferry"), "home_country": None, "scope": "international"},
    {**covered_feed("f-old"), "home_country": None, "scope": "declared"},
    {**covered_feed("f-cam"), "home_country": "MC", "scope": "domestic"},
    {**covered_feed("f-zou"), "home_country": "MC", "scope": "domestic"},
    {**covered_feed("f-ter"), "home_country": "MC", "scope": "domestic"},
]


def _edge(place_id, feed_id, tier, category, relevance, cross=False, **kw):
    return edge(
        place_id,
        feed_id,
        tier=tier,
        relevance_category=category,
        relevance=relevance,
        cross_border=cross,
        **kw,
    )


EDGES = [
    # Helsinki: a city bus feed (primary), a regional coach (secondary), the
    # national rail (tertiary), an unknown-tier declared feed, and two
    # cross-border feeds.
    _edge("hel", "f-hsl", "local", "primary", 0.9),
    _edge("hel", "f-hsl", "regional", "secondary", 0.9),
    _edge("hel", "f-coach", "regional", "secondary", 0.4),
    _edge("hel", "f-vr", "national", "tertiary", 0.5),
    _edge("hel", "f-old", "unknown", "unknown", 0.0, cross=True),
    _edge("hel", "f-tlt", "international", "international", 0.1, cross=True),
    _edge("hel", "f-ferry", "international", "international", 0.3, cross=True),
    # The region and the country.
    _edge("uus", "f-coach", "regional", "secondary", 0.6),
    _edge("uus", "f-vr", "national", "tertiary", 0.7),
    _edge("uus", "f-hsl", "local", "primary", 0.2),
    _edge("fi", "f-vr", "national", "tertiary", 0.8),
    _edge("fi", "f-coach", "national", "tertiary", 0.3),
    _edge("fi", "f-hsl", "local", "primary", 0.1),
    _edge("tll", "f-tlt", "local", "primary", 0.8),
] + [
    # The sized places: a local-only, a regional and a national feed each.
    _edge(place_id, feed_id, tier, category, 0.5)
    for place_id, feed_ids in (
        ("mc", ("f-cam", "f-zou", "f-ter")),
        ("town", ("f-hsl", "f-coach", "f-vr")),
        ("wide", ("f-hsl", "f-coach", "f-vr")),
    )
    for feed_id, (tier, category) in zip(
        feed_ids,
        (("local", "primary"), ("regional", "secondary"), ("national", "tertiary")),
    )
]


@pytest.fixture(autouse=True)
def _reader_reads_schema_7(monkeypatch):
    monkeypatch.setattr(
        transitio, "__version__", reader.MIN_READER_VERSIONS[7], raising=False
    )


@pytest.fixture
def index(tmp_path):
    return reader.read_index(
        write_partitioned_index(
            tmp_path / "index", feeds=FEEDS, places=PLACES, edges=EDGES
        )
    )


def _ids(feeds):
    return [feed.feed_id for feed in feeds]


def test_the_default_view_follows_the_place_kind_and_ranks_by_relevance(index):
    helsinki = reader.place("hel", index=index)
    # A city: primary then secondary, each by relevance; the national rail,
    # the unknown feed and the cross-border feeds are not in the default view.
    view = helsinki.feeds()
    assert _ids(view) == ["f-hsl", "f-coach"]
    assert view[0].relevance_category == "primary" and view[0].relevance == 0.9
    assert view[0].tiers == {"local", "regional"} and view[0].cross_border is False
    # A region: secondary and tertiary; a country: tertiary only, best first.
    assert _ids(reader.place("uus", index=index).feeds()) == ["f-coach", "f-vr"]
    assert _ids(reader.place("fi", index=index).feeds()) == ["f-vr", "f-coach"]
    # Every category on request; a tier query is answered in tiers.
    assert _ids(helsinki.feeds(categories=None)) == ["f-hsl", "f-coach", "f-vr"]
    assert _ids(helsinki.feeds(categories=["tertiary"])) == ["f-vr"]
    # A tier query keeps its unknown edges flagged, as before, after the tier.
    assert _ids(helsinki.feeds(tiers=["national"])) == ["f-vr"]
    # The declared feed without a home country is a link: unknown tier,
    # shown only with the cross-border feeds, after them.
    assert _ids(helsinki.feeds(tiers=["national"], international=True)) == [
        "f-vr",
        "f-old",
    ]


@pytest.mark.parametrize(
    "place_id, expected",
    [
        pytest.param("mc", ["f-cam", "f-zou", "f-ter"], id="country-10km2"),
        pytest.param("town", ["f-hsl", "f-coach", "f-vr"], id="region-250km2"),
        pytest.param("wide", ["f-coach", "f-vr"], id="region-12000km2"),
    ],
)
def test_a_town_sized_region_or_country_lists_its_local_feeds(
    index, place_id, expected
):
    # At most 1,000 km²: primary, secondary and tertiary; larger: by kind.
    assert _ids(reader.place(place_id, index=index).feeds()) == expected


def test_international_adds_the_cross_border_feeds(index, tmp_path):
    helsinki = reader.place("hel", index=index)
    # On the whole index the cross-border edges are in the joined table: the
    # default view gains the international feeds, every category all of them.
    assert _ids(helsinki.feeds(international=True)) == [
        "f-hsl",
        "f-coach",
        "f-ferry",
        "f-tlt",
    ]
    everything = helsinki.feeds(international=True, categories=None)
    assert _ids(everything) == ["f-hsl", "f-coach", "f-vr", "f-ferry", "f-tlt", "f-old"]
    ferry = everything[3]
    assert ferry.relevance_category == "international" and ferry.cross_border is True
    # On a country load they come from the links and the partitions that
    # hold their feeds, read once.
    finland = reader.read_index(tmp_path / "index", country="FI")
    helsinki = reader.place("hel", index=finland)
    assert _ids(helsinki.feeds(categories=None)) == ["f-hsl", "f-coach", "f-vr"]
    assert _ids(helsinki.feeds(international=True)) == [
        "f-hsl",
        "f-coach",
        "f-ferry",
        "f-tlt",
    ]
    linked = helsinki.feeds(international=True, categories=["international"])
    assert _ids(linked) == ["f-ferry", "f-tlt"]
    assert linked[0].name == "f-ferry" and linked[1].name == "f-tlt"
    assert {p for p, t in finland._partition_tables if t == "feeds"} == {
        "EE",
        "international",
    }
    assert linked[0].relevance == 0.3 and linked[1].relevance == 0.1
    # A country served only through links has no home feeds or domestic
    # edges, and still answers with them on request.
    estonia = reader.read_index(tmp_path / "index", country="EE")
    assert _ids(reader.place("tll", index=estonia).feeds()) == ["f-tlt"]


def test_an_index_without_relevance_lists_every_feed_by_id():
    import geopandas
    import pandas

    from test_index_feeds import EDGES as OLD_EDGES, FEEDS as OLD_FEEDS, PLACES as OLD

    index = reader.Index(
        {},
        pandas.DataFrame(OLD_FEEDS),
        geopandas.GeoDataFrame(OLD, geometry="geometry", crs="EPSG:4326"),
        pandas.DataFrame(OLD_EDGES),
    )
    metro = reader.place("Q102", index=index)
    feeds = metro.feeds(spec=None)
    assert _ids(feeds) == sorted(_ids(feeds))
    assert feeds[0].relevance_category is None and feeds[0].relevance is None
    assert _ids(metro.feeds(international=True, spec=None)) == _ids(feeds)


# Near the equator, so shares follow the boxes' degree areas. Country aa
# holds region r with two cities; bb a city; cc has no feeds.
AREA_PLACES = [
    place("aa", "country", country_code="AA", geometry=_box(2, 0, 4, 2)),
    place("r", "region", country_code="AA", parent_id="aa", geometry=_box(2, 0, 3, 1)),
    place(
        "c1", "city", country_code="AA", parent_id="r", geometry=_box(2, 0, 2.4, 0.4)
    ),
    place(
        "k2",
        "city",
        source_subtype="county",
        country_code="AA",
        parent_id="r",
        geometry=_box(2.4, 0, 2.8, 0.4),
    ),
    place("m", "metro", country_code="AA", geometry=_box(2, 0, 2.8, 0.4)),
    place("bb", "country", country_code="BB", geometry=_box(4, 0, 6, 2)),
    place(
        "b1", "city", country_code="BB", parent_id="bb", geometry=_box(4, 0, 4.4, 0.4)
    ),
    place("cc", "country", country_code="CC", geometry=_box(0, 0, 2, 2)),
]
AREA_EDGES = [
    edge(place_id, feed_id, tier=tier)
    for place_id, feed_id, tier in (
        ("aa", "f-aa", "national"),
        ("r", "f-r", "regional"),
        ("c1", "f-c1", "local"),
        ("c1", "f-bus", "local"),
        ("k2", "f-bus", "local"),
        ("m", "f-m", "local"),
        ("bb", "f-bb", "national"),
        ("b1", "f-b1", "local"),
    )
]


@pytest.mark.parametrize(
    "bounds, country, parts, coverage, feeds",
    [
        pytest.param(
            (2, 0, 2.8, 0.4),
            None,
            [("c1", True, 1.0, 0.5), ("k2", True, 1.0, 0.5)],
            1.0,
            {"f-bus": ["c1", "k2"], "f-c1": ["c1"]},
            id="city-and-county",
        ),
        pytest.param(
            (2.1, 0.1, 2.2, 0.2),
            None,
            [("c1", False, 0.06, 1.0)],
            1.0,
            {"f-bus": ["c1"], "f-c1": ["c1"]},
            id="box-in-a-district",
        ),
        pytest.param(
            (2.3, 0.1, 2.5, 0.25),
            None,
            [("c1", False, 0.09, 0.5), ("k2", False, 0.09, 0.5)],
            1.0,
            {"f-bus": ["c1", "k2"], "f-c1": ["c1"]},
            id="across-two-cities",
        ),
        pytest.param((10, 0, 10.5, 0.5), None, [], 0.0, {}, id="off-the-country"),
        pytest.param(
            (3.9, 0, 4.1, 0.3),
            "BB",
            [("b1", False, 0.19, 0.5)],
            1.0,
            {"f-b1": ["b1"]},
            id="country-filter",
        ),
        pytest.param(
            (1.4, 1.1, 2.2, 1.3),
            None,
            [("aa", False, 0.01, 0.25)],
            0.25,
            {"f-aa": ["aa"]},
            id="mostly-in-a-country-without-feeds",
        ),
        pytest.param(
            (2.5, 0.3, 2.9, 0.95),
            None,
            [("k2", False, 0.19, 0.12)],
            0.12,
            {"f-bus": ["k2"]},
            id="city-over-1pc-displaces-its-region",
        ),
        pytest.param(
            (2.79, 0.39, 2.99, 0.79),
            None,
            [("r", False, 0.08, 1.0)],
            1.0,
            {"f-r": ["r"]},
            id="city-under-1pc-leaves-its-region",
        ),
    ],
)
def test_an_area_is_made_of_the_places_covering_it(
    tmp_path, bounds, country, parts, coverage, feeds
):
    from index_fixture import covered_feed as feed, write_index

    ids = sorted({edge["feed_id"] for edge in AREA_EDGES})
    index = reader.read_index(
        write_index(
            tmp_path / "index",
            feeds=[feed(feed_id) for feed_id in ids],
            places=AREA_PLACES,
            edges=AREA_EDGES,
        )
    )
    area = reader.area(bounds, country=country, index=index)
    found = [
        (part.place.id, part.whole, round(part.inside, 2), round(part.holds, 2))
        for part in area.parts
    ]
    assert found == parts
    assert round(area.coverage, 2) == coverage
    assert f"parts={len(parts)}, coverage={coverage:.2f})" in repr(area)
    listed = {f.feed_id: sorted({p for p, _ in f.edges}) for f in area.feeds()}
    assert listed == feeds
    # Categories given once serve every part.
    ranked = [
        {**e, "relevance_category": "primary", "relevance": 0.5} for e in AREA_EDGES
    ]
    ranked_index = reader.read_index(
        write_partitioned_index(
            tmp_path / "ranked",
            feeds=[feed(feed_id) for feed_id in ids],
            places=AREA_PLACES,
            edges=ranked,
        )
    )
    area = reader.area(bounds, country=country, index=ranked_index)
    once = area.feeds(categories=iter(["primary"]))
    every = area.feeds(categories=["primary"])
    assert [sorted(f.edges) for f in once] == [sorted(f.edges) for f in every]
