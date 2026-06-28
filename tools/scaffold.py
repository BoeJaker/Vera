#!/usr/bin/env python3
"""
scaffold.py — Vera onboarding & deployment artifact generator
=============================================================

Generates, in one shot, everything a new operator needs to stand up and explore
Vera:

    ansible/playbook.yml            Ansible deploy (docker OR native venv, by tag)
    ansible/inventory.ini.example   Sample inventory
    docker-compose.yml              Compose stack (only if missing, unless --force)
    .env.example                    All tunable env vars with sane defaults
    Makefile                        make up / down / logs / run / venv / test / tour …
    build.sh                        POSIX build/run helper (bash)
    build.ps1                       Windows build/run helper (PowerShell)
    docs/GETTING_STARTED.md         Standalone getting-started guide
    README.MD                       "Getting Started" managed block injected
    welcome/index.html             Interactive HTML guide + guided tour
    welcome/welcome.py             Terminal guided tour (stdlib only)
    notebooks/vera_quickstart.ipynb Jupyter notebook: boot the system & test caps

Everything is derived from the real runtime contract:
    * local entrypoint : python -m Vera.vera.capability_orchestration  (port 8999)
    * docker entrypoint: uvicorn Vera.vera.capability_orchestration:APP
    * cap endpoints    : GET /health · GET /mcp/tools · POST /mcp/call · /docs · /

Usage:
    python tools/scaffold.py                 # generate everything (skip existing files)
    python tools/scaffold.py --force         # overwrite existing files too
    python tools/scaffold.py --only make,env # generate a subset
    python tools/scaffold.py --list          # list generators and exit
    python tools/scaffold.py --dry-run       # show what would be written

Run it from the repo root (the directory that contains the `vera/` package).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# ── Runtime contract (single source of truth for every template) ───────────────
PORT          = 8999
MODULE        = "Vera.vera.capability_orchestration"
APP_PATH      = f"{MODULE}:APP"
BASE_URL      = f"http://localhost:{PORT}"
REPO_NAME     = "Vera"

PORTS = {
    "orchestrator": PORT,
    "redis":        6379,
    "postgres":     5433,
    "chroma":       8008,
    "neo4j_http":   7474,
    "neo4j_bolt":   7687,
}

# Capabilities worth showing off in the notebook / tour (name, sample args)
DEMO_CAPS = [
    ("health.check", {}),
    ("echo",         {"message": "Hello from Vera"}),
    ("obs.modules",  {}),
]


# ═══════════════════════════════════════════════════════════════════════════════
#  Template builders — each returns the file body as a string
# ═══════════════════════════════════════════════════════════════════════════════
def t_env_example() -> str:
    return f"""\
# ═══════════════════════════════════════════════════════════════════════════════
#  Vera — environment configuration
#  Copy to `.env` and edit. docker-compose and the build scripts read this file.
# ═══════════════════════════════════════════════════════════════════════════════

# ── Orchestrator ──────────────────────────────────────────────────────────────
ORCHESTRATOR_HOST=0.0.0.0
ORCHESTRATOR_PORT={PORT}
BACKEND_HOST=llm.int

# ── Master key for encrypting stored credentials (Fernet) ─────────────────────
#  Generate once and keep it stable, or sealed secrets become undecryptable:
#    python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"
VERA_SECRET_KEY=

# ── Backing services ──────────────────────────────────────────────────────────
#  For docker compose these point at the compose service names.
#  For a native/local run, point them at wherever your services live.
REDIS_URL=redis://redis:6379
POSTGRES_URL=postgresql://admin:admin@postgres:5432/postgres
CHROMA_HOST=chromadb
CHROMA_PORT=8000
NEO4J_URI=bolt://neo4j:7687
NEO4J_USER=neo4j
NEO4J_PASS=veraneo4j

# ── Host port mappings (docker compose) ───────────────────────────────────────
REDIS_PORT={PORTS['redis']}
POSTGRES_PORT={PORTS['postgres']}
CHROMA_PORT_HOST={PORTS['chroma']}
NEO4J_HTTP_PORT={PORTS['neo4j_http']}
NEO4J_BOLT_PORT={PORTS['neo4j_bolt']}

# ── Ollama cluster (external hosts) ───────────────────────────────────────────
OLLAMA_GPU_URL=http://192.168.0.250:11435
OLLAMA_CPU_A_URL=http://192.168.0.246:11435
OLLAMA_CPU_B_URL=http://192.168.0.247:11435
OLLAMA_MODEL=jaahas/qwen3.5-uncensored
OLLAMA_EMBED_URL=http://192.168.0.246:11435
OLLAMA_EMBED_MODEL=nomic-embed-text

# ── GPU inference (Whisper / TTS / SD) ────────────────────────────────────────
GPU_INFER_URL=http://192.168.0.250:8765

# ── Tuning ────────────────────────────────────────────────────────────────────
MAX_CAPS_IN_PROMPT=25
EMBED_CAPS_ON_START=1
SYSLOG_MONITOR=1
"""


def t_makefile() -> str:
    # NOTE: Makefile requires literal tabs for recipe lines.
    return f"""\
# ═══════════════════════════════════════════════════════════════════════════════
#  Vera — Makefile  (generated by tools/scaffold.py)
#  Run `make help` for the full target list.
# ═══════════════════════════════════════════════════════════════════════════════
.DEFAULT_GOAL := help
SHELL := /bin/bash

# Parent of the repo root must be importable so `Vera.vera...` resolves.
REPO_ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
PARENT    := $(abspath $(REPO_ROOT)/..)
PY        ?= python3
VENV      ?= .venv
PORT      ?= {PORT}
BASE      ?= {BASE_URL}

COMPOSE := $(shell if docker compose version >/dev/null 2>&1; then echo "docker compose"; else echo "docker-compose"; fi)

.PHONY: help
help:  ## Show this help
\t@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \\
\t\tawk 'BEGIN {{FS = ":.*?## "}}; {{printf "  \\033[36m%-14s\\033[0m %s\\n", $$1, $$2}}'

# ── Docker stack ───────────────────────────────────────────────────────────────
.PHONY: up
up:  ## Start the full stack in docker (detached)
\t$(COMPOSE) up -d

.PHONY: build
build:  ## Rebuild the vera image and start
\t$(COMPOSE) up -d --build

.PHONY: down
down:  ## Stop the stack
\t$(COMPOSE) down

.PHONY: nuke
nuke:  ## Stop the stack AND delete volumes (DESTRUCTIVE)
\t$(COMPOSE) down -v

.PHONY: logs
logs:  ## Follow orchestrator logs
\t$(COMPOSE) logs -f vera

.PHONY: ps
ps:  ## Show stack status
\t$(COMPOSE) ps

# ── Native (no docker) ─────────────────────────────────────────────────────────
.PHONY: venv
venv:  ## Create a local virtualenv and install requirements
\t$(PY) -m venv $(VENV)
\t$(VENV)/bin/pip install --upgrade pip
\t$(VENV)/bin/pip install -r requirements.txt

.PHONY: run
run:  ## Run the orchestrator locally (python -m), reading .env if present
\tcd $(PARENT) && PYTHONPATH=$(PARENT) $(PY) -m {MODULE}

.PHONY: secret
secret:  ## Print a fresh VERA_SECRET_KEY
\t$(PY) -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"

# ── Verification & exploration ─────────────────────────────────────────────────
.PHONY: health
health:  ## Hit GET /health on the running orchestrator
\tcurl -sf $(BASE)/health | $(PY) -m json.tool || echo "Vera not reachable at $(BASE)"

.PHONY: caps
caps:  ## List registered capabilities (GET /mcp/tools)
\tcurl -sf $(BASE)/mcp/tools | $(PY) -m json.tool || echo "Vera not reachable at $(BASE)"

.PHONY: test
test:  ## Smoke-test the running orchestrator
\t$(PY) welcome/welcome.py --check

.PHONY: notebook
notebook:  ## Launch the quickstart Jupyter notebook
\tjupyter notebook notebooks/vera_quickstart.ipynb

.PHONY: tour
tour:  ## Run the terminal guided tour
\t$(PY) welcome/welcome.py

.PHONY: welcome
welcome:  ## Open the HTML welcome guide in a browser
\t$(PY) -c "import webbrowser,pathlib;webbrowser.open(pathlib.Path('welcome/index.html').resolve().as_uri())"

.PHONY: docs
docs:  ## Open the API docs (Swagger UI)
\t$(PY) -c "import webbrowser;webbrowser.open('$(BASE)/docs')"
"""


def t_build_sh() -> str:
    return f"""\
#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
#  Vera — build/run helper (POSIX)   generated by tools/scaffold.py
#  Usage: ./build.sh <command>
# ═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
PARENT="$(dirname "$REPO_ROOT")"
PY="${{PY:-python3}}"
VENV="${{VENV:-.venv}}"
PORT="${{PORT:-{PORT}}}"
BASE="http://localhost:$PORT"

if docker compose version >/dev/null 2>&1; then COMPOSE="docker compose"; else COMPOSE="docker-compose"; fi

usage() {{
  cat <<EOF
Vera build helper

  ./build.sh up         Start full docker stack (detached)
  ./build.sh build      Rebuild image and start
  ./build.sh down       Stop the stack
  ./build.sh logs       Follow orchestrator logs
  ./build.sh venv       Create .venv and install requirements
  ./build.sh run        Run orchestrator locally (no docker)
  ./build.sh secret     Print a fresh VERA_SECRET_KEY
  ./build.sh health     Curl GET /health
  ./build.sh caps       List capabilities (GET /mcp/tools)
  ./build.sh tour       Terminal guided tour
  ./build.sh welcome    Open the HTML welcome guide
EOF
}}

cmd="${{1:-help}}"
case "$cmd" in
  up)      $COMPOSE up -d ;;
  build)   $COMPOSE up -d --build ;;
  down)    $COMPOSE down ;;
  logs)    $COMPOSE logs -f vera ;;
  venv)    "$PY" -m venv "$VENV" && "$VENV/bin/pip" install --upgrade pip && "$VENV/bin/pip" install -r requirements.txt ;;
  run)     cd "$PARENT" && PYTHONPATH="$PARENT" "$PY" -m {MODULE} ;;
  secret)  "$PY" -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())" ;;
  health)  curl -sf "$BASE/health" | "$PY" -m json.tool || echo "Vera not reachable at $BASE" ;;
  caps)    curl -sf "$BASE/mcp/tools" | "$PY" -m json.tool || echo "Vera not reachable at $BASE" ;;
  tour)    "$PY" welcome/welcome.py ;;
  welcome) "$PY" -c "import webbrowser,pathlib;webbrowser.open(pathlib.Path('welcome/index.html').resolve().as_uri())" ;;
  help|*)  usage ;;
esac
"""


def t_build_ps1() -> str:
    return f"""\
# ═══════════════════════════════════════════════════════════════════════════════
#  Vera - build/run helper (Windows PowerShell)   generated by tools/scaffold.py
#  Usage: .\\build.ps1 <command>
# ═══════════════════════════════════════════════════════════════════════════════
param([Parameter(Position=0)][string]$Command = "help")

$ErrorActionPreference = "Stop"
$RepoRoot = $PSScriptRoot
$Parent   = Split-Path $RepoRoot -Parent
$Py       = if ($env:PY) {{ $env:PY }} else {{ "python" }}
$Venv     = if ($env:VENV) {{ $env:VENV }} else {{ ".venv" }}
$Port     = if ($env:PORT) {{ $env:PORT }} else {{ "{PORT}" }}
$Base     = "http://localhost:$Port"

function Get-Compose {{
  docker compose version *> $null
  if ($LASTEXITCODE -eq 0) {{ return "docker compose" }} else {{ return "docker-compose" }}
}}

function Show-Usage {{
  @"
Vera build helper

  .\\build.ps1 up         Start full docker stack (detached)
  .\\build.ps1 build      Rebuild image and start
  .\\build.ps1 down       Stop the stack
  .\\build.ps1 logs       Follow orchestrator logs
  .\\build.ps1 venv       Create .venv and install requirements
  .\\build.ps1 run        Run orchestrator locally (no docker)
  .\\build.ps1 secret     Print a fresh VERA_SECRET_KEY
  .\\build.ps1 health     GET /health
  .\\build.ps1 caps       List capabilities (GET /mcp/tools)
  .\\build.ps1 tour       Terminal guided tour
  .\\build.ps1 welcome    Open the HTML welcome guide
"@ | Write-Output
}}

switch ($Command) {{
  "up"      {{ Invoke-Expression "$(Get-Compose) up -d" }}
  "build"   {{ Invoke-Expression "$(Get-Compose) up -d --build" }}
  "down"    {{ Invoke-Expression "$(Get-Compose) down" }}
  "logs"    {{ Invoke-Expression "$(Get-Compose) logs -f vera" }}
  "venv"    {{ & $Py -m venv $Venv; & "$Venv\\Scripts\\pip.exe" install --upgrade pip; & "$Venv\\Scripts\\pip.exe" install -r requirements.txt }}
  "run"     {{ $env:PYTHONPATH = $Parent; Push-Location $Parent; & $Py -m {MODULE}; Pop-Location }}
  "secret"  {{ & $Py -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())" }}
  "health"  {{ try {{ Invoke-RestMethod "$Base/health" | ConvertTo-Json -Depth 6 }} catch {{ Write-Output "Vera not reachable at $Base" }} }}
  "caps"    {{ try {{ Invoke-RestMethod "$Base/mcp/tools" | ConvertTo-Json -Depth 6 }} catch {{ Write-Output "Vera not reachable at $Base" }} }}
  "tour"    {{ & $Py welcome/welcome.py }}
  "welcome" {{ & $Py -c "import webbrowser,pathlib;webbrowser.open(pathlib.Path('welcome/index.html').resolve().as_uri())" }}
  default   {{ Show-Usage }}
}}
"""


def t_ansible_inventory() -> str:
    return """\
# Vera Ansible inventory — copy to inventory.ini and edit.
#
# Two ways to deploy (choose with --tags):
#   ansible-playbook -i inventory.ini playbook.yml --tags docker
#   ansible-playbook -i inventory.ini playbook.yml --tags native

[vera]
vera-host ansible_host=192.168.0.10 ansible_user=ubuntu

[vera:vars]
# Where to place the repo on the target host
vera_dest=/opt/vera
# Master Fernet key (generate with: make secret). Leave blank to auto-generate.
vera_secret_key=
"""


def t_ansible_playbook() -> str:
    return f"""\
# ═══════════════════════════════════════════════════════════════════════════════
#  Vera — Ansible deployment   (generated by tools/scaffold.py)
#
#  Docker deploy (recommended):
#    ansible-playbook -i inventory.ini playbook.yml --tags docker
#  Native deploy (systemd + venv):
#    ansible-playbook -i inventory.ini playbook.yml --tags native
# ═══════════════════════════════════════════════════════════════════════════════
- name: Deploy Vera
  hosts: vera
  become: true
  vars:
    vera_dest: /opt/vera
    vera_secret_key: ""
    vera_repo: "{{{{ playbook_dir }}}}/.."   # sync the local checkout by default

  tasks:
    # ── Sync source to the target ────────────────────────────────────────────
    - name: Ensure destination exists
      ansible.builtin.file:
        path: "{{{{ vera_dest }}}}"
        state: directory
        mode: "0755"

    - name: Copy Vera source to host
      ansible.posix.synchronize:
        src: "{{{{ vera_repo }}}}/"
        dest: "{{{{ vera_dest }}}}/"
        rsync_opts:
          - "--exclude=.git"
          - "--exclude=.venv"
          - "--exclude=__pycache__"
          - "--exclude=deprecated"

    - name: Generate a Fernet secret if none provided
      ansible.builtin.shell: "python3 -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())'"
      register: gen_key
      when: vera_secret_key | length == 0
      changed_when: false

    - name: Render .env
      ansible.builtin.copy:
        dest: "{{{{ vera_dest }}}}/.env"
        mode: "0600"
        content: |
          ORCHESTRATOR_PORT={PORT}
          VERA_SECRET_KEY={{{{ vera_secret_key if vera_secret_key | length > 0 else gen_key.stdout }}}}
          REDIS_URL=redis://redis:6379
          POSTGRES_URL=postgresql://admin:admin@postgres:5432/postgres
          CHROMA_HOST=chromadb
          CHROMA_PORT=8000
          NEO4J_URI=bolt://neo4j:7687
          NEO4J_USER=neo4j
          NEO4J_PASS=veraneo4j

    # ── Docker deployment ────────────────────────────────────────────────────
    - name: Install Docker (Debian/Ubuntu)
      ansible.builtin.apt:
        name: [docker.io, docker-compose-plugin]
        state: present
        update_cache: true
      tags: [docker]

    - name: Bring up the Vera stack
      community.docker.docker_compose_v2:
        project_src: "{{{{ vera_dest }}}}"
        state: present
        build: always
      tags: [docker]

    # ── Native deployment (systemd + venv) ───────────────────────────────────
    - name: Install Python toolchain
      ansible.builtin.apt:
        name: [python3, python3-venv, python3-pip, build-essential, libpq-dev]
        state: present
        update_cache: true
      tags: [native]

    - name: Create virtualenv & install requirements
      ansible.builtin.pip:
        requirements: "{{{{ vera_dest }}}}/requirements.txt"
        virtualenv: "{{{{ vera_dest }}}}/.venv"
        virtualenv_command: python3 -m venv
      tags: [native]

    - name: Install systemd unit
      ansible.builtin.copy:
        dest: /etc/systemd/system/vera.service
        mode: "0644"
        content: |
          [Unit]
          Description=Vera Orchestrator
          After=network.target

          [Service]
          WorkingDirectory={{{{ vera_dest }}}}/..
          EnvironmentFile={{{{ vera_dest }}}}/.env
          Environment=PYTHONPATH={{{{ vera_dest }}}}/..
          ExecStart={{{{ vera_dest }}}}/.venv/bin/python -m {MODULE}
          Restart=on-failure

          [Install]
          WantedBy=multi-user.target
      tags: [native]

    - name: Enable & start vera.service
      ansible.builtin.systemd:
        name: vera
        enabled: true
        state: restarted
        daemon_reload: true
      tags: [native]
"""


def t_docker_compose() -> str:
    """Faithful copy of the curated compose, only written when one is absent."""
    return f"""\
# ═══════════════════════════════════════════════════════════════════════════════
#  Vera Orchestration Platform — Docker Compose  (generated by tools/scaffold.py)
#    vera (port {PORT}) · redis · postgres · chromadb · neo4j
#  Usage: docker compose up -d  |  docker compose logs -f vera  |  docker compose down -v
# ═══════════════════════════════════════════════════════════════════════════════
services:
  vera:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: vera-orchestrator
    ports:
      - "${{ORCHESTRATOR_PORT:-{PORT}}}:{PORT}"
    environment:
      ORCHESTRATOR_HOST: "0.0.0.0"
      ORCHESTRATOR_PORT: "{PORT}"
      VERA_SECRET_KEY: "${{VERA_SECRET_KEY:-}}"
      REDIS_URL: "redis://redis:6379"
      POSTGRES_URL: "postgresql://admin:admin@postgres:5432/postgres"
      CHROMA_HOST: "chromadb"
      CHROMA_PORT: "8000"
      NEO4J_URI: "bolt://neo4j:7687"
      NEO4J_USER: "${{NEO4J_USER:-neo4j}}"
      NEO4J_PASS: "${{NEO4J_PASS:-veraneo4j}}"
      OLLAMA_GPU_URL: "${{OLLAMA_GPU_URL:-http://192.168.0.250:11435}}"
      OLLAMA_MODEL: "${{OLLAMA_MODEL:-jaahas/qwen3.5-uncensored}}"
      VERA_PROJECT_ROOT: "/data/projects"
    volumes:
      - vera-projects:/data/projects
    depends_on:
      redis: {{condition: service_healthy}}
      postgres: {{condition: service_healthy}}
      chromadb: {{condition: service_started}}
      neo4j: {{condition: service_healthy}}
    restart: unless-stopped
    networks: [vera-net]

  redis:
    image: redis:7-alpine
    container_name: vera-redis
    ports: ["${{REDIS_PORT:-{PORTS['redis']}}}:6379"]
    volumes: [redis-data:/data]
    command: redis-server --appendonly yes --maxmemory 512mb --maxmemory-policy allkeys-lru
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 3s
      retries: 5
    restart: unless-stopped
    networks: [vera-net]

  postgres:
    image: postgres:16-alpine
    container_name: vera-postgres
    ports: ["${{POSTGRES_PORT:-{PORTS['postgres']}}}:5432"]
    environment:
      POSTGRES_USER: admin
      POSTGRES_PASSWORD: admin
      POSTGRES_DB: postgres
    volumes: [pg-data:/var/lib/postgresql/data]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U admin -d postgres"]
      interval: 10s
      timeout: 3s
      retries: 5
    restart: unless-stopped
    networks: [vera-net]

  chromadb:
    image: chromadb/chroma:0.5.23
    container_name: vera-chromadb
    ports: ["${{CHROMA_PORT_HOST:-{PORTS['chroma']}}}:8000"]
    volumes: [chroma-data:/chroma/chroma]
    environment:
      IS_PERSISTENT: "TRUE"
      ANONYMIZED_TELEMETRY: "FALSE"
    restart: unless-stopped
    networks: [vera-net]

  neo4j:
    image: neo4j:5-community
    container_name: vera-neo4j
    ports:
      - "${{NEO4J_HTTP_PORT:-{PORTS['neo4j_http']}}}:7474"
      - "${{NEO4J_BOLT_PORT:-{PORTS['neo4j_bolt']}}}:7687"
    environment:
      NEO4J_AUTH: "${{NEO4J_USER:-neo4j}}/${{NEO4J_PASS:-veraneo4j}}"
      NEO4J_PLUGINS: '["apoc"]'
      NEO4J_server_memory_heap_max__size: "512m"
    volumes: [neo4j-data:/data]
    restart: unless-stopped
    networks: [vera-net]

volumes:
  redis-data:
  pg-data:
  chroma-data:
  neo4j-data:
  vera-projects:

networks:
  vera-net:
    driver: bridge
"""


GETTING_STARTED_BODY = f"""\
Vera can run two ways: the **full docker stack** (everything wired up for you) or
a **native** orchestrator process talking to backends you host elsewhere.

### Option A — Docker (everything in one command)

```bash
cp .env.example .env          # then edit secrets/hosts
make secret                   # generate VERA_SECRET_KEY -> paste into .env
make up                       # start vera + redis + postgres + chromadb + neo4j
make logs                     # watch it boot
```

Or without `make`:

```bash
# Linux / macOS
./build.sh up

# Windows
.\\build.ps1 up
```

Then open the harness at **{BASE_URL}/** and the API docs at **{BASE_URL}/docs**.

### Option B — Native (no docker)

You supply Redis / Postgres / Chroma / Neo4j (or let caps degrade gracefully —
core capability registration and HTTP come up even without backends).

```bash
make venv                     # create .venv + install requirements
make run                      # python -m {MODULE}
```

`make run` sets `PYTHONPATH` to the repo's parent so the `{MODULE}`
package resolves, and binds `0.0.0.0:{PORT}`.

### Verify it's alive

```bash
make health                   # GET /health
make caps                     # GET /mcp/tools  (list every capability)
```

Or invoke a capability directly:

```bash
curl -s {BASE_URL}/mcp/call \\
  -H 'content-type: application/json' \\
  -d '{{"name":"echo","arguments":{{"message":"hello"}}}}'
```

### Explore interactively

| What | How |
|---|---|
| **Guided terminal tour** | `make tour` (or `python welcome/welcome.py`) |
| **HTML welcome guide** | `make welcome` — opens `welcome/index.html` |
| **Quickstart notebook** | `make notebook` — boot & test caps from Jupyter |
| **Swagger API docs** | `make docs` — opens `{BASE_URL}/docs` |

### Common commands

| Command | Action |
|---|---|
| `make up` / `make down` | start / stop the docker stack |
| `make build` | rebuild the vera image and start |
| `make logs` | follow orchestrator logs |
| `make run` | run the orchestrator natively |
| `make health` / `make caps` | smoke-test a running instance |
| `make nuke` | stop **and delete volumes** (destructive) |
"""


def t_getting_started_md() -> str:
    return f"# Getting Started with Vera\n\n{GETTING_STARTED_BODY}"


def t_welcome_py() -> str:
    return r'''#!/usr/bin/env python3
"""
welcome.py — Vera guided tour & smoke test (stdlib only).

    python welcome/welcome.py            # interactive guided tour
    python welcome/welcome.py --check    # non-interactive smoke test (CI-friendly)
"""
import argparse, json, sys, urllib.request, webbrowser
from urllib.error import URLError

BASE = "http://localhost:__PORT__"
C = {"h": "\033[36m", "g": "\033[32m", "y": "\033[33m", "r": "\033[31m",
     "b": "\033[1m", "x": "\033[0m"}

BANNER = r"""
 __     __        _____  __ _
 \ \   / /__ _ _|  __ \/ _` |
  \ \ / / -_) '_| |_) | (_| |   Distributed Capability Runtime
   \_/  \___|_| |_.__/ \__,_|   guided tour
"""

STOPS = [
    ("What is Vera?",
     "A distributed capability runtime. Everything - LLM calls, memory, DAGs,\n"
     "research, the IDE - is a 'capability': a decorated async function that is\n"
     "simultaneously a REST endpoint, an MCP tool, and a DAG node."),
    ("The orchestrator",
     "A FastAPI app on :__PORT__. It registers caps, mounts their routes,\n"
     "streams events over Redis, and dispatches work to Ollama worker nodes."),
    ("Backends",
     "Redis (events/queues), Postgres (state), ChromaDB (vectors),\n"
     "Neo4j (memory graph). They connect lazily - Vera boots without them."),
    ("Talking to caps",
     "GET  /mcp/tools            list every capability\n"
     "POST /mcp/call             {\"name\":..., \"arguments\":{...}}\n"
     "GET  /health               backends + worker + cap counts\n"
     "GET  /docs                 Swagger UI for every REST route"),
    ("The harness UI",
     f"Open {BASE}/ for the single-page harness: panels for caps, DAGs,\n"
     "memory graph, data fabric, IDE and the live event stream."),
]


def _get(path):
    with urllib.request.urlopen(BASE + path, timeout=4) as r:
        return json.loads(r.read().decode())


def is_up():
    for p in ("/health", "/debug/health"):
        try:
            return _get(p)
        except Exception:
            continue
    return None


def smoke():
    print(f"{C['b']}Vera smoke test -> {BASE}{C['x']}")
    health = is_up()
    if not health:
        print(f"{C['r']}x orchestrator not reachable. Start it with `make up` or `make run`.{C['x']}")
        return 1
    print(f"{C['g']}+ orchestrator up{C['x']}  "
          f"caps={health.get('caps','?')} workers={health.get('workers','?')} "
          f"mode={health.get('mode','?')}")
    for k in ("redis", "postgres", "chroma", "neo4j"):
        ok = health.get(k)
        mark = f"{C['g']}+{C['x']}" if ok else f"{C['y']}-{C['x']}"
        print(f"   {mark} {k}")
    try:
        tools = _get("/mcp/tools")
        items = tools.get("tools") if isinstance(tools, dict) else tools
        names = [t.get("name") for t in items]
        print(f"{C['g']}+ {len(names)} capabilities registered{C['x']}  "
              f"e.g. {', '.join(filter(None, names[:6]))} ...")
    except Exception as e:
        print(f"{C['y']}! could not list caps: {e}{C['x']}")
    return 0


def tour():
    print(C["h"] + BANNER + C["x"])
    health = is_up()
    if health:
        print(f"{C['g']}* connected to a running orchestrator "
              f"({health.get('caps','?')} caps){C['x']}\n")
    else:
        print(f"{C['y']}* no running orchestrator detected - start one with "
              f"`make up` or `make run` to try the live calls{C['x']}\n")
    for i, (title, body) in enumerate(STOPS, 1):
        print(f"{C['b']}[{i}/{len(STOPS)}] {title}{C['x']}")
        print(body + "\n")
        try:
            input(f"{C['h']}  [enter] next ...{C['x']}")
        except (EOFError, KeyboardInterrupt):
            print(); return 0
        print()
    if health:
        print(f"{C['b']}Live: a few capabilities on this instance{C['x']}")
        smoke()
    try:
        if input(f"\n{C['h']}Open the HTML welcome guide in your browser? [y/N] {C['x']}").strip().lower() == "y":
            import pathlib
            webbrowser.open((pathlib.Path(__file__).parent / "index.html").resolve().as_uri())
    except (EOFError, KeyboardInterrupt):
        pass
    print(f"\n{C['g']}Enjoy Vera.{C['x']}  Docs: {BASE}/docs - UI: {BASE}/")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Vera guided tour / smoke test")
    ap.add_argument("--check", action="store_true", help="non-interactive smoke test")
    args = ap.parse_args()
    sys.exit(smoke() if args.check else tour())
'''.replace("__PORT__", str(PORT))


def t_welcome_html() -> str:
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Welcome to Vera</title>
<style>
  :root { --bg:#0c0f14; --panel:#141923; --ink:#e6edf3; --mut:#8b97a7;
          --acc:#44ffaa; --acc2:#3aa0ff; --line:#222b38; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font:15px/1.6 system-ui,Segoe UI,Roboto,sans-serif; }
  header { padding:32px 24px; border-bottom:1px solid var(--line);
           background:linear-gradient(180deg,#10151d,#0c0f14); }
  header h1 { margin:0; font-size:30px; letter-spacing:.5px; }
  header h1 span { color:var(--acc); }
  header p { margin:6px 0 0; color:var(--mut); }
  .wrap { display:grid; grid-template-columns:220px 1fr; gap:0; min-height:60vh; }
  nav { border-right:1px solid var(--line); padding:18px 12px; }
  nav a { display:block; padding:8px 12px; color:var(--mut);
          text-decoration:none; border-radius:8px; }
  nav a:hover, nav a.on { background:var(--panel); color:var(--ink); }
  main { padding:28px 32px; max-width:900px; }
  section { display:none; animation:f .25s ease; }
  section.on { display:block; }
  @keyframes f { from{opacity:0;transform:translateY(6px)} to{opacity:1} }
  h2 { margin-top:0; }
  code, pre { font-family:ui-monospace,Consolas,monospace; }
  pre { background:var(--panel); border:1px solid var(--line); border-radius:10px;
        padding:14px 16px; overflow:auto; }
  .card { background:var(--panel); border:1px solid var(--line);
          border-radius:12px; padding:16px 18px; margin:14px 0; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:12px; }
  .pill { display:inline-block; padding:2px 10px; border-radius:999px;
          background:#16202c; color:var(--acc); font-size:12px; border:1px solid var(--line); }
  button { background:var(--acc); color:#04110b; border:0; border-radius:9px;
           padding:9px 14px; font-weight:600; cursor:pointer; }
  button.ghost { background:transparent; color:var(--ink); border:1px solid var(--line); }
  .status { font-family:ui-monospace,monospace; white-space:pre-wrap;
            color:var(--mut); margin-top:10px; }
  .ok { color:var(--acc);} .bad{ color:#ff6b6b;} .warn{color:#ffcf5c;}
  a.link { color:var(--acc2); }
  footer { color:var(--mut); border-top:1px solid var(--line); padding:18px 32px; }
  .tourbar { display:flex; gap:10px; align-items:center; margin-top:18px; }
</style>
</head>
<body>
<header>
  <h1>Welcome to <span>Vera</span></h1>
  <p>A Distributed Capability Runtime - guided tour &amp; live console.</p>
</header>

<div class="wrap">
  <nav id="nav"></nav>
  <main id="main"></main>
</div>

<footer>
  Base URL: <code id="base"></code> &middot;
  <a class="link" id="ui" href="#">Harness UI</a> &middot;
  <a class="link" id="docs" href="#">API Docs</a>
</footer>

<script>
const PORT = __PORT__;
const BASE = `http://localhost:${PORT}`;
document.getElementById('base').textContent = BASE;
document.getElementById('ui').href = BASE + '/';
document.getElementById('docs').href = BASE + '/docs';

const SECTIONS = [
  { id:'start', title:'Get started', html:`
    <h2>Get started</h2>
    <p>Pick a path. The docker stack is the fastest way to see everything working.</p>
    <div class="grid">
      <div class="card"><span class="pill">Docker</span>
        <pre>cp .env.example .env
make secret   # paste into .env
make up
make logs</pre></div>
      <div class="card"><span class="pill">Native</span>
        <pre>make venv
make run
# python -m __MODULE__</pre></div>
    </div>
    <p>No <code>make</code>? Use <code>./build.sh up</code> (Linux/macOS) or
       <code>.\\build.ps1 up</code> (Windows).</p>` },

  { id:'concepts', title:'Core concepts', html:`
    <h2>Everything is a capability</h2>
    <p>A capability is one decorated async function that becomes, at once:</p>
    <div class="grid">
      <div class="card">a <b>REST</b> endpoint (auto OpenAPI)</div>
      <div class="card">an <b>MCP</b> tool (<code>/mcp/tools</code>, <code>/mcp/call</code>)</div>
      <div class="card">a <b>DAG</b> node</div>
      <div class="card">an <b>event</b> emitter (Redis stream)</div>
    </div>
    <pre>@capability("math.add", http_method="POST", http_path="/math/add")
async def cap_add(a: float, b: float, trace_id=None):
    return {"sum": a + b}</pre>` },

  { id:'arch', title:'Architecture', html:`
    <h2>The moving parts</h2>
    <div class="grid">
      <div class="card"><b>Orchestrator</b><br>FastAPI on :${PORT}. Registry, routing, events.</div>
      <div class="card"><b>Redis</b><br>Event streams, task queues, cache.</div>
      <div class="card"><b>Postgres</b><br>Durable state.</div>
      <div class="card"><b>ChromaDB</b><br>Vector embeddings.</div>
      <div class="card"><b>Neo4j</b><br>Memory graph.</div>
      <div class="card"><b>Ollama nodes</b><br>Agnostic LLM workers (GPU/CPU).</div>
    </div>` },

  { id:'console', title:'Live console', html:`
    <h2>Live console</h2>
    <p>These call the running orchestrator from your browser.</p>
    <div class="tourbar">
      <button onclick="ping()">Check /health</button>
      <button class="ghost" onclick="listCaps()">List capabilities</button>
      <button class="ghost" onclick="doEcho()">Call echo</button>
    </div>
    <div class="status" id="out">Idle.</div>` },

  { id:'next', title:'Where next', html:`
    <h2>Where to go next</h2>
    <ul>
      <li>Open the <a class="link" href="${BASE}/" target="_blank">harness UI</a>.</li>
      <li>Browse every route in the <a class="link" href="${BASE}/docs" target="_blank">API docs</a>.</li>
      <li>Run the quickstart notebook: <code>make notebook</code>.</li>
      <li>Read <code>docs/GETTING_STARTED.md</code> and the <code>documentation/</code> folder.</li>
      <li>Write your first capability - see <code>documentation/01-capability-framework.md</code>.</li>
    </ul>` },
];

const nav = document.getElementById('nav');
const main = document.getElementById('main');
SECTIONS.forEach((s, i) => {
  const a = document.createElement('a'); a.textContent = `${i+1}. ${s.title}`;
  a.href = '#' + s.id; a.dataset.id = s.id; a.onclick = () => show(s.id);
  nav.appendChild(a);
  const sec = document.createElement('section'); sec.id = 'sec-' + s.id;
  sec.innerHTML = s.html + tourBar(i); main.appendChild(sec);
});
function tourBar(i){
  const prev = i>0 ? `<button class="ghost" onclick="show('${SECTIONS[i-1].id}')">Back</button>`:'';
  const next = i<SECTIONS.length-1 ? `<button onclick="show('${SECTIONS[i+1].id}')">Next</button>`:'';
  return `<div class="tourbar">${prev}${next}</div>`;
}
function show(id){
  SECTIONS.forEach(s=>{
    document.getElementById('sec-'+s.id).classList.toggle('on', s.id===id);
    [...nav.children].forEach(a=>a.classList.toggle('on', a.dataset.id===id));
  });
  window.scrollTo({top:0,behavior:'smooth'});
}
show('start');

const out = () => document.getElementById('out');
async function ping(){
  out().textContent = 'GET /health ...';
  try {
    const r = await fetch(BASE + '/health'); const j = await r.json();
    out().innerHTML = `<span class="ok">+ orchestrator up</span>  caps=${j.caps} `
      + `workers=${j.workers} mode=${j.mode}\n`
      + ['redis','postgres','chroma','neo4j'].map(k=>
          `${j[k]?'<span class=ok>+</span>':'<span class=warn>-</span>'} ${k}`).join('   ');
  } catch(e){ out().innerHTML = `<span class="bad">x not reachable at ${BASE}</span> - start it with make up / make run`; }
}
async function listCaps(){
  out().textContent = 'GET /mcp/tools ...';
  try {
    const r = await fetch(BASE + '/mcp/tools'); const j = await r.json();
    const tools = Array.isArray(j) ? j : (j.tools||[]);
    out().innerHTML = `<span class="ok">+ ${tools.length} capabilities</span>\n`
      + tools.slice(0,40).map(t=>'- '+(t.name||t)).join('\n');
  } catch(e){ out().innerHTML = `<span class="bad">x ${e}</span>`; }
}
async function doEcho(){
  out().textContent = 'POST /mcp/call echo ...';
  try {
    const r = await fetch(BASE + '/mcp/call', {method:'POST',
      headers:{'content-type':'application/json'},
      body: JSON.stringify({name:'echo', arguments:{message:'Hello from the welcome guide'}})});
    out().textContent = JSON.stringify(await r.json(), null, 2);
  } catch(e){ out().innerHTML = `<span class="bad">x ${e}</span>`; }
}
</script>
</body>
</html>
""".replace("__PORT__", str(PORT)).replace("__MODULE__", MODULE)


# ── Jupyter notebook (built programmatically) ──────────────────────────────────
def _md(*lines):
    return {"cell_type": "markdown", "metadata": {}, "source": _src(lines)}


def _code(*lines):
    return {"cell_type": "code", "execution_count": None, "metadata": {},
            "outputs": [], "source": _src(lines)}


def _src(lines):
    """Notebook source as a list of strings, each (except the last) ending in \\n."""
    text = "\n".join(lines)
    parts = text.split("\n")
    return [p + "\n" for p in parts[:-1]] + [parts[-1]]


def t_notebook() -> str:
    demo_lines = []
    for n, a in DEMO_CAPS:
        kwargs = "" if not a else ", " + ", ".join(f"{k}={v!r}" for k, v in a.items())
        demo_lines.append(f"cap({n!r}{kwargs})")
    cells = [
        _md("# Vera — Quickstart Notebook",
            "",
            "Boot the system, confirm it's healthy, list capabilities, and call a few.",
            "",
            f"The orchestrator is expected at **{BASE_URL}**. Start it first with",
            "`make up` (docker) or `make run` (native)."),
        _md("## 1. Configure"),
        _code(f'BASE = "{BASE_URL}"',
              "import json, time, urllib.request",
              "",
              "def call(method, path, body=None):",
              "    data = json.dumps(body).encode() if body is not None else None",
              "    req = urllib.request.Request(BASE + path, data=data, method=method,",
              "                                 headers={'content-type': 'application/json'})",
              "    with urllib.request.urlopen(req, timeout=30) as r:",
              "        return json.loads(r.read().decode())",
              "",
              "def GET(p):        return call('GET', p)",
              "def POST(p, body): return call('POST', p, body)"),
        _md("## 2. Is it up? (optionally start it)",
            "",
            "If you have docker and the orchestrator isn't running, uncomment the",
            "`docker compose up -d` line to start the whole stack from the notebook."),
        _code("# import subprocess; subprocess.run(['docker','compose','up','-d'])",
              "",
              "def wait_until_up(timeout=120):",
              "    start = time.time()",
              "    while time.time() - start < timeout:",
              "        try:",
              "            return GET('/health')",
              "        except Exception as e:",
              "            print('waiting for orchestrator ...', e); time.sleep(3)",
              "    raise RuntimeError('orchestrator did not come up in time')",
              "",
              "health = wait_until_up()",
              "health"),
        _md("## 3. Backend health at a glance"),
        _code("for k in ('redis','postgres','chroma','neo4j'):",
              "    print(f\"{'OK ' if health.get(k) else '-- '} {k}\")",
              "print('caps:', health.get('caps'), '| workers:', health.get('workers'),",
              "      '| mode:', health.get('mode'))"),
        _md("## 4. List capabilities",
            "",
            "Every capability is an MCP tool. `/mcp/tools` returns them all."),
        _code("tools = GET('/mcp/tools')",
              "tools = tools['tools'] if isinstance(tools, dict) and 'tools' in tools else tools",
              "names = [t.get('name') for t in tools]",
              "print(len(names), 'capabilities')",
              "names[:40]"),
        _md("## 5. Call capabilities",
            "",
            "Invoke any cap via `POST /mcp/call` with `{name, arguments}`."),
        _code("def cap(name, **arguments):",
              "    return POST('/mcp/call', {'name': name, 'arguments': arguments})",
              "",
              "# Demo calls (safe, no side effects):",
              *demo_lines),
        _md("## 6. Loaded modules",
            "",
            "See which capability modules loaded and how many caps each contributed."),
        _code("mods = GET('/modules')",
              "for m in (mods if isinstance(mods, list) else mods.get('modules', [])):",
              "    print(f\"{m.get('status','?'):>6}  {m.get('name'):<28} +{m.get('caps_added',0)}\")"),
        _md("## 7. Watch the event stream",
            "",
            "Recent events emitted by capability calls (Redis-backed)."),
        _code("try:",
              "    print(json.dumps(GET('/events'), indent=2)[:2000])",
              "except Exception as e:",
              "    print('events endpoint unavailable:', e)"),
        _md("---",
            f"**Next:** open the harness UI at `{BASE_URL}/`, the API docs at "
            f"`{BASE_URL}/docs`, or run the guided tour with `python welcome/welcome.py`."),
    ]
    nb = {
        "cells": cells,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python", "version": "3.11"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    return json.dumps(nb, indent=1)


# ═══════════════════════════════════════════════════════════════════════════════
#  Generator registry
# ═══════════════════════════════════════════════════════════════════════════════
# key -> (relative path, builder, executable?, skip-if-exists-by-default?)
GENERATORS = {
    "env":      (".env.example",                 t_env_example,       False, True),
    "make":     ("Makefile",                     t_makefile,          False, True),
    "sh":       ("build.sh",                     t_build_sh,          True,  True),
    "ps1":      ("build.ps1",                    t_build_ps1,         False, True),
    "ansible":  ("ansible/playbook.yml",         t_ansible_playbook,  False, True),
    "inventory":("ansible/inventory.ini.example",t_ansible_inventory, False, True),
    "compose":  ("docker-compose.yml",           t_docker_compose,    False, True),
    "gs":       ("docs/GETTING_STARTED.md",      t_getting_started_md,False, True),
    "welcome":  ("welcome/welcome.py",           t_welcome_py,        True,  True),
    "html":     ("welcome/index.html",           t_welcome_html,      False, True),
    "notebook": ("notebooks/vera_quickstart.ipynb", t_notebook,       False, True),
}

README_BEGIN = "<!-- BEGIN GETTING STARTED (managed by tools/scaffold.py) -->"
README_END   = "<!-- END GETTING STARTED -->"


def inject_readme(root: Path, force: bool, dry: bool) -> str:
    """Insert/refresh a managed Getting Started block in README.MD."""
    readme = root / "README.MD"
    if not readme.exists():
        readme = root / "README.md"
    block = f"{README_BEGIN}\n\n## Getting Started\n\n{GETTING_STARTED_BODY}\n\n{README_END}"
    if not readme.exists():
        if not dry:
            (root / "README.MD").write_text(f"# Vera\n\n{block}\n", encoding="utf-8")
        return "created README.MD"
    text = readme.read_text(encoding="utf-8")
    if README_BEGIN in text and README_END in text:
        pre = text[: text.index(README_BEGIN)]
        post = text[text.index(README_END) + len(README_END):]
        new = pre + block + post
        action = "refreshed managed block"
    else:
        new = text.rstrip() + "\n\n---\n\n" + block + "\n"
        action = "appended managed block"
    if not dry:
        readme.write_text(new, encoding="utf-8")
    return action


def write(path: Path, content: str, executable: bool, force: bool,
          skip_default: bool, dry: bool) -> str:
    if path.exists() and skip_default and not force:
        return "skip (exists)"
    if dry:
        return "would write"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    if executable:
        try:
            os.chmod(path, 0o755)
        except OSError:
            pass
    return "written"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Generate Vera onboarding & deploy artifacts.")
    ap.add_argument("--force", action="store_true", help="overwrite existing files")
    ap.add_argument("--only", default="", help="comma list of keys (see --list)")
    ap.add_argument("--list", action="store_true", help="list generators and exit")
    ap.add_argument("--dry-run", action="store_true", help="show actions without writing")
    ap.add_argument("--root", default=".", help="repo root (contains the vera/ package)")
    ap.add_argument("--no-readme", action="store_true", help="don't touch README")
    args = ap.parse_args(argv)

    if args.list:
        print("Available generators:")
        for k, (rel, *_rest) in GENERATORS.items():
            print(f"  {k:<10} -> {rel}")
        print("  readme     -> README.MD (managed Getting Started block)")
        return 0

    root = Path(args.root).resolve()
    if not (root / "vera").is_dir():
        print(f"warning: {root} does not look like the Vera repo root "
              f"(no vera/ dir). Continuing anyway.", file=sys.stderr)

    only = {k.strip() for k in args.only.split(",") if k.strip()} if args.only else None

    print(f"Vera scaffold -> {root}\n")
    for key, (rel, builder, ex, skip) in GENERATORS.items():
        if only and key not in only:
            continue
        status = write(root / rel, builder(), ex, args.force, skip, args.dry_run)
        print(f"  {status:<14} {rel}")

    if not args.no_readme and (not only or "readme" in only):
        status = inject_readme(root, args.force, args.dry_run)
        print(f"  {status:<14} README.MD")

    print("\nDone. Next:")
    print("  make up        # or ./build.sh up  /  .\\build.ps1 up")
    print("  make tour      # guided tour")
    print("  make notebook  # quickstart notebook")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
