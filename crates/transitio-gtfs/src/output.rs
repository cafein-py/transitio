//! Writing a feed zip: parsed tables, rows streamed straight from a source,
//! and archive entries copied through verbatim, in whatever order the
//! caller produces them.

use std::collections::BTreeMap;
use std::path::Path;

use crate::scan::Table;

/// A feed zip under construction. Entries are written as they come, so a
/// table far larger than memory can be streamed row by row between the
/// parsed tables.
pub(crate) struct ZipOutput {
    writer: zip::ZipWriter<std::fs::File>,
    written: Vec<String>,
}

impl ZipOutput {
    pub(crate) fn create(path: &Path) -> Result<Self, String> {
        let file = std::fs::File::create(path)
            .map_err(|e| format!("cannot create {}: {e}", path.display()))?;
        Ok(ZipOutput {
            writer: zip::ZipWriter::new(file),
            written: Vec::new(),
        })
    }

    /// Write a parsed table with RFC 4180 quoting.
    pub(crate) fn table(&mut self, name: &str, table: &Table) -> Result<usize, String> {
        self.rows(
            name,
            &table.headers,
            table.rows.iter().map(|row| row.fields.as_slice()),
        )
    }

    /// Write a CSV entry row by row; the row count written is returned.
    pub(crate) fn rows<I>(
        &mut self,
        name: &str,
        headers: &[String],
        rows: I,
    ) -> Result<usize, String>
    where
        I: IntoIterator,
        I::Item: AsRef<[String]>,
    {
        self.writer
            .start_file(name, zip::write::SimpleFileOptions::default())
            .map_err(|e| format!("cannot write {name}: {e}"))?;
        let mut count = 0;
        {
            let mut csv_writer = csv::Writer::from_writer(&mut self.writer);
            csv_writer
                .write_record(headers)
                .map_err(|e| format!("cannot write {name} header: {e}"))?;
            for row in rows {
                csv_writer
                    .write_record(row.as_ref())
                    .map_err(|e| format!("cannot write {name} row: {e}"))?;
                count += 1;
            }
            csv_writer
                .flush()
                .map_err(|e| format!("cannot finish {name}: {e}"))?;
        }
        self.written.push(name.to_string());
        Ok(count)
    }

    /// Copy the named entries of the source archive through verbatim:
    /// entries the parser deliberately did not model (GTFS-Flex files,
    /// unknown files), since semantic equivalence forbids dropping data the
    /// transformation never touched. Hostile names are the exception —
    /// traversal components, aliases of the entries already written and
    /// symlink entries never reach the output.
    pub(crate) fn passthrough(&mut self, source: &Path, names: &[String]) -> Result<(), String> {
        if names.is_empty() {
            return Ok(());
        }
        let file = std::fs::File::open(source)
            .map_err(|e| format!("cannot reopen {}: {e}", source.display()))?;
        let mut archive = zip::ZipArchive::new(file)
            .map_err(|e| format!("cannot reread {}: {e}", source.display()))?;
        let mut copied: std::collections::HashSet<String> = std::collections::HashSet::new();
        for index in 0..archive.len() {
            let entry = archive
                .by_index_raw(index)
                .map_err(|e| format!("cannot reread zip entry {index}: {e}"))?;
            let entry_name = entry.name().to_string();
            let symlink = entry
                .unix_mode()
                .map(|mode| mode & 0o170000 == 0o120000)
                .unwrap_or(false);
            if names.contains(&entry_name)
                && !symlink
                && self.safe_passthrough(&entry_name)
                && copied.insert(entry_name)
            {
                self.writer
                    .raw_copy_file(entry)
                    .map_err(|e| format!("cannot copy archive entry: {e}"))?;
            }
        }
        Ok(())
    }

    fn safe_passthrough(&self, name: &str) -> bool {
        if name.contains('\\') || name.starts_with('/') {
            return false;
        }
        if name
            .split('/')
            .any(|part| part.is_empty() || part == "." || part == "..")
        {
            return false;
        }
        !self
            .written
            .iter()
            .any(|written| written.eq_ignore_ascii_case(name))
    }

    pub(crate) fn finish(self) -> Result<(), String> {
        self.writer
            .finish()
            .map_err(|e| format!("cannot finish archive: {e}"))?;
        Ok(())
    }
}

/// Write the tables to a fresh zip, then copy the passthrough entries of
/// the source archive through verbatim.
pub(crate) fn write_zip(
    tables: &BTreeMap<String, Table>,
    passthrough: Option<(&Path, &[String])>,
    output: &Path,
) -> Result<(), String> {
    let mut zip = ZipOutput::create(output)?;
    for (name, table) in tables {
        zip.table(name, table)?;
    }
    if let Some((source, names)) = passthrough {
        zip.passthrough(source, names)?;
    }
    zip.finish()
}

#[cfg(test)]
mod tests {
    use std::io::Write;

    use super::*;
    use crate::scan::{scan_reader, Row};

    fn table(headers: &[&str], rows: &[&[&str]]) -> Table {
        Table {
            headers: headers.iter().map(|h| h.to_string()).collect(),
            rows: rows
                .iter()
                .enumerate()
                .map(|(i, fields)| Row {
                    csv_row: i as u64 + 2,
                    fields: fields.iter().map(|f| f.to_string()).collect(),
                })
                .collect(),
        }
    }

    #[test]
    fn parsed_streamed_and_copied_entries_read_back_together() {
        let dir = std::env::temp_dir().join(format!("transitio-output-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let source = dir.join("source.zip");
        {
            let mut writer = zip::ZipWriter::new(std::fs::File::create(&source).unwrap());
            let options = zip::write::SimpleFileOptions::default();
            for (name, content) in [
                ("notes.txt", "kept verbatim\n"),
                ("stops.txt", "shadowed\n"),
                ("../escape.txt", "never copied\n"),
            ] {
                writer.start_file(name, options).unwrap();
                writer.write_all(content.as_bytes()).unwrap();
            }
            writer.finish().unwrap();
        }
        let output = dir.join("out.zip");
        let mut zip = ZipOutput::create(&output).unwrap();
        let stops = table(
            &["stop_id", "stop_name", "stop_lat", "stop_lon"],
            &[&["a", "Alpha, quoted", "60.1", "24.9"]],
        );
        assert_eq!(zip.table("stops.txt", &stops).unwrap(), 1);
        // rows arriving one by one, as a stream would deliver them
        let streamed = (0..3).map(|i| {
            vec![
                format!("t{i}"),
                "a".to_string(),
                format!("08:0{i}:00"),
                format!("08:0{i}:00"),
                "1".to_string(),
            ]
        });
        let headers: Vec<String> = [
            "trip_id",
            "stop_id",
            "arrival_time",
            "departure_time",
            "stop_sequence",
        ]
        .iter()
        .map(|h| h.to_string())
        .collect();
        assert_eq!(zip.rows("stop_times.txt", &headers, streamed).unwrap(), 3);
        let names: Vec<String> = ["notes.txt", "stops.txt", "../escape.txt"]
            .iter()
            .map(|n| n.to_string())
            .collect();
        zip.passthrough(&source, &names).unwrap();
        zip.finish().unwrap();

        let result = scan_reader(std::fs::File::open(&output).unwrap()).unwrap();
        assert_eq!(
            result.tables["stops.txt"].rows[0].fields[1],
            "Alpha, quoted"
        );
        assert_eq!(result.tables["stop_times.txt"].rows.len(), 3);
        assert_eq!(result.tables["stop_times.txt"].rows[2].fields[0], "t2");
        // the written table is not shadowed by the source's copy, and the
        // traversal name never arrives
        let mut archive = zip::ZipArchive::new(std::fs::File::open(&output).unwrap()).unwrap();
        let entries: Vec<String> = (0..archive.len())
            .map(|i| archive.by_index_raw(i).unwrap().name().to_string())
            .collect();
        assert_eq!(entries, ["stops.txt", "stop_times.txt", "notes.txt"]);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
