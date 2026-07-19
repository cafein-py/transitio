"""Exception classes raised by transitio."""


class TransitioError(Exception):
    """Base class for all transitio-specific errors."""


class MissingTokenError(TransitioError):
    """No Mobility Database refresh token is available."""


class DownloadError(TransitioError):
    """A dataset cannot be downloaded or fails checksum verification."""


class ExtractNotFoundError(TransitioError):
    """No OSM extract covers the requested area."""
