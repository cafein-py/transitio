//! GTFS ingest and validation core: parses a feed zip into raw tables while
//! collecting notices under the canonical gtfs-validator code convention,
//! instead of failing hard on the first defect.

pub mod crop;
pub mod fields;
pub mod notice;
pub mod repair;
pub mod rules;
pub mod scan;
pub mod schema;
pub mod semantics;

pub use crop::{crop, CropOptions, CropResult};
pub use notice::{Notice, Severity};
pub use repair::{repair, Fix, RepairResult};
pub use scan::{
    scan, scan_reader, scan_reader_with, scan_with, ScanOptions, ScanResult, Table,
    DEFAULT_MAX_COLUMNS, DEFAULT_MAX_ENTRY_BYTES, DEFAULT_MAX_ROWS, DEFAULT_MAX_TOTAL_BYTES,
};

/// The full current rule set: the structural scan plus the field-format and
/// referential-integrity tiers.
pub fn validate(path: &std::path::Path, mut options: ScanOptions) -> Result<ScanResult, String> {
    if options.reference_date.is_none() {
        options.reference_date = Some(chrono::Utc::now().date_naive());
    }
    let mut result = scan::scan_with(path, options)?;
    rules::run_rules(&mut result, &options);
    semantics::run_semantics(&mut result, &options);
    Ok(result)
}
