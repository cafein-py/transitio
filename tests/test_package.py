import pytest


def test_public_names_importable():
    import transitio

    assert transitio.MobilityDatabase is not None
    assert transitio.Feed is not None
    assert transitio.Dataset is not None
    assert transitio.fetch is transitio.pipeline.fetch
    assert transitio.FetchResult is transitio.pipeline.FetchResult


def test_exceptions_hierarchy():
    import transitio
    from transitio.exceptions import (
        TransitioError,
        DownloadError,
        MissingTokenError,
    )

    assert issubclass(MissingTokenError, TransitioError)
    assert issubclass(DownloadError, TransitioError)
    assert transitio.exceptions.TransitioError is TransitioError


def test_unknown_attribute():
    import transitio

    with pytest.raises(AttributeError):
        transitio.does_not_exist


def test_version():
    pytest.importorskip("transitio._core")
    import transitio

    assert transitio.__version__
