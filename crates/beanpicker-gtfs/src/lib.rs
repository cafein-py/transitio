//! GTFS ingest and validation core: parses a feed zip into raw tables while
//! collecting notices under the canonical gtfs-validator code convention,
//! instead of failing hard on the first defect.

pub mod fields;
pub mod notice;
pub mod rules;
pub mod scan;
pub mod schema;

pub use notice::{Notice, Severity};
pub use scan::{
    scan, scan_reader, scan_reader_with, scan_with, ScanOptions, ScanResult, Table,
    DEFAULT_MAX_COLUMNS, DEFAULT_MAX_ENTRY_BYTES, DEFAULT_MAX_ROWS, DEFAULT_MAX_TOTAL_BYTES,
};

/// The full current rule set: the structural scan plus the field-format and
/// referential-integrity tiers.
pub fn validate(path: &std::path::Path, options: ScanOptions) -> Result<ScanResult, String> {
    let mut result = scan::scan_with(path, options)?;
    rules::run_rules(&mut result, &options);
    Ok(result)
}
