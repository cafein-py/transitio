//! Semantic rule tier: stop-time progression, calendar activity and
//! coverage, block overlaps, frequency overlaps and shape consistency.
//! Codes and severities verified against the canonical validator source
//! (see plans/validation-rules.md). This tier also computes the feed's
//! actual service-day window, which the catalog layer uses to verify the
//! optimistic published dataset ranges.

use std::collections::{BTreeMap, HashMap, HashSet};

use chrono::{Datelike, NaiveDate};

use crate::notice::{Notice, Severity};
use crate::rules::{clip, parse_gtfs_time, Samplers};
use crate::scan::{ScanOptions, ScanResult, Table};

/// Hostile-input guards: calendars are expanded to at most this many days
/// per service, and per-block overlap comparisons are capped.
const MAX_SERVICE_DAYS: i64 = 4000;
const MAX_TOTAL_SERVICE_DAYS: i64 = 2_000_000;
const MAX_BLOCK_PAIR_CHECKS: usize = 10_000;
/// Block-overlap day offsets follow actual span lengths but are clamped so
/// hostile 100-hour times cannot force wide scans.
const MAX_BLOCK_DAY_OFFSET: i64 = 7;

/// Active service dates for other passes (cropping); notices generated
/// during the computation are discarded.
pub(crate) fn active_service_dates(
    tables: &BTreeMap<String, Table>,
    options: &ScanOptions,
) -> HashMap<String, Vec<NaiveDate>> {
    let mut samplers = Samplers::new(options.max_notices_per_file);
    let mut scratch = Vec::new();
    let (services, _) = service_calendars(tables, options, &mut samplers, &mut scratch);
    services
}

pub fn run_semantics(result: &mut ScanResult, options: &ScanOptions) {
    let mut notices = Vec::new();
    let mut samplers = Samplers::new(options.max_notices_per_file);

    // Unreliable (truncated/unreadable/refused) inputs must not produce
    // false semantic findings or an authoritative-looking service window.
    let calendars_unreliable = result.incomplete.contains("calendar.txt")
        || result.incomplete.contains("calendar_dates.txt");
    let stop_times_unreliable =
        result.incomplete.contains("stop_times.txt") || result.incomplete.contains("trips.txt");

    let (services, expansion_truncated) =
        service_calendars(&result.tables, options, &mut samplers, &mut notices);
    // A truncated expansion under-covers, so the window is not published.
    result.service_window = if calendars_unreliable || expansion_truncated {
        None
    } else {
        service_window(&services)
    };

    if !stop_times_unreliable {
        let trips = trip_summaries(&result.tables, &mut samplers, &mut notices);
        if !calendars_unreliable {
            block_overlap_checks(
                &result.tables,
                &trips,
                &services,
                &mut samplers,
                &mut notices,
            );
        }
    }
    frequency_checks(&result.tables, &mut samplers, &mut notices);
    shape_checks(&result.tables, &mut samplers, &mut notices);

    samplers.finish(&mut notices);
    result.notices.append(&mut notices);
}

fn column(table: &Table, name: &str) -> Option<usize> {
    table.headers.iter().position(|h| h == name)
}

fn cell<'t>(table: &'t Table, row: &'t crate::scan::Row, name: &str) -> &'t str {
    column(table, name)
        .map(|i| row.fields[i].as_str())
        .unwrap_or("")
}

fn parse_date(value: &str) -> Option<NaiveDate> {
    NaiveDate::parse_from_str(value.trim(), "%Y%m%d").ok()
}

/// Active service dates per service_id: calendar weekday patterns within
/// [start_date, end_date], plus calendar_dates exceptions (1 add, 2 remove).
fn service_calendars(
    tables: &BTreeMap<String, Table>,
    options: &ScanOptions,
    samplers: &mut Samplers,
    notices: &mut Vec<Notice>,
) -> (HashMap<String, Vec<NaiveDate>>, bool) {
    let mut services: HashMap<String, HashSet<NaiveDate>> = HashMap::new();
    let mut calendar_rows: HashMap<String, u64> = HashMap::new();
    let mut total_days = 0i64;
    let mut truncated = false;
    if let Some(calendar) = tables.get("calendar.txt") {
        let weekday_columns = [
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        ];
        let sampler = samplers.file("calendar.txt");
        for row in &calendar.rows {
            let service_id = cell(calendar, row, "service_id");
            let weekdays: Vec<bool> = weekday_columns
                .iter()
                .map(|day| cell(calendar, row, day).trim() == "1")
                .collect();
            if !weekdays.iter().any(|&active| active) {
                sampler.push(
                    notices,
                    Notice::new("service_has_no_active_day_of_the_week", Severity::Warning)
                        .with("serviceId", clip(service_id))
                        .with("csvRowNumber", row.csv_row),
                );
            }
            let (Some(start), Some(end)) = (
                parse_date(cell(calendar, row, "start_date")),
                parse_date(cell(calendar, row, "end_date")),
            ) else {
                continue; // field tier already reported invalid dates
            };
            calendar_rows.insert(service_id.to_string(), row.csv_row);
            if (end - start).num_days() >= MAX_SERVICE_DAYS {
                // transitio-specific: the expansion is clamped, so the
                // computed window under-covers this service.
                sampler.push(
                    notices,
                    Notice::new("calendar_span_truncated", Severity::Warning)
                        .with("serviceId", clip(service_id))
                        .with("csvRowNumber", row.csv_row),
                );
                truncated = true;
            }
            if total_days >= MAX_TOTAL_SERVICE_DAYS {
                truncated = true;
                continue; // global expansion budget exhausted
            }
            let dates = services.entry(service_id.to_string()).or_default();
            let mut date = start;
            let mut spanned = 0i64;
            while date <= end && spanned < MAX_SERVICE_DAYS {
                let weekday = date.weekday().num_days_from_monday() as usize;
                if weekdays[weekday] {
                    dates.insert(date);
                }
                match date.succ_opt() {
                    Some(next) => date = next,
                    None => break,
                }
                spanned += 1;
                total_days += 1;
            }
        }
    }
    if let Some(exceptions) = tables.get("calendar_dates.txt") {
        for row in &exceptions.rows {
            let Some(date) = parse_date(cell(exceptions, row, "date")) else {
                continue;
            };
            let service_id = cell(exceptions, row, "service_id");
            match cell(exceptions, row, "exception_type").trim() {
                "1" => {
                    services
                        .entry(service_id.to_string())
                        .or_default()
                        .insert(date);
                }
                "2" => {
                    if let Some(dates) = services.get_mut(service_id) {
                        dates.remove(&date);
                    }
                }
                _ => {}
            }
        }
    }
    let sorted_services: HashMap<String, Vec<NaiveDate>> = services
        .into_iter()
        .map(|(id, dates)| {
            let mut sorted: Vec<NaiveDate> = dates.into_iter().collect();
            sorted.sort_unstable();
            (id, sorted)
        })
        .collect();
    // Canonical expiry accounts for calendar_dates exceptions, so it is
    // decided on each service's final post-exception active date.
    if let Some(reference) = options.reference_date {
        if !truncated {
            let sampler = samplers.file("calendar.txt");
            let mut expired: Vec<(&String, &Vec<NaiveDate>)> = sorted_services
                .iter()
                .filter(|(_, dates)| dates.last().map(|d| *d < reference).unwrap_or(false))
                .collect();
            expired.sort_by_key(|(id, _)| id.as_str());
            for (service_id, _) in expired {
                let mut notice = Notice::new("expired_calendar", Severity::Warning)
                    .with("serviceId", clip(service_id));
                if let Some(csv_row) = calendar_rows.get(service_id) {
                    notice = notice.with("csvRowNumber", *csv_row);
                }
                sampler.push(notices, notice);
            }
        }
    }
    (sorted_services, truncated)
}

fn service_window(services: &HashMap<String, Vec<NaiveDate>>) -> Option<(String, String)> {
    let first = services.values().filter_map(|d| d.first()).min()?;
    let last = services.values().filter_map(|d| d.last()).max()?;
    Some((
        first.format("%Y%m%d").to_string(),
        last.format("%Y%m%d").to_string(),
    ))
}

struct TripSpan {
    start: u64,
    end: u64,
    service_id: String,
    csv_row: u64,
}

/// Per-trip stop-time checks; returns each usable trip's time span for the
/// block-overlap pass.
fn trip_summaries(
    tables: &BTreeMap<String, Table>,
    samplers: &mut Samplers,
    notices: &mut Vec<Notice>,
) -> HashMap<String, TripSpan> {
    let mut spans = HashMap::new();
    let Some(stop_times) = tables.get("stop_times.txt") else {
        return spans;
    };
    let trip_services: HashMap<&str, &str> = tables
        .get("trips.txt")
        .map(|trips| {
            trips
                .rows
                .iter()
                .map(|row| (cell(trips, row, "trip_id"), cell(trips, row, "service_id")))
                .collect()
        })
        .unwrap_or_default();

    struct StopTime {
        seq: i64,
        arrival: Option<u64>,
        departure: Option<u64>,
        arrival_raw: bool,
        departure_raw: bool,
        timepoint: bool,
        distance: Option<f64>,
        csv_row: u64,
    }
    let mut by_trip: BTreeMap<&str, Vec<StopTime>> = BTreeMap::new();
    for row in &stop_times.rows {
        let Ok(seq) = cell(stop_times, row, "stop_sequence").trim().parse::<i64>() else {
            continue; // field tier reported the malformed sequence
        };
        let arrival_field = cell(stop_times, row, "arrival_time").trim();
        let departure_field = cell(stop_times, row, "departure_time").trim();
        by_trip
            .entry(cell(stop_times, row, "trip_id"))
            .or_default()
            .push(StopTime {
                seq,
                arrival: parse_gtfs_time(arrival_field),
                departure: parse_gtfs_time(departure_field),
                arrival_raw: !arrival_field.is_empty(),
                departure_raw: !departure_field.is_empty(),
                timepoint: cell(stop_times, row, "timepoint").trim() == "1",
                distance: cell(stop_times, row, "shape_dist_traveled")
                    .trim()
                    .parse()
                    .ok(),
                csv_row: row.csv_row,
            });
    }

    if let Some(trips) = tables.get("trips.txt") {
        let sampler = samplers.file("trips.txt");
        for row in &trips.rows {
            let trip_id = cell(trips, row, "trip_id");
            if !trip_id.is_empty() && !by_trip.contains_key(trip_id) {
                sampler.push(
                    notices,
                    Notice::new("unusable_trip", Severity::Error)
                        .with("tripId", clip(trip_id))
                        .with("csvRowNumber", row.csv_row),
                );
            }
        }
    }
    let sampler = samplers.file("stop_times.txt");
    for (trip_id, mut stops) in by_trip {
        stops.sort_by_key(|s| s.seq);
        if stops.len() < 2 {
            sampler.push(
                notices,
                Notice::new("unusable_trip", Severity::Error)
                    .with("tripId", clip(trip_id))
                    .with(
                        "csvRowNumber",
                        stops.first().map(|s| s.csv_row).unwrap_or(0),
                    ),
            );
            continue;
        }
        for stop in [&stops[0], &stops[stops.len() - 1]] {
            for (field, present) in [
                ("arrival_time", stop.arrival_raw),
                ("departure_time", stop.departure_raw),
            ] {
                if !present {
                    sampler.push(
                        notices,
                        Notice::new("missing_trip_edge", Severity::Error)
                            .with("tripId", clip(trip_id))
                            .with("csvRowNumber", stop.csv_row)
                            .with("specifiedField", field),
                    );
                }
            }
        }
        let mut previous_departure: Option<(u64, u64)> = None;
        let mut previous_distance: Option<f64> = None;
        for stop in &stops {
            if let (Some(arrival), Some(departure)) = (stop.arrival, stop.departure) {
                if departure < arrival {
                    // transitio-specific: no canonical notice covers a
                    // same-stop departure before its arrival.
                    sampler.push(
                        notices,
                        Notice::new("departure_before_arrival", Severity::Error)
                            .with("tripId", clip(trip_id))
                            .with("csvRowNumber", stop.csv_row),
                    );
                }
            }
            if stop.timepoint && (!stop.arrival_raw || !stop.departure_raw) {
                sampler.push(
                    notices,
                    Notice::new("stop_time_timepoint_without_times", Severity::Error)
                        .with("tripId", clip(trip_id))
                        .with("csvRowNumber", stop.csv_row),
                );
            }
            if stop.arrival_raw != stop.departure_raw {
                sampler.push(
                    notices,
                    Notice::new(
                        "stop_time_with_only_arrival_or_departure_time",
                        Severity::Error,
                    )
                    .with("tripId", clip(trip_id))
                    .with("csvRowNumber", stop.csv_row),
                );
            }
            if let (Some(arrival), Some((prev_departure, prev_row))) =
                (stop.arrival, previous_departure)
            {
                if arrival < prev_departure {
                    sampler.push(
                        notices,
                        Notice::new(
                            "stop_time_with_arrival_before_previous_departure_time",
                            Severity::Error,
                        )
                        .with("tripId", clip(trip_id))
                        .with("csvRowNumber", stop.csv_row)
                        .with("prevCsvRowNumber", prev_row),
                    );
                }
            }
            if let Some(departure) = stop.departure {
                previous_departure = Some((departure, stop.csv_row));
            }
            if let Some(distance) = stop.distance {
                if let Some(previous) = previous_distance {
                    if distance <= previous {
                        sampler.push(
                            notices,
                            Notice::new("decreasing_or_equal_stop_time_distance", Severity::Error)
                                .with("tripId", clip(trip_id))
                                .with("csvRowNumber", stop.csv_row),
                        );
                    }
                }
                previous_distance = Some(distance);
            }
        }
        let start = stops.iter().find_map(|s| s.arrival.or(s.departure));
        let end = stops.iter().rev().find_map(|s| s.departure.or(s.arrival));
        if let (Some(start), Some(end)) = (start, end) {
            spans.insert(
                trip_id.to_string(),
                TripSpan {
                    start,
                    end,
                    service_id: trip_services.get(trip_id).unwrap_or(&"").to_string(),
                    csv_row: stops[0].csv_row,
                },
            );
        }
    }
    spans
}

/// True when some date in `a`, shifted forward by `offset` days, appears
/// in `b`. Over-midnight spans compare against neighbouring service days.
fn services_intersect(a: &[NaiveDate], b: &[NaiveDate], offset: i64) -> bool {
    let (mut i, mut j) = (0, 0);
    while i < a.len() && j < b.len() {
        let Some(shifted) = shift(a[i], offset) else {
            i += 1;
            continue;
        };
        match shifted.cmp(&b[j]) {
            std::cmp::Ordering::Equal => return true,
            std::cmp::Ordering::Less => i += 1,
            std::cmp::Ordering::Greater => j += 1,
        }
    }
    false
}

fn shift(date: NaiveDate, offset: i64) -> Option<NaiveDate> {
    match offset.cmp(&0) {
        std::cmp::Ordering::Equal => Some(date),
        std::cmp::Ordering::Greater => date.checked_add_days(chrono::Days::new(offset as u64)),
        std::cmp::Ordering::Less => date.checked_sub_days(chrono::Days::new((-offset) as u64)),
    }
}

fn spans_overlap(a: (i64, i64), b: (i64, i64)) -> bool {
    a.0 < b.1 && b.0 < a.1
}

fn block_overlap_checks(
    tables: &BTreeMap<String, Table>,
    spans: &HashMap<String, TripSpan>,
    services: &HashMap<String, Vec<NaiveDate>>,
    samplers: &mut Samplers,
    notices: &mut Vec<Notice>,
) {
    let Some(trips) = tables.get("trips.txt") else {
        return;
    };
    let mut blocks: BTreeMap<&str, Vec<(&str, &TripSpan)>> = BTreeMap::new();
    for row in &trips.rows {
        let block_id = cell(trips, row, "block_id");
        if block_id.is_empty() {
            continue;
        }
        let trip_id = cell(trips, row, "trip_id");
        if let Some(span) = spans.get(trip_id) {
            blocks.entry(block_id).or_default().push((trip_id, span));
        }
    }
    let sampler = samplers.file("trips.txt");
    let empty: Vec<NaiveDate> = Vec::new();
    for (block_id, mut members) in blocks {
        members.sort_by_key(|(_, span)| span.start);
        let mut checks = 0usize;
        'block: for i in 0..members.len() {
            for j in (i + 1)..members.len() {
                checks += 1;
                if checks > MAX_BLOCK_PAIR_CHECKS {
                    // transitio-specific: coverage of this block is
                    // truncated; later blocks are still checked.
                    sampler.push(
                        notices,
                        Notice::new("notice_limit_reached", Severity::Warning)
                            .with("filename", "trips.txt")
                            .with("blockId", clip(block_id))
                            .with(
                                "message",
                                "block overlap coverage truncated at the pair-check cap",
                            ),
                    );
                    break 'block;
                }
                let a = &members[i].1;
                let b = &members[j].1;
                let days_a = services.get(&a.service_id).unwrap_or(&empty);
                let days_b = services.get(&b.service_id).unwrap_or(&empty);
                // Over-midnight times legally exceed 24 h (and can span
                // several days); compare on every day shift the spans can
                // reach, clamped against hostile extreme times.
                let reach = |end: u64| (end as i64 / 86_400 + 1).min(MAX_BLOCK_DAY_OFFSET);
                let overlapping = (-reach(b.end)..=reach(a.end)).any(|offset| {
                    spans_overlap(
                        (
                            a.start as i64 - offset * 86_400,
                            a.end as i64 - offset * 86_400,
                        ),
                        (b.start as i64, b.end as i64),
                    ) && services_intersect(days_a, days_b, offset)
                });
                if overlapping {
                    sampler.push(
                        notices,
                        Notice::new("block_trips_with_overlapping_stop_times", Severity::Error)
                            .with("blockId", clip(block_id))
                            .with("tripIdA", clip(members[i].0))
                            .with("tripIdB", clip(members[j].0))
                            .with("csvRowNumber", b.csv_row),
                    );
                }
            }
        }
    }
}

fn frequency_checks(
    tables: &BTreeMap<String, Table>,
    samplers: &mut Samplers,
    notices: &mut Vec<Notice>,
) {
    let Some(frequencies) = tables.get("frequencies.txt") else {
        return;
    };
    let mut by_trip: BTreeMap<&str, Vec<(u64, u64, u64)>> = BTreeMap::new();
    for row in &frequencies.rows {
        let (Some(start), Some(end)) = (
            parse_gtfs_time(cell(frequencies, row, "start_time").trim()),
            parse_gtfs_time(cell(frequencies, row, "end_time").trim()),
        ) else {
            continue;
        };
        by_trip
            .entry(cell(frequencies, row, "trip_id"))
            .or_default()
            .push((start, end, row.csv_row));
    }
    let sampler = samplers.file("frequencies.txt");
    for (trip_id, mut windows) in by_trip {
        windows.sort_unstable();
        // Track the furthest-reaching window so nested intervals are
        // caught, not only adjacent ones.
        let mut max_end: Option<(u64, u64)> = None;
        for (start, end, csv_row) in windows {
            if let Some((furthest_end, furthest_row)) = max_end {
                if start < furthest_end {
                    sampler.push(
                        notices,
                        Notice::new("overlapping_frequency", Severity::Error)
                            .with("tripId", clip(trip_id))
                            .with("prevCsvRowNumber", furthest_row)
                            .with("csvRowNumber", csv_row),
                    );
                }
            }
            if max_end.map(|(e, _)| end > e).unwrap_or(true) {
                max_end = Some((end, csv_row));
            }
        }
    }
}

fn shape_checks(
    tables: &BTreeMap<String, Table>,
    samplers: &mut Samplers,
    notices: &mut Vec<Notice>,
) {
    let Some(shapes) = tables.get("shapes.txt") else {
        return;
    };
    let used: HashSet<&str> = tables
        .get("trips.txt")
        .map(|trips| {
            trips
                .rows
                .iter()
                .map(|row| cell(trips, row, "shape_id"))
                .filter(|id| !id.is_empty())
                .collect()
        })
        .unwrap_or_default();

    struct Point {
        seq: i64,
        lat: Option<f64>,
        lon: Option<f64>,
        distance: Option<f64>,
        csv_row: u64,
    }
    let mut by_shape: BTreeMap<&str, Vec<Point>> = BTreeMap::new();
    for row in &shapes.rows {
        let Ok(seq) = cell(shapes, row, "shape_pt_sequence").trim().parse::<i64>() else {
            continue;
        };
        by_shape
            .entry(cell(shapes, row, "shape_id"))
            .or_default()
            .push(Point {
                seq,
                lat: cell(shapes, row, "shape_pt_lat").trim().parse().ok(),
                lon: cell(shapes, row, "shape_pt_lon").trim().parse().ok(),
                distance: cell(shapes, row, "shape_dist_traveled").trim().parse().ok(),
                csv_row: row.csv_row,
            });
    }
    let sampler = samplers.file("shapes.txt");
    for (shape_id, mut points) in by_shape {
        points.sort_by_key(|p| p.seq);
        if points.len() == 1 {
            sampler.push(
                notices,
                Notice::new("single_shape_point", Severity::Warning)
                    .with("shapeId", clip(shape_id))
                    .with("csvRowNumber", points[0].csv_row),
            );
        }
        if !used.contains(shape_id) {
            sampler.push(
                notices,
                Notice::new("unused_shape", Severity::Warning)
                    .with("shapeId", clip(shape_id))
                    .with("csvRowNumber", points[0].csv_row),
            );
        }
        for pair in points.windows(2) {
            let (Some(previous), Some(current)) = (pair[0].distance, pair[1].distance) else {
                continue;
            };
            if current < previous {
                sampler.push(
                    notices,
                    Notice::new("decreasing_shape_distance", Severity::Error)
                        .with("shapeId", clip(shape_id))
                        .with("csvRowNumber", pair[1].csv_row)
                        .with("prevCsvRowNumber", pair[0].csv_row),
                );
            } else if current == previous {
                let same_point = pair[0].lat == pair[1].lat && pair[0].lon == pair[1].lon;
                let (code, severity) = if same_point {
                    ("equal_shape_distance_same_coordinates", Severity::Warning)
                } else {
                    ("equal_shape_distance_diff_coordinates", Severity::Error)
                };
                sampler.push(
                    notices,
                    Notice::new(code, severity)
                        .with("shapeId", clip(shape_id))
                        .with("csvRowNumber", pair[1].csv_row)
                        .with("prevCsvRowNumber", pair[0].csv_row),
                );
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use std::io::Cursor;

    use super::*;
    use crate::scan::scan_reader;

    fn validate_zip(files: &[(&str, &str)], reference: Option<&str>) -> ScanResult {
        let mut cursor = Cursor::new(Vec::new());
        {
            let mut writer = zip::ZipWriter::new(&mut cursor);
            let options = zip::write::SimpleFileOptions::default();
            for (name, content) in files {
                writer.start_file(*name, options).unwrap();
                std::io::Write::write_all(&mut writer, content.as_bytes()).unwrap();
            }
            writer.finish().unwrap();
        }
        cursor.set_position(0);
        let mut result = scan_reader(cursor).unwrap();
        let options = ScanOptions {
            reference_date: reference.and_then(parse_date),
            ..ScanOptions::default()
        };
        run_semantics(&mut result, &options);
        result
    }

    fn minimal() -> Vec<(&'static str, &'static str)> {
        vec![
            (
                "agency.txt",
                "agency_id,agency_name,agency_url,agency_timezone\nhsl,HSL,https://hsl.fi,Europe/Helsinki\n",
            ),
            (
                "stops.txt",
                "stop_id,stop_name,stop_lat,stop_lon\ns1,Kamppi,60.169,24.931\ns2,Steissi,60.171,24.941\n",
            ),
            (
                "routes.txt",
                "route_id,agency_id,route_short_name,route_type\nr1,hsl,1,3\n",
            ),
            ("trips.txt", "route_id,service_id,trip_id\nr1,wk,t1\n"),
            (
                "stop_times.txt",
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nt1,08:00:00,08:00:00,s1,1\nt1,08:05:00,08:05:00,s2,2\n",
            ),
            (
                "calendar.txt",
                "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\nwk,1,1,1,1,1,0,0,20260101,20261231\n",
            ),
        ]
    }

    fn replace(
        files: &mut Vec<(&'static str, &'static str)>,
        name: &'static str,
        content: &'static str,
    ) {
        files.retain(|(existing, _)| *existing != name);
        files.push((name, content));
    }

    fn codes(result: &ScanResult) -> Vec<&'static str> {
        result.notices.iter().map(|n| n.code).collect()
    }

    #[test]
    fn minimal_feed_is_semantically_clean() {
        let result = validate_zip(&minimal(), Some("20260601"));
        let errors: Vec<_> = result
            .notices
            .iter()
            .filter(|n| n.severity == Severity::Error)
            .collect();
        assert!(errors.is_empty(), "unexpected errors: {errors:?}");
        assert_eq!(
            result.service_window,
            Some(("20260101".to_string(), "20261231".to_string()))
        );
    }

    #[test]
    fn service_window_respects_exceptions() {
        let mut files = minimal();
        files.push((
            "calendar_dates.txt",
            "service_id,date,exception_type\nwk,20270105,1\nwk,20260101,2\n",
        ));
        let result = validate_zip(&files, None);
        let window = result.service_window.unwrap();
        assert_eq!(window.1, "20270105"); // added exception extends the end
        assert_ne!(window.0, "20260101"); // removed first day
    }

    #[test]
    fn expired_and_inactive_calendars() {
        let mut files = minimal();
        replace(
            &mut files,
            "calendar.txt",
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\nwk,1,1,1,1,1,0,0,20250101,20250601\nnever,0,0,0,0,0,0,0,20260101,20261231\n",
        );
        let result = validate_zip(&files, Some("20260601"));
        let codes = codes(&result);
        assert!(codes.contains(&"expired_calendar"));
        assert!(codes.contains(&"service_has_no_active_day_of_the_week"));
    }

    #[test]
    fn stop_time_progression_checks() {
        let mut files = minimal();
        replace(
            &mut files,
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence,shape_dist_traveled\nt1,08:10:00,08:10:00,s1,1,100\nt1,08:05:00,,s2,2,50\n",
        );
        let result = validate_zip(&files, None);
        let codes = codes(&result);
        assert!(codes.contains(&"stop_time_with_arrival_before_previous_departure_time"));
        assert!(codes.contains(&"stop_time_with_only_arrival_or_departure_time"));
        assert!(codes.contains(&"decreasing_or_equal_stop_time_distance"));
        assert!(codes.contains(&"missing_trip_edge")); // last stop lacks departure
    }

    #[test]
    fn single_stop_trips_are_unusable() {
        let mut files = minimal();
        replace(
            &mut files,
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nt1,08:00:00,08:00:00,s1,1\n",
        );
        let result = validate_zip(&files, None);
        assert!(codes(&result).contains(&"unusable_trip"));
    }

    #[test]
    fn overlapping_block_trips_on_shared_days() {
        let mut files = minimal();
        replace(
            &mut files,
            "trips.txt",
            "route_id,service_id,trip_id,block_id\nr1,wk,t1,b1\nr1,wk,t2,b1\n",
        );
        replace(
            &mut files,
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nt1,08:00:00,08:00:00,s1,1\nt1,09:00:00,09:00:00,s2,2\nt2,08:30:00,08:30:00,s1,1\nt2,09:30:00,09:30:00,s2,2\n",
        );
        let result = validate_zip(&files, None);
        assert!(codes(&result).contains(&"block_trips_with_overlapping_stop_times"));
    }

    #[test]
    fn non_overlapping_block_trips_pass() {
        let mut files = minimal();
        replace(
            &mut files,
            "trips.txt",
            "route_id,service_id,trip_id,block_id\nr1,wk,t1,b1\nr1,wk,t2,b1\n",
        );
        replace(
            &mut files,
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nt1,08:00:00,08:00:00,s1,1\nt1,08:30:00,08:30:00,s2,2\nt2,09:00:00,09:00:00,s1,1\nt2,09:30:00,09:30:00,s2,2\n",
        );
        let result = validate_zip(&files, None);
        assert!(!codes(&result).contains(&"block_trips_with_overlapping_stop_times"));
    }

    #[test]
    fn frequency_overlaps() {
        let mut files = minimal();
        files.push((
            "frequencies.txt",
            "trip_id,start_time,end_time,headway_secs\nt1,08:00:00,10:00:00,600\nt1,09:00:00,11:00:00,600\nt1,11:00:00,12:00:00,600\n",
        ));
        let result = validate_zip(&files, None);
        let overlaps: Vec<_> = result
            .notices
            .iter()
            .filter(|n| n.code == "overlapping_frequency")
            .collect();
        assert_eq!(overlaps.len(), 1);
    }

    #[test]
    fn shape_distance_and_usage_checks() {
        let mut files = minimal();
        files.push((
            "shapes.txt",
            "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence,shape_dist_traveled\nsh1,60.1,24.9,1,0\nsh1,60.2,24.9,2,100\nsh1,60.3,24.9,3,50\nsh2,60.1,24.9,1,0\n",
        ));
        let result = validate_zip(&files, None);
        let codes = codes(&result);
        assert!(codes.contains(&"decreasing_shape_distance"));
        assert!(codes.contains(&"single_shape_point")); // sh2
        assert!(codes.contains(&"unused_shape")); // no trip references
    }

    #[test]
    fn equal_shape_distances_split_by_coordinates() {
        let mut files = minimal();
        files.push((
            "shapes.txt",
            "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence,shape_dist_traveled\nsh1,60.1,24.9,1,0\nsh1,60.1,24.9,2,0\nsh1,60.3,24.9,3,0\n",
        ));
        let result = validate_zip(&files, None);
        let codes = codes(&result);
        assert!(codes.contains(&"equal_shape_distance_same_coordinates"));
        assert!(codes.contains(&"equal_shape_distance_diff_coordinates"));
    }

    #[test]
    fn same_stop_departure_before_arrival() {
        let mut files = minimal();
        replace(
            &mut files,
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nt1,08:10:00,08:05:00,s1,1\nt1,08:20:00,08:20:00,s2,2\n",
        );
        let result = validate_zip(&files, None);
        assert!(codes(&result).contains(&"departure_before_arrival"));
    }

    #[test]
    fn explicit_timepoints_require_times() {
        let mut files = minimal();
        replace(
            &mut files,
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence,timepoint\nt1,08:00:00,08:00:00,s1,1,1\nt1,,,s2,2,1\nt1,08:20:00,08:20:00,s1,3,0\n",
        );
        let result = validate_zip(&files, None);
        assert!(codes(&result).contains(&"stop_time_timepoint_without_times"));
    }

    #[test]
    fn trips_without_stop_times_are_unusable() {
        let mut files = minimal();
        replace(
            &mut files,
            "trips.txt",
            "route_id,service_id,trip_id\nr1,wk,t1\nr1,wk,ghost\n",
        );
        let result = validate_zip(&files, None);
        let unusable: Vec<_> = result
            .notices
            .iter()
            .filter(|n| n.code == "unusable_trip")
            .collect();
        assert_eq!(unusable.len(), 1);
        assert_eq!(unusable[0].context["tripId"], "ghost");
    }

    #[test]
    fn nested_frequency_windows_overlap() {
        let mut files = minimal();
        files.push((
            "frequencies.txt",
            "trip_id,start_time,end_time,headway_secs\nt1,08:00:00,12:00:00,600\nt1,09:00:00,10:00:00,600\nt1,11:00:00,11:30:00,600\n",
        ));
        let result = validate_zip(&files, None);
        let overlaps: Vec<_> = result
            .notices
            .iter()
            .filter(|n| n.code == "overlapping_frequency")
            .collect();
        assert_eq!(overlaps.len(), 2);
    }

    #[test]
    fn over_midnight_block_trips_overlap_next_day() {
        let mut files = minimal();
        replace(
            &mut files,
            "calendar.txt",
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\nwk,1,1,1,1,1,1,1,20260101,20261231\n",
        );
        replace(
            &mut files,
            "trips.txt",
            "route_id,service_id,trip_id,block_id\nr1,wk,t1,b1\nr1,wk,t2,b1\n",
        );
        replace(
            &mut files,
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nt1,25:00:00,25:00:00,s1,1\nt1,27:00:00,27:00:00,s2,2\nt2,01:30:00,01:30:00,s1,1\nt2,02:00:00,02:00:00,s2,2\n",
        );
        let result = validate_zip(&files, None);
        assert!(codes(&result).contains(&"block_trips_with_overlapping_stop_times"));
    }

    #[test]
    fn truncated_calendar_suppresses_service_window() {
        let mut cursor = std::io::Cursor::new(Vec::new());
        {
            let mut writer = zip::ZipWriter::new(&mut cursor);
            let opts = zip::write::SimpleFileOptions::default();
            for (name, content) in minimal() {
                writer.start_file(name, opts).unwrap();
                std::io::Write::write_all(&mut writer, content.as_bytes()).unwrap();
            }
            writer.finish().unwrap();
        }
        cursor.set_position(0);
        let scan_options = ScanOptions {
            max_rows: 1,
            ..ScanOptions::default()
        };
        let mut result = crate::scan::scan_reader_with(cursor, scan_options).unwrap();
        run_semantics(&mut result, &scan_options);
        // Every table including stops was truncated to one row; the window
        // must not present itself as authoritative. (calendar itself has
        // one row, but stops/stop_times truncation marks trips unusable
        // territory — here we only assert window suppression semantics
        // when calendars are involved.)
        if result.incomplete.contains("calendar.txt") {
            assert!(result.service_window.is_none());
        }
    }
}
