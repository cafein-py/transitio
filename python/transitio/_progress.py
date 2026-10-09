"""Progress on stderr: a tqdm bar per download, a widget in Jupyter when
ipywidgets is installed, and status lines."""

import contextlib
import contextvars
import importlib.util
import re
import sys

# The callback a download given none reports to (:func:`reporting`).
_current = contextvars.ContextVar("transitio_download_progress", default=None)

# C0 and C1 control characters and DEL: in a feed name or an error text they
# could move the cursor, send terminal codes or forge a line.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _printable(text):
    """``text`` with each control character a space."""
    return _CONTROL.sub(" ", text)


def _bar_class():
    """``(tqdm class, disable)``: in a Jupyter kernel the widget bar when
    ipywidgets is installed, else the text bar, which the notebook redraws
    in place; elsewhere the text bar, hidden when stderr is no terminal."""
    from tqdm import std

    # A kernel has imported IPython; nothing else is imported to find out.
    ipython = sys.modules.get("IPython")
    shell = getattr(ipython, "get_ipython", lambda: None)()
    if type(shell).__name__ != "ZMQInteractiveShell":
        return std.tqdm, None
    if importlib.util.find_spec("ipywidgets") is None:
        return std.tqdm, False
    from tqdm import notebook

    return notebook.tqdm, False


def bar(desc, total, unit="B"):
    """A tqdm bar of ``unit`` (bytes by default) on stderr described
    ``desc``, its control characters spaces, ``total`` None when unknown
    (:func:`_bar_class`). Bytes show scaled (``13.1M``), other units as
    whole counts."""
    tqdm, disable = _bar_class()
    made = tqdm(
        total=total,
        file=sys.stderr,
        unit=unit,
        unit_scale=unit == "B",
        unit_divisor=1000,
        leave=True,
        mininterval=0.2,
        dynamic_ncols=True,
        disable=disable,
    )
    # Set after: the widget bar shows a description given to it as HTML until
    # it redraws, and escapes it only then.
    made.set_description(_printable(desc))
    return made


def say(text):
    """Write ``text`` as one line on stderr, above any open text bar, its
    control characters spaces."""
    from tqdm import std

    std.tqdm.write(_printable(text), file=sys.stderr)
    sys.stderr.flush()


class Download:
    """A progress callback for :func:`transitio._http.download`, called with
    ``(written, total)``: a :func:`bar` described ``desc``, opened at the
    first call after :meth:`close` and moved back when a download restarts;
    a bar that does not show is said as a line instead. ``downloaded``
    counts the bytes of the bars closed."""

    def __init__(self, desc):
        self.desc = desc
        self.bar = None
        self.written = self.downloaded = 0

    def __call__(self, written, total):
        if self.bar is None:
            self.bar = bar(self.desc, total)
            if self.bar.disable:
                say(self.desc)
        elif written < self.written or total != self.bar.total:
            self.bar.reset(total)
        self.bar.update(written - self.bar.n)
        self.written = written

    def close(self):
        if self.bar is not None:
            self.bar.close()
            self.downloaded += self.written
            self.bar, self.written = None, 0


def current():
    """The callback :func:`reporting` set, None outside one."""
    return _current.get()


@contextlib.contextmanager
def reporting(callback):
    """Make ``callback`` the progress callback of each download in the block
    that is given none (:func:`transitio._http.download`)."""
    token = _current.set(callback)
    try:
        yield
    finally:
        _current.reset(token)
