"""Opt-in operational runner for the proven LangGraph Workflow IR subset.

The runner imports LangGraph only when called. Vera retains task admission and
execution authority through an injected executor; LangGraph schedules the
already-compiled graph but never resolves or invokes a capability itself.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import re
from typing import Annotated, Any, Awaitable, Callable, Mapping, TypedDict

from .langgraph_workflow_adapter import PLAN_SCHEMA


TaskExecutor = Callable[
    [str, Mapping[str, Any]], Any | Awaitable[Any]
]
_MAX_STATE_BYTES = 1_048_576
_MAX_NODES = 1_024
_MAX_EDGES = 4_096
_TASK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _merge_records(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    overlap = set(left) & set(right)
    if overlap:
        raise ValueError("LangGraph emitted a duplicate node record")
    return {**left, **right}


class _ExecutionState(TypedDict):
    initial: dict[str, Any]
    records: Annotated[dict[str, dict[str, Any]], _merge_records]


def _plan_identity(plan: Mapping[str, Any]) -> str:
    body = {key: value for key, value in plan.items() if key != "plan_id"}
    return "lgplan_" + hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


def _bounded_state(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    try:
        encoded = _canonical(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be canonical JSON") from exc
    if len(encoded.encode("utf-8")) > _MAX_STATE_BYTES:
        raise ValueError(f"{name} exceeds the one MiB runtime boundary")
    return copy.deepcopy(dict(value))


class LangGraphOperationalRunner:
    """Execute one compiler-produced plan through a lazily loaded StateGraph.

    ``allowed_tasks`` is mandatory authority, not discovery metadata. Every
    plan task is checked before any node can execute. The injected executor is
    the only component allowed to call Vera capabilities and receives a
    detached snapshot of state.
    """

    def __init__(self, executor: TaskExecutor, *, allowed_tasks: tuple[str, ...]):
        if not callable(executor):
            raise TypeError("executor must be callable")
        tasks = tuple(sorted(set(allowed_tasks)))
        if not tasks or any(not isinstance(task, str) or not _TASK_NAME.fullmatch(task)
                            for task in tasks):
            raise ValueError("allowed tasks must be non-empty bounded names")
        self._executor = executor
        self._allowed_tasks = frozenset(tasks)

    async def __call__(self, plan: Mapping[str, Any],
                       input_state: Mapping[str, Any]) -> dict[str, Any]:
        checked, order = self._validate_plan(plan)
        initial = _bounded_state(input_state, "input_state")
        identity = {
            "runtime_id": "langgraph",
            "plan_id": checked["plan_id"],
            "workflow_hash": checked["workflow_hash"],
        }
        if not checked["nodes"]:
            return {**identity, "status": "succeeded", "result_state": initial}
        try:
            from langgraph.graph import END, START, StateGraph
        except (ImportError, ModuleNotFoundError):
            return {**identity, "status": "failed",
                    "error_code": "langgraph_runtime_unavailable"}

        try:
            graph = StateGraph(_ExecutionState)
            for node in checked["nodes"]:
                graph.add_node(node["id"], self._node(node, order))
            for edge in checked["edges"]:
                source = START if edge["from"] == "__start__" else edge["from"]
                graph.add_edge(source, edge["to"])
            for terminal in checked["terminal_nodes"]:
                graph.add_edge(terminal, END)
            runnable = graph.compile()
            observed = await runnable.ainvoke({"initial": initial, "records": {}})
            result = self._snapshot(initial, observed.get("records", {}), order)
            result = _bounded_state(result, "result_state")
            return {**identity, "status": "succeeded", "result_state": result}
        except asyncio.CancelledError:
            return {**identity, "status": "cancelled"}
        except Exception:
            return {**identity, "status": "failed",
                    "error_code": "langgraph_execution_failed"}

    def _node(self, node: Mapping[str, Any], order: tuple[str, ...]):
        node_id, task, output = node["id"], node["task"], node.get("output", "")
        guard = node.get("guard")

        async def execute(state: Mapping[str, Any]) -> dict[str, Any]:
            snapshot = self._snapshot(state.get("initial", {}),
                                      state.get("records", {}), order)
            if guard and not bool(snapshot.get(guard["key"])):
                record = {"executed": False, "output": "", "value": None}
            else:
                value = self._executor(task, copy.deepcopy(snapshot))
                if inspect.isawaitable(value):
                    value = await value
                # Validate every result before it can enter shared graph state.
                _bounded_state({"value": value}, "task result")
                record = {"executed": True, "output": output, "value": value}
            return {"records": {node_id: record}}

        return execute

    @staticmethod
    def _snapshot(initial: Mapping[str, Any], records: Mapping[str, Any],
                  order: tuple[str, ...]) -> dict[str, Any]:
        state = copy.deepcopy(dict(initial))
        for node_id in order:
            record = records.get(node_id)
            if not isinstance(record, Mapping) or not record.get("executed"):
                continue
            output = record.get("output", "")
            if output:
                state[output] = copy.deepcopy(record.get("value"))
        return state

    def _validate_plan(self, plan: Mapping[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
        if not isinstance(plan, Mapping) or plan.get("schema") != PLAN_SCHEMA:
            raise ValueError("runner requires a LangGraph workflow plan")
        checked = _bounded_state(plan, "workflow plan")
        if checked.get("adapter") != "langgraph":
            raise ValueError("runner plan adapter must be langgraph")
        if checked.get("execution_authority") != "injected_langgraph_runner":
            raise ValueError("runner plan execution authority drifted")
        if checked.get("plan_id") != _plan_identity(checked):
            raise ValueError("runner plan identity does not match its content")
        nodes = checked.get("nodes")
        edges = checked.get("edges")
        terminals = checked.get("terminal_nodes")
        entries = checked.get("entrypoints")
        if not isinstance(nodes, list) or not isinstance(edges, list) \
                or not isinstance(terminals, list) or not isinstance(entries, list):
            raise ValueError("runner plan graph fields are malformed")
        if len(nodes) > _MAX_NODES or len(edges) > _MAX_EDGES:
            raise ValueError("runner plan graph exceeds operational bounds")
        node_ids: list[str] = []
        outputs: list[str] = []
        for node in nodes:
            if not isinstance(node, Mapping) or node.get("kind") != "task":
                raise ValueError("runner supports task nodes only")
            node_id, task = node.get("id"), node.get("task")
            if (not isinstance(node_id, str) or not node_id or len(node_id) > 256
                    or not isinstance(task, str) or task not in self._allowed_tasks):
                raise ValueError("runner plan contains an unauthorized task")
            guard = node.get("guard")
            if guard is not None and (not isinstance(guard, Mapping)
                                      or guard.get("kind") != "state_truthy"
                                      or not isinstance(guard.get("key"), str)
                                      or not guard["key"]):
                raise ValueError("runner plan contains an invalid guard")
            output = node.get("output", "")
            if not isinstance(output, str) or len(output) > 256:
                raise ValueError("runner plan contains an invalid output key")
            if output:
                outputs.append(output)
            node_ids.append(node_id)
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("runner plan node IDs must be unique")
        if len(set(outputs)) != len(outputs):
            raise ValueError("runner plan output keys must be unique")
        known = set(node_ids)
        pairs: list[tuple[str, str]] = []
        for edge in edges:
            if (not isinstance(edge, Mapping)
                    or edge.get("from") not in known | {"__start__"}
                    or edge.get("to") not in known):
                raise ValueError("runner plan contains an invalid edge")
            pairs.append((edge["from"], edge["to"]))
        if len(set(pairs)) != len(pairs):
            raise ValueError("runner plan edges must be unique")
        if any(value not in known for value in terminals):
            raise ValueError("runner plan contains an invalid terminal node")
        if bool(nodes) != bool(terminals):
            raise ValueError("runner plan terminal nodes are incomplete")
        if any(value not in known for value in entries) or len(set(entries)) != len(entries):
            raise ValueError("runner plan contains invalid entrypoints")
        declared_entries = {target for source, target in pairs if source == "__start__"}
        if declared_entries != set(entries):
            raise ValueError("runner plan entrypoints do not match start edges")
        graph_edges = [(source, target) for source, target in pairs
                       if source != "__start__"]
        declared_terminals = known - {source for source, _target in graph_edges}
        if declared_terminals != set(terminals):
            raise ValueError("runner plan terminal nodes do not match graph sinks")
        reached = set(entries)
        while True:
            expanded = reached | {target for source, target in graph_edges
                                  if source in reached}
            if expanded == reached:
                break
            reached = expanded
        if reached != known:
            raise ValueError("runner plan contains unreachable nodes")
        indegree = {node_id: 0 for node_id in node_ids}
        followers = {node_id: [] for node_id in node_ids}
        for source, target in graph_edges:
            indegree[target] += 1
            followers[source].append(target)
        ready = [node_id for node_id in node_ids if indegree[node_id] == 0]
        visited = 0
        while ready:
            current = ready.pop()
            visited += 1
            for target in followers[current]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    ready.append(target)
        if visited != len(node_ids):
            raise ValueError("runner plan graph must be acyclic")
        return checked, tuple(node_ids)
