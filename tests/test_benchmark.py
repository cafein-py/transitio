import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("beanpicker._core")

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "benchmark_validator.py"

GTFS = {
    "agency.txt": (
        "agency_id,agency_name,agency_url,agency_timezone\n"
        "hsl,HSL,https://hsl.fi,Europe/Helsinki\n"
    ),
    "stops.txt": (
        "stop_id,stop_name,stop_lat,stop_lon\n"
        "s1,Kamppi,60.169,24.931\ns2,Steissi,60.171,24.941\n"
    ),
    "routes.txt": "route_id,agency_id,route_short_name,route_type\nr1,hsl,1,3\n",
    "trips.txt": "route_id,service_id,trip_id\nr1,wk,t1\n",
    "stop_times.txt": (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "t1,08:00:00,08:00:00,s1,1\nt1,08:05:00,08:05:00,s2,2\n"
    ),
    "calendar.txt": (
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
        "start_date,end_date\nwk,1,1,1,1,1,0,0,20260101,20261231\n"
    ),
}


def test_benchmark_cli_end_to_end(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in GTFS.items():
            archive.writestr(name, content)
    feed = tmp_path / "mini.zip"
    feed.write_bytes(buffer.getvalue())
    (tmp_path / "mini.canonical.json").write_text(
        json.dumps(
            {
                "notices": [
                    {"code": "unused_shape", "severity": "WARNING", "totalNotices": 3}
                ]
            }
        )
    )

    out = tmp_path / "results.json"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(REPO / "python"), env.get("PYTHONPATH")])
    )
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), str(feed), "--runs", "1", "--json", str(out)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr

    (result,) = json.loads(out.read_text())
    assert result["feed"] == str(feed)
    assert result["bestSeconds"] > 0
    assert result["rowTotal"] > 0
    assert result["parity"]["hostedOnly"] == ["unused_shape"]
    assert "parity" in completed.stdout


def test_benchmark_checksum_pairing(tmp_path):
    import hashlib

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in GTFS.items():
            archive.writestr(name, content)
    feed = tmp_path / "mini.zip"
    feed.write_bytes(buffer.getvalue())
    sha = hashlib.sha256(feed.read_bytes()).hexdigest()

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(REPO / "python"), env.get("PYTHONPATH")])
    )

    (tmp_path / "mini.canonical.json").write_text(
        json.dumps({"feedSha256": sha, "notices": []})
    )
    out = tmp_path / "results.json"
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), str(feed), "--runs", "1", "--json", str(out)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    (result,) = json.loads(out.read_text())
    assert result["pairingVerified"] is True
    assert result["feedSha256"] == sha

    (tmp_path / "mini.canonical.json").write_text(
        json.dumps({"feedSha256": "0" * 64, "notices": []})
    )
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), str(feed), "--runs", "1"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.returncode != 0
    assert "sha256" in completed.stderr


def test_benchmark_json_must_not_overwrite_corpus(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in GTFS.items():
            archive.writestr(name, content)
    feed = tmp_path / "mini.zip"
    feed.write_bytes(buffer.getvalue())
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), str(feed), "--runs", "1", "--json", str(feed)],
        capture_output=True,
        text=True,
        env=dict(
            os.environ,
            PYTHONPATH=os.pathsep.join(
                filter(None, [str(REPO / "python"), os.environ.get("PYTHONPATH")])
            ),
        ),
    )
    assert completed.returncode == 2
    assert "must not overwrite" in completed.stderr


def test_benchmark_rejects_zero_runs(tmp_path):
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path / "x.zip"), "--runs", "0"],
        capture_output=True,
        text=True,
        env=dict(
            os.environ,
            PYTHONPATH=os.pathsep.join(
                filter(None, [str(REPO / "python"), os.environ.get("PYTHONPATH")])
            ),
        ),
    )
    assert completed.returncode == 2
    assert "must be >= 1" in completed.stderr


def test_benchmark_cli_missing_feed(tmp_path):
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path / "absent.zip")],
        capture_output=True,
        text=True,
        env=dict(
            os.environ,
            PYTHONPATH=os.pathsep.join(
                filter(None, [str(REPO / "python"), os.environ.get("PYTHONPATH")])
            ),
        ),
    )
    assert completed.returncode == 2
    assert "feed not found" in completed.stderr
