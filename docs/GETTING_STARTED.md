# Getting Started with Vera

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
.\build.ps1 up
```

Then open the harness at **http://localhost:8999/** and the API docs at **http://localhost:8999/docs**.

### Option B — Native (no docker)

You supply Redis / Postgres / Chroma / Neo4j (or let caps degrade gracefully —
core capability registration and HTTP come up even without backends).

```bash
make venv                     # create .venv + install requirements
make run                      # python -m Vera.vera.capability_orchestration
```

`make run` sets `PYTHONPATH` to the repo's parent so the `Vera.vera.capability_orchestration`
package resolves, and binds `0.0.0.0:8999`.

### Verify it's alive

```bash
make health                   # GET /health
make caps                     # GET /mcp/tools  (list every capability)
```

Or invoke a capability directly:

```bash
curl -s http://localhost:8999/mcp/call \
  -H 'content-type: application/json' \
  -d '{"name":"echo","arguments":{"message":"hello"}}'
```

### Explore interactively

| What | How |
|---|---|
| **Guided terminal tour** | `make tour` (or `python welcome/welcome.py`) |
| **HTML welcome guide** | `make welcome` — opens `welcome/index.html` |
| **Quickstart notebook** | `make notebook` — boot & test caps from Jupyter |
| **Swagger API docs** | `make docs` — opens `http://localhost:8999/docs` |

### Common commands

| Command | Action |
|---|---|
| `make up` / `make down` | start / stop the docker stack |
| `make build` | rebuild the vera image and start |
| `make logs` | follow orchestrator logs |
| `make run` | run the orchestrator natively |
| `make health` / `make caps` | smoke-test a running instance |
| `make nuke` | stop **and delete volumes** (destructive) |
