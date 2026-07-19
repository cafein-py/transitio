use pyo3::exceptions::{PyIOError, PyValueError};
use pyo3::prelude::*;

/// Run the structural scan on a GTFS zip; returns the report as a JSON
/// string ({"notices": [...], "row_counts": {...}}) that the Python layer
/// decodes. Serializing once here keeps the boundary to a single string
/// instead of nested Python object construction.
#[pyfunction]
#[pyo3(signature = (path, *, max_entry_bytes=None, max_total_bytes=None, max_rows=None, max_columns=None, max_notices_per_file=None, reference_date=None))]
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
    if let Some(value) = reference_date {
        let parsed = chrono::NaiveDate::parse_from_str(value.trim(), "%Y%m%d")
            .map_err(|_| PyValueError::new_err(format!("invalid reference_date: {value:?}")))?;
        options.reference_date = Some(parsed);
    }
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
        });
        Ok(report.to_string())
    })
    .map_err(|e: String| PyIOError::new_err(e))
}

/// Repair a feed into `output` and return the fix log plus the
/// post-parse validation notices as JSON.
#[pyfunction]
#[pyo3(signature = (path, output, *, max_entry_bytes=None, max_total_bytes=None, max_rows=None, max_columns=None, max_notices_per_file=None, reference_date=None))]
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
    if let Some(value) = reference_date {
        let parsed = chrono::NaiveDate::parse_from_str(value.trim(), "%Y%m%d")
            .map_err(|_| PyValueError::new_err(format!("invalid reference_date: {value:?}")))?;
        options.reference_date = Some(parsed);
    }
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
#[pyo3(signature = (path, output, *, bbox=None, start_date=None, end_date=None, full_trips_only=false, max_entry_bytes=None, max_total_bytes=None, max_rows=None, max_columns=None, max_notices_per_file=None, reference_date=None))]
#[allow(clippy::too_many_arguments)]
fn crop_feed(
    py: Python<'_>,
    path: std::path::PathBuf,
    output: std::path::PathBuf,
    bbox: Option<(f64, f64, f64, f64)>,
    start_date: Option<String>,
    end_date: Option<String>,
    full_trips_only: bool,
    max_entry_bytes: Option<u64>,
    max_total_bytes: Option<u64>,
    max_rows: Option<u64>,
    max_columns: Option<usize>,
    max_notices_per_file: Option<u64>,
    reference_date: Option<&str>,
) -> PyResult<String> {
    if bbox.is_none() && start_date.is_none() && end_date.is_none() {
        return Err(PyValueError::new_err(
            "nothing to crop: pass bbox and/or a date window",
        ));
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
    if let Some(value) = reference_date {
        let parsed = chrono::NaiveDate::parse_from_str(value.trim(), "%Y%m%d")
            .map_err(|_| PyValueError::new_err(format!("invalid reference_date: {value:?}")))?;
        options.reference_date = Some(parsed);
    }
    let crop_options = transitio_gtfs::CropOptions {
        bbox,
        start_date,
        end_date,
        full_trips_only,
    };
    py.allow_threads(move || {
        let result = transitio_gtfs::crop(&path, &output, options, &crop_options)?;
        let report = serde_json::json!({
            "row_counts": result.row_counts,
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
