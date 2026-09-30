"""HTTP client settings shared by transitio's downloads."""

import httpx

try:
    from transitio._core import __version__
except ImportError:  # the pure-Python modules import without the compiled core
    __version__ = "unknown"

USER_AGENT = f"transitio/{__version__} (+https://github.com/cafein-py/transitio)"


def client(*, headers=None, **options):
    """Return an ``httpx.Client`` that sends transitio's ``User-Agent``.

    ``headers`` are merged over the default ``User-Agent`` header; every other
    keyword passes to :class:`httpx.Client` unchanged.
    """
    merged = httpx.Headers({"User-Agent": USER_AGENT})
    merged.update(headers or {})
    return httpx.Client(headers=merged, **options)
