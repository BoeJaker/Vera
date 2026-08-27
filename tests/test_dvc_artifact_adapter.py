import hashlib
from pathlib import Path

import pytest

from vera.fabric.artifact_provider import LocalArtifactProvider
from vera.fabric.dvc_artifact_adapter import DVCArtifactAdapter


pytestmark = pytest.mark.critical
REVISION = "a" * 40
NOW = "2026-08-27T00:00:00Z"
REPOSITORY_ID = "fixture/dvc-source"


def adapter(repo, **kwargs):
    return DVCArtifactAdapter(repo, repository_id=REPOSITORY_ID,
                              git_revision=REVISION, descriptor="data.dvc",
                              **kwargs)


def write_repo(tmp_path: Path, data=b"frozen-data", *, descriptor=None,
               revision=REVISION):
    repo = tmp_path / "source"
    (repo / ".git" / "refs" / "heads").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (repo / ".git" / "refs" / "heads" / "main").write_text(revision + "\n")
    digest = hashlib.md5(data, usedforsecurity=False).hexdigest()
    cache = repo / ".dvc" / "cache" / "files" / "md5" / digest[:2] / digest[2:]
    cache.parent.mkdir(parents=True)
    cache.write_bytes(data)
    text = descriptor or (
        "outs:\n"
        f"- md5: {digest}\n"
        f"  size: {len(data)}\n"
        "  hash: md5\n"
        "  path: data/frozen.bin\n"
    )
    (repo / "data.dvc").write_text(text)
    return repo, digest, cache


def tree_state(root: Path):
    return {path.relative_to(root).as_posix(): (path.stat().st_size,
                                                path.read_bytes())
            for path in root.rglob("*") if path.is_file()}


def test_inspect_verifies_pinned_git_and_cache_without_mutation(tmp_path):
    repo, digest, _ = write_repo(tmp_path)
    before = tree_state(repo)
    result = adapter(repo).inspect()
    assert result.repository == REPOSITORY_ID
    assert result.git_revision == REVISION
    assert result.path == "data/frozen.bin"
    assert result.descriptor_checksum.startswith("sha256:")
    assert result.dvc_hash == "md5:" + digest
    assert result.cache_path.endswith(digest[:2] + "/" + digest[2:])
    assert tree_state(repo) == before


def test_import_copies_verified_bytes_to_vera_sha256_artifact(tmp_path):
    repo, digest, _ = write_repo(tmp_path, b"model")
    artifacts = LocalArtifactProvider(tmp_path / "vera-artifacts")
    result = adapter(repo).import_artifact(
            artifacts, created_at=NOW, media_type="application/octet-stream")
    assert result.artifact.kind == "dvc-tracked-file"
    assert result.artifact.uri == f"fabric://artifacts/{result.artifact.id}"
    assert result.artifact.checksum.startswith("sha256:")
    assert artifacts.get(result.artifact.id) == b"model"
    assert result.source.dvc_hash == "md5:" + digest


def test_missing_or_corrupt_cache_fails_closed(tmp_path):
    repo, _, cache = write_repo(tmp_path)
    value = adapter(repo)
    cache.unlink()
    with pytest.raises(FileNotFoundError, match="cache object"):
        value.inspect()
    repo, _, cache = write_repo(tmp_path / "corrupt")
    value = adapter(repo)
    cache.write_bytes(b"frozen-datx")
    with pytest.raises(OSError, match="hash mismatch"):
        value.inspect()


def test_size_mismatch_and_limit_fail_closed(tmp_path):
    data = b"12345"
    digest = hashlib.md5(data, usedforsecurity=False).hexdigest()
    descriptor = f"outs:\n- md5: {digest}\n  size: 4\n  path: data.bin\n"
    repo, _, _ = write_repo(tmp_path, data, descriptor=descriptor)
    with pytest.raises(OSError, match="size mismatch"):
        adapter(repo).inspect()
    repo, _, _ = write_repo(tmp_path / "limit", data)
    with pytest.raises(ValueError, match="import size limit"):
        adapter(repo, max_import_bytes=4)


def test_revision_must_be_full_and_match_read_only_head(tmp_path):
    repo, _, _ = write_repo(tmp_path)
    with pytest.raises(ValueError, match="full 40"):
        DVCArtifactAdapter(repo, repository_id=REPOSITORY_ID,
                           git_revision="main", descriptor="data.dvc")
    with pytest.raises(ValueError, match="does not match"):
        DVCArtifactAdapter(repo, repository_id=REPOSITORY_ID,
                           git_revision="b" * 40, descriptor="data.dvc")
    (repo / ".git" / "refs" / "heads" / "main").unlink()
    (repo / ".git" / "packed-refs").write_text(f"{REVISION} refs/heads/main\n")
    assert adapter(repo).inspect().git_revision == REVISION


def test_head_or_descriptor_change_after_binding_is_rejected(tmp_path):
    repo, _, _ = write_repo(tmp_path)
    value = adapter(repo)
    (repo / ".git" / "refs" / "heads" / "main").write_text("b" * 40 + "\n")
    with pytest.raises(OSError, match="HEAD changed"):
        value.inspect()
    repo, _, _ = write_repo(tmp_path / "descriptor")
    value = adapter(repo)
    with (repo / "data.dvc").open("a") as handle:
        handle.write("# changed\n")
    with pytest.raises(OSError, match="descriptor changed"):
        value.inspect()


@pytest.mark.parametrize("descriptor", ["../data.dvc", "/data.dvc", "data.yaml"])
def test_descriptor_path_escape_and_wrong_suffix_are_rejected(tmp_path, descriptor):
    repo, _, _ = write_repo(tmp_path)
    with pytest.raises(ValueError, match="relative .dvc"):
        DVCArtifactAdapter(repo, repository_id=REPOSITORY_ID,
                           git_revision=REVISION, descriptor=descriptor)


@pytest.mark.parametrize("body, message", [
    ("outs:\n- md5: 0123456789abcdef0123456789abcdef\n  size: 1\n"
     "  path: a\n- md5: 0123456789abcdef0123456789abcdef\n  size: 1\n  path: b\n",
     "exactly one"),
    ("outs:\n- md5: 0123456789abcdef0123456789abcdef\n  size: 1\n"
     "  path: a\n  cache: false\n", "uncached"),
    ("stages:\n  train:\n    cmd: touch owned\n", "standalone"),
    ("outs:\n- md5: 0123456789abcdef0123456789abcdef.dir\n  size: 1\n"
     "  path: directory\n", "full MD5"),
    ("outs:\n- md5: 0123456789abcdef0123456789abcdef\n  size: 1\n"
     "  path: ../secret\n", "path is invalid"),
])
def test_unsupported_pipeline_directory_uncached_and_multi_output_fail(tmp_path,
                                                                       body, message):
    repo, _, _ = write_repo(tmp_path, descriptor=body)
    with pytest.raises(ValueError, match=message):
        adapter(repo)


def test_descriptor_and_cache_symlinks_cannot_escape_repository(tmp_path):
    if not hasattr(Path, "symlink_to"):
        pytest.skip("symlinks unavailable")
    repo, digest, cache = write_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.write_bytes(b"outs: []\n")
    (repo / "data.dvc").unlink()
    try:
        (repo / "data.dvc").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError, match="escapes repository"):
        adapter(repo)
    (repo / "data.dvc").unlink()
    (repo / "data.dvc").write_text(
        f"outs:\n- md5: {digest}\n  size: 11\n  path: data.bin\n")
    cache.unlink()
    cache.symlink_to(outside)
    with pytest.raises(ValueError, match="cache path escapes"):
        adapter(repo)
