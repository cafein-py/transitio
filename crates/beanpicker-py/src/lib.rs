use pyo3::exceptions::PyIOError;
use pyo3::prelude::*;

/// Run the structural scan on a GTFS zip; returns the report as a JSON
/// string ({"notices": [...], "row_counts": {...}}) that the Python layer
/// decodes. Serializing once here keeps the boundary to a single string
/// instead of nested Python object construction.
#[pyfunction]
#[pyo3(signature = (path, *, max_entry_bytes=None, max_total_bytes=None, max_rows=None, max_columns=None, max_notices_per_file=None))]
fn scan_feed(
    py: Python<'_>,
    path: std::path::PathBuf,
    max_entry_bytes: Option<u64>,
    max_total_bytes: Option<u64>,
    max_rows: Option<u64>,
    max_columns: Option<usize>,
    max_notices_per_file: Option<u64>,
) -> PyResult<String> {
    let mut options = beanpicker_gtfs::ScanOptions::default();
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
    // The whole scan (I/O, decompression, parsing, serialization) runs
    // without the GIL; only the result crosses back into Python.
    py.allow_threads(move || {
        let result = beanpicker_gtfs::validate(&path, options)?;
        let mut row_counts = serde_json::Map::new();
        for (name, table) in &result.tables {
            row_counts.insert(name.clone(), serde_json::Value::from(table.rows.len()));
        }
        let report = serde_json::json!({
            "notices": result.notices,
            "row_counts": row_counts,
        });
        Ok(report.to_string())
    })
    .map_err(|e: String| PyIOError::new_err(e))
}

#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add_function(wrap_pyfunction!(scan_feed, m)?)?;
    Ok(())
}
