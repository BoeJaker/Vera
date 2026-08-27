"""Inspect and import one local DVC-tracked file without invoking DVC or Git."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from pathlib import Path, PurePosixPath
import re
from typing import Any

from vera.execution.run_protocol import ArtifactRef
from vera.fabric.artifact_provider import LocalArtifactProvider


DVC_ADAPTER_VERSION = "vera.dvc-artifact-adapter/v1"
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_MD5 = re.compile(r"^[0-9a-f]{32}$")
_KEY = re.compile(r"^[a-z][a-z0-9_]*$")
_REPOSITORY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_MAX_DESCRIPTOR_BYTES = 64 * 1024


@dataclass(frozen=True)
class DVCTrackedArtifact:
    repository: str
    git_revision: str
    descriptor: str
    descriptor_checksum: str
    path: str
    dvc_hash: str
    size_bytes: int
    cache_path: str
    schema_version: str = DVC_ADAPTER_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class DVCArtifactImport:
    artifact: ArtifactRef
    source: DVCTrackedArtifact

    def to_dict(self) -> dict:
        return {"artifact": asdict(self.artifact), "source": self.source.to_dict()}


def _clean_scalar(value: str) -> str:
    value = value.strip()
    if not value or value in {"|", ">"}:
        raise ValueError("DVC descriptor contains an unsupported scalar")
    if value[0:1] in {'"', "'"}:
        if len(value) < 2 or value[-1] != value[0]:
            raise ValueError("DVC descriptor contains an invalid quoted scalar")
        value = value[1:-1]
    if "#" in value or "\x00" in value:
        raise ValueError("DVC descriptor contains an unsupported scalar")
    return value


def _parse_single_output(text: str) -> dict[str, str]:
    """Parse the deliberately small standalone `.dvc` subset used by v1."""
    output: dict[str, str] = {}
    in_outputs = False
    output_count = 0
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if indent == 0 and not (in_outputs and line.startswith("-")):
            if line == "outs:":
                if in_outputs:
                    raise ValueError("DVC descriptor repeats outs")
                in_outputs = True
                continue
            if in_outputs:
                raise ValueError("DVC descriptor has unsupported top-level data after outs")
            # DVC may include its own stage checksum before outs. It is recorded
            # by Git but is not authority for the cached output.
            if line.startswith("md5:"):
                continue
            raise ValueError("only standalone DVC file descriptors are supported")
        if not in_outputs:
            raise ValueError("DVC descriptor has nested data before outs")
        if line.startswith("-"):
            output_count += 1
            if output_count != 1:
                raise ValueError("exactly one DVC output is supported")
            line = line[1:].strip()
        elif output_count != 1:
            raise ValueError("DVC output list is malformed")
        if ":" not in line:
            raise ValueError("DVC output field is malformed")
        key, value = line.split(":", 1)
        key = key.strip()
        if not _KEY.fullmatch(key) or key in output:
            raise ValueError("DVC output field is invalid or repeated")
        output[key] = _clean_scalar(value)
    if not in_outputs or output_count != 1:
        raise ValueError("exactly one DVC output is required")
    allowed = {"md5", "hash", "size", "path", "cache", "isexec"}
    if set(output) - allowed:
        raise ValueError("DVC output contains unsupported fields")
    return output


def _git_head(repo: Path) -> str:
    git_dir = (repo / ".git").resolve()
    if not git_dir.is_relative_to(repo) or not git_dir.is_dir():
        raise ValueError("repository must have an in-tree .git directory")
    head_path = (git_dir / "HEAD").resolve()
    if not head_path.is_relative_to(git_dir):
        raise ValueError("Git HEAD escapes .git")
    if not head_path.is_file() or head_path.stat().st_size > 4096:
        raise ValueError("Git HEAD is missing or exceeds size limit")
    head = head_path.read_text(encoding="utf-8").strip()
    if _COMMIT.fullmatch(head):
        return head
    if not head.startswith("ref: "):
        raise ValueError("Git HEAD is invalid")
    ref = head[5:]
    if not ref.startswith("refs/") or ".." in PurePosixPath(ref).parts:
        raise ValueError("Git HEAD reference is invalid")
    ref_path = (git_dir / PurePosixPath(ref)).resolve()
    if not ref_path.is_relative_to(git_dir.resolve()):
        raise ValueError("Git HEAD reference escapes .git")
    try:
        if ref_path.stat().st_size > 4096:
            raise ValueError("Git HEAD reference exceeds size limit")
        value = ref_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        value = ""
        packed = (git_dir / "packed-refs").resolve()
        if not packed.is_relative_to(git_dir):
            raise ValueError("Git packed refs escape .git")
        if packed.is_file():
            if packed.stat().st_size > 1024 * 1024:
                raise ValueError("Git packed refs exceed size limit")
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.startswith(("#", "^")) or " " not in line:
                    continue
                candidate, name = line.split(" ", 1)
                if name == ref:
                    value = candidate
                    break
    if not _COMMIT.fullmatch(value):
        raise ValueError("Git HEAD does not resolve to a full commit")
    return value


class DVCArtifactAdapter:
    """Read-only adapter for one cached file described by one `.dvc` file."""

    def __init__(self, repository: str | Path, *, repository_id: str,
                 git_revision: str,
                 descriptor: str | Path, max_import_bytes: int = 64 * 1024 * 1024):
        self.repository = Path(repository).resolve()
        if not self.repository.is_dir():
            raise ValueError("DVC repository does not exist")
        repository_id = str(repository_id or "").strip()
        if not _REPOSITORY_ID.fullmatch(repository_id):
            raise ValueError("invalid repository_id")
        self._repository_id = repository_id
        git_revision = str(git_revision or "").lower()
        if not _COMMIT.fullmatch(git_revision):
            raise ValueError("git_revision must be a full 40-character commit")
        if _git_head(self.repository) != git_revision:
            raise ValueError("repository HEAD does not match pinned git_revision")
        relative = PurePosixPath(str(descriptor).replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".dvc":
            raise ValueError("descriptor must be a relative .dvc path")
        self.descriptor = (self.repository / relative).resolve()
        if not self.descriptor.is_relative_to(self.repository) or not self.descriptor.is_file():
            raise ValueError("DVC descriptor is missing or escapes repository")
        if self.descriptor.stat().st_size > _MAX_DESCRIPTOR_BYTES:
            raise ValueError("DVC descriptor exceeds size limit")
        try:
            descriptor_bytes = self.descriptor.read_bytes()
            text = descriptor_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("DVC descriptor must be UTF-8") from exc
        output = _parse_single_output(text)
        digest = output.get("md5", "").lower()
        if not _MD5.fullmatch(digest) or output.get("hash", "md5").lower() != "md5":
            raise ValueError("DVC output must use a full MD5 content hash")
        if digest.endswith(".dir") or "nfiles" in output:
            raise ValueError("DVC directory outputs are not supported")
        if output.get("cache", "true").lower() not in {"true", "yes"}:
            raise ValueError("uncached DVC outputs are not supported")
        try:
            size = int(output.get("size", ""))
        except ValueError as exc:
            raise ValueError("DVC output size is invalid") from exc
        if size < 0 or size > int(max_import_bytes):
            raise ValueError("DVC output exceeds import size limit")
        tracked = PurePosixPath(output.get("path", ""))
        if not tracked.parts or tracked.is_absolute() or ".." in tracked.parts:
            raise ValueError("DVC output path is invalid")
        cache_root = (self.repository / ".dvc" / "cache" / "files" / "md5").resolve()
        cache_path = (cache_root / digest[:2] / digest[2:]).resolve()
        if not cache_path.is_relative_to(cache_root):
            raise ValueError("DVC cache path escapes local cache")
        self._digest = digest
        self._size = size
        self._tracked_path = tracked.as_posix()
        self._cache_path = cache_path
        self._git_revision = git_revision
        self._max_import_bytes = max(1, int(max_import_bytes))
        self._descriptor_checksum = "sha256:" + hashlib.sha256(
            descriptor_bytes).hexdigest()

    def _check_source_identity(self) -> None:
        if _git_head(self.repository) != self._git_revision:
            raise OSError("repository HEAD changed after adapter construction")
        current = self.descriptor.resolve()
        if current != self.descriptor or not current.is_relative_to(self.repository):
            raise OSError("DVC descriptor path changed after adapter construction")
        if not current.is_file() or current.stat().st_size > _MAX_DESCRIPTOR_BYTES:
            raise OSError("DVC descriptor changed after adapter construction")
        checksum = "sha256:" + hashlib.sha256(current.read_bytes()).hexdigest()
        if checksum != self._descriptor_checksum:
            raise OSError("DVC descriptor changed after adapter construction")

    def _read_verified(self) -> bytes:
        self._check_source_identity()
        try:
            if not self._cache_path.is_file():
                raise FileNotFoundError
            if self._cache_path.stat().st_size > self._max_import_bytes:
                raise ValueError("DVC cache object exceeds import size limit")
            data = self._cache_path.read_bytes()
        except FileNotFoundError as exc:
            raise FileNotFoundError("DVC cache object is missing") from exc
        if len(data) > self._max_import_bytes:
            raise ValueError("DVC cache object exceeds import size limit")
        if len(data) != self._size:
            raise OSError("DVC cache object size mismatch")
        if hashlib.md5(data, usedforsecurity=False).hexdigest() != self._digest:
            raise OSError("DVC cache object hash mismatch")
        return data

    def _source(self) -> DVCTrackedArtifact:
        return DVCTrackedArtifact(
            repository=self._repository_id,
            git_revision=self._git_revision,
            descriptor=self.descriptor.relative_to(self.repository).as_posix(),
            descriptor_checksum=self._descriptor_checksum,
            path=self._tracked_path,
            dvc_hash="md5:" + self._digest,
            size_bytes=self._size,
            cache_path=f".dvc/cache/files/md5/{self._digest[:2]}/{self._digest[2:]}",
        )

    def inspect(self) -> DVCTrackedArtifact:
        self._read_verified()
        return self._source()

    def import_artifact(self, artifacts: LocalArtifactProvider, *, created_at: str,
                        media_type: str = "application/octet-stream") -> DVCArtifactImport:
        data = self._read_verified()
        source = self._source()
        stat = artifacts.put(data, media_type=media_type, created_at=created_at)
        return DVCArtifactImport(
            artifact=ArtifactRef(
                id=stat.artifact_id,
                kind="dvc-tracked-file",
                uri=f"fabric://artifacts/{stat.artifact_id}",
                checksum=stat.checksum,
                media_type=stat.media_type,
                size_bytes=stat.size,
            ),
            source=source,
        )
