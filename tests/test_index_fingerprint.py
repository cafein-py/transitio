"""The classification fingerprint's canonicalisation."""

import pytest

from transitio.index import fingerprint

ROUTES = {
    "r1": {"route_type": 3, "agency_id": "a"},
    "r2": {"route_type": None, "agency_id": ""},
}
COORDS = {"s1": (24.9384, 60.1699), "s2": (24.95, 60.17)}
SERVED = {"r1": {"s2", "s1"}, "r2": {"s2"}}


def test_the_kinds_differ_and_both_see_a_moved_stop():
    # Same route ids throughout: only the stop geography changes, which is
    # exactly the case route ids alone would miss.
    route_stops = fingerprint.compute("route_stops", ROUTES, COORDS, SERVED)
    feed_stops = fingerprint.compute("feed_stops", ROUTES, COORDS)
    assert route_stops != feed_stops
    moved = {**COORDS, "s2": (25.1, 60.17)}
    assert fingerprint.compute("route_stops", ROUTES, moved, SERVED) != route_stops
    assert fingerprint.compute("feed_stops", ROUTES, moved) != feed_stops
    # A route serving one stop fewer changes the strong kind only.
    fewer = {**SERVED, "r1": {"s1"}}
    assert fingerprint.compute("route_stops", ROUTES, COORDS, fewer) != route_stops
    assert fingerprint.compute("feed_stops", ROUTES, COORDS) == feed_stops


def test_coordinates_are_rounded_and_order_is_canonical():
    jitter = {"s2": (24.950000004, 60.17), "s1": (24.9384, 60.16990000001)}
    assert fingerprint.compute("feed_stops", ROUTES, jitter) == fingerprint.compute(
        "feed_stops", dict(reversed(list(ROUTES.items()))), COORDS
    )
    # Negative zero and zero spell the same.
    assert fingerprint.compute("feed_stops", {}, {"s": (-0.0, 0.0)}) == (
        fingerprint.compute("feed_stops", {}, {"s": (0.0, 0.0)})
    )


@pytest.mark.parametrize(
    ("kind", "served", "message"),
    [
        ("stops", SERVED, "unknown fingerprint kind"),
        ("route_stops", None, "served stops"),
    ],
)
def test_bad_inputs_are_refused(kind, served, message):
    with pytest.raises(ValueError, match=message):
        fingerprint.compute(kind, ROUTES, COORDS, served)
