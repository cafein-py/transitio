"""Eight reference places against what their operators and transport
authorities publish.

``data/reference_places.json`` lists, for each place, the services a
researcher expects there: the feeds that should deliver each (``via``, every
one taken; ``via_any``, at least one), the feeds that should be left out and
the kind of reason (``left_out``), the feeds only to be recorded at the place
(``listed``), the tiers a feed should carry (``tiers``), the routes a cropped
feed should keep (``routes``) and what the index lacks (``gap``, reported as
a skip). Every feed recorded for a reference place, and for each place of
``sample``, must appear in the recommendation, taken or left out with a
reason.

The study day is the index's own: the first Wednesday after it was built. A
service may name a day of its own (``day``), such as one in the past.
Whether a feed's timetable as indexed runs on the day, and whether the
index measured one, belongs to the index, not to the place: a feed to be
left out may be left out as out of date or without a timetable, whatever
kind of reason ``left_out`` names, and a feed that should deliver a service
may be left out as out of date, the check then being skipped with its
reason.

Known defects are named where they occur, each as ``[problem, defect]``:
the problem observed (``PROBLEMS``) and the defect behind it. ``fails`` maps
a feed (or ``via_any``) to one, ``tiers_fails`` a feed and ``routes_fails`` a
route; ``disappears_fails`` and the ``sample`` values name the defect behind
cross-border feeds missing from a recommendation. A check fails on any
problem the data does not name, or names as another problem; it is an
expected failure while named ones remain, and a named one that no longer
occurs is reported as a warning. With ``TRANSITIO_REFERENCE_RAW`` set no
defect is named, so each check reports its outcome as it stands, on
whichever index it reads.

The checks read the newest published snapshot, installed into
``cache/reference-index`` when ``TRANSITIO_REFERENCE_PLACES`` is set, or the
installed copy that ``TRANSITIO_REFERENCE_INDEX`` names. The crop checks
download feeds and run only with ``TRANSITIO_REFERENCE_FETCH`` set.
"""

import csv
import datetime
import io
import json
import os
import pathlib
import re
import warnings
import zipfile

import pytest

DATA = pathlib.Path(__file__).parent / "data"
REFERENCE = json.loads((DATA / "reference_places.json").read_text(encoding="utf-8"))
ENABLE = "TRANSITIO_REFERENCE_PLACES"
INSTALLED = "TRANSITIO_REFERENCE_INDEX"
FETCH = "TRANSITIO_REFERENCE_FETCH"
RAW = "TRANSITIO_REFERENCE_RAW"

# The weekday of the study day (Monday is 0).
STUDY_WEEKDAY = 2

# The kind of a left-out feed's reason, by the reason's opening words.
KINDS = (
    ("not considered", "not considered"),
    ("private or campus", "private"),
    ("unofficial compilation", "compilation"),
    ("repeats ", "duplicate"),
    ("contained in ", "duplicate"),
    ("stale when indexed", "out of date"),
    ("its timetable as indexed", "out of date"),
    ("not measured", "no timetable"),
)


# The problems a check observes.
PROBLEMS = (
    "not at the place",
    "left out",
    "not in the result",
    "none taken",
    "taken",
    "another reason",
    "missing tier",
    "cut off",
)


def _kind(reason):
    return next((kind for start, kind in KINDS if reason.startswith(start)), None)


def _out_of_date(reason):
    return _kind(reason or "") == "out of date"


def _timetable_reason(reason):
    """Whether ``reason`` is the timetable's: out of date or not measured."""
    return _kind(reason or "") in ("out of date", "no timetable")


def _study_day(index):
    """The first ``STUDY_WEEKDAY`` after the day ``index`` was built."""
    built = datetime.date.fromisoformat(index.snapshot["built_at"][:10])
    ahead = (STUDY_WEEKDAY - built.weekday()) % 7 or 7
    return built + datetime.timedelta(days=ahead)


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48]


def _named(entries):
    """The known defects as data names them, ``{key: (problem, defect)}``;
    none in a raw run."""
    if os.environ.get(RAW):
        return {}
    return {key: tuple(entry) for key, entry in (entries or {}).items()}


def _judge(problems, known, gap=None):
    """Fail on a problem (``{key: (problem, text)}``) that ``known`` does not
    name as that problem, and warn of a named one gone; while named ones
    remain, an expected failure; else skip a ``gap``."""
    unexpected = [
        f"{key}: {text}"
        for key, (problem, text) in sorted(problems.items())
        if known.get(key, (None,))[0] != problem
    ]
    assert not unexpected, "; ".join(unexpected)
    gone = sorted(set(known) - set(problems))
    if gone:
        warnings.warn(f"named defects no longer occur: {', '.join(gone)}", stacklevel=2)
    remaining = {defect for key, (_, defect) in known.items() if key in problems}
    if remaining:
        pytest.xfail("; ".join(sorted(remaining)))
    if gap:
        pytest.skip(f"not in the index: {gap}")


def _service_params(check):
    return [
        pytest.param(
            place, service, id=f"{_slug(place['name'])}/{_slug(service['service'])}"
        )
        for place in REFERENCE["places"]
        for service in place["services"]
        if check is None or check in service
    ]


def _place_params():
    params = [
        pytest.param(place["place"], id=_slug(place["name"]))
        for place in REFERENCE["places"]
    ]
    params += [pytest.param(place_id, id=place_id) for place_id in REFERENCE["sample"]]
    return params


def _disappears_defect(place_id):
    for place in REFERENCE["places"]:
        if place["place"] == place_id:
            return place.get("disappears_fails")
    return REFERENCE["sample"].get(place_id)


def service_problems(place, found, service):
    """``{feed id or "via_any": (problem, text)}`` for one service: a feed it
    names not recorded at the place, one to take left out (other than as out
    of date) or not in the result, none of ``via_any`` taken and not all of
    them out of date, or one to leave out taken, not in the result or left
    out for another kind of reason than named or the timetable's."""
    listed = {f.feed_id for f in place.feeds(categories=None, international=True)}
    taken = set(found.feed_ids)
    reasons = {choice.feed.feed_id: choice.reason for choice in found.left_out}
    named = {
        *service.get("via", ()),
        *service.get("via_any", ()),
        *service.get("left_out", {}),
        *service.get("listed", ()),
    }
    problems = {f: ("not at the place", "not at the place") for f in named - listed}
    for feed_id in service.get("via", ()):
        reason = reasons.get(feed_id)
        if feed_id in listed and feed_id not in taken and not _out_of_date(reason):
            problem = "left out" if reason else "not in the result"
            problems[feed_id] = (problem, reason or "not in the result")
    any_of = service.get("via_any")
    if (
        any_of
        and not taken.intersection(any_of)
        and not all(_out_of_date(reasons.get(f)) for f in any_of)
    ):
        problems["via_any"] = (
            "none taken",
            "; ".join(f"{f} {reasons.get(f, 'not in the result')}" for f in any_of),
        )
    for feed_id, kinds in service.get("left_out", {}).items():
        kinds = [kinds] if isinstance(kinds, str) else kinds
        reason = "taken" if feed_id in taken else reasons.get(feed_id)
        if (
            feed_id in listed
            and not _timetable_reason(reason)
            and _kind(reason or "") not in kinds
        ):
            problem = (
                "taken"
                if feed_id in taken
                else "another reason" if reason else "not in the result"
            )
            expected = " or ".join(kinds)
            problems[feed_id] = (
                problem,
                f"{reason or 'not in the result'}, expected {expected}",
            )
    return problems


def out_of_date_gaps(found, service):
    """``"<feed id> <reason>"`` for each feed that should deliver the service
    and is left out as out of date: of ``via``, and of ``via_any`` when none
    of them is taken."""
    reasons = {choice.feed.feed_id: choice.reason for choice in found.left_out}
    named = list(service.get("via", ()))
    any_of = service.get("via_any") or ()
    if not set(found.feed_ids).intersection(any_of):
        named += any_of
    return [f"{f} {reasons[f]}" for f in named if _out_of_date(reasons.get(f))]


def tier_problems(place, service):
    """``{feed id: (problem, text)}`` for each named feed lacking an
    expected tier."""
    feeds = {f.feed_id: f for f in place.feeds(categories=None, international=True)}
    problems = {}
    for feed_id, tiers in service["tiers"].items():
        have = feeds[feed_id].tiers if feed_id in feeds else frozenset()
        if not have.issuperset(tiers):
            problems[feed_id] = (
                "missing tier",
                f"{', '.join(sorted(have)) or 'no edge'}, expected {', '.join(tiers)}",
            )
    return problems


def missing_problems(place, found):
    """``{feed id or "cross-border": (problem, text)}`` for the recorded feeds
    absent from the recommendation, the cross-border ones together."""
    feeds = {f.feed_id: f for f in place.feeds(categories=None, international=True)}
    accounted = {choice.feed.feed_id for choice in found.taken + found.left_out}
    missing = sorted(set(feeds) - accounted)
    problems = {
        f: ("not in the result", "not in the result")
        for f in missing
        if not feeds[f].cross_border
    }
    crossing = [f for f in missing if feeds[f].cross_border]
    if crossing:
        problems["cross-border"] = (
            "not in the result",
            f"not in the result: {', '.join(crossing)}",
        )
    return problems


def _install(root):
    """The newest published snapshot, installed under ``root`` as the only
    one kept there."""
    from transitio.index import _refresh, refresh

    summary = refresh(cache_dir=root, keep=1, progress=False)
    return _refresh._snapshots(root) / summary["snapshot_id"]


@pytest.fixture(scope="session")
def reference_index():
    from transitio.index import read_index

    path = os.environ.get(INSTALLED)
    if not path:
        if not os.environ.get(ENABLE):
            pytest.skip(f"set {ENABLE}=1 to check the reference places")
        path = _install(pathlib.Path(__file__).parent / "cache" / "reference-index")
    return read_index(path)


@pytest.fixture(scope="session")
def study_day(reference_index):
    return _study_day(reference_index)


@pytest.fixture(scope="session")
def recommended(reference_index):
    """``(place, recommendation)`` for a place id on a day, each made once."""
    import transitio.index as transitio_index

    made = {}

    def get(place_id, day):
        if (place_id, day) not in made:
            place = transitio_index.place(place_id, index=reference_index)
            made[place_id, day] = (place, place.recommend(day))
        return made[place_id, day]

    return get


@pytest.fixture(scope="session")
def cropped(reference_index, tmp_path_factory):
    """The route ids of a feed fetched and cropped to a place, each once."""
    import transitio

    made = {}

    def get(place_id, day, feed_id):
        if (place_id, day, feed_id) not in made:
            result = transitio.fetch(
                place=place_id,
                when=day,
                feeds=[feed_id],
                index=reference_index,
                osm=False,
                directory=tmp_path_factory.mktemp("cropped"),
                progress=False,
            )
            with zipfile.ZipFile(result.paths[feed_id]) as archive:
                text = archive.read("routes.txt").decode("utf-8-sig")
            rows = csv.DictReader(io.StringIO(text))
            made[place_id, day, feed_id] = {row["route_id"] for row in rows}
        return made[place_id, day, feed_id]

    return get


@pytest.mark.parametrize("place, service", _service_params(None))
def test_reference_service(recommended, study_day, place, service):
    found_place, found = recommended(place["place"], service.get("day", study_day))
    problems = service_problems(found_place, found, service)
    gaps = [service["gap"]] if service.get("gap") else []
    gaps += out_of_date_gaps(found, service)
    _judge(problems, _named(service.get("fails")), "; ".join(gaps) or None)


@pytest.mark.parametrize("place, service", _service_params("tiers"))
def test_reference_tiers(recommended, study_day, place, service):
    found_place, _ = recommended(place["place"], study_day)
    _judge(tier_problems(found_place, service), _named(service.get("tiers_fails")))


@pytest.mark.parametrize("place_id", _place_params())
def test_nothing_disappears(recommended, study_day, place_id):
    place, found = recommended(place_id, study_day)
    defect = None if os.environ.get(RAW) else _disappears_defect(place_id)
    known = {"cross-border": ("not in the result", defect)} if defect else {}
    _judge(missing_problems(place, found), known)


@pytest.mark.parametrize("place, service", _service_params("routes"))
def test_reference_crop(cropped, study_day, place, service):
    if not os.environ.get(FETCH):
        pytest.skip(f"set {FETCH}=1 to download and crop the feeds")
    day = service.get("day", study_day)
    routes = cropped(place["place"], day, service["via"][0])
    problems = {
        r: ("cut off", "cut off by the crop")
        for r in service["routes"]
        if r not in routes
    }
    _judge(problems, _named(service.get("routes_fails")))
