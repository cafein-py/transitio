use std::collections::hash_map::Entry;
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;

use crate::notice::{Notice, Severity};
use crate::rules::clip;
use crate::schema;

/// Default guards against hostile archives (zip bombs, amplification). Byte
/// budgets bound the decompressed input; the row and column caps bound the
/// retained parsed representation and the notice count, whose per-field
/// overhead can amplify small inputs. Worst-case transient memory is a small
/// multiple of `max_entry_bytes` — lower the limits for untrusted input,
/// raise them for oversized trusted feeds. `u64::MAX` disables a byte or
/// row limit. A violated limit is reported per file (`unreadable_file` /
/// `too_many_rows`) and the scan continues; only an untraversable archive
/// aborts with an error. One entry may use the whole total budget: a large
/// city cropped from a national feed keeps a `stop_times.txt` over 1 GiB.
pub const DEFAULT_MAX_ENTRY_BYTES: u64 = 2 * 1024 * 1024 * 1024;
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

/// Bytes after the end-of-central-directory record that the archive-tail
/// read has room for even behind the longest comment; some published feeds
/// carry a few.
const TRAILING_BYTES_ROOM: u64 = 64 * 1024;

/// Recognized non-CSV GTFS files: not unknown, content out of the
/// structural tier's scope.
const NON_CSV_FILES: &[&str] = &["locations.geojson"];

/// An explicit validation target: a service day, optionally with a
/// wall-clock time of day in seconds. Unlike `reference_date` it is never
/// defaulted — the date-targeted checks run only when the caller asked.
#[derive(Clone, Copy)]
pub struct Moment {
    pub date: chrono::NaiveDate,
    pub time: Option<u32>,
}

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
    /// Target for the date(-time)-targeted service checks.
    pub moment: Option<Moment>,
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
            moment: None,
        }
    }
}

/// One data row with its 1-based CSV row number (header row is 1).
pub struct Row {
    pub csv_row: u64,
    pub fields: Vec<String>,
}

/// One parsed file: headers plus the rows that survived the structural
/// checks, names and values trimmed of surrounding whitespace; malformed
/// and empty rows are noticed and skipped. Bytes that are not UTF-8 read
/// as U+FFFD, and a row holding U+FFFD is kept with one
/// `invalid_character` notice per such field.
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
    /// Cafein-readiness summary; None until the readiness tier runs.
    pub readiness: Option<crate::readiness::Readiness>,
    /// Moment measurement block; None without an explicit reference
    /// date or under unreliable inputs.
    pub moment: Option<crate::semantics::MomentSummary>,
}

pub fn scan(path: &Path) -> Result<ScanResult, String> {
    scan_with(path, ScanOptions::default())
}

pub fn scan_with(path: &Path, options: ScanOptions) -> Result<ScanResult, String> {
    scan_streaming(path, options, &[])
}

/// `scan_with`, leaving the named GTFS files for the caller to stream: they
/// are read for nothing here, so they appear in neither `tables` nor
/// `unparsed_entries` and are not charged against the byte budgets, while
/// still counting as present for the feed-level checks.
pub fn scan_streaming(
    path: &Path,
    options: ScanOptions,
    streamed: &[&str],
) -> Result<ScanResult, String> {
    let file = File::open(path).map_err(|e| format!("cannot open {}: {e}", path.display()))?;
    scan_reader_streaming(file, options, streamed)
}

pub fn scan_reader<R: Read + Seek>(reader: R) -> Result<ScanResult, String> {
    scan_reader_with(reader, ScanOptions::default())
}

pub fn scan_reader_with<R: Read + Seek>(
    reader: R,
    options: ScanOptions,
) -> Result<ScanResult, String> {
    scan_reader_streaming(reader, options, &[])
}

pub fn scan_reader_streaming<R: Read + Seek>(
    mut reader: R,
    options: ScanOptions,
    streamed: &[&str],
) -> Result<ScanResult, String> {
    // The zip crate's read index is keyed by name and silently keeps only
    // the last occurrence of a duplicated entry, so shadowed duplicates are
    // invisible through its API. GTFS files duplicated in the archive are
    // ambiguous (other readers may take the first occurrence); walk the
    // central directory directly to detect and refuse them.
    let (duplicated, archive_end) =
        duplicated_gtfs_entries(&mut reader).map_err(|e| format!("not a readable zip: {e}"))?;
    reader
        .seek(SeekFrom::Start(0))
        .map_err(|e| format!("cannot rewind archive: {e}"))?;

    // Bytes after the end-of-central-directory record are hidden, so the
    // zip crate reads the archive the walker checked.
    let bounded = Bounded {
        inner: reader,
        len: archive_end,
        pos: 0,
    };
    let mut archive =
        zip::ZipArchive::new(bounded).map_err(|e| format!("not a readable zip: {e}"))?;
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
        if streamed.contains(&spec.name) {
            present.insert(spec.name);
            continue; // the caller streams it
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
        let remaining = options.max_total_bytes.saturating_sub(total_bytes);
        let budget = options.max_entry_bytes.min(remaining);
        // Every budget a size is over, since raising one alone may not do.
        let over = |size: u64| {
            let mut budgets = Vec::new();
            if size > options.max_entry_bytes {
                budgets.push("max_entry_bytes");
            }
            if size > remaining {
                budgets.push("max_total_bytes");
            }
            budgets
        };
        if entry.size() > budget {
            notices.push(
                unreadable_file(
                    spec.name,
                    &format!(
                        "declares {} uncompressed bytes, over the {budget}-byte budget",
                        entry.size()
                    ),
                )
                .with("budgets", over(entry.size())),
            );
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
            notices.push(
                unreadable_file(spec.name, &format!("exceeds the {budget}-byte budget"))
                    .with("budgets", over(bytes.len() as u64)),
            );
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
        readiness: None,
        moment: None,
    })
}

/// Why a feed cannot be taken whole to `verb` it, or None when it can,
/// from the notices of its scan and the files it left incomplete: each
/// file cut short, with every budget it exceeded and that budget's value
/// ("stops.txt exceeds max_rows (5)", or for the delimiter guard the line
/// and the guard `max_columns` sets), and with `notice_caps` each file
/// whose notices were sampled.
pub fn refusal<'a>(
    notices: &[Notice],
    incomplete: impl IntoIterator<Item = &'a str>,
    options: &ScanOptions,
    notice_caps: bool,
    verb: &str,
) -> Option<String> {
    let mut reasons = BTreeSet::new();
    let mut budgets = BTreeSet::new();
    let mut explained = HashSet::new();
    let mut raisable = true;
    for notice in notices {
        let Some(file) = notice.context.get("filename").and_then(|v| v.as_str()) else {
            continue;
        };
        let exceeded: Vec<&str> = match notice.code {
            "too_many_rows" => vec!["max_rows"],
            "unreadable_file" => notice
                .context
                .get("budgets")
                .and_then(|v| v.as_array())
                .map(|names| names.iter().filter_map(|n| n.as_str()).collect())
                .unwrap_or_default(),
            "notice_limit_reached" if notice_caps => {
                if notice.context.contains_key("blockId") {
                    reasons.insert(format!(
                        "{file} reaches the block overlap check cap that no budget raises"
                    ));
                    raisable = false;
                    continue;
                }
                vec!["max_notices_per_file"]
            }
            _ => continue,
        };
        for budget in exceeded {
            let value = match budget {
                "max_entry_bytes" => options.max_entry_bytes,
                "max_total_bytes" => options.max_total_bytes,
                "max_rows" => options.max_rows,
                "max_columns" => options.max_columns as u64,
                "max_notices_per_file" => options.max_notices_per_file,
                _ => continue,
            };
            let guard = notice.context.get("delimiterGuard");
            let line = notice.context.get("lineNumber");
            reasons.insert(match (budget, guard, line) {
                ("max_columns", Some(guard), Some(line)) => format!(
                    "{file} line {line} has more than {guard} delimiters outside quotes, \
                     the guard set by {budget} ({value})"
                ),
                _ => format!("{file} exceeds {budget} ({value})"),
            });
            budgets.insert(budget);
            if notice.code != "notice_limit_reached" {
                explained.insert(file);
            }
        }
    }
    for file in incomplete {
        if explained.contains(file) {
            continue;
        }
        // A duplicated, corrupt or header-unparseable entry, or a guard no
        // budget raises: nothing reads it whole.
        let cause = notices.iter().rev().find(|n| {
            matches!(
                n.code,
                "unreadable_file" | "csv_parsing_failed" | "duplicate_zip_entry"
            ) && n.context.get("filename").and_then(|v| v.as_str()) == Some(file)
        });
        let detail = cause.map_or("unreadable", |n| {
            n.context
                .get("message")
                .and_then(|v| v.as_str())
                .unwrap_or(n.code)
        });
        reasons.insert(format!("{file} cannot be read whole: {detail}"));
        raisable = false;
    }
    if reasons.is_empty() {
        return None;
    }
    let list = reasons.into_iter().collect::<Vec<_>>().join(", ");
    Some(if !raisable {
        format!("{list}; cannot {verb} this feed")
    } else if budgets.len() > 1 {
        format!("{list}; raise them to {verb} this feed")
    } else {
        format!("{list}; raise it to {verb} this feed")
    })
}

/// Walk the central directory and return the root-level GTFS filenames that
/// occur more than once, with the offset where the archive ends. The
/// end-of-central-directory record is located by its signature in the
/// archive tail, where trailing bytes may follow it; ZIP64 archives are
/// followed through the ZIP64 locator.
fn duplicated_gtfs_entries<R: Read + Seek>(
    reader: &mut R,
) -> Result<(BTreeSet<&'static str>, u64), String> {
    let file_len = reader
        .seek(SeekFrom::End(0))
        .map_err(|e| format!("cannot read archive length: {e}"))?;
    // EOCD is 22 bytes plus a comment of at most 65535 bytes; the ZIP64
    // locator (20 bytes) sits directly before the EOCD when present, and
    // trailing bytes may follow it.
    let tail_len = file_len.min(20 + 22 + 65_535 + TRAILING_BYTES_ROOM);
    let tail_start = file_len - tail_len;
    reader
        .seek(SeekFrom::Start(tail_start))
        .map_err(|e| format!("cannot seek archive tail: {e}"))?;
    let mut tail = vec![0u8; tail_len as usize];
    reader
        .read_exact(&mut tail)
        .map_err(|e| format!("cannot read archive tail: {e}"))?;

    let (record, (total_entries, cd_size, cd_offset)) =
        find_eocd(reader, &tail, tail_start)?.ok_or("no end-of-central-directory record found")?;
    let archive_end = tail_start + record.end as u64;
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

    let duplicated = counts
        .into_iter()
        .filter(|(_, count)| *count > 1)
        .map(|(name, _)| name)
        .collect();
    Ok((duplicated, archive_end))
}

/// A central directory's entry count, size and offset.
type Directory = (u64, u64, u64);

/// The span within `tail` of the last EOCD record that fits in it and whose
/// central directory checks out, with that directory. `tail_start` is the
/// tail's offset in the archive. A stray signature inside entry data or
/// trailing bytes fails the check.
fn find_eocd<R: Read + Seek>(
    reader: &mut R,
    tail: &[u8],
    tail_start: u64,
) -> Result<Option<(std::ops::Range<usize>, Directory)>, String> {
    let sig = [0x50, 0x4b, 0x05, 0x06];
    for pos in (0..tail.len().saturating_sub(21)).rev() {
        if tail[pos..pos + 4] != sig {
            continue;
        }
        let comment_len = u16::from_le_bytes([tail[pos + 20], tail[pos + 21]]) as usize;
        let record = pos..pos + 22 + comment_len;
        if record.end > tail.len() {
            continue;
        }
        if let Some(directory) = central_directory(reader, tail, tail_start, pos)? {
            return Ok(Some((record, directory)));
        }
    }
    Ok(None)
}

/// The central directory described by the EOCD record at `pos` in `tail`,
/// or None unless the directory ends where the record starts. A ZIP64
/// record is followed through the locator directly before the EOCD, and
/// the directory must end where the ZIP64 record starts.
fn central_directory<R: Read + Seek>(
    reader: &mut R,
    tail: &[u8],
    tail_start: u64,
    pos: usize,
) -> Result<Option<Directory>, String> {
    let eocd = &tail[pos..pos + 22];
    let entries = u16::from_le_bytes([eocd[10], eocd[11]]) as u64;
    let cd_size = u32::from_le_bytes([eocd[12], eocd[13], eocd[14], eocd[15]]) as u64;
    let cd_offset = u32::from_le_bytes([eocd[16], eocd[17], eocd[18], eocd[19]]) as u64;
    if entries != 0xFFFF && cd_size != 0xFFFF_FFFF && cd_offset != 0xFFFF_FFFF {
        let consistent = cd_offset + cd_size == tail_start + pos as u64;
        return Ok(consistent.then_some((entries, cd_size, cd_offset)));
    }

    let Some(locator_pos) = pos.checked_sub(20) else {
        return Ok(None);
    };
    let locator = &tail[locator_pos..pos];
    if locator[0..4] != [0x50, 0x4b, 0x06, 0x07] {
        return Ok(None);
    }
    // The 56-byte ZIP64 record must lie before its locator.
    let zip64_offset = u64::from_le_bytes(locator[8..16].try_into().unwrap());
    let locator_offset = tail_start + locator_pos as u64;
    if locator_offset < 56 || zip64_offset > locator_offset - 56 {
        return Ok(None);
    }
    reader
        .seek(SeekFrom::Start(zip64_offset))
        .map_err(|e| format!("cannot seek ZIP64 record: {e}"))?;
    let mut zip64 = [0u8; 56];
    reader
        .read_exact(&mut zip64)
        .map_err(|e| format!("cannot read ZIP64 record: {e}"))?;
    if zip64[0..4] != [0x50, 0x4b, 0x06, 0x06] {
        return Ok(None);
    }
    let entries = u64::from_le_bytes(zip64[32..40].try_into().unwrap());
    let cd_size = u64::from_le_bytes(zip64[40..48].try_into().unwrap());
    let cd_offset = u64::from_le_bytes(zip64[48..56].try_into().unwrap());
    let consistent = cd_offset.checked_add(cd_size) == Some(zip64_offset);
    Ok(consistent.then_some((entries, cd_size, cd_offset)))
}

/// A reader over the first `len` bytes of `inner`, whose position it
/// tracks as `pos`.
struct Bounded<R> {
    inner: R,
    len: u64,
    pos: u64,
}

impl<R: Read> Read for Bounded<R> {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        let limit = self.len.saturating_sub(self.pos).min(buf.len() as u64) as usize;
        let read = self.inner.read(&mut buf[..limit])?;
        self.pos += read as u64;
        Ok(read)
    }
}

impl<R: Seek> Seek for Bounded<R> {
    fn seek(&mut self, pos: SeekFrom) -> std::io::Result<u64> {
        let target = match pos {
            SeekFrom::Start(offset) => Some(offset),
            SeekFrom::End(delta) => self.len.checked_add_signed(delta),
            SeekFrom::Current(delta) => self.pos.checked_add_signed(delta),
        }
        .ok_or_else(|| {
            std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "invalid seek to a negative or overflowing position",
            )
        })?;
        self.pos = self.inner.seek(SeekFrom::Start(target))?;
        Ok(self.pos)
    }
}

fn read_table(
    spec: &'static schema::FileSpec,
    bytes: &[u8],
    options: &ScanOptions,
    notices: &mut Vec<Notice>,
) -> (Option<Table>, bool) {
    if bytes.is_empty() {
        notices.push(empty_file(spec.name));
        return (None, false);
    }
    // The entry budget bounds a record held in memory, so only the
    // delimiter count is guarded.
    let guarded = DelimiterGuard::new(bytes, options, u64::MAX);
    let mut reader = match TableReader::open(spec, guarded, options, options.max_rows, notices) {
        Ok(reader) => reader,
        Err(NoTable::Empty) => return (None, false),
        Err(NoTable::Unreadable) => return (None, true),
    };
    let mut rows = Vec::new();
    while let Some(row) = reader.next_row(notices) {
        rows.push(row);
    }
    let truncated = reader.truncated();
    if rows.is_empty() {
        // Header-only files (or files whose every row was dropped) carry no
        // entities; a required file passing clean in that state would be a
        // false negative. A file cut short is noticed as such instead.
        if !truncated {
            notices.push(empty_file(spec.name));
        }
        return (None, truncated);
    }
    (
        Some(Table {
            headers: reader.into_headers(),
            rows,
        }),
        truncated,
    )
}

/// Why a stream yields no table: nothing to read, or input the caller
/// marks incomplete. The notices are already recorded either way.
pub enum NoTable {
    Empty,
    Unreadable,
}

/// One GTFS file's rows streamed from any reader under the structural
/// checks of the scan, so a pass over a file far larger than memory keeps
/// only what it takes from each row. `read_table` collects it into a
/// `Table`; header checks, row checks and the sampled row-level notices
/// are the same either way. Header names and values are trimmed of
/// surrounding whitespace, and one notice per file says so.
pub struct TableReader<R: Read> {
    spec: &'static schema::FileSpec,
    records: csv::ByteRecordsIntoIter<std::io::Chain<std::io::Cursor<Vec<u8>>, R>>,
    headers: Vec<String>,
    csv_row: u64,
    max_rows: u64,
    max_notices: u64,
    error_notices: u64,
    warning_notices: u64,
    trimmed: Trimmed,
    truncated: bool,
    finished: bool,
}

/// What a reader trimmed: how many header names and values, and the
/// first as (csv row, field name, text as written), clipped.
#[derive(Default)]
struct Trimmed {
    count: u64,
    first: Option<(u64, String, String)>,
}

impl Trimmed {
    fn note(&mut self, count: u64, csv_row: u64, field: &str, written: &str) {
        self.count += count;
        if self.first.is_none() {
            self.first = Some((csv_row, clip(field), clip(written)));
        }
    }
}

/// Python's `str.isspace`: Unicode whitespace plus the separators
/// U+001C to U+001F, so a value trims here as `str.strip()` trims it.
fn is_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

impl<R: Read> TableReader<R> {
    /// Read and check the header row. `max_rows` caps the data rows
    /// (`u64::MAX` for none); a leading UTF-8 byte-order mark is skipped.
    pub fn open(
        spec: &'static schema::FileSpec,
        mut reader: R,
        options: &ScanOptions,
        max_rows: u64,
        notices: &mut Vec<Notice>,
    ) -> Result<Self, NoTable> {
        let mut prefix = vec![0u8; 3];
        let mut filled = 0;
        while filled < prefix.len() {
            match reader.read(&mut prefix[filled..]) {
                Ok(0) => break,
                Ok(n) => filled += n,
                Err(error) => {
                    notices.push(unreadable_file(spec.name, &error.to_string()));
                    return Err(NoTable::Unreadable);
                }
            }
        }
        prefix.truncate(filled);
        if prefix.is_empty() {
            notices.push(empty_file(spec.name));
            return Err(NoTable::Empty);
        }
        if prefix == b"\xef\xbb\xbf" {
            prefix.clear();
        }
        let mut records = csv::ReaderBuilder::new()
            .has_headers(false)
            .flexible(true)
            .from_reader(std::io::Cursor::new(prefix).chain(reader))
            .into_byte_records();

        let written: Vec<String> = match records.next() {
            None => {
                notices.push(empty_file(spec.name));
                return Err(NoTable::Empty);
            }
            Some(Err(error)) if error.is_io_error() => {
                notices.push(read_failure(spec.name, &error));
                return Err(NoTable::Unreadable);
            }
            Some(Err(error)) => {
                notices.push(csv_parsing_failed(spec.name, 1, &error));
                return Err(NoTable::Unreadable);
            }
            Some(Ok(record)) => record
                .iter()
                .map(|field| String::from_utf8_lossy(field).into_owned())
                .collect(),
        };
        if written.len() > options.max_columns {
            notices.push(
                unreadable_file(
                    spec.name,
                    &format!(
                        "{} columns exceed the {}-column limit",
                        written.len(),
                        options.max_columns
                    ),
                )
                .with("budgets", vec!["max_columns"]),
            );
            return Err(NoTable::Unreadable);
        }
        let mut trimmed = Trimmed::default();
        let headers: Vec<String> = written
            .iter()
            .map(|raw| {
                let name = raw.trim_matches(is_space);
                if name.len() != raw.len() {
                    trimmed.note(1, 1, name, raw);
                }
                name.to_string()
            })
            .collect();
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
        Ok(TableReader {
            spec,
            records,
            headers,
            csv_row: 1,
            max_rows,
            max_notices: options.max_notices_per_file,
            error_notices: 0,
            warning_notices: 0,
            trimmed,
            truncated: false,
            finished: false,
        })
    }

    pub fn headers(&self) -> &[String] {
        &self.headers
    }

    pub fn into_headers(self) -> Vec<String> {
        self.headers
    }

    /// Whether the rows stopped short of the file: the row cap, or a read
    /// failure part-way through.
    pub fn truncated(&self) -> bool {
        self.truncated
    }

    /// The next row that passes the structural checks; malformed rows are
    /// noticed and skipped, and a field holding U+FFFD is noticed as
    /// `invalid_character` with its row kept. Ends at the row cap with
    /// `too_many_rows`, and at a read failure with `unreadable_file`; the
    /// suppressed-notice summary is recorded once when the rows end.
    pub fn next_row(&mut self, notices: &mut Vec<Notice>) -> Option<Row> {
        if self.finished {
            return None;
        }
        // Row-level notices are sampled: past the per-file cap they are
        // counted but not retained, so millions of malformed rows cannot
        // balloon the notice list. Errors and warnings have separate quotas
        // so a flood of warnings can never crowd out error notices.
        let max_notices = self.max_notices;
        let push_sampled = |notices: &mut Vec<Notice>, counter: &mut u64, notice: Notice| {
            if *counter < max_notices {
                notices.push(notice);
            }
            *counter += 1;
        };
        loop {
            let Some(result) = self.records.next() else {
                self.finish(notices);
                return None;
            };
            if let Err(error) = &result {
                if error.is_io_error() {
                    // The source itself failed; retrying would fail again,
                    // and the failure is not a row.
                    notices.push(read_failure(self.spec.name, error));
                    self.truncated = true;
                    self.finish(notices);
                    return None;
                }
            }
            self.csv_row += 1;
            // The row cap bounds the retained representation and the notice
            // count, which byte budgets alone cannot (per-field overhead
            // amplifies delimiter-heavy input).
            if self.csv_row - 1 > self.max_rows {
                notices.push(
                    Notice::new("too_many_rows", Severity::Error)
                        .with("filename", self.spec.name)
                        .with("rowNumber", self.csv_row),
                );
                self.truncated = true;
                self.finish(notices);
                return None;
            }
            let record = match result {
                Err(error) => {
                    // Collector model: notice the malformed record and keep
                    // reading; already-parsed rows stay usable.
                    let notice = csv_parsing_failed(self.spec.name, self.csv_row, &error);
                    push_sampled(notices, &mut self.error_notices, notice);
                    continue;
                }
                Ok(record) => record,
            };
            if record.len() != self.headers.len() {
                let notice = Notice::new("invalid_row_length", Severity::Error)
                    .with("filename", self.spec.name)
                    .with("csvRowNumber", self.csv_row)
                    .with("rowLength", record.len())
                    .with("headerCount", self.headers.len());
                push_sampled(notices, &mut self.error_notices, notice);
                continue;
            }
            let mut trimmed = 0;
            let mut first = None;
            let fields: Vec<String> = record
                .iter()
                .enumerate()
                .map(|(index, field)| {
                    let written = String::from_utf8_lossy(field);
                    let value = written.trim_matches(is_space);
                    if value.len() != written.len() {
                        trimmed += 1;
                        first.get_or_insert(index);
                    }
                    value.to_string()
                })
                .collect();
            if let Some(index) = first {
                let written = String::from_utf8_lossy(&record[index]);
                self.trimmed
                    .note(trimmed, self.csv_row, &self.headers[index], &written);
            }
            if fields.iter().all(|field| field.is_empty()) {
                let notice = Notice::new("empty_row", Severity::Warning)
                    .with("filename", self.spec.name)
                    .with("csvRowNumber", self.csv_row);
                push_sampled(notices, &mut self.warning_notices, notice);
                continue;
            }
            for (header, field) in self.headers.iter().zip(&fields) {
                if field.contains('\u{FFFD}') {
                    let notice = invalid_character(self.spec.name, self.csv_row)
                        .with("fieldName", clip(header));
                    push_sampled(notices, &mut self.error_notices, notice);
                }
            }
            return Some(Row {
                csv_row: self.csv_row,
                fields,
            });
        }
    }

    fn finish(&mut self, notices: &mut Vec<Notice>) {
        self.finished = true;
        if let Some((csv_row, field, written)) = self.trimmed.first.take() {
            notices.push(
                Notice::new("leading_or_trailing_whitespaces", Severity::Warning)
                    .with("filename", self.spec.name)
                    .with("csvRowNumber", csv_row)
                    .with("fieldName", field)
                    .with("fieldValue", written)
                    .with("trimmedCount", self.trimmed.count),
            );
        }
        let suppressed_errors = self.error_notices.saturating_sub(self.max_notices);
        let suppressed_warnings = self.warning_notices.saturating_sub(self.max_notices);
        if suppressed_errors + suppressed_warnings > 0 {
            // The summary escalates to ERROR when error notices were dropped.
            let severity = if suppressed_errors > 0 {
                Severity::Error
            } else {
                Severity::Warning
            };
            notices.push(
                Notice::new("notice_limit_reached", severity)
                    .with("filename", self.spec.name)
                    .with("suppressedCount", suppressed_errors + suppressed_warnings),
            );
        }
    }
}

/// The longest logical CSV record a stream may carry; anything beyond it
/// is a flood, not a GTFS row.
pub const MAX_RECORD_BYTES: u64 = 16 * 1024 * 1024;

/// A reader that fails once a logical CSV record carries more delimiters
/// outside quotes than the guard allows (4 × `max_columns`, at least
/// 4096), or more bytes than its record limit: the csv reader allocates
/// per-field offsets for a whole record before any row check can run.
/// Quoting is tracked the way the csv reader reads it, so a record that
/// spans quoted newlines counts as one record. The bytes before the flood
/// are still delivered, so the rows read so far stay usable.
pub struct DelimiterGuard<R: Read> {
    inner: R,
    guard: usize,
    max_record_bytes: u64,
    delimiters: usize,
    record_bytes: u64,
    field: FieldState,
    /// Leading bytes matched against the byte-order marks the parser
    /// removes (`TableReader::open` skips one, the csv reader a second);
    /// `LEADING_MARK_BYTES` once they are complete or ruled out.
    leading: usize,
    /// The 1-based line of the next byte, and the line the current record
    /// starts on; `\n`, `\r` and `\r\n` each end one line, quoted or not.
    line: u64,
    record_line: u64,
    after_return: bool,
    tripped: Option<Tripped>,
}

/// Why a `DelimiterGuard` stopped: the line the record starts on, and the
/// guard when the delimiter count tripped it.
#[derive(Clone, Debug)]
struct Tripped {
    reason: String,
    line: u64,
    delimiter_guard: Option<usize>,
}

impl std::fmt::Display for Tripped {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.reason)
    }
}

impl std::error::Error for Tripped {}

const BYTE_ORDER_MARK: [u8; 3] = [0xef, 0xbb, 0xbf];
const LEADING_MARK_BYTES: usize = 2 * BYTE_ORDER_MARK.len();

/// Where the guard is inside the CSV grammar: at the start of a field, in
/// an unquoted field, inside quotes, or just after a quote inside quotes
/// (the next byte tells an escaped quote from the closing one).
#[derive(Clone, Copy)]
enum FieldState {
    Start,
    Unquoted,
    Quoted,
    QuoteInQuoted,
}

impl<R: Read> DelimiterGuard<R> {
    /// `max_record_bytes` caps a record (`u64::MAX` for no cap).
    pub fn new(inner: R, options: &ScanOptions, max_record_bytes: u64) -> Self {
        DelimiterGuard {
            inner,
            guard: options.max_columns.saturating_mul(4).max(4096),
            max_record_bytes,
            delimiters: 0,
            record_bytes: 0,
            field: FieldState::Start,
            leading: 0,
            line: 1,
            record_line: 1,
            after_return: false,
            tripped: None,
        }
    }
}

impl<R: Read> Read for DelimiterGuard<R> {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        if let Some(tripped) = &self.tripped {
            return Err(std::io::Error::other(tripped.clone()));
        }
        let read = self.inner.read(buf)?;
        for (offset, &byte) in buf[..read].iter().enumerate() {
            if self.leading < LEADING_MARK_BYTES {
                if byte == BYTE_ORDER_MARK[self.leading % BYTE_ORDER_MARK.len()] {
                    self.leading += 1;
                    continue; // a byte-order mark is not record content
                }
                // Not a mark after all: the bytes taken for a partial one
                // were ordinary field bytes.
                let partial = self.leading % BYTE_ORDER_MARK.len();
                if partial > 0 {
                    self.record_bytes += partial as u64;
                    self.field = FieldState::Unquoted;
                }
                self.leading = LEADING_MARK_BYTES;
            }
            self.record_bytes += 1;
            if byte == b'\r' || (byte == b'\n' && !self.after_return) {
                self.line += 1;
            }
            self.after_return = byte == b'\r';
            let mut delimiter = false;
            let mut record_end = false;
            self.field = match (self.field, byte) {
                (FieldState::Start, b'"') => FieldState::Quoted,
                (FieldState::Quoted, b'"') => FieldState::QuoteInQuoted,
                (FieldState::Quoted, _) => FieldState::Quoted,
                (FieldState::QuoteInQuoted, b'"') => FieldState::Quoted,
                (_, b',') => {
                    delimiter = true;
                    FieldState::Start
                }
                // the csv reader ends a record at \n, \r or \r\n alike
                (_, b'\n' | b'\r') => {
                    record_end = true;
                    FieldState::Start
                }
                _ => FieldState::Unquoted,
            };
            if record_end {
                self.delimiters = 0;
                self.record_bytes = 0;
                self.record_line = self.line;
                continue;
            }
            if delimiter {
                self.delimiters += 1;
            }
            let line = self.record_line;
            let tripped = if self.delimiters > self.guard {
                Tripped {
                    reason: format!(
                        "line {line} has more than {} delimiters outside quotes \
                         (the delimiter guard: 4 × max_columns, at least 4096)",
                        self.guard
                    ),
                    line,
                    delimiter_guard: Some(self.guard),
                }
            } else if self.record_bytes > self.max_record_bytes {
                Tripped {
                    reason: format!(
                        "line {line} starts a record longer than {} bytes",
                        self.max_record_bytes
                    ),
                    line,
                    delimiter_guard: None,
                }
            } else {
                continue;
            };
            self.tripped = Some(tripped.clone());
            // Hand back what precedes the flood; the next read fails. An
            // empty read would pass for a clean end.
            return if offset == 0 {
                Err(std::io::Error::other(tripped))
            } else {
                Ok(offset)
            };
        }
        Ok(read)
    }
}

fn empty_file(filename: &'static str) -> Notice {
    Notice::new("empty_file", Severity::Error).with("filename", filename)
}

/// transitio-specific (no canonical equivalent): the entry exists but
/// cannot be safely read — corrupt member, or a violated size/column guard,
/// whose `ScanOptions` fields the caller adds as `budgets`.
fn unreadable_file(filename: &'static str, message: &str) -> Notice {
    Notice::new("unreadable_file", Severity::Error)
        .with("filename", filename)
        .with("message", message)
}

/// `unreadable_file` for a read that failed. When a `DelimiterGuard`
/// stopped it, the notice names the line, and for too many delimiters the
/// guard and its budget.
fn read_failure(filename: &'static str, error: &csv::Error) -> Notice {
    let notice = unreadable_file(filename, &error.to_string());
    let tripped = match error.kind() {
        csv::ErrorKind::Io(io) => io
            .get_ref()
            .and_then(|inner| inner.downcast_ref::<Tripped>()),
        _ => None,
    };
    let Some(tripped) = tripped else {
        return notice;
    };
    let notice = notice.with("lineNumber", tripped.line);
    match tripped.delimiter_guard {
        Some(guard) => notice
            .with("budgets", vec!["max_columns"])
            .with("delimiterGuard", guard),
        None => notice,
    }
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
pub(crate) mod tests {
    use std::io::Cursor;

    use super::*;

    fn zip_with(files: &[(&str, &[u8])]) -> Cursor<Vec<u8>> {
        zip_written(files, zip::write::SimpleFileOptions::default(), false)
    }

    /// `zip_with` under the given entry options, with a ZIP64 footer when
    /// `zip64` is set.
    fn zip_written(
        files: &[(&str, &[u8])],
        options: zip::write::SimpleFileOptions,
        zip64: bool,
    ) -> Cursor<Vec<u8>> {
        let mut cursor = Cursor::new(Vec::new());
        {
            let mut writer = zip::ZipWriter::new(&mut cursor);
            for (name, content) in files {
                writer.start_file(*name, options).unwrap();
                std::io::Write::write_all(&mut writer, content).unwrap();
            }
            if zip64 {
                // Any extensible data sector, even an empty one, makes the
                // writer emit the ZIP64 footer.
                writer.set_raw_zip64_extensible_data_sector(Box::new([]));
            }
            writer.finish().unwrap();
        }
        cursor.set_position(0);
        cursor
    }

    pub(crate) fn build_zip(files: &[(&str, &str)]) -> Cursor<Vec<u8>> {
        let bytes: Vec<(&str, &[u8])> = files
            .iter()
            .map(|(name, content)| (*name, content.as_bytes()))
            .collect();
        zip_with(&bytes)
    }

    pub(crate) fn minimal() -> Vec<(&'static str, &'static str)> {
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
    fn delimiter_heavy_rows_are_refused_from_their_line() {
        let commas = ",".repeat(5000);
        let polygon = format!("area_id,wkt\na1,\"POLYGON(({commas}))\"\n");
        let header = "stop_id,stop_name,stop_lat,stop_lon\n";
        let (s1, s2) = ("s1,Kamppi,60.169,24.931\n", "s2,Steissi,60.171,24.941\n");
        let flooded = format!("{header}{s1}{commas}\n{s2}");
        let flooded_first = format!("{header}{commas}\n{s1}");
        // the file, its content, max_columns, the IDs kept, the file's
        // notices, and the line the guard trips on
        let cases = [
            ("areas.txt", polygon, 1000, vec!["a1"], vec![], None),
            (
                "stops.txt",
                flooded.clone(),
                1000,
                vec!["s1"],
                vec!["unreadable_file"],
                Some(3),
            ),
            (
                "stops.txt",
                flooded_first,
                1000,
                vec![],
                vec!["unreadable_file"],
                Some(2),
            ),
            (
                "stops.txt",
                flooded,
                2000,
                vec!["s1", "s2"],
                vec!["invalid_row_length"],
                None,
            ),
        ];
        for (file, content, max_columns, kept, expected, line) in cases {
            let mut files = minimal();
            files.retain(|(name, _)| *name != file);
            files.push((file, &content));
            let options = ScanOptions {
                max_columns,
                ..ScanOptions::default()
            };
            let result = scan_reader_with(build_zip(&files), options).unwrap();
            let notices: Vec<&Notice> = result
                .notices
                .iter()
                .filter(|n| n.context.get("filename").and_then(|v| v.as_str()) == Some(file))
                .collect();
            let found: Vec<&str> = notices.iter().map(|n| n.code).collect();
            assert_eq!(found, expected, "{file} under {max_columns}");
            let rows = result.tables.get(file).map_or(&[][..], |table| &table.rows);
            let ids: Vec<&str> = rows.iter().map(|row| row.fields[0].as_str()).collect();
            assert_eq!(ids, kept, "{file} under {max_columns}");
            if file == "areas.txt" {
                assert_eq!(rows[0].fields[1], format!("POLYGON(({commas}))"));
            }
            assert_eq!(result.incomplete.contains(file), line.is_some());
            let incomplete = result.incomplete.iter().map(String::as_str);
            let reason = refusal(&result.notices, incomplete, &options, false, "crop");
            let Some(line) = line else {
                assert_eq!(reason, None);
                continue;
            };
            let context = &notices[0].context;
            assert_eq!(context["lineNumber"], line);
            assert_eq!(context["delimiterGuard"], 4096);
            assert_eq!(context["budgets"], serde_json::json!(["max_columns"]));
            let expected = format!(
                "{file} line {line} has more than 4096 delimiters outside quotes, the guard \
                 set by max_columns (1000); raise it to crop this feed"
            );
            assert_eq!(reason.as_deref(), Some(expected.as_str()));
        }
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
    fn undecodable_rows_are_kept_with_a_notice_per_field() {
        let mut files: Vec<(&str, &[u8])> = minimal()
            .into_iter()
            .filter(|(name, _)| *name != "stops.txt")
            .map(|(name, content)| (name, content.as_bytes()))
            .collect();
        files.push((
            "stops.txt",
            b"stop_id,stop_name\ns1,Kamppi\ns2,\xff\xfe\ns\xff3,B\xffad\ns4,Steissi\n",
        ));
        files.push(("shapes.txt", b""));
        let result = scan_reader(zip_with(&files)).unwrap();
        let codes = codes(&result);
        assert!(codes.contains(&"empty_file")); // shapes.txt
        assert!(!codes.contains(&"missing_required_file"));
        let rows = &result.tables["stops.txt"].rows;
        assert_eq!(rows.len(), 4);
        assert_eq!(rows[1].csv_row, 3);
        assert_eq!(rows[1].fields, ["s2", "\u{FFFD}\u{FFFD}"]);
        let found: Vec<(u64, &str)> = result
            .notices
            .iter()
            .filter(|n| n.code == "invalid_character")
            .map(|n| {
                assert_eq!(n.severity, Severity::Error);
                (
                    n.context["csvRowNumber"].as_u64().unwrap(),
                    n.context["fieldName"].as_str().unwrap(),
                )
            })
            .collect();
        assert_eq!(found, [(3, "stop_name"), (4, "stop_id"), (4, "stop_name")]);
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

    #[test]
    fn bytes_after_the_end_record_are_tolerated() {
        let feed: Vec<(&str, &[u8])> = minimal()
            .into_iter()
            .map(|(name, content)| (name, content.as_bytes()))
            .collect();
        let deflated = zip::write::SimpleFileOptions::default();
        let archive = zip_written(&feed, deflated, false).into_inner();
        let zip64 = zip_written(&feed, deflated, true).into_inner();
        let appended = |extra: &[u8]| [archive.as_slice(), extra].concat();
        let mut truncated = archive.clone();
        truncated.pop();
        // An end record whose entry count defers to a missing ZIP64 record.
        let mut zip64_like = [0u8; 22];
        zip64_like[..4].copy_from_slice(&[0x50, 0x4b, 0x05, 0x06]);
        zip64_like[10..12].copy_from_slice(&[0xff, 0xff]);
        // A stored entry holding an empty end record, cut before the real
        // central directory: the stray record claims a directory that does
        // not end where it starts.
        let mut record = [0u8; 22];
        record[..4].copy_from_slice(&[0x50, 0x4b, 0x05, 0x06]);
        let stored = deflated.compression_method(zip::CompressionMethod::Stored);
        let mut stray = zip_written(&[("notes.bin", &record)], stored, false).into_inner();
        let end = stray.len();
        let cd_offset = u32::from_le_bytes(stray[end - 6..end - 2].try_into().unwrap());
        stray.truncate(cd_offset as usize);

        let mut names: Vec<&str> = minimal().iter().map(|(name, _)| *name).collect();
        names.sort_unstable();
        let cases = [
            ("none", archive.clone(), true),
            ("one byte", appended(&[0]), true),
            ("two bytes", appended(&[0, 0]), true),
            ("64 KiB", appended(&[0; 64 * 1024]), true),
            ("zip64", [zip64.as_slice(), &[0, 0]].concat(), true),
            ("zip64-like trailer", appended(&zip64_like), true),
            ("stray record", stray, false),
            ("truncated", truncated, false),
        ];
        for (case, bytes, accepted) in cases {
            match scan_reader(Cursor::new(bytes)) {
                Ok(result) => {
                    assert!(accepted, "{case}: expected a refusal");
                    let listed: Vec<&str> = result.tables.keys().map(String::as_str).collect();
                    assert_eq!(listed, names, "{case}");
                }
                Err(error) => {
                    assert!(!accepted, "{case}: {error}");
                    assert_eq!(
                        error, "not a readable zip: no end-of-central-directory record found",
                        "{case}"
                    );
                }
            }
        }
    }

    struct Streamed {
        headers: Vec<String>,
        rows: Vec<Row>,
        notices: Vec<Notice>,
        truncated: bool,
    }

    fn stream(bytes: &[u8], options: &ScanOptions, max_rows: u64) -> Streamed {
        let spec = schema::spec_for("stops.txt").unwrap();
        let mut notices = Vec::new();
        let reader = DelimiterGuard::new(bytes, options, MAX_RECORD_BYTES);
        let mut reader = match TableReader::open(spec, reader, options, max_rows, &mut notices) {
            Ok(reader) => reader,
            Err(outcome) => {
                return Streamed {
                    headers: Vec::new(),
                    rows: Vec::new(),
                    notices,
                    truncated: matches!(outcome, NoTable::Unreadable),
                }
            }
        };
        let mut rows = Vec::new();
        while let Some(row) = reader.next_row(&mut notices) {
            rows.push(row);
        }
        Streamed {
            truncated: reader.truncated(),
            headers: reader.into_headers(),
            rows,
            notices,
        }
    }

    fn notice_codes(notices: &[Notice]) -> Vec<&'static str> {
        notices.iter().map(|n| n.code).collect()
    }

    #[test]
    fn whitespace_is_trimmed_with_one_notice_per_file() {
        // the file, the first row's fields, the notice codes, and the
        // whitespace notice's row, field, value as written and count
        let header = "stop_id,stop_name,stop_lat,stop_lon\n";
        let cases = [
            (
                " stop_id ,stop_name,stop_lat,stop_lon \n s1,Kamppi ,60.1,24.9\ns2,K,60.2,24.8 \n"
                    .to_string(),
                ["s1", "Kamppi", "60.1", "24.9"],
                vec!["leading_or_trailing_whitespaces"],
                Some((1, "stop_id", " stop_id ", 5)),
            ),
            (
                format!("{header}\" s1 \",Kamppi,60.1,24.9\n"),
                ["s1", "Kamppi", "60.1", "24.9"],
                vec!["leading_or_trailing_whitespaces"],
                Some((2, "stop_id", " s1 ", 1)),
            ),
            (
                format!("{header}s1, ,60.1,24.9\n"),
                ["s1", "", "60.1", "24.9"],
                vec!["leading_or_trailing_whitespaces"],
                Some((2, "stop_name", " ", 1)),
            ),
            (
                format!("{header}s1,\u{a0}Kamppi\u{b}\u{1f},60.1,24.9\n"),
                ["s1", "Kamppi", "60.1", "24.9"],
                vec!["leading_or_trailing_whitespaces"],
                Some((2, "stop_name", "\u{a0}Kamppi\u{b}\u{1f}", 1)),
            ),
            (
                format!("{header} , ,,\t\ns1,Kamppi,60.1,24.9\n"),
                ["s1", "Kamppi", "60.1", "24.9"],
                vec!["empty_row", "leading_or_trailing_whitespaces"],
                Some((2, "stop_id", " ", 3)),
            ),
            (
                format!("{header}s1,Kamppi,60.1,24.9\n"),
                ["s1", "Kamppi", "60.1", "24.9"],
                vec![],
                None,
            ),
            (
                "stop_id,stop_name, stop_id,stop_lat,stop_lon\ns1,Kamppi,s1,60.1,24.9\n"
                    .to_string(),
                ["s1", "Kamppi", "s1", "60.1"],
                vec!["duplicated_column", "leading_or_trailing_whitespaces"],
                Some((1, "stop_id", " stop_id", 1)),
            ),
        ];
        for (bytes, fields, codes, expected) in cases {
            let options = ScanOptions::default();
            let streamed = stream(bytes.as_bytes(), &options, options.max_rows);
            assert_eq!(streamed.rows[0].fields[..4], fields, "{bytes:?}");
            assert_eq!(notice_codes(&streamed.notices), codes, "{bytes:?}");
            let found = streamed
                .notices
                .iter()
                .find(|n| n.code == "leading_or_trailing_whitespaces")
                .map(|n| {
                    (
                        n.context["csvRowNumber"].as_u64().unwrap(),
                        n.context["fieldName"].as_str().unwrap(),
                        n.context["fieldValue"].as_str().unwrap(),
                        n.context["trimmedCount"].as_u64().unwrap(),
                    )
                });
            assert_eq!(found, expected, "{bytes:?}");
        }
    }

    #[test]
    fn streamed_rows_and_notices_match_the_collected_parse() {
        // a byte-order mark, a short row, an empty row and an undecodable
        // byte between two good rows; a doubled byte-order mark, which the
        // csv reader removes after the first one is skipped, as before
        for (bytes, expected_codes) in [
            (
                &b"\xef\xbb\xbfstop_id,stop_name,stop_lat,stop_lon\n\
                a,Alpha,60.1,24.9\n\
                short,row\n\
                ,,,\n\
                b,Br\xffavo,60.2,24.8\n\
                c,Charlie,60.3,24.7\n"[..],
                vec!["invalid_row_length", "empty_row", "invalid_character"],
            ),
            (
                &b"\xef\xbb\xbf\xef\xbb\xbfstop_id,stop_name,stop_lat,stop_lon\na,Alpha,60.1,24.9\n"[..],
                vec![],
            ),
        ] {
            let options = ScanOptions::default();
            let spec = schema::spec_for("stops.txt").unwrap();
            let mut collected_notices = Vec::new();
            let (table, truncated) = read_table(spec, bytes, &options, &mut collected_notices);
            let table = table.unwrap();
            assert!(!truncated);

            let streamed = stream(bytes, &options, options.max_rows);
            assert!(!streamed.truncated);
            assert_eq!(streamed.headers, table.headers);
            assert_eq!(streamed.rows.len(), table.rows.len());
            for (streamed, collected) in streamed.rows.iter().zip(&table.rows) {
                assert_eq!(streamed.csv_row, collected.csv_row);
                assert_eq!(streamed.fields, collected.fields);
            }
            // code, severity and context alike
            assert_eq!(
                format!("{:?}", streamed.notices),
                format!("{collected_notices:?}")
            );
            assert_eq!(notice_codes(&streamed.notices), expected_codes);
        }
    }

    #[test]
    fn streamed_rows_stop_at_the_cap_with_the_notice_summary() {
        // the cap counts every data row, malformed ones included
        let bytes = b"stop_id,stop_name,stop_lat,stop_lon\n\
            a,Alpha,60.1,24.9\n\
            short\n\
            shorter\n\
            b,Bravo,60.2,24.8\n";
        let options = ScanOptions {
            max_notices_per_file: 1,
            ..ScanOptions::default()
        };
        let streamed = stream(bytes, &options, 3);
        assert_eq!(streamed.rows.len(), 1);
        assert!(streamed.truncated);
        assert_eq!(
            notice_codes(&streamed.notices),
            [
                "invalid_row_length",
                "too_many_rows",
                "notice_limit_reached"
            ]
        );
    }

    /// A source that fails after delivering its first `good` bytes.
    struct FailAfter {
        bytes: Vec<u8>,
        good: usize,
        delivered: usize,
    }

    impl std::io::Read for FailAfter {
        fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
            if self.delivered >= self.good {
                return Err(std::io::Error::other("source failed"));
            }
            let end = self.good.min(self.delivered + buf.len());
            let read = end - self.delivered;
            buf[..read].copy_from_slice(&self.bytes[self.delivered..end]);
            self.delivered = end;
            Ok(read)
        }
    }

    #[test]
    fn a_read_failure_ends_a_stream_as_unreadable() {
        let bytes = b"stop_id,stop_name,stop_lat,stop_lon\na,Alpha,60.1,24.9\nb,Bravo,60.2,24.8\n";
        let spec = schema::spec_for("stops.txt").unwrap();
        // failing inside the header, and right after the one allowed row,
        // which must not read as the row cap
        for (good, max_rows, rows_before) in [(10, u64::MAX, 0), (54, 1, 1)] {
            let mut notices = Vec::new();
            let source = FailAfter {
                bytes: bytes.to_vec(),
                good,
                delivered: 0,
            };
            let options = ScanOptions::default();
            let mut rows = 0;
            let truncated = match TableReader::open(spec, source, &options, max_rows, &mut notices)
            {
                Ok(mut reader) => {
                    while reader.next_row(&mut notices).is_some() {
                        rows += 1;
                    }
                    reader.truncated()
                }
                Err(outcome) => matches!(outcome, NoTable::Unreadable),
            };
            assert_eq!(rows, rows_before);
            assert!(truncated);
            assert_eq!(notice_codes(&notices), ["unreadable_file"]);
        }
    }

    #[test]
    fn a_streamed_file_is_left_to_the_caller() {
        // a byte budget below stop_times.txt's size: parsed, it would be
        // refused as unreadable; streamed, it is not read at all
        let files = minimal();
        let stop_times = files
            .iter()
            .find(|(name, _)| *name == "stop_times.txt")
            .map(|(_, content)| content.len())
            .unwrap();
        let options = ScanOptions {
            max_entry_bytes: stop_times as u64 - 1,
            ..ScanOptions::default()
        };
        let parsed = scan_reader_with(build_zip(&files), options).unwrap();
        assert!(parsed.incomplete.contains("stop_times.txt"));

        let result =
            scan_reader_streaming(build_zip(&files), options, &["stop_times.txt"]).unwrap();
        assert!(!result.tables.contains_key("stop_times.txt"));
        assert!(!result
            .unparsed_entries
            .contains(&"stop_times.txt".to_string()));
        assert!(!result.incomplete.contains("stop_times.txt"));
        let about_stop_times: Vec<_> = result
            .notices
            .iter()
            .filter(|n| {
                n.context.get("filename").and_then(|v| v.as_str()) == Some("stop_times.txt")
                    || n.code == "missing_required_file"
            })
            .map(|n| n.code)
            .collect();
        assert!(about_stop_times.is_empty(), "{about_stop_times:?}");
        // the other tables are parsed as usual
        assert!(result.tables.contains_key("trips.txt"));
    }

    #[test]
    fn a_flooded_record_ends_a_stream_as_unreadable() {
        let header = b"stop_id,stop_name,stop_lat,stop_lon\n";
        let row = b"a,Alpha,60.1,24.9\n";
        let commas: Vec<u8> = std::iter::repeat_n(b',', 5000).collect();
        // 5000 delimiters in a data row and in the header; one record over
        // the byte limit through quoted newlines, and one through a field
        let flooded_row: Vec<u8> = [&header[..], &row[..], &commas[..], &b"\n"[..]].concat();
        let flooded_header: Vec<u8> = [&commas[..], &b"\n"[..], &row[..]].concat();
        let mut quoted = [&header[..], &row[..], &b"q,\""[..]].concat();
        quoted.extend(std::iter::repeat_n(b'\n', MAX_RECORD_BYTES as usize + 1));
        quoted.extend(b"\",1,2\n");
        let mut oversized = [&header[..], &row[..], &b"o,"[..]].concat();
        oversized.extend(std::iter::repeat_n(b'x', MAX_RECORD_BYTES as usize + 1));
        oversized.extend(b",1,2\n");
        // a quoted first header field right after one byte-order mark, and
        // after the two the parser removes
        let mut marked_header = b"\xef\xbb\xbf\"stop".to_vec();
        marked_header.extend(std::iter::repeat_n(b'\n', MAX_RECORD_BYTES as usize + 1));
        marked_header.extend(b"_id\",stop_name,stop_lat,stop_lon\n");
        marked_header.extend(row);
        let twice_marked_header: Vec<u8> = [&BYTE_ORDER_MARK[..], &marked_header[..]].concat();
        // \r\n, a quoted \n and \r each end one line before the flood
        let line_ends: Vec<u8> = [
            &b"stop_id,stop_name,stop_lat,stop_lon\r\nb,\"Br\navo\",60.2,24.8\r"[..],
            &commas[..],
        ]
        .concat();
        // the flooded row arriving right after the last allowed row is a
        // read failure, not the row cap; only the delimiter guard follows a
        // budget; the line is the one the record starts on
        let columns = Some(serde_json::json!(["max_columns"]));
        for (bytes, max_rows, rows_before, budgets, line) in [
            (flooded_row, 1, 1, columns.clone(), 3),
            (flooded_header, u64::MAX, 0, columns.clone(), 1),
            (quoted, u64::MAX, 1, None, 3),
            (oversized, u64::MAX, 1, None, 3),
            (marked_header, u64::MAX, 0, None, 1),
            (twice_marked_header, u64::MAX, 0, None, 1),
            (line_ends, u64::MAX, 1, columns, 4),
        ] {
            let streamed = stream(&bytes, &ScanOptions::default(), max_rows);
            // the rows before the flood are usable; nothing after it is read
            assert_eq!(streamed.rows.len(), rows_before);
            assert!(streamed.truncated);
            assert_eq!(notice_codes(&streamed.notices), ["unreadable_file"]);
            let context = &streamed.notices[0].context;
            assert_eq!(context.get("budgets"), budgets.as_ref());
            assert_eq!(context["lineNumber"], line);
        }
    }

    #[test]
    fn quoted_delimiters_and_newlines_do_not_trip_the_guard() {
        // quoting starts right after the byte-order mark, in the header
        // as in the rows
        let bytes = b"\xef\xbb\xbf\"x,\ny\",stop_id,stop_name,stop_lat,stop_lon\n\
            1,a,\"Alpha, the \"\"first\"\"\nstop\",60.1,24.9\n\
            2,b,Bravo,60.2,24.8\n";
        let streamed = stream(bytes, &ScanOptions::default(), u64::MAX);
        assert!(!streamed.truncated);
        assert_eq!(streamed.headers[0], "x,\ny");
        assert_eq!(streamed.rows.len(), 2);
        assert_eq!(streamed.rows[0].fields[2], "Alpha, the \"first\"\nstop");
        assert!(streamed.notices.is_empty());
    }

    #[test]
    fn carriage_returns_end_records_for_the_guard() {
        // 2000 rows of three delimiters each, separated by lone carriage
        // returns: far over the guard unless every row resets it
        let mut bytes = b"stop_id,stop_name,stop_lat,stop_lon\r".to_vec();
        for i in 0..2000 {
            bytes.extend(format!("s{i},Stop {i},60.1,24.9\r").as_bytes());
        }
        let streamed = stream(&bytes, &ScanOptions::default(), u64::MAX);
        assert!(!streamed.truncated);
        assert_eq!(streamed.rows.len(), 2000);
        assert!(streamed.notices.is_empty());
    }
}
