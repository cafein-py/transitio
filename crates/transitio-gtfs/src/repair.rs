//! Feed repair under the gtfstidy contract: the repaired feed serves the
//! same trips with the same attributes from the passenger's perspective.
//! Two passes — default-value normalisation of fixable optional fields and
//! drop-entities with cascading removals — and every action is logged as a
//! structured fix record naming the notice that motivated it. Semantically
//! ambiguous data is dropped or left alone, never reconstructed.

use std::collections::{BTreeMap, HashSet};
use std::io::Write;
use std::path::Path;

use serde::Serialize;

use crate::scan::{ScanOptions, ScanResult, Table};
use crate::{rules, scan, semantics};

/// Optional enumerated fields with a spec-defined default: an invalid value
/// is reset to the default instead of dropping the whole row.
const FIELD_DEFAULTS: &[(&str, &str, &str)] = &[
    ("stops.txt", "location_type", "0"),
    ("stops.txt", "wheelchair_boarding", "0"),
    ("routes.txt", "continuous_pickup", "1"),
    ("routes.txt", "continuous_drop_off", "1"),
    ("trips.txt", "wheelchair_accessible", "0"),
    ("trips.txt", "bikes_allowed", "0"),
    ("trips.txt", "cars_allowed", "0"),
    ("stop_times.txt", "pickup_type", "0"),
    ("stop_times.txt", "drop_off_type", "0"),
    ("stop_times.txt", "timepoint", "1"),
    ("frequencies.txt", "exact_times", "0"),
];

/// Dangling references in these child fields are cleared (the entity
/// survives without the optional link); any other dangling reference is
/// load-bearing or semantics-bearing (fare and transfer selectors would
/// silently broaden if cleared) and dooms its row instead.
const CLEARABLE_REFERENCES: &[(&str, &str)] = &[
    ("trips.txt", "shape_id"),
    ("stops.txt", "level_id"),
    ("routes.txt", "network_id"),
    ("fare_attributes.txt", "agency_id"),
    ("attributions.txt", "agency_id"),
    ("attributions.txt", "route_id"),
    ("attributions.txt", "trip_id"),
    ("stop_times.txt", "pickup_booking_rule_id"),
    ("stop_times.txt", "drop_off_booking_rule_id"),
];

#[derive(Serialize, Debug)]
pub struct Fix {
    pub action: &'static str,
    pub filename: String,
    #[serde(rename = "csvRowNumber")]
    pub csv_row: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub field: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub old_value: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub new_value: Option<String>,
    pub triggered_by: String,
}

pub struct RepairResult {
    pub fixes: Vec<Fix>,
    pub validation: ScanResult,
}

pub fn repair(path: &Path, output: &Path, options: ScanOptions) -> Result<RepairResult, String> {
    let mut options = options;
    if options.reference_date.is_none() {
        options.reference_date = Some(chrono::Utc::now().date_naive());
    }
    let mut result = scan::scan_with(path, options)?;
    rules::run_rules(&mut result, &options);
    semantics::run_semantics(&mut result, &options);

    // Repairing a truncated snapshot would silently rewrite a subset of
    // the feed as if it were whole; refuse instead.
    if !result.incomplete.is_empty()
        || result
            .notices
            .iter()
            .any(|n| matches!(n.code, "too_many_rows" | "notice_limit_reached"))
    {
        // Sampled notices would leave defects invisibly unrepaired.
        return Err(
            "feed exceeds the scan or notice budgets; raise the limits to repair it".to_string(),
        );
    }

    if output
        .symlink_metadata()
        .map(|m| m.is_symlink())
        .unwrap_or(false)
    {
        return Err("output path is a symlink; refusing to follow it".to_string());
    }
    let same = match (path.canonicalize(), output.canonicalize()) {
        (Ok(a), Ok(b)) => a == b,
        _ => false,
    };
    if same {
        return Err("output path aliases the source archive".to_string());
    }

    let mut fixes = Vec::new();
    default_value_pass(&mut result, &mut fixes);
    drop_entities_pass(&mut result, &mut fixes);

    // Write to a temporary sibling, revalidate it, then move it into
    // place, so a failure never leaves a truncated file at `output`.
    let staging = output.with_extension("zip.part");
    if staging
        .symlink_metadata()
        .map(|m| m.is_symlink())
        .unwrap_or(false)
    {
        return Err("staging path is a symlink; refusing to follow it".to_string());
    }
    let _ = std::fs::remove_file(&staging);
    write_zip(&result.tables, &staging)?;

    // The returned validation describes the REPAIRED feed, so callers see
    // exactly which notices remain.
    let validation_result = scan::scan_with(&staging, options);
    let mut validation = match validation_result {
        Ok(validation) => validation,
        Err(error) => {
            let _ = std::fs::remove_file(&staging);
            return Err(error);
        }
    };
    rules::run_rules(&mut validation, &options);
    semantics::run_semantics(&mut validation, &options);
    std::fs::rename(&staging, output)
        .map_err(|e| format!("cannot move repaired feed into place: {e}"))?;
    Ok(RepairResult { fixes, validation })
}

fn column(table: &Table, name: &str) -> Option<usize> {
    table.headers.iter().position(|h| h == name)
}

/// Reset invalid values of optional enumerated fields to their spec
/// default. Only fields the validator flagged are touched.
fn default_value_pass(result: &mut ScanResult, fixes: &mut Vec<Fix>) {
    // (filename, csvRowNumber, fieldName) -> triggering notice code.
    let flagged: BTreeMap<(String, u64, String), String> = result
        .notices
        .iter()
        .filter(|n| {
            matches!(
                n.code,
                "unexpected_enum_value" | "invalid_integer" | "number_out_of_range"
            )
        })
        .filter_map(|n| {
            Some((
                (
                    n.context.get("filename")?.as_str()?.to_string(),
                    n.context.get("csvRowNumber")?.as_u64()?,
                    n.context.get("fieldName")?.as_str()?.to_string(),
                ),
                n.code.to_string(),
            ))
        })
        .collect();
    if flagged.is_empty() {
        return;
    }
    for (file, field, default) in FIELD_DEFAULTS {
        let Some(table) = result.tables.get_mut(*file) else {
            continue;
        };
        let Some(index) = column(table, field) else {
            continue;
        };
        for row in &mut table.rows {
            let key = (file.to_string(), row.csv_row, field.to_string());
            let Some(trigger) = flagged.get(&key) else {
                continue;
            };
            let old = std::mem::replace(&mut row.fields[index], default.to_string());
            fixes.push(Fix {
                action: "default_value",
                filename: file.to_string(),
                csv_row: row.csv_row,
                field: Some(field.to_string()),
                old_value: Some(old),
                new_value: Some(default.to_string()),
                triggered_by: trigger.clone(),
            });
        }
    }
}

/// Drop entities with unfixable errors and cascade the removals until the
/// feed is referentially consistent again.
fn drop_entities_pass(result: &mut ScanResult, fixes: &mut Vec<Fix>) {
    // Row-level drops the validator motivated directly, plus optional
    // dangling references to clear in place.
    let mut doomed: BTreeMap<String, HashSet<u64>> = BTreeMap::new();
    let mut cleared: BTreeMap<(String, String), HashSet<u64>> = BTreeMap::new();
    let mut triggers: BTreeMap<(String, u64), String> = BTreeMap::new();
    for notice in &result.notices {
        match notice.code {
            "foreign_key_violation" => {
                let (Some(file), Some(field), Some(row)) = (
                    notice.context.get("childFilename").and_then(|v| v.as_str()),
                    notice
                        .context
                        .get("childFieldName")
                        .and_then(|v| v.as_str()),
                    notice.context.get("csvRowNumber").and_then(|v| v.as_u64()),
                ) else {
                    continue;
                };
                if CLEARABLE_REFERENCES.contains(&(file, field)) {
                    cleared
                        .entry((file.to_string(), field.to_string()))
                        .or_default()
                        .insert(row);
                } else {
                    doomed.entry(file.to_string()).or_default().insert(row);
                }
            }
            "stop_without_location"
            | "departure_before_arrival"
            | "stop_time_with_arrival_before_previous_departure_time"
            | "duplicate_key"
            | "missing_required_field"
            | "invalid_date"
            | "invalid_time" => {
                let file = match notice.code {
                    "stop_without_location" => "stops.txt",
                    "departure_before_arrival"
                    | "stop_time_with_arrival_before_previous_departure_time" => "stop_times.txt",
                    _ => match notice.context.get("filename").and_then(|v| v.as_str()) {
                        Some(file) => file,
                        None => continue,
                    },
                };
                if let Some(row) = notice.context.get("csvRowNumber").and_then(|v| v.as_u64()) {
                    doomed.entry(file.to_string()).or_default().insert(row);
                    triggers.insert((file.to_string(), row), notice.code.to_string());
                }
            }
            _ => {}
        }
    }

    for ((file, field), rows) in &cleared {
        let Some(table) = result.tables.get_mut(file) else {
            continue;
        };
        let Some(index) = column(table, field) else {
            continue;
        };
        for row in &mut table.rows {
            if rows.contains(&row.csv_row) {
                let old = std::mem::take(&mut row.fields[index]);
                fixes.push(Fix {
                    action: "clear_reference",
                    filename: file.clone(),
                    csv_row: row.csv_row,
                    field: Some(field.clone()),
                    old_value: Some(old),
                    new_value: Some(String::new()),
                    triggered_by: "foreign_key_violation".to_string(),
                });
            }
        }
    }
    // H-3 conditional clears: parent_station only when the stop's
    // location type does not require a parent; routes.agency_id only in
    // single-agency feeds. Otherwise the dangling reference dooms the row.
    conditional_reference_repairs(result, fixes, &mut doomed);
    for (file, rows) in &doomed {
        drop_rows(result, fixes, file, rows, &triggers);
    }
    cascade(result, fixes);
}

fn conditional_reference_repairs(
    result: &mut ScanResult,
    fixes: &mut Vec<Fix>,
    doomed: &mut BTreeMap<String, HashSet<u64>>,
) {
    let dangling: Vec<(String, String, u64)> = result
        .notices
        .iter()
        .filter(|n| n.code == "foreign_key_violation")
        .filter_map(|n| {
            let file = n.context.get("childFilename")?.as_str()?;
            let field = n.context.get("childFieldName")?.as_str()?;
            let row = n.context.get("csvRowNumber")?.as_u64()?;
            if (file, field) == ("stops.txt", "parent_station")
                || (file, field) == ("routes.txt", "agency_id")
            {
                Some((file.to_string(), field.to_string(), row))
            } else {
                None
            }
        })
        .collect();
    if dangling.is_empty() {
        return;
    }
    let single_agency = result
        .tables
        .get("agency.txt")
        .map(|t| t.rows.len() <= 1)
        .unwrap_or(true);
    for (file, field, csv_row) in dangling {
        let Some(table) = result.tables.get_mut(&file) else {
            continue;
        };
        let (Some(index), location_index) = (column(table, &field), column(table, "location_type"))
        else {
            continue;
        };
        for row in &mut table.rows {
            if row.csv_row != csv_row {
                continue;
            }
            let clearable = if field == "parent_station" {
                let location_type = location_index.map(|i| row.fields[i].trim()).unwrap_or("");
                matches!(location_type, "" | "0" | "1")
            } else {
                single_agency
            };
            if clearable {
                let old = std::mem::take(&mut row.fields[index]);
                fixes.push(Fix {
                    action: "clear_reference",
                    filename: file.clone(),
                    csv_row,
                    field: Some(field.clone()),
                    old_value: Some(old),
                    new_value: Some(String::new()),
                    triggered_by: "foreign_key_violation".to_string(),
                });
            } else {
                doomed.entry(file.clone()).or_default().insert(csv_row);
            }
        }
    }
}

fn drop_rows(
    result: &mut ScanResult,
    fixes: &mut Vec<Fix>,
    file: &str,
    rows: &HashSet<u64>,
    triggers: &BTreeMap<(String, u64), String>,
) {
    let Some(table) = result.tables.get_mut(file) else {
        return;
    };
    let mut kept = Vec::with_capacity(table.rows.len());
    for row in table.rows.drain(..) {
        if rows.contains(&row.csv_row) {
            let trigger = triggers
                .get(&(file.to_string(), row.csv_row))
                .cloned()
                .unwrap_or_else(|| "foreign_key_violation".to_string());
            fixes.push(Fix {
                action: "drop_entity",
                filename: file.to_string(),
                csv_row: row.csv_row,
                field: None,
                old_value: None,
                new_value: None,
                triggered_by: trigger,
            });
        } else {
            kept.push(row);
        }
    }
    table.rows = kept;
}

/// Re-derive referential consistency after drops: stop_times referencing
/// removed stops/trips go, trips left with fewer than two stop_times go,
/// and frequencies of removed trips go. Iterates to a fixpoint.
fn cascade(result: &mut ScanResult, fixes: &mut Vec<Fix>) {
    for _ in 0..4 {
        let stops: HashSet<String> = collect_ids(result, "stops.txt", "stop_id");
        let trips: HashSet<String> = collect_ids(result, "trips.txt", "trip_id");
        let routes: HashSet<String> = collect_ids(result, "routes.txt", "route_id");

        let mut changed = false;
        changed |= retain_rows(result, fixes, "stop_times.txt", |table, row| {
            let stop_ok = column(table, "stop_id")
                .map(|i| {
                    let id = row.fields[i].as_str();
                    id.is_empty() || stops.contains(id)
                })
                .unwrap_or(true);
            let trip_ok = column(table, "trip_id")
                .map(|i| trips.contains(row.fields[i].as_str()))
                .unwrap_or(false);
            stop_ok && trip_ok
        });

        // Trips need a route and at least two remaining stop times.
        let mut usage: BTreeMap<String, u64> = BTreeMap::new();
        if let Some(stop_times) = result.tables.get("stop_times.txt") {
            if let Some(i) = column(stop_times, "trip_id") {
                for row in &stop_times.rows {
                    *usage.entry(row.fields[i].clone()).or_insert(0) += 1;
                }
            }
        }
        changed |= retain_rows(result, fixes, "trips.txt", |table, row| {
            let trip_id = column(table, "trip_id")
                .map(|i| row.fields[i].as_str())
                .unwrap_or("");
            let route_ok = column(table, "route_id")
                .map(|i| routes.contains(row.fields[i].as_str()))
                .unwrap_or(false);
            route_ok && usage.get(trip_id).copied().unwrap_or(0) >= 2
        });

        let trips_after: HashSet<String> = collect_ids(result, "trips.txt", "trip_id");
        changed |= retain_rows(result, fixes, "frequencies.txt", |table, row| {
            column(table, "trip_id")
                .map(|i| trips_after.contains(row.fields[i].as_str()))
                .unwrap_or(false)
        });
        // Transfers and pathways whose load-bearing stop references were
        // removed go with them.
        for file in ["transfers.txt", "pathways.txt"] {
            changed |= retain_rows(result, fixes, file, |table, row| {
                ["from_stop_id", "to_stop_id"].iter().all(|name| {
                    column(table, name)
                        .map(|i| {
                            let id = row.fields[i].as_str();
                            id.is_empty() || stops.contains(id)
                        })
                        .unwrap_or(true)
                })
            });
        }

        if !changed {
            return;
        }
    }
}

fn collect_ids(result: &ScanResult, file: &str, field: &str) -> HashSet<String> {
    result
        .tables
        .get(file)
        .and_then(|table| {
            column(table, field).map(|i| {
                table
                    .rows
                    .iter()
                    .map(|row| row.fields[i].clone())
                    .filter(|id| !id.is_empty())
                    .collect()
            })
        })
        .unwrap_or_default()
}

fn retain_rows(
    result: &mut ScanResult,
    fixes: &mut Vec<Fix>,
    file: &str,
    keep: impl Fn(&Table, &crate::scan::Row) -> bool,
) -> bool {
    let Some(table) = result.tables.get_mut(file) else {
        return false;
    };
    let headers = table.headers.clone();
    let probe = Table {
        headers,
        rows: Vec::new(),
    };
    let mut kept = Vec::with_capacity(table.rows.len());
    let mut changed = false;
    for row in table.rows.drain(..) {
        if keep(&probe, &row) {
            kept.push(row);
        } else {
            changed = true;
            fixes.push(Fix {
                action: "drop_entity",
                filename: file.to_string(),
                csv_row: row.csv_row,
                field: None,
                old_value: None,
                new_value: None,
                triggered_by: "cascade".to_string(),
            });
        }
    }
    table.rows = kept;
    changed
}

/// Write the repaired tables to a fresh zip with RFC 4180 quoting.
pub(crate) fn write_zip(tables: &BTreeMap<String, Table>, output: &Path) -> Result<(), String> {
    let file = std::fs::File::create(output)
        .map_err(|e| format!("cannot create {}: {e}", output.display()))?;
    let mut writer = zip::ZipWriter::new(file);
    let zip_options = zip::write::SimpleFileOptions::default();
    for (name, table) in tables {
        writer
            .start_file(name, zip_options)
            .map_err(|e| format!("cannot write {name}: {e}"))?;
        let mut csv_writer = csv::Writer::from_writer(Vec::new());
        csv_writer
            .write_record(&table.headers)
            .map_err(|e| format!("cannot write {name} header: {e}"))?;
        for row in &table.rows {
            csv_writer
                .write_record(&row.fields)
                .map_err(|e| format!("cannot write {name} row: {e}"))?;
        }
        let bytes = csv_writer
            .into_inner()
            .map_err(|e| format!("cannot finish {name}: {e}"))?;
        writer
            .write_all(&bytes)
            .map_err(|e| format!("cannot write {name}: {e}"))?;
    }
    writer
        .finish()
        .map_err(|e| format!("cannot finish archive: {e}"))?;
    Ok(())
}
