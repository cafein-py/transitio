"""Exception classes raised by transitio."""


class TransitioError(Exception):
    """Base class for all transitio-specific errors."""


class MissingTokenError(TransitioError):
    """No Mobility Database refresh token is available."""


class DownloadError(TransitioError):
    """A dataset cannot be downloaded or fails checksum verification."""


class ExtractNotFoundError(TransitioError):
    """No OSM extract covers the requested area."""


class InvalidFeedError(TransitioError):
    """A saved feed has error-severity validation notices.

    The full validation report is available as :attr:`report`.
    """

    report = None


class ChangeLogDesyncError(TransitioError):
    """The tables no longer match the change log, so undo/redo refused.

    Raised when a direct edit through the ``tables`` escape hatch has
    changed what a logged entry recorded; nothing is modified.
    """
