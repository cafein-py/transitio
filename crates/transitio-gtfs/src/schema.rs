/// Spec knowledge the structural pass needs: which files the spec defines,
/// which are required, their unconditional required columns, and the
/// primary-key columns for duplicate detection. Conditional requirements
/// (e.g. stop coordinates by location_type) belong to the field-level rule
/// set, not here.
pub struct FileSpec {
    pub name: &'static str,
    pub required: bool,
    pub required_columns: &'static [&'static str],
    /// Columns forming the primary key; empty disables the key check. The
    /// check is skipped when a key column is absent from the header (e.g.
    /// optional agency_id in single-agency feeds).
    pub key_columns: &'static [&'static str],
}

pub const FILES: &[FileSpec] = &[
    FileSpec {
        name: "agency.txt",
        required: true,
        required_columns: &["agency_name", "agency_url", "agency_timezone"],
        key_columns: &["agency_id"],
    },
    FileSpec {
        name: "stops.txt",
        required: true,
        required_columns: &["stop_id"],
        key_columns: &["stop_id"],
    },
    FileSpec {
        name: "routes.txt",
        required: true,
        required_columns: &["route_id", "route_type"],
        key_columns: &["route_id"],
    },
    FileSpec {
        name: "trips.txt",
        required: true,
        required_columns: &["route_id", "service_id", "trip_id"],
        key_columns: &["trip_id"],
    },
    FileSpec {
        name: "stop_times.txt",
        required: true,
        required_columns: &["trip_id", "stop_sequence"],
        key_columns: &["trip_id", "stop_sequence"],
    },
    FileSpec {
        name: "calendar.txt",
        required: false,
        required_columns: &[
            "service_id",
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
            "start_date",
            "end_date",
        ],
        key_columns: &["service_id"],
    },
    FileSpec {
        name: "calendar_dates.txt",
        required: false,
        required_columns: &["service_id", "date", "exception_type"],
        key_columns: &["service_id", "date"],
    },
    FileSpec {
        name: "feed_info.txt",
        required: false,
        required_columns: &["feed_publisher_name", "feed_publisher_url", "feed_lang"],
        key_columns: &[],
    },
    FileSpec {
        name: "shapes.txt",
        required: false,
        required_columns: &[
            "shape_id",
            "shape_pt_lat",
            "shape_pt_lon",
            "shape_pt_sequence",
        ],
        key_columns: &["shape_id", "shape_pt_sequence"],
    },
    FileSpec {
        name: "frequencies.txt",
        required: false,
        required_columns: &["trip_id", "start_time", "end_time", "headway_secs"],
        key_columns: &["trip_id", "start_time"],
    },
    FileSpec {
        name: "transfers.txt",
        required: false,
        required_columns: &["transfer_type"],
        key_columns: &[],
    },
    FileSpec {
        name: "fare_attributes.txt",
        required: false,
        required_columns: &[
            "fare_id",
            "price",
            "currency_type",
            "payment_method",
            "transfers",
        ],
        key_columns: &["fare_id"],
    },
    FileSpec {
        name: "fare_rules.txt",
        required: false,
        required_columns: &["fare_id"],
        key_columns: &["fare_id"],
    },
    FileSpec {
        name: "fare_media.txt",
        required: false,
        required_columns: &["fare_media_id", "fare_media_type"],
        key_columns: &["fare_media_id"],
    },
    FileSpec {
        name: "fare_products.txt",
        required: false,
        required_columns: &["fare_product_id", "amount", "currency"],
        key_columns: &["fare_product_id"],
    },
    FileSpec {
        name: "fare_leg_rules.txt",
        required: false,
        required_columns: &["fare_product_id"],
        key_columns: &["fare_product_id"],
    },
    FileSpec {
        name: "fare_leg_join_rules.txt",
        required: false,
        required_columns: &["from_network_id", "to_network_id"],
        key_columns: &[],
    },
    FileSpec {
        name: "fare_transfer_rules.txt",
        required: false,
        required_columns: &["fare_transfer_type"],
        key_columns: &[],
    },
    FileSpec {
        name: "rider_categories.txt",
        required: false,
        required_columns: &[
            "rider_category_id",
            "rider_category_name",
            "is_default_fare_category",
        ],
        key_columns: &["rider_category_id"],
    },
    FileSpec {
        name: "timeframes.txt",
        required: false,
        required_columns: &["timeframe_group_id", "service_id"],
        key_columns: &["timeframe_group_id", "service_id"],
    },
    FileSpec {
        name: "areas.txt",
        required: false,
        required_columns: &["area_id"],
        key_columns: &["area_id"],
    },
    FileSpec {
        name: "stop_areas.txt",
        required: false,
        required_columns: &["area_id", "stop_id"],
        key_columns: &["area_id", "stop_id"],
    },
    FileSpec {
        name: "networks.txt",
        required: false,
        required_columns: &["network_id"],
        key_columns: &["network_id"],
    },
    FileSpec {
        name: "route_networks.txt",
        required: false,
        required_columns: &["network_id", "route_id"],
        key_columns: &["route_id"],
    },
    FileSpec {
        name: "pathways.txt",
        required: false,
        required_columns: &[
            "pathway_id",
            "from_stop_id",
            "to_stop_id",
            "pathway_mode",
            "is_bidirectional",
        ],
        key_columns: &["pathway_id"],
    },
    FileSpec {
        name: "levels.txt",
        required: false,
        required_columns: &["level_id", "level_index"],
        key_columns: &["level_id"],
    },
    FileSpec {
        name: "location_groups.txt",
        required: false,
        required_columns: &["location_group_id"],
        key_columns: &["location_group_id"],
    },
    FileSpec {
        name: "location_group_stops.txt",
        required: false,
        required_columns: &["location_group_id", "stop_id"],
        key_columns: &["location_group_id", "stop_id"],
    },
    FileSpec {
        name: "booking_rules.txt",
        required: false,
        required_columns: &["booking_rule_id", "booking_type"],
        key_columns: &["booking_rule_id"],
    },
    FileSpec {
        name: "translations.txt",
        required: false,
        required_columns: &["table_name", "field_name", "language", "translation"],
        key_columns: &[],
    },
    FileSpec {
        name: "attributions.txt",
        required: false,
        required_columns: &["organization_name"],
        key_columns: &["attribution_id"],
    },
];

pub fn spec_for(name: &str) -> Option<&'static FileSpec> {
    FILES.iter().find(|spec| spec.name == name)
}

/// Optional primary-key components: part of the table's composite key when
/// the column is present, treated as an empty component when absent.
/// Tables whose whole key is optional-conditional (transfers, translations)
/// stay keyless in the structural tier; see plans/validation-rules.md.
pub fn optional_key_columns(name: &str) -> &'static [&'static str] {
    match name {
        "fare_products.txt" => &["rider_category_id", "fare_media_id"],
        "fare_rules.txt" => &["route_id", "origin_id", "destination_id", "contains_id"],
        "fare_leg_rules.txt" => &[
            "network_id",
            "from_area_id",
            "to_area_id",
            "from_timeframe_group_id",
            "to_timeframe_group_id",
        ],
        "timeframes.txt" => &["start_time", "end_time"],
        _ => &[],
    }
}
