import json

import httpx
import pytest
from shapely.geometry import box

from transitio.exceptions import ExtractNotFoundError
from transitio.osm import fetch_pbf
from transitio.osm._fetch import _as_geometry, _crop_filename

pytest.importorskip("pyrosm")

HELSINKI_BBOX = (24.6, 60.1, 25.2, 60.4)
PBF_BYTES = b"\x00fake-pbf-payload"


def make_transport(requests=None):
    def handler(request):
        if requests is not None:
            requests.append(request)
        return httpx.Response(200, content=PBF_BYTES)

    return httpx.MockTransport(handler)


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


def test_fetch_full_extract(tmp_path):
    requests = []
    path = fetch_pbf(
        HELSINKI_BBOX,
        crop=False,
        cache_dir=tmp_path,
        transport=make_transport(requests),
    )

    assert path.exists()
    assert path.read_bytes() == PBF_BYTES
    assert path.name.endswith(".osm.pbf")
    assert "finland" in path.name

    (request,) = requests
    assert request.url.host == "download.geofabrik.de"

    provenance = json.loads(path.with_suffix(".provenance.json").read_text())
    assert provenance["cropped"] is False
    assert provenance["source_url"] == str(request.url)
    assert provenance["file_sha256"] == provenance["extract_sha256"]
    assert provenance["aoi_bounds"] == list(HELSINKI_BBOX)


def test_fetch_full_extract_is_cached(tmp_path):
    requests = []
    transport = make_transport(requests)
    first = fetch_pbf(
        HELSINKI_BBOX, crop=False, cache_dir=tmp_path, transport=transport
    )
    second = fetch_pbf(
        HELSINKI_BBOX, crop=False, cache_dir=tmp_path, transport=transport
    )

    assert first == second
    assert len(requests) == 1


def test_fetch_cropped(tmp_path, fake_osm):
    path = fetch_pbf(HELSINKI_BBOX, cache_dir=tmp_path, transport=make_transport())

    assert path.name == _crop_filename(HELSINKI_BBOX, box(*HELSINKI_BBOX))
    assert path.read_bytes() == b"\x00cropped-pbf"

    (osm,) = fake_osm.instances
    assert osm.bounding_box.bounds == HELSINKI_BBOX

    provenance = json.loads(path.with_suffix(".provenance.json").read_text())
    assert provenance["cropped"] is True
    assert provenance["file_sha256"] != provenance["extract_sha256"]


def test_fetch_cropped_polygon_uses_true_geometry(tmp_path, fake_osm):
    triangle = box(24.6, 60.1, 25.2, 60.4).difference(box(24.6, 60.1, 24.9, 60.25))
    fetch_pbf(triangle, cache_dir=tmp_path, transport=make_transport())

    (osm,) = fake_osm.instances
    assert osm.bounding_box.equals(triangle)


def test_fetch_by_place_name(tmp_path, fake_osm, monkeypatch):
    import pyrosm

    monkeypatch.setattr(pyrosm, "geocode", lambda query: box(*HELSINKI_BBOX))
    path = fetch_pbf(
        "Helsinki, Finland", cache_dir=tmp_path, transport=make_transport()
    )

    assert path.name == "helsinki-finland-fcb962ea.osm.pbf"


def test_fetch_no_single_covering_extract(tmp_path):
    # Spans North America and Europe; no single Geofabrik extract covers both.
    with pytest.raises(ExtractNotFoundError):
        fetch_pbf(
            (-75, 35, -8, 45),
            crop=False,
            cache_dir=tmp_path,
            transport=make_transport(),
        )


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
