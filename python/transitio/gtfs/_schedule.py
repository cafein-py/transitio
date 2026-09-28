"""Vectorised readings of GTFS clock times and service dates."""

from __future__ import annotations

import numpy as np
import pandas as pd

_CLOCK = r"\d+:[0-5]\d:[0-5]\d"
# Longer hours saturate here, so the integer conversion never overflows.
_MAX_HOURS = 999_999
_DATE = r"\d{8}"
_WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


def _matching(text, pattern):
    return np.array(text.str.fullmatch(pattern).fillna(False), dtype=bool)


def clock_seconds(values):
    """Seconds after midnight of each ``H:MM:SS`` value, hours past
    999,999 counted as 999,999; NaN where malformed."""
    text = values.str.strip()
    valid = _matching(text, _CLOCK)
    clock = text[valid]
    hours = clock.str.slice(stop=-6)
    hours = hours.where(hours.str.len() <= len(str(_MAX_HOURS)), str(_MAX_HOURS))
    seconds = np.full(len(text), np.nan)
    seconds[valid] = (
        hours.astype("int64") * 3600
        + clock.str.slice(-5, -3).astype("int64") * 60
        + clock.str.slice(-2).astype("int64")
    ).to_numpy()
    return pd.Series(seconds, index=values.index)


def parse_dates(values):
    """``datetime64[D]`` of each ``YYYYMMDD`` value; NaT where it names no date."""
    text = values.str.strip()
    valid = _matching(text, _DATE)
    number = np.zeros(len(text), dtype="int64")
    number[valid] = text[valid].astype("int64").to_numpy()
    year, month, day = number // 10000, number // 100 % 100, number % 100
    months = ((year - 1970) * 12 + month - 1).astype("datetime64[M]")
    dates = months.astype("datetime64[D]") + (day - 1).astype("timedelta64[D]")
    valid &= (year >= 1) & (month >= 1) & (month <= 12) & (day >= 1)
    valid &= dates.astype("datetime64[M]") == months
    return np.where(valid, dates, np.datetime64("NaT", "D"))


def service_span(tables, within):
    """The first and last day any service runs within ``within``, a
    ``(first, last)`` pair of dates, from calendar.txt and
    calendar_dates.txt; None when none runs then. Rows with an unreadable
    date are skipped.
    """
    low, high = (np.datetime64(day, "D") for day in within)
    empty = {"service_id": [], "date": np.array([], dtype="datetime64[D]")}
    added = removed = pd.DataFrame(empty)
    exceptions = tables.get("calendar_dates.txt")
    if exceptions is not None and {"service_id", "date", "exception_type"} <= set(
        exceptions.columns
    ):
        listed = pd.DataFrame(
            {
                "service_id": exceptions["service_id"].to_numpy(),
                "date": parse_dates(exceptions["date"]),
            }
        )
        kind = exceptions["exception_type"].str.strip().to_numpy()
        inside = ((listed["date"] >= low) & (listed["date"] <= high)).to_numpy()
        added, removed = listed[inside & (kind == "1")], listed[inside & (kind == "2")]
    frames = [added]
    calendar = tables.get("calendar.txt")
    if calendar is not None and "service_id" in calendar.columns:
        blank = pd.Series("", index=calendar.index)
        start = np.maximum(parse_dates(calendar.get("start_date", blank)), low)
        end = np.minimum(parse_dates(calendar.get("end_date", blank)), high)
        readable = ~(np.isnat(start) | np.isnat(end))
        span = np.where(readable, (end - start).astype("int64") + 1, 0).clip(0)
        # A week of running days holds at least one day no removal hides,
        # so each row's first and last running days lie within a week per
        # removal of its service, plus one, of its ends.
        services = calendar["service_id"]
        hidden = services.map(removed.groupby("service_id").size())
        edge = 7 * (1 + hidden.fillna(0).to_numpy(dtype="int64"))
        count = np.minimum(span, 2 * edge)
        rows = np.repeat(np.arange(len(calendar)), count)
        step = np.arange(len(rows)) - np.repeat(np.cumsum(count) - count, count)
        step = np.where(step < edge[rows], step, span[rows] - count[rows] + step)
        days = start[rows] + step.astype("timedelta64[D]")
        flags = np.column_stack(
            [
                (
                    np.array(calendar[day].str.strip() == "1", dtype=bool)
                    if day in calendar.columns
                    else np.zeros(len(calendar), dtype=bool)
                )
                for day in _WEEKDAYS
            ]
        )
        # 1970-01-01, day 0, was a Thursday.
        runs = flags[rows, (days.astype("int64") + 3) % 7]
        frames.append(
            pd.DataFrame(
                {"service_id": services.to_numpy()[rows[runs]], "date": days[runs]}
            )
        )
    dates = pd.concat(frames, ignore_index=True)
    if len(removed):
        gone = pd.MultiIndex.from_frame(removed)
        dates = dates[~pd.MultiIndex.from_frame(dates).isin(gone)]
    if dates.empty:
        return None
    days = dates["date"].to_numpy().astype("datetime64[D]")
    return days.min().item(), days.max().item()
