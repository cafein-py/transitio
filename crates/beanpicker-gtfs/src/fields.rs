//! Typed column vocabulary for the field-format rule tier. Only kinds with
//! routing consequences are validated; presentation-only formats (URLs,
//! e-mail addresses, colors, languages) stay `Text`. Files whose vocabulary
//! is marked incomplete never produce `unknown_column`.

pub enum FieldKind {
    Text,
    Date,
    Time,
    Integer { min: i64, max: i64 },
    Float { min: f64, max: f64 },
    Latitude,
    Longitude,
    Enumeration(&'static [i64]),
    Timezone,
}

pub struct ColumnSpec {
    pub name: &'static str,
    pub kind: FieldKind,
    /// Unconditionally required to be non-empty when the column exists;
    /// conditional requirements live in the rules pass.
    pub required: bool,
}

pub struct TableFields {
    pub file: &'static str,
    pub complete: bool,
    pub columns: &'static [ColumnSpec],
}

const fn text(name: &'static str, required: bool) -> ColumnSpec {
    ColumnSpec {
        name,
        kind: FieldKind::Text,
        required,
    }
}

const fn date(name: &'static str, required: bool) -> ColumnSpec {
    ColumnSpec {
        name,
        kind: FieldKind::Date,
        required,
    }
}

const fn time(name: &'static str, required: bool) -> ColumnSpec {
    ColumnSpec {
        name,
        kind: FieldKind::Time,
        required,
    }
}

const fn int(name: &'static str, min: i64, max: i64, required: bool) -> ColumnSpec {
    ColumnSpec {
        name,
        kind: FieldKind::Integer { min, max },
        required,
    }
}

const fn float(name: &'static str, min: f64, max: f64, required: bool) -> ColumnSpec {
    ColumnSpec {
        name,
        kind: FieldKind::Float { min, max },
        required,
    }
}

const fn en(name: &'static str, values: &'static [i64], required: bool) -> ColumnSpec {
    ColumnSpec {
        name,
        kind: FieldKind::Enumeration(values),
        required,
    }
}

pub const TABLE_FIELDS: &[TableFields] = &[
    TableFields {
        file: "agency.txt",
        complete: true,
        columns: &[
            text("agency_id", false),
            text("agency_name", true),
            text("agency_url", true),
            ColumnSpec {
                name: "agency_timezone",
                kind: FieldKind::Timezone,
                required: true,
            },
            text("agency_lang", false),
            text("agency_phone", false),
            text("agency_fare_url", false),
            text("agency_email", false),
        ],
    },
    TableFields {
        file: "stops.txt",
        complete: true,
        columns: &[
            text("stop_id", true),
            text("stop_code", false),
            text("stop_name", false),
            text("tts_stop_name", false),
            text("stop_desc", false),
            ColumnSpec {
                name: "stop_lat",
                kind: FieldKind::Latitude,
                required: false,
            },
            ColumnSpec {
                name: "stop_lon",
                kind: FieldKind::Longitude,
                required: false,
            },
            text("zone_id", false),
            text("stop_url", false),
            en("location_type", &[0, 1, 2, 3, 4], false),
            text("parent_station", false),
            ColumnSpec {
                name: "stop_timezone",
                kind: FieldKind::Timezone,
                required: false,
            },
            en("wheelchair_boarding", &[0, 1, 2], false),
            text("level_id", false),
            text("platform_code", false),
        ],
    },
    TableFields {
        file: "routes.txt",
        complete: true,
        columns: &[
            text("route_id", true),
            text("agency_id", false),
            text("route_short_name", false),
            text("route_long_name", false),
            text("route_desc", false),
            // Extended route types (3-digit codes) are legal in the wild;
            // unexpected values are a WARNING, so they never block a feed.
            en("route_type", &[0, 1, 2, 3, 4, 5, 6, 7, 11, 12], true),
            text("route_url", false),
            text("route_color", false),
            text("route_text_color", false),
            int("route_sort_order", 0, i64::MAX, false),
            en("continuous_pickup", &[0, 1, 2, 3], false),
            en("continuous_drop_off", &[0, 1, 2, 3], false),
            text("network_id", false),
        ],
    },
    TableFields {
        file: "trips.txt",
        complete: true,
        columns: &[
            text("route_id", true),
            text("service_id", true),
            text("trip_id", true),
            text("trip_headsign", false),
            text("trip_short_name", false),
            en("direction_id", &[0, 1], false),
            text("block_id", false),
            text("shape_id", false),
            en("wheelchair_accessible", &[0, 1, 2], false),
            en("bikes_allowed", &[0, 1, 2], false),
            en("cars_allowed", &[0, 1, 2], false),
        ],
    },
    TableFields {
        file: "stop_times.txt",
        complete: true,
        columns: &[
            text("trip_id", true),
            time("arrival_time", false),
            time("departure_time", false),
            text("stop_id", false),
            text("location_group_id", false),
            text("location_id", false),
            int("stop_sequence", 0, i64::MAX, true),
            text("stop_headsign", false),
            time("start_pickup_drop_off_window", false),
            time("end_pickup_drop_off_window", false),
            en("pickup_type", &[0, 1, 2, 3], false),
            en("drop_off_type", &[0, 1, 2, 3], false),
            en("continuous_pickup", &[0, 1, 2, 3], false),
            en("continuous_drop_off", &[0, 1, 2, 3], false),
            float("shape_dist_traveled", 0.0, f64::MAX, false),
            en("timepoint", &[0, 1], false),
            text("pickup_booking_rule_id", false),
            text("drop_off_booking_rule_id", false),
        ],
    },
    TableFields {
        file: "calendar.txt",
        complete: true,
        columns: &[
            text("service_id", true),
            en("monday", &[0, 1], true),
            en("tuesday", &[0, 1], true),
            en("wednesday", &[0, 1], true),
            en("thursday", &[0, 1], true),
            en("friday", &[0, 1], true),
            en("saturday", &[0, 1], true),
            en("sunday", &[0, 1], true),
            date("start_date", true),
            date("end_date", true),
        ],
    },
    TableFields {
        file: "calendar_dates.txt",
        complete: true,
        columns: &[
            text("service_id", true),
            date("date", true),
            en("exception_type", &[1, 2], true),
        ],
    },
    TableFields {
        file: "feed_info.txt",
        complete: true,
        columns: &[
            text("feed_publisher_name", true),
            text("feed_publisher_url", true),
            text("feed_lang", true),
            text("default_lang", false),
            date("feed_start_date", false),
            date("feed_end_date", false),
            text("feed_version", false),
            text("feed_contact_email", false),
            text("feed_contact_url", false),
        ],
    },
    TableFields {
        file: "shapes.txt",
        complete: true,
        columns: &[
            text("shape_id", true),
            ColumnSpec {
                name: "shape_pt_lat",
                kind: FieldKind::Latitude,
                required: true,
            },
            ColumnSpec {
                name: "shape_pt_lon",
                kind: FieldKind::Longitude,
                required: true,
            },
            int("shape_pt_sequence", 0, i64::MAX, true),
            float("shape_dist_traveled", 0.0, f64::MAX, false),
        ],
    },
    TableFields {
        file: "frequencies.txt",
        complete: true,
        columns: &[
            text("trip_id", true),
            time("start_time", true),
            time("end_time", true),
            int("headway_secs", 1, i64::MAX, true),
            en("exact_times", &[0, 1], false),
        ],
    },
    TableFields {
        file: "transfers.txt",
        complete: true,
        columns: &[
            text("from_stop_id", false),
            text("to_stop_id", false),
            text("from_route_id", false),
            text("to_route_id", false),
            text("from_trip_id", false),
            text("to_trip_id", false),
            en("transfer_type", &[0, 1, 2, 3, 4, 5], true),
            int("min_transfer_time", 0, i64::MAX, false),
        ],
    },
    TableFields {
        file: "pathways.txt",
        complete: true,
        columns: &[
            text("pathway_id", true),
            text("from_stop_id", true),
            text("to_stop_id", true),
            en("pathway_mode", &[1, 2, 3, 4, 5, 6, 7], true),
            en("is_bidirectional", &[0, 1], true),
            float("length", 0.0, f64::MAX, false),
            int("traversal_time", 1, i64::MAX, false),
            int("stair_count", i64::MIN, i64::MAX, false),
            float("max_slope", f64::MIN, f64::MAX, false),
            float("min_width", 0.0, f64::MAX, false),
            text("signposted_as", false),
            text("reversed_signposted_as", false),
        ],
    },
    TableFields {
        file: "levels.txt",
        complete: true,
        columns: &[
            text("level_id", true),
            float("level_index", f64::MIN, f64::MAX, true),
            text("level_name", false),
        ],
    },
    TableFields {
        file: "fare_attributes.txt",
        complete: true,
        columns: &[
            text("fare_id", true),
            float("price", 0.0, f64::MAX, true),
            text("currency_type", true),
            en("payment_method", &[0, 1], true),
            en("transfers", &[0, 1, 2], false),
            text("agency_id", false),
            int("transfer_duration", 0, i64::MAX, false),
        ],
    },
    TableFields {
        file: "fare_rules.txt",
        complete: true,
        columns: &[
            text("fare_id", true),
            text("route_id", false),
            text("origin_id", false),
            text("destination_id", false),
            text("contains_id", false),
        ],
    },
    TableFields {
        file: "attributions.txt",
        complete: true,
        columns: &[
            text("attribution_id", false),
            text("agency_id", false),
            text("route_id", false),
            text("trip_id", false),
            text("organization_name", true),
            en("is_producer", &[0, 1], false),
            en("is_operator", &[0, 1], false),
            en("is_authority", &[0, 1], false),
            text("attribution_url", false),
            text("attribution_email", false),
            text("attribution_phone", false),
        ],
    },
    TableFields {
        file: "translations.txt",
        complete: true,
        columns: &[
            text("table_name", true),
            text("field_name", true),
            text("language", true),
            text("translation", true),
            text("record_id", false),
            text("record_sub_id", false),
            text("field_value", false),
        ],
    },
];

pub fn fields_for(file: &str) -> Option<&'static TableFields> {
    TABLE_FIELDS.iter().find(|table| table.file == file)
}
