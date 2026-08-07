import os
import pathlib

import pytest

DATA_DIRECTORY = pathlib.Path(__file__).parent / "data"


def _data_path(name):
    path = DATA_DIRECTORY / name
    if not path.exists():
        message = (
            f"{name} missing; run scripts/fetch_test_data.py to download "
            "the shared test datasets"
        )
        if os.environ.get("TRANSITIO_REQUIRE_TEST_DATA"):
            pytest.fail(message)
        pytest.skip(message)
    return path


@pytest.fixture(scope="session")
def helsinki_gtfs():
    return _data_path("helsinki_gtfs.zip")


@pytest.fixture(scope="session")
def kantakaupunki_pbf():
    return _data_path("kantakaupunki.osm.pbf")


@pytest.fixture(scope="session")
def transit_pbf():
    """The committed transit-only extract used by the shape tests."""
    return _data_path("helsinki-transit.osm.pbf")


@pytest.fixture(scope="session")
def helsinki_metro_pbf():
    """The metropolitan clip: generated locally, never fetched.

    Unlike the shared datasets, this one is not downloadable, so it
    skips when absent even under ``TRANSITIO_REQUIRE_TEST_DATA`` —
    that flag guards data CI is supposed to have.
    """
    path = DATA_DIRECTORY / "helsinki-metro.osm.pbf"
    if not path.exists():
        pytest.skip("helsinki-metro.osm.pbf is generated locally; not in CI")
    return path
