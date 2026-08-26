"""Dependency-injected Hugging Face Datasets adapter.

The module never imports ``datasets`` and never performs work at import time.
Callers explicitly inject ``datasets.load_dataset`` and the installed package
version. This keeps discovery/offline validation free of network and cache side
effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import base64
import hashlib
import json
import re
from typing import Any, Callable, Mapping

from .dataset_provider import (
    CancellationSignal, DatasetSnapshot, QueryCancelled, _json_copy,
)


ADAPTER_VERSION = "vera.huggingface-dataset-adapter/v1"
SUPPORTED_DATASETS_VERSION = "4.8.4"
MAX_IMPORT_RECORDS = 100_000
MAX_STREAM_PAGE = 1_000
MAX_CURSOR_CHARS = 16_384
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,255}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}(?:[A-Za-z0-9.+-]*)$")


class HuggingFaceAdapterError(RuntimeError):
    pass


def _canonical(value: Any, field_name: str) -> Any:
    return _json_copy(value, field_name)


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class HuggingFaceSource:
    repo_id: str
    revision: str
    split: str
    config: str = ""
    dataset_id: str = ""
    adapter_version: str = ADAPTER_VERSION

    def __post_init__(self) -> None:
        repo_id = str(self.repo_id or "").strip()
        revision = str(self.revision or "").strip().lower()
        split = str(self.split or "").strip()
        config = str(self.config or "").strip()
        if not _REPO.fullmatch(repo_id):
            raise ValueError("repo_id must be an owner/name Hub dataset")
        if not _COMMIT.fullmatch(revision):
            raise ValueError("revision must be a pinned 40-character commit SHA")
        if not _PART.fullmatch(split):
            raise ValueError("invalid split")
        if config and not _PART.fullmatch(config):
            raise ValueError("invalid config")
        if self.adapter_version != ADAPTER_VERSION:
            raise ValueError("unsupported Hugging Face adapter version")
        dataset_id = str(self.dataset_id or "").strip()
        if not dataset_id:
            stem = re.sub(r"[^A-Za-z0-9._-]", "_", repo_id)
            variant = re.sub(r"[^A-Za-z0-9._-]", "_", config or "default")
            dataset_id = f"hf.{stem}.{variant}.{split}"
            if len(dataset_id) > 256:
                suffix = _digest({"repo_id": repo_id, "config": config,
                                  "split": split})[:24]
                dataset_id = f"hf.{stem[:180]}.{suffix}"
        if not _PART.fullmatch(dataset_id):
            raise ValueError("invalid dataset_id")
        object.__setattr__(self, "repo_id", repo_id)
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "split", split)
        object.__setattr__(self, "config", config)
        object.__setattr__(self, "dataset_id", dataset_id)

    @property
    def source_id(self) -> str:
        return "hfsrc_" + _digest({
            "adapter_version": self.adapter_version, "repo_id": self.repo_id,
            "revision": self.revision, "config": self.config,
            "split": self.split, "dataset_id": self.dataset_id,
        })

    def to_dict(self) -> dict:
        return {"adapter_version": self.adapter_version, "repo_id": self.repo_id,
                "revision": self.revision, "config": self.config,
                "split": self.split, "dataset_id": self.dataset_id,
                "source_id": self.source_id}


@dataclass(frozen=True)
class HuggingFaceStreamPage:
    source_id: str
    records: tuple[dict, ...]
    next_cursor: str
    exhausted: bool
    schema: dict = field(repr=False)
    provenance: dict = field(repr=False)

    def to_dict(self) -> dict:
        return {"source_id": self.source_id,
                "records": [_canonical(row, "record") for row in self.records],
                "next_cursor": self.next_cursor, "exhausted": self.exhausted,
                "schema": _canonical(self.schema, "schema"),
                "provenance": _canonical(self.provenance, "provenance")}


def _encode_checkpoint(source_id: str, state: Mapping[str, Any]) -> str:
    body = {"source_id": source_id, "state": _canonical(dict(state), "state")}
    envelope = {"body": body, "checksum": _digest(body)}
    raw = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    if len(encoded) > MAX_CURSOR_CHARS:
        raise HuggingFaceAdapterError("stream checkpoint exceeds cursor limit")
    return encoded


def _decode_checkpoint(cursor: str, source_id: str) -> dict:
    if not cursor:
        return {}
    if len(cursor) > MAX_CURSOR_CHARS:
        raise ValueError("Hugging Face stream cursor exceeds size limit")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        envelope = json.loads(raw)
        body = envelope["body"]
        if envelope["checksum"] != _digest(body):
            raise ValueError
        if body["source_id"] != source_id or not isinstance(body["state"], dict):
            raise ValueError
        return _canonical(body["state"], "state")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid or mismatched Hugging Face stream cursor") from exc


class HuggingFaceDatasetAdapter:
    """Maps one pinned Hub source through an injected ``load_dataset`` callable."""

    name = "huggingface"

    def __init__(self, loader: Callable[..., Any], *, package_version: str):
        if not callable(loader):
            raise TypeError("loader must be callable")
        package_version = str(package_version or "").strip()
        if not _VERSION.fullmatch(package_version):
            raise ValueError("package_version must be explicit")
        if package_version != SUPPORTED_DATASETS_VERSION:
            raise ValueError(
                f"datasets package must be pinned to {SUPPORTED_DATASETS_VERSION}")
        self._loader = loader
        self.package_version = package_version

    def _load(self, source: HuggingFaceSource, *, streaming: bool) -> Any:
        arguments = {
            "path": source.repo_id, "split": source.split,
            "revision": source.revision, "streaming": streaming,
            # Never discover ~/.huggingface credentials implicitly. Credentialed
            # sources require a later explicit secret-reference integration.
            "token": False,
        }
        if source.config:
            arguments["name"] = source.config
        try:
            return self._loader(**arguments)
        except Exception as exc:
            raise HuggingFaceAdapterError("Hugging Face dataset load failed") from exc

    @staticmethod
    def _schema(dataset: Any) -> dict:
        features = getattr(dataset, "features", None)
        if features is None:
            return {}
        if hasattr(features, "to_dict"):
            features = features.to_dict()
        if not isinstance(features, Mapping):
            raise HuggingFaceAdapterError("dataset features are not a JSON object")
        return _canonical(dict(features), "features")

    def _provenance(self, source: HuggingFaceSource, dataset: Any,
                    *, streaming: bool) -> dict:
        value = {
            "provider": self.name, "package": "datasets",
            "package_version": self.package_version, "source": source.to_dict(),
            "streaming": streaming,
        }
        fingerprint = getattr(dataset, "_fingerprint", None)
        if fingerprint:
            value["provider_fingerprint"] = str(fingerprint)[:256]
        return value

    def materialize(self, source: HuggingFaceSource, *, created_at: str,
                    max_records: int = 10_000,
                    cancellation: CancellationSignal | None = None
                    ) -> tuple[DatasetSnapshot, tuple[dict, ...]]:
        max_records = int(max_records)
        if max_records < 1 or max_records > MAX_IMPORT_RECORDS:
            raise ValueError(f"max_records must be between 1 and {MAX_IMPORT_RECORDS}")
        signal = cancellation or CancellationSignal()
        signal.checkpoint()
        dataset = self._load(source, streaming=False)
        try:
            count = len(dataset)
        except Exception as exc:
            raise HuggingFaceAdapterError(
                "materialization requires a sized single-split dataset") from exc
        if count > max_records:
            raise HuggingFaceAdapterError(
                f"dataset has {count} records, above materialization limit {max_records}")
        records = []
        try:
            iterator = iter(dataset)
        except Exception as exc:
            raise HuggingFaceAdapterError("dataset iteration failed") from exc
        for _ in range(count + 1):
            signal.checkpoint()
            try:
                row = next(iterator)
            except StopIteration:
                break
            except Exception as exc:
                raise HuggingFaceAdapterError("dataset iteration failed") from exc
            if not isinstance(row, Mapping):
                raise HuggingFaceAdapterError("dataset row is not a JSON object")
            records.append(_canonical(dict(row), "dataset row"))
        if len(records) != count:
            raise HuggingFaceAdapterError("dataset length changed during materialization")
        return DatasetSnapshot.create(
            dataset_id=source.dataset_id, created_at=created_at, records=records,
            schema=self._schema(dataset),
            provenance=self._provenance(source, dataset, streaming=False))

    def stream_page(self, source: HuggingFaceSource, *, limit: int = 100,
                    cursor: str = "",
                    cancellation: CancellationSignal | None = None
                    ) -> HuggingFaceStreamPage:
        limit = int(limit)
        if limit < 1 or limit > MAX_STREAM_PAGE:
            raise ValueError(f"limit must be between 1 and {MAX_STREAM_PAGE}")
        signal = cancellation or CancellationSignal()
        signal.checkpoint()
        dataset = self._load(source, streaming=True)
        checkpoint = _decode_checkpoint(cursor, source.source_id)
        if checkpoint:
            restore = getattr(dataset, "load_state_dict", None)
            if not callable(restore):
                raise HuggingFaceAdapterError("stream does not support checkpoint restore")
            try:
                restore(checkpoint)
            except Exception as exc:
                raise HuggingFaceAdapterError("stream checkpoint restore failed") from exc
        state = getattr(dataset, "state_dict", None)
        if not callable(state):
            raise HuggingFaceAdapterError("stream does not expose checkpoint state")
        records = []
        try:
            iterator = iter(dataset)
        except Exception as exc:
            raise HuggingFaceAdapterError("stream iteration failed") from exc
        exhausted = False
        for _ in range(limit):
            signal.checkpoint()
            try:
                row = next(iterator)
            except StopIteration:
                exhausted = True
                break
            except Exception as exc:
                raise HuggingFaceAdapterError("stream iteration failed") from exc
            if not isinstance(row, Mapping):
                raise HuggingFaceAdapterError("stream row is not a JSON object")
            records.append(_canonical(dict(row), "stream row"))
        signal.checkpoint()
        next_cursor = ""
        if not exhausted:
            try:
                checkpoint_state = _canonical(state(), "stream state")
            except Exception as exc:
                raise HuggingFaceAdapterError("stream checkpoint capture failed") from exc
            next_cursor = _encode_checkpoint(source.source_id, checkpoint_state)
        return HuggingFaceStreamPage(
            source_id=source.source_id, records=tuple(records),
            next_cursor=next_cursor, exhausted=exhausted,
            schema=self._schema(dataset),
            provenance=self._provenance(source, dataset, streaming=True))
