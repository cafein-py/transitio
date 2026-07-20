//! Spatial and temporal feed cropping: retain the service relevant to an
//! area and date window and cascade everything else away, keeping the
//! result referentially consistent. Times and attributes of retained
//! trips are never altered.

use std::collections::{BTreeMap, HashSet};
use std::path::Path;

use crate::repair::write_zip;
use crate::scan::{ScanOptions, ScanResult, Table};
use crate::{rules, scan, semantics};

pub struct CropOptions {
    /// (minx, miny, maxx, maxy) in WGS84; None disables the spatial crop.
    pub bbox: Option<(f64, f64, f64, f64)>,
    /// YYYYMMDD inclusive window; None disables the temporal crop.
    pub start_date: Option<String>,
    pub end_date: Option<String>,
    /// Retain only trips whose every stop lies inside the box (stricter);
    /// the default keeps any trip serving at least one inside stop, with
    /// its full stop sequence.
    pub full_trips_only: bool,
}

pub struct CropResult {
    pub row_counts: BTreeMap<String, usize>,
    pub validation: ScanResult,
}

pub fn crop(
    path: &Path,
    output: &Path,
    options: ScanOptions,
    crop_options: &CropOptions,
) -> Result<CropResult, String> {
    let mut result = scan::scan_with(path, options)?;
    rules::run_rules(&mut result, &options);
    semantics::run_semantics(&mut result, &options);
    if !result.incomplete.is_empty()
        || result
            .notices
            .iter()
            .any(|n| matches!(n.code, "too_many_rows" | "notice_limit_reached"))
    {
        return Err(
            "feed exceeds the scan or notice budgets; raise the limits to crop it".to_string(),
        );
    }
    if output
        .symlink_metadata()
        .map(|m| m.is_symlink())
        .unwrap_or(false)
    {
        return Err("output path is a symlink; refusing to follow it".to_string());
    }
    if let (Ok(a), Ok(b)) = (path.canonicalize(), output.canonicalize()) {
        if a == b {
            return Err("output path aliases the source archive".to_string());
        }
    }

    let kept_trips = select_trips(&result, options, crop_options)?;
    retain(
        &mut result,
        &kept_trips,
        (&crop_options.start_date, &crop_options.end_date),
    );

    let staging = output.with_extension("zip.part");
    if staging
        .symlink_metadata()
        .map(|m| m.is_symlink())
        .unwrap_or(false)
    {
        return Err("staging path is a symlink; refusing to follow it".to_string());
    }
    if let (Ok(a), Ok(b)) = (path.canonicalize(), staging.canonicalize()) {
        if a == b {
            return Err("staging path aliases the source archive".to_string());
        }
    }
    let _ = std::fs::remove_file(&staging);
    write_zip(
        &result.tables,
        Some((path, &result.unparsed_entries)),
        &staging,
    )?;
    let validation = match scan::scan_with(&staging, options) {
        Ok(mut validation) => {
            rules::run_rules(&mut validation, &options);
            semantics::run_semantics(&mut validation, &options);
            validation
        }
        Err(error) => {
            let _ = std::fs::remove_file(&staging);
            return Err(error);
        }
    };
    std::fs::rename(&staging, output)
        .map_err(|e| format!("cannot move cropped feed into place: {e}"))?;
    let row_counts = result
        .tables
        .iter()
        .map(|(name, table)| (name.clone(), table.rows.len()))
        .collect();
    Ok(CropResult {
        row_counts,
        validation,
    })
}

fn column(table: &Table, name: &str) -> Option<usize> {
    table.headers.iter().position(|h| h == name)
}

fn ids<'t>(table: &'t Table, field: &str) -> Option<(usize, &'t Table)> {
    column(table, field).map(|i| (i, table))
}

/// Decide which trips survive both crops.
fn select_trips(
    result: &ScanResult,
    scan_options_ref: ScanOptions,
    crop_options: &CropOptions,
) -> Result<HashSet<String>, String> {
    let trips_table = result
        .tables
        .get("trips.txt")
        .ok_or("feed has no usable trips.txt")?;
    let trip_index = column(trips_table, "trip_id").ok_or("trips.txt has no trip_id column")?;
    let service_index = column(trips_table, "service_id");

    // Spatial selection over stop coordinates.
    let inside_stops: Option<HashSet<String>> =
        crop_options.bbox.map(|(minx, miny, maxx, maxy)| {
            result
                .tables
                .get("stops.txt")
                .and_then(|stops| {
                    let id = column(stops, "stop_id")?;
                    let lat = column(stops, "stop_lat")?;
                    let lon = column(stops, "stop_lon")?;
                    Some(
                        stops
                            .rows
                            .iter()
                            .filter_map(|row| {
                                let latitude: f64 = row.fields[lat].trim().parse().ok()?;
                                let longitude: f64 = row.fields[lon].trim().parse().ok()?;
                                (latitude >= miny
                                    && latitude <= maxy
                                    && longitude >= minx
                                    && longitude <= maxx)
                                    .then(|| row.fields[id].clone())
                            })
                            .collect(),
                    )
                })
                .unwrap_or_default()
        });

    // Temporal selection over actual service activity: weekday flags and
    // calendar_dates exceptions included, via the semantic tier's
    // active-date computation.
    let active_services: Option<HashSet<String>> =
        match (&crop_options.start_date, &crop_options.end_date) {
            (None, None) => None,
            (start, end) => {
                let parse = |value: &Option<String>, fallback: &str| {
                    chrono::NaiveDate::parse_from_str(
                        value.as_deref().unwrap_or(fallback),
                        "%Y%m%d",
                    )
                    .map_err(|_| "invalid crop date; expected YYYYMMDD".to_string())
                };
                let window_start = parse(start, "00010101")?;
                let window_end = parse(end, "99991231")?;
                let dates = semantics::active_service_dates(&result.tables, &scan_options_ref);
                Some(
                    dates
                        .into_iter()
                        .filter(|(_, days)| {
                            days.iter().any(|d| *d >= window_start && *d <= window_end)
                        })
                        .map(|(id, _)| id)
                        .collect(),
                )
            }
        };

    // Trip stop membership from stop_times.
    let mut trip_stops: BTreeMap<String, Vec<String>> = BTreeMap::new();
    if let Some((trip_i, table)) = result
        .tables
        .get("stop_times.txt")
        .and_then(|t| ids(t, "trip_id"))
    {
        if let Some(stop_i) = column(table, "stop_id") {
            for row in &table.rows {
                trip_stops
                    .entry(row.fields[trip_i].clone())
                    .or_default()
                    .push(row.fields[stop_i].clone());
            }
        }
    }

    let mut kept = HashSet::new();
    for row in &trips_table.rows {
        let trip_id = &row.fields[trip_index];
        if let Some(active) = &active_services {
            let service = service_index.map(|i| row.fields[i].as_str()).unwrap_or("");
            if !active.contains(service) {
                continue;
            }
        }
        if let Some(inside) = &inside_stops {
            let stops = trip_stops.get(trip_id).map(|v| v.as_slice()).unwrap_or(&[]);
            let keep = if crop_options.full_trips_only {
                !stops.is_empty() && stops.iter().all(|s| inside.contains(s))
            } else {
                stops.iter().any(|s| inside.contains(s))
            };
            if !keep {
                continue;
            }
        }
        kept.insert(trip_id.clone());
    }
    Ok(kept)
}

/// Retain only the kept trips and everything they reference, then the
/// supporting entities between retained stops.
fn retain(
    result: &mut ScanResult,
    kept_trips: &HashSet<String>,
    window: (&Option<String>, &Option<String>),
) {
    keep_rows(result, "trips.txt", "trip_id", kept_trips);
    keep_rows(result, "stop_times.txt", "trip_id", kept_trips);
    keep_rows(result, "frequencies.txt", "trip_id", kept_trips);

    // Stops actually served (their full sequences), plus their parents.
    let mut kept_stops = referenced(result, "stop_times.txt", "stop_id");
    if let Some(stops) = result.tables.get("stops.txt") {
        if let (Some(id), Some(parent)) =
            (column(stops, "stop_id"), column(stops, "parent_station"))
        {
            let parents: HashSet<String> = stops
                .rows
                .iter()
                .filter(|row| kept_stops.contains(&row.fields[id]))
                .map(|row| row.fields[parent].clone())
                .filter(|p| !p.is_empty())
                .collect();
            kept_stops.extend(parents);
        }
    }
    keep_rows(result, "stops.txt", "stop_id", &kept_stops);

    let kept_routes = referenced(result, "trips.txt", "route_id");
    keep_rows(result, "routes.txt", "route_id", &kept_routes);
    let kept_shapes = referenced(result, "trips.txt", "shape_id");
    keep_rows(result, "shapes.txt", "shape_id", &kept_shapes);
    let kept_services = referenced(result, "trips.txt", "service_id");
    keep_rows(result, "calendar.txt", "service_id", &kept_services);
    keep_rows(result, "calendar_dates.txt", "service_id", &kept_services);
    // Retained calendars must not advertise service outside the window,
    // including a one-sided window (the open side keeps its bound).
    if window.0.is_some() || window.1.is_some() {
        let start = window.0.clone().unwrap_or_else(|| "00010101".to_string());
        let end = window.1.clone().unwrap_or_else(|| "99991231".to_string());
        if let Some(calendar) = result.tables.get_mut("calendar.txt") {
            let s = column(calendar, "start_date");
            let e = column(calendar, "end_date");
            for row in &mut calendar.rows {
                if let Some(i) = s {
                    if row.fields[i].as_str() < start.as_str() {
                        row.fields[i] = start.clone();
                    }
                }
                if let Some(i) = e {
                    if row.fields[i].as_str() > end.as_str() {
                        row.fields[i] = end.clone();
                    }
                }
            }
            // A calendar wholly outside a one-sided window clamps to an
            // empty interval; the service survives only through its
            // calendar_dates additions, so the row itself must go.
            if let (Some(i), Some(j)) = (s, e) {
                calendar.rows.retain(|row| row.fields[i] <= row.fields[j]);
            }
        }
        if let Some(dates) = result.tables.get_mut("calendar_dates.txt") {
            if let Some(i) = column(dates, "date") {
                dates.rows.retain(|row| {
                    let date = row.fields[i].trim();
                    date >= start.as_str() && date <= end.as_str()
                });
            }
        }
    }

    // Supporting entities between retained stops only.
    for file in ["transfers.txt", "pathways.txt"] {
        let Some(table) = result.tables.get_mut(file) else {
            continue;
        };
        let from = column(table, "from_stop_id");
        let to = column(table, "to_stop_id");
        table.rows.retain(|row| {
            [from, to].iter().all(|index| {
                index
                    .map(|i| {
                        let id = row.fields[i].as_str();
                        id.is_empty() || kept_stops.contains(id)
                    })
                    .unwrap_or(true)
            })
        });
    }
    for (field, parents) in [
        ("from_route_id", &kept_routes),
        ("to_route_id", &kept_routes),
        ("from_trip_id", kept_trips),
        ("to_trip_id", kept_trips),
    ] {
        let Some(table) = result.tables.get_mut("transfers.txt") else {
            break;
        };
        let Some(i) = column(table, field) else {
            continue;
        };
        table.rows.retain(|row| {
            let id = row.fields[i].as_str();
            id.is_empty() || parents.contains(id)
        });
    }
    if let Some(attributions) = result.tables.get_mut("attributions.txt") {
        let route = column(attributions, "route_id");
        let trip = column(attributions, "trip_id");
        attributions.rows.retain(|row| {
            let route_ok = route
                .map(|i| {
                    let id = row.fields[i].as_str();
                    id.is_empty() || kept_routes.contains(id)
                })
                .unwrap_or(true);
            let trip_ok = trip
                .map(|i| {
                    let id = row.fields[i].as_str();
                    id.is_empty() || kept_trips.contains(id)
                })
                .unwrap_or(true);
            route_ok && trip_ok
        });
    }
    if let Some(networks) = result.tables.get_mut("route_networks.txt") {
        if let Some(i) = column(networks, "route_id") {
            networks
                .rows
                .retain(|row| kept_routes.contains(&row.fields[i]));
        }
    }
    let kept_agencies = referenced(result, "routes.txt", "agency_id");
    if let Some(agency) = result.tables.get_mut("agency.txt") {
        if let Some(id) = column(agency, "agency_id") {
            if !kept_agencies.is_empty() {
                agency
                    .rows
                    .retain(|row| kept_agencies.contains(&row.fields[id]));
            }
        }
    }
    // Attributions pointing only at a pruned agency must go with it.
    if !kept_agencies.is_empty() {
        if let Some(attributions) = result.tables.get_mut("attributions.txt") {
            if let Some(i) = column(attributions, "agency_id") {
                attributions.rows.retain(|row| {
                    let id = row.fields[i].as_str();
                    id.is_empty() || kept_agencies.contains(id)
                });
            }
        }
    }
    if let Some(fare_rules) = result.tables.get_mut("fare_rules.txt") {
        if let Some(route) = column(fare_rules, "route_id") {
            fare_rules.rows.retain(|row| {
                let id = row.fields[route].as_str();
                id.is_empty() || kept_routes.contains(id)
            });
        }
    }
    // Fares referenced by surviving rules; feeds without fare_rules keep
    // their fare_attributes untouched.
    if result.tables.contains_key("fare_rules.txt") {
        let kept_fares = referenced(result, "fare_rules.txt", "fare_id");
        keep_rows(result, "fare_attributes.txt", "fare_id", &kept_fares);
    }
}

fn referenced(result: &ScanResult, file: &str, field: &str) -> HashSet<String> {
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

fn keep_rows(result: &mut ScanResult, file: &str, field: &str, kept: &HashSet<String>) {
    let Some(table) = result.tables.get_mut(file) else {
        return;
    };
    let Some(index) = column(table, field) else {
        return;
    };
    table.rows.retain(|row| kept.contains(&row.fields[index]));
}
