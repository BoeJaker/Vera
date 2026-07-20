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
| `kiosk_set` | `{text?, title?, url?, mode:"status"?, color?, bg?, size?, rotation?, bmp?}` | kiosk | draw on the display: free text (RGB565 `color`/`bg`, `size` 1-4), the built-in status dashboard (`mode:"status"`), or a 24-bit BMP from SD (`bmp:"/img.bmp"`, Arduino only) |
| `control_set` | `{channel, value}` | control | actuate GPIO/relay (`1/0/on/off/toggle`) |
| `alert` | `{message, level, sound}` | alert | buzzer/LED/screen alert (full-screen red banner on kiosk nodes) |
| `sd_list` | `{path?}` | storage | list SD directory → `{files:[{name,size,dir}], total_mb}` |
| `sd_read` | `{path, max?}` | storage | read a file (≤1400 bytes per result) → `{content, size, truncated}` |
| `sd_write` | `{path, content, append?}` | storage | write/append a file on SD |
| `sd_delete` | `{path}` | storage | delete a file/empty dir on SD |
| `watch` cfg | via `config.watch={url,interval_s}` | watch | node polls a target, alerts on failure |
| `config` | `{config:{...}}` | — | apply per-module settings |
| `identify` | `{seconds}` | — | blink LED to locate the node |
| `reboot` | `{}` | — | restart |
| `neopixel_set` | `{r,g,b,pin?,n?,effect?,brightness?}` | rgb | drive the on-board WS2812 (`effect`: solid/blink/breathe/rainbow/off) |
| `neo_probe` | `{pins?,dwell_ms?}` | rgb | light each candidate GPIO in turn to find an unknown NeoPixel pin |
| `ble_scan` | `{seconds?,active?}` | ble | scan BLE → `result.devices=[{mac,name,rssi}]` (auto-ingested to netmap) |
| `sniff` | `{channel?,seconds?}` | toolkit | Wi-Fi promiscuous frame/MAC density counts (Arduino/IDF only) |
| `csi_start` / `csi_stop` | `{interval_s?,threshold?,source?}` | toolkit | Wi-Fi CSI device-free motion → `csi_motion`/`csi_present` telemetry (Arduino/IDF only) |
| `i2c_scan` | `{sda,scl,freq?}` | toolkit | scan the I2C bus → `result.addresses` |
| `touch_read` | `{pin}` | toolkit | capacitive touch value (also telemetry) |
| `temp_read` | `{}` | toolkit | internal temperature → `mcu_temp` |
| `channel_survey` | `{}` | toolkit | per-channel AP occupancy → `result.survey`, `clearest` |
| `rf_range` | `{target,kind?,samples?}` | position | median RSSI to a target MAC/BSSID → `result.{target,rssi}` (feeds positioning) |
| `espnow_ping` | `{peer,count?}` | toolkit | ESP-NOW connectionless ranging (MicroPython) |
| `sysinfo` | `{}` | toolkit | chip/flash/PSRAM/MAC/heap detail as `result` |
| `deep_sleep` | `{seconds}` | toolkit | timer deep-sleep; wakes and re-enrolls |
| `ui_screen` | `{screen:{title?,bg?,widgets:[{t,x,y,…}]}}` | ui | render a server-pushed screen (widget `t`: label/rect/hline/button/bar) |
| `ui_clear` | `{}` | ui | drop back to the local status dashboard |
| `touch_raw` | `{samples?}` | ui | return raw resistive-touch ADC `{raw:[x,y,z]}` for calibration |
| `touch_cal` | `{x0,x1,y0,y1,zmin,zmax,swap,invx,invy}` | ui | set touch calibration |

### Server-driven UI (display nodes)

A node with a display renders **screens** Vera pushes via `ui_screen`; the node is a
thin renderer, so "apps" (status, system monitor, macro pad, chat, companion viewer)
are screen-builders on the server ([mesh_ui_capabilities.py](mesh_ui_capabilities.py):
`mesh.ui.screen/text/home/sysmon/macropad`), no reflashing to add one. Widgets carry
RGB565 int colours. Touch reporting (tap → run a capability) lands with the touch
increment; buttons already carry an `action` for it.

**Auto-OTA:** the firmware reports `FW_VERSION` in its `hello`. If a node's
`config.ota.auto` is set and its version trails the served firmware, Vera queues an
`ota` (mode=file) update automatically — de-duped per version, http nodes only.

**SD "file server":** `mesh.sd.ls/cat/put` wrap the `sd_list/sd_read/sd_write` jobs
for browsing/serving the card; results return via `mesh.jobs`.

**Touch:** 4-wire resistive (shares LCD pins). A tap hit-tests the pushed buttons and
POSTs `{node_id, action}` to `/mesh/ui/event` → `route_ui_event` (nav:* switches
screens, macro:* runs the mapped capability). Calibrate on-hardware with
`mesh.ui.touch_raw` (read raw ADC at the screen corners) → `mesh.ui.calibrate`
(x0/x1/y0/y1 + zmin/zmax + swap/invx/invy). Default touch pins for this shield:
XP=GPIO3(D6), YM=GPIO14(D7), YP=GPIO6(RS), XM=GPIO7(WR) — override via `config.io.touch`.

### ESP32-S3 toolkit, CSI positioning & the Network Map

A mesh of ESP32-S3 nodes becomes a distributed RF sensor grid. The extended
firmware adds the job types above; the backend
([mesh_toolkit_capabilities.py](mesh_toolkit_capabilities.py)) exposes typed
shortcuts (`mesh.rgb`, `mesh.ble.scan`, `mesh.csi.start`, `mesh.sniff`,
`mesh.i2c.scan`, `mesh.sysinfo`, …) and wires results back:

- **Netmap** — WiFi scans (`netscan.wifi.ingest`) and BLE scans
  (`netscan.ble.ingest`) land in the same aux graph the Network Map renders;
  every mesh node is projected as a `:NetHost` (`mesh.netmap.sync`, also on a
  timer). So the fleet and everything each node hears appears on the map.
- **CSI presence** — nodes running `csi_start` report `csi_motion`/`csi_present`
  telemetry (device-free human motion sensing); surfaced by `mesh.presence`.
- **RSSI positioning** — set anchor coordinates with `mesh.node.position`, have
  several nodes report RSSI to a target (broadcast `rf_range`, or ongoing
  wifi/ble scans), then `mesh.locate` multilaterates the target's (x,y) via a
  log-distance path-loss model.

Only headless nodes need the toolkit; CSI + promiscuous `sniff` require the
Arduino/IDF firmware (MicroPython can't access CSI/promiscuous mode and returns
a clear error for those two). RGB pin is board-specific — `config.io.neopixel`
sets it, or `neo_probe` finds it.

### Display / SD nodes (ILI9488 Uno shield)

The Arduino and MicroPython reference firmwares include a self-contained driver
for the common 3.5" Arduino-Uno TFT shield (ILI9488, 320x480, 8-bit parallel
bus + SD slot) on ESP32-S3 Uno-footprint boards. Nodes with a working display
advertise the `kiosk` module and boot into a live **status dashboard** (node id,
IP/RSSI, server, uptime, SD usage, last job) that refreshes with each telemetry
tick; `kiosk_set` switches between text/BMP content and the dashboard. A
mounted card advertises `storage`, reports `sd_total_mb`/`sd_used_mb` in
telemetry, and serves the `sd_*` jobs.

All display/SD pins are **runtime-remappable** (no reflash) via server config:

```json
{ "io": { "tft": { "rst":4, "cs":5, "dc":6, "wr":7, "rd":1,
                    "d": [8,9,18,17,19,20,3,14] },
           "sd":  { "clk":12, "miso":13, "mosi":11, "cs":10 } },
  "kiosk": { "rotation": 1 } }
```

## Topology / node graph

Nodes may report `parent_id` (their mesh uplink). The graph (`GET /mesh/graph`) draws
`child → parent` edges where the parent is a known node, else a star edge to the
**Vera Hub**. A flat Wi-Fi fleet therefore renders as a star; an ESP-MESH relay tree
renders as a tree.
