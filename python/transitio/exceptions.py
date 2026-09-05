"""Exception classes raised by transitio."""


class TransitioError(Exception):
    """Base class for all transitio-specific errors."""


class MissingTokenError(TransitioError):
    """No Mobility Database refresh token is available."""


class IncompatibleIndexError(TransitioError):
    """The feed index was built for a schema this transitio cannot read."""


class PlaceNotFoundError(TransitioError):
    """No place in the index matches the query."""


class AmbiguousPlaceError(TransitioError):
    """Several places match the query and none wins outright.

    The tied candidates are available as :attr:`candidates`, in ranked order.
    """

    candidates = ()


class StaleSelectorError(TransitioError):
    """A feed's selector no longer matches the downloaded feed.

    Raised under ``on_untrusted_selector="error"`` when the selector has no
    route evidence to trust (``unavailable``), the fingerprint it was built
    from does not recompute from the download, or a selected route id is
    absent from it. The feed id is :attr:`feed_id`.
    """

    feed_id = None


class DownloadError(TransitioError):
    """A dataset cannot be downloaded or fails checksum verification."""


class ExtractNotFoundError(TransitioError):
    """No OSM extract covers the requested area."""


class InvalidFeedError(TransitioError):
    """A saved feed has error-severity validation notices.

    The full validation report is available as :attr:`report`.
    """

    report = None


class PatchError(TransitioError):
    """Patching could not certify the output.

    Raised when any validation stage is sampled/truncated (always), or
    when the patched output still carries ERROR notices (under
    ``check=True``; the output file is still written and the exception
    carries the full report as ``.report``).
    """


class ShapeInferenceError(TransitioError):
    """The feed written with inferred shapes does not validate.

    Raised when the output carries error-severity notices the input did
    not, so a broken ``shapes.txt`` can never leave silently. The file
    is still written, like :class:`InvalidFeedError`; the full report is
    available as :attr:`report`.
    """

    report = None


class ChangeLogDesyncError(TransitioError):
    """The tables no longer match the change log, so undo/redo refused.

    Raised when a direct edit through the ``tables`` escape hatch has
    changed what a logged entry recorded; nothing is modified.
    """
