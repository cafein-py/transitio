"""The release contract a client reads: which releases it may take and why
a manifest is not one it can use."""

import json

import pytest
from index_fixture import manifest_bytes
from transitio.index import release as contract


@pytest.mark.parametrize(
    ("release", "reason"),
    [
        ({"tag_name": "index-0123456789abcdef", "draft": True}, None),
        ({"tag_name": "v1.0.0"}, None),
        ({"tag_name": "index-not-an-id"}, None),
        ({"tag_name": "index-0123456789abcdef", "assets": []}, "no readable manifest"),
    ],
)
def test_releases_a_client_must_not_take(release, reason):
    chosen, manifest, skipped = contract.newest_compatible([release], lambda a: None)
    assert chosen is None and manifest is None
    assert skipped == ([(release["tag_name"], reason)] if reason else [])


@pytest.mark.parametrize(
    ("manifest", "reason"),
    [
        ("text", "not an object"),
        (
            {"snapshot_id": "0123456789abcdef", "schema_version": [3]},
            "schema_version [3]",
        ),
        ({"schema_version": 4, "min_reader_version": "0.11.0"}, "no snapshot id"),
        ({"snapshot_id": "0123456789abcdef", "schema_version": 2}, "schema_version 2"),
        (
            {"snapshot_id": "0123456789abcdef", "schema_version": 4},
            "no min_reader_version",
        ),
        (
            {
                "snapshot_id": "0123456789abcdef",
                "schema_version": 4,
                "min_reader_version": "999.0.0",
            },
            "needs transitio >= 999.0.0",
        ),
    ],
)
def test_incompatible_manifests_say_why(manifest, reason):
    ok, why = contract.compatible(manifest)
    assert ok is False and reason in why


def test_releases_are_ordered_by_publication_not_commit_time():
    manifests = {
        "index-0000000000000001": manifest_bytes(snapshot_id="0000000000000001"),
        "index-0000000000000002": manifest_bytes(snapshot_id="0000000000000002"),
    }
    releases = [
        {
            "id": 1,
            "tag_name": "index-0000000000000001",
            "created_at": "2026-09-02T00:00:00Z",  # a later commit,
            "published_at": "2026-09-01T00:00:00Z",  # published first
            "assets": [{"name": "manifest.json", "tag": "index-0000000000000001"}],
        },
        {
            "id": 2,
            "tag_name": "index-0000000000000002",
            "created_at": "2026-09-01T00:00:00Z",
            "published_at": "2026-09-03T00:00:00Z",
            "assets": [{"name": "manifest.json", "tag": "index-0000000000000002"}],
        },
    ]
    _, manifest, _ = contract.newest_compatible(
        releases, lambda asset: json.loads(manifests[asset["tag"]])
    )
    assert manifest["snapshot_id"] == "0000000000000002"
