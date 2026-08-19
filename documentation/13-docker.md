# 13 · Docker Subsystem

`workers/docker_capabilities.py` gives Vera a real, **server-side** Docker backend. The UI talks to the Docker Engine *through* Vera (no browser→daemon CORS or unix-socket problems), an agent can drive containers as sandboxed execution environments, and — most importantly — Vera can spin up **Docker workers**: containers running the Vera image that join the cluster and consume the task stream.

---

## 1. Connection model

Like the rest of Vera (the SSH host registry, the IDE), a Docker host can be reached three ways:

| Mode | How |
|---|---|
| `local` | The orchestrator's own Docker — unix socket (`DOCKER_SOCK`, default `/var/run/docker.sock`) or `$DOCKER_HOST` |
| `tcp` | A remote daemon over the Engine HTTP API (`http://host:2375`, `tcp://…`) |
| `ssh` | A daemon reached over a **stored SSH host** — `curl`-over-ssh for the Engine API, `docker -H ssh://user@host` for CLI actions |

The `ssh` mode reuses the same SSH host registry as [`exec.ssh.*`](./12-execution.md), so a host configured once is usable for both shell and Docker.

---

## 2. Host registry — `docker.hosts.*`

| Cap | Purpose |
|---|---|
| `docker.hosts.list` | List configured Docker hosts |
| `docker.hosts.save` | Add/replace a host (id, mode, url/ssh-ref) |
| `docker.hosts.delete` | Remove a host |

Hosts persist to `~/.vera_docker_hosts.json` (override with `VERA_DOCKER_HOSTS`).

---

## 3. Monitoring

The Workers panel's Docker pane reads the daemon through a **reverse proxy** mounted on Vera's own port, so the browser never talks to the daemon directly:

```
GET|POST|DELETE /workers/docker/engine/{host_id}/{api_path}
        ↓ proxied (auth + connection mode handled server-side)
   the host's Docker Engine API
```

Plus three convenience caps:

| Cap | Purpose |
|---|---|
| `docker.ping` | Is the daemon reachable? + version |
| `docker.ps` | List containers |
| `docker.images` | List images |

---

## 4. Container lifecycle

These shell out to the Docker CLI and are **gated by the exec sandbox** (`exec_capabilities._sandbox_check`) — the same `<vera-sandbox-controls>` policy that governs Exec and IDE Run. Streaming actions return SSE:

| Cap | Streaming | Purpose |
|---|---|---|
| `docker.build` | SSE | Build an image, streaming build output |
| `docker.run` | SSE | Run a container, streaming logs |
| `docker.logs` | SSE | Follow a container's logs |
| `docker.exec` | — | Exec a command in a running container |
| `docker.stop` | — | Stop a container |
| `docker.rm` | — | Remove a container |

---

## 5. Docker workers — running Vera

This is the payoff. Every Vera process is also a worker (see `capability_orchestration.worker_loop` and the [Capability Framework](./01-capability-framework.md#6-distributed-dispatch)), so a Vera container that shares the cluster's `REDIS_URL` automatically registers in `WORKER_REGISTRY` and starts draining `vera:tasks`.

| Cap | Purpose |
|---|---|
| `docker.worker.spawn` | `docker run -d` the Vera image, wired to the shared `REDIS_URL` etc.; it self-registers and joins the cluster |
| `docker.worker.list` | List Vera worker containers |
| `docker.worker.stop` | Stop a worker container |
| `docker.worker.logs` | Follow a worker's logs |
| `docker.image.ensure` | Guarantee a **local-only** image exists on a host — build from this repo's Dockerfile or `save`/`load`-transfer from the local daemon, never a registry pull |

`docker.worker.spawn` uses `VERA_WORKER_IMAGE` (default `vera:latest`). The Vera
image is **not on Docker Hub** — a bare `docker run vera:latest` on a fresh host
fails with *pull access denied*. Spawn therefore calls `docker.image.ensure`
first (`ensure_image=true` by default): if the host already has the image it's a
no-op; if the local daemon has it and the host is remote, the image is streamed
over with `docker save | docker load`; otherwise it is **built from the repo's
Dockerfile** — `docker -H <host> build` ships the local build context, so this
works for ssh/tcp hosts too. `docker compose build` tags the same `vera:latest`
(the compose service declares `image: vera:latest`). This means you can scale
capability throughput by spawning workers onto any registered Docker host —
local, a TCP daemon, or over SSH — straight from the harness.

### Provisioning the backing stores

The compose stack's backing stores (redis, postgres, chromadb, neo4j and the
garage blob store) can be provisioned onto any registered Docker host with the
`provision.store.*` group (see `vera/provisioning/stores_capabilities.py` and
the **Provision → Docker** pane):

| Cap | Purpose |
|---|---|
| `provision.stores` | Catalog (image, ports, volumes per store) |
| `provision.store.deploy` | Run one store — or `all` — as `vera-<store>` with named data volumes; garage gets a generated config + automatic admin-API bootstrap |
| `provision.store.status` | Container state + reachability probe per store |
| `provision.store.remove` | Remove the container (volumes kept unless `purge_volumes`) |
| `provision.store.garage.bootstrap` | Layout / key import / bucket+grant via the garage **admin API** — idempotent; also repairs a local stack whose `garage-init` never completed (`fabric.objects.status` → AccessDenied) |

---

## 6. Build service — `vera-builder`

A dedicated compilation container so Vera never needs a toolchain in its own image. It ships **arduino-cli + the ESP32 Arduino core, PlatformIO, gcc/g++/make/cmake/ninja, esptool and mpy-cross** behind a small HTTP API ([`vera/build/builder_service.py`](../vera/build/builder_service.py)); Vera reaches it at `VERA_BUILDER_URL` over `vera-net`.

| Endpoint (builder) | Vera capability | What it does |
|---|---|---|
| `POST /build/arduino` | `build.arduino`, `mesh.firmware.build` | `arduino-cli compile` → a **merged, flash-at-0x0** `.bin` (bootloader+partitions+app via `esptool merge_bin`) dropped into the mesh firmware catalog so the panel flasher can pick it up |
| `POST /build/platformio` | `build.platformio` | `pio run` for any PlatformIO board/framework (PlatformIO auto-installs platforms + `lib_deps` in its own per-project env) |
| `POST /build/python` | `build.python` | run Python in a **fresh, isolated virtualenv** — installs `requirements`, runs, discards the env |
| `POST /build/exec` | `build.run` | run an arbitrary build command (make/cmake/cargo/go/tsc/…) in a sandbox; optional `apt` (system pkgs), `pip` (into a venv) and `env` |
| `GET /health` | `build.status` | which toolchains + installed cores/libs are present + reachability |

**Automatic dependency management.** The builder installs what a build needs rather than requiring a pre-baked image:

- **Arduino** — `build.arduino`/`mesh.firmware.build` install the **board core** for the FQBN if it's missing (`esp32:esp32`, `arduino:avr`, `rp2040:rp2040`, `STMicroelectronics:stm32`, … — third-party cores via `board_urls`), then scan the sketch's `#include`s and auto-install the **libraries** they map to (`auto_libs`, on by default; skips core-bundled/std headers, resolves the rest via the library index). ArduinoJson is pinned to **v6** (the node sketch uses the v6 API). The returned `deps` lists what was installed.
- **Python** — `build.python` (and `build.run` with `pip`/`venv`) provisions a **per-call virtualenv**, installs the requested packages (or a `requirements.txt` in `files`) into it, runs, then throws it away — builds never pollute each other or the image.
- **System** — `build.run` can `apt`-install packages for a build (not isolated; persists in the running container until restart).
- Cores/libs and PlatformIO platforms are cached in the `builder-cache` / `builder-pio` volumes, so the second build of a given kind is fast.

`mesh.firmware.build` prefers the builder and falls back to a local `arduino-cli` if one is installed; with neither it returns a hint to start the container or build in the Arduino IDE. It picks the FQBN from the selected board profile's `chip` (falling back to `esp32:esp32:esp32s3:CDCOnBoot=default` — ESP32-S3 with USB-CDC **off**), because the reference display board wires the parallel TFT's D4/D5 onto the S3's native USB pins (GPIO19/20) and CDC-off frees them. It also applies the panel's bake options (board pin map, display/SD/CSI, Wi-Fi, server URL) to the source *before* compiling, so the resulting `.bin` matches what was configured. Source is passed inline as JSON (`{files:{path:content}}`); artifacts come back base64-encoded, so no shared volume is needed.

**Discovery.** `VERA_BUILDER_URL` wins if set. Otherwise Vera probes the published port (`http://localhost:$BUILDER_PORT`) *and* the compose DNS name and remembers whichever answers `/health` — the compose name only resolves in-stack, so without this a native (`./build.sh run`) orchestrator reports `can_build: false` even with the container running.

> **Security:** `build.run` / `/build/exec` runs arbitrary commands — it's a self-hosted build runner (like a CI worker). Keep it on `vera-net` / a trusted LAN; don't expose the port to untrusted networks.

> **First build is heavy:** it installs the ESP32 toolchains (~2 GB), then caches them in the `builder-cache` volume.

Bring it up whichever way suits the deployment:

```bash
docker compose up -d --build vera-builder     # in-stack
```

`build.builder.up` does the same thing for a native orchestrator (and is what the Mesh panel's **Start build service** button calls): it builds `vera/build/Dockerfile` if the image is missing, runs the container with `$BUILDER_PORT` published, and waits for `/health`. It's idempotent — a reachable builder returns immediately; `rebuild: true` forces a fresh image.

### Progress on long builds

An image build takes ~10 minutes and a sketch compile ~90 seconds. Both run in the **background** and report into a shared job registry, so the UI shows what's happening instead of a greyed-out button:

- `build.builder.up` and `mesh.firmware.build` return `{job_id}` immediately (pass `background: false` to block instead).
- `build.progress?job_id=…` returns `{phase, pct, done, ok, elapsed_s, log, result, error}`.
- The image build **streams** `docker build` output line by line; `Step n/m` is parsed into a real percentage. The sketch compile reports phases (baking → compiling → saving) and appends the compiler log at the end.
- Logs are capped at 400 lines, keeping the newest; finished jobs are evicted once 40 accumulate.

The Mesh panel's Flash card polls this and renders a phase line, a percentage bar, an elapsed timer, and a following log tail.

## 7. Configuration

| Env var | Default | Purpose |
|---|---|---|
| `VERA_DOCKER_HOSTS` | `~/.vera_docker_hosts.json` | Host-registry path |
| `VERA_WORKER_IMAGE` | `vera:latest` | Image used by `docker.worker.spawn` |
| `DOCKER_SOCK` | `/var/run/docker.sock` | Local unix socket path |
| `VERA_BUILDER_URL` | `http://vera-builder:8080` | Build service URL (native orchestrator: `http://localhost:8785`) |
| `BUILDER_PORT` | `8785` | Host port the builder is published on |
| `BUILDER_DEFAULT_FQBN` | `esp32:esp32:esp32` | Default board for `build.arduino` when unspecified |

---

## 8. UI

The Docker pane lives inside the **Workers** tab (`workers_ollama_panel.html` / the workers panels). It lists hosts, containers, and images via the engine proxy, and exposes the lifecycle + worker-spawn actions. Because lifecycle actions are sandbox-gated, the same `<vera-sandbox-controls>` editor shown there governs whether they run.

---

## See also

- [Execution & Network Mapping](./12-execution.md) — the sandbox that gates Docker CLI caps; `netscan.docker.scan`
- [Capability Framework](./01-capability-framework.md#6-distributed-dispatch) — how a spawned container becomes a worker
- [LLM Cluster](./04-ollama-cluster.md) — the worker/cluster view a Docker worker joins
- [Workers, Jobs & Syslog](./22-workers-jobs-syslog.md) — worker registry, metrics, job feed
- [Configuration](./10-configuration.md) — all env vars in one place

## Screenshots

## Host, container, and persistence model

Docker hosts are registered endpoints; containers and images are discovered
from a selected host. A container ID is only meaningful with its host identity.
Published ports describe reachability from that host, while volumes define
which state survives replacement. Operator connections may use a published web
port, but lifecycle remains owned by Docker capabilities.

Before restart/recreate/remove, inspect mounts, environment references, health,
dependent services, and whether the container belongs to Loop Lab. Never infer
disposability from a generated-looking name. Stopping a process is reversible;
removing a container may not be; removing volumes is destructive data loss.

For failures, separate daemon reachability, authentication/context, image pull,
container start, healthcheck, and application health. Logs explain the process;
`docker ps` explains only container state. Disk pressure frequently comes from
images, layers, build cache, logs, and volumes, so measure each category before
pruning.

<!-- VERA:AUTO:screenshots START -->
_No screenshots captured yet — run `docs.build` (or `operator.mission.run documentation`)._
<!-- VERA:AUTO:screenshots END -->

## Capabilities

<!-- VERA:AUTO:capabilities START -->
_No capabilities resolved for this domain._
<!-- VERA:AUTO:capabilities END -->
