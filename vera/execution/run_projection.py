"""Ephemeral shadow projections and DAG child-run observation."""

from __future__ import annotations

import hashlib
import json
import os
from collections import OrderedDict
from typing import Any
from uuid import uuid4

from .run_journal import MemoryRunJournal, SqliteRunJournal
from .run_protocol import ArtifactRef, PROTOCOL_VERSION, Run, RunError, RunEvent, RunStatus


def _leaf_count(graph: list) -> int:
    count = 0
    for node in graph:
        count += len(node) if isinstance(node, list) and node and isinstance(node[0], list) else 1
    return count


def _result_artifact(run_id: str, result: Any, *, partial: bool = False) -> ArtifactRef:
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, default=str).encode("utf-8")
    return ArtifactRef(id=str(uuid4()), kind=("capability.partial_output" if partial
                                               else "capability.output"),
                       uri=f"run://{run_id}/result",
                       checksum=f"sha256:{hashlib.sha256(encoded).hexdigest()}",
                       media_type="application/json", size_bytes=len(encoded))


class ShadowRunRegistry:
    """Bounded process-local projection registry; explicitly non-authoritative."""

    def __init__(self, max_runs: int = 200, journal=None) -> None:
        self.max_runs = max(1, int(max_runs))
        self.runs: OrderedDict[str, Run] = OrderedDict()
        self.journal = journal or MemoryRunJournal()

    @property
    def storage(self) -> str:
        return ("sqlite_journal_process_local_projection"
                if isinstance(self.journal, SqliteRunJournal)
                else "process_local_memory")

    def record(self, run: Run, event: RunEvent) -> None:
        self.runs[run.id] = run
        self.runs.move_to_end(run.id)
        self.journal.append(event)
        while len(self.runs) > self.max_runs:
            old_id, _ = self.runs.popitem(last=False)
            # Memory rows follow their bounded projection. A durable journal has
            # its own retention policy and must never be erased by cache eviction.
            if isinstance(self.journal, MemoryRunJournal):
                exported = self.journal.export(old_id)
                if exported["last_checksum"]:
                    self.journal.delete(old_id, expected_checksum=exported["last_checksum"])

    def get(self, run_id: str) -> dict[str, Any] | None:
        run = self.runs.get(run_id)
        if not run:
            return None
        verification = self.journal.verify(run_id)
        children = [child.to_dict() for child in self.runs.values()
                    if child.parent_run_id == run_id]
        return {"authoritative": False, "storage": self.storage,
                "run": run.to_dict(), "children": children,
                "journal": verification}

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        values = list(self.runs.values())[-max(1, min(int(limit), 200)):]
        return [{"id": run.id, "kind": run.kind, "status": run.status.value,
                 "parent_run_id": run.parent_run_id, "trace_id": run.trace_id}
                for run in reversed(values)]


_journal_path = os.getenv("VERA_RUN_JOURNAL_PATH", "").strip()
SHADOW_RUNS = ShadowRunRegistry(
    journal=SqliteRunJournal(_journal_path) if _journal_path else MemoryRunJournal())


class DagRunObserver:
    def __init__(self, *, parent: Run, graph: list, emit, registry=None) -> None:
        self.parent = parent
        self.emit = emit
        self.registry = registry if registry is not None else SHADOW_RUNS
        self.total = _leaf_count(graph)
        self.finished = 0
        self.children: dict[tuple[int, ...], Run] = {}

    async def _publish(self, run: Run, event: RunEvent) -> None:
        try:
            self.registry.record(run, event)
            await self.emit({"type": "run.event", "protocol": PROTOCOL_VERSION,
                             "run": run.to_dict(include_events=False),
                             "event": event.to_dict()})
        except Exception:
            return

    def _child(self, path: tuple[int, ...], cap_name: str) -> Run:
        child = Run(id=str(uuid4()), kind="vera.dag.node",
                    parent_run_id=self.parent.id, workflow_id=self.parent.workflow_id,
                    task_id=".".join(str(part) for part in path),
                    trace_id=self.parent.trace_id)
        self.children[path] = child
        return child

    async def node_started(self, path: tuple[int, ...], cap_name: str) -> None:
        child = self._child(path, cap_name)
        event = child.transition(RunStatus.RUNNING, event_type="run.started",
                                 payload={"capability": cap_name, "path": list(path)},
                                 causation_id=self.parent.events[0].id if self.parent.events else "")
        await self._publish(child, event)

    async def node_skipped(self, path: tuple[int, ...], cap_name: str,
                           reason: str) -> None:
        child = self._child(path, cap_name)
        event = child.transition(RunStatus.SKIPPED, event_type="run.skipped",
                                 payload={"capability": cap_name, "path": list(path),
                                          "reason": reason},
                                 causation_id=self.parent.events[0].id if self.parent.events else "")
        await self._publish(child, event)
        await self._progress(event.id)

    async def node_finished(self, path: tuple[int, ...], cap_name: str, result: Any,
                            error: str = "") -> None:
        child = self.children[path]
        if error:
            child.error = RunError(code="dag_node_error", message=error)
            if result is not None:
                child.artifacts.append(_result_artifact(child.id, result, partial=True))
            event = child.transition(RunStatus.FAILED, event_type="run.failed",
                                     payload={"capability": cap_name, "path": list(path)},
                                     causation_id=child.events[-1].id)
        else:
            child.error = None
            child.artifacts.append(_result_artifact(child.id, result))
            event = child.transition(RunStatus.COMPLETED, event_type="run.completed",
                                     payload={"capability": cap_name, "path": list(path),
                                              "artifact_id": child.artifacts[0].id},
                                     causation_id=child.events[-1].id)
        await self._publish(child, event)
        await self._progress(event.id)

    async def node_retry(self, path: tuple[int, ...], cap_name: str,
                         attempt: int, error: str) -> None:
        child = self.children[path]
        child.error = RunError(code="dag_node_retry", message=error, retryable=True)
        retry = child.transition(
            RunStatus.RETRYING, event_type="run.retrying",
            payload={"capability": cap_name, "path": list(path),
                     "next_attempt": attempt, "error": error},
            causation_id=child.events[-1].id)
        await self._publish(child, retry)
        child.attempt = attempt
        resumed = child.transition(
            RunStatus.RUNNING, event_type="run.retry.started",
            payload={"capability": cap_name, "path": list(path),
                     "attempt": attempt}, causation_id=retry.id)
        await self._publish(child, resumed)

    async def _progress(self, causation_id: str) -> None:
        self.finished += 1
        self.parent.progress = self.finished / self.total if self.total else 1.0
        event = self.parent.record_event(
            "run.progress", payload={"completed_nodes": self.finished,
                                     "total_nodes": self.total,
                                     "progress": self.parent.progress},
            causation_id=causation_id)
        await self._publish(self.parent, event)
