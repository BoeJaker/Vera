# 14 · Device Mesh

`mesh/mesh_capabilities.py` manages a fleet of **ESP32** (or any HTTP / MQTT / serial-speaking) edge nodes. Each node enrolls, advertises a set of **modules** it can run, streams **telemetry**, and can be **sent work**. The whole fleet is browsable as a node graph in the Mesh panel.

The wire format every transport carries is specified in [`mesh/PROTOCOL.md`](../vera/mesh/PROTOCOL.md) — this page is the architecture and capability reference; the protocol doc is the byte-level contract for firmware authors.

---

## 1. Design — one durable queue, many drains

Every command sent to a node is a durable `mesh_jobs` row that moves `queued → sent → done/error`. **That row is the single source of truth.** Whichever transport the node happens to be using drains the *same* queue, so a node can switch transports (Wi-Fi drops, falls back to serial) and never lose work.

`_deliver` persists the job, then *nudges* every live channel. `_drain_commands` atomically pops queued rows (`BEGIN IMMEDIATE`) so two transports never double-deliver the same job. If no push channel is live, the row simply waits for the node's next poll. Devices are expected to **dedupe by `job_id`** since a job could in theory arrive on two channels.

Storage mirrors the Markets module: fresh WAL SQLite connections opened in an executor. Optional transports are dependency-guarded (`HAS_AIOMQTT`, `HAS_PYSERIAL`) so their absence never breaks startup.

---

## 2. Transports

| Transport | Framing | Direction | Availability |
|---|---|---|---|
| **HTTP long-poll** | REST; `GET /mesh/poll` held open ~25 s | pull | Always on |
| **WebSocket** | JSON msgs on `/mesh/ws`; server pushes `{"type":"jobs"}` | push + pull | Always on |
| **MQTT** | `vera/mesh/<id>/up` ⇄ `vera/mesh/<id>/down` | pub/sub | Optional (`VERA_MQTT_URL` + `aiomqtt`) |
| **Serial (server)** | newline-JSON on a host USB port | pull | Optional (`VERA_MESH_SERIAL_PORTS` + `pyserial`) |
| **Serial (browser)** | newline-JSON via the Web Serial API | relay | Panel relays a USB node into the mesh — no backend dep |

A node advertises which channels it has in `hello.channels` (e.g. `["http","serial"]`).

---

## 3. Node modules

A node tells the server what it *can* do via `hello.modules`. The server stores per-module config (which persists across re-enrolls) and the node applies it:

| Module | Does |
|---|---|
| `sensor` | Pushes telemetry samples (temp, humidity, ADC, …) |
| `web_fetch` | Fetches a URL on the node's behalf |
| `watch` | Polls a target on an interval; alerts on failure |
| `alert` | Buzzer / LED / screen alert |
| `kiosk` | Drives a touchscreen display |
| `control` | Actuates a GPIO / relay channel |

---

## 4. Capabilities

### Fleet & inspection

| Cap | Purpose |
|---|---|
| `mesh.nodes` | List all enrolled nodes (status, modules, last-seen, RSSI) |
| `mesh.node` | One node's full detail |
| `mesh.graph` | The fleet topology graph (see §6) |
| `mesh.telemetry` | Recent telemetry for a node/metric |
| `mesh.jobs` | Job queue history for a node |

### Sending work

| Cap | Purpose |
|---|---|
| `mesh.send` | Queue an arbitrary `{type, payload}` job to one node |
| `mesh.broadcast` | Queue a job to every node (optionally filtered by module) |
| `mesh.config` | Push per-module config settings to a node |
| `mesh.update` | Update a node's stored record |
| `mesh.forget` | De-enroll / remove a node |

### Typed job shortcuts

Thin wrappers over `mesh.send` for the common job types:

| Cap | Job type | Effect on node |
|---|---|---|
| `mesh.web_fetch` | `web_fetch` | Node fetches a URL → returns status/body |
| `mesh.kiosk_set` | `kiosk_set` | Set the touchscreen display (url/text/brightness) |
| `mesh.control_set` | `control_set` | Actuate a GPIO/relay (`1/0/on/off/toggle`) |
| `mesh.alert` | `alert` | Trigger a buzzer/LED/screen alert |
| `mesh.identify` | `identify` | Blink an LED to physically locate the node |
| `mesh.reboot` | `reboot` | Restart the node |

---

## 5. Telemetry → Data Fabric

Numeric telemetry readings are best-effort appended to [Data Fabric](./06-data-fabric.md) datasets `mesh.<node_id>.<metric>` (alongside the fast local table the panel reads). So a fleet of temperature sensors becomes queryable, chartable fabric history without any extra wiring — and is recallable by the same DSL as everything else Vera stores.

---

## 6. Identity, auth & topology

- A node picks a stable `node_id` (e.g. `esp32-<chip-mac>`); the first `hello` enrolls it.
- Auth is **open on the LAN by default**. Set `VERA_MESH_TOKEN` to require a token on every device call; on enroll the server also issues a per-node token returned by `hello`.
- Nodes may report `parent_id` (their mesh uplink). `mesh.graph` draws `child → parent` edges where the parent is a known node, else a star edge to the **Vera Hub**. A flat Wi-Fi fleet renders as a star; an ESP-MESH relay tree renders as a tree.

---

## 7. UI

**`mesh-panel`** (`mesh_panel.html`) renders the fleet: per-node cards (status, RSSI, modules, telemetry sparklines), the topology graph, a job/command console, and the Web Serial relay for provisioning a USB-connected node directly from the browser.

---

## 8. Configuration

| Env var | Purpose |
|---|---|
| `VERA_MESH_TOKEN` | If set, every device call must present this token |
| `VERA_MQTT_URL` | Enable the MQTT transport (needs `aiomqtt`) |
| `VERA_MESH_SERIAL_PORTS` | Host USB ports to drain (needs `pyserial`) |

---

## See also

- [`mesh/PROTOCOL.md`](../vera/mesh/PROTOCOL.md) — the byte-level wire protocol (envelopes, job types, provisioning)
- [Data Fabric](./06-data-fabric.md) — where telemetry lands (`mesh.<node>.<metric>`)
- [Capability Framework](./01-capability-framework.md) — `mesh.*` registration & events
- [Markets](./15-markets.md) — the sibling module this one's storage pattern mirrors
