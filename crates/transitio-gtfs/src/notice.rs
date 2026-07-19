use std::collections::BTreeMap;

use serde::Serialize;
use serde_json::Value;

/// Canonical gtfs-validator severities.
#[derive(Serialize, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Debug)]
#[serde(rename_all = "UPPERCASE")]
pub enum Severity {
    Info,
    Warning,
    Error,
}

/// One validation notice. `code` follows the canonical gtfs-validator
/// naming (notice class name minus `Notice`, snake_cased) so transitio
/// reports stay mergeable with hosted reports; `context` carries the
/// notice-specific fields.
#[derive(Serialize, Debug)]
pub struct Notice {
    pub code: &'static str,
    pub severity: Severity,
    pub context: BTreeMap<&'static str, Value>,
}

impl Notice {
    pub fn new(code: &'static str, severity: Severity) -> Self {
        Notice {
            code,
            severity,
            context: BTreeMap::new(),
        }
    }

    pub fn with(mut self, key: &'static str, value: impl Into<Value>) -> Self {
        self.context.insert(key, value.into());
        self
    }
}
