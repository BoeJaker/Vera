# Getting started with Vera

This guide gets a first Vera instance running, verifies the important surfaces,
makes a first capability call, and shows which services are optional. It covers
the recommended Docker stack and a native (no-Docker orchestrator) run for
development. Every command here was checked against the repository's
`Makefile`, `build.sh`, `docker-compose.yml`, `Dockerfile`, and `.env.example`.

## Contents

- [1. Check the fit](#1-check-the-fit)
- [2. Clone and configure](#2-clone-and-configure)
- [3. Start the Docker stack](#3-start-the-docker-stack)
- [4. Verify the runtime](#4-verify-the-runtime)
- [5. Make your first capability call](#5-make-your-first-capability-call)
- [6. Connect models](#6-connect-models)
- [7. Add your own capability](#7-add-your-own-capability)
- [Native development](#native-development)
  - [Run the orchestrator natively](#run-the-orchestrator-natively)
  - [Backends for a native run](#backends-for-a-native-run)
- [Services and ports](#services-and-ports)
- [Configuration essentials](#configuration-essentials)
- [Useful commands](#useful-commands)
- [First troubleshooting checks](#first-troubleshooting-checks)
- [Where to go next](#where-to-go-next)

## 1. Check the fit

For a lightweight evaluation, use a machine with 4 modern CPU cores, 8–16 GB
RAM, and 20 GB free disk, and route model calls to a hosted provider or a
separate Ollama node. Running local models and all databases on the same host
generally needs substantially more.

Read [Performance and sizing](../documentation/00-performance-and-sizing.md)
before downloading models or planning a persistent deployment.

You also need:

- Git;
- Docker with Compose (`docker compose` or `docker-compose`) for the
  recommended path;
- Python 3.11 on the host for `make secret`, `make health`/`make caps`, and
  native runs;
- a terminal that can run `make`, `build.sh`, or `build.ps1`; and
- free host ports for the services in [Services and ports](#services-and-ports).

## 2. Clone and configure

```bash
git clone https://github.com/BoeJaker/Vera.git
cd Vera
cp .env.example .env
```

Every value in `.env.example` is commented out and shows the default. Review it
before starting.

Generate Vera's master key and add it to `.env`:

```bash
make secret                        # needs: pip install cryptography
# copy the printed key into .env:
# VERA_SECRET_KEY=<printed key>
```

`VERA_SECRET_KEY` encrypts stored credentials (calendar, accounts, and other
sealed secrets). If it is unset, Vera generates a key file at
`~/.vera/secret.key` — inside a container that file is lost on rebuild, making
secrets already stored in the persisted Redis volume undecryptable.

Do not commit `.env`, access tokens, provider keys, or generated secrets.

> [!IMPORTANT]
> Docker Compose uses `.env` for variable substitution in `docker-compose.yml`.
> Only variables referenced in the `vera` service's `environment:` block reach
> the orchestrator container, and `.env` itself is excluded from the image by
> `.dockerignore`. To pass any other setting (for example `VERA_POLICY_MODE` or
> `VERA_MODULES`) into the container, add it to that block.

## 3. Start the Docker stack

```bash
make up        # docker compose up -d
make logs      # docker compose logs -f vera
```

Without `make`:

```bash
# Linux / macOS
./build.sh up
./build.sh logs

# Windows PowerShell
.\build.ps1 up
```

`make up` builds the `vera:latest` image on first run (the `vera` service
declares both `build:` and `image: vera:latest`). Use `make build` to force a
rebuild after changing the source. The first build can take time: Python
dependencies, Playwright Chromium for Operator, and the backing service images
all download.

The `vera` container waits for Redis, Postgres, and Neo4j health checks and for
the one-shot `garage-init` bootstrap to finish before starting.

## 4. Verify the runtime

Open:

- Harness: <http://localhost:8999/>
- OpenAPI (Swagger): <http://localhost:8999/docs>
- Health: <http://localhost:8999/health>
- MCP tool catalog: <http://localhost:8999/mcp/tools>

From the repository:

```bash
make health    # GET /health, pretty-printed
make caps      # GET /mcp/tools, pretty-printed
```

`/health` returns one entry per backend and node, for example:

```json
{"redis": true, "postgres": true, "chroma": true, "neo4j": true,
 "workers": 0, "caps": 2687, "mcp_servers": 0,
 "ollama": {"gpu-250": {"status": "offline", "latency_ms": null, "has_gpu": true}},
 "mode": "local"}
```

Each backend flag is set by actually probing the service, not by the presence of
a connection object. A backend reporting `false` only degrades the capabilities
that need it; `caps` shows how many capabilities registered.

> [!NOTE]
> With `TLS_ENABLED=1` the same port serves `https://` with a self-signed
> certificate generated on first start. The `make` targets detect this
> automatically; for `curl`, use `https://` and `-k`.

## 5. Make your first capability call

Every capability is callable through `POST /mcp/call` with a `name` and an
`arguments` object:

```bash
curl -s http://localhost:8999/mcp/call \
  -H 'content-type: application/json' \
  -d '{"name":"echo","arguments":{"message":"hello"}}'
```

```json
{"type":"tool_result","tool_name":"echo","trace_id":"…",
 "content":{"echo":"hello","ts":"…","trace_id":"…"}}
```

Capabilities that declare an HTTP route are also reachable directly; `echo` is
mounted at `POST /debug/echo`:

```bash
curl -s -X POST http://localhost:8999/debug/echo \
  -H 'content-type: application/json' -d '{"message":"hi"}'
```

A few more read-only calls that work with no model attached:

```bash
# How much of the registry declares a Capability Contract
curl -s 'http://localhost:8999/cap/contracts/coverage?limit=5'

# Validate the frozen evaluation corpus
curl -s http://localhost:8999/eval/corpus

# Current capability-policy enforcement mode
curl -s http://localhost:8999/cap/policy/enforcement
```

A successful `echo` proves the registry and HTTP/MCP dispatch path are working.
Optional databases may still be connecting in the background.

## 6. Connect models

Vera routes to Ollama, vLLM, or configured hosted providers. Model workers may
run on the orchestrator host or elsewhere; the Docker stack does **not** include
an Ollama container.

The Ollama endpoints default to example LAN addresses. Set these in `.env` to
your own nodes, then `make up` again:

| Variable | Purpose |
|---|---|
| `OLLAMA_GPU_URL` | GPU generation node |
| `OLLAMA_CPU_A_URL`, `OLLAMA_CPU_B_URL` | CPU generation nodes |
| `OLLAMA_MODEL` | Default generation model |
| `OLLAMA_EMBED_URL`, `OLLAMA_EMBED_MODEL` | Embedding node and model (default model `nomic-embed-text`) |
| `GPU_INFER_URL` | Optional Whisper / TTS / Stable Diffusion server |

Verify:

```bash
curl -s http://localhost:8999/mcp/call \
  -H 'content-type: application/json' \
  -d '{"name":"ollama.instances","arguments":{}}'
```

Each node should report `"status": "online"`. Do not assume a model fits from
parameter count alone: quantization, context, batching, and concurrent requests
all add memory. See [LLM cluster](../documentation/04-ollama-cluster.md) and
[vLLM](../documentation/21-vllm.md).

## 7. Add your own capability

Create a Python file anywhere on the host, decorate a function, and tell Vera to
load it with `VERA_MODULES` (comma-separated paths to `.py` files):

```python
# my_caps.py
from Vera.vera.capability_orchestration import capability


@capability("example.greet", description="Return a friendly greeting.",
            http_method="POST", http_path="/example/greet", http_tags=["example"])
async def greet(name: str, trace_id=None):
    return {"message": f"Hello, {name}!"}
```

```bash
# native run
VERA_MODULES=/abs/path/my_caps.py make run
```

The startup log shows `✓ my_caps  caps=…` when it loads (or `✗ my_caps failed to
load` with a traceback). Then:

```bash
curl -s http://localhost:8999/mcp/call -H 'content-type: application/json' \
  -d '{"name":"example.greet","arguments":{"name":"Vera"}}'
```

For a Docker run, the module must exist inside the container (for example under
the repository, which is copied to `/app/Vera/`) and `VERA_MODULES` must be added
to the `vera` service environment. See the
[Capability framework](../documentation/01-capability-framework.md) and
[Capability contracts](../documentation/43-capability-contracts.md) for the full
decorator and contract options.

## Native development

### Run the orchestrator natively

```bash
make venv                     # creates .venv and installs requirements.txt
source .venv/bin/activate     # so `make run` uses the venv's python3
make run
```

`make run` changes to the **parent** of the repository, sets `PYTHONPATH` to it,
and runs `python3 -m Vera.vera.capability_orchestration`, which listens on
`0.0.0.0:8999` (`ORCHESTRATOR_HOST` / `ORCHESTRATOR_PORT`). Because imports are
`Vera.vera.…`, the repository directory must be named `Vera`. A native run reads
the repository-root `.env` itself; values already in the process environment
win.

Equivalent without `make`: `./build.sh venv` then `./build.sh run`.

> [!TIP]
> Core capability registration starts even when no backend is reachable. A
> native run with no Redis, Postgres, Chroma, Neo4j, or Ollama still serves the
> harness, `/health`, `/mcp/tools`, `echo`, and the contract, policy, and
> evaluation inspection capabilities; capabilities that depend on missing
> services are degraded.

### Backends for a native run

The native defaults point at `localhost` on the same host ports the Compose file
publishes, so you can run just the backing services in Docker:

```bash
docker compose up -d redis postgres chromadb neo4j
```

| Backend | Native default | Notes |
|---|---|---|
| Redis | `REDIS_URL=redis://localhost:6379` | Events, queues, caching |
| Postgres | `POSTGRES_URL=postgresql://admin:admin@localhost:5433/postgres` | Compose publishes 5432 as 5433 |
| ChromaDB | `CHROMA_HOST=localhost`, `CHROMA_PORT=8008` | Vector store |
| Neo4j | `NEO4J_URI=bolt://localhost:7687`, `NEO4J_USER=neo4j` | Native default password is `neo4j`; the Compose service uses `veraneo4j`, so set `NEO4J_PASS=veraneo4j` in `.env` |
| Object store | `FABRIC_OBJECT_STORE=none` | Native default disables blob storage; Docker defaults to Garage |

## Services and ports

Host ports are configurable in `.env` (left side of each mapping):

| Service | Container | Host port variable | Default |
|---|---|---|---|
| Orchestrator (`vera`) | `vera-orchestrator` | `ORCHESTRATOR_PORT` | 8999 |
| Redis | `redis` | `REDIS_PORT` | 6379 |
| Postgres | `postgres` | `POSTGRES_PORT` | 5433 |
| ChromaDB | `chromadb` | `CHROMA_PORT` | 8008 |
| Neo4j HTTP / Bolt | `neo4j` | `NEO4J_HTTP_PORT` / `NEO4J_BOLT_PORT` | 7474 / 7687 |
| Garage S3 / admin | `garage` | `GARAGE_S3_PORT` / `GARAGE_ADMIN_PORT` | 3900 / 3903 |
| code-server IDE | `vscode` | `VSCODE_PORT` | 8843 |
| Build service | `vera-builder` | `BUILDER_PORT` | 8785 |

## Configuration essentials

| Variable | Default | Why you might change it |
|---|---|---|
| `VERA_SECRET_KEY` | *(generated, ephemeral in Docker)* | Always set for persistent deployments |
| `TLS_ENABLED` | `0` | Set `1` for HTTPS (Web Serial and webcam need a secure context off-localhost) |
| `TLS_EXTRA_SANS` | *(empty)* | Extra names/IPs for the self-signed certificate |
| `BACKEND_HOST` | `llm.int` | Internal domain used to build several default URLs and injected into the UI |
| `NEO4J_USER`, `NEO4J_PASS` | `neo4j` / `veraneo4j` (Compose) | Change credentials |
| `FABRIC_S3_ACCESS`, `FABRIC_S3_SECRET` | deterministic dev values | Replace with real credentials (generation commands are in `.env.example`) |
| `GARAGE_ADMIN_TOKEN` | dev placeholder | Must match `garage/garage.toml` |
| `VSCODE_PASSWORD` | `vera-code` | code-server login |
| `VERA_MODULES` | *(empty)* | Load extra capability modules |

The complete reference is [Configuration](../documentation/10-configuration.md).

## Useful commands

| Command | Action |
|---|---|
| `make help` | List every target |
| `make up` | Start the configured Docker stack (builds on first run) |
| `make build` | Rebuild the Vera image and start |
| `make down` | Stop the stack without deleting persistent volumes |
| `make ps` | Show stack status |
| `make logs` | Follow orchestrator logs |
| `make venv` | Create `.venv` and install requirements |
| `make run` | Run the orchestrator natively |
| `make secret` | Print a fresh `VERA_SECRET_KEY` |
| `make health` | Check runtime health |
| `make caps` | List registered capabilities |
| `make test` | Smoke-test the running orchestrator (`welcome/welcome.py --check`) |
| `make test-unit` / `make test-critical` | Run the pytest suite / the critical tier (needs `requirements-dev.txt`) |
| `make tour` | Run the guided terminal tour |
| `make welcome` | Open the HTML welcome guide |
| `make notebook` | Open the quick-start notebook (needs Jupyter) |
| `make docs` | Open the Swagger UI |
| `make install-hooks` / `make scan` | Enable repository git hooks / scan tracked files for secrets |
| `make nuke` | **Destructive:** stop the stack and delete volumes |

## First troubleshooting checks

1. Run `make logs` and read the first startup error. Module load failures are
   logged as `✗ <module> failed to load` with a traceback; the rest of the
   registry still starts.
2. Check `/health` to distinguish a core failure from an optional backend
   (`false` entries) or offline model nodes.
3. Confirm the configured backend hostnames resolve inside the Vera container
   (Compose uses service names such as `redis`, `postgres`, `neo4j`).
4. If a setting in `.env` seems ignored under Docker, check that it is listed in
   the `vera` service's `environment:` block.
5. `make run` fails with `No module named 'Vera'`: the clone directory must be
   named `Vera`, and you must run from the repository via `make run` (which sets
   `PYTHONPATH` to the parent directory).
6. Check disk space before rebuilding or downloading models.
7. Run `perf.scan` (through `/mcp/call` or the Performance Monitor) if the UI
   connects but feels slow or WebSockets flap.

## Where to go next

- [Project overview](../README.MD) — what Vera is and the full documentation map.
- [Documentation hub](../documentation/README.md) — every subsystem guide.
- [Capability framework](../documentation/01-capability-framework.md) and
  [Harness UI](../documentation/02-harness-ui.md).
- [DAG and loop engine](../documentation/03-dag-engine.md) for workflows and agents.
- [Loop Lab](../documentation/33-evolve.md) for changing Vera itself safely.
