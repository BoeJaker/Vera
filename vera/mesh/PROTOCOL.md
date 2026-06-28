# Vera Mesh — Wire Protocol

How an ESP32 (or anything) talks to the Vera Mesh Manager. Every transport carries
the **same JSON envelopes**; only the framing differs. The backend lives in
[mesh_capabilities.py](mesh_capabilities.py).

## Core idea

Every command Vera sends a node is a durable `mesh_jobs` row (`queued → sent →
done/error`). Whatever transport the node uses drains the **same** queue, so a node
can switch transports and never lose work. Devices **dedupe by `job_id`** (a job may
in theory arrive on two channels; running it twice should be harmless or guarded).

## Identity & auth

- A node picks a stable `node_id` (e.g. `esp32-<chip-mac>`). First `hello` enrolls it.
- Auth is **open on the LAN by default**. If the server sets `VERA_MESH_TOKEN`, every
  device call must present it (`token` field or `X-Mesh-Token` header). On enroll the
  server also issues a **per-node token** (returned by `hello`) — send it back on
  subsequent calls as `token`.

## Transports

| Transport | Framing | Direction | Notes |
|---|---|---|---|
| **HTTP long-poll** | REST | pull | Always on. `GET /mesh/poll` is held open ~25 s. |
| **WebSocket** | JSON msgs on `/mesh/ws` | push + pull | Always on. Server pushes `{"type":"jobs"}`. |
| **MQTT** | `vera/mesh/<id>/up` ⇄ `vera/mesh/<id>/down` | pub/sub | Optional (`VERA_MQTT_URL` + aiomqtt). |
| **Serial (server)** | newline JSON on a host USB port | pull | Optional (`VERA_MESH_SERIAL_PORTS` + pyserial). |
| **Serial (browser)** | newline JSON via Web Serial | relay | Panel reads the node and relays to HTTP. |

A node advertises which channels it has in `hello.channels` (e.g. `["http","serial"]`).

## Envelopes

### hello (node → server) — enroll / re-announce
```json
{ "kind":"hello", "node_id":"esp32-AABBCC", "name":"Porch", "board":"esp32",
  "fw":"1.0", "mac":"AA:BB:CC:..", "ip":"192.168.0.51", "rssi":-58,
  "parent_id":"", "modules":["sensor","kiosk","control"],
  "channels":["http","serial"], "token":"" }
```
Server replies:
```json
{ "ok":true, "node_id":"esp32-AABBCC", "token":"<per-node>",
  "modules":{"sensor":{"enabled":true}}, "config":{"sensor":{"interval_s":30}},
  "heartbeat":30, "poll_url":"/mesh/poll", "server_ts":"..." }
```
`modules` = what the device advertised it *can* do. `config` = server-managed
per-module settings the device should apply (persists across re-enrolls).

### poll (HTTP) — `GET /mesh/poll?node_id=<id>&wait=25&token=<t>`
Returns queued commands (and marks them `sent`):
```json
{ "jobs":[ {"job_id":"ab12..","type":"web_fetch","payload":{"url":"https://.."}} ],
  "heartbeat":30, "ts":"..." }
```
Empty `jobs` after `wait` seconds = nothing pending; poll again.

### telemetry (node → server) — `POST /mesh/telemetry`
Two accepted shapes:
```json
{ "kind":"telemetry", "node_id":"esp32-AABBCC", "rssi":-58,
  "metrics": { "temp":21.4, "humidity":48, "adc":1990 } }
```
```json
{ "node_id":"esp32-AABBCC",
  "readings":[ {"metric":"temp","value":21.4,"unit":"C","ts":"..."} ] }
```
Numeric readings are also appended to Data Fabric dataset `mesh.<node>.<metric>`.

### result (node → server) — `POST /mesh/result`
```json
{ "kind":"result", "node_id":"esp32-AABBCC", "job_id":"ab12..",
  "status":"done", "result":{"status_code":200,"len":1234}, "error":"" }
```

### WebSocket
Connect `/mesh/ws`; first message must be `hello`. Then send `telemetry`/`result`/
`ping`; receive `{"type":"hello_ok",...}`, `{"type":"jobs","jobs":[...]}`, `{"type":"pong"}`.

### Serial provisioning (host/browser → node)
A line the node accepts to bootstrap Wi-Fi:
```json
{ "cmd":"provision", "ssid":"..", "pass":"..", "server":"http://host:8000", "node_id":"esp32-001" }
```
Over serial, the node also **prints** its `hello`/`telemetry`/`result` envelopes as
newline-delimited JSON so the browser/host can relay them into the mesh.

## Job types (`type` + `payload`)

| type | payload | module | effect |
|---|---|---|---|
| `web_fetch` | `{url, method}` | web_fetch | node fetches URL → `result.status_code/body` |
| `read_sensor` | `{}` | sensor | node pushes a telemetry sample now |
| `kiosk_set` | `{url? , text?, brightness?}` | kiosk | set touchscreen display |
| `control_set` | `{channel, value}` | control | actuate GPIO/relay (`1/0/on/off/toggle`) |
| `alert` | `{message, level, sound}` | alert | buzzer/LED/screen alert |
| `watch` cfg | via `config.watch={url,interval_s}` | watch | node polls a target, alerts on failure |
| `config` | `{config:{...}}` | — | apply per-module settings |
| `identify` | `{seconds}` | — | blink LED to locate the node |
| `reboot` | `{}` | — | restart |

## Topology / node graph

Nodes may report `parent_id` (their mesh uplink). The graph (`GET /mesh/graph`) draws
`child → parent` edges where the parent is a known node, else a star edge to the
**Vera Hub**. A flat Wi-Fi fleet therefore renders as a star; an ESP-MESH relay tree
renders as a tree.
