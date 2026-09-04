import pytest


def test_public_names_importable():
    import transitio

    assert transitio.MobilityDatabase is not None
    assert transitio.Feed is not None
    assert transitio.Dataset is not None
    assert transitio.fetch is transitio.pipeline.fetch
    assert transitio.FetchResult is transitio.pipeline.FetchResult


def test_exceptions_hierarchy():
    import transitio
    from transitio.exceptions import (
        TransitioError,
        DownloadError,
        MissingTokenError,
    )

    assert issubclass(MissingTokenError, TransitioError)
    assert issubclass(DownloadError, TransitioError)
    assert transitio.exceptions.TransitioError is TransitioError


def test_unknown_attribute():
    import transitio

    with pytest.raises(AttributeError):
        transitio.does_not_exist


def test_version():
    pytest.importorskip("transitio._core")
    import transitio

    assert transitio.__version__


def test_the_wheel_declares_the_licence_files():
    """LICENSE and NOTICE must be declared license files, so the wheel ships
    them under dist-info/licenses/ (never the wheel root, where they would
    collide with another distribution). The usual regression is one dropping
    out of the list."""
    import re
    from pathlib import Path

    text = (
        Path(__file__).resolve().parent.parent.joinpath("pyproject.toml").read_text()
    )
    match = re.search(r"(?m)^license-files = \[(.*?)\]", text)
    assert match, "the license-files list was not found"
    body = match.group(1)
    for name in ("LICENSE", "NOTICE"):
        assert f'"{name}"' in body, f"{name} is not a declared license file"


def test_the_built_wheel_contains_the_licence_files(tmp_path):
    """The real wheel, once built, carries the licence files. Opt-in, since it
    builds the Rust extension: set TRANSITIO_TEST_WHEEL=1 with maturin and
    cargo on PATH. The fast include guard above runs everywhere else."""
    import os
    import shutil
    import subprocess
    import tempfile
    import zipfile
    from pathlib import Path

    if not os.environ.get("TRANSITIO_TEST_WHEEL"):
        pytest.skip("set TRANSITIO_TEST_WHEEL=1 to build and inspect the wheel")
    if not (shutil.which("maturin") and shutil.which("cargo")):
        pytest.skip("maturin and cargo are required to build the wheel")

    repo = Path(__file__).resolve().parent.parent
    # Build from an isolated copy of the source, with stand-in index files
    # staged there, so the checkout is never touched and the package-data
    # globs are still exercised. Copying only what maturin reads keeps it cheap.
    src = Path(tempfile.mkdtemp())
    try:
        for name in ("pyproject.toml", "Cargo.toml", "Cargo.lock", "README.md",
                     "LICENSE", "NOTICE"):
            source = repo / name
            if source.exists():
                shutil.copy2(source, src / name)
        shutil.copytree(repo / "crates", src / "crates")
        shutil.copytree(repo / "python", src / "python")
        index_dir = src / "python" / "transitio" / "index"
        (index_dir / "snapshot.json").write_bytes(b"{}")
        (index_dir / "NOTICE").write_bytes(b"stand-in\n")
        (index_dir / "feeds.parquet").write_bytes(b"stand-in")
        subprocess.run(
            ["maturin", "build", "--release", "-o", str(tmp_path)],
            cwd=src,
            check=True,
            capture_output=True,
        )
        (wheel,) = tmp_path.glob("*.whl")
        names = zipfile.ZipFile(wheel).namelist()
    finally:
        shutil.rmtree(src, ignore_errors=True)
    assert any(n.endswith(".dist-info/licenses/LICENSE") for n in names)
    assert any(n.endswith(".dist-info/licenses/NOTICE") for n in names)
    # Never at the wheel root, where pip would drop them into site-packages.
    assert "LICENSE" not in names and "NOTICE" not in names
    # The package-data globs ship the index beside the package when present.
    for member in ("snapshot.json", "NOTICE", "feeds.parquet"):
        assert f"transitio/index/{member}" in names
