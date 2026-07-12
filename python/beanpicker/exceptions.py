"""Exception classes raised by beanpicker."""


class BeanpickerError(Exception):
    """Base class for all beanpicker-specific errors."""


class MissingTokenError(BeanpickerError):
    """No Mobility Database refresh token is available."""


class DownloadError(BeanpickerError):
    """A dataset cannot be downloaded or fails checksum verification."""


class ExtractNotFoundError(BeanpickerError):
    """No OSM extract covers the requested area."""
