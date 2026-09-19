"""Opt-in, payload-free live validation for the container RuntimeAdapter.

This module is deliberately not a registered capability and is never run by
the normal test suite.  An operator invokes it explicitly on a Docker-owning
host.  It uses short-lived, network-isolated containers and reports only
bounded lifecycle facts; child output and stderr are not returned.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from typing import Any

from . import agentbridge_runtime as runtime


SCHEMA = "vera.runtime-adapter-live-validation/v1"


def _argv(image: str, name: str, code: str, *, entrypoint: str = "python3") -> list[str]:
    return [
        "docker", "run", "--rm", "--name", name,
        "--network", "none", "--memory", "128m", "--cpus", "0.5",
        "--pids-limit", "64", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--entrypoint", entrypoint,
        image, "-u", "-c", code,
    ]


async def _container_present(name: str) -> bool:
    result = await runtime.sh(["docker", "container", "inspect", name], timeout=5)
    return bool(result.get("ok"))


async def _wait_container_absent(name: str, timeout_s: float = 5.0) -> bool:
    """Allow Docker's asynchronous ``--rm`` cleanup a small bounded grace."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while await _container_present(name):
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.05)
    return True


async def _case(image: str, label: str, code: str, expected: str, *,
                timeout_s: int = 5, stall_s: int = 2,
                entrypoint: str = "python3", cancel: bool = False) -> dict[str, Any]:
    suffix = uuid.uuid4().hex[:10]
    run_id = f"runtime-live-{label}-{suffix}"
    container = f"vera-runtime-live-{label}-{suffix}"
    events: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    task = asyncio.create_task(runtime.stream_bridge_container(
        run_id=run_id, session_id="runtime-live-validation",
        argv=_argv(image, container, code, entrypoint=entrypoint),
        event_type_prefix="runtime.live", emit=emit,
        timeout_s=timeout_s, stall_s=stall_s,
    ))
    cancellation_accepted = False
    if cancel:
        for _ in range(100):
            if await _container_present(container):
                break
            if task.done():
                break
            await asyncio.sleep(0.05)
        result = await runtime.cancel_bridge_run(run_id)
        cancellation_accepted = bool(result.get("accepted"))
    await task

    terminal = [event for event in events
                if event.get("type") in {"runtime.live.done", "runtime.live.error"}]
    observed = "done" if terminal and terminal[0].get("type") == "runtime.live.done" \
        else str(terminal[0].get("reason_code") if terminal else "missing_terminal")
    cleaned = await _wait_container_absent(container)
    passed = len(terminal) == 1 and observed == expected and cleaned
    if cancel:
        passed = passed and cancellation_accepted
    return {
        "case": label,
        "passed": passed,
        "expected_terminal": expected,
        "observed_terminal": observed,
        "terminal_count": len(terminal),
        "container_cleaned": cleaned,
        "cancellation_accepted": cancellation_accepted if cancel else None,
    }


async def validate(image: str = "python:3.11-slim") -> dict[str, Any]:
    docker = await runtime.sh(
        ["docker", "version", "--format", "{{.Server.Version}}"], timeout=10)
    image_present = await runtime.image_present(image) if docker.get("ok") else False
    if not docker.get("ok") or not image_present:
        return {
            "schema": SCHEMA, "passed": False,
            "docker_available": bool(docker.get("ok")),
            "image_present": image_present, "cases": [],
            "reason_code": "docker_or_image_unavailable",
        }

    cases = []
    cases.append(await _case(
        image, "success",
        "import json; print('BRIDGE_RESULT:' + json.dumps({'ok': True}))",
        "done"))
    cases.append(await _case(
        image, "malformed", "print('BRIDGE_RESULT:{broken')",
        "invalid_output"))
    cases.append(await _case(
        image, "crash", "import sys; sys.exit(7)", "process_exit"))
    cases.append(await _case(
        image, "missing-dependency",
        "import json\ntry:\n import vera_dependency_that_does_not_exist\n"
        "except ImportError:\n print('BRIDGE_RESULT:' + json.dumps({'ok': False, 'error': 'ImportError'}))",
        "done"))
    cases.append(await _case(
        image, "stall", "import time; time.sleep(10)", "stalled",
        timeout_s=5, stall_s=1))
    cases.append(await _case(
        image, "timeout", "import time; time.sleep(10)", "timeout",
        timeout_s=1, stall_s=1))
    cases.append(await _case(
        image, "cancel", "import time; time.sleep(30)", "cancelled",
        timeout_s=10, stall_s=5, cancel=True))
    return {
        "schema": SCHEMA,
        "passed": all(case["passed"] for case in cases),
        "docker_available": True,
        "image_present": True,
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="python:3.11-slim")
    args = parser.parse_args()
    report = asyncio.run(validate(args.image))
    print(json.dumps(report, sort_keys=True))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
