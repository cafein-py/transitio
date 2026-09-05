//! Spatial and temporal feed cropping: retain the service relevant to an
//! area and date window and cascade everything else away, keeping the
//! result referentially consistent. Times and attributes of retained
//! trips are never altered.

use std::collections::{BTreeMap, HashSet};
use std::path::Path;

use crate::repair::write_zip;
use crate::scan::{ScanOptions, ScanResult, Table};
use crate::{rules, scan, semantics};

/// One polygon: its outer ring first, then any holes, as WGS84
/// (longitude, latitude) pairs.
pub type PolygonRings = Vec<Vec<(f64, f64)>>;

/// The spatial crop area: a bounding box, plus the polygon parts to test
/// within it when the crop is polygon-true.
type CropArea<'a> = ((f64, f64, f64, f64), Option<&'a [PolygonRings]>);

pub struct CropOptions {
    /// (minx, miny, maxx, maxy) in WGS84; None disables the spatial crop.
    pub bbox: Option<(f64, f64, f64, f64)>,
    /// Polygon parts to crop to instead of a box; None disables it. A
    /// stop inside any part is inside the area.
    pub polygon: Option<Vec<PolygonRings>>,
    /// YYYYMMDD inclusive window; None disables the temporal crop.
    pub start_date: Option<String>,
    pub end_date: Option<String>,
    /// Retain only trips whose every stop lies inside the crop area
    /// (stricter); the default keeps any trip serving at least one inside
    /// stop, with its full stop sequence.
    pub full_trips_only: bool,
    /// Retain only trips whose ``route_id`` is in this set; None keeps every
    /// route. Applied alongside the spatial and temporal crops (all AND).
    pub routes: Option<HashSet<String>>,
}

pub struct CropResult {
    pub row_counts: BTreeMap<String, usize>,
    pub validation: ScanResult,
    /// The distinct ``route_id`` values in routes.txt before retention, from
    /// the same scan the crop runs on — so a caller auditing what a route
    /// filter dropped shares one snapshot with the crop rather than re-reading.
    /// ``None`` when routes.txt or its ``route_id`` column is absent (the drop
    /// is then undetermined), never an empty vector standing in for it.
    pub source_routes: Option<Vec<String>>,
}

pub fn crop(
    path: &Path,
    output: &Path,
    options: ScanOptions,
    crop_options: &CropOptions,
) -> Result<CropResult, String> {
    // Check the area before reading anything: an invalid one would
    // otherwise crop silently wrong.
    if crop_options.bbox.is_some() && crop_options.polygon.is_some() {
        return Err("pass either bbox or polygon, not both".to_string());
    }
    let area = match &crop_options.polygon {
        Some(parts) => {
            validate_polygon(parts)?;
            Some(closed_parts(parts))
        }
        None => None,
    };
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

    let source_routes: Option<Vec<String>> = result.tables.get("routes.txt").and_then(|table| {
        column(table, "route_id").map(|i| {
            table
                .rows
                .iter()
                .map(|row| row.fields[i].clone())
                .collect::<std::collections::BTreeSet<String>>()
                .into_iter()
                .collect()
        })
    });
    let kept_trips = select_trips(&result, options, crop_options, area.as_deref())?;
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
        source_routes,
    })
}

fn column(table: &Table, name: &str) -> Option<usize> {
    table.headers.iter().position(|h| h == name)
}

/// Whether a point lies on a ring's boundary (within a rounding
/// tolerance), which counts as inside for every ring, hole or not.
fn on_boundary(x: f64, y: f64, ring: &[(f64, f64)]) -> bool {
    // Degrees: about a tenth of a millimetre, and compared against the
    // point's distance from the edge rather than the raw cross product,
    // which grows with edge length.
    const EPSILON: f64 = 1e-9;
    ring.windows(2).any(|edge| {
        let ((x1, y1), (x2, y2)) = (edge[0], edge[1]);
        let (dx, dy) = (x2 - x1, y2 - y1);
        let length = dx.hypot(dy);
        let cross = (x - x1) * dy - (y - y1) * dx;
        let distance = if length > 0.0 {
            cross.abs() / length
        } else {
            (x - x1).hypot(y - y1) // a zero-length edge is a point
        };
        if distance > EPSILON {
            return false;
        }
        // collinear: inside the segment's own extent
        x >= x1.min(x2) - EPSILON
            && x <= x1.max(x2) + EPSILON
            && y >= y1.min(y2) - EPSILON
            && y <= y1.max(y2) + EPSILON
    })
}

/// Even-odd ray cast, excluding the boundary (callers test that first).
fn strictly_inside_ring(x: f64, y: f64, ring: &[(f64, f64)]) -> bool {
    let mut inside = false;
    for edge in ring.windows(2) {
        let ((x1, y1), (x2, y2)) = (edge[0], edge[1]);
        if (y1 > y) != (y2 > y) {
            let t = (y - y1) / (y2 - y1);
            if x < x1 + t * (x2 - x1) {
                inside = !inside;
            }
        }
    }
    inside
}

/// Whether a point is inside one polygon part: inside its outer ring and
/// not strictly inside a hole. A point on any ring counts as inside.
fn inside_part(x: f64, y: f64, rings: &PolygonRings) -> bool {
    let mut parts = rings.iter();
    let Some(outer) = parts.next() else {
        return false;
    };
    if on_boundary(x, y, outer) {
        return true;
    }
    if !strictly_inside_ring(x, y, outer) {
        return false;
    }
    !parts.any(|hole| !on_boundary(x, y, hole) && strictly_inside_ring(x, y, hole))
}

/// Whether a point is inside any part of the cropping area.
fn inside_polygon(x: f64, y: f64, parts: &[PolygonRings]) -> bool {
    parts.iter().any(|rings| inside_part(x, y, rings))
}

/// The bounding box of every ring, as a cheap pre-filter.
fn polygon_bounds(parts: &[PolygonRings]) -> (f64, f64, f64, f64) {
    let (mut minx, mut miny) = (f64::INFINITY, f64::INFINITY);
    let (mut maxx, mut maxy) = (f64::NEG_INFINITY, f64::NEG_INFINITY);
    for rings in parts {
        for ring in rings {
            for &(x, y) in ring {
                minx = minx.min(x);
                miny = miny.min(y);
                maxx = maxx.max(x);
                maxy = maxy.max(y);
            }
        }
    }
    (minx, miny, maxx, maxy)
}

/// Every ring closed (its first point repeated at the end), so the
/// edge walk covers the closing segment. Callers that already close
/// their rings — GeoJSON requires it — are unaffected.
pub fn closed_parts(parts: &[PolygonRings]) -> Vec<PolygonRings> {
    parts
        .iter()
        .map(|rings| {
            rings
                .iter()
                .map(|ring| {
                    let mut ring = ring.clone();
                    match (ring.first().copied(), ring.last().copied()) {
                        (Some(first), Some(last)) if first != last => ring.push(first),
                        _ => {}
                    }
                    ring
                })
                .collect()
        })
        .collect()
}

/// Reject areas that cannot describe a region before any work starts.
pub fn validate_polygon(parts: &[PolygonRings]) -> Result<(), String> {
    if parts.is_empty() {
        return Err("crop polygon has no parts".to_string());
    }
    for rings in parts {
        let Some(outer) = rings.first() else {
            return Err("crop polygon part has no outer ring".to_string());
        };
        for ring in rings {
            for &(x, y) in ring {
                if !x.is_finite() || !y.is_finite() {
                    return Err("crop polygon has a non-finite coordinate".to_string());
                }
                if !(-180.0..=180.0).contains(&x) || !(-90.0..=90.0).contains(&y) {
                    return Err(format!("crop polygon coordinate out of range: ({x}, {y})"));
                }
            }
        }
        let mut distinct: Vec<(f64, f64)> = Vec::new();
        for &point in outer {
            if !distinct.contains(&point) {
                distinct.push(point);
            }
        }
        if distinct.len() < 3 {
            return Err("crop polygon needs at least three distinct points".to_string());
        }
    }
    Ok(())
}

fn ids<'t>(table: &'t Table, field: &str) -> Option<(usize, &'t Table)> {
    column(table, field).map(|i| (i, table))
}

/// Decide which trips survive both crops.
fn select_trips(
    result: &ScanResult,
    scan_options_ref: ScanOptions,
    crop_options: &CropOptions,
    polygon: Option<&[PolygonRings]>,
) -> Result<HashSet<String>, String> {
    let trips_table = result
        .tables
        .get("trips.txt")
        .ok_or("feed has no usable trips.txt")?;
    let trip_index = column(trips_table, "trip_id").ok_or("trips.txt has no trip_id column")?;
    let service_index = column(trips_table, "service_id");
    let route_index = column(trips_table, "route_id");

    // Spatial selection over stop coordinates: a box, or the polygon
    // parts (pre-filtered by their own bounds) when one was given.
    let area: Option<CropArea> = match (polygon, crop_options.bbox) {
        (Some(parts), _) => Some((polygon_bounds(parts), Some(parts))),
        (None, Some(bbox)) => Some((bbox, None)),
        (None, None) => None,
    };
    let inside_stops: Option<HashSet<String>> = area.map(|((minx, miny, maxx, maxy), parts)| {
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
                            let in_box = latitude >= miny
                                && latitude <= maxy
                                && longitude >= minx
                                && longitude <= maxx;
                            let inside = in_box
                                && parts
                                    .is_none_or(|parts| inside_polygon(longitude, latitude, parts));
                            inside.then(|| row.fields[id].clone())
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
        if let Some(routes) = &crop_options.routes {
            let route = route_index.map(|i| row.fields[i].as_str()).unwrap_or("");
            if !routes.contains(route) {
                continue;
            }
        }
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
    // stop associations follow their stops, or they dangle
    keep_rows(result, "stop_areas.txt", "stop_id", &kept_stops);
    keep_rows(result, "location_group_stops.txt", "stop_id", &kept_stops);

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

#[cfg(test)]
mod tests {
    use super::*;

    fn triangle() -> Vec<PolygonRings> {
        // the lower-right half of the unit square, left unclosed
        vec![vec![vec![(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]]]
    }

    #[test]
    fn unclosed_rings_are_closed_before_testing() {
        let open = triangle();
        // without the closing edge the diagonal is missing and a point
        // above it reads as inside
        assert!(inside_polygon(0.2, 0.8, &open));
        let closed = closed_parts(&open);
        assert!(!inside_polygon(0.2, 0.8, &closed));
        assert!(inside_polygon(0.6, 0.2, &closed));
    }

    #[test]
    fn long_edges_keep_their_boundary_points() {
        // a degree-wide sloped edge: the raw cross product for a point on
        // it is far larger than a coordinate-scale epsilon
        let wedge = closed_parts(&[vec![vec![(0.0, 0.0), (60.0, 30.0), (60.0, 0.0)]]]);
        assert!(inside_polygon(20.0, 10.0, &wedge)); // exactly on the slope
        assert!(!inside_polygon(20.0, 10.1, &wedge));
    }

    #[test]
    fn ring_boundaries_count_as_inside() {
        let closed = closed_parts(&triangle());
        assert!(inside_polygon(0.5, 0.5, &closed)); // on the diagonal
        assert!(inside_polygon(0.5, 0.0, &closed)); // on an axis edge
        assert!(inside_polygon(0.0, 0.0, &closed)); // on a vertex
    }

    #[test]
    fn holes_are_excluded_but_their_boundary_is_not() {
        let square = vec![vec![
            vec![(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (0.0, 0.0)],
            vec![(0.4, 0.4), (0.6, 0.4), (0.6, 0.6), (0.4, 0.6), (0.4, 0.4)],
        ]];
        assert!(!inside_polygon(0.5, 0.5, &square)); // inside the hole
        assert!(inside_polygon(0.4, 0.5, &square)); // on the hole's edge
        assert!(inside_polygon(0.1, 0.1, &square)); // outside the hole
    }

    #[test]
    fn later_parts_are_not_holes() {
        let two = vec![
            vec![vec![
                (0.0, 0.0),
                (1.0, 0.0),
                (1.0, 1.0),
                (0.0, 1.0),
                (0.0, 0.0),
            ]],
            vec![vec![
                (4.0, 4.0),
                (5.0, 4.0),
                (5.0, 5.0),
                (4.0, 5.0),
                (4.0, 4.0),
            ]],
        ];
        assert!(inside_polygon(0.5, 0.5, &two));
        assert!(inside_polygon(4.5, 4.5, &two));
        assert!(!inside_polygon(2.0, 2.0, &two));
    }

    #[test]
    fn degenerate_areas_are_refused() {
        assert!(validate_polygon(&[]).is_err());
        assert!(validate_polygon(&[vec![]]).is_err());
        // fewer than three distinct points
        assert!(validate_polygon(&[vec![vec![(0.0, 0.0), (1.0, 1.0), (0.0, 0.0)]]]).is_err());
        assert!(validate_polygon(&[vec![vec![(0.0, 0.0), (1.0, 0.0), (f64::NAN, 1.0)]]]).is_err());
        assert!(validate_polygon(&[vec![vec![(0.0, 0.0), (200.0, 0.0), (1.0, 1.0)]]]).is_err());
        assert!(validate_polygon(&triangle()).is_ok());
    }

    #[test]
    fn crop_refuses_both_predicates() {
        let options = CropOptions {
            bbox: Some((0.0, 0.0, 1.0, 1.0)),
            polygon: Some(triangle()),
            start_date: None,
            end_date: None,
            full_trips_only: false,
            routes: None,
        };
        let error = match crop(
            Path::new("does-not-matter.zip"),
            Path::new("out.zip"),
            ScanOptions::default(),
            &options,
        ) {
            Err(error) => error,
            Ok(_) => panic!("crop accepted both a bbox and a polygon"),
        };
        assert!(error.contains("not both"), "{error}");
    }
}
