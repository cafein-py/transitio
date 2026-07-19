"""Benchmark beanpicker's validator against canonical reports.

Runs ``beanpicker.validate_feed`` over a corpus of feed zips, timing the
scan, and compares the notices against a canonical GTFS validator report
(the hosted Mobility Database report, or a local canonical-validator
run) when one sits next to a feed as ``<feed stem>.canonical.json``.

The project's reference corpus: the pinned Helsinki sample from
``scripts/fetch_test_data.py`` (``tests/data/helsinki_gtfs.zip``), a
national aggregate and a known-broken feed obtained via
``beanpicker.fetch`` or the catalog client; hosted reports downloaded
through ``MobilityDatabase.validation_report`` serve as the canonical
side. A sidecar may carry a top-level ``feedSha256`` naming the feed
zip's checksum; when present the pairing is verified (mismatch aborts),
when absent the results are marked as unverified pairing.

Usage::

    python scripts/benchmark_validator.py FEED.zip [FEED2.zip ...]
        [--runs 3] [--json OUT] [--max-notices 1000000]
"""

import argparse
import hashlib
import json
import pathlib
import sys
import time


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def benchmark_feed(path, *, runs=3, max_notices=1_000_000):
    """Time validation of one feed and compare with its canonical report.

    A canonical sidecar carrying a ``feedSha256`` key is verified against
    the feed's actual checksum (mismatch aborts); without the key the
    filename pairing is reported as unverified.
    """
    from beanpicker.report import build_report, parity_summary
    from beanpicker.validate import validate_feed

    if runs < 1:
        raise ValueError("runs must be >= 1")

    feed_sha256 = _sha256(path)
    feed_bytes = path.stat().st_size
    canonical = None
    pairing_verified = None
    sidecar = path.with_suffix(".canonical.json")
    if sidecar.exists():
        canonical = json.loads(sidecar.read_text(encoding="utf-8"))
        expected = canonical.get("feedSha256")
        if expected is not None and expected != feed_sha256:
            raise ValueError(
                f"{sidecar} names feed sha256 {expected}, "
                f"but {path} has {feed_sha256}"
            )
        pairing_verified = expected is not None

    timings = []
    validation = None
    for _ in range(runs):
        started = time.perf_counter()
        validation = validate_feed(path, max_notices_per_file=max_notices)
        timings.append(time.perf_counter() - started)

    if _sha256(path) != feed_sha256:
        raise ValueError(f"{path} changed during the benchmark")

    report = build_report(validation, hosted=canonical)

    result = {
        "feed": str(path),
        "feedSha256": feed_sha256,
        "bytes": feed_bytes,
        "runsSeconds": [round(t, 4) for t in timings],
        "bestSeconds": round(min(timings), 4),
        "rowTotal": sum(validation["row_counts"].values()),
        "counts": report["summary"]["counts"],
        "parity": parity_summary(report) if canonical is not None else None,
        "pairingVerified": pairing_verified,
    }
    return result


def _print_result(result):
    mib = result["bytes"] / (1 << 20)
    print(f"\n{result['feed']}")
    print(
        f"  {mib:.1f} MiB, {result['rowTotal']:,} rows, "
        f"best of {len(result['runsSeconds'])}: {result['bestSeconds']:.3f} s"
    )
    counts = result["counts"]
    print(
        f"  local notices: {counts['errors']} errors, "
        f"{counts['warnings']} warnings, {counts['infos']} infos"
    )
    parity = result["parity"]
    if parity is None:
        print("  no canonical report (expected <feed stem>.canonical.json)")
        return
    if not result["pairingVerified"]:
        print("  note: sidecar has no feedSha256, pairing unverified")
    print(
        f"  parity: {len(parity['agreeing'])} codes agree, "
        f"{len(parity['countDisagreements'])} disagree on counts, "
        f"{len(parity['localOnly'])} local-only, "
        f"{len(parity['hostedOnly'])} canonical-only"
    )
    for entry in parity["countDisagreements"]:
        print(
            f"    {entry['code']}: local {entry['local']} "
            f"vs canonical {entry['hosted']}"
        )
    if parity["localOnly"]:
        print(f"    local-only: {', '.join(parity['localOnly'])}")
    if parity["hostedOnly"]:
        print(f"    canonical-only: {', '.join(parity['hostedOnly'])}")
    if parity["countsAreLowerBounds"]:
        print("    note: local sampling active, counts are lower bounds")


def _positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("feeds", nargs="+", type=pathlib.Path, metavar="FEED.zip")
    parser.add_argument(
        "--runs", type=_positive, default=3, help="timing runs per feed"
    )
    parser.add_argument("--json", type=pathlib.Path, help="write results as JSON")
    parser.add_argument(
        "--max-notices",
        type=int,
        default=1_000_000,
        help="per-file notice cap; keep high so sampling does not distort parity",
    )
    args = parser.parse_args(argv)

    missing = [str(path) for path in args.feeds if not path.exists()]
    if missing:
        parser.error(f"feed not found: {', '.join(missing)}")

    if args.json:
        protected = set()
        for path in args.feeds:
            protected.add(path.resolve())
            protected.add(path.with_suffix(".canonical.json").resolve())
        if args.json.is_symlink() or args.json.resolve() in protected:
            parser.error(f"--json must not overwrite corpus files: {args.json}")

    results = []
    for path in args.feeds:
        result = benchmark_feed(path, runs=args.runs, max_notices=args.max_notices)
        results.append(result)
        _print_result(result)

    if args.json:
        args.json.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nresults written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
