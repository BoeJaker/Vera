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

`docker.worker.spawn` uses `VERA_WORKER_IMAGE` (default `vera:latest`). This means you can scale capability throughput by spawning workers onto any registered Docker host — local, a TCP daemon, or over SSH — straight from the harness.

---

## 6. Configuration

| Env var | Default | Purpose |
|---|---|---|
| `VERA_DOCKER_HOSTS` | `~/.vera_docker_hosts.json` | Host-registry path |
| `VERA_WORKER_IMAGE` | `vera:latest` | Image used by `docker.worker.spawn` |
| `DOCKER_SOCK` | `/var/run/docker.sock` | Local unix socket path |

---

## 7. UI

The Docker pane lives inside the **Workers** tab (`workers_ollama_panel.html` / the workers panels). It lists hosts, containers, and images via the engine proxy, and exposes the lifecycle + worker-spawn actions. Because lifecycle actions are sandbox-gated, the same `<vera-sandbox-controls>` editor shown there governs whether they run.

---

## See also

- [Execution & Network Mapping](./12-execution.md) — the sandbox that gates Docker CLI caps; `netscan.docker.scan`
- [Capability Framework](./01-capability-framework.md#6-distributed-dispatch) — how a spawned container becomes a worker
- [LLM Cluster](./04-ollama-cluster.md) — the worker/cluster view a Docker worker joins
- [Workers, Jobs & Syslog](./22-workers-jobs-syslog.md) — worker registry, metrics, job feed
- [Configuration](./10-configuration.md) — all env vars in one place
