import csv
import json

import pytest

pytest.importorskip("transitio._core")

from transitio.edit import FeedBuilder, FeedEditor  # noqa: E402
from transitio.exceptions import ChangeLogDesyncError  # noqa: E402


def tables_csv(builder):
    return {name: table.to_csv(index=False) for name, table in builder.tables.items()}


def build_minimal(builder=None):
    builder = builder or FeedBuilder()
    builder.add_agency("a", "A", "https://a.example", "Europe/Helsinki")
    builder.add_stop("s1", "First", 60.169, 24.931)
    builder.add_stop("s2", "Second", 60.171, 24.941)
    builder.add_route("r1", 0, "1", agency_id="a")
    builder.add_service("wk", "weekdays", "20260101", "20261231")
    return builder


def build_editor(tmp_path):
    """The minimal feed as a FeedEditor (the update helpers live there)."""
    source = tmp_path / "minimal.zip"
    build_minimal().save(source, check=False)
    return FeedEditor(source)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.add_stop("s3", "Third", 60.18, 24.95),
        lambda b: b.add_route("r2", 3, "2", agency_id="a"),
        lambda b: b.add_agency("b", "B", "https://b.example", "Europe/Helsinki"),
        lambda b: b.add_service("we", "weekend", "20260101", "20261231"),
        lambda b: b.add_shape("sh1", [(60.169, 24.931), (60.171, 24.941)]),
        lambda b: b.add_trip(
            "r1",
            "wk",
            "t1",
            [("s1", "08:00:00", "08:00:00"), ("s2", "08:05:00", "08:05:30")],
        ),
        lambda b: b.add_frequency_trip(
            "r1",
            "wk",
            "tf",
            [("s1", 0), ("s2", 300)],
            start="06:00:00",
            end="09:00:00",
            headway=600,
        ),
    ],
)
def test_every_helper_round_trips(mutate):
    builder = build_minimal()
    before = tables_csv(builder)
    mutate(builder)
    after = tables_csv(builder)
    assert after != before  # the mutation did something

    assert builder.undo() is not None
    assert tables_csv(builder) == before  # byte-identical revert
    assert builder.redo() is not None
    assert tables_csv(builder) == after  # byte-identical replay


def test_update_and_shift_round_trip(tmp_path):
    builder = build_editor(tmp_path)
    builder.add_frequency_trip(
        "r1",
        "wk",
        "t1",
        [("s1", 0), ("s2", 300)],
        start="06:00:00",
        end="09:00:00",
        headway=600,
    )
    for mutate in (
        lambda: builder.update_stop("s1", stop_name="Renamed"),
        lambda: builder.update_route("r1", route_short_name="1A"),
        lambda: builder.set_headway("t1", 300, end="10:00:00"),
        lambda: builder.shift_trip("t1", 3600),
    ):
        before = tables_csv(builder)
        mutate()
        after = tables_csv(builder)
        assert after != before
        builder.undo()
        assert tables_csv(builder) == before
        builder.redo()
        assert tables_csv(builder) == after


def test_drop_route_cascade_round_trips(tmp_path):
    builder = build_minimal()
    builder.add_trip(
        "r1",
        "wk",
        "t1",
        [("s1", "08:00:00", "08:00:00"), ("s2", "08:05:00", "08:05:00")],
    )
    builder.add_frequency_trip(
        "r1",
        "wk",
        "t-freq",
        [("s1", 0), ("s2", 300)],
        start="06:00:00",
        end="09:00:00",
        headway=600,
    )
    source = tmp_path / "feed.zip"
    builder.save(source, check=False)
    editor = FeedEditor(source)
    # references in every cascading table
    editor.insert_rows("fare_attributes.txt", [{"fare_id": "f", "price": "2.80"}])
    editor.insert_rows("fare_rules.txt", [{"fare_id": "f", "route_id": "r1"}])
    editor.insert_rows(
        "transfers.txt",
        [{"from_route_id": "r1", "to_route_id": "r1", "transfer_type": "0"}],
    )
    editor.insert_rows(
        "attributions.txt",
        [{"route_id": "r1", "organization_name": "Org"}],
    )
    before = tables_csv(editor)
    editor.drop_route("r1")
    after = tables_csv(editor)
    assert len(editor.tables["routes.txt"]) == 0
    assert len(editor.tables["trips.txt"]) == 0
    assert len(editor.tables["stop_times.txt"]) == 0
    assert len(editor.tables["frequencies.txt"]) == 0
    assert len(editor.tables["fare_rules.txt"]) == 0
    assert len(editor.tables["transfers.txt"]) == 0
    assert len(editor.tables["attributions.txt"]) == 0

    assert editor.undo() == "drop_route"
    assert tables_csv(editor) == before  # the whole cascade came back
    assert editor.redo() == "drop_route"
    assert tables_csv(editor) == after


def test_actions_group_and_interleave(tmp_path):
    builder = build_editor(tmp_path)
    builder.add_trip(
        "r1",
        "wk",
        "t1",
        [("s1", "08:00:00", "08:00:00"), ("s2", "08:05:00", "08:05:00")],
    )
    # one add_trip = one undo step despite trips + stop_times entries
    assert builder.undo() == "add_trip"
    assert "t1" not in set(builder.tables.get("trips.txt", {}).get("trip_id", []))

    builder.add_stop("s3", "Third", 60.18, 24.95)
    builder.update_stop("s3", stop_name="Renamed")
    assert builder.undo() == "update_stop"
    assert builder.undo() == "add_stop"
    assert builder.redo() == "add_stop"
    # a fresh mutation clears the remaining redo
    builder.update_stop("s1", stop_name="X")
    assert builder.redo() is None
    assert builder.redo_label is None


def test_primitives_log_positions_and_payloads():
    builder = FeedBuilder()
    builder.insert_rows("stops.txt", [{"stop_id": "a"}, {"stop_id": "b"}])
    kinds = [change.kind for change in builder.changes]
    assert kinds == ["add_table", "add_column", "insert", "insert"]
    inserted = [c for c in builder.changes if c.kind == "insert"]
    assert [c.row for c in inserted] == [0, 1]
    assert json.loads(inserted[1].new) == {"stop_id": "b"}

    builder.set_value("stops.txt", 0, "stop_name", "Named")
    entry = builder.changes[-1]
    assert (entry.kind, entry.old, entry.new) == ("set", "", "Named")
    builder.delete_rows("stops.txt", [0])
    entry = builder.changes[-1]
    assert entry.kind == "delete" and json.loads(entry.old)["stop_id"] == "a"

    with pytest.raises(AttributeError):
        builder.changes.append("nope")  # a tuple: immutable to callers


def test_bare_primitives_are_single_actions():
    builder = FeedBuilder()
    builder.insert_rows("stops.txt", [{"stop_id": "a"}, {"stop_id": "b"}])
    assert builder.undo() == "insert_rows"
    assert "stops.txt" not in builder.tables  # add_table undone too


def test_column_and_table_lifecycle_round_trip():
    builder = build_minimal()
    before = tables_csv(builder)
    # a set_value creating its column, undone, removes the column again
    builder.set_value("stops.txt", 0, "platform_code", "1")
    assert "platform_code" in builder.tables["stops.txt"].columns
    builder.undo()
    assert "platform_code" not in builder.tables["stops.txt"].columns
    assert tables_csv(builder) == before

    # the first insert into an absent optional table, undone, removes it
    builder.add_frequency_trip(
        "r1",
        "wk",
        "t1",
        [("s1", 0), ("s2", 300)],
        start="06:00:00",
        end="09:00:00",
        headway=600,
    )
    assert "frequencies.txt" in builder.tables
    builder.undo()
    assert "frequencies.txt" not in builder.tables


def test_set_stops_round_trips_via_replace_table():
    builder = build_minimal()
    before = tables_csv(builder)
    frame = builder.stops
    frame["stop_name"] = ["A", "B"]
    builder.set_stops(frame.drop(columns=["geometry"]))
    assert builder.changes[-1].kind == "replace_table"
    after = tables_csv(builder)
    builder.undo()
    assert tables_csv(builder) == before
    builder.redo()
    assert tables_csv(builder) == after


def test_desync_refuses_and_leaves_tables_alone(tmp_path):
    builder = build_editor(tmp_path)
    builder.add_trip(
        "r1",
        "wk",
        "t1",
        [("s1", "08:00:00", "08:00:00"), ("s2", "08:05:00", "08:05:00")],
    )
    # a direct escape-hatch edit invalidates the LAST entry of the action
    builder.tables["stop_times.txt"].iat[1, 0] = "hacked"
    before = tables_csv(builder)
    with pytest.raises(ChangeLogDesyncError):
        builder.undo()
    assert tables_csv(builder) == before  # nothing partially reverted

    # symmetric trap between undo and redo
    fresh = build_editor(tmp_path)
    fresh.update_stop("s1", stop_name="Renamed")
    fresh.undo()
    fresh.tables["stops.txt"].iat[0, 1] = "meddled"
    before = tables_csv(fresh)
    with pytest.raises(ChangeLogDesyncError):
        fresh.redo()
    assert tables_csv(fresh) == before

    # a mismatch on an EARLY entry of the action: the later entries'
    # inverses simulate fine on the copies first, then the early one
    # fails — and nothing may be partially reverted
    early = build_editor(tmp_path)
    early.add_trip(
        "r1",
        "wk",
        "t2",
        [("s1", "09:00:00", "09:00:00"), ("s2", "09:05:00", "09:05:00")],
    )
    # the trips row is the action's FIRST entry; corrupt it while the
    # stop_times rows (later entries, inverted first) stay pristine
    trips = early.tables["trips.txt"]
    trips.iat[len(trips) - 1, trips.columns.get_loc("trip_id")] = "hacked"
    before = tables_csv(early)
    with pytest.raises(ChangeLogDesyncError):
        early.undo()
    assert tables_csv(early) == before

    # an unlogged insert shifts rows: caught by the length check
    shifted = build_editor(tmp_path)
    shifted.update_stop("s2", stop_name="Renamed")
    import pandas as pd

    shifted.tables["stops.txt"] = pd.concat(
        [shifted.tables["stops.txt"], shifted.tables["stops.txt"].iloc[[0]]],
        ignore_index=True,
    )
    with pytest.raises(ChangeLogDesyncError):
        shifted.undo()


def test_failing_action_rolls_back(tmp_path):
    builder = build_editor(tmp_path)
    builder.update_stop("s1", stop_name="kept")
    builder.undo()  # something on the redo stack
    before = tables_csv(builder)
    changes_before = len(builder.changes)

    # a genuine mid-action failure: primitives HAVE mutated tables when
    # the exception fires, and everything must roll back
    with pytest.raises(RuntimeError):
        with builder.action("half-done"):
            builder.set_value("stops.txt", 0, "stop_name", "temporary")
            builder.insert_rows("stops.txt", [{"stop_id": "ghost"}])
            raise RuntimeError("boom")
    assert tables_csv(builder) == before
    assert len(builder.changes) == changes_before
    assert builder.redo_label == "update_stop"  # redo stack untouched

    # a NESTED helper failing inside an outer action that swallows the
    # error must also leave no half-applied helper behind
    with builder.action("outer"):
        builder.set_value("stops.txt", 0, "stop_name", "outer-edit")
        try:
            with builder.action("inner"):
                builder.set_value("stops.txt", 1, "stop_name", "inner-edit")
                raise RuntimeError("inner boom")
        except RuntimeError:
            pass
    assert builder.tables["stops.txt"].iat[0, 1] == "outer-edit"  # kept
    assert builder.tables["stops.txt"].iat[1, 1] == "Second"  # rolled back
    assert builder.undo() == "outer"
    assert tables_csv(builder) == before


def test_sidecar_contents_and_lifecycle(tmp_path):
    builder = build_minimal()
    path = tmp_path / "feed.zip"
    builder.save(path, check=False)
    sidecar = tmp_path / "feed.changes.txt"
    rows = list(csv.reader(sidecar.open()))
    header, entries, meta = rows[0], rows[1:-1], rows[-1]
    assert header[:6] == [
        "sequence",
        "timestamp",
        "action_id",
        "action_label",
        "row_count",
        "kind",
    ]
    assert len(entries) == len(builder.changes)
    inserted = [row for row in entries if row[5] == "insert"]
    assert all(json.loads(row[10]) for row in inserted)  # payloads parse
    assert meta[5] == "meta" and meta[9] == ""  # a builder has no source
    import hashlib

    assert meta[10] == hashlib.sha256(path.read_bytes()).hexdigest()

    # undone actions do not appear; an emptied log removes the sidecar
    builder.undo()
    builder.save(path, check=False)
    rows = list(csv.reader(sidecar.open()))
    assert len(rows) == len(builder.changes) + 2
    while builder.undo():
        pass
    builder.save(path, check=False)
    assert not sidecar.exists()

    # change_log=False writes none and clears a stale one
    builder.redo()
    builder.save(path, check=False)
    assert sidecar.exists()
    builder.save(path, check=False, change_log=False)
    assert not sidecar.exists()


def test_sidecar_meta_records_the_source(tmp_path):
    builder = build_minimal()
    source = tmp_path / "source.zip"
    builder.save(source, check=False)
    import hashlib

    editor = FeedEditor(source)
    editor.update_stop("s1", stop_name="Renamed")
    out = tmp_path / "edited.zip"
    editor.save(out, check=False)
    meta = list(csv.reader((tmp_path / "edited.changes.txt").open()))[-1]
    assert meta[9] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert meta[10] == hashlib.sha256(out.read_bytes()).hexdigest()

    # the log survives the save, and undo still works after it
    assert editor.undo() == "update_stop"


def test_publication_failures(tmp_path, monkeypatch):
    import os as os_module

    builder = build_minimal()
    path = tmp_path / "feed.zip"
    builder.save(path, check=False)
    sidecar = tmp_path / "feed.changes.txt"
    zip_before = path.read_bytes()
    sidecar_before = sidecar.read_bytes()

    # 1. sidecar STAGING failure aborts everything — forced surgically
    # AFTER mkstemp succeeded, so the write/cleanup path itself runs
    builder.add_stop("s9", "Nine", 60.2, 25.0)

    def failing_write(handle, entries, source_sha, result_sha):
        raise OSError("disk full mid-write")

    from transitio.edit import _changes as changes_module

    monkeypatch.setattr(changes_module, "write_sidecar", failing_write)
    with pytest.raises(OSError):
        builder.save(path, check=False)
    monkeypatch.undo()
    assert path.read_bytes() == zip_before  # old pair fully intact
    assert sidecar.read_bytes() == sidecar_before
    assert not list(tmp_path.glob(".changes-*"))  # temp cleaned up

    # 2. sidecar REPLACE failure after the zip's succeeds: error names
    # both paths, the new zip is in place
    real_replace = os_module.replace
    calls = []

    def failing_second_replace(src, dst):
        calls.append(str(dst))
        if str(dst).endswith(".changes.txt"):
            raise OSError("sidecar publish failed")
        return real_replace(src, dst)

    monkeypatch.setattr(os_module, "replace", failing_second_replace)
    with pytest.raises(OSError) as caught:
        builder.save(path, check=False)
    monkeypatch.undo()
    # the diagnostic names BOTH halves of the now-incoherent pair
    assert str(path) in str(caught.value)
    assert "feed.changes.txt" in str(caught.value)
    assert path.read_bytes() != zip_before  # the zip DID publish

    # 3. stale-sidecar removal failure aborts before the zip publishes
    while builder.undo():
        pass
    builder.save(path, check=False)  # normalise: no sidecar, current zip
    builder.add_stop("s10", "Ten", 60.3, 25.1)
    builder.save(path, check=False)  # sidecar exists again
    while builder.undo():
        pass
    zip_now = path.read_bytes()

    def failing_unlink(target, *args, **kwargs):
        if str(target).endswith(".changes.txt"):
            raise OSError("cannot remove")
        return os_module.remove(target)

    monkeypatch.setattr(os_module, "unlink", failing_unlink)
    with pytest.raises(OSError, match="stale change log"):
        builder.save(path, check=False)
    monkeypatch.undo()
    assert path.read_bytes() == zip_now  # nothing published
