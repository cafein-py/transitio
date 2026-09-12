"""Schema 7: a place's default view by kind, cross-border feeds on request,
and ordering by relevance."""

import pytest

pytest.importorskip("geopandas")

import transitio  # noqa: E402
from transitio import index as reader  # noqa: E402
from index_fixture import (  # noqa: E402
    covered_feed,
    edge,
    place,
    write_partitioned_index,
)

PLACES = [
    place("fi", "country", country_code="FI", name="Finland"),
    place("uus", "region", country_code="FI", name="Uusimaa", parent_id="fi"),
    place("hel", "city", country_code="FI", name="Helsinki", parent_id="uus"),
    place("tll", "city", country_code="EE", name="Tallinn"),
]
FEEDS = [
    {**covered_feed("f-hsl"), "home_country": "FI", "scope": "domestic"},
    {**covered_feed("f-coach"), "home_country": "FI", "scope": "domestic"},
    {**covered_feed("f-vr"), "home_country": "FI", "scope": "domestic"},
    {**covered_feed("f-tlt"), "home_country": "EE", "scope": "domestic"},
    {**covered_feed("f-ferry"), "home_country": None, "scope": "international"},
    {**covered_feed("f-old"), "home_country": None, "scope": "declared"},
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
