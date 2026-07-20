use std::collections::hash_map::Entry;
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;

use crate::notice::{Notice, Severity};
use crate::schema;

/// Default guards against hostile archives (zip bombs, amplification). Byte
/// budgets bound the decompressed input; the row and column caps bound the
/// retained parsed representation and the notice count, whose per-field
/// overhead can amplify small inputs. Worst-case transient memory is a small
/// multiple of `max_entry_bytes` — lower the limits for untrusted input,
/// raise them for oversized trusted feeds. `u64::MAX` disables a byte or
/// row limit. A violated limit is reported per file (`unreadable_file` /
/// `too_many_rows`) and the scan continues; only an untraversable archive
/// aborts with an error.
pub const DEFAULT_MAX_ENTRY_BYTES: u64 = 1024 * 1024 * 1024;
pub const DEFAULT_MAX_TOTAL_BYTES: u64 = 2 * 1024 * 1024 * 1024;
pub const DEFAULT_MAX_ROWS: u64 = 20_000_000;
pub const DEFAULT_MAX_COLUMNS: usize = 1000;
pub const DEFAULT_MAX_NOTICES_PER_FILE: u64 = 10_000;

/// Central directories beyond this size are refused outright; real GTFS
/// archives hold a few dozen entries.
const MAX_CENTRAL_DIRECTORY_BYTES: u64 = 256 * 1024 * 1024;

/// Archives describing more entries than this are refused before any
/// indexing allocation; a GTFS feed holds a few dozen files.
const MAX_ARCHIVE_ENTRIES: u64 = 4096;

/// Recognized non-CSV GTFS files: not unknown, content out of the
/// structural tier's scope.
const NON_CSV_FILES: &[&str] = &["locations.geojson"];

#[derive(Clone, Copy)]
pub struct ScanOptions {
    pub max_entry_bytes: u64,
    pub max_total_bytes: u64,
    pub max_rows: u64,
    pub max_columns: usize,
    pub max_notices_per_file: u64,
    /// Reference day for expiry checks; `None` disables them. `validate`
    /// defaults it to the current date.
    pub reference_date: Option<chrono::NaiveDate>,
}

impl Default for ScanOptions {
    fn default() -> Self {
        ScanOptions {
            max_entry_bytes: DEFAULT_MAX_ENTRY_BYTES,
            max_total_bytes: DEFAULT_MAX_TOTAL_BYTES,
            max_rows: DEFAULT_MAX_ROWS,
            max_columns: DEFAULT_MAX_COLUMNS,
            max_notices_per_file: DEFAULT_MAX_NOTICES_PER_FILE,
            reference_date: None,
        }
    }
}

/// One data row with its 1-based CSV row number (header row is 1).
pub struct Row {
    pub csv_row: u64,
    pub fields: Vec<String>,
}

/// One parsed file: raw (untrimmed) headers plus the rows that survived the
/// structural checks; malformed, empty and undecodable rows are noticed and
/// skipped.
pub struct Table {
    pub headers: Vec<String>,
    pub rows: Vec<Row>,
}

pub struct ScanResult {
    pub tables: BTreeMap<String, Table>,
    pub notices: Vec<Notice>,
    /// Files whose retained content is unreliable — truncated by the row
    /// cap, unreadable, refused as duplicates, or header-unparseable.
    /// Reference checks must not treat their ID sets as exhaustive.
    pub incomplete: std::collections::BTreeSet<String>,
    /// Actual service-day window computed from the calendars; None until
    /// the semantic tier runs (or when no service is active at all).
    pub service_window: Option<(String, String)>,
    /// Root-level archive entries recognized or tolerated but not parsed
    /// into `tables` (GTFS-Flex files, unknown files); archive rewrites
    /// copy them through verbatim.
    pub unparsed_entries: Vec<String>,
}

pub fn scan(path: &Path) -> Result<ScanResult, String> {
    scan_with(path, ScanOptions::default())
}

pub fn scan_with(path: &Path, options: ScanOptions) -> Result<ScanResult, String> {
    let file = File::open(path).map_err(|e| format!("cannot open {}: {e}", path.display()))?;
    scan_reader_with(file, options)
}

pub fn scan_reader<R: Read + Seek>(reader: R) -> Result<ScanResult, String> {
    scan_reader_with(reader, ScanOptions::default())
}

pub fn scan_reader_with<R: Read + Seek>(
    mut reader: R,
    options: ScanOptions,
) -> Result<ScanResult, String> {
    // The zip crate's read index is keyed by name and silently keeps only
    // the last occurrence of a duplicated entry, so shadowed duplicates are
    // invisible through its API. GTFS files duplicated in the archive are
    // ambiguous (other readers may take the first occurrence); walk the
    // central directory directly to detect and refuse them.
    let duplicated =
        duplicated_gtfs_entries(&mut reader).map_err(|e| format!("not a readable zip: {e}"))?;
    reader
        .seek(SeekFrom::Start(0))
        .map_err(|e| format!("cannot rewind archive: {e}"))?;

    let mut archive =
        zip::ZipArchive::new(reader).map_err(|e| format!("not a readable zip: {e}"))?;
    let mut notices = Vec::new();
    let mut tables = BTreeMap::new();
    let mut incomplete = std::collections::BTreeSet::new();
    let mut unparsed_entries: Vec<String> = Vec::new();
    let mut present: HashSet<&'static str> = HashSet::new();

    for name in &duplicated {
        notices.push(Notice::new("duplicate_zip_entry", Severity::Error).with("filename", *name));
        present.insert(name);
        incomplete.insert(name.to_string());
    }

    let mut names = Vec::with_capacity(archive.len());
    for index in 0..archive.len() {
        let entry = archive
            .by_index_raw(index)
            .map_err(|e| format!("cannot read zip entry {index}: {e}"))?;
        names.push(entry.name().to_owned());
    }

    let mut total_bytes = 0u64;
    for (index, name) in names.iter().enumerate() {
        if name.ends_with('/') {
            continue;
        }
        if let Some((_, basename)) = name.rsplit_once('/') {
            // GTFS files hidden in a subfolder are a canonical error; other
            // nested entries (archive junk) are ignored by the parser but
            // still passed through verbatim on rewrites.
            if schema::spec_for(basename).is_some() {
                notices.push(
                    Notice::new("invalid_input_files_in_subfolder", Severity::Error)
                        .with("filename", name.as_str()),
                );
            }
            if !unparsed_entries.contains(name) {
                unparsed_entries.push(name.clone());
            }
            continue;
        }
        if NON_CSV_FILES.contains(&name.as_str()) {
            if !unparsed_entries.contains(name) {
                unparsed_entries.push(name.clone());
            }
            continue; // recognized GTFS-Flex file; contents out of scope
        }
        let Some(spec) = schema::spec_for(name) else {
            notices
                .push(Notice::new("unknown_file", Severity::Info).with("filename", name.as_str()));
            if !unparsed_entries.contains(name) {
                unparsed_entries.push(name.clone());
            }
            continue;
        };
        if duplicated.contains(spec.name) {
            continue; // noticed above; never parse an ambiguous table
        }
        // Per-entry failures (corrupt member, budget violation) are noticed
        // and skipped so every other readable table still gets validated;
        // only an untraversable archive aborts the scan.
        let mut entry = match archive.by_index(index) {
            Ok(entry) => entry,
            Err(error) => {
                notices.push(unreadable_file(spec.name, &error.to_string()));
                present.insert(spec.name);
                incomplete.insert(spec.name.to_string());
                continue;
            }
        };
        // An entry may never read past the remaining cumulative budget, so
        // the total limit holds while reading, not after the fact.
        let budget = options
            .max_entry_bytes
            .min(options.max_total_bytes.saturating_sub(total_bytes));
        if entry.size() > budget {
            notices.push(unreadable_file(
                spec.name,
                &format!(
                    "declares {} uncompressed bytes, over the {budget}-byte budget",
                    entry.size()
                ),
            ));
            present.insert(spec.name);
            incomplete.insert(spec.name.to_string());
            continue;
        }
        let mut bytes = Vec::new();
        // The declared size can lie; cap the bytes actually read.
        // saturating_add keeps u64::MAX usable as an unlimited sentinel.
        let mut limited = (&mut entry).take(budget.saturating_add(1));
        let read_result = limited.read_to_end(&mut bytes);
        // Every decompressed byte is charged against the cumulative budget,
        // including bytes from failed or rejected reads — otherwise each
        // entry could burn the whole budget in CPU before being discarded.
        total_bytes = total_bytes.saturating_add(bytes.len() as u64);
        if let Err(error) = read_result {
            notices.push(unreadable_file(spec.name, &error.to_string()));
            present.insert(spec.name);
            incomplete.insert(spec.name.to_string());
            continue;
        }
        if bytes.len() as u64 > budget {
            notices.push(unreadable_file(
                spec.name,
                &format!("exceeds the {budget}-byte budget"),
            ));
            present.insert(spec.name);
            incomplete.insert(spec.name.to_string());
            continue;
        }
        present.insert(spec.name);
        let (table, truncated) = read_table(spec, &bytes, &options, &mut notices);
        if truncated {
            incomplete.insert(spec.name.to_string());
        }
        if let Some(table) = table {
            tables.insert(spec.name.to_string(), table);
        }
    }

    feed_level_checks(&tables, &present, &mut notices);
    duplicate_key_checks(&tables, &options, &mut notices);

    Ok(ScanResult {
        tables,
        notices,
        incomplete,
        service_window: None,
        unparsed_entries,
    })
}

/// Walk the central directory and return the root-level GTFS filenames that
/// occur more than once. The end-of-central-directory record is located by
/// its signature in the archive tail; ZIP64 archives are followed through
/// the ZIP64 locator.
fn duplicated_gtfs_entries<R: Read + Seek>(
    reader: &mut R,
) -> Result<BTreeSet<&'static str>, String> {
    let file_len = reader
        .seek(SeekFrom::End(0))
        .map_err(|e| format!("cannot read archive length: {e}"))?;
    // EOCD is 22 bytes plus a comment of at most 65535 bytes; the ZIP64
    // locator (20 bytes) sits directly before the EOCD when present.
    let tail_len = file_len.min(22 + 65_535 + 20);
    reader
        .seek(SeekFrom::Start(file_len - tail_len))
        .map_err(|e| format!("cannot seek archive tail: {e}"))?;
    let mut tail = vec![0u8; tail_len as usize];
    reader
        .read_exact(&mut tail)
        .map_err(|e| format!("cannot read archive tail: {e}"))?;

    let eocd_pos = find_eocd(&tail).ok_or("no end-of-central-directory record found")?;
    let eocd = &tail[eocd_pos..];
    let mut total_entries = u16::from_le_bytes([eocd[10], eocd[11]]) as u64;
    let mut cd_size = u32::from_le_bytes([eocd[12], eocd[13], eocd[14], eocd[15]]) as u64;
    let mut cd_offset = u32::from_le_bytes([eocd[16], eocd[17], eocd[18], eocd[19]]) as u64;

    if total_entries == 0xFFFF || cd_size == 0xFFFF_FFFF || cd_offset == 0xFFFF_FFFF {
        // ZIP64: the locator directly precedes the EOCD.
        let locator_pos = eocd_pos
            .checked_sub(20)
            .ok_or("truncated ZIP64 end-of-central-directory locator")?;
        let locator = &tail[locator_pos..eocd_pos];
        if locator[0..4] != [0x50, 0x4b, 0x06, 0x07] {
            return Err("missing ZIP64 end-of-central-directory locator".to_string());
        }
        let zip64_eocd_offset = u64::from_le_bytes(locator[8..16].try_into().unwrap());
        reader
            .seek(SeekFrom::Start(zip64_eocd_offset))
            .map_err(|e| format!("cannot seek ZIP64 record: {e}"))?;
        let mut zip64 = [0u8; 56];
        reader
            .read_exact(&mut zip64)
            .map_err(|e| format!("cannot read ZIP64 record: {e}"))?;
        if zip64[0..4] != [0x50, 0x4b, 0x06, 0x06] {
            return Err("invalid ZIP64 end-of-central-directory record".to_string());
        }
        total_entries = u64::from_le_bytes(zip64[32..40].try_into().unwrap());
        cd_size = u64::from_le_bytes(zip64[40..48].try_into().unwrap());
        cd_offset = u64::from_le_bytes(zip64[48..56].try_into().unwrap());
    }
    if cd_size > MAX_CENTRAL_DIRECTORY_BYTES {
        return Err(format!(
            "central directory of {cd_size} bytes exceeds the {MAX_CENTRAL_DIRECTORY_BYTES}-byte limit"
        ));
    }
    if total_entries > MAX_ARCHIVE_ENTRIES {
        // Refused before ZipArchive builds its index or names are cloned.
        return Err(format!(
            "archive declares {total_entries} entries, over the {MAX_ARCHIVE_ENTRIES}-entry limit"
        ));
    }

    reader
        .seek(SeekFrom::Start(cd_offset))
        .map_err(|e| format!("cannot seek central directory: {e}"))?;
    let mut directory = vec![0u8; cd_size as usize];
    reader
        .read_exact(&mut directory)
        .map_err(|e| format!("cannot read central directory: {e}"))?;

    let mut counts: HashMap<&'static str, u32> = HashMap::new();
    let mut cursor = 0usize;
    for _ in 0..total_entries {
        let record = directory
            .get(cursor..cursor + 46)
            .ok_or("truncated central-directory record")?;
        if record[0..4] != [0x50, 0x4b, 0x01, 0x02] {
            return Err("invalid central-directory record signature".to_string());
        }
        let name_len = u16::from_le_bytes([record[28], record[29]]) as usize;
        let extra_len = u16::from_le_bytes([record[30], record[31]]) as usize;
        let comment_len = u16::from_le_bytes([record[32], record[33]]) as usize;
        let name_bytes = directory
            .get(cursor + 46..cursor + 46 + name_len)
            .ok_or("truncated central-directory filename")?;
        let name = String::from_utf8_lossy(name_bytes);
        if !name.contains('/') {
            if let Some(spec) = schema::spec_for(&name) {
                *counts.entry(spec.name).or_insert(0) += 1;
            }
        }
        cursor += 46 + name_len + extra_len + comment_len;
    }

    Ok(counts
        .into_iter()
        .filter(|(_, count)| *count > 1)
        .map(|(name, _)| name)
        .collect())
}

fn find_eocd(tail: &[u8]) -> Option<usize> {
    // Scan backwards for the EOCD signature at a position whose comment
    // length is consistent with the record ending at the archive tail.
    let sig = [0x50, 0x4b, 0x05, 0x06];
    (0..tail.len().saturating_sub(21)).rev().find(|&pos| {
        tail[pos..pos + 4] == sig && {
            let comment_len = u16::from_le_bytes([tail[pos + 20], tail[pos + 21]]) as usize;
            pos + 22 + comment_len == tail.len()
        }
    })
}

fn read_table(
    spec: &'static schema::FileSpec,
    bytes: &[u8],
    options: &ScanOptions,
    notices: &mut Vec<Notice>,
) -> (Option<Table>, bool) {
    let bytes = bytes.strip_prefix(b"\xef\xbb\xbf").unwrap_or(bytes);
    if bytes.is_empty() {
        notices.push(empty_file(spec.name));
        return (None, false);
    }
    // Crude quote-agnostic guard against naive delimiter floods: the CSV
    // reader allocates per-field offsets for a whole record before any
    // post-parse check can run. The threshold is deliberately slack so
    // legitimate quoted commas never trip it. This is defense-in-depth, not
    // a complete control — a record spread across quoted newlines evades
    // the per-line count and is bounded by the byte and row budgets
    // instead; the compact columnar representation planned for the rule
    // engine removes the amplification surface entirely.
    let delimiter_guard = options.max_columns.saturating_mul(4).max(4096);
    for (line_index, line) in bytes.split(|b| *b == b'\n').enumerate() {
        let delimiters = line.iter().filter(|b| **b == b',').count();
        if delimiters > delimiter_guard {
            notices.push(unreadable_file(
                spec.name,
                &format!(
                    "line {} has {delimiters} delimiters, over the {delimiter_guard} guard",
                    line_index + 1
                ),
            ));
            return (None, true);
        }
    }
    let mut reader = csv::ReaderBuilder::new()
        .has_headers(false)
        .flexible(true)
        .from_reader(bytes);
    let mut records = reader.byte_records();

    // Headers are kept verbatim: normalising them here would be silent
    // repair, and a feed other readers reject must not validate clean.
    let headers: Vec<String> = match records.next() {
        None => {
            notices.push(empty_file(spec.name));
            return (None, false);
        }
        Some(Err(error)) => {
            notices.push(csv_parsing_failed(spec.name, 1, &error));
            return (None, true);
        }
        Some(Ok(record)) => record
            .iter()
            .map(|field| String::from_utf8_lossy(field).into_owned())
            .collect(),
    };
    if headers.len() > options.max_columns {
        notices.push(unreadable_file(
            spec.name,
            &format!(
                "{} columns exceed the {}-column limit",
                headers.len(),
                options.max_columns
            ),
        ));
        return (None, true);
    }
    if headers.iter().any(|h| h.contains('\u{FFFD}')) {
        notices.push(invalid_character(spec.name, 1));
    }

    let mut seen_headers = HashSet::new();
    for header in &headers {
        if header.is_empty() {
            notices.push(
                Notice::new("empty_column_name", Severity::Warning).with("filename", spec.name),
            );
            continue;
        }
        if header.trim() != header {
            notices.push(
                Notice::new("leading_or_trailing_whitespaces", Severity::Warning)
                    .with("filename", spec.name)
                    .with("csvRowNumber", 1)
                    .with("fieldValue", header.as_str()),
            );
        }
        if !seen_headers.insert(header.clone()) {
            notices.push(
                Notice::new("duplicated_column", Severity::Error)
                    .with("filename", spec.name)
                    .with("fieldName", header.as_str()),
            );
        }
    }
    for column in spec.required_columns {
        if !headers.iter().any(|h| h == column) {
            notices.push(
                Notice::new("missing_required_column", Severity::Error)
                    .with("filename", spec.name)
                    .with("fieldName", *column),
            );
        }
    }

    let mut rows = Vec::new();
    let mut csv_row = 1u64;
    let mut truncated = false;
    // Row-level notices are sampled: past the per-file cap they are counted
    // but not retained, so millions of malformed rows cannot balloon the
    // notice list. Errors and warnings have separate quotas so a flood of
    // warnings can never crowd out error notices.
    let mut error_notices = 0u64;
    let mut warning_notices = 0u64;
    let push_sampled = |notices: &mut Vec<Notice>, counter: &mut u64, notice: Notice| {
        if *counter < options.max_notices_per_file {
            notices.push(notice);
        }
        *counter += 1;
    };
    for result in records {
        csv_row += 1;
        // The row cap bounds the retained representation and the notice
        // count, which byte budgets alone cannot (per-field overhead
        // amplifies delimiter-heavy input).
        if csv_row - 1 > options.max_rows {
            notices.push(
                Notice::new("too_many_rows", Severity::Error)
                    .with("filename", spec.name)
                    .with("rowNumber", csv_row),
            );
            truncated = true;
            break;
        }
        let record = match result {
            Err(error) => {
                // Collector model: notice the malformed record and keep
                // reading; already-parsed rows stay usable.
                let notice = csv_parsing_failed(spec.name, csv_row, &error);
                push_sampled(notices, &mut error_notices, notice);
                continue;
            }
            Ok(record) => record,
        };
        if record.len() != headers.len() {
            let notice = Notice::new("invalid_row_length", Severity::Error)
                .with("filename", spec.name)
                .with("csvRowNumber", csv_row)
                .with("rowLength", record.len())
                .with("headerCount", headers.len());
            push_sampled(notices, &mut error_notices, notice);
            continue;
        }
        let fields: Vec<String> = record
            .iter()
            .map(|field| String::from_utf8_lossy(field).into_owned())
            .collect();
        if fields.iter().all(|field| field.trim().is_empty()) {
            let notice = Notice::new("empty_row", Severity::Warning)
                .with("filename", spec.name)
                .with("csvRowNumber", csv_row);
            push_sampled(notices, &mut warning_notices, notice);
            continue;
        }
        if fields.iter().any(|field| field.contains('\u{FFFD}')) {
            let notice = invalid_character(spec.name, csv_row);
            push_sampled(notices, &mut error_notices, notice);
            continue;
        }
        rows.push(Row { csv_row, fields });
    }
    let suppressed_errors = error_notices.saturating_sub(options.max_notices_per_file);
    let suppressed_warnings = warning_notices.saturating_sub(options.max_notices_per_file);
    if suppressed_errors + suppressed_warnings > 0 {
        // The summary escalates to ERROR when error notices were dropped.
        let severity = if suppressed_errors > 0 {
            Severity::Error
        } else {
            Severity::Warning
        };
        notices.push(
            Notice::new("notice_limit_reached", severity)
                .with("filename", spec.name)
                .with("suppressedCount", suppressed_errors + suppressed_warnings),
        );
    }
    if rows.is_empty() {
        // Header-only files (or files whose every row was dropped) carry no
        // entities; a required file passing clean in that state would be a
        // false negative.
        notices.push(empty_file(spec.name));
        return (None, truncated);
    }
    (Some(Table { headers, rows }), truncated)
}

fn empty_file(filename: &'static str) -> Notice {
    Notice::new("empty_file", Severity::Error).with("filename", filename)
}

/// transitio-specific (no canonical equivalent): the entry exists but
/// cannot be safely read — corrupt member, or a violated size/column guard.
fn unreadable_file(filename: &'static str, message: &str) -> Notice {
    Notice::new("unreadable_file", Severity::Error)
        .with("filename", filename)
        .with("message", message)
}

fn invalid_character(filename: &'static str, csv_row: u64) -> Notice {
    Notice::new("invalid_character", Severity::Error)
        .with("filename", filename)
        .with("csvRowNumber", csv_row)
}

fn csv_parsing_failed(filename: &'static str, csv_row: u64, error: &csv::Error) -> Notice {
    Notice::new("csv_parsing_failed", Severity::Error)
        .with("filename", filename)
        .with("csvRowNumber", csv_row)
        .with("message", error.to_string())
}

fn feed_level_checks(
    tables: &BTreeMap<String, Table>,
    present: &HashSet<&'static str>,
    notices: &mut Vec<Notice>,
) {
    for spec in schema::FILES {
        if spec.required && !present.contains(spec.name) {
            notices.push(
                Notice::new("missing_required_file", Severity::Error).with("filename", spec.name),
            );
        }
    }
    if !present.contains("calendar.txt") && !present.contains("calendar_dates.txt") {
        notices.push(Notice::new(
            "missing_calendar_and_calendar_date_files",
            Severity::Error,
        ));
    }
    if !present.contains("feed_info.txt") {
        notices.push(
            Notice::new("missing_recommended_file", Severity::Warning)
                .with("filename", "feed_info.txt"),
        );
    }
    if let Some(feed_info) = tables.get("feed_info.txt") {
        if feed_info.rows.len() > 1 {
            notices.push(
                Notice::new("more_than_one_entity", Severity::Error)
                    .with("filename", "feed_info.txt")
                    .with("entityCount", feed_info.rows.len()),
            );
        }
    }
}

fn duplicate_key_checks(
    tables: &BTreeMap<String, Table>,
    options: &ScanOptions,
    notices: &mut Vec<Notice>,
) {
    for (name, table) in tables {
        let spec = match schema::spec_for(name) {
            Some(spec) if !spec.key_columns.is_empty() => spec,
            _ => continue,
        };
        let required: Option<Vec<usize>> = spec
            .key_columns
            .iter()
            .map(|column| table.headers.iter().position(|h| h == column))
            .collect();
        let Some(required) = required else {
            continue; // a mandatory key column is absent (e.g. agency_id)
        };
        // Optional key components resolve to the empty string when their
        // column is absent (e.g. fare_products' rider_category_id).
        let optional: Vec<Option<usize>> = schema::optional_key_columns(name)
            .iter()
            .map(|column| table.headers.iter().position(|h| h == column))
            .collect();
        let mut seen: HashMap<Vec<&str>, u64> = HashMap::new();
        let mut emitted = 0u64;
        for row in &table.rows {
            let mut key: Vec<&str> = required.iter().map(|&i| row.fields[i].as_str()).collect();
            // Rows whose required key components are all blank carry no
            // identity (e.g. optional attribution_id left empty) and are
            // exempt from the uniqueness check.
            if key.iter().all(|component| component.is_empty()) {
                continue;
            }
            key.extend(
                optional
                    .iter()
                    .map(|index| index.map_or("", |i| row.fields[i].as_str())),
            );
            match seen.entry(key) {
                Entry::Occupied(entry) => {
                    if emitted < options.max_notices_per_file {
                        let mut field_names: Vec<&str> = spec.key_columns.to_vec();
                        field_names.extend(schema::optional_key_columns(name));
                        notices.push(
                            Notice::new("duplicate_key", Severity::Error)
                                .with("filename", spec.name)
                                .with("oldCsvRowNumber", *entry.get())
                                .with("csvRowNumber", row.csv_row)
                                .with("fieldNames", field_names.join(", ")),
                        );
                    }
                    emitted += 1;
                }
                Entry::Vacant(entry) => {
                    entry.insert(row.csv_row);
                }
            }
        }
        if emitted > options.max_notices_per_file {
            // Suppressed duplicates are errors, so the summary is one too.
            notices.push(
                Notice::new("notice_limit_reached", Severity::Error)
                    .with("filename", spec.name)
                    .with("suppressedCount", emitted - options.max_notices_per_file),
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use std::io::Cursor;

    use super::*;

    fn zip_with(files: &[(&str, &[u8])]) -> Cursor<Vec<u8>> {
        let mut cursor = Cursor::new(Vec::new());
        {
            let mut writer = zip::ZipWriter::new(&mut cursor);
            let options = zip::write::SimpleFileOptions::default();
            for (name, content) in files {
                writer.start_file(*name, options).unwrap();
                std::io::Write::write_all(&mut writer, content).unwrap();
            }
            writer.finish().unwrap();
        }
        cursor.set_position(0);
        cursor
    }

    fn build_zip(files: &[(&str, &str)]) -> Cursor<Vec<u8>> {
        let bytes: Vec<(&str, &[u8])> = files
            .iter()
            .map(|(name, content)| (*name, content.as_bytes()))
            .collect();
        zip_with(&bytes)
    }

    fn minimal() -> Vec<(&'static str, &'static str)> {
        vec![
            (
                "agency.txt",
                "agency_id,agency_name,agency_url,agency_timezone\nhsl,HSL,https://hsl.fi,Europe/Helsinki\n",
            ),
            (
                "stops.txt",
                "stop_id,stop_name,stop_lat,stop_lon\ns1,Kamppi,60.169,24.931\ns2,Steissi,60.171,24.941\n",
            ),
            (
                "routes.txt",
                "route_id,agency_id,route_short_name,route_type\nr1,hsl,1,3\n",
            ),
            ("trips.txt", "route_id,service_id,trip_id\nr1,wk,t1\n"),
            (
                "stop_times.txt",
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nt1,08:00:00,08:00:00,s1,1\nt1,08:05:00,08:05:00,s2,2\n",
            ),
            (
                "calendar.txt",
                "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\nwk,1,1,1,1,1,0,0,20260101,20261231\n",
            ),
        ]
    }

    fn codes(result: &ScanResult) -> Vec<&'static str> {
        result.notices.iter().map(|n| n.code).collect()
    }

    #[test]
    fn minimal_feed_has_no_errors() {
        let result = scan_reader(build_zip(&minimal())).unwrap();
        let errors: Vec<_> = result
            .notices
            .iter()
            .filter(|n| n.severity == Severity::Error)
            .collect();
        assert!(errors.is_empty(), "unexpected errors: {errors:?}");
        assert_eq!(result.tables["stop_times.txt"].rows.len(), 2);
        assert!(codes(&result).contains(&"missing_recommended_file"));
    }

    #[test]
    fn missing_required_files_are_noticed() {
        let files: Vec<_> = minimal()
            .into_iter()
            .filter(|(name, _)| *name != "stops.txt" && *name != "calendar.txt")
            .collect();
        let result = scan_reader(build_zip(&files)).unwrap();
        assert!(codes(&result).contains(&"missing_required_file"));
        assert!(codes(&result).contains(&"missing_calendar_and_calendar_date_files"));
    }

    #[test]
    fn duplicate_keys_are_noticed_with_row_numbers() {
        let mut files = minimal();
        files.retain(|(name, _)| *name != "trips.txt");
        files.push((
            "trips.txt",
            "route_id,service_id,trip_id\nr1,wk,t1\nr1,wk,t1\n",
        ));
        let result = scan_reader(build_zip(&files)).unwrap();
        let dup = result
            .notices
            .iter()
            .find(|n| n.code == "duplicate_key")
            .expect("duplicate_key notice");
        assert_eq!(dup.context["filename"], "trips.txt");
        assert_eq!(dup.context["oldCsvRowNumber"], 2);
        assert_eq!(dup.context["csvRowNumber"], 3);
    }

    #[test]
    fn composite_key_with_optional_components() {
        let mut files = minimal();
        // Optional key columns absent: equal fare_product_id rows collide.
        files.push((
            "fare_products.txt",
            "fare_product_id,amount,currency\nsingle,3.20,EUR\nsingle,4.10,EUR\n",
        ));
        let result = scan_reader(build_zip(&files)).unwrap();
        assert!(codes(&result).contains(&"duplicate_key"));

        let mut files = minimal();
        // Distinct optional component: no collision.
        files.push((
            "fare_products.txt",
            "fare_product_id,rider_category_id,amount,currency\nsingle,adult,3.20,EUR\nsingle,child,1.60,EUR\n",
        ));
        let result = scan_reader(build_zip(&files)).unwrap();
        assert!(!codes(&result).contains(&"duplicate_key"));

        let mut files = minimal();
        // fare_rules: fare_id plus optional selectors form the key.
        files.push(("fare_rules.txt", "fare_id,route_id\nf1,r1\nf1,r1\n"));
        let result = scan_reader(build_zip(&files)).unwrap();
        assert!(codes(&result).contains(&"duplicate_key"));

        let mut files = minimal();
        files.push(("fare_rules.txt", "fare_id,route_id\nf1,r1\nf1,r2\n"));
        let result = scan_reader(build_zip(&files)).unwrap();
        assert!(!codes(&result).contains(&"duplicate_key"));
    }

    #[test]
    fn blank_optional_keys_are_not_duplicates() {
        let mut files = minimal();
        files.push((
            "attributions.txt",
            "attribution_id,organization_name\n,Org A\n,Org B\nx1,Org C\nx1,Org D\n",
        ));
        let result = scan_reader(build_zip(&files)).unwrap();
        let dup: Vec<_> = result
            .notices
            .iter()
            .filter(|n| n.code == "duplicate_key")
            .collect();
        // The two blank IDs are exempt; the two x1 rows collide.
        assert_eq!(dup.len(), 1);
        assert_eq!(dup[0].context["csvRowNumber"], 5);
    }

    #[test]
    fn multi_row_feed_info_is_an_error() {
        let mut files = minimal();
        files.push((
            "feed_info.txt",
            "feed_publisher_name,feed_publisher_url,feed_lang\nA,https://a,fi\nB,https://b,sv\n",
        ));
        let result = scan_reader(build_zip(&files)).unwrap();
        let notice = result
            .notices
            .iter()
            .find(|n| n.code == "more_than_one_entity")
            .expect("more_than_one_entity");
        assert_eq!(notice.severity, Severity::Error);
    }

    #[test]
    fn locations_geojson_is_recognized() {
        let mut files = minimal();
        files.push(("locations.geojson", "{\"type\":\"FeatureCollection\"}"));
        let result = scan_reader(build_zip(&files)).unwrap();
        assert!(!codes(&result).contains(&"unknown_file"));
    }

    #[test]
    fn row_notices_are_sampled_past_the_cap() {
        let options = ScanOptions {
            max_notices_per_file: 2,
            ..ScanOptions::default()
        };
        let mut files = minimal();
        files.retain(|(name, _)| *name != "shapes.txt");
        files.push((
            "shapes.txt",
            "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n,,,\n,,,\n,,,\n,,,\nsh1,60.1,24.9,1\n",
        ));
        let result = scan_reader_with(build_zip(&files), options).unwrap();
        let empty_rows = result
            .notices
            .iter()
            .filter(|n| n.code == "empty_row")
            .count();
        assert_eq!(empty_rows, 2);
        let capped = result
            .notices
            .iter()
            .find(|n| n.code == "notice_limit_reached")
            .expect("notice_limit_reached");
        assert_eq!(capped.context["suppressedCount"], 2);
    }

    #[test]
    fn delimiter_heavy_rows_are_refused() {
        let hostile = format!("stop_id,stop_name\ns1,Kamppi\n{}\n", ",".repeat(5000));
        let mut files = minimal();
        files.retain(|(name, _)| *name != "stops.txt");
        let hostile_files: Vec<(&str, &[u8])> = files
            .iter()
            .map(|(name, content)| (*name, content.as_bytes()))
            .chain(std::iter::once(("stops.txt", hostile.as_bytes())))
            .collect();
        let result = scan_reader(zip_with(&hostile_files)).unwrap();
        assert!(codes(&result).contains(&"unreadable_file"));
        assert!(!result.tables.contains_key("stops.txt"));
    }

    #[test]
    fn excessive_entry_counts_are_refused() {
        let mut cursor = Cursor::new(Vec::new());
        {
            let mut writer = zip::ZipWriter::new(&mut cursor);
            let options = zip::write::SimpleFileOptions::default();
            for index in 0..4100 {
                writer
                    .start_file(format!("junk-{index}.bin"), options)
                    .unwrap();
            }
            writer.finish().unwrap();
        }
        cursor.set_position(0);
        let error = match scan_reader(cursor) {
            Err(error) => error,
            Ok(_) => panic!("expected the entry-count limit to refuse the archive"),
        };
        assert!(error.contains("entry limit"), "got: {error}");
    }

    #[test]
    fn header_and_row_shape_notices() {
        let mut files = minimal();
        files.retain(|(name, _)| *name != "routes.txt");
        files.push((
            "routes.txt",
            "route_id,route_id,,route_short_name\nr1,r1,x,1,EXTRA\n,,,\n",
        ));
        let result = scan_reader(build_zip(&files)).unwrap();
        let codes = codes(&result);
        assert!(codes.contains(&"duplicated_column"));
        assert!(codes.contains(&"empty_column_name"));
        assert!(codes.contains(&"missing_required_column")); // route_type
        assert!(codes.contains(&"invalid_row_length"));
        assert!(codes.contains(&"empty_row"));
        // every routes row was dropped, so the file carries no entities
        assert!(codes.contains(&"empty_file"));
    }

    #[test]
    fn padded_headers_are_not_silently_repaired() {
        let mut files = minimal();
        files.retain(|(name, _)| *name != "trips.txt");
        files.push(("trips.txt", " route_id,service_id,trip_id\nr1,wk,t1\n"));
        let result = scan_reader(build_zip(&files)).unwrap();
        let codes = codes(&result);
        assert!(codes.contains(&"leading_or_trailing_whitespaces"));
        assert!(codes.contains(&"missing_required_column")); // exact "route_id" absent
    }

    #[test]
    fn header_only_file_is_empty() {
        let mut files = minimal();
        files.retain(|(name, _)| *name != "stops.txt");
        files.push(("stops.txt", "stop_id,stop_name,stop_lat,stop_lon\n"));
        let result = scan_reader(build_zip(&files)).unwrap();
        assert!(codes(&result).contains(&"empty_file"));
        assert!(!codes(&result).contains(&"missing_required_file"));
        assert!(!result.tables.contains_key("stops.txt"));
    }

    #[test]
    fn unknown_and_nested_files() {
        let mut files = minimal();
        files.push(("notes.txt", "hello\n"));
        files.push(("nested/agency.txt", "agency_name\nX\n"));
        files.push(("__MACOSX/._agency.txt", "junk"));
        let result = scan_reader(build_zip(&files)).unwrap();
        let codes = codes(&result);
        assert!(codes.contains(&"unknown_file"));
        assert!(codes.contains(&"invalid_input_files_in_subfolder"));
    }

    #[test]
    fn undecodable_rows_are_skipped_not_fatal() {
        let mut files: Vec<(&str, &[u8])> = minimal()
            .into_iter()
            .filter(|(name, _)| *name != "stops.txt")
            .map(|(name, content)| (name, content.as_bytes()))
            .collect();
        files.push((
            "stops.txt",
            b"stop_id,stop_name\ns1,Kamppi\ns2,\xff\xfe\ns3,Steissi\n",
        ));
        files.push(("shapes.txt", b""));
        let result = scan_reader(zip_with(&files)).unwrap();
        let codes = codes(&result);
        assert!(codes.contains(&"invalid_character"));
        assert!(codes.contains(&"empty_file")); // shapes.txt
        assert!(!codes.contains(&"missing_required_file"));
        // the two clean stop rows survive
        assert_eq!(result.tables["stops.txt"].rows.len(), 2);
    }

    #[test]
    fn entry_size_budget_is_noticed_per_file() {
        let options = ScanOptions {
            max_entry_bytes: 64,
            max_total_bytes: 1024,
            ..ScanOptions::default()
        };
        let result = scan_reader_with(build_zip(&minimal()), options).unwrap();
        assert!(codes(&result).contains(&"unreadable_file"));
        // Oversized entries are skipped, the rest still validates.
        assert!(result.tables.contains_key("trips.txt"));
        assert!(!result.tables.contains_key("stop_times.txt"));
    }

    #[test]
    fn cumulative_budget_is_noticed_while_reading() {
        // Each file fits alone, but the archive exceeds the total budget.
        let options = ScanOptions {
            max_entry_bytes: 512,
            max_total_bytes: 300,
            ..ScanOptions::default()
        };
        let result = scan_reader_with(build_zip(&minimal()), options).unwrap();
        assert!(codes(&result).contains(&"unreadable_file"));
        assert!(result.tables.len() < 6);
    }

    #[test]
    fn unlimited_sentinel_budgets_do_not_overflow() {
        let options = ScanOptions {
            max_entry_bytes: u64::MAX,
            max_total_bytes: u64::MAX,
            max_rows: u64::MAX,
            ..ScanOptions::default()
        };
        let result = scan_reader_with(build_zip(&minimal()), options).unwrap();
        let errors: Vec<_> = result
            .notices
            .iter()
            .filter(|n| n.severity == Severity::Error)
            .collect();
        assert!(errors.is_empty(), "unexpected errors: {errors:?}");
        assert_eq!(result.tables["stop_times.txt"].rows.len(), 2);
    }

    #[test]
    fn too_many_rows_caps_retention() {
        let options = ScanOptions {
            max_rows: 1,
            ..ScanOptions::default()
        };
        let result = scan_reader_with(build_zip(&minimal()), options).unwrap();
        assert!(codes(&result).contains(&"too_many_rows"));
        // The first row is kept; reading stops at the cap.
        assert_eq!(result.tables["stop_times.txt"].rows.len(), 1);
    }

    #[test]
    fn not_a_zip_is_an_error() {
        assert!(scan_reader(Cursor::new(b"plain text".to_vec())).is_err());
    }
}
