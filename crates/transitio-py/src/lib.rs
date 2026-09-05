use pyo3::exceptions::{PyIOError, PyValueError};
use pyo3::prelude::*;

/// Wall-clock "HH:MM" / "HH:MM:SS" (00-23 h) — GTFS >24:00 notation is a
/// service-day offset, not a clock time.
fn parse_clock_time(value: &str) -> Option<u32> {
    let parts: Vec<&str> = value.trim().split(':').collect();
    if parts.len() != 2 && parts.len() != 3 {
        return None;
    }
    // Exactly two digits per component, per the documented "HH:MM[:SS]".
    if parts
        .iter()
        .any(|part| part.len() != 2 || !part.bytes().all(|b| b.is_ascii_digit()))
    {
        return None;
    }
    let hours: u32 = parts[0].parse().ok()?;
    let minutes: u32 = parts[1].parse().ok()?;
    let seconds: u32 = parts.get(2).map_or(Some(0), |p| p.parse().ok())?;
    if hours > 23 || minutes > 59 || seconds > 59 {
        return None;
    }
    Some(hours * 3600 + minutes * 60 + seconds)
}

/// Stop bounds from the already-budgeted stops table: [min_lon,
/// min_lat, max_lon, max_lat], skipping non-finite coordinates. None
/// when no finite coordinate exists or stops.txt is incomplete —
/// partial bounds from a truncated table would mislead.
fn stop_bounds(result: &transitio_gtfs::ScanResult) -> Option<[f64; 4]> {
    if result.incomplete.contains("stops.txt") {
        return None;
    }
    let table = result.tables.get("stops.txt")?;
    let lat = table.headers.iter().position(|h| h == "stop_lat")?;
    let lon = table.headers.iter().position(|h| h == "stop_lon")?;
    let mut bounds: Option<[f64; 4]> = None;
    for row in &table.rows {
        let (Ok(lat), Ok(lon)) = (
            row.fields[lat].trim().parse::<f64>(),
            row.fields[lon].trim().parse::<f64>(),
        ) else {
            continue;
        };
        if !lat.is_finite() || !lon.is_finite() {
            continue;
        }
        bounds = Some(match bounds {
            None => [lon, lat, lon, lat],
            Some(b) => [b[0].min(lon), b[1].min(lat), b[2].max(lon), b[3].max(lat)],
        });
    }
    bounds
}

/// An explicit reference_date drives the expiry checks AND targets the
/// date(-time) service checks; reference_time refines the latter.
fn apply_reference(
    options: &mut transitio_gtfs::ScanOptions,
    reference_date: Option<&str>,
    reference_time: Option<&str>,
) -> PyResult<()> {
    if reference_time.is_some() && reference_date.is_none() {
        return Err(PyValueError::new_err(
            "reference_time requires reference_date",
        ));
    }
    if let Some(value) = reference_date {
        let date = chrono::NaiveDate::parse_from_str(value.trim(), "%Y%m%d")
            .map_err(|_| PyValueError::new_err(format!("invalid reference_date: {value:?}")))?;
        let time = match reference_time {
            Some(value) => Some(parse_clock_time(value).ok_or_else(|| {
                PyValueError::new_err(format!(
                    "invalid reference_time: {value:?} (expected HH:MM or HH:MM:SS)"
                ))
            })?),
            None => None,
        };
        options.reference_date = Some(date);
        options.moment = Some(transitio_gtfs::Moment { date, time });
    }
    Ok(())
}

/// Run the structural scan on a GTFS zip; returns the report as a JSON
/// string ({"notices": [...], "row_counts": {...}}) that the Python layer
/// decodes. Serializing once here keeps the boundary to a single string
/// instead of nested Python object construction.
#[pyfunction]
#[pyo3(signature = (path, *, max_entry_bytes=None, max_total_bytes=None, max_rows=None, max_columns=None, max_notices_per_file=None, reference_date=None, reference_time=None))]
#[allow(clippy::too_many_arguments)]
fn scan_feed(
    py: Python<'_>,
    path: std::path::PathBuf,
    max_entry_bytes: Option<u64>,
    max_total_bytes: Option<u64>,
    max_rows: Option<u64>,
    max_columns: Option<usize>,
    max_notices_per_file: Option<u64>,
    reference_date: Option<&str>,
    reference_time: Option<&str>,
) -> PyResult<String> {
    let mut options = transitio_gtfs::ScanOptions::default();
    if let Some(value) = max_entry_bytes {
        options.max_entry_bytes = value;
    }
    if let Some(value) = max_total_bytes {
        options.max_total_bytes = value;
    }
    if let Some(value) = max_rows {
        options.max_rows = value;
    }
    if let Some(value) = max_columns {
        options.max_columns = value;
    }
    if let Some(value) = max_notices_per_file {
        options.max_notices_per_file = value;
    }
    apply_reference(&mut options, reference_date, reference_time)?;
    // The whole scan (I/O, decompression, parsing, serialization) runs
    // without the GIL; only the result crosses back into Python.
    py.allow_threads(move || {
        let result = transitio_gtfs::validate(&path, options)?;
        let mut row_counts = serde_json::Map::new();
        for (name, table) in &result.tables {
            row_counts.insert(name.clone(), serde_json::Value::from(table.rows.len()));
        }
        let report = serde_json::json!({
            "notices": result.notices,
            "row_counts": row_counts,
            "service_window": result.service_window,
            "readiness": result.readiness,
            "moment": result.moment,
            "incomplete": result.incomplete,
            "stop_bounds": stop_bounds(&result),
        });
        Ok(report.to_string())
    })
    .map_err(|e: String| PyIOError::new_err(e))
}

/// Repair a feed into `output` and return the fix log plus the
/// post-parse validation notices as JSON.
#[pyfunction]
#[pyo3(signature = (path, output, *, max_entry_bytes=None, max_total_bytes=None, max_rows=None, max_columns=None, max_notices_per_file=None, reference_date=None, reference_time=None))]
#[allow(clippy::too_many_arguments)]
fn repair_feed(
    py: Python<'_>,
    path: std::path::PathBuf,
    output: std::path::PathBuf,
    max_entry_bytes: Option<u64>,
    max_total_bytes: Option<u64>,
    max_rows: Option<u64>,
    max_columns: Option<usize>,
    max_notices_per_file: Option<u64>,
    reference_date: Option<&str>,
    reference_time: Option<&str>,
) -> PyResult<String> {
    let mut options = transitio_gtfs::ScanOptions::default();
    if let Some(value) = max_entry_bytes {
        options.max_entry_bytes = value;
    }
    if let Some(value) = max_total_bytes {
        options.max_total_bytes = value;
    }
    if let Some(value) = max_rows {
        options.max_rows = value;
    }
    if let Some(value) = max_columns {
        options.max_columns = value;
    }
    if let Some(value) = max_notices_per_file {
        options.max_notices_per_file = value;
    }
    apply_reference(&mut options, reference_date, reference_time)?;
    py.allow_threads(move || {
        let result = transitio_gtfs::repair(&path, &output, options)?;
        let report = serde_json::json!({
            "fixes": result.fixes,
            "remaining_notices": result.validation.notices,
            "service_window": result.validation.service_window,
        });
        Ok(report.to_string())
    })
    .map_err(|e: String| PyIOError::new_err(e))
}

/// Crop a feed spatially and/or temporally into `output`.
#[pyfunction]
#[pyo3(signature = (path, output, *, bbox=None, polygon=None, start_date=None, end_date=None, full_trips_only=false, routes=None, max_entry_bytes=None, max_total_bytes=None, max_rows=None, max_columns=None, max_notices_per_file=None, reference_date=None, reference_time=None))]
#[allow(clippy::too_many_arguments)]
fn crop_feed(
    py: Python<'_>,
    path: std::path::PathBuf,
    output: std::path::PathBuf,
    bbox: Option<(f64, f64, f64, f64)>,
    polygon: Option<Vec<transitio_gtfs::PolygonRings>>,
    start_date: Option<String>,
    end_date: Option<String>,
    full_trips_only: bool,
    routes: Option<Vec<String>>,
    max_entry_bytes: Option<u64>,
    max_total_bytes: Option<u64>,
    max_rows: Option<u64>,
    max_columns: Option<usize>,
    max_notices_per_file: Option<u64>,
    reference_date: Option<&str>,
    reference_time: Option<&str>,
) -> PyResult<String> {
    if bbox.is_none()
        && polygon.is_none()
        && start_date.is_none()
        && end_date.is_none()
        && routes.is_none()
    {
        return Err(PyValueError::new_err(
            "nothing to crop: pass an area, a date window and/or routes",
        ));
    }
    if bbox.is_some() && polygon.is_some() {
        return Err(PyValueError::new_err(
            "pass either bbox or polygon, not both",
        ));
    }
    if let Some(parts) = polygon.as_deref() {
        transitio_gtfs::validate_polygon(parts).map_err(PyValueError::new_err)?;
    }
    let mut options = transitio_gtfs::ScanOptions::default();
    if let Some(value) = max_entry_bytes {
        options.max_entry_bytes = value;
    }
    if let Some(value) = max_total_bytes {
        options.max_total_bytes = value;
    }
    if let Some(value) = max_rows {
        options.max_rows = value;
    }
    if let Some(value) = max_columns {
        options.max_columns = value;
    }
    if let Some(value) = max_notices_per_file {
        options.max_notices_per_file = value;
    }
    apply_reference(&mut options, reference_date, reference_time)?;
    let crop_options = transitio_gtfs::CropOptions {
        bbox,
        polygon,
        start_date,
        end_date,
        full_trips_only,
        routes: routes.map(|r| r.into_iter().collect()),
    };
    py.allow_threads(move || {
        let result = transitio_gtfs::crop(&path, &output, options, &crop_options)?;
        let report = serde_json::json!({
            "row_counts": result.row_counts,
            "source_routes": result.source_routes,
            "remaining_notices": result.validation.notices,
            "service_window": result.validation.service_window,
        });
        Ok(report.to_string())
    })
    .map_err(|e: String| PyIOError::new_err(e))
}

#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add_function(wrap_pyfunction!(scan_feed, m)?)?;
    m.add_function(wrap_pyfunction!(repair_feed, m)?)?;
    m.add_function(wrap_pyfunction!(crop_feed, m)?)?;
    Ok(())
}
