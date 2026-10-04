"""
node_cap_audit.py — static half of the node-parity evidence (no app imports)

For every @capability in the tree: the module-level functions it reaches -
inside its module and across Vera modules (from-imports and module aliases),
but not into the orchestrator, which every process shares - and what that
path shows: host markers (SQLite, the fabric, docker, subprocesses, SSH,
local files, git, a browser, loopback services, environment reads a worker
lacks), writes to module state, reads of module state the host changes at
runtime, and the capabilities it calls in-process.

The runtime half is cap_footprint_core (what the host actually touched while
a capability ran). nodes.parity.vet needs both clean. Run as a module (it
takes ~2 min of CPU on the full tree): `python -m Vera.vera.workers.node_cap_audit <repo root>`
prints {cap: {file, markers, state, calls, via}} as JSON.
"""
import ast
import json
import os
import re
import sys

MARKERS = {
    "sqlite": re.compile(r"_sqlite_conn|sqlite3|_sqlite_query|\.db\b|_db_path|DB_PATH"),
    "fabric": re.compile(r"_get_fabric|data_fabric|FABRIC_"),
    "docker": re.compile(r"\bdocker\b|/var/run/docker"),
    "subprocess": re.compile(r"subprocess|create_subprocess|spawn_core|run_argv|os\.system|\b_sh\("),
    "ssh": re.compile(r"ssh_run|paramiko|asyncssh|\b_ssh\(|exec\.ssh"),
    "localfs": re.compile(r"\bopen\(|Path\(|os\.path|shutil|\.write_text|\.read_text|os\.listdir|glob\.glob|os\.makedirs"),
    "git": re.compile(r"\bgit\b"),
    "browser": re.compile(r"playwright|chromium"),
    "localhost": re.compile(r"127\.0\.0\.1|\blocalhost\b"),
    # a worker's environment carries only the backend URLs: anything else read
    # from the environment (API keys, host config) is missing on a node
    "env": re.compile(r"os\.getenv|os\.environ|\bgetenv\(|\bcfg\.[A-Z]"),
}
CALL_CAP = re.compile(r"""(?:_call|_cap|_rawcap|call_capability|CAPABILITY_REGISTRY(?:\.get)?)\s*[\(\[]\s*["']([a-z_]+\.[a-z0-9_.]+)["']""")


def modkey(path, VROOT):
    rel = os.path.relpath(path, VROOT)[:-3].replace(os.sep, ".")
    return rel


def resolve_mod(name, cur):
    """A Vera module name -> modkey, else None."""
    for pre in ("Vera.vera.", "vera."):
        if name.startswith(pre):
            return name[len(pre):]
    return None


def audit(root):
    VROOT = os.path.join(root, "vera")
    ROOT = root
    MODS = {}   # modkey -> {src, funcs, mutable, imports(name->(mk, attr)), aliases(name->mk), caps}
    for dp, dn, fn in os.walk(VROOT):
        if "__pycache__" in dp:
            continue
        for f in fn:
            if not f.endswith(".py"):
                continue
            path = os.path.join(dp, f)
            try:
                src = open(path, encoding="utf-8").read()
                tree = ast.parse(src)
            except Exception:
                continue
            funcs, mutable, imports, aliases, caps = {}, set(), {}, {}, []
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    funcs[node.name] = node
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    funcs.setdefault(node.name, node)
                    for d in node.decorator_list:
                        if (isinstance(d, ast.Call) and getattr(d.func, "id", getattr(d.func, "attr", "")) == "capability"
                                and d.args and isinstance(d.args[0], ast.Constant) and isinstance(d.args[0].value, str)):
                            caps.append((d.args[0].value, node))
                elif isinstance(node, ast.ImportFrom) and node.module:
                    mk = resolve_mod(node.module, None)
                    for a in node.names:
                        if mk is not None:
                            imports[a.asname or a.name] = (mk, a.name)
                            aliases.setdefault(a.asname or a.name, mk + "." + a.name)
                elif isinstance(node, ast.Import):
                    for a in node.names:
                        mk = resolve_mod(a.name, None)
                        if mk is not None:
                            aliases[a.asname or a.name.split(".")[0]] = mk
            for node in tree.body:
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    tgts = node.targets if isinstance(node, ast.Assign) else [node.target]
                    if isinstance(node.value, (ast.Dict, ast.List, ast.Set, ast.Call)):
                        for t in tgts:
                            if isinstance(t, ast.Name):
                                mutable.add(t.id)
            MODS[modkey(path, VROOT)] = {"src": src, "funcs": funcs, "mutable": mutable, "imports": imports,
                                  "aliases": aliases, "caps": caps, "path": os.path.relpath(path, ROOT)}


    def writes_state(n, mutable):
        for c in ast.walk(n):
            if isinstance(c, ast.Global):
                return True
            if isinstance(c, (ast.Assign, ast.AugAssign)):
                for t in (c.targets if isinstance(c, ast.Assign) else [c.target]):
                    if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) and t.value.id in mutable:
                        return True
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and isinstance(c.func.value, ast.Name):
                if c.func.value.id in mutable and c.func.attr in ("update", "append", "pop", "setdefault", "clear", "add", "remove", "extend", "insert", "discard"):
                    return True
        return False


    INFO, EDGES, WRITTEN = {}, {}, {}


    def written(mk):
        """Module-level mutable names some function of the module writes: state
        the host process changes at runtime, so a worker's copy can differ."""
        if mk not in WRITTEN:
            m = MODS[mk]
            out = set()
            for fn_ in m["funcs"].values():
                for c in ast.walk(fn_):
                    if isinstance(c, ast.Global):
                        out |= set(c.names) & m["mutable"]
                    elif isinstance(c, (ast.Assign, ast.AugAssign)):
                        for t in (c.targets if isinstance(c, ast.Assign) else [c.target]):
                            if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) and t.value.id in m["mutable"]:
                                out.add(t.value.id)
                    elif (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                          and isinstance(c.func.value, ast.Name) and c.func.value.id in m["mutable"]
                          and c.func.attr in ("update", "append", "pop", "setdefault", "clear", "add",
                                              "remove", "extend", "insert", "discard")):
                        out.add(c.func.value.id)
            WRITTEN[mk] = out
        return WRITTEN[mk]


    def info(mk, node):
        k = (mk, id(node))
        if k not in INFO:
            m = MODS[mk]
            seg = ast.get_source_segment(m["src"], node) or ""
            hits = {x for x, rx in MARKERS.items() if rx.search(seg)}
            w = written(mk)
            if any(isinstance(c, ast.Name) and c.id in w for c in ast.walk(node)):
                hits.add("reads_state")
            if orch_reads(m, node):
                hits.add("orch_state")
            INFO[k] = (hits, set(CALL_CAP.findall(seg)), writes_state(node, m["mutable"]))
        return INFO[k]


    #: Orchestrator state filled once while modules load - the same in every
    #: process, a worker included. Everything else it writes at runtime
    #: (instance health, in-flight counts, logs, routing stats, the worker
    #: registry ...) is the HOST's: a worker has the structure, never the data.
    ORCH_LOAD_TIME = {"CAPABILITY_REGISTRY", "LOADED_MODULES", "UI_PANELS", "SHUTDOWN_HOOKS",
                      "STREAM_SUBS", "SCHEDULED_TASKS", "CAP_ROUTING_DECLARED",
                      "ROLE_PROFILES_DECLARED", "_LOG_QLISTENERS"}
    ORCH = "capability_orchestration"
    ORCH_RUNTIME = (written(ORCH) - ORCH_LOAD_TIME) if ORCH in MODS else set()


    def orch_reads(m, node):
        """Does this function read orchestrator runtime state? Through a name
        imported from it, `<alias>.NAME`, or getattr(<alias>, "NAME")."""
        if not ORCH_RUNTIME:
            return False
        direct = {n for n, (tmk, attr) in m["imports"].items() if tmk == ORCH and attr in ORCH_RUNTIME}
        aliases = {a for a, tmk in m["aliases"].items() if tmk == ORCH}
        for c in ast.walk(node):
            if isinstance(c, ast.Name) and c.id in direct:
                return True
            if (isinstance(c, ast.Attribute) and isinstance(c.value, ast.Name)
                    and c.value.id in aliases and c.attr in ORCH_RUNTIME):
                return True
            if (isinstance(c, ast.Call) and getattr(c.func, "id", "") == "getattr" and len(c.args) >= 2
                    and isinstance(c.args[0], ast.Name) and c.args[0].id in aliases
                    and isinstance(c.args[1], ast.Constant) and c.args[1].value in ORCH_RUNTIME):
                return True
        return False


    #: The runtime every process shares (a worker runs the same orchestrator):
    #: its helpers behave the same on a node, so they are not followed.
    SHARED_RUNTIME = {"capability_orchestration", "config"}


    def callees(mk, node):
        k = (mk, id(node))
        if k in EDGES:
            return EDGES[k]
        m = MODS[mk]
        out = []
        for c in ast.walk(node):
            if not isinstance(c, ast.Call):
                continue
            f = c.func
            if isinstance(f, ast.Name):
                if f.id in m["funcs"] and m["funcs"][f.id] is not node:
                    out.append((mk, m["funcs"][f.id]))
                elif f.id in m["imports"]:
                    tmk, attr = m["imports"][f.id]
                    t = MODS.get(tmk)
                    if t and attr in t["funcs"] and tmk not in SHARED_RUNTIME:
                        out.append((tmk, t["funcs"][attr]))
            elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in m["aliases"]:
                tmk = m["aliases"][f.value.id]
                t = MODS.get(tmk)
                if t and f.attr in t["funcs"] and tmk not in SHARED_RUNTIME:
                    out.append((tmk, t["funcs"][f.attr]))
        EDGES[k] = out
        return out


    rows = {}
    for mk, m in MODS.items():
        for name, node in m["caps"]:
            hits, calls, state, seen, stack, mods = set(), set(), False, set(), [(mk, node)], set()
            while stack:
                cmk, n = stack.pop()
                if (cmk, id(n)) in seen:
                    continue
                seen.add((cmk, id(n)))
                h, c, st = info(cmk, n)
                hits |= h; calls |= c; state = state or st
                if cmk != mk:
                    mods.add(cmk)
                stack.extend(callees(cmk, n))
            rows[name] = {"file": m["path"], "markers": sorted(hits), "state": state,
                          "calls": sorted(x for x in calls if x != name), "via": sorted(mods)[:12]}

    return rows


if __name__ == "__main__":
    print(json.dumps(audit(sys.argv[1] if len(sys.argv) > 1 else os.getcwd())))
