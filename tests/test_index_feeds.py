import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

pytest.importorskip("geopandas")
import geopandas  # noqa: E402
import pandas  # noqa: E402
import shapely  # noqa: E402

import transitio  # noqa: E402
from transitio import index as transitio_index  # noqa: E402
from transitio.exceptions import TransitioError  # noqa: E402
from transitio.index.places import _PlaceLookup  # noqa: E402


def _place_row(place_id, kind, name, **kw):
    return {
        "place_id": place_id,
        "kind": kind,
        "name": name,
        "names": {"en": name},
        "aliases": kw.get("aliases", []),
        "default_metro_id": kw.get("default_metro_id"),
        "parent_id": kw.get("parent_id"),
        "metro_ids": kw.get("metro_ids", []),
        "member_ids": kw.get("member_ids", []),
        "country_code": "US",
        "geometry": kw.get("geometry"),
    }


def _feed_row(feed_id, **kw):
    return {
        "feed_id": feed_id,
        "onestop_id": kw.get("onestop_id"),
        "name": kw.get("name", feed_id),
        "spec": kw.get("spec", "gtfs"),
        "coverage_source": kw.get("coverage_source", "declared"),
        "atlas": kw.get("atlas"),
        "snapshot": "snap-1",
    }


def _edge_row(place_id, feed_id, **kw):
    selector = kw.get("selector")
    return {
        "place_id": place_id,
        "feed_id": feed_id,
        "tier": kw.get("tier", "unknown"),
        "service": json.dumps(kw["service"]) if kw.get("service") else None,
        "tier_confidence": kw.get("tier_confidence", 0.0),
        "method": kw.get("method", "inferred"),
        "needs_review": kw.get("needs_review", True),
        "selector_state": kw.get("selector_state", "unavailable"),
        "selector": json.dumps(selector) if selector else None,
        "evidence": json.dumps({"declared_level": "municipality"}),
    }


BOX = shapely.box(-74.1, 40.6, -73.9, 40.9)
PLACES = [
    _place_row(
        "Q101",
        "city",
        "Gotham",
        parent_id="Q103",
        metro_ids=["Q102"],
        default_metro_id="Q102",
        geometry=BOX,
    ),
    _place_row("Q102", "metro", "Gotham metro", member_ids=["Q101"]),
    _place_row("Q103", "region", "Gotham State"),
    _place_row("Q104", "city", "Twinford"),
    _place_row("Q105", "city", "Twinford"),
]

LICENSE = {"spdx_identifier": "CC0-1.0", "redistribution_allowed": True}
FEEDS = [
    _feed_row("f-city", atlas=json.dumps({"license": LICENSE})),
    _feed_row("f-mix"),
    _feed_row("f-nat"),
    _feed_row("f-bike", spec="gbfs"),
]

# Every tier edge of a (place, feed) pair carries the pair's service level.
MIX_SERVICE = {"stops": 12, "routes": 2, "departures_per_day": 400.0}
EDGES = [
    # A declared city feed, propagated to region and metro.
    _edge_row("Q101", "f-city"),
    _edge_row("Q103", "f-city"),
    _edge_row("Q102", "f-city"),
    # A synthetic classified feed: local (complete selector) + national (whole).
    _edge_row(
        "Q102",
        "f-mix",
        tier="local",
        service=MIX_SERVICE,
        needs_review=False,
        selector_state="complete",
        selector={"route_id": ["r1", "r2"], "declared_as": {"agency_id": ["a1"]}},
    ),
    _edge_row("Q102", "f-bike"),
    _edge_row(
        "Q102",
        "f-mix",
        tier="national",
        service=MIX_SERVICE,
        needs_review=False,
        selector_state="whole_feed",
    ),
    _edge_row(
        "Q102",
        "f-nat",
        tier="national",
        service={"stops": 1, "routes": 1, "departures_per_day": 6.0},
        needs_review=False,
        selector_state="whole_feed",
    ),
    # The Twinford tiebreaker: three feeds vs one.
    _edge_row("Q104", "f-city"),
    _edge_row("Q104", "f-mix"),
    _edge_row("Q104", "f-nat"),
    _edge_row("Q105", "f-city"),
]


def _index(places=PLACES, feeds=FEEDS, edges=EDGES):
    return transitio_index.Index(
        {},
        pandas.DataFrame(feeds),
        geopandas.GeoDataFrame(places, geometry="geometry", crs="EPSG:4326"),
        pandas.DataFrame(edges),
    )


@pytest.fixture
def idx():
    return _index()


def test_a_bare_query_lists_every_feed_with_an_edge(idx):
    metro = transitio_index.place("Q102", index=idx)
    feeds = metro.feeds()
    assert [f.feed_id for f in feeds] == ["f-city", "f-mix", "f-nat"]
    declared = feeds[0]
    assert declared.tiers == frozenset({"unknown"})
    assert declared.service.stops is None  # declared: nothing measured
    assert declared.needs_review is True
    assert declared.selector.state == "unavailable"
    assert declared.coverage_source == "declared"


def test_a_tier_query_keeps_unknown_edges_flagged_by_default(idx):
    metro = transitio_index.place("Q102", index=idx)
    feeds = metro.feeds(tiers=["local"])
    assert [f.feed_id for f in feeds] == ["f-city", "f-mix"]  # f-nat dropped
    mix = feeds[1]
    assert mix.tiers == frozenset({"local"})
    assert mix.service.departures_per_day == 400.0
    assert mix.needs_review is False


def test_on_unknown_exclude_drops_unknown_only_feeds(idx):
    metro = transitio_index.place("Q102", index=idx)
    feeds = metro.feeds(tiers=["local"], on_unknown="exclude")
    assert [f.feed_id for f in feeds] == ["f-mix"]


def test_exclude_drops_a_feed_whose_every_edge_is_excluded(idx):
    metro = transitio_index.place("Q102", index=idx)
    feeds = metro.feeds(exclude=["national"])
    ids = [f.feed_id for f in feeds]
    assert "f-nat" not in ids
    mix = next(f for f in feeds if f.feed_id == "f-mix")
    # Only the local edge remains matched, so the aggregates follow it.
    assert mix.tiers == frozenset({"local"})
    assert mix.service.stops == 12


def test_selector_aggregation_follows_the_weakest_link(idx):
    metro = transitio_index.place("Q102", index=idx)
    mix = next(
        f for f in metro.feeds(tiers=["local", "national"]) if f.feed_id == "f-mix"
    )
    # local (complete) unioned with national (whole_feed) absorbs to whole_feed.
    assert mix.selector.state == "whole_feed"
    local_only = next(f for f in metro.feeds(tiers=["local"], on_unknown="exclude"))
    assert local_only.selector.state == "complete"
    assert local_only.selector.route_ids == ("r1", "r2")
    declared = next(f for f in metro.feeds() if f.feed_id == "f-city")
    assert declared.selector.state == "unavailable"


def test_spec_defaults_to_static_gtfs(idx):
    metro = transitio_index.place("Q102", index=idx)
    assert "f-bike" not in [f.feed_id for f in metro.feeds()]
    everything = [f.feed_id for f in metro.feeds(spec=None)]
    assert "f-bike" in everything
    assert [f.feed_id for f in metro.feeds(spec=["gbfs"])] == ["f-bike"]


def test_a_single_complete_selector_keeps_its_declared_as(idx):
    metro = transitio_index.place("Q102", index=idx)
    local_only = next(iter(metro.feeds(tiers=["local"], on_unknown="exclude")))
    assert local_only.selector.declared_as == {"agency_id": ["a1"]}


def test_malformed_selector_states_fail_safe_to_unavailable():
    edges = [
        _edge_row("Q102", "f-mix", tier="local", selector_state="bogus"),
        _edge_row(
            "Q102",
            "f-nat",
            tier="local",
            selector_state="complete",  # complete but carries no route ids
        ),
    ]
    idx = _index(edges=edges)
    metro = transitio_index.place("Q102", index=idx)
    for feed in metro.feeds():
        assert feed.selector.state == "unavailable"


def test_a_feed_carries_its_snapshot_id(idx):
    metro = transitio_index.place("Q102", index=idx)
    assert next(iter(metro.feeds())).snapshot == "snap-1"


def test_the_service_level_is_the_pairs_shared_struct(idx):
    # Whichever tier the query matched, the feed's service in the place is
    # the same struct; a place without published totals reports none.
    metro = transitio_index.place("Q102", index=idx)
    by_id = {f.feed_id: f for f in metro.feeds(tiers=["national"])}
    assert by_id["f-mix"].service.routes == 2
    assert by_id["f-nat"].service.departures_per_day == 6.0
    assert metro.service.feeds == 0
    assert metro.service.stops is None


def test_the_license_block_comes_from_the_atlas_record(idx):
    metro = transitio_index.place("Q102", index=idx)
    declared = next(f for f in metro.feeds() if f.feed_id == "f-city")
    assert declared.license == LICENSE
    assert next(f for f in metro.feeds() if f.feed_id == "f-mix").license is None


def test_to_geodataframe_tabulates_the_feeds(idx):
    metro = transitio_index.place("Q102", index=idx)
    frame = metro.feeds().to_geodataframe()
    assert len(frame) == 3
    assert set(frame["feed_id"]) == {"f-city", "f-mix", "f-nat"}
    assert frame.set_index("feed_id").loc["f-mix", "departures_per_day"] == 400.0
    # An empty result keeps the documented columns.
    empty = metro.feeds(spec=["nonexistent"]).to_geodataframe()
    assert len(empty) == 0
    assert "feed_id" in empty.columns
    assert "departures_per_day" in empty.columns


def test_a_bare_city_name_promotes_and_finds_the_declared_feed(idx):
    # The plan's offline guarantee: a city-only declared feed is reachable
    # through the city's default-metro query.
    place = transitio.place("Gotham", index=idx)
    assert place.id == "Q102"
    assert "f-city" in [f.feed_id for f in place.feeds()]


def test_feed_counts_break_a_name_tie_when_the_margin_is_met(idx):
    # Twinford A carries three feeds, B one: 3 > 2 x 1, so A wins outright.
    assert transitio_index.place("Twinford", index=idx).id == "Q104"


def test_an_edgeless_index_returns_no_feeds():
    idx = _index(edges=[])
    idx.edges = None
    metro = transitio_index.place("Q102", index=idx)
    assert metro.feeds() == []


def test_a_detached_lookup_refuses_feed_queries():
    frame = geopandas.GeoDataFrame(PLACES, geometry="geometry", crs="EPSG:4326")
    lookup = _PlaceLookup(frame)
    place = lookup.get("Q102")
    with pytest.raises(TransitioError, match="not attached"):
        place.feeds()


def test_an_invalid_on_unknown_value_is_refused(idx):
    metro = transitio_index.place("Q102", index=idx)
    with pytest.raises(ValueError, match="on_unknown"):
        metro.feeds(on_unknown="drop")
