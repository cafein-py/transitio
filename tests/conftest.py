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
