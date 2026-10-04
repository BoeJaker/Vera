#!/usr/bin/env python3
"""Verify the quality-review claims (items 17-25) against every goal in the given census files.
Counts, from the loop's own event streams, the shapes the review described.

    python3 verify_review.py census.run70.jsonl census.run71.jsonl ...
"""
import json, subprocess, sys, re, collections


def rc(*a):
    return subprocess.run(["redis-cli", *a], capture_output=True, text=True).stdout


def events(sid):
    return [json.loads(l) for l in rc("lrange", f"vera:loop:events:{sid}", "0", "-1").splitlines() if l.strip()]


C = collections.Counter()
EX = collections.defaultdict(list)
goals = 0
for f in sys.argv[1:]:
    for line in open(f):
        row = json.loads(line)
        sid = row["session_id"]
        goals += 1
        ev = events(sid)
        tag = f"{f.split('.')[1]}/{row['id']}"
        C["executor_turns"] += sum(1 for e in ev if e.get("type") == "agent_loop_v5.tool_call")
        calls = [e for e in ev if e.get("type") in ("agent_loop_v5.tool_call", "agent_loop_v5.tool_done")]
        # --- 21b: recovery started for a call that RAN and stopped on its own limit (browser budget, wall cap)
        for e in ev:
            if e.get("type") == "agent_loop.error_recovery_start":
                err = str(e.get("error") or e.get("reason") or e.get("note") or "").lower()
                if any(m in err for m in ("time_budget", "no_progress", "repeating_action", "too_many_errors", "stopped after")) or str(e.get("tool") or "").startswith("operator."):
                    C["21_recovery_of_run_stop"] += 1
                    EX["21b"].append(f"{tag} c{e.get('cycle')} {e.get('tool')} {err[:50]}")
        # --- 17e: research calls in a step AFTER two successful web.research results (the sources are in hand)
        _rs_ok = {}
        for e in calls:
            st = str(e.get("step_id")); tool = str(e.get("tool") or "")
            if e["type"] == "agent_loop_v5.tool_done" and tool == "web.research" and e.get("ok"):
                _rs_ok[st] = _rs_ok.get(st, 0) + 1
            elif e["type"] == "agent_loop_v5.tool_call" and tool.startswith(("web.", "http.get")) and _rs_ok.get(st, 0) >= 2:
                if e.get("repeat"):
                    C["17e_research_calls_served"] += 1        # 17e served it at 0 ms - no call ran
                else:
                    C["17e_research_calls_after_sources"] += 1
                    EX["17e"].append(f"{tag} s{st} c{e.get('cycle')} {tool}")
        # --- 17d: a successful author call re-issued for the SAME path in the same step with no failure between
        _authored = {}
        _failed_since = set()
        _last_author_path = {}
        for e in calls:
            st = str(e.get("step_id")); tool = e.get("tool")
            if e["type"] == "agent_loop_v5.tool_call" and tool in ("code.author", "prose.author"):
                pth = str((e.get("args") or {}).get("path") or "").rsplit("/", 1)[-1]
                _last_author_path[(st, str(e.get("cycle")))] = pth
                key = (st, pth)
                if pth and key in _authored and key not in _failed_since:
                    C["17d_reauthor_same_path"] += 1
                    EX["17d"].append(f"{tag} s{st} c{e.get('cycle')} {tool} {pth}")
            elif e["type"] == "agent_loop_v5.tool_done":
                pth = str((e.get("args") or {}).get("path") or _last_author_path.get((st, str(e.get("cycle"))), "")).rsplit("/", 1)[-1]
                if tool in ("code.author", "prose.author") and e.get("ok") and pth:
                    _authored[(st, pth)] = e.get("cycle"); _failed_since.discard((st, pth))
                elif not e.get("ok"):
                    for k in list(_authored):
                        if k[0] == st: _failed_since.add(k)
        # --- 17: identical successful call repeated within a step
        seen = {}
        pending = {}
        for e in calls:
            if e["type"] == "agent_loop_v5.tool_call":
                key = (str(e.get("step_id")), e.get("tool"), json.dumps(e.get("args"), sort_keys=True))
                pending[(str(e.get("step_id")), e.get("tool"), e.get("cycle"))] = key
                if key in seen and seen[key]:
                    C["17_repeat_of_successful_call"] += 1
                    EX["17"].append(f"{tag} c{e.get('cycle')} {e.get('tool')}")
            else:
                key = pending.get((str(e.get("step_id")), e.get("tool"), e.get("cycle")))
                err = str(e.get("error") or "")
                if key is not None:
                    seen[key] = str(e.get("ok")) == "True" and not err
                if "repeat of an identical failed call" in err or ("budget" in err and "spent" in err):
                    C["refused_calls"] += 1
                if "There is NO capability called 'done'" in err:
                    C["24_done_pseudo_tool"] += 1
                    EX["24"].append(tag)
                if "`find` text not present" in err and "Closest text" in err:
                    C["25_stale_anchor_with_closest"] += 1
                    EX["25"].append(tag)
        # 17b: read/cat/ls of a file right after an authored ok
        last = None
        for e in calls:
            if e["type"] == "agent_loop_v5.tool_done":
                last = (e.get("tool"), str(e.get("ok")) == "True")
            else:
                a = json.dumps(e.get("args"))
                if last and last[1] and last[0] in ("code.author", "prose.author", "code.edit") and (
                        e.get("tool") == "sandbox.session.fs.read"
                        or (e.get("tool") == "exec.bash.run" and re.search(r"\b(cat|ls|wc|head)\b", a))):
                    C["17b_reverify_after_authored_ok"] += 1
        # --- 18 / 19 / 20 / 21 / 22 / 23
        goal = rc("hget", f"vera:loop:run:{sid}", "goal").lower()
        for e in ev:
            t = e.get("type")
            if t == "agent_loop_v6.verify":
                r = str(e.get("reason") or "").lower()
                C["verify_total"] += 1
                if (re.search(r"summary (explicitly )?(states|says|confirms|reports)|step's (result|output) summary|the summary", r)
                        and not re.search(r"\b(?:operator\.run|web\.\w+|exec\.\w+|code\.\w+|prose\.author|tool (?:call|output|result)|the tool)\b", r)
                        and "never written to disk" not in r):      # the named-file check's own wording
                    C["18_verify_cites_summary"] += 1         # the executor's story, not a tool's own summary field
                    EX["18"].append(f"{tag} s{e.get('step_id')}")
                if "despite" in r and "no stdout" in r:
                    C["18_verify_inverts_no_stdout"] += 1
            elif t == "agent_loop_v5.step_done":
                s = str(e.get("summary") or "").lstrip()
                C["step_done_total"] += 1
                if s.startswith("{") or s.startswith("```"):
                    C["19_summary_is_raw_json"] += 1
            elif t == "agent_loop_v5.tool_call" and e.get("tool") in ("code.author", "code.edit", "prose.author"):
                a = e.get("args") or {}
                if isinstance(a, dict) and "CONTROLLER STEER" in str(a.get("task", "")):
                    C["20_steer_in_task"] += 1
                    EX["20"].append(f"{tag} c{e.get('cycle')}")
            elif t == "agent_loop.error_recovery_done":
                C["21_recovery_attempts"] += 1
                if str(e.get("recovered")) == "True":
                    C["21_recovered"] += 1
                rs = str(e.get("reason") or "")
                if "unknown provider" in rs:
                    C["21_unknown_provider"] += 1
                    EX["21p"].append(f"{tag}: {rs[:60]}")
                if "ERR_EMPTY_RESPONSE" in rs or "id=None" in rs:
                    C["21_target_rewritten"] += 1
            elif t == "agent_loop_v6.plan":
                steps = e.get("steps")
                if isinstance(steps, str):
                    try:
                        steps = eval(steps, {"__builtins__": {}}, {})
                    except Exception:
                        steps = []
                C["plans"] += 1
                if any(re.match(r"verify|validate|confirm|check", str(s.get("title", "")).lower()) for s in steps or []):
                    C["22_plan_has_verify_step"] += 1
                    EX["22v"].append(tag)
                if row["id"] == "operate-exec" and len(steps or []) > 1:
                    C["22_operate_goal_multistep"] += 1
                    EX["22o"].append(f"{tag} steps={len(steps)}")
                for s in steps or []:
                    succ = str(s.get("success", "")).lower()
                    for term in ("reload", "pause", "reset", "mm:ss", "59", "keyboard", "progress bar"):
                        if term in succ and term not in goal:
                            C["22_success_adds_requirement"] += 1
                            EX["22r"].append(f"{tag}: '{term}'")
                            break
            elif t == "agent_loop_v6.deliverable":
                md = str(e.get("markdown") or "")
                C["deliverables"] += 1
                _dump = any((b.count("\n") >= 8 and ("<!DOCTYPE" in b or "<html" in b or "function " in b or "def " in b))
                            for b in re.findall(r"```[^\n]*\n(.*?)\n[ \t]*```", md, re.S))
                if _dump:
                    C["23_answer_dumps_source"] += 1          # a fenced whole-file paste, not a mention
                    EX["23"].append(tag)
                if "lead with the direct answer" in md.lower() or "faithful, concrete account" in md.lower():
                    C["23_answer_echoes_template"] += 1
                    EX["23e"].append(tag)
                if row["id"].startswith("research") and not re.search(r"https?://", md):
                    C["23_research_answer_without_url"] += 1
                    EX["23u"].append(tag)
            elif t == "agent_loop_v6.deliverable_shaped":
                C["23_deliverable_shaped"] += 1
                EX["23s"].append(f"{tag}: {str(e.get('notes'))[:80]}")
            elif t == "agent_loop_v6.plan_hygiene":
                C["22_plan_hygiene_applied"] += 1
                EX["22h"].append(f"{tag}: {str(e.get('dropped') or e.get('merged'))[:100]}")
            elif t == "agent_loop_v6.done":
                ans = str(e.get("summary") or "")
                C["answers"] += 1
                if "```html" in ans or "<!DOCTYPE html>" in ans:
                    C["23_final_dumps_source"] += 1
                    EX["23f"].append(tag)
                if row.get("intent") == "research" and "http" not in ans and "[" not in ans:
                    C["23_research_answer_no_citation"] += 1
                    EX["23r"].append(tag)
        # --- 24: operator.run called again after a done operator result in the same step
        done_steps = set()
        for e in calls:
            if e["type"] == "agent_loop_v5.tool_done" and e.get("tool") == "operator.run" and str(e.get("ok")) == "True":
                done_steps.add(str(e.get("step_id")))
            elif e["type"] == "agent_loop_v5.tool_call" and e.get("tool") == "operator.run" and str(e.get("step_id")) in done_steps:
                if e.get("repeat"):
                    C["24_operator_after_done_served"] += 1      # 24a served it at 0 ms - no browser ran
                else:
                    C["24_operator_call_after_done"] += 1
                    EX["24o"].append(f"{tag} c{e.get('cycle')}")

print(f"goals={goals} executor_turns={C['executor_turns']}")
for k in sorted(C):
    if k == "executor_turns":
        continue
    print(f"  {k:38s} {C[k]}")
for k, v in EX.items():
    print(f"EX {k}: {len(v)} e.g. {v[:4]}")
