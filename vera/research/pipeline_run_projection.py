"""Content-free Run protocol observer for native research pipelines."""
from __future__ import annotations

import re
from typing import Any

from Vera.vera.execution.run_protocol import Run, RunError, RunStatus
from Vera.vera.execution.run_projection import SHADOW_RUNS, ShadowRunRegistry


SCHEMA = "vera.research-pipeline-run-projection/v1"
_ACTIVE: dict[str, "ResearchPipelineRunProjection"] = {}
_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class ResearchPipelineRunProjection:
    def __init__(self, run_id: str, workflow_id: str, stage_ids: list[str],
                 *, registry: ShadowRunRegistry | None = None) -> None:
        if not run_id or not workflow_id:
            raise ValueError("run_id and workflow_id are required")
        if (not isinstance(stage_ids, list) or not 1 <= len(stage_ids) <= 100
                or any(not isinstance(item, str) or not _OPAQUE_ID.fullmatch(item)
                       for item in stage_ids) or len(set(stage_ids)) != len(stage_ids)):
            raise ValueError("stage_ids must contain 1..100 unique opaque identifiers")
        self.stage_ids = list(stage_ids)
        self.stage_count = len(stage_ids)
        self.registry = registry if registry is not None else SHADOW_RUNS
        self.parent = Run(id=run_id, kind="research.pipeline", workflow_id=workflow_id)
        self._transition(self.parent, RunStatus.QUEUED,
                         event_type="research.pipeline.queued",
                         payload={"stage_count": self.stage_count})
        self.children: dict[int, Run] = {}

    def _transition(self, run: Run, status: RunStatus, *, event_type: str,
                    payload: dict[str, Any] | None = None) -> None:
        event = run.transition(status, event_type=event_type, payload=payload)
        self.registry.record(run, event)

    def start(self) -> None:
        if self.parent.status == RunStatus.QUEUED:
            self._transition(self.parent, RunStatus.RUNNING,
                             event_type="research.pipeline.started")

    def stage_start(self, index: int, kind: str) -> None:
        if not 0 <= int(index) < self.stage_count:
            raise ValueError("stage index is outside the pipeline")
        self.start()
        if index in self.children:
            raise ValueError("stage already started")
        child = Run(id=f"{self.parent.id}:stage:{index + 1}", kind="research.pipeline.stage",
                    parent_run_id=self.parent.id, workflow_id=self.parent.workflow_id,
                    task_id=self.stage_ids[index])
        self.children[index] = child
        self._transition(child, RunStatus.QUEUED, event_type="research.stage.queued",
                         payload={"stage_index": index, "stage_kind": _safe_kind(kind)})
        self._transition(child, RunStatus.RUNNING, event_type="research.stage.started")

    def stage_done(self, index: int, *, ok: bool, citation_count: int = 0,
                   native_job_id: str = "") -> None:
        child = self.children.get(index)
        if not child or child.status != RunStatus.RUNNING:
            raise ValueError("stage is not running")
        payload = {"stage_index": index,
                   "citation_count": min(1_000_000_000, max(0, int(citation_count or 0)))}
        if isinstance(native_job_id, str) and _OPAQUE_ID.fullmatch(native_job_id):
            payload["native_job_id"] = native_job_id
        if ok:
            self._transition(child, RunStatus.COMPLETED,
                             event_type="research.stage.completed", payload=payload)
        else:
            child.error = RunError("native_stage_failed", "Native research stage failed")
            self._transition(child, RunStatus.FAILED,
                             event_type="research.stage.failed", payload=payload)

    def finish(self, status: str) -> None:
        target = {"done": RunStatus.COMPLETED, "cancelled": RunStatus.CANCELLED,
                  "error": RunStatus.FAILED}.get(str(status), RunStatus.FAILED)
        for child in self.children.values():
            if child.status == RunStatus.RUNNING:
                self._transition(child, RunStatus.CANCELLED if target == RunStatus.CANCELLED
                                 else RunStatus.FAILED,
                                 event_type="research.stage.interrupted")
        if self.parent.status == RunStatus.QUEUED:
            self._transition(self.parent, RunStatus.RUNNING,
                             event_type="research.pipeline.started")
        if self.parent.status == RunStatus.RUNNING:
            if target == RunStatus.FAILED:
                self.parent.error = RunError("native_pipeline_failed",
                                             "Native research pipeline failed")
            self._transition(self.parent, target,
                             event_type=f"research.pipeline.{target.value}")

    def to_dict(self) -> dict[str, Any]:
        value = self.registry.get(self.parent.id)
        if value is None:
            raise ValueError("projected run is missing from the shared registry")
        return {"schema": SCHEMA, "execution_authority": "native_research_pipeline",
                "content_policy": "metadata_only", **value}


def _safe_kind(value: Any) -> str:
    value = str(value or "research").strip().lower()
    return value if value in {"research", "transform", "synthesis"} else "unknown"


def create(run_id: str, workflow_id: str,
           stage_ids: list[str], *,
           registry: ShadowRunRegistry | None = None) -> ResearchPipelineRunProjection:
    observer = ResearchPipelineRunProjection(
        run_id, workflow_id, stage_ids, registry=registry)
    _ACTIVE[run_id] = observer
    return observer


def get(run_id: str) -> dict[str, Any] | None:
    observer = _ACTIVE.get(run_id)
    if observer:
        return observer.to_dict()
    value = SHADOW_RUNS.get(run_id)
    return ({"schema": SCHEMA, "execution_authority": "native_research_pipeline",
             "content_policy": "metadata_only", **value} if value else None)


def start(run_id: str) -> None:
    _ACTIVE[run_id].start()


def stage_start(run_id: str, index: int, kind: str) -> None:
    _ACTIVE[run_id].stage_start(index, kind)


def stage_done(run_id: str, index: int, *, ok: bool, citation_count: int = 0,
               native_job_id: str = "") -> None:
    _ACTIVE[run_id].stage_done(index, ok=ok, citation_count=citation_count,
                               native_job_id=native_job_id)


def finish(run_id: str, status: str) -> None:
    _ACTIVE[run_id].finish(status)


def drop(run_id: str, *, delete_recorded: bool = False) -> None:
    observer = _ACTIVE.pop(run_id, None)
    if not delete_recorded:
        return
    registry = observer.registry if observer else SHADOW_RUNS
    ids = [item.id for item in registry.runs.values()
           if item.id == run_id or item.parent_run_id == run_id]
    for recorded_id in ids:
        verification = registry.journal.verify(recorded_id)
        checksum = verification.get("last_checksum", "")
        if checksum:
            registry.journal.delete(recorded_id, expected_checksum=checksum)
        registry.runs.pop(recorded_id, None)
