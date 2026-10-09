//! Spatial and temporal feed cropping: retain the service relevant to an
//! area and date window and cascade everything else away, keeping the
//! result referentially consistent. Times and attributes of retained
//! trips are never altered beyond the surrounding whitespace the reader
//! trims and the bytes that are not UTF-8 it reads as U+FFFD.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::path::Path;

use serde::Serialize;

use crate::notice::Notice;
use crate::output::ZipOutput;
use crate::scan::{
    DelimiterGuard, NoTable, Row, ScanOptions, ScanResult, Table, TableReader, MAX_RECORD_BYTES,
};
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
    /// Leave out the trips whose ``trip_id`` is in this set; None leaves out
    /// none. Applied alongside the other crops.
    pub exclude_trips: Option<HashSet<String>>,
}

pub struct CropResult {
    pub row_counts: BTreeMap<String, usize>,
    pub validation: ScanResult,
    /// The distinct ``route_id`` values in routes.txt before retention, from
    /// the same scan the crop runs on — so a caller auditing what a route
    /// filter dropped shares one snapshot with the crop rather than re-reading.
    /// ``None`` when routes.txt is absent (the drop is then undetermined),
    /// never an empty vector standing in for it.
    pub source_routes: Option<Vec<String>>,
    /// The whitespace the reader trimmed from the source, one notice per
    /// file read, with rows numbered as in the source. shapes.txt is read
    /// only when a kept trip has a shape.
    pub source_notices: Vec<Notice>,
    /// The rows of kept trips left out because a reference names no row of
    /// its parent table, the trips this left with fewer than two
    /// stop_times, and the exact repeats of a kept trips.txt row, per file,
    /// field and code.
    pub dropped_rows: Vec<DroppedRows>,
}

/// Rows the crop left out for one reason: a kept trip's stop_times row
/// naming a stop, or a trip naming a route, that the feed lacks, a trip
/// that losing such rows left with fewer than two stop_times, or an exact
/// repeat of a kept trips.txt row.
#[derive(Serialize, Debug, Clone, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct DroppedRows {
    /// The notice the rows raise in the source or, kept, in the cropped feed.
    pub code: &'static str,
    pub filename: &'static str,
    pub field_name: &'static str,
    pub parent_filename: Option<&'static str>,
    pub row_count: usize,
    /// The distinct values among the rows.
    pub value_count: usize,
    /// Up to 50 of them, sorted and clipped.
    pub sample_values: Vec<String>,
}

/// A file, a field and the notice code its left-out rows raise.
type Reason = (&'static str, &'static str, &'static str);

/// The rows left out, per file, field and code, with the parent table
/// and the distinct values named. A dangling stop is held here instead of
/// among the kept stops.
#[derive(Default, Clone)]
struct Dropped {
    rows: BTreeMap<Reason, (Option<&'static str>, usize, BTreeSet<String>)>,
}

impl Dropped {
    /// Whether `value`, from `file`'s `field`, names none of `known`, the
    /// ids of `parent` (None: the feed has no `parent` to check against);
    /// such a row is counted. An empty value names nothing.
    fn dangles(
        &mut self,
        file: &'static str,
        field: &'static str,
        parent: &'static str,
        known: Option<&HashSet<&str>>,
        value: &str,
    ) -> bool {
        if value.is_empty() || known.is_none_or(|ids| ids.contains(value)) {
            return false;
        }
        self.count("foreign_key_violation", file, field, Some(parent), value);
        true
    }

    /// Count a row of `file` left out as `code`, its `field` holding `value`.
    fn count(
        &mut self,
        code: &'static str,
        file: &'static str,
        field: &'static str,
        parent: Option<&'static str>,
        value: &str,
    ) {
        let (_, rows, values) = self
            .rows
            .entry((file, field, code))
            .or_insert_with(|| (parent, 0, BTreeSet::new()));
        *rows += 1;
        if !values.contains(value) {
            values.insert(value.to_string());
        }
    }

    fn records(self) -> Vec<DroppedRows> {
        self.rows
            .into_iter()
            .map(
                |((filename, field_name, code), (parent, row_count, values))| DroppedRows {
                    code,
                    filename,
                    field_name,
                    parent_filename: parent,
                    row_count,
                    value_count: values.len(),
                    sample_values: values.iter().take(50).map(|v| rules::clip(v)).collect(),
                },
            )
            .collect()
    }
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
    // crop silently wrong. Sampled notices do not matter, as the crop reads
    // no notices. The cropped feed is validated below.
    let incomplete = result.incomplete.iter().map(String::as_str);
    if let Some(reason) = scan::refusal(&result.notices, incomplete, &options, None, "crop") {
        return Err(reason);
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
    // Kept trips and stop_times must name a route and a stop the feed has,
    // which a parent table without its id column cannot tell.
    for (file, field) in [("routes.txt", "route_id"), ("stops.txt", "stop_id")] {
        if result
            .tables
            .get(file)
            .is_some_and(|table| column(table, field).is_none())
        {
            return Err(format!(
                "{file} has no {field} column; cannot crop this feed"
            ));
        }
    }
    let mut dropped = Dropped::default();
    let mut source_notices: Vec<Notice> = whitespace(&result.notices).collect();
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
    let (mut kept_trips, trips) = select_trips(
        &source,
        &options,
        crop_options,
        touched.as_ref(),
        active.as_ref(),
        &mut source_notices,
        &mut dropped,
        ids(&result, "routes.txt", "route_id").as_ref(),
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
        &mut kept_trips,
        crop_options,
        &mut source_notices,
        &mut dropped,
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
    // A cropped feed the budgets cannot read whole is not published: its
    // report would describe only part of it. Sampled notices stay in the
    // report, whose notice_limit_reached says so.
    let incomplete = validation.incomplete.iter().map(String::as_str);
    if let Some(reason) = scan::refusal(&validation.notices, incomplete, &options, None, "crop") {
        let _ = std::fs::remove_file(&staging);
        return Err(reason);
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
        source_notices,
        dropped_rows: dropped.records(),
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

/// The named table streamed from the archive under the delimiter guard,
/// with records capped at `MAX_RECORD_BYTES` as no entry budget bounds
/// them, or None when the archive has no such entry or it is empty.
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
    let guarded = DelimiterGuard::new(entry, options, MAX_RECORD_BYTES);
    match TableReader::open(spec, guarded, options, u64::MAX, &mut notices) {
        Ok(reader) => Ok(Some(reader)),
        Err(NoTable::Empty) => Ok(None),
        Err(NoTable::Unreadable) => Err(unreadable(name, &notices, options)),
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
    options: &ScanOptions,
) -> Result<(), String> {
    if reader.truncated() {
        return Err(unreadable(name, notices, options));
    }
    Ok(())
}

fn unreadable(name: &str, notices: &[crate::notice::Notice], options: &ScanOptions) -> String {
    scan::refusal(notices, [name], options, None, "crop")
        .unwrap_or_else(|| format!("{name} cannot be read whole; cannot crop this feed"))
}

/// The notices of what the reader trimmed, among a pass's notices.
fn whitespace(notices: &[Notice]) -> impl Iterator<Item = Notice> + '_ {
    notices
        .iter()
        .filter(|n| n.code == "leading_or_trailing_whitespaces")
        .cloned()
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
    // service calendars.
    match (&crop_options.start_date, &crop_options.end_date) {
        (None, None) => Ok(None),
        (start, end) => {
            let parse = |value: &Option<String>, fallback: &str| {
                chrono::NaiveDate::parse_from_str(value.as_deref().unwrap_or(fallback), "%Y%m%d")
                    .map_err(|_| "invalid crop date; expected YYYYMMDD".to_string())
            };
            let window_start = parse(start, "00010101")?;
            let window_end = parse(end, "99991231")?;
            Ok(Some(semantics::active_services_between(
                &result.tables,
                options,
                window_start,
                window_end,
            )))
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
    whole(&reader, "stop_times.txt", &notices, options)?;
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
        whole(&reader, "stop_times.txt", &notices, options)?;
        touched.retain(|trip| !partly_outside.contains(trip));
    }
    Ok(touched)
}

/// Decide which trips survive every crop, streaming trips.txt and keeping
/// only the survivors as the trips table. `routes` holds the routes.txt ids.
/// An exact repeat of a kept row is left out and counted.
#[allow(clippy::too_many_arguments)]
fn select_trips(
    source: &std::fs::File,
    options: &ScanOptions,
    crop_options: &CropOptions,
    touched: Option<&HashSet<String>>,
    active: Option<&HashSet<String>>,
    source_notices: &mut Vec<Notice>,
    dropped: &mut Dropped,
    routes: Option<&HashSet<&str>>,
) -> Result<(HashSet<String>, Table), String> {
    let mut archive = open_archive(source)?;
    let Some(mut reader) = stream_table(&mut archive, "trips.txt", options)? else {
        return Err("feed has no usable trips.txt".to_string());
    };
    let headers = reader.headers().to_vec();
    let trip_index = position(&headers, "trip_id").ok_or("trips.txt has no trip_id column")?;
    let service_index = position(&headers, "service_id");
    let route_index = position(&headers, "route_id");
    let mut kept: HashMap<String, usize> = HashMap::new();
    let mut rows: Vec<Row> = Vec::new();
    let mut notices = Vec::new();
    while let Some(row) = reader.next_row(&mut notices) {
        if let Some(routes) = &crop_options.routes {
            let route = route_index.map(|i| row.fields[i].as_str()).unwrap_or("");
            if !routes.contains(route) {
                continue;
            }
        }
        if let Some(excluded) = &crop_options.exclude_trips {
            if excluded.contains(&row.fields[trip_index]) {
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
        if route_index.is_some_and(|i| {
            dropped.dangles(
                "trips.txt",
                "route_id",
                "routes.txt",
                routes,
                &row.fields[i],
            )
        }) {
            continue;
        }
        let trip = &row.fields[trip_index];
        if let Some(&first) = kept.get(trip) {
            // An exact repeat is left out; any other is ambiguous, and with no
            // row cap on trips.txt a way to grow the kept table without bound.
            if rows[first].fields != row.fields {
                return Err(format!(
                    "trips.txt repeats trip_id {trip:?}; an ambiguous feed cannot be cropped"
                ));
            }
            dropped.count("duplicate_key", "trips.txt", "trip_id", None, trip);
            continue;
        }
        kept.insert(trip.clone(), rows.len());
        rows.push(Row {
            csv_row: row.csv_row,
            fields: row.fields,
        });
    }
    whole(&reader, "trips.txt", &notices, options)?;
    source_notices.extend(whitespace(&notices));
    Ok((kept.into_keys().collect(), Table { headers, rows }))
}

/// Write the cropped feed: the kept trips' stop_times straight from the
/// source (pass 2), the parsed tables after the cascade, the kept trips'
/// shapes straight from the source, and the entries copied through. A
/// kept trip that the dropped stop_times rows leave with fewer than two of
/// its two or more goes, its other rows with it.
/// Returns the row counts of the streamed tables.
#[allow(clippy::too_many_arguments)]
fn write_cropped(
    source: &std::fs::File,
    staging: &Path,
    options: &ScanOptions,
    result: &mut ScanResult,
    kept_trips: &mut HashSet<String>,
    crop_options: &CropOptions,
    source_notices: &mut Vec<Notice>,
    dropped: &mut Dropped,
) -> Result<BTreeMap<String, usize>, String> {
    let mut zip = ZipOutput::create(staging)?;
    let mut counts = BTreeMap::new();
    let mut served = Served::default();
    {
        let stops = ids(result, "stops.txt", "stop_id");
        let groups = ids(result, "location_groups.txt", "location_group_id");
        let (before, notices_before) = (dropped.clone(), source_notices.len());
        let mut short = HashSet::new();
        let mut written = write_stop_times(
            source,
            options,
            &mut zip,
            kept_trips,
            &short,
            stops.as_ref(),
            groups.as_ref(),
            &mut served,
            dropped,
            source_notices,
        )?;
        if let Some((_, found)) = written.as_mut().filter(|(_, found)| !found.is_empty()) {
            // stop_times.txt is the first entry, so the archive starts
            // again and the second pass counts drops and notices afresh.
            short = std::mem::take(found);
            *dropped = before;
            source_notices.truncate(notices_before);
            served = Served::default();
            zip = zip.restart()?;
            written = write_stop_times(
                source,
                options,
                &mut zip,
                kept_trips,
                &short,
                stops.as_ref(),
                groups.as_ref(),
                &mut served,
                dropped,
                source_notices,
            )?;
            // An unchanged source, read again, leaves the same trips short.
            if !written.as_ref().is_some_and(|(_, found)| *found == short) {
                return Err("the source changed while it was cropped".to_string());
            }
        }
        for trip in &short {
            dropped.count("unusable_trip", "trips.txt", "trip_id", None, trip);
        }
        kept_trips.retain(|trip| !short.contains(trip));
        if let Some((count, _)) = written {
            counts.insert("stop_times.txt".to_string(), count);
        }
    }
    retain(
        result,
        kept_trips,
        (&crop_options.start_date, &crop_options.end_date),
        served,
    );
    // An optional table the crop emptied is left out rather than written as
    // a header alone, which validators report as an empty file.
    result.tables.retain(|name, table| {
        !table.rows.is_empty() || schema::spec_for(name).is_none_or(|spec| spec.required)
    });
    for (name, table) in &result.tables {
        zip.table(name, table)?;
    }
    let kept_shapes = referenced(result, "trips.txt", "shape_id");
    if !kept_shapes.is_empty() {
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
            whole(&reader, "shapes.txt", &notices, options)?;
            source_notices.extend(whitespace(&notices));
            counts.insert("shapes.txt".to_string(), count);
        }
    }
    zip.passthrough(&mut open_archive(source)?, &result.unparsed_entries)?;
    zip.finish()?;
    Ok(counts)
}

/// What the stop_times rows name: the stops and defined location groups of
/// the rows written, and the defined location groups of every row read.
#[derive(Default)]
struct Served {
    stops: HashSet<String>,
    location_groups: HashSet<String>,
    named_location_groups: HashSet<String>,
}

/// Stream the kept trips' stop_times from the source into `zip`, leaving
/// out a row naming a stop that `stops` lacks and every row of the `short`
/// trips, and record in `served` what the rows name, of location groups
/// only those `groups` defines. Returns the rows
/// written and the kept trips that lost such a row and kept fewer than two
/// of their two or more; None without stop_times.txt.
#[allow(clippy::too_many_arguments)]
fn write_stop_times(
    source: &std::fs::File,
    options: &ScanOptions,
    zip: &mut ZipOutput,
    kept_trips: &HashSet<String>,
    short: &HashSet<String>,
    stops: Option<&HashSet<&str>>,
    groups: Option<&HashSet<&str>>,
    served: &mut Served,
    dropped: &mut Dropped,
    source_notices: &mut Vec<Notice>,
) -> Result<Option<(usize, HashSet<String>)>, String> {
    let mut archive = open_archive(source)?;
    let Some(mut reader) = stream_table(&mut archive, "stop_times.txt", options)? else {
        return Ok(None);
    };
    let headers = reader.headers().to_vec();
    let trip = position(&headers, "trip_id");
    let stop = position(&headers, "stop_id");
    let group = position(&headers, "location_group_id");
    // Per kept trip, its rows and the rows left out.
    let mut tally: HashMap<&str, (usize, usize)> =
        kept_trips.iter().map(|t| (t.as_str(), (0, 0))).collect();
    let mut notices = Vec::new();
    let rows = std::iter::from_fn(|| reader.next_row(&mut notices)).filter_map(|row| {
        // Only defined groups are recorded, so the sets stay within the
        // loaded location_groups.txt.
        let group = group
            .map(|i| &row.fields[i])
            .filter(|g| groups.is_some_and(|known| known.contains(g.as_str())));
        if let Some(g) = group {
            if !served.named_location_groups.contains(g) {
                served.named_location_groups.insert(g.clone());
            }
        }
        let id = row.fields[trip?].as_str();
        let (seen, lost) = tally.get_mut(id)?;
        *seen += 1;
        let value = stop.map(|i| &row.fields[i]);
        if value
            .is_some_and(|v| dropped.dangles("stop_times.txt", "stop_id", "stops.txt", stops, v))
        {
            *lost += 1;
            return None;
        }
        if short.contains(id) {
            return None;
        }
        if let Some(v) = value {
            served.stops.insert(v.clone());
        }
        if let Some(g) = group {
            served.location_groups.insert(g.clone());
        }
        Some(row.fields)
    });
    let count = zip.rows("stop_times.txt", &headers, rows)?;
    whole(&reader, "stop_times.txt", &notices, options)?;
    source_notices.extend(whitespace(&notices));
    let found = tally
        .into_iter()
        .filter(|&(_, (seen, lost))| seen >= 2 && seen - lost < 2)
        .map(|(trip, _)| trip.to_string())
        .collect();
    Ok(Some((count, found)))
}

/// Retain only the kept trips and everything they reference, then the
/// supporting entities between retained stops and the definitions still
/// named.
fn retain(
    result: &mut ScanResult,
    kept_trips: &HashSet<String>,
    window: (&Option<String>, &Option<String>),
    served: Served,
) {
    let mut named = named_definitions(result);
    named
        .entry("location_groups.txt")
        .or_default()
        .extend(served.named_location_groups);
    keep_rows(result, "trips.txt", "trip_id", kept_trips);
    keep_rows(result, "stop_times.txt", "trip_id", kept_trips);
    keep_rows(result, "frequencies.txt", "trip_id", kept_trips);

    // Stops actually served (their full sequences), plus their parents.
    let mut kept_stops = served.stops;
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
    prune_definitions(result, &named, served.location_groups);
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
    retain_fares(result, &kept_routes, &kept_agencies);
}

/// A file and one of its columns.
type Column = (&'static str, &'static str);

/// The definition tables, their id column and the columns naming their
/// rows. stop_times.txt also names location groups; it is streamed, so its
/// ids come through `Served`.
const DEFINITIONS: &[(&str, &str, &[Column])] = &[
    (
        "areas.txt",
        "area_id",
        &[
            ("stop_areas.txt", "area_id"),
            ("fare_leg_rules.txt", "from_area_id"),
            ("fare_leg_rules.txt", "to_area_id"),
        ],
    ),
    (
        "location_groups.txt",
        "location_group_id",
        &[("location_group_stops.txt", "location_group_id")],
    ),
    (
        "networks.txt",
        "network_id",
        &[
            ("route_networks.txt", "network_id"),
            ("routes.txt", "network_id"),
            ("fare_leg_rules.txt", "network_id"),
            ("fare_leg_join_rules.txt", "from_network_id"),
            ("fare_leg_join_rules.txt", "to_network_id"),
        ],
    ),
];

/// Per definition table, the ids its naming columns hold.
fn named_definitions(result: &ScanResult) -> HashMap<&'static str, HashSet<String>> {
    DEFINITIONS
        .iter()
        .map(|&(file, _, naming)| {
            let named = naming
                .iter()
                .flat_map(|&(table, field)| referenced(result, table, field))
                .collect();
            (file, named)
        })
        .collect()
}

/// Leave out a definition row whose id was `named` before the crop and is
/// named by no kept row; `location_groups` holds the ids the kept
/// stop_times rows name.
fn prune_definitions(
    result: &mut ScanResult,
    named: &HashMap<&'static str, HashSet<String>>,
    location_groups: HashSet<String>,
) {
    let mut still = named_definitions(result);
    still
        .entry("location_groups.txt")
        .or_default()
        .extend(location_groups);
    for &(file, field, _) in DEFINITIONS {
        let Some(table) = result.tables.get_mut(file) else {
            continue;
        };
        let Some(i) = column(table, field) else {
            continue;
        };
        let (before, after) = (&named[file], &still[file]);
        table.rows.retain(|row| {
            let id = &row.fields[i];
            !before.contains(id) || after.contains(id)
        });
    }
}

/// A fare rule naming a removed route or zone goes. Its fare goes whole,
/// rules included, when that leaves the fare without the route rules or
/// origin-destination rules it had, or without a contains zone it named,
/// so no fare applies more widely than in the source; so does a fare of a
/// pruned agency. A fare without rules applies everywhere and stays.
fn retain_fares(
    result: &mut ScanResult,
    kept_routes: &HashSet<String>,
    kept_agencies: &HashSet<String>,
) {
    fn value(row: &Row, index: Option<usize>) -> Option<&str> {
        index
            .map(|i| row.fields[i].as_str())
            .filter(|id| !id.is_empty())
    }
    let kept_zones = referenced(result, "stops.txt", "zone_id");
    let mut dropped: HashSet<String> = HashSet::new();
    if !kept_agencies.is_empty() {
        if let Some(fares) = result.tables.get("fare_attributes.txt") {
            if let Some(id) = column(fares, "fare_id") {
                let agency = column(fares, "agency_id");
                dropped.extend(
                    fares
                        .rows
                        .iter()
                        .filter(|row| {
                            value(row, agency).is_some_and(|a| !kept_agencies.contains(a))
                        })
                        .map(|row| row.fields[id].clone()),
                );
            }
        }
    }
    if let Some(rules) = result.tables.get_mut("fare_rules.txt") {
        let fare = column(rules, "fare_id");
        let route = column(rules, "route_id");
        let ends = [column(rules, "origin_id"), column(rules, "destination_id")];
        let contains = column(rules, "contains_id");
        let dead = |row: &Row| {
            value(row, route).is_some_and(|id| !kept_routes.contains(id))
                || [ends[0], ends[1], contains]
                    .into_iter()
                    .any(|zone| value(row, zone).is_some_and(|id| !kept_zones.contains(id)))
        };
        // Per fare: whether it had route rules and whether one survives, the
        // same for origin-destination rules, and the contains zones of all
        // its rules and of the surviving ones.
        #[derive(Default)]
        struct Selectors<'r> {
            routes: [bool; 2],
            pairs: [bool; 2],
            zones: [HashSet<&'r str>; 2],
        }
        let mut selectors: BTreeMap<&str, Selectors> = BTreeMap::new();
        for row in &rules.rows {
            let live = !dead(row);
            let entry = selectors.entry(value(row, fare).unwrap_or("")).or_default();
            if value(row, route).is_some() {
                entry.routes = [true, entry.routes[1] || live];
            }
            if ends.iter().any(|&end| value(row, end).is_some()) {
                entry.pairs = [true, entry.pairs[1] || live];
            }
            if let Some(zone) = value(row, contains) {
                entry.zones[0].insert(zone);
                if live {
                    entry.zones[1].insert(zone);
                }
            }
        }
        dropped.extend(
            selectors
                .into_iter()
                .filter(|(_, s)| {
                    s.routes == [true, false]
                        || s.pairs == [true, false]
                        || s.zones[0].len() > s.zones[1].len()
                })
                .map(|(id, _)| id.to_string()),
        );
        rules
            .rows
            .retain(|row| !dead(row) && !dropped.contains(value(row, fare).unwrap_or("")));
    }
    if !dropped.is_empty() {
        if let Some(fares) = result.tables.get_mut("fare_attributes.txt") {
            if let Some(id) = column(fares, "fare_id") {
                fares.rows.retain(|row| !dropped.contains(&row.fields[id]));
            }
        }
    }
}

fn referenced(result: &ScanResult, file: &str, field: &str) -> HashSet<String> {
    ids(result, file, field)
        .map(|ids| ids.into_iter().map(str::to_string).collect())
        .unwrap_or_default()
}

/// The non-empty values of a parsed table's column, borrowed, or None when
/// the table or the column is absent.
fn ids<'a>(result: &'a ScanResult, file: &str, field: &str) -> Option<HashSet<&'a str>> {
    let table = result.tables.get(file)?;
    column(table, field).map(|i| {
        table
            .rows
            .iter()
            .map(|row| row.fields[i].as_str())
            .filter(|id| !id.is_empty())
            .collect()
    })
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
            exclude_trips: None,
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

    #[test]
    fn only_a_table_cut_short_refuses_the_crop() {
        let dir = std::env::temp_dir().join(format!("transitio-crop-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let mut files = crate::scan::tests::minimal();
        files.retain(|(name, _)| *name != "stops.txt");
        // two warnings in the source (empty rows) and two in the cropped
        // feed (names with a line break)
        files.push((
            "stops.txt",
            "stop_id,stop_name,stop_lat,stop_lon\n\
             s1,\"Ka\npi\",60.169,24.931\n,,,\ns2,\"Ste\nsi\",60.171,24.941\n,,,\n",
        ));
        let flooded = format!(
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n{}\n",
            ",".repeat(5000)
        );
        let crop_options = CropOptions {
            bbox: Some((24.9, 60.1, 25.0, 60.2)),
            polygon: None,
            start_date: None,
            end_date: None,
            full_trips_only: false,
            routes: None,
            exclude_trips: None,
        };
        let defaults = ScanOptions::default();
        // the options, a streamed stop_times.txt, and the refusal
        let cases = [
            (
                ScanOptions {
                    max_notices_per_file: 1,
                    ..defaults
                },
                None,
                None,
            ),
            (
                ScanOptions {
                    max_rows: 1,
                    ..defaults
                },
                None,
                Some("stops.txt exceeds max_rows (1); raise it to crop this feed"),
            ),
            (
                ScanOptions {
                    max_entry_bytes: 100,
                    ..defaults
                },
                None,
                Some("calendar.txt exceeds max_entry_bytes (100); raise it to crop this feed"),
            ),
            // 146 bytes are read before calendar.txt (123) and stops.txt (95)
            (
                ScanOptions {
                    max_entry_bytes: 100,
                    max_total_bytes: 160,
                    ..defaults
                },
                None,
                Some(
                    "calendar.txt exceeds max_entry_bytes (100), \
                     calendar.txt exceeds max_total_bytes (160), \
                     stops.txt exceeds max_total_bytes (160); raise them to crop this feed",
                ),
            ),
            (
                defaults,
                Some(flooded.as_str()),
                Some(
                    "stop_times.txt line 2 has more than 4096 delimiters outside quotes, \
                     the guard set by max_columns (1000); raise it to crop this feed",
                ),
            ),
        ];
        for (options, stop_times, expected) in cases {
            let mut files = files.clone();
            if let Some(stop_times) = stop_times {
                files.retain(|(name, _)| *name != "stop_times.txt");
                files.push(("stop_times.txt", stop_times));
            }
            let source = dir.join("source.zip");
            std::fs::write(&source, crate::scan::tests::build_zip(&files).into_inner()).unwrap();
            let output = dir.join("cropped.zip");
            let _ = std::fs::remove_file(&output);
            match (crop(&source, &output, options, &crop_options), expected) {
                (Ok(result), None) => {
                    let capped: Vec<&str> = result
                        .validation
                        .notices
                        .iter()
                        .filter(|n| n.code == "notice_limit_reached")
                        .filter_map(|n| n.context["filename"].as_str())
                        .collect();
                    assert_eq!(capped, ["stops.txt"]);
                }
                (Err(error), Some(expected)) => {
                    assert_eq!(error, expected);
                    assert!(!output.exists());
                }
                (Ok(_), Some(expected)) => panic!("cropped despite {expected}"),
                (Err(error), None) => panic!("refused: {error}"),
            }
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// The minimal feed with `extra` added or replacing its entries,
    /// cropped to route r1 in a directory of its own named after `name`.
    fn crop_to_r1(name: &str, extra: &[(&'static str, &'static str)]) -> CropResult {
        let dir =
            std::env::temp_dir().join(format!("transitio-crop-{}-{name}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let mut files = crate::scan::tests::minimal();
        files.retain(|(file, _)| extra.iter().all(|(other, _)| other != file));
        files.extend_from_slice(extra);
        let source = dir.join("source.zip");
        std::fs::write(&source, crate::scan::tests::build_zip(&files).into_inner()).unwrap();
        let crop_options = CropOptions {
            bbox: None,
            polygon: None,
            start_date: None,
            end_date: None,
            full_trips_only: false,
            routes: Some(HashSet::from(["r1".to_string()])),
            exclude_trips: None,
        };
        let output = dir.join("cropped.zip");
        let result = crop(&source, &output, ScanOptions::default(), &crop_options);
        let _ = std::fs::remove_dir_all(&dir);
        result.unwrap()
    }

    /// The ids left in a cropped table, sorted.
    fn left(result: &CropResult, file: &str, field: &str) -> Vec<String> {
        let mut ids: Vec<String> = referenced(&result.validation, file, field)
            .into_iter()
            .collect();
        ids.sort();
        ids
    }

    #[test]
    fn definitions_follow_the_rows_that_name_them() {
        let result = crop_to_r1(
            "definitions",
            &[
                (
                    "stops.txt",
                    "stop_id,stop_name,stop_lat,stop_lon\n\
                     s1,Kamppi,60.169,24.931\ns2,Steissi,60.171,24.941\ns3,Espoo,60.205,24.655\n",
                ),
                (
                    "routes.txt",
                    "route_id,agency_id,route_short_name,route_type\n\
                     r1,hsl,1,3\nr2,hsl,2,3\nr3,hsl,3,3\n",
                ),
                (
                    "trips.txt",
                    "route_id,service_id,trip_id\nr1,wk,t1\nr2,wk,t2\n",
                ),
                (
                    "stop_times.txt",
                    "trip_id,arrival_time,departure_time,stop_id,location_group_id,\
                     stop_sequence,start_pickup_drop_off_window,end_pickup_drop_off_window\n\
                     t1,08:00:00,08:00:00,s1,,1,,\nt1,08:05:00,08:05:00,s2,,2,,\n\
                     t1,,,,lg-flex,3,08:05:00,09:00:00\n\
                     t2,09:00:00,09:00:00,s3,,1,,\nt2,09:30:00,09:30:00,s3,,2,,\n\
                     t2,,,,lg-trip,3,09:30:00,10:00:00\n",
                ),
                ("areas.txt", "area_id\na-in\na-out\na-fare\na-free\n"),
                (
                    "stop_areas.txt",
                    "area_id,stop_id\na-in,s1\na-out,s3\na-fare,s3\n",
                ),
                (
                    "fare_leg_rules.txt",
                    "leg_group_id,from_area_id,fare_product_id\nl1,a-fare,p1\n",
                ),
                ("networks.txt", "network_id\nn-in\nn-out\nn-fare\n"),
                (
                    "route_networks.txt",
                    "network_id,route_id\nn-in,r1\nn-out,r2\nn-fare,r3\n",
                ),
                (
                    "fare_leg_join_rules.txt",
                    "from_network_id,to_network_id\nn-fare,n-in\n",
                ),
                (
                    "location_groups.txt",
                    "location_group_id\nlg-in\nlg-flex\nlg-trip\n",
                ),
                (
                    "location_group_stops.txt",
                    "location_group_id,stop_id\nlg-in,s1\nlg-flex,s3\n",
                ),
            ],
        );
        assert_eq!(
            left(&result, "areas.txt", "area_id"),
            ["a-fare", "a-free", "a-in"]
        );
        assert_eq!(
            left(&result, "networks.txt", "network_id"),
            ["n-fare", "n-in"]
        );
        assert_eq!(
            left(&result, "location_groups.txt", "location_group_id"),
            ["lg-flex", "lg-in"]
        );
    }

    #[test]
    fn route_network_ids_name_networks() {
        let result = crop_to_r1(
            "route-networks",
            &[
                (
                    "routes.txt",
                    "route_id,agency_id,route_short_name,route_type,network_id\n\
                     r1,hsl,1,3,n-in\nr2,hsl,2,3,n-out\n",
                ),
                ("networks.txt", "network_id\nn-in\nn-out\n"),
            ],
        );
        assert_eq!(left(&result, "networks.txt", "network_id"), ["n-in"]);
    }
}
