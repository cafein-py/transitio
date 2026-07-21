import sys

import pytest

from transitio._cli import main


def test_edit_delegates_to_the_editor_package(monkeypatch):
    import types

    calls = {}
    cli = types.ModuleType("transitio_editor.cli")

    def fake_main(argv):
        calls["argv"] = argv
        return 0

    cli.main = fake_main
    package = types.ModuleType("transitio_editor")
    package.cli = cli
    monkeypatch.setitem(sys.modules, "transitio_editor", package)
    monkeypatch.setitem(sys.modules, "transitio_editor.cli", cli)

    assert main(["edit", "feed.zip", "--port", "9000"]) == 0
    assert calls["argv"] == ["feed.zip", "--port", "9000"]


def test_edit_without_editor_package_errors(monkeypatch):
    monkeypatch.setitem(sys.modules, "transitio_editor", None)
    monkeypatch.setitem(sys.modules, "transitio_editor.cli", None)
    with pytest.raises(SystemExit):
        main(["edit", "feed.zip"])
