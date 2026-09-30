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

HELSINKI_BBOX = (24.6, 60.1, 25.2, 60.4)
PBF_BYTES = b"\x00fake-pbf-payload"
EXTRACT_URL = "https://download.geofabrik.de/europe/finland-latest.osm.pbf"
FAILED_URL = "https://download.bbbike.org/osm/bbbike/Helsinki/Helsinki.osm.pbf"


class FakeOSM:
    """Stands in for pyrosm.OSM: records the crop geometry, writes the target."""

    instances = []

    def __init__(self, filepath, bounding_box=None):
        self.filepath = filepath
        self.bounding_box = bounding_box
        FakeOSM.instances.append(self)

    def to_pbf(self, output_path=None):
        with open(output_path, "wb") as handle:
            handle.write(b"\x00cropped-pbf")
        return output_path


@pytest.fixture
def fake_osm(monkeypatch):
    import pyrosm

    FakeOSM.instances = []
    monkeypatch.setattr(pyrosm, "OSM", FakeOSM)
    return FakeOSM


@pytest.fixture
def area_extract(monkeypatch):
    """Stands in for pyrosm.get_data_by_area: records each call and, when
    missing or on update, writes Geofabrik's Finland extract into the
    directory it is given."""
    import pyrosm

    calls = []

    def get_data_by_area(area, crop=True, update=False, directory=None, **kwargs):
        directory = Path(directory)
        calls.append(
            dict(
                area=area,
                crop=crop,
                update=update,
                directory=directory,
                existed=directory.is_dir(),
            )
        )
        path = directory / "geofabrik_finland-latest.osm.pbf"
        if update or not path.exists():
            path.write_bytes(PBF_BYTES)
        return types.SimpleNamespace(
            path=str(path),
            provider="Geofabrik",
            extract="finland",
            url=EXTRACT_URL,
            bytes=len(PBF_BYTES),
            failed=[(FAILED_URL, "HTTP Error 503")],
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
    assert path == out / "geofabrik_finland-latest.osm.pbf"
    assert path.read_bytes() == PBF_BYTES

    sidecar = path.with_suffix(".provenance.json")
    provenance = json.loads(sidecar.read_text())
    digest = hashlib.sha256(PBF_BYTES).hexdigest()
    written = datetime.datetime.fromtimestamp(
        path.stat().st_mtime, datetime.timezone.utc
    )
    assert provenance.pop("retrieved_at") == written.isoformat()
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
    }

    # A repeat reuses the extract and keeps when it was downloaded; update
    # downloads it again.
    os.utime(path, (86400, 86400))
    retrieved = []
    for update in (False, True):
        fetch_pbf(HELSINKI_BBOX, update=update, **options)
        retrieved.append(json.loads(sidecar.read_text())["retrieved_at"])
    assert retrieved[0] == "1970-01-02T00:00:00+00:00" != retrieved[1]


def test_fetch_cropped(tmp_path, area_extract, fake_osm):
    out = tmp_path / "out"
    options = dict(directory=out, cache_dir=tmp_path / "cache")
    full = fetch_pbf(HELSINKI_BBOX, crop=False, cache_dir=tmp_path / "cache")
    area_extract.clear()
    path = fetch_pbf(HELSINKI_BBOX, **options)

    assert path == out / _crop_filename(HELSINKI_BBOX, box(*HELSINKI_BBOX))
    assert path.read_bytes() == b"\x00cropped-pbf"
    extract = tmp_path / "cache" / "osm" / "geofabrik_finland-latest.osm.pbf"
    (call,) = area_extract
    assert (call["crop"], call["directory"]) == (False, extract.parent)
    (osm,) = fake_osm.instances
    assert (osm.filepath, osm.bounding_box.bounds) == (str(extract), HELSINKI_BBOX)

    provenance = json.loads(path.with_suffix(".provenance.json").read_text())
    assert provenance["cropped"] is True
    assert provenance["file_sha256"] != provenance["extract_sha256"]

    # A cached crop is returned without pyrosm; update, or a crop whose
    # sidecar is missing, fetches and crops again.
    assert fetch_pbf(HELSINKI_BBOX, **options) == path
    assert (len(area_extract), len(fake_osm.instances)) == (1, 1)
    # The update replaced the extract, so its own sidecar went with it.
    assert full.with_suffix(".provenance.json").exists()
    assert fetch_pbf(HELSINKI_BBOX, update=True, **options) == path
    assert not full.with_suffix(".provenance.json").exists()
    path.with_suffix(".provenance.json").unlink()
    assert fetch_pbf(HELSINKI_BBOX, **options) == path
    assert [call["update"] for call in area_extract] == [False, True, False]
    assert len(fake_osm.instances) == 3
    assert path.with_suffix(".provenance.json").exists()


@pytest.mark.parametrize("buffer_m", [0, 1600])
def test_fetch_picks_and_crops_by_the_buffered_area(
    tmp_path, area_extract, fake_osm, buffer_m
):
    polygon = box(24.6, 60.1, 25.2, 60.4).difference(box(24.6, 60.1, 24.9, 60.25))
    path = fetch_pbf(polygon, buffer_m=buffer_m, cache_dir=tmp_path)

    area = _buffered(polygon, buffer_m)
    assert area.equals(polygon) == (buffer_m == 0)
    (osm,) = fake_osm.instances
    assert area_extract[0]["area"].equals(area) and osm.bounding_box.equals(area)
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


def test_fetch_by_place_name(tmp_path, area_extract, fake_osm, monkeypatch):
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
    "aoi, raised, expected, match",
    [
        pytest.param(
            HELSINKI_BBOX,
            ValueError(_NO_EXTRACT),
            ExtractNotFoundError,
            "No Geofabrik, BBBike or Movisda",
            id="no-extract",
        ),
        pytest.param(
            HELSINKI_BBOX,
            ExtractDownloadError(_NOT_DOWNLOADED, [(EXTRACT_URL, "timed out")]),
            DownloadError,
            EXTRACT_URL,
            id="not-downloaded",
        ),
        pytest.param(
            (24.6, 60.1, 25.2, 60.1), None, ValueError, "has no area", id="no-area"
        ),
    ],
)
def test_fetch_pbf_errors(tmp_path, monkeypatch, aoi, raised, expected, match):
    import pyrosm

    calls = []

    def get_data_by_area(area, **kwargs):
        calls.append(area)
        raise raised

    monkeypatch.setattr(pyrosm, "get_data_by_area", get_data_by_area)
    with pytest.raises(expected, match=match) as caught:
        fetch_pbf(aoi, crop=False, cache_dir=tmp_path)
    assert caught.value.__cause__ is raised
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
