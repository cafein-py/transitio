#!/usr/bin/env python3

"""Download the shared test datasets into tests/data/.

The datasets are the r5py sample data for the Helsinki region
(https://github.com/r5py/r5py.sampledata.helsinki), pinned by release tag
and SHA-256 so that beanpicker, cafein and r5py test against
byte-identical input files.
"""

import hashlib
import pathlib
import shutil
import sys
import time
import urllib.request

DOWNLOAD_ATTEMPTS = 3

BASE_URL = "https://github.com/r5py/r5py.sampledata.helsinki/raw/v1.1.1/data"

DATASETS = {
    "helsinki_gtfs.zip": (
        f"{BASE_URL}/helsinki_gtfs.zip",
        "8ecccde3e76441b47e90c7f311fc57a8d38df92e9ee592e8f440a9b7e3abf228",
    ),
    "kantakaupunki.osm.pbf": (
        f"{BASE_URL}/kantakaupunki.osm.pbf",
        "94f1a86cb8defaca4b6eea64fba699fde957a848151642b2ad2599bd5ad1e858",
    ),
}

DATA_DIRECTORY = pathlib.Path(__file__).parent.parent / "tests" / "data"


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as opened_file:
        for chunk in iter(lambda: opened_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(name, url, expected_sha256):
    target = DATA_DIRECTORY / name
    if target.exists() and sha256(target) == expected_sha256:
        print(f"{name}: cached")
        return
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            # Resume partial downloads: some proxies truncate long streams,
            # so each retry continues from the bytes already on disk.
            size_before = target.stat().st_size if target.exists() else 0
            resume_from = size_before
            request = urllib.request.Request(url)
            mode = "wb"
            if resume_from:
                request.add_header("Range", f"bytes={resume_from}-")
                mode = "ab"
            print(f"{name}: downloading (attempt {attempt}, from {resume_from})")
            with urllib.request.urlopen(request) as response:
                if resume_from and response.status != 206:
                    mode = "wb"  # server ignored the range; restart clean
                with open(target, mode) as opened_file:
                    shutil.copyfileobj(response, opened_file)
            actual = sha256(target)
            if actual == expected_sha256:
                return
            if attempt == DOWNLOAD_ATTEMPTS:
                target.unlink()
                raise RuntimeError(
                    f"checksum mismatch: expected {expected_sha256}, got {actual}"
                )
            if target.stat().st_size == size_before:
                # Nothing was appended: the cached bytes themselves are
                # corrupt or stale, not a truncated prefix — restart clean.
                target.unlink()
                print(f"{name}: cached bytes invalid, restarting", file=sys.stderr)
            else:
                print(f"{name}: incomplete, resuming", file=sys.stderr)
        except Exception as error:  # noqa: B902
            print(f"{name}: {error}", file=sys.stderr)
            if attempt == DOWNLOAD_ATTEMPTS:
                raise
            time.sleep(2 * attempt)


def main():
    DATA_DIRECTORY.mkdir(parents=True, exist_ok=True)
    for name, (url, expected) in DATASETS.items():
        fetch(name, url, expected)


if __name__ == "__main__":
    main()
