"""The invertible change log behind feed undo/redo.

Every sanctioned mutation logs entries here; ``FeedBuilder.undo`` and
``redo`` revert or reapply whole actions by simulating their inverses on
copies of the affected tables, committing only when every entry still
matches what was logged.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from typing import NamedTuple

import pandas as pd

from transitio.exceptions import ChangeLogDesyncError

# The change kinds a log entry or sidecar row can carry. `meta` appears
# only in the saved sidecar (both endpoint checksums); `barrier` is
# reserved for a future mutation with no sensible inverse — nothing
# emits one today.
KINDS = (
    "set",
    "insert",
    "delete",
    "add_column",
    "add_table",
    "replace_table",
    "meta",
    "barrier",
)

_COLUMNS = (
    "sequence",
    "timestamp",
    "action_id",
    "action_label",
    "row_count",
    "kind",
    "file",
    "row",
    "column",
    "old",
    "new",
)


class Change(NamedTuple):
    """One logged, invertible change."""

    sequence: int
    timestamp: str
    action_id: object
    action_label: object
    row_count: int
    kind: str
    file: str
    row: object
    column: str
    old: str
    new: str


def _now():
    return datetime.now(timezone.utc).isoformat()


def _row_payload(table, position):
    return {column: table.iat[position, i] for i, column in enumerate(table.columns)}


def _table_csv(table):
    return table.to_csv(index=False, lineterminator="\n")


def _csv_table(text):
    if not text.strip():
        return pd.DataFrame()
    return pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)


class _TableView:
    """The evolving copies an undo/redo simulation works on.

    ``None`` marks a table as absent (either it never existed or an
    ``add_table`` inverse removed it); commit applies the final state.
    """

    def __init__(self, tables):
        self._tables = tables
        self._copies = {}

    def get(self, filename):
        if filename not in self._copies:
            table = self._tables.get(filename)
            # positional operations need a clean RangeIndex, whatever a
            # direct edit may have left behind
            self._copies[filename] = (
                None if table is None else table.copy().reset_index(drop=True)
            )
        return self._copies[filename]

    def put(self, filename, table):
        self._copies[filename] = table

    def commit(self):
        for filename, table in self._copies.items():
            if table is None:
                self._tables.pop(filename, None)
            else:
                self._tables[filename] = table


def _desync(entry, reason):
    where = f"{entry.file} row {entry.row}"
    if entry.column:
        where += f" column {entry.column!r}"
    raise ChangeLogDesyncError(
        f"tables no longer match the change log at {where} ({reason}); "
        "the feed was edited outside the logged surface — undo/redo refused"
    )


def _safe_eq(a, b):
    # a pandas NA introduced through the escape hatch poisons ordinary
    # equality (bool(NA) raises); any such comparison is a mismatch
    try:
        return bool(a == b)
    except (TypeError, ValueError):
        return False


def _require(entry, condition, reason):
    try:
        satisfied = bool(condition)
    except (TypeError, ValueError):  # e.g. pandas NA from a direct edit
        satisfied = False
    if not satisfied:
        _desync(entry, reason)


def _check_length(entry, table, expected):
    length = 0 if table is None else len(table)
    _require(entry, length == expected, f"expected {expected} rows, found {length}")


def apply_inverse(tables, entries):
    """Revert ``entries`` (one action, in log order) or raise untouched."""
    view = _TableView(tables)
    for entry in reversed(entries):
        table = view.get(entry.file)
        if entry.kind in ("set", "insert", "delete"):
            _require(entry, table is not None, "table missing")
        if entry.kind == "set":
            _check_length(entry, table, entry.row_count)
            _require(entry, entry.column in table.columns, "column missing")
            location = table.columns.get_loc(entry.column)
            current = table.iat[entry.row, location]
            _require(entry, _safe_eq(current, entry.new), f"cell holds {current!r}")
            table.iat[entry.row, location] = entry.old
        elif entry.kind == "insert":
            _check_length(entry, table, entry.row_count)
            payload = json.loads(entry.new)
            current = _row_payload(table, entry.row)
            _require(entry, set(current) == set(payload), "table columns changed")
            _require(entry, _safe_eq(current, payload), "inserted row changed")
            view.put(
                entry.file,
                table.drop(index=entry.row).reset_index(drop=True),
            )
        elif entry.kind == "delete":
            _check_length(entry, table, entry.row_count)
            payload = json.loads(entry.old)
            _require(
                entry,
                set(payload) == set(table.columns),
                "table columns changed",
            )
            addition = pd.DataFrame([payload], dtype=str)
            merged = pd.concat(
                [table.iloc[: entry.row], addition, table.iloc[entry.row :]],
                ignore_index=True,
            ).fillna("")
            view.put(entry.file, merged[list(table.columns)])
        elif entry.kind == "add_column":
            _require(entry, table is not None, "table missing")
            _require(entry, entry.column in table.columns, "column missing")
            _require(
                entry,
                all(_safe_eq(value, "") for value in table[entry.column]),
                "created column no longer empty",
            )
            view.put(entry.file, table.drop(columns=[entry.column]))
        elif entry.kind == "add_table":
            _require(entry, table is not None, "table missing")
            _require(entry, len(table) == 0, "created table no longer empty")
            view.put(entry.file, None)
        elif entry.kind == "replace_table":
            _require(entry, table is not None, "table missing")
            _require(entry, _safe_eq(_table_csv(table), entry.new), "table changed")
            view.put(entry.file, _csv_table(entry.old))
        else:  # barrier or unknown: no inverse exists
            _desync(entry, f"no inverse for {entry.kind!r}")
    view.commit()


def apply_forward(tables, entries):
    """Reapply ``entries`` (one action, in log order) or raise untouched."""
    view = _TableView(tables)
    for entry in entries:
        table = view.get(entry.file)
        if entry.kind in ("set", "insert", "delete"):
            _require(entry, table is not None, "table missing")
        if entry.kind == "set":
            _check_length(entry, table, entry.row_count)
            _require(entry, entry.column in table.columns, "column missing")
            location = table.columns.get_loc(entry.column)
            current = table.iat[entry.row, location]
            _require(entry, _safe_eq(current, entry.old), f"cell holds {current!r}")
            table.iat[entry.row, location] = entry.new
        elif entry.kind == "insert":
            _check_length(entry, table, entry.row_count - 1)
            payload = json.loads(entry.new)
            _require(
                entry,
                set(payload) == set(table.columns),
                "table columns changed",
            )
            addition = pd.DataFrame([payload], dtype=str)
            merged = pd.concat(
                [table.iloc[: entry.row], addition, table.iloc[entry.row :]],
                ignore_index=True,
            ).fillna("")
            view.put(entry.file, merged[list(table.columns)])
        elif entry.kind == "delete":
            _check_length(entry, table, entry.row_count + 1)
            payload = json.loads(entry.old)
            current = _row_payload(table, entry.row)
            _require(entry, set(current) == set(payload), "table columns changed")
            _require(entry, _safe_eq(current, payload), "row to delete changed")
            view.put(
                entry.file,
                table.drop(index=entry.row).reset_index(drop=True),
            )
        elif entry.kind == "add_column":
            _require(entry, table is not None, "table missing")
            _require(entry, entry.column not in table.columns, "column exists")
            table = table.copy()
            table[entry.column] = ""
            view.put(entry.file, table)
        elif entry.kind == "add_table":
            _require(entry, table is None, "table exists")
            view.put(entry.file, pd.DataFrame())
        elif entry.kind == "replace_table":
            _require(entry, table is not None, "table missing")
            _require(entry, _safe_eq(_table_csv(table), entry.old), "table changed")
            view.put(entry.file, _csv_table(entry.new))
        else:
            _desync(entry, f"no forward application for {entry.kind!r}")
    view.commit()


def write_sidecar(handle, entries, source_sha256, result_sha256):
    """Write the applied history plus the endpoints `meta` row as CSV."""
    writer = csv.writer(handle, lineterminator="\n")
    writer.writerow(_COLUMNS)
    for entry in entries:
        writer.writerow(
            [
                entry.sequence,
                entry.timestamp,
                entry.action_id,
                entry.action_label,
                entry.row_count,
                entry.kind,
                entry.file,
                entry.row,
                entry.column,
                entry.old,
                entry.new,
            ]
        )
    writer.writerow(
        ["", _now(), "", "", "", "meta", "", "", "", source_sha256 or "", result_sha256]
    )
