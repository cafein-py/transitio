"""The ``transitio`` command line."""

from __future__ import annotations

import argparse
import sys


def main(argv=None):
    argv = list(sys.argv[1:]) if argv is None else list(argv)

    # Manual dispatch: everything after `edit` goes to the editor package
    # verbatim (argparse's REMAINDER would swallow leading options).
    if argv and argv[0] == "edit":
        try:
            from transitio_editor.cli import main as editor_main
        except ImportError:
            print(
                "transitio edit: the editor is a separate package: "
                "pip install transitio-editor",
                file=sys.stderr,
            )
            raise SystemExit(2) from None
        return editor_main(argv[1:])

    parser = argparse.ArgumentParser(
        prog="transitio", description="transitio command line"
    )
    parser.add_argument(
        "command",
        choices=["edit"],
        help="edit: edit a GTFS feed in the local map GUI " "(needs transitio-editor)",
    )
    parser.parse_args(argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
