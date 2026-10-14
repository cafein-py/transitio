"""Schema 7: a place's default view by kind, cross-border feeds on request,
and ordering by relevance."""

import datetime

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


# The place kind's view, the default before every feed was listed.
VIEW = {"categories": "default", "international": False}


def test_feeds_lists_every_feed_and_the_kind_view_follows_the_place_kind(index):
    helsinki = reader.place("hel", index=index)
    # Every feed by default: by category, then relevance, cross-border ones
    # and the declared feed of unknown tier included.
    every = ["f-hsl", "f-coach", "f-vr", "f-ferry", "f-tlt", "f-old"]
    assert _ids(helsinki.feeds()) == every
    # A city's view: primary then secondary, each by relevance; the national
    # rail, the unknown feed and the cross-border feeds are not in it.
    view = helsinki.feeds(**VIEW)
    assert _ids(view) == ["f-hsl", "f-coach"]
    assert view[0].relevance_category == "primary" and view[0].relevance == 0.9
    assert view[0].tiers == {"local", "regional"} and view[0].cross_border is False
    # A region: secondary and tertiary; a country: tertiary only, best first.
    assert _ids(reader.place("uus", index=index).feeds(**VIEW)) == ["f-coach", "f-vr"]
    assert _ids(reader.place("fi", index=index).feeds(**VIEW)) == ["f-vr", "f-coach"]
    # Named categories; a tier query is answered in tiers.
    assert _ids(helsinki.feeds(international=False)) == ["f-hsl", "f-coach", "f-vr"]
    assert _ids(helsinki.feeds(categories=["tertiary"])) == ["f-vr"]
    # A tier query keeps its unknown edges flagged, as before, after the tier.
    assert _ids(helsinki.feeds(tiers=["national"], international=False)) == ["f-vr"]
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
    assert _ids(reader.place(place_id, index=index).feeds(**VIEW)) == expected


def test_international_adds_the_cross_border_feeds(index, tmp_path):
    helsinki = reader.place("hel", index=index)
    # On the whole index the cross-border edges are in the joined table: the
    # kind's view gains the international feeds, every category all of them.
    assert _ids(helsinki.feeds(categories="default")) == [
        "f-hsl",
        "f-coach",
        "f-ferry",
        "f-tlt",
    ]
    everything = helsinki.feeds()
    assert _ids(everything) == ["f-hsl", "f-coach", "f-vr", "f-ferry", "f-tlt", "f-old"]
    ferry = everything[3]
    assert ferry.relevance_category == "international" and ferry.cross_border is True
    table = everything.to_dataframe().set_index("feed_id")
    assert table["cross_border"].tolist() == [False] * 3 + [True] * 3
    assert table["no_tier_reason"].isna().tolist() == [True] * 5 + [False]
    assert table.loc["f-old", "reason"] == (
        "unknown (unknown tier); serves the place across a border; no tier "
        "(known only from a catalogue: the index has not crawled it)"
    )
    # On a country load they come from the links and the partitions that
    # hold their feeds, read once.
    finland = reader.read_index(tmp_path / "index", country="FI")
    helsinki = reader.place("hel", index=finland)
    assert _ids(helsinki.feeds(international=False)) == ["f-hsl", "f-coach", "f-vr"]
    assert _ids(helsinki.feeds(categories="default")) == [
        "f-hsl",
        "f-coach",
        "f-ferry",
        "f-tlt",
    ]
    linked = helsinki.feeds(categories=["international"])
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


@pytest.mark.parametrize(
    "tier, method, departures, kinds, expected",
    [
        ("local", "crawl", 10.0, ["bus"], None),
        ("unknown", "inferred", None, [], "known only from a catalogue"),
        ("unknown", "crawl", None, [], "the index measured no service for it"),
        ("unknown", "crawl", 10.0, ["1501", "1100"], "of types the index gives no"),
        ("unknown", "crawl", 10.0, ["bus"], "could not classify its routes"),
    ],
    ids=["tiered", "catalogued", "no-service", "air-and-taxi", "unclassified"],
)
def test_a_feed_of_unknown_tier_says_why(tier, method, departures, kinds, expected):
    from types import SimpleNamespace

    from transitio.index.feeds import IndexedFeed

    edge = SimpleNamespace(
        tier=tier,
        method=method,
        service=SimpleNamespace(departures_per_day=departures),
        summary={"overlap": {"departures": dict.fromkeys(kinds, 1.0)}},
    )
    reason = IndexedFeed.no_tier_reason.fget(SimpleNamespace(edges={tier: edge}))
    assert reason is None if expected is None else expected in reason


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


# Why a place meeting an area is not one of its parts.
COVERED, SMALL = (
    "narrower places cover its land in the area",
    "holds less than 1 % of the area",
)


@pytest.mark.parametrize(
    "bounds, country, parts, coverage, feeds, unused",
    [
        pytest.param(
            (2, 0, 2.8, 0.4),
            None,
            [("c1", True, 1.0, 0.5), ("k2", True, 1.0, 0.5)],
            1.0,
            {"f-bus": ["c1", "k2"], "f-c1": ["c1"]},
            [("aa", COVERED), ("r", COVERED)],
            id="city-and-county",
        ),
        pytest.param(
            (2.1, 0.1, 2.2, 0.2),
            None,
            [("c1", False, 0.06, 1.0)],
            1.0,
            {"f-bus": ["c1"], "f-c1": ["c1"]},
            [("aa", COVERED), ("r", COVERED)],
            id="box-in-a-district",
        ),
        pytest.param(
            (2.3, 0.1, 2.5, 0.25),
            None,
            [("c1", False, 0.09, 0.5), ("k2", False, 0.09, 0.5)],
            1.0,
            {"f-bus": ["c1", "k2"], "f-c1": ["c1"]},
            [("aa", COVERED), ("r", COVERED)],
            id="across-two-cities",
        ),
        pytest.param((10, 0, 10.5, 0.5), None, [], 0.0, {}, [], id="off-the-country"),
        pytest.param(
            shapely.Point(2.1, 0.1),
            None,
            [],
            0.0,
            {},
            [(p, "the area has no extent") for p in ("aa", "c1", "r")],
            id="a-point",
        ),
        pytest.param(
            (3.9, 0, 4.1, 0.3),
            "BB",
            [("b1", False, 0.19, 0.5)],
            1.0,
            {"f-b1": ["b1"]},
            [("bb", COVERED)],
            id="country-filter",
        ),
        pytest.param(
            (1.4, 1.1, 2.2, 1.3),
            None,
            [("aa", False, 0.01, 0.25)],
            0.25,
            {"f-aa": ["aa"]},
            [],
            id="mostly-in-a-country-without-feeds",
        ),
        pytest.param(
            (2.5, 0.3, 2.9, 0.95),
            None,
            [("r", False, 0.26, 0.88), ("k2", False, 0.19, 0.12)],
            1.0,
            {"f-bus": ["k2"], "f-r": ["r"]},
            [("aa", COVERED)],
            id="a-city-and-its-region-for-the-rest",
        ),
        pytest.param(
            (2.79, 0.39, 2.99, 0.79),
            None,
            [("r", False, 0.08, 1.0)],
            1.0,
            {"f-r": ["r"]},
            [("aa", COVERED), ("k2", SMALL)],
            id="city-under-1pc-leaves-its-region",
        ),
    ],
)
def test_an_area_is_made_of_the_places_covering_it(
    tmp_path, bounds, country, parts, coverage, feeds, unused
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
    assert [(u.place.id, u.reason) for u in area.unused] == unused
    assert all(isinstance(u, reader.AreaUnused) for u in area.unused)
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


def test_a_region_part_takes_the_feeds_serving_the_land_it_stands_for(tmp_path):
    from index_fixture import covered_feed as feed, write_index

    # Region r stands for the box outside city k2: a regional feed elsewhere
    # in r is left out, one meeting that land or of unknown coverage kept.
    coverage = {
        "f-near": _box(2.6, 0.5, 2.7, 0.6),
        "f-far": _box(2.0, 0.8, 2.1, 0.9),
        "f-empty": shapely.to_wkb(shapely.Point()).hex(),
    }
    ids = ("f-near", "f-far", "f-unknown", "f-empty")
    feeds = [{**feed(i), "coverage": coverage.get(i)} for i in ids]
    edges = [edge("r", i, tier="regional") for i in ids]
    index = reader.read_index(
        write_index(
            tmp_path / "index",
            feeds=[*feeds, feed("f-bus")],
            places=AREA_PLACES,
            edges=[*edges, edge("k2", "f-bus", tier="local")],
        )
    )
    area = reader.area((2.5, 0.3, 2.9, 0.95), index=index)
    assert sorted(_ids(area.feeds())) == ["f-bus", "f-empty", "f-near", "f-unknown"]
    # A feed kept through its city edge alone ranks by that edge, not by the
    # region edge left out.
    ranked = [
        edge("k2", "f-bus", tier="local", relevance_category="primary", relevance=0.5),
        edge("k2", "f-far", tier="regional", relevance_category="secondary"),
        edge("r", "f-far", tier="regional", relevance_category="primary", relevance=1),
    ]
    index = reader.read_index(
        write_partitioned_index(
            tmp_path / "ranked",
            feeds=[feeds[1], feed("f-bus")],
            places=AREA_PLACES,
            edges=ranked,
        )
    )
    area = reader.area((2.5, 0.3, 2.9, 0.95), index=index)
    assert _ids(area.feeds()) == ["f-bus", "f-far"]


# A Munich-like city: three large feeds each running nearly all of its
# service (f-mvv nameless, its S-Bahn typed as tram), a smaller city feed
# behind an account, a regional rail feed behind unknown credentials, a stale
# copy, a regional aggregate and a small feed it contains. Each feed's
# departures a day by mode, its stop count and, per other feed, the share of
# each mode's departures it also runs.
MUNICH = {
    "f-mvv": {
        "extent": 0.6,
        "name": None,
        "tiers": ("local", "regional"),
        "modes": {"bus": 283773, "tram": 76006, "subway": 37658, "rail": 1938},
        "stops": 28330,
        "end": "2026-11-30",
        "with": {
            "f-delfi": {"bus": 1.0, "tram": 1.0, "subway": 1.0, "rail": 1.0},
            "f-mvg": {"bus": 0.97, "tram": 0.81, "subway": 1.0},
            "f-urban": {"bus": 0.99, "tram": 0.81, "subway": 1.0},
            "f-rail": {"tram": 0.19, "rail": 1.0},
        },
    },
    "f-delfi": {
        "extent": 5.0,
        "name": "DELFI",
        "mdb": {"name": "Registration required at the DELFI portal"},
        "tiers": ("regional",),
        "modes": {"bus": 288266, "tram": 61747, "subway": 37658, "rail": 17631},
        "stops": 550396,
        "with": {
            "f-mvv": {"bus": 0.98, "tram": 1.0, "subway": 1.0, "rail": 0.98},
            "f-urban": {"bus": 0.98, "tram": 1.0, "subway": 1.0},
            "f-mvg": {"bus": 0.95, "tram": 1.0, "subway": 1.0},
            "f-rail": {"rail": 0.97},
        },
    },
    "f-urban": {
        "extent": 5.0,
        "name": "Public Transport Germany",
        "modes": {"bus": 283289, "tram": 61747, "subway": 37860},
        "stops": 674929,
        "end": "2026-11-02",
        "with": {
            "f-mvv": {"bus": 0.99, "tram": 1.0, "subway": 0.99},
            "f-delfi": {"bus": 1.0, "tram": 1.0, "subway": 1.0},
            "f-mvg": {"bus": 0.96, "tram": 1.0, "subway": 0.99},
        },
    },
    "f-mvg": {
        "extent": 0.15,
        "name": "MVG",
        "modes": {"bus": 274129, "tram": 61747, "subway": 37658},
        "stops": 4250,
        "access": "key",
        "provider": "mvg-api",
        "with": {
            "f-mvv": {"bus": 0.99, "tram": 1.0, "subway": 1.0},
            "f-delfi": {"bus": 1.0, "tram": 1.0, "subway": 1.0},
            "f-urban": {"bus": 0.99, "tram": 1.0, "subway": 1.0},
        },
    },
    "f-rail": {
        "extent": 1.5,
        "name": "Regional Rail",
        "tiers": ("regional",),
        "modes": {"rail": 17190},
        "stops": 16688,
        "end": "2026-11-02",
        "access": "key",
        "with": {"f-mvv": {"rail": 1.0}, "f-delfi": {"rail": 1.0}},
    },
    "f-old": {"name": "MVV (old)", "stops": 27000, "end": "2026-07-31"},
    "f-bw": {
        "extent": 2.5,
        "name": "BW aggregate",
        "tiers": ("regional",),
        "modes": {"bus": 200, "rail": 46},
        "stops": 61261,
        "with": {
            "f-mvv": {"bus": 0.6, "rail": 0.85},
            "f-delfi": {"bus": 0.95, "rail": 0.95},
            "f-rail": {"rail": 0.55},
        },
    },
    "f-tiny": {
        "extent": 0.05,
        "name": "Tiny",
        "modes": {"bus": 30},
        "stops": 120,
        "contained": ["f-bw"],
        "with": {
            "f-mvv": {"bus": 0.77},
            "f-delfi": {"bus": 0.96},
            "f-bw": {"bus": 1.0},
        },
    },
}
MUNICH_PROVIDERS = [
    {
        "provider_id": "mvg-api",
        "name": "MVG API",
        "credential_fields": ["key"],
        "free": True,
    },
]


def _around_munich(extent):
    return (11.58 - extent, 48.14 - extent, 11.58 + extent, 48.14 + extent)


def munich_index(directory, feeds=MUNICH, *, overlap=True, providers=None, kind="city"):
    """A schema-11 index of one place of ``kind``, ``muc``, served by
    ``feeds`` (specs as in :data:`MUNICH`, an ``extent`` the half-width in
    degrees of a feed's coverage around the city, ``cross_border`` its edges'
    flag); ``overlap=False`` leaves out the overlap evidence and the recorded
    staleness, as an index built before them."""
    total = sum(sum(spec.get("modes", {}).values()) for spec in feeds.values())
    records, edges = [], []
    for feed_id, spec in feeds.items():
        records.append(
            {
                **covered_feed(feed_id, name=spec["name"]),
                "home_country": "DE",
                "scope": "domestic",
                "mdb": spec.get("mdb"),
                "stop_count": spec["stops"],
                "service_start": spec.get("start", "2026-06-01"),
                "service_end": spec.get("end", "2026-12-31"),
                "last_crawled": "2026-10-01T03:00:00+00:00",
                "access": spec.get("access", "open"),
                "access_provider": spec.get("provider"),
                "coverage": (
                    shapely.box(*_around_munich(spec["extent"])).wkb_hex
                    if "extent" in spec
                    else None
                ),
            }
        )
        modes = spec.get("modes")
        departures = sum(modes.values()) if modes else None
        evidence = {"share_basis": "departures"}
        evidence["share_of_place"] = (departures or 0) / total
        if overlap and modes:
            evidence["overlap"] = {"departures": modes, "with": spec.get("with", {})}
        if overlap and spec.get("end") == "2026-07-31":
            evidence["stale_when_indexed"] = spec["end"]
        evidence.update(spec.get("evidence", {}))
        service = {"stops": 100, "routes": 10, "departures_per_day": departures}
        for n, tier in enumerate(spec.get("tiers", ("local",))):
            category = "primary" if tier == "local" else "secondary"
            edges.append(
                {
                    **_edge(
                        "muc",
                        feed_id,
                        tier,
                        category,
                        0.5,
                        cross=spec.get("cross_border", False),
                        service=service,
                    ),
                    # The first tier's routes run all of the feed's service.
                    "evidence": evidence if n == 0 else {**evidence, "overlap": None},
                }
            )
    path = write_partitioned_index(
        directory,
        feeds=records,
        places=[place("muc", kind, country_code="DE", name="Munich")],
        edges=edges,
        contained={i: spec.get("contained", []) for i, spec in feeds.items()},
        access={"providers": MUNICH_PROVIDERS if providers is None else providers},
    )
    return reader.read_index(path)


@pytest.mark.parametrize("overlap", [True, False], ids=["overlap", "no-overlap"])
def test_the_feed_table_ranks_and_explains_each_feed(tmp_path, monkeypatch, overlap):
    monkeypatch.setattr(transitio, "__version__", reader.MIN_READER_VERSIONS[11])
    munich = reader.place("muc", index=munich_index(tmp_path, overlap=overlap))
    table = munich.feeds(categories=None).to_dataframe().set_index("feed_id")
    assert (
        list(table.reset_index().columns)
        == (
            "feed_id name catalogue_name tiers relevance_category relevance "
            "cross_border no_tier_reason mark share_of_place departures_per_day "
            "stops routes modes covers repeats "
            "service_start service_end stale_when_indexed nominal_window "
            "contained_in access "
            "stop_count reason"
        ).split()
    )
    mvv, delfi = table.loc["f-mvv"], table.loc["f-delfi"]
    assert table["name"].isna()["f-mvv"]
    assert delfi["catalogue_name"].startswith("Registration")
    assert mvv["departures_per_day"] == 399375 and mvv["stop_count"] == 28330
    assert table.loc["f-old", "stale_when_indexed"] == datetime.date(2026, 7, 31)
    assert table.loc["f-tiny", "contained_in"] == ["f-bw"]
    # The departures summed over the feeds are 1,578,573; each counted once,
    # 405,314.3: f-delfi's, plus the share of f-bw's that f-delfi does not
    # run (5 % of 246).
    share = "25 % of the departures summed over the place's feeds"
    if not overlap:
        assert table["covers"].isna().all() and table["repeats"].isna().all()
        assert mvv["modes"] == [] and mvv["share_of_place"] == 399375 / 1578573
        assert mvv["reason"] == f"primary (local, regional tiers): {share}"
    else:
        assert mvv["covers"] == pytest.approx(399375 / 405314.3)
        assert mvv["modes"] == ["bus", "tram", "subway", "rail"]
        assert mvv["repeats"] == "f-delfi 100 %, f-urban 95 %, f-mvg 93 %"
        assert mvv["reason"] == (
            f"primary (local, regional tiers): {share}; repeats f-delfi (100 %)"
        )
        assert table.loc["f-tiny", "reason"] == (
            "primary (local tier): 0.0019 % of the departures summed over the "
            "place's feeds; repeats f-bw (100 %); contained in f-bw"
        )
    assert table.loc["f-old", "reason"] == (
        "primary (local tier): 0 % of the departures summed over the place's "
        "feeds; stale when indexed: its timetable ended 2026-07-31"
    )
    assert table.loc["f-mvg", "reason"].endswith("; needs a free account with MVG API")
    assert table.loc["f-rail", "reason"].endswith(
        "; needs credentials the index has no details for"
    )
