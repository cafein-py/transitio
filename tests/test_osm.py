import contextlib
import datetime
import hashlib
import json
import math
import os
import types
from pathlib import Path

import pytest
import shapely
from shapely.geometry import box

from transitio.exceptions import DownloadError, ExtractNotFoundError
from transitio.osm import fetch_pbf
from transitio.osm._fetch import _as_geometry, _buffered, _crop_filename

pytest.importorskip("pyrosm")

from pyrosm.exceptions import ExtractDownloadError  # noqa: E402
from pyrosm.exceptions import (  # noqa: E402
    ExtractNotFoundError as PyrosmExtractNotFoundError,
)

HELSINKI_BBOX = (24.6, 60.1, 25.2, 60.4)
PBF_BYTES = b"\x00fake-pbf-payload"
CROP_BYTES = b"\x00cropped-pbf"
EXTRACT_URL = "https://download.geofabrik.de/europe/finland-latest.osm.pbf"
FAILED_URL = "https://download.bbbike.org/osm/bbbike/Helsinki/Helsinki.osm.pbf"
MOVISDA_URL = "https://osm.download.movisda.io/admin/SE/SE-BD/latest.osm.pbf"
SNAPSHOT = datetime.datetime(2026, 9, 30, 20, 21, 2, tzinfo=datetime.timezone.utc)
UTC = datetime.timezone.utc
FINLAND = ("Geofabrik", "finland", EXTRACT_URL, len(PBF_BYTES), SNAPSHOT)


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture
def area_extract(monkeypatch):
    """Stands in for pyrosm.get_data_by_area: records each call; writes each
    source extract of ``calls.sources`` (provider, id, url, bytes, snapshot)
    into the directory it is given when missing or on update, then the crop
    to ``output_path``, or with ``crop=False`` and several sources the
    merged file."""
    import pyrosm

    class Calls(list):
        sources = [FINLAND]

    calls = Calls()

    def get_data_by_area(
        area, crop=True, update=False, directory=None, output_path=None, **kwargs
    ):
        directory = Path(directory)
        calls.append(
            dict(
                area=area,
                crop=crop,
                update=update,
                directory=directory,
                existed=directory.is_dir(),
                output_path=output_path,
                **kwargs,
            )
        )
        sources = []
        for provider, extract, url, size, snapshot in calls.sources:
            path = directory / f"{provider.lower()}_{extract}-latest.osm.pbf"
            if update or not path.exists():
                path.write_bytes(PBF_BYTES)
            sources.append(
                types.SimpleNamespace(
                    provider=provider,
                    extract=extract,
                    url=url,
                    bytes=size,
                    path=str(path),
                    sha256=_sha256(path),
                    snapshot=snapshot,
                )
            )
        written = Path(sources[0].path)
        if crop:
            written = Path(output_path)
            written.write_bytes(CROP_BYTES)
        elif len(sources) > 1:
            written = directory / "merged_0123456789ab.osm.pbf"
            written.write_bytes(b"".join(Path(s.path).read_bytes() for s in sources))
        merged = len(sources) > 1
        sizes = [source.bytes for source in sources]
        return types.SimpleNamespace(
            path=str(written),
            provider="+".join(source.provider for source in sources),
            extract="+".join(source.extract for source in sources),
            url=None if merged else sources[0].url,
            bytes=None if None in sizes else sum(sizes),
            failed=[(FAILED_URL, "HTTP Error 503")],
            sources=sources,
            sha256=_sha256(written),
            snapshot=min(source.snapshot for source in sources),
        )

    monkeypatch.setattr(pyrosm, "get_data_by_area", get_data_by_area)
    return calls


@pytest.mark.parametrize("directory", [None, "out"])
def test_fetch_full_extract(tmp_path, area_extract, directory):
    out = tmp_path / directory if directory else tmp_path / "cache" / "osm"
    options = dict(
        crop=False, directory=out if directory else None, cache_dir=tmp_path / "cache"
    )
    path = fetch_pbf(HELSINKI_BBOX, **options)

    (call,) = area_extract
    assert (call["crop"], call["directory"], call["existed"]) == (False, out, True)
    assert (call["strategy"], call["output_path"]) == ("smallest_total", None)
    assert path == out / "geofabrik_finland-latest.osm.pbf"
    assert path.read_bytes() == PBF_BYTES

    sidecar = path.with_suffix(".provenance.json")
    provenance = json.loads(sidecar.read_text())
    digest = hashlib.sha256(PBF_BYTES).hexdigest()
    written = datetime.datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
    source = {
        "url": EXTRACT_URL,
        "provider": "Geofabrik",
        "extract": "finland",
        "bytes": len(PBF_BYTES),
        "sha256": digest,
        "snapshot": SNAPSHOT.isoformat(),
        "retrieved_at": written,
    }
    assert provenance == {
        "source_url": EXTRACT_URL,
        "provider": "Geofabrik",
        "extract": "finland",
        "extract_bytes": len(PBF_BYTES),
        "failed_extracts": [{"url": FAILED_URL, "error": "HTTP Error 503"}],
        "extract_sha256": digest,
        "file_sha256": digest,
        "cropped": False,
        "aoi_bounds": list(HELSINKI_BBOX),
        "retrieved_at": written,
        "sources": [source],
        "snapshot": SNAPSHOT.isoformat(),
        "must_cover_bounds": None,
    }

    # A repeat reuses the extract and keeps when it was downloaded; update
    # downloads it again.
    os.utime(path, (86400, 86400))
    retrieved = []
    for update in (False, True):
        fetch_pbf(HELSINKI_BBOX, update=update, **options)
        retrieved.append(json.loads(sidecar.read_text())["retrieved_at"])
    assert retrieved[0] == "1970-01-02T00:00:00+00:00" != retrieved[1]


def test_fetch_cropped(tmp_path, area_extract):
    out = tmp_path / "out"
    options = dict(directory=out, cache_dir=tmp_path / "cache")
    full = fetch_pbf(HELSINKI_BBOX, crop=False, cache_dir=tmp_path / "cache")
    area_extract.clear()
    path = fetch_pbf(HELSINKI_BBOX, **options)

    assert path == out / _crop_filename(HELSINKI_BBOX, box(*HELSINKI_BBOX))
    assert path.read_bytes() == CROP_BYTES
    (call,) = area_extract
    assert (call["crop"], call["directory"]) == (True, full.parent)
    assert call["strategy"] == "smallest_total"
    # pyrosm writes the crop in a staging directory beside its name.
    assert Path(call["output_path"]).parent.parent == out

    provenance = json.loads(path.with_suffix(".provenance.json").read_text())
    assert provenance["cropped"] is True
    assert provenance["file_sha256"] == hashlib.sha256(CROP_BYTES).hexdigest()
    assert provenance["extract_sha256"] == hashlib.sha256(PBF_BYTES).hexdigest()

    # A cached crop is returned without pyrosm; update, or a crop whose
    # sidecar is missing, fetches and crops again.
    assert fetch_pbf(HELSINKI_BBOX, **options) == path
    assert len(area_extract) == 1
    # The update replaced the extract, so its own sidecar went with it.
    full_sidecar = full.with_suffix(".provenance.json")
    os.utime(full, (0, 0))
    os.utime(full_sidecar, (86400, 86400))
    assert fetch_pbf(HELSINKI_BBOX, update=True, **options) == path
    assert not full_sidecar.exists()
    path.with_suffix(".provenance.json").unlink()
    assert fetch_pbf(HELSINKI_BBOX, **options) == path
    assert [call["update"] for call in area_extract] == [False, True, False]
    assert path.with_suffix(".provenance.json").exists()


def test_fetch_merged_extracts(tmp_path, area_extract):
    older = SNAPSHOT - datetime.timedelta(hours=5)
    movisda = ("Movisda", "SE-BD", MOVISDA_URL, 300, older)
    area_extract.sources = [FINLAND, movisda]
    out = tmp_path / "out"
    out.mkdir()
    for name, written in [("geofabrik_finland", 2), ("movisda_SE-BD", 1)]:
        source = out / f"{name}-latest.osm.pbf"
        source.write_bytes(PBF_BYTES)
        os.utime(source, (written * 86400, written * 86400))
    point = shapely.Point(24.9, 60.2)
    path = fetch_pbf(
        HELSINKI_BBOX, crop=False, must_cover=point, directory=out, cache_dir=tmp_path
    )

    (call,) = area_extract
    assert call["must_cover"].equals(point)
    assert path == out / "merged_0123456789ab.osm.pbf"
    digest = hashlib.sha256(PBF_BYTES).hexdigest()
    sources = [
        {
            "url": url,
            "provider": provider,
            "extract": extract,
            "bytes": size,
            "sha256": digest,
            "snapshot": snapshot.isoformat(),
            "retrieved_at": f"1970-01-0{day}T00:00:00+00:00",
        }
        for (provider, extract, url, size, snapshot), day in zip(
            area_extract.sources, (3, 2)
        )
    ]
    provenance = json.loads(path.with_suffix(".provenance.json").read_text())
    assert provenance == {
        "source_url": None,
        "provider": "Geofabrik+Movisda",
        "extract": "finland+SE-BD",
        "extract_bytes": len(PBF_BYTES) + 300,
        "failed_extracts": [{"url": FAILED_URL, "error": "HTTP Error 503"}],
        "extract_sha256": None,
        "file_sha256": hashlib.sha256(PBF_BYTES * 2).hexdigest(),
        "cropped": False,
        "aoi_bounds": list(HELSINKI_BBOX),
        "retrieved_at": "1970-01-02T00:00:00+00:00",
        "sources": sources,
        "snapshot": older.isoformat(),
        "must_cover_bounds": [24.9, 60.2, 24.9, 60.2],
    }


@pytest.mark.parametrize("buffer_m", [0, 1600])
def test_fetch_picks_and_crops_by_the_buffered_area(tmp_path, area_extract, buffer_m):
    polygon = box(24.6, 60.1, 25.2, 60.4).difference(box(24.6, 60.1, 24.9, 60.25))
    path = fetch_pbf(polygon, buffer_m=buffer_m, cache_dir=tmp_path)

    area = _buffered(polygon, buffer_m)
    assert area.equals(polygon) == (buffer_m == 0)
    (call,) = area_extract
    assert call["area"].equals(area) and call["crop"] is True
    provenance = json.loads(path.with_suffix(".provenance.json").read_text())
    assert provenance["aoi_bounds"] == list(area.bounds)


@pytest.mark.parametrize(
    "corners",
    [
        pytest.param([(24.9, 60.1)], id="one"),
        pytest.param([(24.9, 60.1), (42.9, 60.1)], id="1000-km-apart"),
        pytest.param([(179.9, -17.0)], id="antimeridian"),
    ],
)
def test_buffered_grows_each_part_by_the_distance(corners):
    squares = [box(x, y, x + 0.1, y + 0.1) for x, y in corners]
    geometry = shapely.union_all(squares)
    assert _buffered(geometry, 0) is geometry

    clipped = any(square.bounds[2] == 180 for square in squares)
    warns = pytest.warns(UserWarning, match="antimeridian")
    with warns if clipped else contextlib.nullcontext():
        grown = _buffered(geometry, 1600)
    assert -180 <= grown.bounds[0] and grown.bounds[2] <= 180
    for square in squares:
        minx, miny, maxx, maxy = square.bounds
        west, south, east, north = grown.intersection(square.buffer(1)).bounds
        # Metres per degree; the side on the antimeridian is clipped there.
        lon_m, lat_m = 111_320 * math.cos(math.radians(miny)), 111_320
        east_m = 0 if maxx == 180 else 1600
        assert (minx - west) * lon_m == pytest.approx(1600, rel=0.02)
        assert (east - maxx) * lon_m == pytest.approx(east_m, rel=0.02)
        assert (miny - south) * lat_m == pytest.approx(1600, rel=0.02)
        assert (north - maxy) * lat_m == pytest.approx(1600, rel=0.02)


def test_fetch_by_place_name(tmp_path, area_extract, monkeypatch):
    import pyrosm

    monkeypatch.setattr(pyrosm, "geocode", lambda query: box(*HELSINKI_BBOX))
    path = fetch_pbf("Helsinki, Finland", cache_dir=tmp_path)

    assert path.name == "helsinki-finland-fcb962ea.osm.pbf"


_NO_EXTRACT = "No Geofabrik, BBBike or Movisda extract contains the whole area."
_NOT_DOWNLOADED = (
    "Could not download any of the 1 extracts that contain the area: "
    f"{EXTRACT_URL} (timed out)"
)


@pytest.mark.parametrize(
    "aoi, must_cover, raised, expected, match",
    [
        pytest.param(
            HELSINKI_BBOX,
            None,
            PyrosmExtractNotFoundError(_NO_EXTRACT),
            ExtractNotFoundError,
            "No Geofabrik, BBBike or Movisda",
            id="no-extract",
        ),
        pytest.param(
            HELSINKI_BBOX,
            None,
            ExtractDownloadError(_NOT_DOWNLOADED, [(EXTRACT_URL, "timed out")]),
            DownloadError,
            EXTRACT_URL,
            id="not-downloaded",
        ),
        # Any other pyrosm error is not a missing extract.
        pytest.param(
            HELSINKI_BBOX,
            None,
            ValueError("changed while it was being merged"),
            ValueError,
            "being merged",
            id="other-pyrosm-error",
        ),
        pytest.param(
            (24.6, 60.1, 25.2, 60.1),
            None,
            None,
            ValueError,
            "has no area",
            id="no-area",
        ),
        pytest.param(
            HELSINKI_BBOX,
            shapely.Point(26.0, 60.2),
            None,
            ValueError,
            "must_cover lies outside",
            id="must-cover-outside",
        ),
    ],
)
def test_fetch_pbf_errors(
    tmp_path, monkeypatch, aoi, must_cover, raised, expected, match
):
    import pyrosm

    calls = []

    def get_data_by_area(area, **kwargs):
        calls.append(area)
        raise raised

    monkeypatch.setattr(pyrosm, "get_data_by_area", get_data_by_area)
    with pytest.raises(expected, match=match) as caught:
        fetch_pbf(aoi, crop=False, must_cover=must_cover, cache_dir=tmp_path)
    if raised is not None:
        assert raised in (caught.value, caught.value.__cause__)
    assert len(calls) == (raised is not None)


def test_as_geometry_validation():
    assert _as_geometry(HELSINKI_BBOX).bounds == HELSINKI_BBOX
    geom = box(*HELSINKI_BBOX)
    assert _as_geometry(geom) is geom
    with pytest.raises(ValueError):
        _as_geometry((24.6, 60.1))
    with pytest.raises(ValueError):
        _as_geometry((25.2, 60.4, 24.6, 60.1))
    with pytest.raises(ValueError):
        _as_geometry(12345)
