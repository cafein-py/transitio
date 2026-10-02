import os
import stat
import sys
import threading
from pathlib import Path

import pandas as pd
import pytest

import transitio
from transitio import credentials
from transitio.index import Index

posix = pytest.mark.skipif(sys.platform == "win32", reason="no file store on Windows")

PROVIDERS = [
    {"provider_id": "trafiklab", "credential_fields": ["key"]},
    {
        "provider_id": "gcba-transporte",
        "credential_fields": ["client_id", "client_secret"],
    },
]
# Quotes, a backslash, control characters and non-ASCII text.
ODD = 'i"d\\\n\t\x7fé'


def _index(providers=PROVIDERS):
    table = None if providers is None else pd.DataFrame(providers)
    snapshot = {"snapshot_id": "s", "schema_version": 11}
    return Index(snapshot, pd.DataFrame(), access_providers=table)


@pytest.fixture(autouse=True)
def config(monkeypatch, tmp_path):
    """No credential variables and a private config directory."""
    for name in list(os.environ):
        if name.startswith("TRANSITIO_KEY_"):
            monkeypatch.delenv(name)
    monkeypatch.setattr(
        credentials.platformdirs,
        "user_config_dir",
        lambda name: str(tmp_path / "config" / name),
    )
    return tmp_path / "config" / "transitio"


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def _store(directory, text, mode=0o600, directory_mode=0o700):
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(directory_mode)
    file = directory / credentials.FILE_NAME
    file.write_text(text)
    file.chmod(mode)
    return file


@posix
def test_the_store_and_the_lookup_order(config, monkeypatch, tmp_path):
    index = _index()
    assert transitio.credentials is credentials
    credentials.set("gcba-transporte", {"client_id": ODD}, index=index)
    assert _mode(config) == 0o700 and _mode(config / credentials.FILE_NAME) == 0o600
    assert credentials.get("gcba-transporte", index=index) is None
    assert credentials.configured(index=index) == {}
    credentials.set("gcba-transporte", {"client_secret": "s"}, index=index)
    credentials.set("trafiklab", {"key": "k"}, index=index)
    text = (config / credentials.FILE_NAME).read_text()
    assert credentials.tomllib.loads(text) == {
        "gcba-transporte": {"client_id": ODD, "client_secret": "s"},
        "trafiklab": {"key": "k"},
    }
    assert credentials.get("gcba-transporte", index=index) == {
        "client_id": ODD,
        "client_secret": "s",
    }
    assert credentials.configured(index=index) == {
        "trafiklab": "file",
        "gcba-transporte": "file",
    }
    # Per field: explicit over the environment over the file; an empty
    # variable is unset.
    monkeypatch.setenv("TRANSITIO_KEY_GCBA_TRANSPORTE__CLIENT_ID", "env-id")
    monkeypatch.setenv("TRANSITIO_KEY_TRAFIKLAB__KEY", "")
    assert credentials.get("gcba-transporte", index=index)["client_id"] == "env-id"
    assert credentials.configured(index=index) == {
        "trafiklab": "file",
        "gcba-transporte": "mixed",
    }
    gcba = index.access_provider("gcba-transporte")
    monkeypatch.setenv("TRANSITIO_KEY_GCBA_TRANSPORTE__CLIENT_SECRET", "env-secret")
    fields, missing = credentials._resolve(gcba, {"client_secret": "x"})
    assert [(f, repr(s), s.reveal()) for f, s in fields.items()] == [
        ("client_id", "***", "env-id"),
        ("client_secret", "***", "x"),
    ]
    assert missing == []
    with pytest.raises(ValueError, match="non-empty string"):
        credentials._resolve(gcba, {"client_secret": ""})
    # Clearing one provider, one the file lacks, then the whole file.
    monkeypatch.delenv("TRANSITIO_KEY_GCBA_TRANSPORTE__CLIENT_SECRET")
    credentials.clear("gcba-transporte")
    credentials.clear("unknown")
    assert credentials._resolve(gcba)[1] == ["client_secret"]
    assert credentials.get("trafiklab", index=index) == {"key": "k"}
    credentials.clear()
    credentials.clear()
    assert not (config / credentials.FILE_NAME).exists()
    assert credentials.configured(index=index) == {}
    # A caller's file two missing levels deep, with as long a name as a file
    # can have: its own directory is private.
    path = tmp_path / "a" / "b" / ("k" * 250 + ".toml")
    credentials.set("trafiklab", {"key": "k2"}, index=index, path=path)
    assert _mode(path.parent) == 0o700 and _mode(path) == 0o600
    assert credentials.get("trafiklab", index=index, path=path) == {"key": "k2"}
    # A umask that leaves the owner no write access narrows neither mode.
    path = tmp_path / "narrow" / "keys.toml"
    umask = os.umask(0o277)
    try:
        credentials.set("trafiklab", {"key": "k3"}, index=index, path=path)
    finally:
        os.umask(umask)
    assert _mode(path.parent) == 0o700 and _mode(path) == 0o600


def _symlinked_file(config, monkeypatch):
    target = _store(config.parent / "elsewhere", "[trafiklab]\nkey = 'k'\n")
    config.mkdir(mode=0o700)
    (config / credentials.FILE_NAME).symlink_to(target)


def _symlinked_directory(config, monkeypatch):
    _store(config.parent / "elsewhere", "[trafiklab]\nkey = 'k'\n")
    config.symlink_to(config.parent / "elsewhere")


def _group_writable_directory(config, monkeypatch):
    _store(config, "[trafiklab]\nkey = 'k'\n", directory_mode=0o770)


def _group_readable_file(config, monkeypatch):
    _store(config, "[trafiklab]\nkey = 'k'\n", mode=0o640)


def _small_limit(config, monkeypatch):
    _store(config, "[trafiklab]\nkey = 'k'\n")
    monkeypatch.setattr(credentials, "_MAX_FILE_BYTES", 40)


def _vanishing_directory(config, monkeypatch):
    # The directory is gone before it is opened; nothing lands in the cwd.
    monkeypatch.chdir(config.parent.parent)
    monkeypatch.setattr(credentials.os, "mkdir", lambda path, mode: None)


@pytest.mark.parametrize(
    ("prepare", "call", "error", "match"),
    [
        (None, ("set", "nope", {"key": "k"}), ValueError, "no credential provider"),
        (None, ("set", "trafiklab", {"id": "k"}), ValueError, "no credential field"),
        (None, ("set", "trafiklab", {"key": ""}), ValueError, "non-empty string"),
        (None, ("set", "trafiklab", {"key": 1}), ValueError, "non-empty string"),
        (None, ("set", "trafiklab", {"key": "\ud800"}), ValueError, "non-empty"),
        (None, ("get", "nope"), ValueError, "no credential provider"),
        ("schema 10", ("set", "trafiklab", {"key": "k"}), ValueError, "refresh it"),
        ("schema 10", ("get", "trafiklab"), ValueError, "refresh it"),
        ("schema 10", ("configured",), ValueError, "refresh it"),
        (_symlinked_file, ("get", "trafiklab"), PermissionError, "symbolic link"),
        (_symlinked_file, ("clear", "trafiklab"), PermissionError, "symbolic link"),
        (_symlinked_file, ("clear",), PermissionError, "symbolic link"),
        (_small_limit, ("set", "trafiklab", {"key": "k" * 30}), ValueError, "over 40"),
        (
            _vanishing_directory,
            ("set", "trafiklab", {"key": "k"}),
            FileNotFoundError,
            "transitio",
        ),
        (_symlinked_directory, ("configured",), PermissionError, "symbolic link"),
        (_group_writable_directory, ("get", "trafiklab"), PermissionError, "chmod 700"),
        (_group_readable_file, ("get", "trafiklab"), PermissionError, "chmod 600"),
        (
            _group_readable_file,
            ("set", "trafiklab", {"key": "k"}),
            PermissionError,
            "chmod 600",
        ),
        ("[trafiklab]\nkey = ''\n", ("get", "trafiklab"), ValueError, "trafiklab.key"),
        ("[trafiklab]\nkey = 1\n", ("configured",), ValueError, "trafiklab.key"),
        ("trafiklab = 'k'\n", ("clear", "trafiklab"), ValueError, "'trafiklab' is not"),
        ("[trafiklab\nkey = 'k'\n", ("get", "trafiklab"), ValueError, "line 1"),
    ],
)
@posix
def test_refusals(config, monkeypatch, prepare, call, error, match):
    index = _index(None if prepare == "schema 10" else PROVIDERS)
    if isinstance(prepare, str) and prepare != "schema 10":
        _store(config, prepare)
    elif callable(prepare):
        prepare(config, monkeypatch)
    file = config / credentials.FILE_NAME
    before = file.read_bytes() if file.exists() else None
    name, *args = call
    kwargs = {} if name == "clear" else {"index": index}
    with pytest.raises(error, match=match) as raised:
        getattr(credentials, name)(*args, **kwargs)
    assert "'k'" not in str(raised.value)
    assert (file.read_bytes() if file.exists() else None) == before
    if isinstance(prepare, str) and prepare != "schema 10":
        # Raised outside the frames that held the file's text.
        assert raised.value.__context__ is None
        assert not {"_read", "_parse"} & {entry.name for entry in raised.traceback}


def test_windows_has_no_file_store(config, monkeypatch):
    index = _index()
    # A file that would be refused, were it read.
    _store(config, "[trafiklab]\nkey = 'k'\n", mode=0o644)
    monkeypatch.setattr(sys, "platform", "win32")
    for call in (
        lambda: credentials.set("trafiklab", {"key": "k"}, index=index),
        credentials.clear,
    ):
        with pytest.raises(NotImplementedError, match="TRANSITIO_KEY_"):
            call()
    assert credentials.get("trafiklab", index=index) is None
    monkeypatch.setenv("TRANSITIO_KEY_TRAFIKLAB__KEY", "env")
    assert credentials.get("trafiklab", index=index) == {"key": "env"}
    assert credentials.configured(index=index) == {"trafiklab": "env"}


@posix
def test_updates_run_one_at_a_time(config, monkeypatch):
    index = _index()
    read = credentials._read
    other = threading.Thread(
        target=credentials.set,
        args=("trafiklab", {"key": "k"}),
        kwargs={"index": index},
    )

    def first_read(*args):
        tables = read(*args)
        if other.ident is None:
            # The other update waits for this one to write.
            other.start()
            other.join(0.2)
        return tables

    monkeypatch.setattr(credentials, "_read", first_read)
    credentials.set("gcba-transporte", {"client_id": "i"}, index=index)
    other.join()
    assert credentials.tomllib.loads((config / credentials.FILE_NAME).read_text()) == {
        "gcba-transporte": {"client_id": "i"},
        "trafiklab": {"key": "k"},
    }


@posix
def test_a_failed_write_keeps_the_store_and_leaves_no_value(config, monkeypatch):
    index = _index()
    file = _store(config, '[gcba-transporte]\nclient_id = "f1le-id"\n')
    before = file.read_bytes()

    def full(fd):
        raise OSError("disk full")

    monkeypatch.setattr(credentials.os, "fsync", full)
    with pytest.raises(OSError, match="disk full") as raised:
        credentials.set("trafiklab", {"key": "n3w-key"}, index=index)
    assert os.listdir(config) == [credentials.FILE_NAME]
    assert file.read_bytes() == before
    # No frame of the module holds a stored value, nor one below set() the
    # caller's own.
    frames = [e for e in raised.traceback if Path(str(e.path)).name == "credentials.py"]
    assert [e.name for e in frames] == ["set", "_write"]
    for entry in frames:
        seen = repr(entry.locals)
        assert "f1le-id" not in seen
        assert entry.name == "set" or "n3w-key" not in seen
