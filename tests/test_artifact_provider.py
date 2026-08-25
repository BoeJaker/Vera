import pytest

from vera.fabric.artifact_provider import LocalArtifactProvider


pytestmark = pytest.mark.critical
NOW = "2026-01-01T00:00:00Z"
LATER = "2027-01-01T00:00:00Z"


def test_checksum_addressed_put_get_stat_verify_and_duplicate(tmp_path):
    provider = LocalArtifactProvider(tmp_path)
    first = provider.put(b"hello", media_type="text/plain", created_at=NOW)
    replay = provider.put(b"hello", media_type="text/plain", created_at=NOW)
    assert replay == first
    assert first.artifact_id.startswith("art_") and first.size == 5
    assert provider.get(first.artifact_id) == b"hello"
    assert provider.verify(first.artifact_id)
    with pytest.raises(ValueError, match="media_type differs"):
        provider.put(b"hello", media_type="application/octet-stream", created_at=NOW)


def test_missing_corrupt_large_and_bounded_read(tmp_path):
    provider = LocalArtifactProvider(tmp_path, max_put_bytes=5)
    with pytest.raises(ValueError, match="size limit"):
        provider.put(b"123456", media_type="text/plain", created_at=NOW)
    stat = provider.put(b"12345", media_type="text/plain", created_at=NOW)
    with pytest.raises(ValueError, match="read limit"):
        provider.get(stat.artifact_id, max_bytes=4)
    provider._path(stat.artifact_id).write_bytes(b"xxxxx")
    assert not provider.verify(stat.artifact_id)
    with pytest.raises(KeyError, match="not found"):
        provider.stat("art_" + "f" * 64)


def test_references_are_idempotent_and_cannot_be_retargeted(tmp_path):
    provider = LocalArtifactProvider(tmp_path)
    first = provider.put(b"one", media_type="text/plain", created_at=NOW)
    second = provider.put(b"two", media_type="text/plain", created_at=NOW)
    ref = provider.reference(first.artifact_id, "run:one", created_at=NOW)
    assert provider.reference(first.artifact_id, "run:one", created_at=NOW) == ref
    with pytest.raises(ValueError, match="another artifact"):
        provider.reference(second.artifact_id, "run:one", created_at=NOW)


def test_duplicate_put_can_extend_but_not_shorten_retention(tmp_path):
    provider = LocalArtifactProvider(tmp_path)
    first = provider.put(b"held", media_type="text/plain", created_at=NOW,
                         retain_until=NOW)
    extended = provider.put(b"held", media_type="text/plain", created_at=NOW,
                            retain_until=LATER)
    shortened = provider.put(b"held", media_type="text/plain", created_at=NOW,
                             retain_until=NOW)
    assert first.retain_until == NOW
    assert extended.retain_until == LATER
    assert shortened.retain_until == LATER


def test_partial_file_is_removed_when_atomic_publish_fails(tmp_path, monkeypatch):
    provider = LocalArtifactProvider(tmp_path)
    def fail(*args):
        raise OSError("injected publish failure")
    monkeypatch.setattr("vera.fabric.artifact_provider.os.replace", fail)
    with pytest.raises(OSError, match="injected"):
        provider.put(b"hello", media_type="text/plain", created_at=NOW)
    assert list(provider.objects.rglob(".partial-*")) == []
