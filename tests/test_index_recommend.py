"""Which of a place's feeds to use, and why the others are left out."""

import datetime

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
CARRIED = {**MUNICH["f-mvg"], "evidence": {"carried_by": {"f-mvv": ["local"]}}}
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
NOT_COMPARED = "not compared: the index records no overlap for it"


@pytest.mark.parametrize(
    "feeds, when, overlap, taken, reasons",
    [
        pytest.param(
            {**LATER, "f-mvg": CARRIED},
            "2026-10-13",
            True,
            ["f-mvv"],
            {
                "f-delfi": "repeats f-mvv (98 % of its departures); "
                "550,396 stops against 28,330",
                "f-mvg": "carried by f-mvv; needs a free account with MVG API",
                "f-bw": "adds too little: 0.021 % of the place's departures",
                "f-tiny": "contained in f-bw",
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
            ["f-delfi"],
            {
                "f-delfi": "covers over 99 % of the place's departures (over 99 % "
                "of bus; over 99 % of rail, subway and tram)",
                "f-mvv": "its timetable as indexed ends 2026-11-30, before "
                "2026-12-05 (a newer download may run then)",
                "f-mvg": "repeats f-delfi (100 % of its departures); "
                "needs a free account with MVG API",
                "f-next": NOT_COMPARED,
            },
            id="munich-in-december",
        ),
        pytest.param(
            SPLIT,
            "2026-10-13",
            True,
            ["f-city", "f-trains"],
            {
                "f-city": "covers 71 % of the place's departures (100 % of bus; "
                "20 % of rail, subway and tram)",
                "f-trains": "adds rail, subway and tram the feeds above lack: "
                "80 % of the place's rail, subway and tram",
                "f-both": "repeats f-city and f-trains (100 % of its departures); "
                "5,000 stops against 800",
            },
            id="city-and-rail",
        ),
        pytest.param(
            LATER,
            "2026-10-13",
            False,
            ["f-mvv"],
            {
                "f-mvv": "399,375 departures a day as indexed",
                "f-delfi": NOT_COMPARED,
                "f-rail": f"{NOT_COMPARED}; needs credentials the index has no "
                "details for",
                "f-bw": "adds too little: 0.016 % of the departures summed over "
                "the place's feeds",
                "f-tiny": "contained in f-bw",
                "f-old": "stale when indexed: its timetable ended 2026-07-31",
            },
            id="no-overlap-evidence",
        ),
        pytest.param(
            {**MUNICH, **KEYS},
            "2026-10-13",
            True,
            ["f-delfi"],
            {
                "f-mvv": "repeats f-delfi (100 % of its departures); "
                "needs a free account with MVV API",
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
    uncompared = [c for c in found.left_out if c.reason.startswith(NOT_COMPARED)]
    if overlap:
        assert found.basis == "overlap"
        assert found.note == (
            "the coverage leaves out 1 feed not compared with the others "
            "(1,000 departures a day)"
            if uncompared
            else None
        )
    else:
        assert found.basis == "departures" and found.coverage is None
        assert str(found).startswith(
            "Munich (city), 2026-10-13: this index records no overlap between "
            "feeds; taking the feed with the most departures, or a smaller one "
            "within 2 % of it: f-mvv (399,375 departures a day as indexed)\n"
        )


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
        "  + f-a: covers 95 % of the place's departures (100 % of bus; 0 % of "
        "rail, subway and tram)",
        "  + f-c: adds rail, subway and tram the feeds above lack: 100 % of the "
        "place's rail, subway and tram",
        "  - f-b: repeats f-a (100 % of its departures)",
        "  - f-d: its timetable as indexed ends 2026-01-31, before 2026-10-13 "
        "(a newer download may run then)",
    ]
    # One feed covers 95 % of the departures but none of the tram.
    assert area.recommend(max_feeds=1).note == (
        "the feeds taken cover 95 % of the departures, short of 80 % of rail, "
        "subway and tram: at most 1 feed is taken"
    )
    wrongs = ({"target": 0}, {"target": 1.5}, {"max_feeds": 0}, {"max_feeds": 1.5})
    for wrong in wrongs + ({"when": "soon"},):
        with pytest.raises(ValueError):
            area.recommend(**wrong)
