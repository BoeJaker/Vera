# Getting started with Vera

This guide gets a first Vera instance running, verifies the important surfaces,
and points out which services are optional.

## 1. Check the fit

For a lightweight evaluation, use a machine with 4 modern CPU cores, 8–16 GB
RAM, and 20 GB free disk, and route model calls to a hosted provider or separate
Ollama node. Running local models and all databases on the same host generally
needs substantially more.

Read [Performance and sizing](../documentation/00-performance-and-sizing.md)
before downloading models or planning a persistent deployment.

You also need:

- Git;
- Docker with Compose for the recommended path;
- a terminal that can run `make`, `build.sh`, or `build.ps1`; and
- free ports for the services enabled by your Compose configuration.

## 2. Clone and configure

```bash
git clone https://github.com/BoeJaker/Vera.git
cd Vera
cp .env.example .env
```

Review `.env` before starting. Generate Vera's secret key with:

```bash
make secret
```

Do not commit `.env`, access tokens, provider keys, or generated secrets.

## 3. Start the Docker stack

```bash
make up
make logs
```

Without `make`:

```bash
# Linux / macOS
./build.sh up

# Windows PowerShell
.\build.ps1 up
```

The first build can take time because images and browser/model dependencies may
need to download.

## 4. Verify the runtime

Open:

- Harness: <http://localhost:8999/>
- OpenAPI: <http://localhost:8999/docs>
- Health: <http://localhost:8999/health>
- MCP tools: <http://localhost:8999/mcp/tools>

From the repository:

```bash
make health
make caps
```

Or invoke a capability directly:

```bash
curl -s http://localhost:8999/mcp/call \
  -H 'content-type: application/json' \
  -d '{"name":"echo","arguments":{"message":"hello"}}'
```

A healthy response proves the registry and HTTP/MCP dispatch path are working.
Optional databases may still be connecting in the background.

## 5. Connect models

Vera can route to Ollama, vLLM, or configured hosted providers. Model workers may
run on the orchestrator host or elsewhere.

After configuration, verify:

```bash
curl -s http://localhost:8999/mcp/call \
  -H 'content-type: application/json' \
  -d '{"name":"ollama.instances","arguments":{}}'
```

Do not assume a model fits from parameter count alone. Quantization, context,
batching, and concurrent requests all add memory.

## Native development

For an orchestrator process with externally managed backends:

```bash
make venv
make run
```

`make run` sets the package path and starts
`Vera.vera.capability_orchestration` on `0.0.0.0:8999`.

Core capability registration can start while optional backends are unavailable,
but capabilities that depend on those services will be degraded.

## Useful commands

| Command | Action |
|---|---|
| `make up` | Build/start the configured Docker stack |
| `make down` | Stop the stack without deleting persistent volumes |
| `make build` | Rebuild Vera and start it |
| `make logs` | Follow orchestrator logs |
| `make run` | Run the orchestrator natively |
| `make health` | Check runtime health |
| `make caps` | List registered capabilities |
| `make tour` | Run the guided terminal tour |
| `make notebook` | Open the quick-start notebook |
| `make nuke` | **Destructive:** stop the stack and delete volumes |

## First troubleshooting checks

1. Run `make logs` and inspect the first startup error.
2. Check `/health` to distinguish a core failure from an optional backend.
3. Confirm the configured backend hostnames resolve inside the Vera container.
4. Check disk space before rebuilding or downloading models.
5. Run `perf.scan` if the UI connects but feels slow or WebSockets flap.

Continue with the [documentation hub](../documentation/README.md).
