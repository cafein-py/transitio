//! Field-format and referential-integrity rule tier, run over the tables
//! the structural scan retained. Notice codes and severities follow the
//! canonical gtfs-validator naming; the rule roadmap and verification
//! status live in plans/validation-rules.md.

use std::collections::{BTreeMap, HashSet};

use crate::fields::{self, FieldKind};
use crate::notice::{Notice, Severity};
use crate::scan::{ScanOptions, ScanResult, Table};

pub fn run_rules(result: &mut ScanResult, options: &ScanOptions) {
    let mut notices = Vec::new();
    // One sampler per file across every rules-stage pass, so the per-file
    // cap holds for the stage as a whole (the structural scan has its own).
    let mut samplers = Samplers::new(options.max_notices_per_file);
    for (name, table) in &result.tables {
        field_rules(name, table, &mut samplers, &mut notices);
    }
    conditional_rules(&result.tables, &mut samplers, &mut notices);
    reference_rules(
        &result.tables,
        &result.incomplete,
        &mut samplers,
        &mut notices,
    );
    samplers.finish(&mut notices);
    result.notices.append(&mut notices);
}

struct Samplers {
    cap: u64,
    by_file: BTreeMap<String, Sampler>,
}

impl Samplers {
    fn new(cap: u64) -> Self {
        Samplers {
            cap,
            by_file: BTreeMap::new(),
        }
    }

    fn file(&mut self, file: &str) -> &mut Sampler {
        let cap = self.cap;
        self.by_file
            .entry(file.to_string())
            .or_insert_with(|| Sampler::new(cap))
    }

    fn finish(self, notices: &mut Vec<Notice>) {
        for (file, sampler) in self.by_file {
            sampler.finish(notices, &file);
        }
    }
}

/// Per-file sampling with separate error/warning quotas, mirroring the
/// structural pass: floods of one severity never crowd out the other.
struct Sampler {
    cap: u64,
    errors: u64,
    warnings: u64,
}

impl Sampler {
    fn new(cap: u64) -> Self {
        Sampler {
            cap,
            errors: 0,
            warnings: 0,
        }
    }

    fn push(&mut self, notices: &mut Vec<Notice>, notice: Notice) {
        let counter = if notice.severity == Severity::Error {
            &mut self.errors
        } else {
            &mut self.warnings
        };
        if *counter < self.cap {
            notices.push(notice);
        }
        *counter += 1;
    }

    fn finish(self, notices: &mut Vec<Notice>, filename: &str) {
        let suppressed_errors = self.errors.saturating_sub(self.cap);
        let suppressed_warnings = self.warnings.saturating_sub(self.cap);
        if suppressed_errors + suppressed_warnings > 0 {
            let severity = if suppressed_errors > 0 {
                Severity::Error
            } else {
                Severity::Warning
            };
            notices.push(
                Notice::new("notice_limit_reached", severity)
                    .with("filename", filename.to_string())
                    .with("suppressedCount", suppressed_errors + suppressed_warnings),
            );
        }
    }
}

/// Untrusted values are clipped before they enter notices, so hostile
/// megabyte-sized fields cannot amplify the serialized report.
fn clip(value: &str) -> String {
    const LIMIT: usize = 120;
    if value.chars().count() <= LIMIT {
        value.to_string()
    } else {
        let mut clipped: String = value.chars().take(LIMIT).collect();
        clipped.push('\u{2026}');
        clipped
    }
}

fn field_notice(
    code: &'static str,
    severity: Severity,
    filename: &'static str,
    csv_row: u64,
    field: &'static str,
    value: &str,
) -> Notice {
    Notice::new(code, severity)
        .with("filename", filename)
        .with("csvRowNumber", csv_row)
        .with("fieldName", field)
        .with("fieldValue", clip(value))
}

fn field_rules(name: &str, table: &Table, samplers: &mut Samplers, notices: &mut Vec<Notice>) {
    let Some(spec) = fields::fields_for(name) else {
        return; // vocabulary not modelled; the structural pass covered it
    };
    // Map each header position to its column spec.
    let columns: Vec<Option<&fields::ColumnSpec>> = table
        .headers
        .iter()
        .map(|header| spec.columns.iter().find(|c| c.name == header))
        .collect();
    let sampler = samplers.file(spec.file);
    if spec.complete {
        for (header, column) in table.headers.iter().zip(&columns) {
            if column.is_none() && !header.is_empty() && header.trim() == header {
                sampler.push(
                    notices,
                    Notice::new("unknown_column", Severity::Info)
                        .with("filename", spec.file)
                        .with("fieldName", clip(header)),
                );
            }
        }
    }

    for row in &table.rows {
        for (index, column) in columns.iter().enumerate() {
            let Some(column) = column else { continue };
            let value = row.fields[index].as_str();
            if value.is_empty() {
                if column.required {
                    sampler.push(
                        notices,
                        field_notice(
                            "missing_required_field",
                            Severity::Error,
                            spec.file,
                            row.csv_row,
                            column.name,
                            value,
                        ),
                    );
                }
                continue;
            }
            if value.trim() != value {
                sampler.push(
                    notices,
                    field_notice(
                        "leading_or_trailing_whitespaces",
                        Severity::Warning,
                        spec.file,
                        row.csv_row,
                        column.name,
                        value,
                    ),
                );
            }
            if value.contains('\n') || value.contains('\r') {
                sampler.push(
                    notices,
                    field_notice(
                        "new_line_in_value",
                        Severity::Warning,
                        spec.file,
                        row.csv_row,
                        column.name,
                        value,
                    ),
                );
            }
            let trimmed = value.trim();
            check_kind(sampler, notices, spec.file, row.csv_row, column, trimmed);
        }
    }
}

fn check_kind(
    sampler: &mut Sampler,
    notices: &mut Vec<Notice>,
    filename: &'static str,
    csv_row: u64,
    column: &fields::ColumnSpec,
    value: &str,
) {
    match &column.kind {
        FieldKind::Text => {}
        FieldKind::Date => {
            if !is_valid_date(value) {
                sampler.push(
                    notices,
                    field_notice(
                        "invalid_date",
                        Severity::Error,
                        filename,
                        csv_row,
                        column.name,
                        value,
                    ),
                );
            }
        }
        FieldKind::Time => {
            if parse_gtfs_time(value).is_none() {
                sampler.push(
                    notices,
                    field_notice(
                        "invalid_time",
                        Severity::Error,
                        filename,
                        csv_row,
                        column.name,
                        value,
                    ),
                );
            }
        }
        FieldKind::Integer { min, max } => match value.parse::<i64>() {
            Err(_) => sampler.push(
                notices,
                field_notice(
                    "invalid_integer",
                    Severity::Error,
                    filename,
                    csv_row,
                    column.name,
                    value,
                ),
            ),
            Ok(parsed) if parsed < *min || parsed > *max => sampler.push(
                notices,
                field_notice(
                    "number_out_of_range",
                    Severity::Error,
                    filename,
                    csv_row,
                    column.name,
                    value,
                ),
            ),
            Ok(_) => {}
        },
        FieldKind::Float { min, max } => match value.parse::<f64>() {
            Err(_) => sampler.push(
                notices,
                field_notice(
                    "invalid_float",
                    Severity::Error,
                    filename,
                    csv_row,
                    column.name,
                    value,
                ),
            ),
            Ok(parsed) if !parsed.is_finite() || parsed < *min || parsed > *max => sampler.push(
                notices,
                field_notice(
                    "number_out_of_range",
                    Severity::Error,
                    filename,
                    csv_row,
                    column.name,
                    value,
                ),
            ),
            Ok(_) => {}
        },
        FieldKind::Latitude => {
            check_coordinate(sampler, notices, filename, csv_row, column, value, 90.0)
        }
        FieldKind::Longitude => {
            check_coordinate(sampler, notices, filename, csv_row, column, value, 180.0)
        }
        FieldKind::Enumeration(values) => match value.parse::<i64>() {
            Err(_) => sampler.push(
                notices,
                field_notice(
                    "invalid_integer",
                    Severity::Error,
                    filename,
                    csv_row,
                    column.name,
                    value,
                ),
            ),
            Ok(parsed) if !values.contains(&parsed) => sampler.push(
                notices,
                field_notice(
                    "unexpected_enum_value",
                    Severity::Warning,
                    filename,
                    csv_row,
                    column.name,
                    value,
                ),
            ),
            Ok(_) => {}
        },
        FieldKind::Timezone => {
            if value.parse::<chrono_tz::Tz>().is_err() {
                sampler.push(
                    notices,
                    field_notice(
                        "invalid_timezone",
                        Severity::Error,
                        filename,
                        csv_row,
                        column.name,
                        value,
                    ),
                );
            }
        }
    }
}

fn check_coordinate(
    sampler: &mut Sampler,
    notices: &mut Vec<Notice>,
    filename: &'static str,
    csv_row: u64,
    column: &fields::ColumnSpec,
    value: &str,
    bound: f64,
) {
    match value.parse::<f64>() {
        Err(_) => sampler.push(
            notices,
            field_notice(
                "invalid_float",
                Severity::Error,
                filename,
                csv_row,
                column.name,
                value,
            ),
        ),
        Ok(parsed) if !parsed.is_finite() || parsed.abs() > bound => sampler.push(
            notices,
            field_notice(
                "number_out_of_range",
                Severity::Error,
                filename,
                csv_row,
                column.name,
                value,
            ),
        ),
        Ok(_) => {}
    }
}

fn is_valid_date(value: &str) -> bool {
    if value.len() != 8 || !value.bytes().all(|b| b.is_ascii_digit()) {
        return false;
    }
    let year: i64 = value[0..4].parse().unwrap_or(0);
    let month: u32 = value[4..6].parse().unwrap_or(0);
    let day: u32 = value[6..8].parse().unwrap_or(0);
    if !(1..=12).contains(&month) || day == 0 {
        return false;
    }
    let days = match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        _ => {
            if (year % 4 == 0 && year % 100 != 0) || year % 400 == 0 {
                29
            } else {
                28
            }
        }
    };
    day <= days
}

/// GTFS times allow hours beyond 24 for over-midnight service.
fn parse_gtfs_time(value: &str) -> Option<u64> {
    let mut parts = value.split(':');
    let (h, m, s) = (parts.next()?, parts.next()?, parts.next()?);
    if parts.next().is_some() || m.len() != 2 || s.len() != 2 || h.is_empty() || h.len() > 3 {
        return None;
    }
    let hours: u64 = h.parse().ok()?;
    let minutes: u64 = m.parse().ok()?;
    let seconds: u64 = s.parse().ok()?;
    if minutes > 59 || seconds > 59 {
        return None;
    }
    Some(hours * 3600 + minutes * 60 + seconds)
}

fn column_index(table: &Table, name: &str) -> Option<usize> {
    table.headers.iter().position(|h| h == name)
}

fn value<'t>(table: &'t Table, row: &'t crate::scan::Row, name: &str) -> &'t str {
    column_index(table, name)
        .map(|i| row.fields[i].as_str())
        .unwrap_or("")
}

fn conditional_rules(
    tables: &BTreeMap<String, Table>,
    samplers: &mut Samplers,
    notices: &mut Vec<Notice>,
) {
    if let Some(stop_times) = tables.get("stop_times.txt") {
        let sampler = samplers.file("stop_times.txt");
        for row in &stop_times.rows {
            let identifiers = ["stop_id", "location_group_id", "location_id"]
                .iter()
                .filter(|column| !value(stop_times, row, column).is_empty())
                .count();
            if identifiers == 0 {
                sampler.push(
                    notices,
                    Notice::new("missing_required_field", Severity::Error)
                        .with("filename", "stop_times.txt")
                        .with("csvRowNumber", row.csv_row)
                        .with("fieldName", "stop_id"),
                );
            } else if identifiers > 1 {
                sampler.push(
                    notices,
                    Notice::new("forbidden_geography_id", Severity::Error)
                        .with("filename", "stop_times.txt")
                        .with("csvRowNumber", row.csv_row),
                );
            }
        }
    }
    if let Some(stops) = tables.get("stops.txt") {
        let sampler = samplers.file("stops.txt");
        for row in &stops.rows {
            let location_type: i64 = value(stops, row, "location_type").parse().unwrap_or(0);
            let needs_location = (0..=2).contains(&location_type);
            let lat = value(stops, row, "stop_lat");
            let lon = value(stops, row, "stop_lon");
            if needs_location && (lat.is_empty() || lon.is_empty()) {
                sampler.push(
                    notices,
                    Notice::new("stop_without_location", Severity::Error)
                        .with("stopId", clip(value(stops, row, "stop_id")))
                        .with("csvRowNumber", row.csv_row),
                );
            } else if let (Ok(lat), Ok(lon)) = (lat.parse::<f64>(), lon.parse::<f64>()) {
                if lat.abs() <= 1.0 && lon.abs() <= 1.0 {
                    sampler.push(
                        notices,
                        Notice::new("point_near_origin", Severity::Warning)
                            .with("filename", "stops.txt")
                            .with("csvRowNumber", row.csv_row),
                    );
                } else if lat.abs() >= 89.0 {
                    sampler.push(
                        notices,
                        Notice::new("point_near_pole", Severity::Warning)
                            .with("filename", "stops.txt")
                            .with("csvRowNumber", row.csv_row),
                    );
                }
            }
        }
        parent_station_rules(stops, sampler, notices);
    }
    if let Some(transfers) = tables.get("transfers.txt") {
        let sampler = samplers.file("transfers.txt");
        for row in &transfers.rows {
            // transfer_type 2 (minimum-time transfer) requires the time.
            if value(transfers, row, "transfer_type").trim() == "2"
                && value(transfers, row, "min_transfer_time").is_empty()
            {
                sampler.push(
                    notices,
                    Notice::new("missing_required_field", Severity::Error)
                        .with("filename", "transfers.txt")
                        .with("csvRowNumber", row.csv_row)
                        .with("fieldName", "min_transfer_time"),
                );
            }
        }
    }
    if let Some(routes) = tables.get("routes.txt") {
        let multiple_agencies = tables
            .get("agency.txt")
            .map(|agency| agency.rows.len() > 1)
            .unwrap_or(false);
        let sampler = samplers.file("routes.txt");
        for row in &routes.rows {
            // With multiple agencies every route must name its agency.
            if multiple_agencies && value(routes, row, "agency_id").is_empty() {
                sampler.push(
                    notices,
                    Notice::new("missing_required_field", Severity::Error)
                        .with("filename", "routes.txt")
                        .with("csvRowNumber", row.csv_row)
                        .with("fieldName", "agency_id"),
                );
            }
            if value(routes, row, "route_short_name").trim().is_empty()
                && value(routes, row, "route_long_name").trim().is_empty()
            {
                sampler.push(
                    notices,
                    Notice::new("route_both_short_and_long_name_missing", Severity::Error)
                        .with("routeId", clip(value(routes, row, "route_id")))
                        .with("csvRowNumber", row.csv_row),
                );
            }
        }
    }
    if let Some(agency) = tables.get("agency.txt") {
        if agency.rows.len() > 1 {
            let sampler = samplers.file("agency.txt");
            let mut timezones: Vec<(&str, u64)> = Vec::new();
            for row in &agency.rows {
                if value(agency, row, "agency_id").is_empty() {
                    sampler.push(
                        notices,
                        Notice::new("missing_required_field", Severity::Error)
                            .with("filename", "agency.txt")
                            .with("csvRowNumber", row.csv_row)
                            .with("fieldName", "agency_id"),
                    );
                }
                timezones.push((value(agency, row, "agency_timezone"), row.csv_row));
            }
            let expected = timezones[0].0;
            for (timezone, csv_row) in &timezones[1..] {
                if timezone != &expected {
                    sampler.push(
                        notices,
                        Notice::new("inconsistent_agency_timezone", Severity::Error)
                            .with("csvRowNumber", *csv_row)
                            .with("expected", clip(expected))
                            .with("actual", clip(timezone)),
                    );
                }
            }
        }
    }
    for (file, start, end) in [
        ("calendar.txt", "start_date", "end_date"),
        ("frequencies.txt", "start_time", "end_time"),
    ] {
        let Some(table) = tables.get(file) else {
            continue;
        };
        let sortable = |raw: &str| -> Option<u64> {
            if file == "calendar.txt" {
                is_valid_date(raw).then(|| raw.parse().unwrap_or(0))
            } else {
                parse_gtfs_time(raw)
            }
        };
        let sampler = samplers.file(file);
        for row in &table.rows {
            let (Some(a), Some(b)) = (
                sortable(value(table, row, start)),
                sortable(value(table, row, end)),
            ) else {
                continue; // format notices already cover unparseable values
            };
            if a > b {
                sampler.push(
                    notices,
                    Notice::new("start_and_end_range_out_of_order", Severity::Error)
                        .with("filename", file)
                        .with("csvRowNumber", row.csv_row),
                );
            } else if a == b && file == "frequencies.txt" {
                sampler.push(
                    notices,
                    Notice::new("start_and_end_range_equal", Severity::Warning)
                        .with("filename", file)
                        .with("csvRowNumber", row.csv_row),
                );
            }
        }
    }
}

fn parent_station_rules(stops: &Table, sampler: &mut Sampler, notices: &mut Vec<Notice>) {
    let mut location_types: BTreeMap<&str, i64> = BTreeMap::new();
    for row in &stops.rows {
        let id = value(stops, row, "stop_id");
        if !id.is_empty() {
            location_types.insert(id, value(stops, row, "location_type").parse().unwrap_or(0));
        }
    }
    for row in &stops.rows {
        let child_type: i64 = value(stops, row, "location_type").parse().unwrap_or(0);
        let parent = value(stops, row, "parent_station");
        if parent.is_empty() {
            // Entrances, generic nodes and boarding areas require a parent.
            if (2..=4).contains(&child_type) {
                sampler.push(
                    notices,
                    Notice::new("missing_required_field", Severity::Error)
                        .with("filename", "stops.txt")
                        .with("csvRowNumber", row.csv_row)
                        .with("fieldName", "parent_station"),
                );
            }
            continue;
        }
        if child_type == 1 {
            sampler.push(
                notices,
                Notice::new("station_with_parent_station", Severity::Error)
                    .with("stopId", clip(value(stops, row, "stop_id")))
                    .with("csvRowNumber", row.csv_row)
                    .with("parentStation", clip(parent)),
            );
            continue;
        }
        let Some(parent_type) = location_types.get(parent) else {
            continue; // dangling parents are foreign_key_violation territory
        };
        let expected = if child_type == 4 { 0 } else { 1 };
        if *parent_type != expected {
            sampler.push(
                notices,
                Notice::new("wrong_parent_location_type", Severity::Error)
                    .with("stopId", clip(value(stops, row, "stop_id")))
                    .with("csvRowNumber", row.csv_row)
                    .with("locationType", child_type)
                    .with("parentStation", clip(parent))
                    .with("parentLocationType", *parent_type)
                    .with("expectedLocationType", expected),
            );
        }
    }
}

fn reference_rules(
    tables: &BTreeMap<String, Table>,
    incomplete: &std::collections::BTreeSet<String>,
    samplers: &mut Samplers,
    notices: &mut Vec<Notice>,
) {
    let collect = |file: &str, column: &str| -> HashSet<String> {
        tables
            .get(file)
            .and_then(|table| {
                column_index(table, column).map(|index| {
                    table
                        .rows
                        .iter()
                        .map(|row| row.fields[index].clone())
                        .filter(|id| !id.is_empty())
                        .collect()
                })
            })
            .unwrap_or_default()
    };
    let agencies = collect("agency.txt", "agency_id");
    let stops = collect("stops.txt", "stop_id");
    let zones = collect("stops.txt", "zone_id");
    let routes = collect("routes.txt", "route_id");
    let trips = collect("trips.txt", "trip_id");
    let shapes = collect("shapes.txt", "shape_id");
    let levels = collect("levels.txt", "level_id");
    let fares = collect("fare_attributes.txt", "fare_id");
    let location_groups = collect("location_groups.txt", "location_group_id");
    let booking_rules = collect("booking_rules.txt", "booking_rule_id");
    let networks = collect("networks.txt", "network_id");
    let mut services = collect("calendar.txt", "service_id");
    services.extend(collect("calendar_dates.txt", "service_id"));
    let services_incomplete =
        incomplete.contains("calendar.txt") || incomplete.contains("calendar_dates.txt");

    // (child file, child column, parent file, parent column, parent ids)
    let checks: &[(
        &'static str,
        &'static str,
        &'static str,
        &'static str,
        &HashSet<String>,
    )] = &[
        ("trips.txt", "route_id", "routes.txt", "route_id", &routes),
        (
            "trips.txt",
            "service_id",
            "calendar.txt",
            "service_id",
            &services,
        ),
        ("trips.txt", "shape_id", "shapes.txt", "shape_id", &shapes),
        ("stop_times.txt", "trip_id", "trips.txt", "trip_id", &trips),
        ("stop_times.txt", "stop_id", "stops.txt", "stop_id", &stops),
        (
            "stop_times.txt",
            "location_group_id",
            "location_groups.txt",
            "location_group_id",
            &location_groups,
        ),
        (
            "stop_times.txt",
            "pickup_booking_rule_id",
            "booking_rules.txt",
            "booking_rule_id",
            &booking_rules,
        ),
        (
            "stop_times.txt",
            "drop_off_booking_rule_id",
            "booking_rules.txt",
            "booking_rule_id",
            &booking_rules,
        ),
        (
            "routes.txt",
            "network_id",
            "networks.txt",
            "network_id",
            &networks,
        ),
        (
            "stops.txt",
            "parent_station",
            "stops.txt",
            "stop_id",
            &stops,
        ),
        ("stops.txt", "level_id", "levels.txt", "level_id", &levels),
        (
            "routes.txt",
            "agency_id",
            "agency.txt",
            "agency_id",
            &agencies,
        ),
        ("frequencies.txt", "trip_id", "trips.txt", "trip_id", &trips),
        (
            "transfers.txt",
            "from_stop_id",
            "stops.txt",
            "stop_id",
            &stops,
        ),
        (
            "transfers.txt",
            "to_stop_id",
            "stops.txt",
            "stop_id",
            &stops,
        ),
        (
            "transfers.txt",
            "from_route_id",
            "routes.txt",
            "route_id",
            &routes,
        ),
        (
            "transfers.txt",
            "to_route_id",
            "routes.txt",
            "route_id",
            &routes,
        ),
        (
            "transfers.txt",
            "from_trip_id",
            "trips.txt",
            "trip_id",
            &trips,
        ),
        (
            "transfers.txt",
            "to_trip_id",
            "trips.txt",
            "trip_id",
            &trips,
        ),
        (
            "pathways.txt",
            "from_stop_id",
            "stops.txt",
            "stop_id",
            &stops,
        ),
        ("pathways.txt", "to_stop_id", "stops.txt", "stop_id", &stops),
        (
            "fare_attributes.txt",
            "agency_id",
            "agency.txt",
            "agency_id",
            &agencies,
        ),
        (
            "fare_rules.txt",
            "fare_id",
            "fare_attributes.txt",
            "fare_id",
            &fares,
        ),
        (
            "fare_rules.txt",
            "route_id",
            "routes.txt",
            "route_id",
            &routes,
        ),
        (
            "fare_rules.txt",
            "origin_id",
            "stops.txt",
            "zone_id",
            &zones,
        ),
        (
            "fare_rules.txt",
            "destination_id",
            "stops.txt",
            "zone_id",
            &zones,
        ),
        (
            "fare_rules.txt",
            "contains_id",
            "stops.txt",
            "zone_id",
            &zones,
        ),
        (
            "attributions.txt",
            "agency_id",
            "agency.txt",
            "agency_id",
            &agencies,
        ),
        (
            "attributions.txt",
            "route_id",
            "routes.txt",
            "route_id",
            &routes,
        ),
        (
            "attributions.txt",
            "trip_id",
            "trips.txt",
            "trip_id",
            &trips,
        ),
    ];
    for &(child_file, child_column, parent_file, parent_column, parents) in checks {
        // Only unreliable parent tables (truncated, unreadable, refused)
        // suppress the check; an absent or empty parent leaves child
        // references genuinely dangling and they are reported.
        let parent_unreliable = if parent_column == "service_id" {
            services_incomplete
        } else {
            incomplete.contains(parent_file)
        };
        if parent_unreliable {
            continue;
        }
        let sampler = samplers.file(child_file);
        foreign_key_check(
            tables,
            sampler,
            notices,
            child_file,
            child_column,
            parent_file,
            parent_column,
            parents,
        );
    }
}

#[allow(clippy::too_many_arguments)]
fn foreign_key_check(
    tables: &BTreeMap<String, Table>,
    sampler: &mut Sampler,
    notices: &mut Vec<Notice>,
    child_file: &str,
    child_column: &str,
    parent_file: &str,
    parent_column: &str,
    parents: &HashSet<String>,
) {
    let Some(table) = tables.get(child_file) else {
        return;
    };
    let Some(index) = column_index(table, child_column) else {
        return;
    };
    for row in &table.rows {
        let id = row.fields[index].as_str();
        if id.is_empty() || parents.contains(id) {
            continue;
        }
        sampler.push(
            notices,
            Notice::new("foreign_key_violation", Severity::Error)
                .with("childFilename", child_file.to_string())
                .with("childFieldName", child_column.to_string())
                .with("parentFilename", parent_file.to_string())
                .with("parentFieldName", parent_column.to_string())
                .with("fieldValue", clip(id))
                .with("csvRowNumber", row.csv_row),
        );
    }
}

#[cfg(test)]
mod tests {
    use std::io::Cursor;

    use super::*;
    use crate::scan::scan_reader;

    fn validate_zip(files: &[(&str, &str)]) -> ScanResult {
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
        run_rules(&mut result, &ScanOptions::default());
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
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nt1,08:00:00,08:00:00,s1,1\nt1,25:05:00,25:05:00,s2,2\n",
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
    fn minimal_feed_passes_all_tiers() {
        let result = validate_zip(&minimal());
        let errors: Vec<_> = result
            .notices
            .iter()
            .filter(|n| n.severity == Severity::Error)
            .collect();
        assert!(errors.is_empty(), "unexpected errors: {errors:?}");
    }

    #[test]
    fn field_formats_are_checked() {
        let mut files = minimal();
        replace(
            &mut files,
            "calendar.txt",
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\nwk,1,1,9,1,1,0,0,2026011,20261232\n",
        );
        replace(
            &mut files,
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nt1,8h00,08:00:00,s1,one\n",
        );
        replace(
            &mut files,
            "agency.txt",
            "agency_id,agency_name,agency_url,agency_timezone\nhsl,HSL,https://hsl.fi,Europe/Nowhere\n",
        );
        let result = validate_zip(&files);
        let codes = codes(&result);
        assert!(codes.contains(&"invalid_date")); // 2026011 (7 digits)
        assert!(codes.contains(&"invalid_time")); // 8h00
        assert!(codes.contains(&"invalid_integer")); // stop_sequence "one"
        assert!(codes.contains(&"invalid_timezone")); // Europe/Nowhere
        assert!(codes.contains(&"unexpected_enum_value")); // wednesday 9
    }

    #[test]
    fn empty_required_fields_are_noticed() {
        let mut files = minimal();
        replace(
            &mut files,
            "trips.txt",
            "route_id,service_id,trip_id\nr1,,t1\n",
        );
        let result = validate_zip(&files);
        assert!(codes(&result).contains(&"missing_required_field"));
    }

    #[test]
    fn conditional_stop_and_route_rules() {
        let mut files = minimal();
        replace(
            &mut files,
            "stops.txt",
            "stop_id,stop_name,stop_lat,stop_lon\ns1,Kamppi,60.169,24.931\ns2,NoCoords,,\n",
        );
        replace(
            &mut files,
            "routes.txt",
            "route_id,agency_id,route_short_name,route_long_name,route_type\nr1,hsl,,,3\n",
        );
        let result = validate_zip(&files);
        let codes = codes(&result);
        assert!(codes.contains(&"stop_without_location"));
        assert!(codes.contains(&"route_both_short_and_long_name_missing"));
    }

    #[test]
    fn coordinate_sanity() {
        let mut files = minimal();
        replace(
            &mut files,
            "stops.txt",
            "stop_id,stop_name,stop_lat,stop_lon\ns1,Origin,0.5,0.5\ns2,Pole,89.5,24.9\n",
        );
        let result = validate_zip(&files);
        let codes = codes(&result);
        assert!(codes.contains(&"point_near_origin"));
        assert!(codes.contains(&"point_near_pole"));
    }

    #[test]
    fn foreign_keys_are_checked() {
        let mut files = minimal();
        replace(
            &mut files,
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nt1,08:00:00,08:00:00,ghost,1\nt1,08:05:00,08:05:00,s2,2\n",
        );
        replace(
            &mut files,
            "trips.txt",
            "route_id,service_id,trip_id\nr1,nosvc,t1\n",
        );
        let result = validate_zip(&files);
        let violations: Vec<_> = result
            .notices
            .iter()
            .filter(|n| n.code == "foreign_key_violation")
            .collect();
        assert_eq!(violations.len(), 2, "got: {violations:?}");
    }

    #[test]
    fn agency_consistency_for_multiple_agencies() {
        let mut files = minimal();
        replace(
            &mut files,
            "agency.txt",
            "agency_id,agency_name,agency_url,agency_timezone\nhsl,HSL,https://hsl.fi,Europe/Helsinki\n,Other,https://other.fi,Europe/Stockholm\n",
        );
        let result = validate_zip(&files);
        let codes = codes(&result);
        assert!(codes.contains(&"missing_required_field")); // empty agency_id
        assert!(codes.contains(&"inconsistent_agency_timezone"));
    }

    #[test]
    fn parent_station_relations() {
        let mut files = minimal();
        replace(
            &mut files,
            "stops.txt",
            "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station\nst1,Station,60.17,24.93,1,\ns1,Platform,60.169,24.931,0,st1\ns2,BadParent,60.171,24.941,0,s1\n",
        );
        let result = validate_zip(&files);
        let wrong: Vec<_> = result
            .notices
            .iter()
            .filter(|n| n.code == "wrong_parent_location_type")
            .collect();
        assert_eq!(wrong.len(), 1);
        assert_eq!(wrong[0].context["stopId"], "s2");
    }

    #[test]
    fn range_order_rules() {
        let mut files = minimal();
        replace(
            &mut files,
            "calendar.txt",
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\nwk,1,1,1,1,1,0,0,20261231,20260101\n",
        );
        files.push((
            "frequencies.txt",
            "trip_id,start_time,end_time,headway_secs\nt1,08:00:00,08:00:00,600\n",
        ));
        let result = validate_zip(&files);
        let codes = codes(&result);
        assert!(codes.contains(&"start_and_end_range_out_of_order"));
        assert!(codes.contains(&"start_and_end_range_equal"));
    }

    #[test]
    fn unknown_columns_are_informational() {
        let mut files = minimal();
        replace(
            &mut files,
            "trips.txt",
            "route_id,service_id,trip_id,vehicle_flavor\nr1,wk,t1,robusta\n",
        );
        let result = validate_zip(&files);
        let unknown = result
            .notices
            .iter()
            .find(|n| n.code == "unknown_column")
            .expect("unknown_column");
        assert_eq!(unknown.severity, Severity::Info);
        assert_eq!(unknown.context["fieldName"], "vehicle_flavor");
    }

    #[test]
    fn gtfs_times_allow_over_midnight() {
        assert_eq!(parse_gtfs_time("25:05:00"), Some(25 * 3600 + 300));
        assert_eq!(parse_gtfs_time("8:00:00"), Some(8 * 3600));
        assert!(parse_gtfs_time("08:60:00").is_none());
        assert!(parse_gtfs_time("08:00").is_none());
    }

    #[test]
    fn impossible_calendar_dates_are_invalid() {
        assert!(is_valid_date("20260228"));
        assert!(is_valid_date("20240229")); // leap year
        assert!(!is_valid_date("20250229"));
        assert!(!is_valid_date("20260231"));
        assert!(!is_valid_date("20260431"));
    }

    #[test]
    fn stop_times_geography_exclusivity() {
        let mut files = minimal();
        replace(
            &mut files,
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,location_id,stop_sequence\nt1,08:00:00,08:00:00,,,1\nt1,08:05:00,08:05:00,s2,loc1,2\n",
        );
        let result = validate_zip(&files);
        let codes = codes(&result);
        assert!(codes.contains(&"missing_required_field")); // neither id
        assert!(codes.contains(&"forbidden_geography_id")); // both ids
    }

    #[test]
    fn station_parent_conditionals() {
        let mut files = minimal();
        replace(
            &mut files,
            "stops.txt",
            "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station\nst1,Station,60.17,24.93,1,st2\nst2,Station2,60.18,24.94,1,\ne1,Entrance,60.17,24.93,2,\n",
        );
        let result = validate_zip(&files);
        let codes = codes(&result);
        assert!(codes.contains(&"station_with_parent_station")); // st1
        assert!(codes.contains(&"missing_required_field")); // e1 parentless
    }

    #[test]
    fn oversized_field_values_are_clipped_in_notices() {
        let long_id = "x".repeat(5000);
        let content = format!("route_id,service_id,trip_id\nr1,{long_id},t1\n");
        let mut files = minimal();
        files.retain(|(name, _)| *name != "trips.txt");
        let content: &'static str = Box::leak(content.into_boxed_str());
        files.push(("trips.txt", content));
        let result = validate_zip(&files);
        let violation = result
            .notices
            .iter()
            .find(|n| n.code == "foreign_key_violation")
            .expect("foreign_key_violation");
        let value = violation.context["fieldValue"].as_str().unwrap();
        assert!(
            value.chars().count() <= 121,
            "not clipped: {} chars",
            value.len()
        );
    }

    #[test]
    fn truncated_parent_tables_suppress_fk_checks() {
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
        run_rules(&mut result, &scan_options);
        // stops.txt was truncated to one row; s2 references must not be
        // reported as dangling.
        let stop_violations: Vec<_> = result
            .notices
            .iter()
            .filter(|n| {
                n.code == "foreign_key_violation" && n.context["parentFilename"] == "stops.txt"
            })
            .collect();
        assert!(stop_violations.is_empty(), "got: {stop_violations:?}");
    }

    #[test]
    fn multi_agency_routes_require_agency_id() {
        let mut files = minimal();
        replace(
            &mut files,
            "agency.txt",
            "agency_id,agency_name,agency_url,agency_timezone\nhsl,HSL,https://hsl.fi,Europe/Helsinki\nvr,VR,https://vr.fi,Europe/Helsinki\n",
        );
        replace(
            &mut files,
            "routes.txt",
            "route_id,agency_id,route_short_name,route_type\nr1,,1,3\n",
        );
        let result = validate_zip(&files);
        let missing: Vec<_> = result
            .notices
            .iter()
            .filter(|n| {
                n.code == "missing_required_field"
                    && n.context["filename"] == "routes.txt"
                    && n.context["fieldName"] == "agency_id"
            })
            .collect();
        assert_eq!(missing.len(), 1);
    }

    #[test]
    fn fare_rules_reference_fare_attributes() {
        let mut files = minimal();
        files.push((
            "fare_attributes.txt",
            "fare_id,price,currency_type,payment_method,transfers\nsingle,3.20,EUR,1,0\n",
        ));
        files.push(("fare_rules.txt", "fare_id,route_id\nghost,r1\n"));
        let result = validate_zip(&files);
        let dangling: Vec<_> = result
            .notices
            .iter()
            .filter(|n| {
                n.code == "foreign_key_violation"
                    && n.context["parentFilename"] == "fare_attributes.txt"
            })
            .collect();
        assert_eq!(dangling.len(), 1);
    }

    #[test]
    fn timed_transfers_require_min_transfer_time() {
        let mut files = minimal();
        files.push((
            "transfers.txt",
            "from_stop_id,to_stop_id,transfer_type,min_transfer_time\ns1,s2,2,\ns2,s1,2,180\n",
        ));
        let result = validate_zip(&files);
        let missing: Vec<_> = result
            .notices
            .iter()
            .filter(|n| {
                n.code == "missing_required_field" && n.context["fieldName"] == "min_transfer_time"
            })
            .collect();
        assert_eq!(missing.len(), 1);
    }
}
