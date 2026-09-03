import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import transitio.index as transitio_index  # noqa: E402
from index_build import publisher  # noqa: E402
from test_index_publisher import API, FakeGitHub, _index  # noqa: E402
from transitio.exceptions import DownloadError, IncompatibleIndexError  # noqa: E402
from transitio.exceptions import TransitioError  # noqa: E402
from transitio.index import _refresh as client  # noqa: E402
from transitio.index import release as contract  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch, tmp_path):
    """No selection, no pin and a private cache for every test."""
    monkeypatch.setattr(
        client, "_state", {"selection": None, "handle": None, "handle_key": None}
    )
    monkeypatch.delenv(client.SNAPSHOT_ENV, raising=False)
    monkeypatch.setattr(
        client.platformdirs, "user_cache_dir", lambda name: str(tmp_path / "xdg" / name)
    )


def _published(tmp_path, fake=None):
    """A fake GitHub holding one snapshot the real publisher released."""
    fake = fake or FakeGitHub()
    summary = publisher.publish_index(
        _index(tmp_path),
        cache_dir=tmp_path / "cache",
        repository="o/r",
        token="secret",
        api_url=API,
        out_dir=tmp_path / "out",
        transport=fake.transport(),
    )
    return fake, summary["snapshot_id"]


def _refresh(fake, **kw):
    return client.refresh(
        repository="o/r", api_url=API, transport=fake.transport(), **kw
    )


def _seed_archive(fake, snapshot_id, archive, *, manifest=None, tag=None):
    """A published release for ``archive`` bytes with a manifest that vouches
    for exactly them, so the archive's own content is what is tested."""
    body = {
        "snapshot_id": snapshot_id,
        "schema_version": 3,
        "min_reader_version": "0.11.0",
        "archive": {
            "name": contract.archive_name(snapshot_id),
            "sha256": hashlib.sha256(archive).hexdigest(),
            "bytes": len(archive),
        },
        **(manifest or {}),
    }
    return fake.seed(
        tag or contract.release_tag(snapshot_id),
        {
            contract.archive_name(snapshot_id): archive,
            contract.MANIFEST_NAME: json.dumps(body).encode(),
        },
    )


def _tar(members, *, symlink=None):
    sink = io.BytesIO()
    with tarfile.open(fileobj=sink, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if symlink is not None:
            info = tarfile.TarInfo(symlink)
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
    return sink.getvalue()


def test_refresh_installs_the_published_snapshot_and_queries_read_it(tmp_path):
    fake, snapshot_id = _published(tmp_path)
    summary = _refresh(fake)
    assert summary == {
        "snapshot_id": snapshot_id,
        "installed": True,
        "skipped": [],
        "removed": [],
    }
    root = client.cache_root()
    assert (root / "snapshots" / snapshot_id / "snapshot.json").is_file()
    assert not [
        p for p in (root / "snapshots").iterdir() if p.name.startswith(".incoming")
    ]
    # Lazy resolution: the one installed snapshot is the active one.
    index = client.active_index()
    assert index.snapshot_id == snapshot_id
    assert transitio_index.place("Q1757").feeds()[0].snapshot == snapshot_id
    assert client.installed() == [(snapshot_id, index.snapshot)]
    # Already installed: nothing downloaded again.
    assert _refresh(fake)["installed"] is False


def test_refresh_skips_what_a_client_must_not_take(tmp_path):
    fake, snapshot_id = _published(tmp_path)
    # Newer but unreadable: skipped and reported, never chosen.
    fake.seed(
        "index-0000000000000009",
        {
            "manifest.json": json.dumps(
                {"snapshot_id": "0000000000000009", "schema_version": 999}
            ).encode()
        },
    )
    # Published without a manifest yet: incomplete.
    fake.seed("index-000000000000000a", {})
    # A draft is invisible to clients.
    _seed_archive(fake, "000000000000000b", b"not an archive")
    fake.releases[max(fake.releases)]["draft"] = True
    summary = _refresh(fake)
    assert summary["snapshot_id"] == snapshot_id
    assert [s["tag"] for s in summary["skipped"]] == [
        "index-000000000000000a",
        "index-0000000000000009",
    ]


def test_nothing_compatible_or_unreachable_leaves_the_active_snapshot(tmp_path):
    fake, snapshot_id = _published(tmp_path)
    _refresh(fake)
    empty = FakeGitHub()
    with pytest.raises(IncompatibleIndexError, match="no published index release"):
        _refresh(empty)

    def unreachable(request):
        raise httpx.ConnectError("no route to host")

    with pytest.raises(DownloadError, match="listing the releases"):
        client.refresh(
            repository="o/r", api_url=API, transport=httpx.MockTransport(unreachable)
        )
    assert client.active_index().snapshot_id == snapshot_id


@pytest.mark.parametrize(
    ("archive", "message"),
    [
        (b"garbage", "could not be read"),
        (_tar({"snapshot.json": b"{}", "feeds.parquet": b""}), "lacks"),
        (
            _tar({name: b"" for name in contract.MEMBERS} | {"extra": b""}),
            "unexpected member",
        ),
        (
            _tar({name: b"" for name in contract.MEMBERS}, symlink="NOTICE"),
            "twice|not a regular file",
        ),
        (_tar({name: b"" for name in contract.MEMBERS}), "unreadable"),
    ],
    ids=["garbage", "missing members", "extra member", "symlink", "empty manifest"],
)
def test_a_hostile_or_broken_archive_is_refused(tmp_path, archive, message):
    fake = FakeGitHub()
    _seed_archive(fake, "00000000000000aa", archive)
    with pytest.raises(DownloadError, match=message):
        _refresh(fake)
    assert client.installed() == []
    root = client.cache_root() / "snapshots"
    assert not root.exists() or list(root.iterdir()) == []


def test_a_digest_mismatch_is_refused(tmp_path):
    fake = FakeGitHub()
    archive = _tar({name: b"" for name in contract.MEMBERS})
    _seed_archive(
        fake,
        "00000000000000ab",
        archive,
        manifest={
            "archive": {
                "name": contract.archive_name("00000000000000ab"),
                "sha256": "0" * 64,
                "bytes": len(archive),
            }
        },
    )
    with pytest.raises(DownloadError, match="does not match the digest"):
        _refresh(fake)


def test_use_and_the_environment_pin_select_an_installed_snapshot(
    tmp_path, monkeypatch
):
    fake, snapshot_id = _published(tmp_path)
    _refresh(fake)
    with pytest.raises(TransitioError, match="not installed"):
        client.use("0000000000000001")
    with pytest.raises(ValueError):
        client.use("nope")
    client.use(snapshot_id)
    assert client.active_index().snapshot_id == snapshot_id
    client.use(None)
    monkeypatch.setenv(client.SNAPSHOT_ENV, "0000000000000001")
    with pytest.raises(TransitioError, match="not installed"):
        client.active_index()
    monkeypatch.setenv(client.SNAPSHOT_ENV, snapshot_id)
    assert client.active_index().snapshot_id == snapshot_id


def test_nothing_installed_says_to_refresh():
    with pytest.raises(TransitioError, match="run transitio.index.refresh"):
        transitio_index.place("Q1757")


def test_the_cache_keeps_the_newest_three_and_the_pinned_one(tmp_path):
    root = client.cache_root()
    for n in range(1, 6):
        snapshot_id = f"{n:016x}"
        path = root / "snapshots" / snapshot_id
        path.mkdir(parents=True)
        (path / "snapshot.json").write_text(
            json.dumps({"snapshot_id": snapshot_id, "built_at": f"2026-09-0{n}"})
        )
    removed = client._prune(root, 3, {f"{1:016x}"})
    assert removed == [f"{2:016x}"]
    assert [s for s, _ in client.installed()] == [
        f"{5:016x}",
        f"{4:016x}",
        f"{3:016x}",
        f"{1:016x}",
    ]
