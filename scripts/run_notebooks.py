"""Run the documentation notebooks and store their outputs.

The docs render the notebooks in ``docs/`` from their stored outputs;
nothing runs on Read the Docs. Run them again after a change that alters
what they show:

    python scripts/run_notebooks.py                     # every notebook
    python scripts/run_notebooks.py quickstart fetching_data

Each notebook runs in a kernel of the Python running this script, with the
transitio it imports, in a scratch folder, so the files it writes stay out
of the repository. Downloads go through the usual caches, so a feed already
cached shows as reused; the pages run in the order the user guide lists
them, the later ones reusing the Quickstart's downloads. The stored outputs
lose the widget progress bars, which a static page cannot draw, and library
log lines; warnings read ``UserWarning: <message>`` without the module's
path. A notebook is not written when its outputs name this machine's home
or temporary folder, or when the file changed while it ran.
"""

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

import nbformat
from nbclient import NotebookClient

DOCS = Path(__file__).resolve().parent.parent / "docs"
ORDER = [
    "quickstart",
    "finding_places",
    "choosing_feeds",
    "fetching_data",
    "download_cache",
    "working_with_feeds",
    "cropping_and_merging",
    "building_feeds",
    "catalogues",
]
KERNEL = "transitio-docs"
_JUPYTER_PATH = [p for p in os.environ.get("JUPYTER_PATH", "").split(os.pathsep) if p]
WIDGET = "application/vnd.jupyter.widget-view+json"
# OpenMP, glog and matplotlib's first-run lines say nothing about the example.
NOISE = re.compile(
    r"^(OMP: (Warning|Hint|Info)\b.*|W\d{8} .*"
    r"|WARNING: Logging before InitGoogleLogging.*|.*cpu_info\.cc.*"
    r"|Matplotlib is building the font cache.*)$"
)
# "<file>:<line>: UserWarning: <message>" and the source line after it; the
# file may be any path, spaces included.
WARNING = re.compile(r"^.+?:\d+: (\w+Warning): (.*)\n(?:  .*\n)?", re.M)


def clean(nb):
    """``nb`` without widget outputs, noise lines and warning paths."""
    for cell in nb.cells:
        cell.metadata.pop("execution", None)
        if cell.cell_type != "code":
            continue
        kept = []
        for output in cell.get("outputs", []):
            if WIDGET in output.get("data", {}):
                continue
            if output.output_type == "stream":
                lines = output.text.splitlines(keepends=True)
                text = "".join(line for line in lines if not NOISE.match(line.rstrip()))
                text = WARNING.sub(r"\1: \2\n", text)
                if not text.strip():
                    continue
                output.text = text
            kept.append(output)
        cell.outputs = kept
    nb.metadata.pop("widgets", None)
    return nb


def _strings(value):
    """The strings in a decoded JSON value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _texts(nb):
    """The text of every output: streams, tracebacks and textual data."""
    for cell in nb.cells:
        for output in cell.get("outputs", []):
            if "text" in output:
                yield output.text
            yield from output.get("traceback", [])
            yield from _strings(output.get("metadata", {}))
            for kind, value in output.get("data", {}).items():
                # Every form but base64-encoded binary images.
                if kind not in ("image/png", "image/jpeg", "image/gif"):
                    yield from _strings(value)


def private_folders(nb):
    """The home and temporary folders the outputs of ``nb`` name, however
    their separators are written."""
    folders = {Path.home(), Path(tempfile.gettempdir())}
    folders |= {folder.resolve() for folder in folders}
    forms = set()
    for folder in folders:
        text = str(folder)
        windows = text.replace("/", "\\")
        # A path's repr doubles its backslashes.
        forms |= {text, text.replace("\\", "/"), windows, windows.replace("\\", "\\\\")}
    # Windows paths match whatever their case.
    fold = str.lower if os.name == "nt" else str
    found = set()
    for text in _texts(nb):
        found |= {form for form in forms if fold(form) in fold(text)}
    return sorted(found)


def _kernel(scratch):
    """A kernelspec in ``scratch`` that runs this Python, found first."""
    spec = Path(scratch) / "kernels" / KERNEL
    spec.mkdir(parents=True)
    (spec / "kernel.json").write_text(
        json.dumps(
            {
                "argv": [
                    sys.executable,
                    "-m",
                    "ipykernel_launcher",
                    "-f",
                    "{connection_file}",
                ],
                "display_name": "transitio docs",
                "language": "python",
            }
        ),
        encoding="utf-8",
    )
    os.environ["JUPYTER_PATH"] = os.pathsep.join([scratch] + _JUPYTER_PATH)
    # The kernel runs in another folder: relative import paths would miss.
    entries = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    if entries:
        os.environ["PYTHONPATH"] = os.pathsep.join(
            str(Path(p).resolve()) for p in entries
        )


def _read_regular(path):
    """The bytes of ``path`` when it is a regular file, read without following
    a symlink (where the system can refuse one); None otherwise."""
    if path.is_symlink():
        return None
    try:
        handle = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    with os.fdopen(handle, "rb") as file:
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            return None
        return file.read()


def _unchanged(path, before):
    """Whether ``path`` is still the regular file whose digest was ``before``."""
    now = _read_regular(path)
    return now is not None and hashlib.sha256(now).hexdigest() == before


def run(name):
    """Run ``docs/<name>.ipynb`` and store its cleaned outputs; False when
    they name a private folder or the notebook changed meanwhile."""
    path = DOCS / f"{name}.ipynb"
    # The digest and the notebook come from the same read.
    source = _read_regular(path)
    if source is None:
        print(f"{name}: not a regular file; not run", file=sys.stderr)
        return False
    before = hashlib.sha256(source).hexdigest()
    nb = nbformat.reads(source.decode("utf-8"), as_version=4)
    with (
        tempfile.TemporaryDirectory() as scratch,
        tempfile.TemporaryDirectory() as specs,
    ):
        _kernel(specs)
        NotebookClient(
            nb,
            timeout=3600,
            kernel_name=KERNEL,
            record_timing=False,
            resources={"metadata": {"path": scratch}},
        ).execute()
    nb = clean(nb)
    found = private_folders(nb)
    if found:
        print(f"{name}: outputs name {', '.join(found)}; not written", file=sys.stderr)
        return False
    if not _unchanged(path, before):
        print(f"{name}: changed while it ran; not written", file=sys.stderr)
        return False
    handle, partial = tempfile.mkstemp(
        dir=path.parent, prefix=f".{name}.", suffix=".part"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as out:
            out.write(nbformat.writes(nb))
        os.chmod(partial, path.stat().st_mode & 0o777)
        # Checked again just before the swap; what remains is the instant
        # between this check and os.replace.
        if not _unchanged(path, before):
            Path(partial).unlink(missing_ok=True)
            print(f"{name}: changed while it ran; not written", file=sys.stderr)
            return False
        os.replace(partial, path)
    except BaseException:
        Path(partial).unlink(missing_ok=True)
        raise
    print(f"{name}: written")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("names", nargs="*", help="notebooks to run; all by default")
    names = parser.parse_args().names or ORDER
    unknown = sorted(set(names) - set(ORDER))
    if unknown:
        parser.error(
            f"no notebook {', '.join(unknown)}; the notebooks: {', '.join(ORDER)}"
        )
    written = [run(name) for name in sorted(names, key=ORDER.index)]
    sys.exit(0 if all(written) else 1)


if __name__ == "__main__":
    main()
