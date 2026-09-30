"""HTTP client settings and the file download shared by transitio's fetchers."""

import hashlib
import os
import re
import tempfile
import time

import httpx

from transitio.exceptions import DownloadError

try:
    from transitio._core import __version__
except ImportError:  # the pure-Python modules import without the compiled core
    __version__ = "unknown"

USER_AGENT = f"transitio/{__version__} (+https://github.com/cafein-py/transitio)"

TIMEOUT = httpx.Timeout(60.0, connect=15.0)
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
RETRY_WAIT = 1.0

_ATTEMPTS = 3
_REQUESTS = 10
# Failures after the connection opened; one that cannot open fails at once.
_DROPS = (
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
)
_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)", re.IGNORECASE)


def client(*, headers=None, **options):
    """Return an ``httpx.Client`` that sends transitio's ``User-Agent``.

    ``headers`` are merged over the default ``User-Agent`` header; every other
    keyword passes to :class:`httpx.Client` unchanged.
    """
    merged = httpx.Headers({"User-Agent": USER_AGENT})
    merged.update(headers or {})
    return httpx.Client(headers=merged, **options)


def sha256_file(path):
    """The SHA-256 hex digest of a file, read in bounded chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(client, url, path):
    """Stream ``url`` to ``path`` with ``client``; return the SHA-256 hex.

    The body goes to a unique partial file beside ``path``, which replaces
    ``path`` only when complete and is removed on any failure. A status in
    :data:`RETRY_STATUSES`, a dropped or stalled connection and a body that
    ends short are retried, after waiting :data:`RETRY_WAIT` seconds doubled
    per failed attempt, up to three failed attempts and ten requests. A drop
    or a short body is resumed with a range request pinned by ``If-Range`` to
    the strong ETag, else the Last-Modified, of the response the file started
    from; an encoded or unpinned body restarts from zero, and an attempt that
    adds resumable bytes does not count as failed. A connection that cannot
    be opened and any other status fail at once.

    Raises :class:`~transitio.exceptions.DownloadError` naming ``url`` and
    the last failure.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, partial = tempfile.mkstemp(
        dir=path.parent, prefix=path.name + ".", suffix=".part"
    )
    try:
        # The descriptor is wrapped before the request, so a connection or
        # HTTP failure closes it rather than leaking it (and lets Windows
        # unlink the temp).
        with os.fdopen(fd, "wb") as handle:
            digest = _fetch(client, url, _Partial(handle))
        os.replace(partial, path)
    except BaseException:
        _discard(partial)
        raise
    return digest


def _discard(path):
    """Remove a temporary file, tolerant of its already being gone."""
    try:
        os.unlink(path)
    except OSError:
        pass


class _Partial:
    """A download's partial file: its bytes, their digest, the bytes the
    current request added and the ``(header, value)`` a resume is pinned to."""

    def __init__(self, handle):
        self.handle = handle
        self.added = 0
        self.restart()

    def restart(self, pin=None):
        self.handle.seek(0)
        self.handle.truncate()
        self.digest = hashlib.sha256()
        self.written = 0
        self.pin = pin

    def write(self, chunk):
        self.handle.write(chunk)
        self.digest.update(chunk)
        self.written += len(chunk)
        self.added += len(chunk)


def _fetch(client, url, partial):
    """Run a download's requests into ``partial``; return its digest."""
    failures = requests = 0
    while True:
        requests += 1
        error = None
        try:
            reason, retry = _request(client, url, partial)
        except _DROPS as caught:
            reason, retry, error = f"{type(caught).__name__}: {caught}", True, caught
        except httpx.HTTPError as caught:
            reason, retry, error = f"{type(caught).__name__}: {caught}", False, caught
        if reason is None:
            return partial.digest.hexdigest()
        if partial.pin is None:
            partial.restart()
        progress = partial.added > 0 and partial.pin is not None
        failures += not progress
        if not retry or failures == _ATTEMPTS or requests == _REQUESTS:
            if requests > 1:
                reason += f" ({requests} requests)"
            raise DownloadError(f"{url}: {reason}") from error
        if not progress:
            time.sleep(RETRY_WAIT * 2 ** (failures - 1))


def _request(client, url, partial):
    """One request of a download into ``partial``; return ``(reason,
    retry)``, the reason None once the file is complete."""
    partial.added = 0
    headers = {}
    if partial.written:
        headers = {"Range": f"bytes={partial.written}-", "If-Range": partial.pin[1]}
    with client.stream("GET", url, headers=headers) as response:
        status = response.status_code
        if status in RETRY_STATUSES or not response.is_success:
            return f"HTTP {status} {response.reason_phrase}", status in RETRY_STATUSES
        if status == 206:
            expected = _resumed_length(response, partial)
            if expected is None:
                partial.restart()
                return "unusable resume answer", True
        else:
            partial.restart(_pin(response))
            declared = response.headers.get("Content-Length", "")
            expected = int(declared) if declared.isdigit() else None
        for chunk in response.iter_bytes():
            partial.write(chunk)
        if status != 206 and _encoded(response):
            received = response.num_bytes_downloaded
        else:
            # Unencoded, the file holds exactly the bytes on the wire.
            received = partial.written
        if expected is not None and received < expected:
            return f"body ended at {received} of {expected} bytes", True
    return None, False


def _encoded(response):
    encoding = response.headers.get("Content-Encoding", "").strip().lower()
    return encoding not in ("", "identity")


def _pin(response):
    """The validator a resume of ``response``'s body is pinned to: its strong
    ETag, else its Last-Modified; None for an encoded body."""
    if _encoded(response):
        return None
    etag = response.headers.get("ETag")
    if etag and not etag.startswith("W/"):
        return ("ETag", etag)
    modified = response.headers.get("Last-Modified")
    return ("Last-Modified", modified) if modified else None


def _resumed_length(response, partial):
    """The full length a 206 declares when its body continues ``partial``:
    unencoded, starting at the bytes written and carrying the pin (a
    Last-Modified pin also holds when the header is absent); None otherwise."""
    if not partial.written or _encoded(response):
        return None
    match = _CONTENT_RANGE.fullmatch(response.headers.get("Content-Range", "").strip())
    if match is None or int(match[1]) != partial.written:
        return None
    header, pinned = partial.pin
    served = response.headers.get(header)
    if served != pinned and (header == "ETag" or served is not None):
        return None
    return int(match[3])
