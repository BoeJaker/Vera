"""Ephemeral shadow projections and DAG child-run observation."""

from __future__ import annotations

import hashlib
import json
import os
from collections import OrderedDict
from typing import Any
from uuid import uuid4

from .run_journal import JournalCorruption, MemoryRunJournal, SqliteRunJournal
from .run_protocol import ArtifactRef, PROTOCOL_VERSION, Run, RunError, RunEvent, RunStatus


_AUTO_TELEMETRY_ENABLED = str(os.getenv("VERA_OTLP_AUTO_EXPORT") or "").strip().lower() in {
    "1", "true", "yes", "on",
}


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
        self.recovery = {"attempted": False, "recovered": 0, "failed": 0,
                         "failures": []}
        if isinstance(self.journal, SqliteRunJournal):
            self.recover()

    @property
    def storage(self) -> str:
        return ("sqlite_journal_process_local_projection"
                if isinstance(self.journal, SqliteRunJournal)
                else "process_local_memory")

    def recovery_status(self) -> dict[str, Any]:
        """Public, content-free catalog recovery outcome with opaque failure refs."""
        failures = self.recovery.get("failures") or []
        return {
            "attempted": bool(self.recovery.get("attempted")),
            "recovered": int(self.recovery.get("recovered") or 0),
            "quarantined": int(self.recovery.get("failed") or 0),
            "quarantined_refs": [{
                "run_ref": hashlib.sha256(str(item.get("run_id") or "").encode("utf-8"))
                .hexdigest()[:12],
                "error_type": str(item.get("error_type") or "recovery_error"),
            } for item in failures[:20]],
            "bounded_limit": self.max_runs,
            "read_only": True,
        }

    def record(self, run: Run, event: RunEvent) -> None:
        self.journal.register(run)
        self.runs[run.id] = run
        self.runs.move_to_end(run.id)
        self.journal.append(event)
        self.journal.checkpoint(run)
        if _AUTO_TELEMETRY_ENABLED and not run.parent_run_id and run.status in {
                RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED,
                RunStatus.TIMED_OUT, RunStatus.SKIPPED}:
            try:
                from .portable_telemetry import TELEMETRY_QUEUE
                children = [child for child in self.runs.values()
                            if child.parent_run_id == run.id]
                TELEMETRY_QUEUE.offer_run(run, children)
            except Exception:
                # Observability must never alter Run recording or execution.
                pass
        while len(self.runs) > self.max_runs:
            old_id, _ = self.runs.popitem(last=False)
            # Memory rows follow their bounded projection. A durable journal has
            # its own retention policy and must never be erased by cache eviction.
            if isinstance(self.journal, MemoryRunJournal):
                exported = self.journal.export(old_id)
                if exported["last_checksum"]:
                    self.journal.delete(old_id, expected_checksum=exported["last_checksum"])

    def recover(self) -> dict[str, Any]:
        """Rebuild a bounded read-only catalog; isolate corrupt Runs individually."""
        recovered: OrderedDict[str, Run] = OrderedDict()
        failures = []
        selected = list(self.journal.run_ids())[:self.max_runs]
        for run_id in reversed(selected):
            try:
                recovered[run_id] = self.journal.rebuild(run_id=run_id)
            except (JournalCorruption, ValueError, TypeError, KeyError) as exc:
                failures.append({"run_id": run_id, "error_type": type(exc).__name__})
        self.runs = recovered
        self.recovery = {"attempted": True, "recovered": len(recovered),
                         "failed": len(failures), "failures": failures}
        return dict(self.recovery)

    def get(self, run_id: str) -> dict[str, Any] | None:
        run = self.runs.get(run_id)
        if not run:
            return None
        verification = self.journal.verify(run_id)
        children = [child.to_dict() for child in self.runs.values()
                    if child.parent_run_id == run_id]
        return {"authoritative": False, "storage": self.storage,
                "run": run.to_dict(), "children": children,
                "journal": verification, "recovery": self.recovery_status()}

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        values = list(self.runs.values())[-max(1, min(int(limit), 200)):]
        return [{"id": run.id, "kind": run.kind, "status": run.status.value,
                 "parent_run_id": run.parent_run_id, "trace_id": run.trace_id}
                for run in reversed(values)]

    def graph(self, *, run_id: str = "", trace_id: str = "",
              session_id: str = "", limit: int = 100) -> dict[str, Any]:
        """Return a content-free graph projection suitable for read-only UIs."""
        cap = max(1, min(int(limit), 200))
        values = list(self.runs.values())
        if run_id:
            values = [run for run in values
                      if run.id == run_id or run.parent_run_id == run_id]
        if trace_id:
            values = [run for run in values if run.trace_id == trace_id]
        if session_id:
            values = [run for run in values if run.session_id == session_id]
        values = values[-cap:]
        ids = {run.id for run in values}
        nodes = []
        edges = []
        for run in values:
            first_payload = run.events[0].payload if run.events else {}
            capability = str(first_payload.get("capability", ""))
            node_type = "run_node" if run.parent_run_id else "run"
            label = capability or run.kind
            nodes.append({
                "id": f"run:{run.id}", "run_id": run.id,
                "parent_run_id": run.parent_run_id,
                "record_type": node_type, "type": node_type, "source": "run",
                "label": label, "capability": capability,
                "summary": f"{label} · {run.status.value}",
                "text": "", "status": run.status.value,
                "session_id": run.session_id, "trace_id": run.trace_id,
                "task_id": run.task_id, "attempt": run.attempt,
                "progress": run.progress, "created_at": run.created_at,
                "importance": 0.65 if run.parent_run_id else 0.8,
                "tags": ["run_protocol", run.status.value],
                "non_authoritative": True,
            })
            if run.parent_run_id and run.parent_run_id in ids:
                edges.append({
                    "from": f"run:{run.parent_run_id}", "to": f"run:{run.id}",
                    "from_id": f"run:{run.parent_run_id}",
                    "to_id": f"run:{run.id}", "type": "RUN_CHILD",
                    "relation": "RUN_CHILD", "source": "run",
                })
        return {"authoritative": False, "storage": self.storage,
                "nodes": nodes, "edges": edges, "count": len(nodes),
                "recovery": self.recovery_status()}


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
                    session_id=self.parent.session_id, trace_id=self.parent.trace_id)
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


class StreamDagRunProjection:
    """Failure-isolated Run projection of the native DAG SSE lifecycle.

    The native generator remains the execution and event authority.  This
    observer records only content-free lifecycle metadata, and intentionally
    never emits into or modifies the caller's SSE stream.
    """

    def __init__(self, *, mode: str, trace_id: str, session_id: str = "",
                 graph: list | None = None, registry=None) -> None:
        self.mode = "stepwise" if mode == "stepwise" else "oneshot"
        self.registry = registry if registry is not None else SHADOW_RUNS
        self.parent = Run(
            id=str(uuid4()), kind="vera.dag.stream", trace_id=trace_id,
            workflow_id=trace_id, session_id=session_id,
        )
        self.children: dict[int, Run] = {}
        self.finished = 0
        self.total: int | None = None
        self._started = False
        self._terminal = False
        if graph is not None:
            self.bind_graph(graph)
        elif self.mode == "stepwise":
            self._safe(self._start)

    @property
    def run_id(self) -> str:
        return self.parent.id

    def _record(self, run: Run, event: RunEvent) -> None:
        self.registry.record(run, event)

    def _safe(self, operation, *args, **kwargs) -> None:
        try:
            operation(*args, **kwargs)
        except Exception:
            # Projection failures must never alter native streamed execution.
            return

    def _start(self) -> None:
        if self._started:
            return
        event = self.parent.transition(
            RunStatus.RUNNING, event_type="run.started",
            payload={"execution_mode": self.mode},
        )
        self._record(self.parent, event)
        self._started = True

    def bind_graph(self, graph: list) -> None:
        """Attach exact one-shot definition identity without executing it."""
        self._safe(self._bind_graph, graph)

    def _bind_graph(self, graph: list) -> None:
        from .dag_workflow_execution import prepare_dag_execution

        prepared = prepare_dag_execution(graph)
        workflow_hash = str(prepared.get("workflow_hash") or "")
        if workflow_hash:
            self.parent.workflow_id = workflow_hash
        self.total = _leaf_count(prepared.get("graph") or graph)
        self._start()
        event = self.parent.record_event(
            "run.workflow.bound",
            payload={
                "definition_authority": (
                    "workflow_ir" if prepared.get("authoritative") else "native"
                ),
                "node_count": self.total,
            },
            causation_id=self.parent.events[-1].id,
        )
        self._record(self.parent, event)

    def observe(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        """Observe one native event; invalid or duplicate input is harmless."""
        self._safe(self._observe, str(event_type or ""), dict(payload or {}))

    def _observe(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._terminal:
            return
        self._start()
        if event_type == "dag.step_start":
            self._step_started(payload)
        elif event_type == "dag.hitl_request":
            self._approval_pending(payload)
        elif event_type == "dag.hitl_rejected":
            self._step_cancelled(payload, "approval_rejected")
        elif event_type == "dag.step_done":
            self._step_finished(payload, failed=False)
        elif event_type == "dag.step_error":
            self._step_finished(payload, failed=True)
        elif event_type == "dag.error":
            self.fail("native_stream_error")
        elif event_type == "dag.complete":
            if "aborted_at" in payload:
                self.cancel("approval_rejected")
            else:
                self.complete()

    @staticmethod
    def _step(payload: dict[str, Any]) -> int:
        return int(payload.get("step", 0))

    def _workflow_id(self, payload: dict[str, Any]) -> str:
        metadata = payload.get("workflow_ir")
        if isinstance(metadata, dict) and metadata.get("workflow_hash"):
            return str(metadata["workflow_hash"])
        if self.mode != "stepwise":
            return self.parent.workflow_id
        cap_name = str(payload.get("cap") or "")
        out_key = str(payload.get("out_key") or "")
        if not cap_name or cap_name == "[parallel]" or not out_key:
            return self.parent.workflow_id
        from .dag_workflow_execution import prepare_stepwise_dag_action

        prepared = prepare_stepwise_dag_action(
            cap_name, out_key, include_workflow_ir=True,
        )
        return str((prepared.get("workflow_ir") or {}).get("workflow_hash")
                   or self.parent.workflow_id)

    def _step_started(self, payload: dict[str, Any]) -> None:
        step = self._step(payload)
        if step in self.children:
            return
        if "total" in payload:
            self.total = max(0, int(payload["total"]))
        cap_name = str(payload.get("cap") or "[unknown]")
        child = Run(
            id=str(uuid4()), kind="vera.dag.stream.step",
            parent_run_id=self.parent.id, workflow_id=self._workflow_id(payload),
            task_id=str(step), session_id=self.parent.session_id,
            trace_id=self.parent.trace_id,
        )
        self.children[step] = child
        event = child.transition(
            RunStatus.RUNNING, event_type="run.started",
            payload={"capability": cap_name, "step": step},
            causation_id=self.parent.events[-1].id,
        )
        self._record(child, event)

    def _approval_pending(self, payload: dict[str, Any]) -> None:
        step = self._step(payload)
        child = self.children.get(step)
        if not child or child.status != RunStatus.RUNNING:
            return
        event = child.transition(
            RunStatus.APPROVAL_PENDING, event_type="run.approval.pending",
            payload={"capability": str(payload.get("cap") or "[unknown]"),
                     "step": step},
            causation_id=child.events[-1].id,
        )
        self._record(child, event)

    def _resume_if_pending(self, child: Run, step: int) -> None:
        if child.status != RunStatus.APPROVAL_PENDING:
            return
        event = child.transition(
            RunStatus.RUNNING, event_type="run.approval.resumed",
            payload={"step": step}, causation_id=child.events[-1].id,
        )
        self._record(child, event)

    def _step_cancelled(self, payload: dict[str, Any], reason: str) -> None:
        step = self._step(payload)
        child = self.children.get(step)
        if not child or child.status not in {
                RunStatus.RUNNING, RunStatus.APPROVAL_PENDING}:
            return
        event = child.transition(
            RunStatus.CANCELLED, event_type="run.cancelled",
            payload={"step": step, "reason_code": reason},
            causation_id=child.events[-1].id,
        )
        self._record(child, event)
        self._progress(event.id)

    def _step_finished(self, payload: dict[str, Any], *, failed: bool) -> None:
        step = self._step(payload)
        child = self.children.get(step)
        if not child:
            return
        self._resume_if_pending(child, step)
        if child.status != RunStatus.RUNNING:
            return
        cap_name = str(payload.get("cap") or "[parallel]")
        if failed:
            child.error = RunError(
                code="dag_stream_step_error", message="native stream step failed",
            )
            status, kind = RunStatus.FAILED, "run.failed"
        else:
            status, kind = RunStatus.COMPLETED, "run.completed"
        event = child.transition(
            status, event_type=kind,
            payload={"capability": cap_name, "step": step},
            causation_id=child.events[-1].id,
        )
        self._record(child, event)
        self._progress(event.id)

    def _progress(self, causation_id: str) -> None:
        self.finished += 1
        progress = None
        if self.total:
            progress = min(1.0, self.finished / self.total)
            self.parent.progress = progress
        payload: dict[str, Any] = {"completed_steps": self.finished}
        if self.total is not None:
            payload["total_steps"] = self.total
        if progress is not None:
            payload["progress"] = progress
        event = self.parent.record_event(
            "run.progress", payload=payload, causation_id=causation_id,
        )
        self._record(self.parent, event)

    def complete(self) -> None:
        self._safe(self._terminate, RunStatus.COMPLETED, "run.completed", "")

    def fail(self, reason_code: str = "native_stream_error") -> None:
        self._safe(self._terminate, RunStatus.FAILED, "run.failed", reason_code)

    def cancel(self, reason_code: str = "stream_cancelled") -> None:
        self._safe(self._terminate, RunStatus.CANCELLED, "run.cancelled", reason_code)

    def _terminate(self, status: RunStatus, event_type: str, reason_code: str) -> None:
        self._start()
        if self._terminal or self.parent.status != RunStatus.RUNNING:
            return
        child_status = (
            RunStatus.FAILED if status == RunStatus.FAILED else RunStatus.CANCELLED
        )
        child_event_type = (
            "run.failed" if child_status == RunStatus.FAILED else "run.cancelled"
        )
        if status != RunStatus.COMPLETED:
            for step, child in self.children.items():
                if child.status not in {
                        RunStatus.RUNNING, RunStatus.APPROVAL_PENDING}:
                    continue
                if child_status == RunStatus.FAILED:
                    self._resume_if_pending(child, step)
                if child_status == RunStatus.FAILED:
                    child.error = RunError(
                        code=reason_code or "native_stream_error",
                        message="native DAG stream failed",
                    )
                child_event = child.transition(
                    child_status, event_type=child_event_type,
                    payload={"step": step, "reason_code": reason_code},
                    causation_id=child.events[-1].id,
                )
                self._record(child, child_event)
        if status == RunStatus.FAILED:
            self.parent.error = RunError(
                code=reason_code or "native_stream_error",
                message="native DAG stream failed",
            )
        if status == RunStatus.COMPLETED:
            self.parent.progress = 1.0
        payload: dict[str, Any] = {"completed_steps": self.finished}
        if reason_code:
            payload["reason_code"] = reason_code
        if status == RunStatus.COMPLETED:
            payload["progress"] = 1.0
        event = self.parent.transition(
            status, event_type=event_type, payload=payload,
            causation_id=self.parent.events[-1].id,
        )
        self._record(self.parent, event)
        self._terminal = True
