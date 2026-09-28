"""Re-capture the prompt goldens after an INTENDED prompt change.

Run inside the app image with the worktree mounted read-write at /app/Vera:

    docker run --rm -v <worktree>:/app/Vera:rw -e PYTHONPATH=/app:/app/Vera \
        -e TLS_ENABLED=0 -e VERA_IS_DEV_SANDBOX=1 -w /app/Vera vera:latest \
        python tests/golden/recapture.py

It composes exactly what tests/test_planner_prompt_golden.py and
tests/test_executor_prompt_compose.py compose, and writes both JSON files.
Commit the result in the same commit as the prompt change.
"""
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

import test_planner_prompt_golden as TP          # noqa: E402
import test_executor_prompt_compose as TE        # noqa: E402

M = TP.M
assert M is not None, "app module not importable - run inside the app image"

planner = {
    "minimal": TP._compose(True),
    "full": TP._compose(False),
    "_rules": {"cap_routing": M._V7_CAP_ROUTING, "criteria_settleable": M._V7_CRITERIA_RULE},
}
old = json.loads(TP.GOLDEN.read_text(encoding="utf-8"))
for k in ("minimal", "full"):
    planner[k] = {"system": planner[k]["system"], "prompt": planner[k]["prompt"]}
TP.GOLDEN.write_text(json.dumps(planner, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
print("planner_prompts.json:", {k: (len(planner[k]["system"]), len(planner[k]["prompt"])) for k in ("minimal", "full")},
      "was", {k: (len(old[k]["system"]), len(old[k]["prompt"])) for k in ("minimal", "full")})

ex = M._v5_compose_executor_system(**TE._inputs())
olde = json.loads(TE.GOLDEN.read_text(encoding="utf-8"))
TE.GOLDEN.write_text(json.dumps({"executor": ex, "_note": olde.get("_note", "")}, indent=1, ensure_ascii=False) + "\n",
                     encoding="utf-8")
print("executor_prompt.json:", len(ex), "was", len(olde.get("executor", "")))
