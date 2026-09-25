//! Spatial and temporal feed cropping: retain the service relevant to an
//! area and date window and cascade everything else away, keeping the
//! result referentially consistent. Times and attributes of retained
//! trips are never altered.

use std::collections::{BTreeMap, HashSet};
use std::path::Path;

use crate::output::ZipOutput;
use crate::scan::{DelimiterGuard, NoTable, Row, ScanOptions, ScanResult, Table, TableReader};
use crate::{rules, scan, schema, semantics};

/// Tables read from the archive row by row rather than parsed whole: the
/// ones a national feed makes far larger than memory. Everything kept
/// from them is bounded by the crop, not by the feed.
const STREAMED: &[&str] = &["stop_times.txt", "trips.txt", "shapes.txt"];

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
    // One open file for the scan, every streaming pass and the copied
    // entries, so a source replaced under the crop cannot mix versions.
    let source =
        std::fs::File::open(path).map_err(|e| format!("cannot open {}: {e}", path.display()))?;
    let mut result = scan::scan_reader_streaming(reopen(&source)?, options, STREAMED)?;
    // The tables parsed whole must be whole: a truncated stops.txt would
    // crop silently wrong. The cropped feed is validated below.
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
    let inside = inside_stops(&result, crop_options, area.as_deref());
    let active = active_services(&result, &options, crop_options)?;
    let touched = match &inside {
        Some(inside) => Some(trips_touching(
            &source,
            &options,
            inside,
            crop_options.full_trips_only,
        )?),
        None => None,
    };
    let (kept_trips, trips) = select_trips(
        &source,
        &options,
        crop_options,
        touched.as_ref(),
        active.as_ref(),
    )?;
    result.tables.insert("trips.txt".to_string(), trips);

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
    let streamed_counts = match write_cropped(
        &source,
        &staging,
        &options,
        &mut result,
        &kept_trips,
        crop_options,
    ) {
        Ok(counts) => counts,
        Err(error) => {
            let _ = std::fs::remove_file(&staging);
            return Err(error);
        }
    };
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
    // A cropped feed the budgets cannot validate whole is not published:
    // its report would describe only part of it.
    if !validation.incomplete.is_empty()
        || validation
            .notices
            .iter()
            .any(|n| matches!(n.code, "too_many_rows" | "notice_limit_reached"))
    {
        let _ = std::fs::remove_file(&staging);
        return Err(
            "cropped feed exceeds the scan or notice budgets; raise the limits to crop it"
                .to_string(),
        );
    }
    std::fs::rename(&staging, output)
        .map_err(|e| format!("cannot move cropped feed into place: {e}"))?;
    let mut row_counts: BTreeMap<String, usize> = result
        .tables
        .iter()
        .map(|(name, table)| (name.clone(), table.rows.len()))
        .collect();
    row_counts.extend(streamed_counts);
    Ok(CropResult {
        row_counts,
        validation,
        source_routes,
    })
}

fn column(table: &Table, name: &str) -> Option<usize> {
    position(&table.headers, name)
}

fn position(headers: &[String], name: &str) -> Option<usize> {
    headers.iter().position(|h| h == name)
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

/// The named table streamed from the archive under the stream guard, or
/// None when the archive has no such entry or it is empty.
fn stream_table<'a>(
    archive: &'a mut zip::ZipArchive<std::fs::File>,
    name: &'static str,
    options: &ScanOptions,
) -> Result<Option<TableReader<DelimiterGuard<zip::read::ZipFile<'a, std::fs::File>>>>, String> {
    let entry = match archive.by_name(name) {
        Ok(entry) => entry,
        Err(zip::result::ZipError::FileNotFound) => return Ok(None),
        Err(error) => return Err(format!("cannot read {name}: {error}")),
    };
    let spec = schema::spec_for(name).expect("a streamed table is a known GTFS file");
    let mut notices = Vec::new();
    let guarded = DelimiterGuard::new(entry, options);
    match TableReader::open(spec, guarded, options, u64::MAX, &mut notices) {
        Ok(reader) => Ok(Some(reader)),
        Err(NoTable::Empty) => Ok(None),
        Err(NoTable::Unreadable) => Err(unreadable(name, &notices)),
    }
}

/// Another handle on the one open source file, for the next pass over it.
fn reopen(source: &std::fs::File) -> Result<std::fs::File, String> {
    source
        .try_clone()
        .map_err(|e| format!("cannot reopen the source archive: {e}"))
}

fn open_archive(source: &std::fs::File) -> Result<zip::ZipArchive<std::fs::File>, String> {
    zip::ZipArchive::new(reopen(source)?)
        .map_err(|e| format!("cannot reread the source archive: {e}"))
}

/// A stream that stopped short of its table cannot be cropped from.
fn whole<R: std::io::Read>(
    reader: &TableReader<R>,
    name: &str,
    notices: &[crate::notice::Notice],
) -> Result<(), String> {
    if reader.truncated() {
        return Err(unreadable(name, notices));
    }
    Ok(())
}

fn unreadable(name: &str, notices: &[crate::notice::Notice]) -> String {
    let message = notices
        .iter()
        .rev()
        .find_map(|n| n.context.get("message").and_then(|v| v.as_str()))
        .unwrap_or("unreadable");
    format!("{name} cannot be streamed: {message}")
}

/// The stops inside the crop area, or None without a spatial crop.
fn inside_stops(
    result: &ScanResult,
    crop_options: &CropOptions,
    polygon: Option<&[PolygonRings]>,
) -> Option<HashSet<String>> {
    // Spatial selection over stop coordinates: a box, or the polygon
    // parts (pre-filtered by their own bounds) when one was given.
    let area: Option<CropArea> = match (polygon, crop_options.bbox) {
        (Some(parts), _) => Some((polygon_bounds(parts), Some(parts))),
        (None, Some(bbox)) => Some((bbox, None)),
        (None, None) => None,
    };
    area.map(|((minx, miny, maxx, maxy), parts)| {
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
    })
}

/// The services active inside the date window, or None without one.
fn active_services(
    result: &ScanResult,
    options: &ScanOptions,
    crop_options: &CropOptions,
) -> Result<Option<HashSet<String>>, String> {
    // Temporal selection over actual service activity: weekday flags and
    // calendar_dates exceptions included, via the semantic tier's
    // active-date computation.
    match (&crop_options.start_date, &crop_options.end_date) {
        (None, None) => Ok(None),
        (start, end) => {
            let parse = |value: &Option<String>, fallback: &str| {
                chrono::NaiveDate::parse_from_str(value.as_deref().unwrap_or(fallback), "%Y%m%d")
                    .map_err(|_| "invalid crop date; expected YYYYMMDD".to_string())
            };
            let window_start = parse(start, "00010101")?;
            let window_end = parse(end, "99991231")?;
            let dates = semantics::active_service_dates(&result.tables, options);
            Ok(Some(
                dates
                    .into_iter()
                    .filter(|(_, days)| days.iter().any(|d| *d >= window_start && *d <= window_end))
                    .map(|(id, _)| id)
                    .collect(),
            ))
        }
    }
}

/// Pass 1 over stop_times.txt: the trips serving an inside stop, less,
/// with full trips only, those also serving an outside one. Both sets
/// are bounded by the area, not the feed.
fn trips_touching(
    source: &std::fs::File,
    options: &ScanOptions,
    inside: &HashSet<String>,
    full_trips_only: bool,
) -> Result<HashSet<String>, String> {
    let mut archive = open_archive(source)?;
    let mut touched = HashSet::new();
    let Some(mut reader) = stream_table(&mut archive, "stop_times.txt", options)? else {
        return Ok(touched);
    };
    let (Some(trip), Some(stop)) = (
        position(reader.headers(), "trip_id"),
        position(reader.headers(), "stop_id"),
    ) else {
        return Ok(touched);
    };
    let mut notices = Vec::new();
    while let Some(row) = reader.next_row(&mut notices) {
        if inside.contains(&row.fields[stop]) {
            touched.insert(row.fields[trip].clone());
        }
    }
    whole(&reader, "stop_times.txt", &notices)?;
    if full_trips_only && !touched.is_empty() {
        // A trip's outside stop may come before its inside one, so the
        // touched set is complete first and pruned in a second pass.
        drop(reader);
        let mut archive = open_archive(source)?;
        let Some(mut reader) = stream_table(&mut archive, "stop_times.txt", options)? else {
            return Ok(touched);
        };
        let mut notices = Vec::new();
        let mut partly_outside = HashSet::new();
        while let Some(row) = reader.next_row(&mut notices) {
            if !inside.contains(&row.fields[stop]) && touched.contains(&row.fields[trip]) {
                partly_outside.insert(row.fields[trip].clone());
            }
        }
        whole(&reader, "stop_times.txt", &notices)?;
        touched.retain(|trip| !partly_outside.contains(trip));
    }
    Ok(touched)
}

/// Decide which trips survive every crop, streaming trips.txt and keeping
/// only the survivors as the trips table.
fn select_trips(
    source: &std::fs::File,
    options: &ScanOptions,
    crop_options: &CropOptions,
    touched: Option<&HashSet<String>>,
    active: Option<&HashSet<String>>,
) -> Result<(HashSet<String>, Table), String> {
    let mut archive = open_archive(source)?;
    let Some(mut reader) = stream_table(&mut archive, "trips.txt", options)? else {
        return Err("feed has no usable trips.txt".to_string());
    };
    let headers = reader.headers().to_vec();
    let trip_index = position(&headers, "trip_id").ok_or("trips.txt has no trip_id column")?;
    let service_index = position(&headers, "service_id");
    let route_index = position(&headers, "route_id");
    let mut kept = HashSet::new();
    let mut rows = Vec::new();
    let mut notices = Vec::new();
    while let Some(row) = reader.next_row(&mut notices) {
        if let Some(routes) = &crop_options.routes {
            let route = route_index.map(|i| row.fields[i].as_str()).unwrap_or("");
            if !routes.contains(route) {
                continue;
            }
        }
        if let Some(active) = active {
            let service = service_index.map(|i| row.fields[i].as_str()).unwrap_or("");
            if !active.contains(service) {
                continue;
            }
        }
        if let Some(touched) = touched {
            if !touched.contains(&row.fields[trip_index]) {
                continue;
            }
        }
        if !kept.insert(row.fields[trip_index].clone()) {
            // Ambiguous, and with no row cap on trips.txt a way to grow the
            // kept table without bound.
            return Err(format!(
                "trips.txt repeats trip_id {:?}; an ambiguous feed cannot be cropped",
                row.fields[trip_index]
            ));
        }
        rows.push(Row {
            csv_row: row.csv_row,
            fields: row.fields,
        });
    }
    whole(&reader, "trips.txt", &notices)?;
    Ok((kept, Table { headers, rows }))
}

/// Write the cropped feed: the kept trips' stop_times straight from the
/// source (pass 2), the parsed tables after the cascade, the kept trips'
/// shapes straight from the source, and the entries copied through.
/// Returns the row counts of the streamed tables.
fn write_cropped(
    source: &std::fs::File,
    staging: &Path,
    options: &ScanOptions,
    result: &mut ScanResult,
    kept_trips: &HashSet<String>,
    crop_options: &CropOptions,
) -> Result<BTreeMap<String, usize>, String> {
    let mut zip = ZipOutput::create(staging)?;
    let mut counts = BTreeMap::new();
    let mut kept_stops: HashSet<String> = HashSet::new();
    {
        let mut archive = open_archive(source)?;
        let opened = stream_table(&mut archive, "stop_times.txt", options)?;
        if let Some(mut reader) = opened {
            let headers = reader.headers().to_vec();
            let trip = position(&headers, "trip_id");
            let stop = position(&headers, "stop_id");
            let mut notices = Vec::new();
            let rows = std::iter::from_fn(|| reader.next_row(&mut notices))
                .filter(|row| trip.is_some_and(|i| kept_trips.contains(&row.fields[i])))
                .inspect(|row| {
                    if let Some(i) = stop {
                        kept_stops.insert(row.fields[i].clone());
                    }
                })
                .map(|row| row.fields);
            let count = zip.rows("stop_times.txt", &headers, rows)?;
            whole(&reader, "stop_times.txt", &notices)?;
            counts.insert("stop_times.txt".to_string(), count);
        }
    }
    retain(
        result,
        kept_trips,
        (&crop_options.start_date, &crop_options.end_date),
        kept_stops,
    );
    for (name, table) in &result.tables {
        zip.table(name, table)?;
    }
    let kept_shapes = referenced(result, "trips.txt", "shape_id");
    {
        let mut archive = open_archive(source)?;
        let opened = stream_table(&mut archive, "shapes.txt", options)?;
        if let Some(mut reader) = opened {
            let headers = reader.headers().to_vec();
            let shape = position(&headers, "shape_id");
            let mut notices = Vec::new();
            let rows = std::iter::from_fn(|| reader.next_row(&mut notices))
                .filter(|row| shape.is_some_and(|i| kept_shapes.contains(&row.fields[i])))
                .map(|row| row.fields);
            let count = zip.rows("shapes.txt", &headers, rows)?;
            whole(&reader, "shapes.txt", &notices)?;
            counts.insert("shapes.txt".to_string(), count);
        }
    }
    zip.passthrough(&mut open_archive(source)?, &result.unparsed_entries)?;
    zip.finish()?;
    Ok(counts)
}

/// Retain only the kept trips and everything they reference, then the
/// supporting entities between retained stops.
fn retain(
    result: &mut ScanResult,
    kept_trips: &HashSet<String>,
    window: (&Option<String>, &Option<String>),
    kept_stops: HashSet<String>,
) {
    keep_rows(result, "trips.txt", "trip_id", kept_trips);
    keep_rows(result, "stop_times.txt", "trip_id", kept_trips);
    keep_rows(result, "frequencies.txt", "trip_id", kept_trips);

    // Stops actually served (their full sequences), plus their parents.
    let mut kept_stops = kept_stops;
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
