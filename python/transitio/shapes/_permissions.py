"""Bus drivability from OSM tags — the PSV access hierarchy.

Which ways a bus may legally drive, resolved per way down
access → vehicle → motor_vehicle → psv → bus: never the car set,
because buses both gain car-forbidden ways (bus-only streets, busways,
bus gates) and lose car-drivable ways tagged bus=no/psv=no.

The highway class is a gate the generic level does not open. An
explicit value at vehicle or below names a class a bus belongs to and
overrides outright, in both directions; a generic access value denies
when it denies but does not grant motor traffic on a way whose type
excludes it — access=yes on a footway or path says the public may use
it, not that a bus may drive it.

Barrier nodes resolve the same hierarchy and block unless an explicit
allow opens them — over-blocking only costs a detour, while
under-blocking would legalise an impossible path.
"""

import numpy as np

from transitio.shapes import _stitch

HIGHWAY_DEFAULTS = {
    "footway": (True, False, False),
    "pedestrian": (True, False, False),
    "steps": (True, False, False),
    "corridor": (True, False, False),
    "platform": (True, False, False),
    "path": (True, True, False),
    "cycleway": (False, True, False),
    "bridleway": (True, False, False),
    "track": (True, True, True),
    "living_street": (True, True, True),
    "residential": (True, True, True),
    "service": (True, True, True),
    "unclassified": (True, True, True),
    "tertiary": (True, True, True),
    "tertiary_link": (True, True, True),
    "secondary": (True, True, True),
    "secondary_link": (True, True, True),
    "primary": (True, True, True),
    "primary_link": (True, True, True),
    "trunk": (False, False, True),
    "trunk_link": (False, False, True),
    "elevator": (True, True, False),
    "road": (True, True, True),
    "busway": (False, False, False),
    "motorway": (False, False, True),
    "motorway_link": (False, False, True),
}


_ALLOWED_ACCESS = frozenset({"yes", "designated", "permissive", "official"})
"""Values that permit ordinary through travel."""

_CONDITIONAL_ACCESS = frozenset({"destination", "customers", "delivery"})
"""Values that permit access only FOR a purpose. A shortest path over
such a way would use it as a through shortcut it does not grant, so
they deny here unless a more specific PSV/bus grant applies — the
route a bus actually runs is the evidence, and tier matching never
needs these ways to connect two other streets."""
"""Access values that permit routing: explicit allow, plus destination /
customers (reachable, just usage-restricted) treated as allowed."""

_DENIED_ACCESS = frozenset(
    {
        "no",
        "private",
        "use_sidepath",
        "dismount",
        # Restrictive values that are not general-public access: routable only
        # for their stated purpose, so denied for general routing here (a
        # mode-specific foot=/bicycle= tag still overrides).
        "delivery",
        "agricultural",
        "forestry",
        "permit",
        "military",
    }
)
"""Access values that deny general routing. `dismount`/`use_sidepath` are
handled specially for bicycle before this set is consulted; for foot they
deny."""


_FALSE_ONEWAY = frozenset({"no", "false", "0"})


_KNOWN_ONEWAY = frozenset({"yes", "true", "1", "-1", "reverse", "no", "false", "0"})
"""Oneway values the graph can model. Anything else — time-varying
(``alternating``, ``reversible``) or malformed — fails closed."""

_BUS_HIGHWAYS = frozenset({"busway", "bus_guideway"})
"""Highway classes that are bus-drivable by definition."""

UNBUSABLE_FILTER = {
    "area": ["yes", "true", "1"],
    "highway": [
        "abandoned",
        "construction",
        "no",
        "planned",
        "platform",
        "proposed",
        "raceway",
        "razed",
        "rest_area",
        "services",
        "bridleway",
        "corridor",
        "cycleway",
        "elevator",
        "escalator",
        "steps",
    ],
}
"""The exclusion filter for the bus-graph extraction: pyrosm's
driving+service highway exclusions with ``bus_guideway`` retained,
WITHOUT pyrosm's access/motor_vehicle exclusions (an
``access=no`` + ``bus=yes`` street must reach the permission
resolver, not vanish at extraction), and WITHOUT the
footway/pedestrian/path/track classes — pedestrianised transit
streets carry explicit ``bus=``/``psv=`` grants that only the
permission chain can honour; untagged ways of those classes are
denied there instead."""


def _bus_permission(bus_default, access, vehicle, motor_vehicle, psv, bus):
    """Bus permission down the OSM access hierarchy, and whether the
    deciding value was unrecognised.

    Specificity decides. The highway type's default starts it. A
    generic ``access`` allow does NOT grant a way whose type excludes
    motor traffic — ``access=yes`` on a footway means the public may
    walk there — but it does deny when it denies. Every key below it
    names a class a bus belongs to (``vehicle`` → ``motor_vehicle`` →
    ``psv`` → ``bus``), so each explicit value there overrides
    whatever came before, in both directions: that is what makes
    ``highway=track`` + ``motor_vehicle=yes`` drivable and
    ``access=no`` + ``bus=yes`` a bus-only street.

    Returns ``(allowed, unresolved)``. ``unresolved`` is true when the
    most specific value present is one the model does not recognise —
    the way's legality is then unknown and the caller drops it.
    """
    allowed = bus_default
    unresolved = False
    # The generic level: a deny propagates, an allow does not upgrade.
    if access is not None:
        if access in _DENIED_ACCESS or access in _CONDITIONAL_ACCESS:
            allowed = False
            unresolved = False
        elif access in _ALLOWED_ACCESS:
            unresolved = False
        else:
            unresolved = True
    # The vehicle-class levels: explicit values override outright.
    for value in (vehicle, motor_vehicle, psv, bus):
        if value is None:
            continue
        if value in _ALLOWED_ACCESS:
            allowed = True
            unresolved = False
        elif value in _DENIED_ACCESS or value in _CONDITIONAL_ACCESS:
            allowed = False
            unresolved = False
        else:
            unresolved = True
    return allowed, unresolved


def bus_permissions(edges):
    """Per-edge ``(forward, reverse)`` bus drivability, and diagnostics.

    ``edges`` is a pyrosm network frame (tags as columns). The forward
    direction runs along the stored geometry. Buses follow the base
    ``oneway`` strictly, with only the explicit ``oneway:bus=`` /
    ``oneway:psv=`` exemptions honoured — contraflow bus-lane
    inference from ``busway:*`` tags is deliberately out of scope, so
    a missing exemption merely costs a detour, never legality.
    """
    columns = {
        tag: _column(edges, tag)
        for tag in (
            "highway",
            "access",
            "vehicle",
            "motor_vehicle",
            "psv",
            "bus",
            "oneway",
            "oneway:bus",
            "oneway:psv",
            "junction",
        )
    }
    n = len(edges)
    forward = np.zeros(n, dtype=bool)
    reverse = np.zeros(n, dtype=bool)
    unknown_access = 0
    unknown_highway = 0
    for i in range(n):
        highway = columns["highway"][i]
        if highway in _BUS_HIGHWAYS:
            bus_default = True
        elif highway == "track":
            # Cars default onto tracks; buses never do without an
            # explicit grant.
            bus_default = False
        elif highway in HIGHWAY_DEFAULTS:
            bus_default = HIGHWAY_DEFAULTS[highway][2]
        else:
            bus_default = False
            unknown_highway += int(highway is not None)
        allowed, unresolved = _bus_permission(
            bus_default,
            columns["access"][i],
            columns["vehicle"][i],
            columns["motor_vehicle"][i],
            columns["psv"][i],
            columns["bus"][i],
        )
        unknown_access += int(unresolved)
        # An unrecognised deciding value leaves legality unknown: the
        # way is dropped, never assumed drivable.
        if not allowed or unresolved:
            continue
        oneway = columns["oneway"][i]
        # One rule for graph and relation routing alike: explicit
        # values, roundabout AND circular junctions, motorways AND
        # their links.
        direction = _stitch.effective_direction(
            {
                "oneway": oneway,
                "junction": columns["junction"][i],
                "highway": highway,
            }
        )
        if direction is None:
            # A value the model cannot express (``alternating``,
            # ``reversible``, a typo): its legal direction is unknown,
            # so the way is dropped rather than assumed bidirectional.
            unknown_access += 1
            continue
        reversed_oneway = direction == -1
        is_oneway = direction != 0
        forward[i] = not (is_oneway and reversed_oneway)
        reverse[i] = not (is_oneway and not reversed_oneway)
        exemption = columns["oneway:bus"][i]
        if exemption is None:
            exemption = columns["oneway:psv"][i]
        if exemption is not None and exemption not in _KNOWN_ONEWAY:
            # An exemption the graph cannot model leaves the direction
            # unresolved: drop rather than legalise a guess.
            unknown_access += 1
            forward[i] = reverse[i] = False
        elif exemption in _FALSE_ONEWAY:
            forward[i] = reverse[i] = True
        elif exemption in ("yes", "true", "1"):
            forward[i], reverse[i] = True, False
        elif exemption in ("-1", "reverse"):
            forward[i], reverse[i] = False, True
    diagnostics = {
        "unknown_access": unknown_access,
        "unknown_highway": unknown_highway,
    }
    return forward, reverse, diagnostics


_BARRIER_MOTOR_PERVIOUS = frozenset(
    {"cattle_grid", "toll_booth", "border_control", "entrance"}
)
"""Barrier types motor traffic passes freely by default. Everything
else — gates, bollards, and every unrecognised type — blocks unless an
explicit allow opens it: over-blocking only drops a pattern to the
next ladder tier, while under-blocking would legalise a wrong path."""


def bus_barrier_blocks(barrier, tags):
    """Whether a barrier node splits the bus graph.

    The most specific present access value among ``bus`` → ``psv`` →
    ``motor_vehicle`` → ``vehicle`` → ``access`` decides: an explicit
    allow opens the barrier, anything else (including an explicit
    deny) blocks; with no access tags, only the motor-pervious barrier
    types pass.
    """
    for key in ("bus", "psv", "motor_vehicle", "vehicle", "access"):
        value = tags.get(key)
        if value is None:
            continue
        return value not in _ALLOWED_ACCESS
    return barrier not in _BARRIER_MOTOR_PERVIOUS


def _column(edges, name):
    """A way-tag column as an object array with missing values as `None`, or
    all-`None` when pyrosm dropped the column (a tag absent everywhere in the
    extract yields no column). pyrosm's out-of-core engine returns string
    columns whose missing entries are the literal string ``"nan"`` (and float
    ``NaN`` on the in-memory path), both normalised to `None` here so the plain
    ``is None`` checks downstream are correct."""
    if name not in edges.columns:
        return np.full(len(edges), None, dtype=object)
    values = edges[name].to_numpy(dtype=object)
    return np.array(
        [
            (
                None
                if v is None
                or v == ""
                or v == "nan"
                or (isinstance(v, float) and v != v)
                else v
            )
            for v in values
        ],
        dtype=object,
    )
