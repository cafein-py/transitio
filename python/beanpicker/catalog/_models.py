"""Data models for Mobility Database catalog entries."""

from __future__ import annotations

import dataclasses
import datetime


def _parse_datetime(value):
    if not value:
        return None
    return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _parse_date(value):
    if not value:
        return None
    return datetime.date.fromisoformat(str(value)[:10])


def as_date(value):
    """Coerce a date, datetime or ISO string to a ``datetime.date``.

    Time-of-day information is ignored: dataset selection only depends on the
    service day.
    """
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        try:
            return datetime.date.fromisoformat(value[:10])
        except ValueError:
            return datetime.datetime.fromisoformat(value).date()
    raise TypeError(f"cannot interpret {value!r} as a date")


@dataclasses.dataclass(frozen=True)
class Feed:
    """A GTFS feed catalogued in the Mobility Database.

    Attributes mirror the fields beanpicker relies on; the complete API
    record is kept in ``raw``.
    """

    id: str
    provider: str | None
    status: str | None
    official: bool | None
    producer_url: str | None
    license_url: str | None
    latest_dataset_url: str | None
    locations: tuple
    raw: dict = dataclasses.field(repr=False, default_factory=dict)

    @classmethod
    def from_api(cls, record):
        source = record.get("source_info") or {}
        latest = record.get("latest_dataset") or {}
        return cls(
            id=record["id"],
            provider=record.get("provider"),
            status=record.get("status"),
            official=record.get("official", record.get("is_official")),
            producer_url=source.get("producer_url"),
            license_url=source.get("license_url"),
            latest_dataset_url=latest.get("hosted_url"),
            locations=tuple(record.get("locations") or ()),
            raw=record,
        )


@dataclasses.dataclass(frozen=True)
class Dataset:
    """One downloadable version of a feed, with its service date range."""

    id: str
    feed_id: str
    hosted_url: str | None
    downloaded_at: datetime.datetime | None
    hash: str | None
    service_start: datetime.date | None
    service_end: datetime.date | None
    validation_report_url: str | None
    raw: dict = dataclasses.field(repr=False, default_factory=dict)

    @classmethod
    def from_api(cls, record):
        report = record.get("validation_report") or {}
        return cls(
            id=record["id"],
            feed_id=record.get("feed_id", ""),
            hosted_url=record.get("hosted_url"),
            downloaded_at=_parse_datetime(record.get("downloaded_at")),
            hash=record.get("hash"),
            service_start=_parse_date(record.get("service_date_range_start")),
            service_end=_parse_date(record.get("service_date_range_end")),
            validation_report_url=report.get("url_json"),
            raw=record,
        )

    def covers(self, when):
        """True if the published service date range includes ``when``.

        Published ranges are frequently optimistic; proper verification
        against the calendar files happens after download.
        """
        when = as_date(when)
        if self.service_start is None or self.service_end is None:
            return False
        return self.service_start <= when <= self.service_end
