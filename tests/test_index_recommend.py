"""Which of a place's feeds to use, and why the others are left out."""

import datetime

import pandas
import pytest

pytest.importorskip("geopandas")

import transitio  # noqa: E402
from transitio import index as reader  # noqa: E402
from index_fixture import covered_feed, write_partitioned_index  # noqa: E402
from test_index_views import (  # noqa: E402
    AREA_PLACES,
    MUNICH,
    MUNICH_PROVIDERS,
    _edge,
    munich_index,
)


@pytest.fixture(autouse=True)
def _reader_reads_schema_11(monkeypatch):
    monkeypatch.setattr(transitio, "__version__", reader.MIN_READER_VERSIONS[11])


# A timetable starting in November, a feed without overlap evidence and one
# the index has no timetable for.
LATER = {
    **MUNICH,
    "f-next": {
        "name": "Next",
        "modes": {"bus": 1000},
        "stops": 900,
        "start": "2026-11-01",
        "evidence": {"overlap": None},
    },
    "f-decl": {"name": "Declared", "stops": 50},
}
KEYS = {
    "f-mvv": {**MUNICH["f-mvv"], "access": "key", "provider": "mvv-api"},
    "f-urban": {**MUNICH["f-urban"], "access": "key", "provider": "paid"},
}
PROVIDERS = MUNICH_PROVIDERS + [
    {"provider_id": "mvv-api", "name": "MVV API", "free": True},
    {"provider_id": "paid", "name": "Urban Data", "free": False},
]
# A city feed without rail, a rail feed and a large feed repeating both.
SPLIT = {
    "f-city": {"name": "City", "modes": {"bus": 900, "tram": 100}, "stops": 500},
    "f-trains": {"name": "Trains", "modes": {"rail": 400}, "stops": 300},
    "f-both": {
        "name": "Both",
        "modes": {"bus": 450, "rail": 200},
        "stops": 5000,
        "with": {"f-city": {"bus": 1.0}, "f-trains": {"rail": 1.0}},
    },
}
TAKEN_UNCOMPARED = (
    "not compared: the index records no overlap for it, so what it adds is "
    "unknown; fetch leaves out the trips it repeats of the feeds before it"
)


@pytest.mark.parametrize(
    "feeds, when, overlap, taken, reasons",
    [
        pytest.param(
            LATER,
            "2026-10-13",
            True,
            ["f-tiny", "f-mvg", "f-mvv", "f-bw", "f-delfi"],
            {
                "f-tiny": "covers 0.0074 % of the place's departures (0.010 % of "
                "bus; 0 % of rail, subway and tram)",
                "f-mvg": "adds bus the feeds above lack: 95 % of the place's bus; "
                "needs a free account with MVG API",
                "f-mvv": "adds rail, subway and tram the feeds above lack: 13 % of "
                "the place's rail, subway and tram",
                "f-bw": "adds bus the feeds above lack: 0.028 % of the place's bus",
                "f-delfi": "adds bus the feeds above lack: 1 % of the place's bus",
                "f-urban": "repeats f-delfi (100 % of its departures)",
                "f-rail": "repeats f-mvv (100 % of its departures); needs "
                "credentials the index has no details for",
                "f-old": "stale when indexed: its timetable ended 2026-07-31",
                "f-next": "its timetable as indexed starts 2026-11-01, after "
                "2026-10-13",
                "f-decl": "not measured: the index has no timetable for it here",
            },
            id="munich",
        ),
        pytest.param(
            LATER,
            datetime.date(2026, 12, 5),
            True,
            ["f-tiny", "f-mvg", "f-bw", "f-delfi", "f-next"],
            {
                "f-delfi": "adds rail, subway and tram the feeds above lack: 15 % "
                "of the place's rail, subway and tram",
                "f-mvv": "its timetable as indexed ends 2026-11-30, before "
                "2026-12-05 (a newer download may run then)",
                "f-next": TAKEN_UNCOMPARED,
            },
            id="munich-in-december",
        ),
        pytest.param(
            SPLIT,
            "2026-10-13",
            True,
            ["f-trains", "f-city"],
            {
                "f-trains": "covers 28 % of the place's departures (0 % of bus; "
                "80 % of rail, subway and tram)",
                "f-city": "adds bus the feeds above lack: 100 % of the place's bus",
                "f-both": "repeats f-city and f-trains (100 % of its departures); "
                "5,000 stops against 800",
            },
            id="city-and-rail",
        ),
        pytest.param(
            LATER,
            "2026-10-13",
            False,
            ["f-tiny", "f-mvg", "f-mvv", "f-rail", "f-bw", "f-delfi", "f-urban"],
            {
                "f-mvv": "399,375 departures a day as indexed",
                "f-delfi": "405,302 departures a day as indexed",
                "f-rail": "17,190 departures a day as indexed; needs credentials "
                "the index has no details for",
                "f-tiny": "30 departures a day as indexed",
                "f-old": "stale when indexed: its timetable ended 2026-07-31",
            },
            id="no-overlap-evidence",
        ),
        pytest.param(
            {**MUNICH, **KEYS},
            "2026-10-13",
            True,
            ["f-tiny", "f-mvg", "f-mvv", "f-bw", "f-delfi"],
            {
                "f-mvv": "adds rail, subway and tram the feeds above lack: 13 % of "
                "the place's rail, subway and tram; needs a free account with MVV "
                "API",
                "f-urban": "needs a paid account with Urban Data",
            },
            id="key-feeds",
        ),
    ],
)
def test_a_recommendation_takes_what_each_feed_adds(
    tmp_path, feeds, when, overlap, taken, reasons
):
    index = munich_index(tmp_path, feeds, overlap=overlap, providers=PROVIDERS)
    found = reader.place("muc", index=index).recommend(when)
    assert found.feed_ids == taken
    assert found.when == datetime.date.fromisoformat(str(when))
    given = {c.feed.feed_id: c.reason for c in found.taken + found.left_out}
    assert sorted(given) == sorted(feeds)
    assert {feed_id: given[feed_id] for feed_id in reasons} == reasons
    table = found.to_dataframe()
    assert list(table["feed_id"]) == list(given) and table["taken"].sum() == len(taken)
    uncompared = [c for c in found.taken if c.reason == TAKEN_UNCOMPARED]
    if overlap:
        assert found.basis == "overlap"
        assert found.note == (
            "the coverage counts only the feeds compared; 1 feed is taken "
            "without comparison (1,000 departures a day)"
            if uncompared
            else None
        )
        if uncompared:
            # A cap that stops an uncompared feed counts it, not what it adds.
            capped = found.place.recommend(when, max_feeds=len(taken) - 1)
            stopped = {c.feed.feed_id: c.reason for c in capped.left_out}
            assert stopped[uncompared[0].feed.feed_id] == (
                "not compared: the index records no overlap for it; at most 4 "
                "feeds are taken"
            )
            assert capped.note == "at most 4 feeds are taken; 1 feed is left out for it"
    else:
        assert found.basis == "departures" and found.coverage is None
        assert str(found).splitlines()[:3] == [
            "Munich (city), 2026-10-13: take 7 feeds",
            "  considers local and regional feeds only, and those of unknown "
            "tier; tiers=None adds national and international feeds",
            "  this index records no overlap between feeds, so every feed is "
            "taken, the most local first; fetch leaves out the trips a feed "
            "repeats of the feeds before it",
        ]


def test_an_area_recommendation_sums_its_parts(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "transitio.pipeline._fetch._today", lambda: datetime.date(2026, 10, 13)
    )

    def served(place_id, feed_id, modes, shares=None):
        service = {"stops": 10, "routes": 1, "departures_per_day": sum(modes.values())}
        record = _edge(place_id, feed_id, "local", "primary", 0.5, service=service)
        overlap = {"departures": modes, "with": shares or {}}
        return {**record, "evidence": {"overlap": overlap}}

    # f-a runs the bus of both cities, f-b repeats it in k2, f-c is c1's tram
    # and f-d's timetable ended in January.
    edges = [
        served("c1", "f-a", {"bus": 600}),
        served("k2", "f-a", {"bus": 400}),
        served("k2", "f-b", {"bus": 400}, {"f-a": {"bus": 1.0}}),
        served("c1", "f-c", {"tram": 50}),
        served("c1", "f-d", {"bus": 10}),
    ]
    feeds = [
        {
            **covered_feed(feed_id),
            "home_country": "AA",
            "service_end": "2026-01-31" if feed_id == "f-d" else None,
        }
        for feed_id in ("f-a", "f-b", "f-c", "f-d")
    ]
    path = write_partitioned_index(
        tmp_path, feeds=feeds, places=AREA_PLACES, edges=edges, access={}
    )
    area = reader.area((2, 0, 2.8, 0.4), index=reader.read_index(path))
    found = area.recommend()
    assert found.feed_ids == ["f-a", "f-c"] and found.when is None
    assert found.coverage == pytest.approx(1.0)
    assert str(found).splitlines() == [
        "The area's 2 places: take 2 feeds, covering about 100 % of the "
        "departures the index records there, each counted once",
        "  considers local and regional feeds only, and those of unknown tier; "
        "tiers=None adds national and international feeds",
        "  + f-a: covers 95 % of the place's departures (100 % of bus; 0 % of "
        "rail, subway and tram)",
        "  + f-c: adds rail, subway and tram the feeds above lack: 100 % of the "
        "place's rail, subway and tram",
        "  - f-b: repeats f-a (100 % of its departures)",
        "  - f-d: its timetable as indexed ends 2026-01-31, before 2026-10-13 "
        "(a newer download may run then)",
    ]
    # A cap stops the selection while the tram feed still adds departures.
    assert area.recommend(max_feeds=1).note == (
        "at most 1 feed is taken; 1 feed is left out for it"
    )
    wrongs = ({"target": 0}, {"target": 1.5}, {"max_feeds": 0}, {"max_feeds": 1.5})
    for wrong in wrongs + ({"when": "soon"},):
        with pytest.raises(ValueError):
            area.recommend(**wrong)


def test_a_recommendation_considers_the_tiers_of_its_kind_of_place(tmp_path):
    # A national train feed, of which no local or regional feed runs a trip,
    # and a cross-border tram of its own.
    ice = {"name": "ICE", "tiers": ("national",), "modes": {"rail": 40}, "stops": 30}
    tram = {"name": "Tram", "modes": {"tram": 60}, "stops": 20, "cross_border": True}
    feeds = {**MUNICH, "f-ice": ice, "f-tram": tram}
    index = munich_index(tmp_path / "city", feeds, providers=PROVIDERS)
    munich = reader.place("muc", index=index)
    found = munich.recommend("2026-10-13")
    assert found.tiers == ("local", "regional")
    assert found.feed_ids == ["f-tiny", "f-mvg", "f-mvv", "f-bw", "f-delfi", "f-tram"]
    given = {c.feed.feed_id: c.reason for c in found.left_out}
    assert given["f-ice"] == "not considered: serves the place at the national tier"
    every = munich.recommend("2026-10-13", tiers=None)
    assert every.tiers is None and "f-ice" in every.feed_ids
    assert "considers" not in str(every)
    # A region considers every tier by default.
    index = munich_index(tmp_path / "region", feeds, providers=PROVIDERS, kind="region")
    region = reader.place("muc", index=index).recommend("2026-10-13")
    assert region.tiers is None and "f-ice" in region.feed_ids
    # Tiers left out by exclude are not said to be considered.
    local = munich.recommend("2026-10-13", exclude="regional")
    assert local.tiers == ("local",)
    for once in (iter(["regional"]), pandas.Index(["regional"])):
        assert str(munich.recommend("2026-10-13", exclude=once)) == str(local)
    assert str(munich.recommend("2026-10-13", tiers=iter(found.tiers))) == str(found)
    assert str(local).splitlines()[1] == (
        "  considers local feeds only, and those of unknown tier; tiers=None adds "
        "national and international feeds"
    )
    named = munich.recommend("2026-10-13", tiers=None, exclude=["unknown"])
    assert named.tiers == ("local", "regional", "national", "international")
    assert str(named).splitlines()[1] == (
        "  considers local, regional, national and international feeds only"
    )
    # A target stops the selection once the feeds taken reach it.
    least = munich.recommend("2026-10-13", target=0.95)
    assert least.feed_ids == ["f-tiny", "f-mvg", "f-mvv"]
    assert least.note == (
        "the feeds taken reach the target of 95 % of the departures; 4 feeds are "
        "left out for it"
    )
    # A cap that stops short of the target says so, not that nothing adds more.
    short = munich.recommend("2026-10-13", target=0.95, max_feeds=2)
    assert short.note == (
        "at most 2 feeds are taken; 6 feeds are left out for it; the feeds taken "
        "cover 92 % of the departures, short of 95 % of them"
    )
    # Without overlap evidence a cap is said as well.
    flat = munich_index(tmp_path / "flat", overlap=False, providers=PROVIDERS)
    capped = reader.place("muc", index=flat).recommend("2026-10-13", max_feeds=2)
    assert capped.feed_ids == ["f-tiny", "f-mvg"]
    assert capped.note == (
        "this index records no overlap between feeds, so at most 2 are taken, the "
        "most local first, and 5 feeds are left out; fetch leaves out the trips a "
        "feed repeats of the feeds before it"
    )
    assert {c.feed.feed_id: c.reason for c in capped.left_out}["f-bw"] == (
        "not compared: the index records no overlap for it; at most 2 feeds are "
        "taken"
    )
    assert {c.feed.feed_id: c.reason for c in least.left_out}["f-delfi"] == (
        "adds 1 % of the place's departures; the feeds taken reach the target "
        "without it"
    )


def test_a_feed_after_an_uncompared_one_adds_to_the_compared_feeds(tmp_path):
    feeds = {**LATER, "f-next": {**LATER["f-next"], "extent": 0.01}}
    index = munich_index(tmp_path, feeds, providers=PROVIDERS)
    found = reader.place("muc", index=index).recommend("2026-12-05")
    assert found.feed_ids[:3] == ["f-next", "f-tiny", "f-mvg"]
    assert found.taken[2].reason == (
        "adds bus the compared feeds above lack: 95 % of the place's bus; needs a "
        "free account with MVG API"
    )


def test_a_feed_the_index_marks_is_not_considered(tmp_path):
    uiuc = {"name": "UIUC", "modes": {"bus": 30}, "stops": 20}
    feeds = {**MUNICH, "f-uiuc": {**uiuc, "evidence": {"mark": "compilation"}}}
    munich = reader.place("muc", index=munich_index(tmp_path, feeds))
    found = munich.recommend("2026-10-13")
    assert {c.feed.feed_id: c.reason for c in found.left_out}["f-uiuc"] == (
        "unofficial compilation, not considered: fetch takes it when named in " "feeds="
    )
    table = munich.feeds().to_dataframe().set_index("feed_id")
    assert table["mark"].dropna().to_dict() == {"f-uiuc": "compilation"}
    assert table.loc["f-uiuc", "reason"].endswith("; unofficial compilation")


@pytest.mark.parametrize(
    "marks, expected",
    [
        ((["x"], {"y": 1}, "other", None), None),
        (([], "private"), "private"),
    ],
    ids=["unknown-values", "after-an-unknown-one"],
)
def test_a_mark_the_reader_does_not_know_counts_as_none(marks, expected):
    from types import SimpleNamespace

    from transitio.index.feeds import IndexedFeed

    edges = {n: SimpleNamespace(summary={"mark": m}) for n, m in enumerate(marks)}
    assert IndexedFeed.mark.fget(SimpleNamespace(edges=edges)) == expected


def test_an_unreadable_coverage_orders_a_feed_last():
    from types import SimpleNamespace

    import shapely

    from transitio.index.recommend import _area

    nan = shapely.Polygon([(0, 0), (float("nan"), 0), (1, 1)])
    for coverage in (None, b"junk", shapely.Point().wkb, nan.wkb):
        assert _area(SimpleNamespace(coverage=coverage)) is None
    assert _area(SimpleNamespace(coverage=shapely.box(0, 0, 1, 2).wkb)) == 2
