//! Cafein-readiness tier: predicts, per trip, which tier of cafein's
//! distance ladder will accept the feed's data, so problems surface
//! before handoff instead of as silent fallbacks after it. The rules
//! mirror cafein 0.10.0 (python/cafein/geometry.py — byte-identical
//! to 0.9.0, re-verified 2026-08-05; 0.10.0's route-relation extraction
//! is groundwork that feeds no ladder tier yet); the constants
//! duplicated here are pinned to that release and must track it.

use std::collections::{BTreeMap, HashMap, HashSet};

use serde::Serialize;

use crate::notice::{Notice, Severity};
use crate::rules::{clip, Samplers};
use crate::scan::{Row, ScanOptions, ScanResult, Table};

/// cafein geometry.py:30 — its haversine radius (the editor's Python
/// helper uses another sphere; the prediction must match cafein).
const EARTH_RADIUS: f64 = 6_371_000.0;
/// cafein geometry.py:27 — tier-2 stop-to-shape snap tolerance, meters.
const SNAP_TOLERANCE: f64 = 100.0;
/// cafein geometry.py:294-297 — tier-1 detour-ratio acceptance bands,
/// bounds inclusive; the kilometer band is unit-corrected by 1000.
const RATIO_METERS: (f64, f64) = (0.8, 5.0);
const RATIO_KILOMETERS: (f64, f64) = (0.8e-3, 5e-3);
/// Tier-2 work cap: point-to-segment evaluations summed feed-wide;
/// patterns that no longer fit are counted as unprocessed.
const MAX_TIER2_POINT_CHECKS: u64 = 20_000_000;
/// Fare-compatibility budget: each (route, priceable fare) pair
/// charges a fixed, precomputed 2 + |OD clauses| + |zones| units, so
/// the cap bounds real comparisons; exhaustion serializes
/// routeCompatibility null.
const MAX_FARE_COMPAT_CHECKS: u64 = 1_000_000;
/// "Near-zero" coverage per the roadmap: warn below this share.
const FARE_COVERAGE_WARN_SHARE: f64 = 0.2;

const CAFEIN_VERSION: &str = "0.10.0";

#[derive(Serialize, Debug)]
pub struct Readiness {
    pub distances: Option<DistanceReadiness>,
    pub fares: Option<FareReadiness>,
}

#[derive(Serialize, Debug)]
pub struct FareReadiness {
    pub v1: V1Fares,
    pub v2: V2Fares,
    #[serde(rename = "transferPricing")]
    pub transfer_pricing: &'static str,
    pub verdict: &'static str,
}

#[derive(Serialize, Debug)]
pub struct V1Fares {
    pub fares: u64,
    pub priceable: u64,
    #[serde(rename = "routeCompatibility")]
    pub route_compatibility: Option<f64>,
    #[serde(rename = "agencyAmbiguous")]
    pub agency_ambiguous: bool,
}

#[derive(Serialize, Debug)]
pub struct V2Fares {
    pub present: bool,
    pub products: u64,
    pub priceable: u64,
    #[serde(rename = "legRules")]
    pub leg_rules: bool,
    #[serde(rename = "transferRules")]
    pub transfer_rules: bool,
}

#[derive(Serialize, Debug)]
pub struct DistanceReadiness {
    pub trips: u64,
    pub skipped: u64,
    pub unprocessed: u64,
    pub predicted: Predicted,
    pub verdict: Option<&'static str>,
    pub cafein: &'static str,
}

#[derive(Serialize, Debug, Default)]
pub struct Predicted {
    pub shape_dist: u64,
    pub shape_linref: u64,
    pub crow_fly: u64,
}

pub fn run_readiness(result: &mut ScanResult, options: &ScanOptions) {
    result.readiness = Some(Readiness {
        distances: distance_readiness(result, options, MAX_TIER2_POINT_CHECKS),
        fares: fare_readiness(result, options, MAX_FARE_COMPAT_CHECKS),
    });
}

fn column(table: &Table, name: &str) -> Option<usize> {
    table.headers.iter().position(|h| h == name)
}

fn cell<'t>(table: &'t Table, row: &'t Row, name: &str) -> &'t str {
    column(table, name)
        .map(|i| row.fields[i].as_str())
        .unwrap_or("")
}

fn haversine(a: (f64, f64), b: (f64, f64)) -> f64 {
    let (lat1, lon1) = (a.0.to_radians(), a.1.to_radians());
    let (lat2, lon2) = (b.0.to_radians(), b.1.to_radians());
    let dlat = lat2 - lat1;
    let dlon = lon2 - lon1;
    let h = (dlat / 2.0).sin().powi(2) + lat1.cos() * lat2.cos() * (dlon / 2.0).sin().powi(2);
    2.0 * EARTH_RADIUS * h.sqrt().asin()
}

/// WGS84 transverse-Mercator forward projection (Krüger series, well
/// under a millimeter of error) — the projection family behind
/// cafein's `estimate_utm_crs()`. Northing/easting offsets are omitted:
/// distances and along-positions are translation-invariant.
struct Transverse {
    lon0: f64,
    radius: f64,
    alpha: [f64; 4],
    flat_n: f64,
}

impl Transverse {
    fn zone(zone: i32) -> Transverse {
        const A: f64 = 6_378_137.0;
        const F: f64 = 1.0 / 298.257_223_563;
        const K0: f64 = 0.9996;
        let n = F / (2.0 - F);
        let n2 = n * n;
        let n3 = n2 * n;
        let n4 = n2 * n2;
        Transverse {
            lon0: -183.0 + 6.0 * zone as f64,
            radius: K0 * A / (1.0 + n) * (1.0 + n2 / 4.0 + n4 / 64.0),
            alpha: [
                n / 2.0 - 2.0 * n2 / 3.0 + 5.0 * n3 / 16.0 + 41.0 * n4 / 180.0,
                13.0 * n2 / 48.0 - 3.0 * n3 / 5.0 + 557.0 * n4 / 1440.0,
                61.0 * n3 / 240.0 - 103.0 * n4 / 140.0,
                49561.0 * n4 / 161_280.0,
            ],
            flat_n: n,
        }
    }

    fn project(&self, lat: f64, lon: f64) -> (f64, f64) {
        let phi = lat.to_radians();
        let lam = (lon - self.lon0).to_radians();
        let e = 2.0 * self.flat_n.sqrt() / (1.0 + self.flat_n);
        let t = (phi.sin().atanh() - e * (e * phi.sin()).atanh()).sinh();
        let xi = t.atan2(lam.cos());
        let eta = (lam.sin() / t.hypot(lam.cos())).asinh();
        let mut x = eta;
        let mut y = xi;
        for (j, a) in self.alpha.iter().enumerate() {
            let k = 2.0 * (j as f64 + 1.0);
            x += a * (k * xi).cos() * (k * eta).sinh();
            y += a * (k * xi).sin() * (k * eta).cosh();
        }
        (self.radius * x, self.radius * y)
    }
}

fn utm_zone(lon: f64) -> i32 {
    (((lon + 180.0) / 6.0).floor() as i32 + 1).clamp(1, 60)
}

/// The zone `estimate_utm_crs()` lands on for a bounds midpoint: the
/// regular 6-degree grid plus the Norway and Svalbard extents that are
/// part of the standard UTM zone definitions.
fn zone_with_exceptions(lat: f64, lon: f64) -> i32 {
    if (56.0..64.0).contains(&lat) && (3.0..12.0).contains(&lon) {
        return 32;
    }
    if (72.0..84.0).contains(&lat) {
        if (0.0..9.0).contains(&lon) {
            return 31;
        }
        if (9.0..21.0).contains(&lon) {
            return 33;
        }
        if (21.0..33.0).contains(&lon) {
            return 35;
        }
        if (33.0..42.0).contains(&lon) {
            return 37;
        }
    }
    utm_zone(lon)
}

/// cafein projects every trip with ONE feed-wide CRS from
/// `estimate_utm_crs()`; these candidates hedge our mimicry of that
/// selection (bounds-midpoint rule versus pyproj's area-of-interest
/// logic) with the midpoint zone's neighbours plus the Norway/Svalbard
/// exception zone — at most four, so wide feeds can neither burn the
/// budget nor manufacture disagreements. Per-trip zones are
/// deliberately not used: cafein does not reproject per trip.
fn candidate_zones(lat_mid: f64, lon_mid: f64) -> Vec<i32> {
    let mid = utm_zone(lon_mid);
    let mut zones = vec![(mid - 1).max(1), mid, (mid + 1).min(60)];
    let exception = zone_with_exceptions(lat_mid, lon_mid);
    if !zones.contains(&exception) {
        zones.push(exception);
    }
    zones.sort_unstable();
    zones.dedup();
    zones
}

/// Distance from `p` to segment `ab`, and the along-length of the
/// projection foot within the segment.
fn project_on_segment(p: (f64, f64), a: (f64, f64), b: (f64, f64)) -> (f64, f64) {
    let (dx, dy) = (b.0 - a.0, b.1 - a.1);
    let len2 = dx * dx + dy * dy;
    let t = if len2 == 0.0 {
        0.0
    } else {
        (((p.0 - a.0) * dx + (p.1 - a.1) * dy) / len2).clamp(0.0, 1.0)
    };
    let foot = (a.0 + t * dx, a.1 + t * dy);
    ((p.0 - foot.0).hypot(p.1 - foot.1), t * len2.sqrt())
}

/// One zone's tier-2 evaluation: per-stop snap distances and the
/// along-shape order check, mirroring cafein's `_locate_on_shape`.
struct ZoneOutcome {
    distances: Vec<f64>,
    ordered: bool,
}

fn evaluate_zone(zone: i32, shape: &[(f64, f64)], stops: &[(f64, f64)]) -> ZoneOutcome {
    let projection = Transverse::zone(zone);
    let line: Vec<(f64, f64)> = shape
        .iter()
        .map(|(lat, lon)| projection.project(*lat, *lon))
        .collect();
    let mut cumulative = vec![0.0; line.len()];
    for i in 1..line.len() {
        cumulative[i] =
            cumulative[i - 1] + (line[i].0 - line[i - 1].0).hypot(line[i].1 - line[i - 1].1);
    }
    let mut distances = Vec::with_capacity(stops.len());
    let mut along = Vec::with_capacity(stops.len());
    for (lat, lon) in stops {
        let p = projection.project(*lat, *lon);
        let mut best = f64::INFINITY;
        let mut best_along = 0.0;
        for i in 1..line.len() {
            let (dist, within) = project_on_segment(p, line[i - 1], line[i]);
            if dist < best {
                best = dist;
                best_along = cumulative[i - 1] + within;
            }
        }
        distances.push(best);
        along.push(best_along);
    }
    let ordered = along.windows(2).all(|pair| pair[1] >= pair[0])
        && along.last().copied().unwrap_or(0.0) > along.first().copied().unwrap_or(0.0);
    ZoneOutcome { distances, ordered }
}

/// A cached tier-2 result for one (shape, stop sequence) pattern.
struct Tier2 {
    /// None: the candidate zones disagreed — the trip is unprocessed.
    pass: Option<bool>,
    /// Every stop all zones agree lies beyond the tolerance, each with
    /// its smallest distance among zones.
    far_stops: Vec<(usize, f64)>,
}

fn evaluate_tier2(zones: &[i32], shape: &[(f64, f64)], stops: &[(f64, f64)]) -> Tier2 {
    let outcomes: Vec<ZoneOutcome> = zones
        .iter()
        .map(|zone| evaluate_zone(*zone, shape, stops))
        .collect();
    let passes: Vec<bool> = outcomes
        .iter()
        .map(|o| o.ordered && o.distances.iter().all(|d| *d <= SNAP_TOLERANCE))
        .collect();
    if passes.iter().any(|p| *p != passes[0]) {
        return Tier2 {
            pass: None,
            far_stops: Vec::new(),
        };
    }
    let mut far_stops = Vec::new();
    if !passes[0] {
        for stop in 0..stops.len() {
            if outcomes.iter().all(|o| o.distances[stop] > SNAP_TOLERANCE) {
                let closest = outcomes
                    .iter()
                    .map(|o| o.distances[stop])
                    .fold(f64::INFINITY, f64::min);
                far_stops.push((stop, closest));
            }
        }
    }
    Tier2 {
        pass: Some(passes[0]),
        far_stops,
    }
}

/// cafein's tier-1 band decision: Some(scale) when the detour ratio is
/// acceptable (1.0 = meters, 1000.0 = kilometers), None otherwise.
/// Bounds inclusive, exactly as in geometry.py:294-297.
fn ratio_scale(ratio: f64) -> Option<f64> {
    if (RATIO_METERS.0..=RATIO_METERS.1).contains(&ratio) {
        return Some(1.0);
    }
    if (RATIO_KILOMETERS.0..=RATIO_KILOMETERS.1).contains(&ratio) {
        return Some(1000.0);
    }
    None
}

fn parse_coord(value: &str) -> Option<f64> {
    let parsed: f64 = value.trim().parse().ok()?;
    parsed.is_finite().then_some(parsed)
}

/// Predicts the ladder outcome per trip. `cap` bounds the tier-2
/// point-to-segment evaluations (a parameter so tests can exhaust it).
fn distance_readiness(
    result: &mut ScanResult,
    options: &ScanOptions,
    cap: u64,
) -> Option<DistanceReadiness> {
    // A verdict from truncated input would be confidently wrong; an
    // absent shapes.txt, in contrast, is a legitimate shapeless feed.
    for file in ["stops.txt", "stop_times.txt", "trips.txt", "shapes.txt"] {
        if result.incomplete.contains(file) {
            return None;
        }
    }
    let tables = &result.tables;
    let trips_table = tables.get("trips.txt")?;
    let stop_times = tables.get("stop_times.txt")?;
    let stops_table = tables.get("stops.txt")?;

    let mut notices = Vec::new();
    let mut samplers = Samplers::new(options.max_notices_per_file);

    let mut coords: HashMap<&str, (f64, f64)> = HashMap::new();
    for row in &stops_table.rows {
        let id = cell(stops_table, row, "stop_id");
        if let (Some(lat), Some(lon)) = (
            parse_coord(cell(stops_table, row, "stop_lat")),
            parse_coord(cell(stops_table, row, "stop_lon")),
        ) {
            coords.insert(id, (lat, lon));
        }
    }

    struct StopTime<'t> {
        seq: i64,
        stop_id: &'t str,
        distance: Option<f64>,
    }
    let mut by_trip: BTreeMap<&str, Vec<StopTime>> = BTreeMap::new();
    for row in &stop_times.rows {
        let Ok(seq) = cell(stop_times, row, "stop_sequence").trim().parse::<i64>() else {
            continue;
        };
        by_trip
            .entry(cell(stop_times, row, "trip_id"))
            .or_default()
            .push(StopTime {
                seq,
                stop_id: cell(stop_times, row, "stop_id"),
                distance: cell(stop_times, row, "shape_dist_traveled")
                    .trim()
                    .parse()
                    .ok(),
            });
    }
    for stops in by_trip.values_mut() {
        stops.sort_by_key(|s| s.seq);
    }

    let mut shapes: BTreeMap<&str, Vec<(i64, f64, f64)>> = BTreeMap::new();
    if let Some(table) = tables.get("shapes.txt") {
        for row in &table.rows {
            let Ok(seq) = cell(table, row, "shape_pt_sequence").trim().parse::<i64>() else {
                continue;
            };
            if let (Some(lat), Some(lon)) = (
                parse_coord(cell(table, row, "shape_pt_lat")),
                parse_coord(cell(table, row, "shape_pt_lon")),
            ) {
                shapes
                    .entry(cell(table, row, "shape_id"))
                    .or_default()
                    .push((seq, lat, lon));
            }
        }
    }
    let shape_lines: HashMap<&str, Vec<(f64, f64)>> = shapes
        .into_iter()
        .map(|(id, mut points)| {
            points.sort_by_key(|p| p.0);
            (
                id,
                points.into_iter().map(|(_, lat, lon)| (lat, lon)).collect(),
            )
        })
        .collect();

    // Sorted trips, deterministic budget consumption.
    let mut trip_shapes: BTreeMap<&str, &str> = BTreeMap::new();
    for row in &trips_table.rows {
        let trip_id = cell(trips_table, row, "trip_id");
        if !trip_id.is_empty() {
            trip_shapes.insert(trip_id, cell(trips_table, row, "shape_id"));
        }
    }

    let mut predicted = Predicted::default();
    let mut skipped = 0u64;
    let mut unprocessed = 0u64;
    let mut without_shapes = 0u64;
    let mut budget = cap;
    let mut exhausted = false;
    let mut tier2_cache: HashMap<(String, Vec<String>), Tier2> = HashMap::new();
    let mut chord_shapes: HashSet<&str> = HashSet::new();

    // The candidate zones come from the feed's stop bounds, like
    // cafein's stop-derived CRS estimate.
    let (mut lat_min, mut lat_max) = (f64::INFINITY, f64::NEG_INFINITY);
    let (mut lon_min, mut lon_max) = (f64::INFINITY, f64::NEG_INFINITY);
    for (lat, lon) in coords.values() {
        lat_min = lat_min.min(*lat);
        lat_max = lat_max.max(*lat);
        lon_min = lon_min.min(*lon);
        lon_max = lon_max.max(*lon);
    }
    let zones = if coords.is_empty() {
        Vec::new()
    } else {
        candidate_zones((lat_min + lat_max) / 2.0, (lon_min + lon_max) / 2.0)
    };

    for (trip_id, shape_id) in &trip_shapes {
        let Some(stops) = by_trip.get(trip_id) else {
            skipped += 1; // unusable trip, flagged elsewhere
            continue;
        };
        let positions: Option<Vec<(f64, f64)>> = stops
            .iter()
            .map(|s| coords.get(s.stop_id).copied())
            .collect();
        let Some(positions) = positions else {
            // cafein's trip_distances raises on a stop without
            // coordinates; the canonical notices already flag it.
            skipped += 1;
            continue;
        };
        let crow: f64 = positions
            .windows(2)
            .map(|pair| haversine(pair[0], pair[1]))
            .sum();

        let shape = shape_lines.get(*shape_id).filter(|line| line.len() >= 2);
        if shape.is_none() {
            without_shapes += 1;
        }

        // Tier 1: the values live in stop_times; no shape involved.
        let values: Option<Vec<f64>> = stops.iter().map(|s| s.distance).collect();
        if let Some(values) = values {
            let monotone = values.windows(2).all(|pair| pair[1] >= pair[0]);
            let total = values.last().unwrap() - values.first().unwrap();
            if monotone && total > 0.0 && crow > 0.0 {
                let ratio = total / crow;
                match ratio_scale(ratio) {
                    Some(scale) => {
                        if scale > 1.0 {
                            // transitio-specific: cafein unit-corrects a
                            // kilometer scale (x1000); informational
                            // only — GTFS itself allows any unit
                            // consistent between stop_times and shapes.
                            samplers.file("stop_times.txt").push(
                                &mut notices,
                                Notice::new("shape_dist_in_kilometers", Severity::Info)
                                    .with("tripId", clip(trip_id))
                                    .with("ratio", (ratio * 1e6).round() / 1e6),
                            );
                        }
                        predicted.shape_dist += 1;
                        continue;
                    }
                    None => {
                        // transitio-specific: predicts cafein's tier-1
                        // band rejection; not a canonical rule.
                        samplers.file("stop_times.txt").push(
                            &mut notices,
                            Notice::new("shape_dist_ratio_implausible", Severity::Warning)
                                .with("tripId", clip(trip_id))
                                .with("ratio", (ratio * 1e6).round() / 1e6)
                                .with("acceptedBands", "0.8..5 or 0.0008..0.005"),
                        );
                    }
                }
            }
        }

        // Tier 2 only for trips that fell past tier 1 and have a shape.
        let Some(line) = shape else {
            predicted.crow_fly += 1;
            continue;
        };
        // cafein's chord rejection: the shape must carry strictly more
        // vertices than the trip has stops.
        if line.len() <= positions.len() {
            if chord_shapes.insert(*shape_id) {
                // transitio-specific: mirrors cafein's tier-2 density
                // rule, not a canonical rule.
                samplers.file("shapes.txt").push(
                    &mut notices,
                    Notice::new("chord_only_shape", Severity::Warning)
                        .with("shapeId", clip(shape_id))
                        .with("tripId", clip(trip_id)),
                );
            }
            predicted.crow_fly += 1;
            continue;
        }
        // A structured key: delimiter-joined ids could collide when an
        // id itself contains the delimiter.
        let key = (
            (*shape_id).to_string(),
            stops
                .iter()
                .map(|s| s.stop_id.to_string())
                .collect::<Vec<String>>(),
        );
        let tier2 = match tier2_cache.get(&key) {
            Some(cached) => cached,
            None => {
                let cost = (zones.len() as u64)
                    .saturating_mul(positions.len() as u64)
                    .saturating_mul((line.len() - 1) as u64);
                if exhausted || cost > budget {
                    // Deterministic sorted-prefix: the first pattern
                    // that no longer fits ends all uncached tier-2
                    // work; cached outcomes stay reusable.
                    exhausted = true;
                    unprocessed += 1;
                    continue;
                }
                budget -= cost;
                tier2_cache
                    .entry(key.clone())
                    .or_insert_with(|| evaluate_tier2(&zones, line, &positions))
            }
        };
        match tier2.pass {
            None => unprocessed += 1,
            Some(true) => predicted.shape_linref += 1,
            Some(false) => {
                for (stop, distance) in &tier2.far_stops {
                    // transitio-specific: mirrors cafein's tier-2 snap
                    // tolerance, not a canonical rule; fires only when
                    // every candidate zone agrees.
                    samplers.file("stop_times.txt").push(
                        &mut notices,
                        Notice::new("stop_far_from_shape", Severity::Warning)
                            .with("tripId", clip(trip_id))
                            .with("stopId", clip(stops[*stop].stop_id))
                            .with("shapeId", clip(shape_id))
                            .with("toleranceMeters", SNAP_TOLERANCE)
                            .with("distanceMeters", (distance * 100.0).round() / 100.0),
                    );
                }
                predicted.crow_fly += 1;
            }
        }
    }

    if without_shapes > 0 {
        samplers.file("trips.txt").push(
            &mut notices,
            // transitio-specific: shapeless trips have no tier-2
            // fallback — usable shape_dist_traveled still resolves at
            // tier 1, anything else drops to crow-fly.
            Notice::new("trips_without_shapes", Severity::Info).with("tripCount", without_shapes),
        );
    }

    samplers.finish(&mut notices);
    result.notices.append(&mut notices);

    let trips_total = trip_shapes.len() as u64;
    let counted = predicted.shape_dist + predicted.shape_linref + predicted.crow_fly;
    let verdict = if counted == 0 {
        None
    } else if predicted.crow_fly == 0 {
        Some("full")
    } else if predicted.shape_dist + predicted.shape_linref == 0 {
        Some("straight_line")
    } else {
        Some("partial")
    };
    Some(DistanceReadiness {
        trips: trips_total,
        skipped,
        unprocessed,
        predicted,
        verdict,
        cafein: CAFEIN_VERSION,
    })
}

fn nonblank(value: &str) -> Option<&str> {
    (!value.trim().is_empty()).then_some(value)
}

/// The shared v1/v2 acceptance rule: a finite non-negative amount and a
/// three-ASCII-letter currency.
fn priceable_amount(value: &str) -> bool {
    matches!(value.trim().parse::<f64>(), Ok(v) if v.is_finite() && v >= 0.0)
}

fn priceable_currency(value: &str) -> bool {
    let trimmed = value.trim();
    trimmed.len() == 3 && trimmed.bytes().all(|b| b.is_ascii_alphabetic())
}

/// Per-fare grants under cafein's row→grant mapping (fares.py, verified
/// 2026-08-05): contains_id joins the fare's one zone set whatever else
/// its row carries; a row with either endpoint adds one OD clause with
/// exactly its present fields; a row whose only field is route_id joins
/// the route grant; a fare with no grants is unrestricted.
#[derive(Default)]
struct Grants {
    routes: HashSet<u32>,
    od: Vec<(Option<u32>, Option<u32>, Option<u32>)>,
    zones: HashSet<u32>,
}

impl Grants {
    fn unrestricted(&self) -> bool {
        self.routes.is_empty() && self.od.is_empty() && self.zones.is_empty()
    }
}

/// Interns an id: hashed once here, compared as a compact number
/// afterwards, so a hostile very-long id cannot multiply per-pair work
/// under the compatibility budget.
fn intern<'t>(numbers: &mut HashMap<&'t str, u32>, id: &'t str) -> u32 {
    let next = numbers.len() as u32;
    *numbers.entry(id).or_insert(next)
}

/// Predicts whether cafein can price journeys on the feed. Coarse
/// route-level compatibility, not a per-journey guarantee; `cap` bounds
/// the (route, fare) evaluations (a parameter so tests exhaust it).
fn fare_readiness(
    result: &mut ScanResult,
    options: &ScanOptions,
    cap: u64,
) -> Option<FareReadiness> {
    // The multi-agency `blocked` detection reads agency.txt, so its
    // truncation must yield the null section like the fare tables'.
    for file in [
        "fare_attributes.txt",
        "fare_rules.txt",
        "fare_products.txt",
        "fare_leg_rules.txt",
        "fare_transfer_rules.txt",
        "agency.txt",
        "routes.txt",
        "stops.txt",
        "trips.txt",
        "stop_times.txt",
    ] {
        if result.incomplete.contains(file) {
            return None;
        }
    }
    let tables = &result.tables;
    // The five core tables must be PRESENT, not merely un-truncated —
    // a feed missing them must not draw a fare verdict; the fare
    // tables themselves stay optional.
    for file in [
        "agency.txt",
        "routes.txt",
        "stops.txt",
        "trips.txt",
        "stop_times.txt",
    ] {
        tables.get(file)?;
    }
    let mut notices = Vec::new();
    let mut samplers = Samplers::new(options.max_notices_per_file);

    // Agency universe and per-route owners, cafein semantics: a blank
    // owner falls back to a sole agency; values compare verbatim.
    // Multi-agency means multiple DISTINCT agency_id values (blank as
    // one value), exactly like cafein's `agencies` set — counting rows
    // instead would predict `blocked` for feeds cafein accepts.
    let mut agencies: HashSet<Option<&str>> = HashSet::new();
    if let Some(table) = tables.get("agency.txt") {
        for row in &table.rows {
            agencies.insert(nonblank(cell(table, row, "agency_id")));
        }
    }
    let multi_agency = agencies.len() > 1;
    let mut agency_numbers: HashMap<&str, u32> = HashMap::new();
    let sole_agency: Option<u32> = if agencies.len() == 1 {
        agencies
            .iter()
            .next()
            .copied()
            .flatten()
            .map(|a| intern(&mut agency_numbers, a))
    } else {
        None
    };
    let mut agency_of_route: HashMap<&str, Option<u32>> = HashMap::new();
    if let Some(table) = tables.get("routes.txt") {
        for row in &table.rows {
            if let Some(route) = nonblank(cell(table, row, "route_id")) {
                agency_of_route.insert(
                    route,
                    nonblank(cell(table, row, "agency_id")).map(|a| intern(&mut agency_numbers, a)),
                );
            }
        }
    }

    struct FareProduct<'t> {
        id: &'t str,
        priceable: bool,
        agency: Option<u32>,
        transfers_present: bool,
    }
    let mut v1_fares: Vec<FareProduct> = Vec::new();
    let mut agency_ambiguous = false;
    if let Some(table) = tables.get("fare_attributes.txt") {
        let sampler = samplers.file("fare_attributes.txt");
        for row in &table.rows {
            let id = cell(table, row, "fare_id");
            let priceable = priceable_amount(cell(table, row, "price"))
                && priceable_currency(cell(table, row, "currency_type"));
            if !priceable {
                // transitio-specific: cafein cannot price this product.
                sampler.push(
                    &mut notices,
                    Notice::new("fare_attribute_not_priceable", Severity::Warning)
                        .with("fareId", clip(id))
                        .with("csvRowNumber", row.csv_row),
                );
            }
            let agency =
                nonblank(cell(table, row, "agency_id")).map(|a| intern(&mut agency_numbers, a));
            if multi_agency && agency.is_none() {
                agency_ambiguous = true;
                // transitio-specific: cafein rejects the whole feed
                // when a multi-agency fare names no agency.
                sampler.push(
                    &mut notices,
                    Notice::new("fare_without_agency_id", Severity::Warning)
                        .with("fareId", clip(id))
                        .with("csvRowNumber", row.csv_row),
                );
            }
            let transfers = cell(table, row, "transfers").trim();
            let duration = cell(table, row, "transfer_duration").trim();
            v1_fares.push(FareProduct {
                id,
                priceable,
                agency,
                transfers_present: matches!(transfers, "0" | "1" | "2")
                    || (!duration.is_empty() && duration.parse::<u64>().is_ok()),
            });
        }
    }

    let mut route_numbers: HashMap<&str, u32> = HashMap::new();
    let mut zone_numbers: HashMap<&str, u32> = HashMap::new();
    let mut grants: HashMap<&str, Grants> = HashMap::new();
    if let Some(table) = tables.get("fare_rules.txt") {
        for row in &table.rows {
            let Some(fare) = nonblank(cell(table, row, "fare_id")) else {
                continue;
            };
            let contains = nonblank(cell(table, row, "contains_id"));
            let origin = nonblank(cell(table, row, "origin_id"));
            let destination = nonblank(cell(table, row, "destination_id"));
            let route = nonblank(cell(table, row, "route_id"));
            let zone = contains.map(|z| intern(&mut zone_numbers, z));
            let origin = origin.map(|z| intern(&mut zone_numbers, z));
            let destination = destination.map(|z| intern(&mut zone_numbers, z));
            let route = route.map(|r| intern(&mut route_numbers, r));
            let entry = grants.entry(fare).or_default();
            if let Some(zone) = zone {
                entry.zones.insert(zone);
            }
            if origin.is_some() || destination.is_some() {
                entry.od.push((origin, destination, route));
            } else if let (Some(route), None) = (route, zone) {
                entry.routes.insert(route);
            }
        }
    }

    // Served zone sets per route with at least one usable trip — the
    // compatibility denominator.
    let mut stop_zone: HashMap<&str, u32> = HashMap::new();
    if let Some(table) = tables.get("stops.txt") {
        for row in &table.rows {
            if let Some(zone) = nonblank(cell(table, row, "zone_id")) {
                let zone = intern(&mut zone_numbers, zone);
                stop_zone.insert(cell(table, row, "stop_id"), zone);
            }
        }
    }
    let mut trip_route: HashMap<&str, &str> = HashMap::new();
    if let Some(table) = tables.get("trips.txt") {
        for row in &table.rows {
            if let (Some(trip), Some(route)) = (
                nonblank(cell(table, row, "trip_id")),
                nonblank(cell(table, row, "route_id")),
            ) {
                trip_route.insert(trip, route);
            }
        }
    }
    let mut trip_stops: HashMap<&str, Vec<&str>> = HashMap::new();
    if let Some(table) = tables.get("stop_times.txt") {
        for row in &table.rows {
            trip_stops
                .entry(cell(table, row, "trip_id"))
                .or_default()
                .push(cell(table, row, "stop_id"));
        }
    }
    let mut route_zones: BTreeMap<&str, HashSet<u32>> = BTreeMap::new();
    for (trip, stops) in &trip_stops {
        if stops.len() < 2 {
            continue;
        }
        if let Some(route) = trip_route.get(trip) {
            let zones = route_zones.entry(route).or_default();
            for stop in stops {
                if let Some(zone) = stop_zone.get(stop) {
                    zones.insert(*zone);
                }
            }
        }
    }

    // Deterministic evaluation: routes in sorted order, fares in file
    // order, grants in fixed order; each pair's precomputed cost is
    // charged whether or not a grant matches.
    // Each fare's grants and cost resolve ONCE here (one fare-id hash
    // total), and every id inside the loop is an interned number, so a
    // charged unit is genuinely constant-cost.
    let fare_data: Vec<(Option<u32>, Option<&Grants>, u64)> = v1_fares
        .iter()
        .filter(|f| f.priceable)
        .map(|fare| {
            let fare_grants = grants.get(fare.id);
            let cost = 2 + fare_grants.map_or(0, |g| (g.od.len() + g.zones.len()) as u64);
            (fare.agency, fare_grants, cost)
        })
        .collect();
    let mut checks = 0u64;
    let mut exhausted = false;
    let mut compatible = 0usize;
    'routes: for (route, zones_r) in &route_zones {
        let route_num = route_numbers.get(*route).copied();
        let owner = agency_of_route
            .get(*route)
            .copied()
            .flatten()
            .or(sole_agency);
        let mut covered = false;
        for (fare_agency, fare_grants, cost) in &fare_data {
            // The pair's cost is known before evaluation and covers
            // every comparison its grants can cause (base + route
            // grant + each OD clause + each zone), so the cap bounds
            // real work and stays independent of match timing.
            if checks.saturating_add(*cost) > cap {
                exhausted = true;
                break 'routes;
            }
            checks += *cost;
            if let Some(agency) = fare_agency {
                if owner != Some(*agency) {
                    continue;
                }
            }
            let Some(fare_grants) = fare_grants else {
                covered = true; // no rule rows: unrestricted
                break;
            };
            if fare_grants.unrestricted() {
                covered = true;
                break;
            }
            if route_num.is_some_and(|r| fare_grants.routes.contains(&r)) {
                covered = true;
                break;
            }
            if fare_grants
                .od
                .iter()
                .any(|(origin, destination, od_route)| {
                    od_route.is_none_or(|r| Some(r) == route_num)
                        && origin.is_none_or(|z| zones_r.contains(&z))
                        && destination.is_none_or(|z| zones_r.contains(&z))
                })
            {
                covered = true;
                break;
            }
            if fare_grants.zones.iter().any(|z| zones_r.contains(z)) {
                covered = true;
                break;
            }
        }
        if covered {
            compatible += 1;
        }
    }
    let route_compatibility = if exhausted || route_zones.is_empty() {
        None
    } else {
        Some(compatible as f64 / route_zones.len() as f64)
    };
    let fully_compatible = !exhausted && !route_zones.is_empty() && compatible == route_zones.len();

    let (mut products, mut v2_priceable) = (0u64, 0u64);
    if let Some(table) = tables.get("fare_products.txt") {
        for row in &table.rows {
            products += 1;
            if priceable_amount(cell(table, row, "amount"))
                && priceable_currency(cell(table, row, "currency"))
            {
                v2_priceable += 1;
            }
        }
    }
    let leg_rules = tables
        .get("fare_leg_rules.txt")
        .is_some_and(|t| !t.rows.is_empty());
    let transfer_rules = tables
        .get("fare_transfer_rules.txt")
        .is_some_and(|t| !t.rows.is_empty());
    let v2_present = products > 0 || leg_rules || transfer_rules;

    let priceable = v1_fares.iter().filter(|f| f.priceable).count() as u64;
    if v1_fares.is_empty() && products == 0 {
        // transitio-specific: no monetary information for cafein at all.
        samplers.file("fare_attributes.txt").push(
            &mut notices,
            Notice::new("no_fare_information", Severity::Info),
        );
    }
    if let Some(share) = route_compatibility {
        // Any v1 fare rows count as "fares exist" — a feed whose fares
        // are all unpriceable still deserves the aggregate warning.
        if !v1_fares.is_empty() && share < FARE_COVERAGE_WARN_SHARE {
            // transitio-specific: fares exist but cover almost nothing.
            samplers.file("fare_attributes.txt").push(
                &mut notices,
                Notice::new("partial_fare_coverage", Severity::Warning)
                    .with("routeCompatibility", (share * 1e4).round() / 1e4)
                    .with("threshold", FARE_COVERAGE_WARN_SHARE),
            );
        }
    }

    samplers.finish(&mut notices);
    result.notices.append(&mut notices);

    // The verdict means "cafein can ingest the fare structure and every
    // used route has a compatible priceable fare" — never a per-journey
    // priceability promise.
    let verdict = if agency_ambiguous {
        "blocked"
    } else if priceable == 0 {
        "absent"
    } else if fully_compatible {
        "computable"
    } else {
        "partial"
    };
    Some(FareReadiness {
        v1: V1Fares {
            fares: v1_fares.len() as u64,
            priceable,
            route_compatibility,
            agency_ambiguous,
        },
        v2: V2Fares {
            present: v2_present,
            products,
            priceable: v2_priceable,
            leg_rules,
            transfer_rules,
        },
        transfer_pricing: if v1_fares.iter().any(|f| f.priceable && f.transfers_present) {
            "present"
        } else {
            "absent"
        },
        verdict,
    })
}

#[cfg(test)]
mod tests {
    use std::io::Cursor;

    use super::*;
    use crate::scan::scan_reader;

    fn scan_zip(files: &[(&str, &str)]) -> ScanResult {
        let mut cursor = Cursor::new(Vec::new());
        {
            let mut writer = zip::ZipWriter::new(&mut cursor);
            let options = zip::write::SimpleFileOptions::default();
            for (name, content) in files {
                writer.start_file(*name, options).unwrap();
                std::io::Write::write_all(&mut writer, content.as_bytes()).unwrap();
            }
            writer.finish().unwrap();
        }
        cursor.set_position(0);
        scan_reader(cursor).unwrap()
    }

    fn readiness_with_cap(
        files: &[(&str, &str)],
        cap: u64,
    ) -> (Option<DistanceReadiness>, Vec<Notice>) {
        let mut result = scan_zip(files);
        let options = ScanOptions::default();
        let distances = distance_readiness(&mut result, &options, cap);
        (distances, result.notices)
    }

    fn readiness(files: &[(&str, &str)]) -> (Option<DistanceReadiness>, Vec<Notice>) {
        readiness_with_cap(files, MAX_TIER2_POINT_CHECKS)
    }

    // Two Helsinki stops ~630 m apart along an east-west street.
    fn base() -> Vec<(&'static str, &'static str)> {
        vec![
            (
                "agency.txt",
                "agency_id,agency_name,agency_url,agency_timezone\nhsl,HSL,https://hsl.fi,Europe/Helsinki\n",
            ),
            (
                "stops.txt",
                "stop_id,stop_name,stop_lat,stop_lon\ns1,A,60.1700,24.9310\ns2,B,60.1700,24.9424\n",
            ),
            (
                "routes.txt",
                "route_id,agency_id,route_short_name,route_type\nr1,hsl,1,3\n",
            ),
            (
                "calendar.txt",
                "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\nwk,1,1,1,1,1,1,1,20260101,20261231\n",
            ),
        ]
    }

    fn crow() -> f64 {
        haversine((60.17, 24.931), (60.17, 24.9424))
    }

    #[test]
    fn valid_shape_dist_needs_no_shape() {
        let mut files = base();
        let meters = crow() * 1.2;
        let stop_times: &'static str = Box::leak(
            format!(
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence,shape_dist_traveled\nt1,08:00:00,08:00:00,s1,1,0\nt1,08:05:00,08:05:00,s2,2,{meters:.1}\n"
            )
            .into_boxed_str(),
        );
        files.push(("trips.txt", "route_id,service_id,trip_id\nr1,wk,t1\n"));
        files.push(("stop_times.txt", stop_times));
        let (distances, notices) = readiness(&files);
        let distances = distances.unwrap();
        assert_eq!(distances.predicted.shape_dist, 1);
        assert_eq!(distances.verdict, Some("full"));
        assert!(notices
            .iter()
            .all(|n| n.code != "shape_dist_ratio_implausible"));
    }

    #[test]
    fn kilometer_scale_is_detected_and_accepted() {
        let mut files = base();
        let km = crow() * 1.2 / 1000.0;
        let stop_times: &'static str = Box::leak(
            format!(
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence,shape_dist_traveled\nt1,08:00:00,08:00:00,s1,1,0\nt1,08:05:00,08:05:00,s2,2,{km:.6}\n"
            )
            .into_boxed_str(),
        );
        files.push(("trips.txt", "route_id,service_id,trip_id\nr1,wk,t1\n"));
        files.push(("stop_times.txt", stop_times));
        let (distances, notices) = readiness(&files);
        assert_eq!(distances.unwrap().predicted.shape_dist, 1);
        assert!(notices.iter().any(|n| n.code == "shape_dist_in_kilometers"));
    }

    #[test]
    fn ratio_band_edges_decide_membership() {
        // Just inside and just outside the inclusive [0.8, 5] band;
        // the margins dwarf the decimal-formatting noise.
        for (factor, inside) in [
            (0.8 * 1.000_001, true),
            (5.0 * 0.999_999, true),
            (0.8 * 0.999, false),
            (5.0 * 1.001, false),
        ] {
            let mut files = base();
            let meters = crow() * factor;
            let stop_times: &'static str = Box::leak(
                format!(
                    "trip_id,arrival_time,departure_time,stop_id,stop_sequence,shape_dist_traveled\nt1,08:00:00,08:00:00,s1,1,0\nt1,08:05:00,08:05:00,s2,2,{meters:.6}\n"
                )
                .into_boxed_str(),
            );
            files.push(("trips.txt", "route_id,service_id,trip_id\nr1,wk,t1\n"));
            files.push(("stop_times.txt", stop_times));
            let (distances, _) = readiness(&files);
            let expected = if inside { 1 } else { 0 };
            assert_eq!(
                distances.unwrap().predicted.shape_dist,
                expected,
                "factor {factor}"
            );
        }
    }

    #[test]
    fn implausible_ratio_is_flagged_and_falls_through() {
        let mut files = base();
        let meters = crow() * 12.0; // outside both bands
        let stop_times: &'static str = Box::leak(
            format!(
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence,shape_dist_traveled\nt1,08:00:00,08:00:00,s1,1,0\nt1,08:05:00,08:05:00,s2,2,{meters:.1}\n"
            )
            .into_boxed_str(),
        );
        files.push(("trips.txt", "route_id,service_id,trip_id\nr1,wk,t1\n"));
        files.push(("stop_times.txt", stop_times));
        let (distances, notices) = readiness(&files);
        let distances = distances.unwrap();
        assert_eq!(distances.predicted.crow_fly, 1);
        assert_eq!(distances.verdict, Some("straight_line"));
        assert!(notices
            .iter()
            .any(|n| n.code == "shape_dist_ratio_implausible"));
        assert!(notices.iter().any(|n| n.code == "trips_without_shapes"));
    }

    #[test]
    fn ratio_bands_are_inclusive_at_exact_edges() {
        assert_eq!(ratio_scale(0.8), Some(1.0));
        assert_eq!(ratio_scale(5.0), Some(1.0));
        assert_eq!(ratio_scale(0.8e-3), Some(1000.0));
        assert_eq!(ratio_scale(5e-3), Some(1000.0));
        assert_eq!(ratio_scale(0.799_999_9), None);
        assert_eq!(ratio_scale(5.000_000_1), None);
        assert_eq!(ratio_scale(0.000_799_99), None);
        assert_eq!(ratio_scale(0.005_000_1), None);
        assert_eq!(ratio_scale(0.1), None); // the gap between the bands
    }

    #[test]
    fn non_decreasing_ties_pass_tier_one() {
        let mut files = base();
        let stops_file = "stop_id,stop_name,stop_lat,stop_lon\ns1,A,60.1700,24.9310\nsm,M,60.1700,24.9367\ns2,B,60.1700,24.9424\n";
        // The tie sits at the end; the total still lands in the band.
        let total = (haversine((60.17, 24.931), (60.17, 24.9367))
            + haversine((60.17, 24.9367), (60.17, 24.9424)))
            * 1.2;
        let stop_times: &'static str = Box::leak(
            format!(
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence,shape_dist_traveled\nt1,08:00:00,08:00:00,s1,1,0\nt1,08:02:00,08:02:00,sm,2,{total:.4}\nt1,08:05:00,08:05:00,s2,3,{total:.4}\n"
            )
            .into_boxed_str(),
        );
        let mut swapped = Vec::new();
        for (name, content) in files.drain(..) {
            if name == "stops.txt" {
                swapped.push(("stops.txt", stops_file));
            } else {
                swapped.push((name, content));
            }
        }
        files = swapped;
        files.push(("trips.txt", "route_id,service_id,trip_id\nr1,wk,t1\n"));
        files.push(("stop_times.txt", stop_times));
        let (distances, _) = readiness(&files);
        assert_eq!(distances.unwrap().predicted.shape_dist, 1);
    }

    #[test]
    fn single_stop_trip_predicts_crow_fly() {
        let mut files = base();
        files.push(("trips.txt", "route_id,service_id,trip_id\nr1,wk,t1\n"));
        files.push((
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nt1,08:00:00,08:00:00,s1,1\n",
        ));
        let (distances, _) = readiness(&files);
        let distances = distances.unwrap();
        // cafein's crow-fly tier still succeeds with zero distance.
        assert_eq!(distances.predicted.crow_fly, 1);
        assert_eq!(distances.skipped, 0);
    }

    #[test]
    fn cap_boundary_processes_exact_fit_then_stops() {
        let mut files = base();
        files.push((
            "trips.txt",
            "route_id,service_id,trip_id,shape_id\nr1,wk,t1,sh1\nr1,wk,t2,sh2\n",
        ));
        files.push((
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nt1,08:00:00,08:00:00,s1,1\nt1,08:05:00,08:05:00,s2,2\nt2,09:00:00,09:00:00,s1,1\nt2,09:05:00,09:05:00,s2,2\n",
        ));
        files.push((
            "shapes.txt",
            "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\nsh1,60.1700,24.9310,1\nsh1,60.1700,24.9367,2\nsh1,60.1700,24.9424,3\nsh2,60.1700,24.9310,1\nsh2,60.1700,24.9367,2\nsh2,60.1700,24.9424,3\n",
        ));
        // One pattern costs zones(3) x stops(2) x segments(2) = 12: a
        // workload exactly fitting the cap completes; the next pattern
        // is counted unprocessed, never mis-tiered.
        let (distances, _) = readiness_with_cap(&files, 12);
        let distances = distances.unwrap();
        assert_eq!(distances.predicted.shape_linref, 1);
        assert_eq!(distances.unprocessed, 1);
        assert_eq!(distances.verdict, Some("full"));
    }

    fn shaped_files(shape: &'static str) -> Vec<(&'static str, &'static str)> {
        let mut files = base();
        files.push((
            "trips.txt",
            "route_id,service_id,trip_id,shape_id\nr1,wk,t1,sh1\n",
        ));
        files.push((
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nt1,08:00:00,08:00:00,s1,1\nt1,08:05:00,08:05:00,s2,2\n",
        ));
        files.push(("shapes.txt", shape));
        files
    }

    #[test]
    fn dense_shape_near_stops_predicts_linref() {
        let files = shaped_files(
            "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\nsh1,60.1700,24.9310,1\nsh1,60.1700,24.9367,2\nsh1,60.1700,24.9424,3\n",
        );
        let (distances, notices) = readiness(&files);
        let distances = distances.unwrap();
        assert_eq!(distances.predicted.shape_linref, 1);
        assert_eq!(distances.verdict, Some("full"));
        assert!(notices.iter().all(|n| n.code != "stop_far_from_shape"));
    }

    #[test]
    fn chord_only_shape_is_rejected_and_flagged() {
        // Two vertices for two stops: not strictly more.
        let files = shaped_files(
            "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\nsh1,60.1700,24.9310,1\nsh1,60.1700,24.9424,2\n",
        );
        let (distances, notices) = readiness(&files);
        assert_eq!(distances.unwrap().predicted.crow_fly, 1);
        let chords: Vec<_> = notices
            .iter()
            .filter(|n| n.code == "chord_only_shape")
            .collect();
        assert_eq!(chords.len(), 1);
    }

    #[test]
    fn far_stop_fails_tier_two_with_agreement() {
        // The shape runs ~330 m north of the stops (0.003 deg lat).
        let files = shaped_files(
            "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\nsh1,60.1730,24.9310,1\nsh1,60.1730,24.9367,2\nsh1,60.1730,24.9424,3\n",
        );
        let (distances, notices) = readiness(&files);
        assert_eq!(distances.unwrap().predicted.crow_fly, 1);
        assert!(notices.iter().any(|n| n.code == "stop_far_from_shape"));
    }

    #[test]
    fn snap_tolerance_boundary_matches_pyproj() {
        // Shape offset north of the stops: 0.00085 deg lat is 94.68 m
        // in UTM 35N per pyproj (EPSG:32635), 0.00095 deg is 105.82 m.
        let near = shaped_files(
            "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\nsh1,60.17085,24.9310,1\nsh1,60.17085,24.9367,2\nsh1,60.17085,24.9424,3\n",
        );
        let (distances, _) = readiness(&near);
        assert_eq!(distances.unwrap().predicted.shape_linref, 1);
        let far = shaped_files(
            "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\nsh1,60.17095,24.9310,1\nsh1,60.17095,24.9367,2\nsh1,60.17095,24.9424,3\n",
        );
        let (distances, _) = readiness(&far);
        assert_eq!(distances.unwrap().predicted.crow_fly, 1);
    }

    #[test]
    fn cap_exhaustion_counts_unprocessed_without_diagnostics() {
        let files = shaped_files(
            "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\nsh1,60.1730,24.9310,1\nsh1,60.1730,24.9367,2\nsh1,60.1730,24.9424,3\n",
        );
        let (distances, notices) = readiness_with_cap(&files, 1);
        let distances = distances.unwrap();
        assert_eq!(distances.unprocessed, 1);
        assert_eq!(distances.verdict, None);
        assert!(notices.iter().all(|n| n.code != "stop_far_from_shape"));
        assert!(notices.iter().all(|n| n.code != "chord_only_shape"));
    }

    #[test]
    fn cross_zone_feed_still_agrees() {
        // Stops straddle the 24 deg boundary between zones 34 and 35.
        let mut files = shaped_files(
            "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\nsh1,60.1700,23.9990,1\nsh1,60.1700,24.0000,2\nsh1,60.1700,24.0010,3\n",
        );
        let mut swapped = Vec::new();
        for (name, content) in files.drain(..) {
            if name == "stops.txt" {
                swapped.push((
                    "stops.txt",
                    "stop_id,stop_name,stop_lat,stop_lon\ns1,A,60.1700,23.9990\ns2,B,60.1700,24.0010\n",
                ));
            } else {
                swapped.push((name, content));
            }
        }
        files = swapped;
        let (distances, _) = readiness(&files);
        assert_eq!(distances.unwrap().predicted.shape_linref, 1);
    }

    #[test]
    fn missing_stop_coordinates_skip_the_trip() {
        let mut files = base();
        let mut swapped = Vec::new();
        for (name, content) in files.drain(..) {
            if name == "stops.txt" {
                swapped.push((
                    "stops.txt",
                    "stop_id,stop_name,stop_lat,stop_lon\ns1,A,60.1700,24.9310\ns2,B,,\n",
                ));
            } else {
                swapped.push((name, content));
            }
        }
        files = swapped;
        files.push(("trips.txt", "route_id,service_id,trip_id\nr1,wk,t1\n"));
        files.push((
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nt1,08:00:00,08:00:00,s1,1\nt1,08:05:00,08:05:00,s2,2\n",
        ));
        let (distances, _) = readiness(&files);
        let distances = distances.unwrap();
        assert_eq!(distances.skipped, 1);
        assert_eq!(distances.verdict, None);
    }

    #[test]
    fn disputed_stop_keeps_agreed_tier_and_stays_silent() {
        // Shape 0.000896 deg north of s1/s2: 99.85/99.80/100.03 m in
        // zones 34/35/36 per pyproj — the verdict differs by zone. The
        // detached middle stop is ~434 m out everywhere, so every zone
        // still fails tier 2: the tier is agreed, and only the
        // unanimously far stop may draw a diagnostic.
        let mut files = base();
        let mut swapped = Vec::new();
        for (name, content) in files.drain(..) {
            if name == "stops.txt" {
                swapped.push((
                    "stops.txt",
                    "stop_id,stop_name,stop_lat,stop_lon\ns1,A,60.1700,24.9310\nsm,M,60.1670,24.9367\ns2,B,60.1700,24.9424\n",
                ));
            } else {
                swapped.push((name, content));
            }
        }
        files = swapped;
        files.push((
            "trips.txt",
            "route_id,service_id,trip_id,shape_id\nr1,wk,t1,sh1\n",
        ));
        files.push((
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nt1,08:00:00,08:00:00,s1,1\nt1,08:02:00,08:02:00,sm,2\nt1,08:05:00,08:05:00,s2,3\n",
        ));
        files.push((
            "shapes.txt",
            "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\nsh1,60.170896,24.9310,1\nsh1,60.170896,24.9350,2\nsh1,60.170896,24.9390,3\nsh1,60.170896,24.9424,4\n",
        ));
        let (distances, notices) = readiness(&files);
        assert_eq!(distances.unwrap().predicted.crow_fly, 1);
        let far: Vec<_> = notices
            .iter()
            .filter(|n| n.code == "stop_far_from_shape")
            .collect();
        assert_eq!(far.len(), 1);
        assert_eq!(far[0].context["stopId"], "sm");
    }

    #[test]
    fn near_tolerance_boundary_passes_in_every_zone() {
        // 0.000893 deg is 99.51/99.47/99.70 m in zones 34/35/36 per
        // pyproj — inside the tolerance everywhere (exact equality is
        // unreachable through geodetic fixtures; the `<=` comparison
        // mirrors cafein's strict `>` failure).
        let files = shaped_files(
            "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\nsh1,60.170893,24.9310,1\nsh1,60.170893,24.9367,2\nsh1,60.170893,24.9424,3\n",
        );
        let (distances, _) = readiness(&files);
        assert_eq!(distances.unwrap().predicted.shape_linref, 1);
    }

    #[test]
    fn norway_exception_zone_is_candidate() {
        assert_eq!(zone_with_exceptions(60.0, 5.0), 32);
        assert_eq!(zone_with_exceptions(78.0, 15.0), 33);
        assert!(candidate_zones(60.0, 5.0).contains(&32));
    }

    #[test]
    fn projection_matches_pyproj_within_tolerance() {
        // EPSG:32635 forward of (60.17, 24.931): pyproj gives easting
        // 385201.1637 m, northing 6672142.3558 m; our offset-free
        // planar frame must agree on distances, checked via the span
        // between two projected points (pyproj: 632.7021 m).
        let projection = Transverse::zone(35);
        let a = projection.project(60.17, 24.931);
        let b = projection.project(60.17, 24.9424);
        let span = (a.0 - b.0).hypot(a.1 - b.1);
        assert!((span - 632.7021).abs() < 0.01, "span {span}");
    }

    fn fares_with_cap(files: &[(&str, &str)], cap: u64) -> (Option<FareReadiness>, Vec<Notice>) {
        let mut result = scan_zip(files);
        let options = ScanOptions::default();
        let fares = fare_readiness(&mut result, &options, cap);
        (fares, result.notices)
    }

    fn fares_of(files: &[(&str, &str)]) -> (Option<FareReadiness>, Vec<Notice>) {
        fares_with_cap(files, MAX_FARE_COMPAT_CHECKS)
    }

    fn upsert(
        files: &mut Vec<(&'static str, &'static str)>,
        name: &'static str,
        content: &'static str,
    ) {
        files.retain(|(existing, _)| *existing != name);
        files.push((name, content));
    }

    /// Two routes over three zoned stops: Z(r1) = {za, zb}, Z(r2) =
    /// {zb, zc}.
    fn fare_feed() -> Vec<(&'static str, &'static str)> {
        vec![
            (
                "agency.txt",
                "agency_id,agency_name,agency_url,agency_timezone\na1,One,https://one.fi,Europe/Helsinki\n",
            ),
            (
                "stops.txt",
                "stop_id,stop_name,stop_lat,stop_lon,zone_id\ns1,A,60.1700,24.9310,za\ns2,B,60.1700,24.9424,zb\ns3,C,60.1800,24.9500,zc\n",
            ),
            (
                "routes.txt",
                "route_id,agency_id,route_short_name,route_type\nr1,a1,1,3\nr2,a1,2,3\n",
            ),
            (
                "calendar.txt",
                "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\nwk,1,1,1,1,1,1,1,20260101,20261231\n",
            ),
            ("trips.txt", "route_id,service_id,trip_id\nr1,wk,t1\nr2,wk,t2\n"),
            (
                "stop_times.txt",
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nt1,08:00:00,08:00:00,s1,1\nt1,08:05:00,08:05:00,s2,2\nt2,09:00:00,09:00:00,s2,1\nt2,09:05:00,09:05:00,s3,2\n",
            ),
        ]
    }

    fn multi_agency_feed() -> Vec<(&'static str, &'static str)> {
        let mut files = fare_feed();
        upsert(
            &mut files,
            "agency.txt",
            "agency_id,agency_name,agency_url,agency_timezone\na1,One,https://one.fi,Europe/Helsinki\na2,Two,https://two.fi,Europe/Helsinki\n",
        );
        upsert(
            &mut files,
            "routes.txt",
            "route_id,agency_id,route_short_name,route_type\nr1,a1,1,3\nr2,a2,2,3\n",
        );
        files
    }

    #[test]
    fn priceable_rule_edges() {
        let mut files = fare_feed();
        upsert(
            &mut files,
            "fare_attributes.txt",
            "fare_id,price,currency_type,payment_method\nf1,,EUR,0\nf2,-0.5,EUR,0\nf3,inf,EUR,0\nf4,NaN,EUR,0\nf5,2.5,EU,0\nf6,2.5,EURO,0\nf7,2.5,EÜR,0\nf8,2.50,EUR,0\n",
        );
        let (fares, notices) = fares_of(&files);
        let fares = fares.unwrap();
        assert_eq!(fares.v1.fares, 8);
        assert_eq!(fares.v1.priceable, 1);
        let flagged = notices
            .iter()
            .filter(|n| n.code == "fare_attribute_not_priceable")
            .count();
        assert_eq!(flagged, 7);
        // The one priceable fare has no rules: unrestricted, unscoped.
        assert_eq!(fares.v1.route_compatibility, Some(1.0));
        assert_eq!(fares.verdict, "computable");
    }

    #[test]
    fn contains_bearing_row_grants_its_zone_never_the_route() {
        let mut files = fare_feed();
        upsert(
            &mut files,
            "fare_attributes.txt",
            "fare_id,price,currency_type,payment_method\nfz,2.50,EUR,0\n",
        );
        // The row names r2, but per cafein a contains-bearing row
        // contributes its zone alone: r1 is covered via za, r2 is not.
        upsert(
            &mut files,
            "fare_rules.txt",
            "fare_id,route_id,origin_id,destination_id,contains_id\nfz,r2,,,za\n",
        );
        let (fares, _) = fares_of(&files);
        let fares = fares.unwrap();
        assert_eq!(fares.v1.route_compatibility, Some(0.5));
        assert_eq!(fares.verdict, "partial");
    }

    #[test]
    fn mixed_grant_rows_cover_both_routes() {
        let mut files = fare_feed();
        upsert(
            &mut files,
            "fare_attributes.txt",
            "fare_id,price,currency_type,payment_method\nfz,2.50,EUR,0\nfr,3.00,EUR,0\n",
        );
        // fz: zone grant za (contains+route row); fr: route grant r2.
        upsert(
            &mut files,
            "fare_rules.txt",
            "fare_id,route_id,origin_id,destination_id,contains_id\nfz,r1,,,za\nfr,r2,,,\n",
        );
        let (fares, _) = fares_of(&files);
        let fares = fares.unwrap();
        assert_eq!(fares.v1.route_compatibility, Some(1.0));
        assert_eq!(fares.verdict, "computable");
    }

    #[test]
    fn od_clause_needs_served_zones_and_matching_route() {
        let mut files = fare_feed();
        upsert(
            &mut files,
            "fare_attributes.txt",
            "fare_id,price,currency_type,payment_method\nfo,2.50,EUR,0\n",
        );
        // Origin zc is never served by r1, and the clause binds to r1,
        // so neither route is covered.
        upsert(
            &mut files,
            "fare_rules.txt",
            "fare_id,route_id,origin_id,destination_id,contains_id\nfo,r1,zc,zc,\n",
        );
        let (fares, notices) = fares_of(&files);
        let fares = fares.unwrap();
        assert_eq!(fares.v1.route_compatibility, Some(0.0));
        assert_eq!(fares.verdict, "partial");
        assert!(notices.iter().any(|n| n.code == "partial_fare_coverage"));
    }

    #[test]
    fn unsatisfiable_fares_do_not_combine() {
        let mut files = fare_feed();
        upsert(
            &mut files,
            "fare_attributes.txt",
            "fare_id,price,currency_type,payment_method\nfa,2.50,EUR,0\nfb,2.50,EUR,0\n",
        );
        // Each clause needs za AND zc on one route; no route serves
        // both, and two fares never merge their zones.
        upsert(
            &mut files,
            "fare_rules.txt",
            "fare_id,route_id,origin_id,destination_id,contains_id\nfa,,za,zc,\nfb,,zc,za,\n",
        );
        let (fares, _) = fares_of(&files);
        assert_eq!(fares.unwrap().v1.route_compatibility, Some(0.0));
    }

    #[test]
    fn fare_id_only_row_is_unrestricted() {
        let mut files = fare_feed();
        upsert(
            &mut files,
            "fare_attributes.txt",
            "fare_id,price,currency_type,payment_method\nfu,2.50,EUR,0\n",
        );
        upsert(
            &mut files,
            "fare_rules.txt",
            "fare_id,route_id,origin_id,destination_id,contains_id\nfu,,,,\n",
        );
        let (fares, _) = fares_of(&files);
        let fares = fares.unwrap();
        assert_eq!(fares.v1.route_compatibility, Some(1.0));
        assert_eq!(fares.verdict, "computable");
    }

    #[test]
    fn agency_scoped_no_rules_fare_covers_only_its_routes() {
        let mut files = multi_agency_feed();
        upsert(
            &mut files,
            "fare_attributes.txt",
            "fare_id,price,currency_type,payment_method,transfers,agency_id\nf1,2.50,EUR,0,,a1\n",
        );
        let (fares, _) = fares_of(&files);
        let fares = fares.unwrap();
        // No network-wide fast path: agency a2's route stays uncovered.
        assert_eq!(fares.v1.route_compatibility, Some(0.5));
        assert_eq!(fares.verdict, "partial");
    }

    #[test]
    fn multi_agency_fare_without_agency_blocks() {
        let mut files = multi_agency_feed();
        upsert(
            &mut files,
            "fare_attributes.txt",
            "fare_id,price,currency_type,payment_method\nf1,2.50,EUR,0\n",
        );
        let (fares, notices) = fares_of(&files);
        let fares = fares.unwrap();
        assert!(fares.v1.agency_ambiguous);
        assert_eq!(fares.verdict, "blocked");
        assert!(notices.iter().any(|n| n.code == "fare_without_agency_id"));
    }

    #[test]
    fn v2_only_feed_is_absent_for_cafein() {
        let mut files = fare_feed();
        upsert(
            &mut files,
            "fare_products.txt",
            "fare_product_id,fare_product_name,amount,currency\np1,Ticket,2.50,EUR\np2,Broken,,EUR\n",
        );
        upsert(
            &mut files,
            "fare_leg_rules.txt",
            "leg_group_id,network_id,fare_product_id\nlg,net,p1\n",
        );
        let (fares, _) = fares_of(&files);
        let fares = fares.unwrap();
        assert!(fares.v2.present);
        assert_eq!(fares.v2.products, 2);
        assert_eq!(fares.v2.priceable, 1);
        assert!(fares.v2.leg_rules);
        assert!(!fares.v2.transfer_rules);
        assert_eq!(fares.verdict, "absent");
    }

    #[test]
    fn compat_budget_is_a_deterministic_prefix() {
        let mut files = fare_feed();
        upsert(
            &mut files,
            "fare_attributes.txt",
            "fare_id,price,currency_type,payment_method\nf1,2.50,EUR,0\n",
        );
        // Two routes x one unrestricted fare = two pairs of cost 2:
        // exactly fitting completes with the computed verdict...
        let (fares, _) = fares_with_cap(&files, 4);
        let fares = fares.unwrap();
        assert_eq!(fares.v1.route_compatibility, Some(1.0));
        assert_eq!(fares.verdict, "computable");
        // ...while an incomplete prefix yields null, never computable.
        let (fares, _) = fares_with_cap(&files, 3);
        let fares = fares.unwrap();
        assert_eq!(fares.v1.route_compatibility, None);
        assert_eq!(fares.verdict, "partial");
    }

    #[test]
    fn grant_order_within_a_fare_is_irrelevant() {
        for rules in [
            "fare_id,route_id,origin_id,destination_id,contains_id\nfz,r1,,,\nfz,,,,zd\nfz,r2,,,\n",
            "fare_id,route_id,origin_id,destination_id,contains_id\nfz,,,,zd\nfz,r2,,,\nfz,r1,,,\n",
        ] {
            let mut files = fare_feed();
            upsert(
                &mut files,
                "fare_attributes.txt",
                "fare_id,price,currency_type,payment_method\nfz,2.50,EUR,0\n",
            );
            upsert(&mut files, "fare_rules.txt", rules);
            let (fares, _) = fares_of(&files);
            let fares = fares.unwrap();
            assert_eq!(fares.v1.route_compatibility, Some(1.0));
            assert_eq!(fares.verdict, "computable");
        }
    }

    #[test]
    fn transfer_pricing_predicate() {
        for (columns, row, expected) in [
            (
                "fare_id,price,currency_type,payment_method,transfers",
                "f1,2.50,EUR,0,0",
                "present",
            ),
            (
                "fare_id,price,currency_type,payment_method,transfer_duration",
                "f1,2.50,EUR,0,5400",
                "present",
            ),
            (
                "fare_id,price,currency_type,payment_method,transfers",
                "f1,2.50,EUR,0,",
                "absent",
            ),
            (
                "fare_id,price,currency_type,payment_method",
                "f1,2.50,EUR,0",
                "absent",
            ),
            // An unpriceable fare cannot carry the transfer policy.
            (
                "fare_id,price,currency_type,payment_method,transfers",
                "f1,,EUR,0,0",
                "absent",
            ),
        ] {
            let mut files = fare_feed();
            let content: &'static str = Box::leak(format!("{columns}\n{row}\n").into_boxed_str());
            upsert(&mut files, "fare_attributes.txt", content);
            let (fares, _) = fares_of(&files);
            assert_eq!(fares.unwrap().transfer_pricing, expected, "{row}");
        }
    }

    #[test]
    fn missing_fares_are_reported_once() {
        let (fares, notices) = fares_of(&fare_feed());
        let fares = fares.unwrap();
        assert_eq!(fares.v1.fares, 0);
        assert_eq!(fares.verdict, "absent");
        let infos = notices
            .iter()
            .filter(|n| n.code == "no_fare_information")
            .count();
        assert_eq!(infos, 1);
    }

    #[test]
    fn all_unpriceable_fares_still_warn_on_coverage() {
        let mut files = fare_feed();
        upsert(
            &mut files,
            "fare_attributes.txt",
            "fare_id,price,currency_type,payment_method\nf1,,EUR,0\n",
        );
        let (fares, notices) = fares_of(&files);
        let fares = fares.unwrap();
        assert_eq!(fares.verdict, "absent");
        assert_eq!(fares.v1.route_compatibility, Some(0.0));
        assert!(notices
            .iter()
            .any(|n| n.code == "fare_attribute_not_priceable"));
        assert!(notices.iter().any(|n| n.code == "partial_fare_coverage"));
    }

    #[test]
    fn one_fare_with_mixed_rows_follows_the_truth_table() {
        let mut files = fare_feed();
        upsert(
            &mut files,
            "fare_attributes.txt",
            "fare_id,price,currency_type,payment_method\nfm,2.50,EUR,0\n",
        );
        // contains+route: zone za only; contains+OD: zone zc AND the
        // (za, zb, r2) clause; route-only: a grant for unused r9. r2 is
        // covered only because the contains+OD row contributed zc even
        // though its own OD clause fails (origin za unserved) — the
        // dual contribution the truth table requires — and nothing may
        // fabricate a cross-row combination.
        upsert(
            &mut files,
            "fare_rules.txt",
            "fare_id,route_id,origin_id,destination_id,contains_id\nfm,r1,,,za\nfm,r2,za,zb,zc\nfm,r9,,,\n",
        );
        let (fares, _) = fares_of(&files);
        assert_eq!(fares.unwrap().v1.route_compatibility, Some(1.0));
    }

    #[test]
    fn near_cap_exhaustion_is_order_independent() {
        // OD clauses are the one row-ordered grant category, so the
        // matching clause genuinely sits first versus last.
        for rules in [
            "fare_id,route_id,origin_id,destination_id,contains_id\nfz,,za,zb,\nfz,,zc,zc,\n",
            "fare_id,route_id,origin_id,destination_id,contains_id\nfz,,zc,zc,\nfz,,za,zb,\n",
        ] {
            let mut files = fare_feed();
            upsert(
                &mut files,
                "fare_attributes.txt",
                "fare_id,price,currency_type,payment_method\nfz,2.50,EUR,0\n",
            );
            upsert(&mut files, "fare_rules.txt", rules);
            // Each pair costs 2 + 2 OD clauses = 4: one of two routes
            // is evaluated when the cap hits and work remains, so the
            // result is null regardless of clause position.
            let (fares, _) = fares_with_cap(&files, 4);
            let fares = fares.unwrap();
            assert_eq!(fares.v1.route_compatibility, None);
            assert_eq!(fares.verdict, "partial");
        }
    }

    #[test]
    fn duplicate_or_blank_agency_rows_follow_cafein_not_row_counts() {
        // cafein counts DISTINCT agency_id values, so two blank-id rows
        // (or duplicated ids) are one agency to it and a fare without
        // agency_id is accepted; the prediction must mirror that,
        // however the rows read as GTFS.
        for agency_rows in [
            "agency_id,agency_name,agency_url,agency_timezone\n,One,https://one.fi,Europe/Helsinki\n,Two,https://two.fi,Europe/Helsinki\n",
            "agency_id,agency_name,agency_url,agency_timezone\na1,One,https://one.fi,Europe/Helsinki\na1,Two,https://two.fi,Europe/Helsinki\n",
        ] {
            let mut files = fare_feed();
            upsert(&mut files, "agency.txt", agency_rows);
            upsert(
                &mut files,
                "fare_attributes.txt",
                "fare_id,price,currency_type,payment_method\nf1,2.50,EUR,0\n",
            );
            let (fares, notices) = fares_of(&files);
            let fares = fares.unwrap();
            assert!(!fares.v1.agency_ambiguous);
            assert_ne!(fares.verdict, "blocked");
            assert!(notices.iter().all(|n| n.code != "fare_without_agency_id"));
        }
    }

    #[test]
    fn v2_priceable_boundaries() {
        for (amount, currency, priceable) in [
            ("2.50", "EUR", 1u64),
            ("", "EUR", 0),
            ("-0.5", "EUR", 0),
            ("inf", "EUR", 0),
            ("NaN", "EUR", 0),
            ("2.50", "EU", 0),
            ("2.50", "EURO", 0),
            ("2.50", "EÜR", 0),
        ] {
            let mut files = fare_feed();
            let content: &'static str = Box::leak(
                format!(
                    "fare_product_id,fare_product_name,amount,currency\np1,Ticket,{amount},{currency}\n"
                )
                .into_boxed_str(),
            );
            upsert(&mut files, "fare_products.txt", content);
            let (fares, _) = fares_of(&files);
            let fares = fares.unwrap();
            assert_eq!(fares.v2.products, 1);
            assert_eq!(fares.v2.priceable, priceable, "{amount} {currency}");
        }
    }

    #[test]
    fn rule_volume_counts_against_the_budget() {
        // 60 OD rows on one fare: each (route, fare) pair pre-charges
        // 2 + 60 units, so a 50-unit cap refuses to evaluate at all
        // and a rule-heavy feed cannot exceed the advertised bound.
        let mut files = fare_feed();
        upsert(
            &mut files,
            "fare_attributes.txt",
            "fare_id,price,currency_type,payment_method\nfz,2.50,EUR,0\n",
        );
        let mut rules = String::from("fare_id,route_id,origin_id,destination_id,contains_id\n");
        for i in 0..60 {
            rules.push_str(&format!("fz,,zx{i},zx{i},\n"));
        }
        let content: &'static str = Box::leak(rules.into_boxed_str());
        upsert(&mut files, "fare_rules.txt", content);
        let (fares, _) = fares_with_cap(&files, 50);
        let fares = fares.unwrap();
        assert_eq!(fares.v1.route_compatibility, None);
        assert_eq!(fares.verdict, "partial");
        // A cap covering the full 2 x 62 units completes.
        let (fares, _) = fares_with_cap(&files, 124);
        assert!(fares.unwrap().v1.route_compatibility.is_some());
    }

    #[test]
    fn very_long_ids_are_interned_once_and_stay_functional() {
        // Every id is hashed exactly once at interning and compared as
        // a number afterwards, so pathological id lengths cannot
        // multiply per-pair work under the budget; boundedness is
        // structural, and this asserts the interned path's semantics.
        let long_zone: &'static str =
            Box::leak(format!("z{}", "x".repeat(10_000)).into_boxed_str());
        let mut files = fare_feed();
        let stops: &'static str = Box::leak(
            format!(
                "stop_id,stop_name,stop_lat,stop_lon,zone_id\ns1,A,60.1700,24.9310,{long_zone}\ns2,B,60.1700,24.9424,zb\ns3,C,60.1800,24.9500,zc\n"
            )
            .into_boxed_str(),
        );
        upsert(&mut files, "stops.txt", stops);
        let fare_id: &'static str = Box::leak(format!("f{}", "y".repeat(10_000)).into_boxed_str());
        let attributes: &'static str = Box::leak(
            format!("fare_id,price,currency_type,payment_method\n{fare_id},2.50,EUR,0\n")
                .into_boxed_str(),
        );
        upsert(&mut files, "fare_attributes.txt", attributes);
        let rules: &'static str = Box::leak(
            format!(
                "fare_id,route_id,origin_id,destination_id,contains_id\n{fare_id},,,,{long_zone}\n"
            )
            .into_boxed_str(),
        );
        upsert(&mut files, "fare_rules.txt", rules);
        // The long zone covers r1 only: Z(r1) carries it, Z(r2) not.
        let (fares, _) = fares_of(&files);
        assert_eq!(fares.unwrap().v1.route_compatibility, Some(0.5));
    }

    #[test]
    fn missing_mandatory_tables_yield_no_fares_section() {
        for file in [
            "agency.txt",
            "routes.txt",
            "stops.txt",
            "trips.txt",
            "stop_times.txt",
        ] {
            let mut files = fare_feed();
            upsert(
                &mut files,
                "fare_attributes.txt",
                "fare_id,price,currency_type,payment_method\nf1,2.50,EUR,0\n",
            );
            files.retain(|(name, _)| *name != file);
            let (fares, _) = fares_of(&files);
            assert!(fares.is_none(), "{file}");
        }
    }

    #[test]
    fn any_incomplete_input_yields_no_fares_section() {
        for file in [
            "fare_attributes.txt",
            "fare_rules.txt",
            "fare_products.txt",
            "fare_leg_rules.txt",
            "fare_transfer_rules.txt",
            "stops.txt",
            "trips.txt",
            "stop_times.txt",
            "routes.txt",
        ] {
            let mut files = fare_feed();
            upsert(
                &mut files,
                "fare_attributes.txt",
                "fare_id,price,currency_type,payment_method\nf1,2.50,EUR,0\n",
            );
            let mut result = scan_zip(&files);
            result.incomplete.insert(file.to_string());
            let options = ScanOptions::default();
            assert!(
                fare_readiness(&mut result, &options, MAX_FARE_COMPAT_CHECKS).is_none(),
                "{file}"
            );
        }
    }

    #[test]
    fn incomplete_agency_table_yields_no_fares_section() {
        let mut files = fare_feed();
        upsert(
            &mut files,
            "fare_attributes.txt",
            "fare_id,price,currency_type,payment_method\nf1,2.50,EUR,0\n",
        );
        let mut result = scan_zip(&files);
        result.incomplete.insert("agency.txt".to_string());
        let options = ScanOptions::default();
        assert!(fare_readiness(&mut result, &options, MAX_FARE_COMPAT_CHECKS).is_none());
    }

    #[test]
    fn no_usable_routes_never_computable() {
        let mut files = fare_feed();
        // A single-stop trip keeps the tables present but leaves no
        // route with a usable trip.
        upsert(
            &mut files,
            "trips.txt",
            "route_id,service_id,trip_id\nr1,wk,t1\n",
        );
        upsert(
            &mut files,
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nt1,08:00:00,08:00:00,s1,1\n",
        );
        upsert(
            &mut files,
            "fare_attributes.txt",
            "fare_id,price,currency_type,payment_method\nf1,2.50,EUR,0\n",
        );
        let (fares, _) = fares_of(&files);
        let fares = fares.unwrap();
        assert_eq!(fares.v1.route_compatibility, None);
        assert_eq!(fares.verdict, "partial");
    }

    #[test]
    fn single_agency_blank_fare_agency_is_unscoped() {
        let mut files = fare_feed();
        upsert(
            &mut files,
            "fare_attributes.txt",
            "fare_id,price,currency_type,payment_method\nf1,2.50,EUR,0\n",
        );
        let (fares, notices) = fares_of(&files);
        let fares = fares.unwrap();
        assert!(!fares.v1.agency_ambiguous);
        assert_eq!(fares.verdict, "computable");
        assert!(notices.iter().all(|n| n.code != "fare_without_agency_id"));
    }

    #[test]
    fn incomplete_inputs_yield_no_verdict() {
        let mut files = base();
        files.push(("trips.txt", "route_id,service_id,trip_id\nr1,wk,t1\n"));
        files.push((
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nt1,08:00:00,08:00:00,s1,1\nt1,08:05:00,08:05:00,s2,2\n",
        ));
        let mut result = scan_zip(&files);
        result.incomplete.insert("shapes.txt".to_string());
        let options = ScanOptions::default();
        assert!(distance_readiness(&mut result, &options, MAX_TIER2_POINT_CHECKS).is_none());
    }
}
