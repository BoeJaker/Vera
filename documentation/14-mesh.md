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
| `kiosk` | Drives a display — the Arduino + MicroPython reference firmwares include a self-contained driver for the 3.5" Arduino-Uno TFT shield (ILI9488, 320x480, 8-bit parallel) on ESP32-S3 Uno-footprint boards: boot status dashboard, `kiosk_set` text/BMP/status modes, runtime pin remap via `config.io.tft` |
| `storage` | SD card (the TFT shield's SPI slot): `sd_list`/`sd_read`/`sd_write`/`sd_delete` jobs + `sd_total_mb`/`sd_used_mb` telemetry; pins via `config.io.sd` |
| `control` | Actuates a GPIO / relay channel |
| `rgb` | On-board WS2812/NeoPixel (`mesh.rgb`, effects; pin via `config.io.neopixel` or `mesh.rgb.probe`) |
| `ble` | BLE scan (`mesh.ble.scan`) → devices ingested to the Network Map + positioning |
| `toolkit` | ESP32-S3 swiss-army knife: CSI motion, Wi-Fi promiscuous sniff, I2C scan, touch, internal temp, channel survey, ESP-NOW ranging, deep-sleep, sysinfo |
| `position` | RF positioning: node coordinates + `rf_range` RSSI reports feed `mesh.locate` multilateration |

### Getting firmware onto a board (all from the panel)

Four routes, none of which need a local toolchain on your machine:

| Route | What happens | When |
|---|---|---|
| **MicroPython** | Flash the runtime over Web Serial (esptool-js), then `main.py` is pushed over the REPL and verified byte-for-byte | Default. No compile step |
| **Arduino** | `mesh.firmware.build` compiles the sketch in the **vera-builder** container, merges it to a flash-at-`0x0` image, drops it in the catalog, and the panel flashes it over Web Serial | Needed for CSI, promiscuous sniff, **and the S3-Uno display** |
| **Upload .bin** | You compiled elsewhere (Arduino IDE); upload the image and flash/OTA it from here forever after | No build service, or a custom sketch |
| **OTA** | `mesh.ota` pushes a built `.bin` (or a `main.py`) to a node already on Wi-Fi | Fleet updates — no cable |

The compiler lives in a separate container so the Vera image stays slim. It is **not running by default**: the Flash card shows its state and a **Start build service** button, which calls `build.builder.up` → builds `vera/build/Dockerfile` and starts the container with its port published. The first run pulls the ESP32 toolchains (~2 GB) and takes minutes; later builds take seconds. `VERA_BUILDER_URL` overrides discovery; otherwise Vera probes the published port *and* the compose DNS name, so a native (`./build.sh run`) orchestrator finds it just as an in-stack one does.

Whichever route you take, the **bake options are applied to the source first** — board pin map, display/SD/CSI toggles, Wi-Fi credentials, server URL — so a flashed node is already configured. `mesh.firmware.build` also picks the FQBN from the board profile's `chip`, so an S3 profile is never compiled as a classic ESP32.

**Wi-Fi credentials** are a default, not a lock: both firmwares prefer what was provisioned over serial (NVS / `vera_cfg.json`) and fall back to the baked pair, which is what gets a freshly erased node online with no cable step. The panel's password box clears after you save it, so the server fills a missing password from the saved (sealed) profile when the SSID matches — the password never round-trips through the browser. A node with no credentials at all says so on its screen and console rather than sitting silently on a blank Wi-Fi.

### Keeping the fleet current — auto-OTA

**On by default.** At every `hello`, a node whose `fw` trails the newest artifact for its runtime gets an update queued over Wi-Fi. Opt out per node with `config.ota.auto = false`, or fleet-wide with the `ota_auto` mesh setting.

| Runtime | Artifact | Mechanism |
|---|---|---|
| MicroPython | the served `main.py` | `mesh.ota mode=file` → written over the REPL-installed script, then `machine.reset()` |
| Arduino | the newest **built** `.bin` for that node's chip | `mesh.ota mode=bin` → `httpUpdate` into the spare OTA partition, then reboots |

Nodes report `runtime` and `chip` in `hello`, and the version comparison uses each firmware's `FW_VERSION` (`x.y.z-mpy` / `x.y.z-ino`). **Bump `FW_VERSION` when you change a firmware** or nodes will never be offered the new build.

The selection rules are deliberately conservative — a wrong artifact here bricks a node:

- Arduino nodes are compared against the **`.bin`'s** recorded version, not the current `.ino`. Editing the sketch after a build therefore can't put nodes in a re-flash loop against an image they already run.
- Only images Vera built are eligible (they carry a `<name>.bin.json` sidecar). An uploaded `.bin` of unknown provenance is never auto-pushed.
- The chip must match. If a node's chip is unknown and several chips have builds, Vera refuses to guess and logs instead.
- Nodes that report neither `runtime` nor a recognisable `FW_VERSION` suffix (i.e. flashed before this existed) are left alone until reflashed.
- Serial/bridged nodes are skipped — they can't fetch the artifact themselves.

### Display bring-up: the GPIO19/20 problem

On the hardware-verified `s3-uno-ili9488` profile the shield's **LCD_D4/D5 land on GPIO19/20 — the ESP32-S3's native USB-Serial-JTAG (D-/D+) lines**. While the USB PHY owns that pad those two data bits are stuck, so the parallel bus writes garbage and the panel stays white. This is why the display "only works in the Arduino sketch": that build is compiled with **USB CDC On Boot: Disabled** (`CDCOnBoot=default`, what `mesh.firmware.build` uses), which puts the console on UART0 and leaves 19/20 free.

Both firmwares now release the pad explicitly (`USB_SERIAL_JTAG_CONF0.USB_PAD_ENABLE`) rather than relying on the board menu, and only when a data pin actually sits on 19/20:

- **Arduino** — `TFT_FREE_USB_PINS` (default on). Belt-and-braces: the display still comes up if the sketch was built with the wrong USB setting. Runs on live pin remap too.
- **MicroPython** — `TFT_FREE_USB_PINS` (default **off**, exposed as the panel's **🔌 Free USB pins** bake option). MicroPython's REPL *is* the USB-Serial-JTAG device, so taking the pad **kills the USB REPL** — the node is then reachable over Wi-Fi and UART0 only. Before it fires, the firmware moves the REPL to UART0 (`os.dupterm`) and waits `TFT_FREE_USB_GRACE` seconds, in which any keypress aborts the takeover. Recovery is always available: hold **BOOT**, tap **RESET**, and the ROM bootloader re-enables the pad so the panel can reflash.

`mesh.sysinfo` reports `usb_pads_freed` and `display`, and the node inspector's pin-map card names all three ways out (Arduino build · Free USB pins · rewire and remap with `mesh.io.pins`).

### Server-driven UI — both firmwares

Vera pushes a **screen** (a list of widgets) with `ui_screen`; the node renders it and reports taps back as `ui_event`, which route to a capability. All app logic stays on the server, so adding an app needs no reflash. Widget schema:

| Widget | Fields |
|---|---|
| `label` | `text, color, bg, size` |
| `rect` | `w, h, color, fill` |
| `hline` | `w, h, color` |
| `button` | `w, h, text, color, bg, size, action` |
| `bar` | `w, h, val (0-100), color, label` |

Colours are RGB565 ints. Both the MicroPython and Arduino firmwares implement the **same** schema and the same job types (`ui_screen`, `ui_clear`, `touch_raw`, `touch_cal`), and both advertise the `ui` module — `tests/test_mesh_firmware_build.py` fails if either side drops one.

**Touch** is the shield's 4-wire resistive panel, whose wires share LCD data/control pins — every read reconfigures them and hands them straight back, or the next render draws garbage. Pins default to the S3-Uno map and are remappable live via `config.io.touch = {xp,ym,yp,xm}`; calibration via `config.touch = {x0,x1,y0,y1,zmin,zmax,swap,invx,invy}` or the `touch_cal` job. Use `touch_raw` (hold a finger down) to read the raw ADC corners.

While a touch UI is on screen the node **shortens its long-poll** from 25s to 2s — taps are polled in the main loop, so a long block would make a macro pad feel dead.

> **Partition scheme:** Arduino builds use `PartitionScheme=min_spiffs` (1.9MB per app slot, OTA preserved). The UI engine had already reached 90% of the 1.3MB default, and an image that doesn't fit can't be OTA'd at all.

### Pictures on the panel — `mesh.ui.image`

Sprites, companions, generated art and page screenshots all go the same way. A 480x320 frame is **300 KB of RGB565** — far too big to push through the job queue and far too big to buffer on the node — so Vera renders the source and the node *streams* it:

1. `mesh.ui.image` takes a `url`, a server `path`, or `data_b64` (what the render/sprite capabilities hand back).
2. Pillow resizes it (`fit`: `contain` letterboxes, `cover` crops to fill, `stretch` ignores aspect) and encodes **V565** — an 8-byte header (`"V565"` + big-endian w,h) followed by raw RGB565 rows.
3. The frame is cached under `firmware/img/` and served at `/mesh/ui/img/<name>.v565` (basenamed, `.v565` only — an unauthenticated LAN device fetches it).
4. The node GETs it and blits row by row. It decodes nothing and never holds a full frame; a short read fails the job rather than leaving half a picture claiming success.

Three ways to show one: the `ui_image` job (full-screen), an `image` widget inside a normal screen (mixed with buttons and labels), or `kiosk_set {img_url}`. The frame cache keeps the newest 60.

> Byte order and geometry are pinned by tests — getting them wrong produces a garbled panel, which is miserable to debug on hardware.

### Animation — `mesh.ui.animate`

Companions, Sprite Studio sheets, animated GIFs and live emoji. Fetching a frame per tick would stutter and flood the link, so the **whole sequence goes in one file** (`V56A`: 12-byte header + raw frames) that the node caches in **PSRAM** and plays locally with no network at all. Playback is driven from `loop()`, so taps and jobs stay responsive.

Sources: an animated GIF/WebP (frames read directly), a sprite **sheet** (pass `cols`/`rows` to slice it), or an explicit `frames` list. `fps` is clamped to something the bus can actually draw, and the node refuses a sequence larger than its free PSRAM with a message naming the size rather than failing mid-blit. `mesh.ui.animate.stop` frees it.

> Every path that leaves animation mode frees the buffer — a stale sequence holds PSRAM until reboot. Pinned by a test.

### App library — `mesh.app.*`

An **app** is a server-side screen builder plus its tap→capability map, so adding one never means reflashing. `mesh.app.list` enumerates them, `mesh.app.launch` runs one, `mesh.app.stop` returns to the status dashboard. Taps route through `app:<id>` (launcher entries), `nav:<screen>` and `macro:<i>`.

**Pads follow the Vera UI you're looking at.** Bind a node in the Mesh panel (*"Selected node follows the Vera tab I'm on"*) and the harness reports the focused panel on every tab change; the node then shows that panel's pad — open Markets, get Markets controls. It is deliberately unobtrusive:

- **Opt-in.** Nothing is sent until you bind a node, so an unbound display is never touched.
- **Idempotent.** Re-pushing the pad already on screen is a no-op, so calling it on every tab change is free.
- **Panels with no pad leave the node alone** rather than blanking it — visiting an unrelated tab shouldn't wipe your pad.

Pads are defined in `PANEL_PADS` (panel id → buttons). A button marked `self` gets the tapping node's own `node_id` injected, so the Mesh pad drives *that* node rather than whichever one came first.

### Macro pads

Beyond the per-panel pads, the pad system itself does the things that make one usable in practice:

- **Every tap answers on the panel.** Running a capability used to change nothing on screen, so a success and a silent failure looked identical. A tap now shows the result (or the error, in red) with a **Back** button. Errors always win over a truncated success.
- **Paging.** A 480x320 panel fits ~6 buttons; more than that used to be laid out past the bottom edge where they could never be tapped. Pads now page, and button indices stay stable across pages.
- **Confirmation.** A button marked `confirm` asks first — a resistive panel picks up knocks and sleeves, and there's no undo on the other side of a tap. Built-in destructive actions (e.g. a LAN scan) are flagged.
- **Saved pads.** `mesh.app.pad.save` / `.delete` persist custom pads; they appear in the launcher and launch as `pad:<id>`.
- `self` on a button injects the tapping node's own `node_id`, so a pad drives *that* node.

### SD toolkit — a card reader you can drive from Vera

The shield's SD slot makes a node a card reader. `mesh.sd.walk` lists a card recursively (budgeted — a card can hold tens of thousands of files and the result must fit one response, so it reports `truncated`; the firmware uses an explicit stack because nested `File` handles would exhaust the task stack).

`mesh.sd.identify` works out what the card *is* from the listing alone — Switch, 3DS, Wii U, Vita or a retro handheld, by signature directories — and lists games by ROM/title extension, biggest first, with dump decorations like `[0100ABC]` and `(USA)` stripped from titles. **Titles come from filenames**: this reads no title database, so a badly named dump reads badly.

`mesh.sd.dump` archives files into Vera's store **idempotently**. The node *pushes* each file (`POST /mesh/sd/upload`) streamed straight off the card — pulling them as job results would cost a long-poll round trip per kilobyte. A file already stored at the same size answers `208` and isn't rewritten, so re-running a dump is nearly free and never duplicates. Uploads land under a `.part` name and are renamed only once complete, so an interrupted transfer can't masquerade as a finished file.

> Store paths are built from device-supplied names on an unauthenticated LAN device, so every path component is sanitised and `..` is dropped — pinned by a traversal test. Spaces and brackets survive, because real game filenames have them.

### Reading a node without a serial console

The Arduino build runs with USB-CDC **off** (that's what frees GPIO19/20 for the display), so `Serial` goes to UART0 and a USB cable shows you nothing. The screen is the console:

- the **firmware version** appears on the boot screen, on the Wi-Fi screen, and in the corner of the status dashboard — so you can confirm which image is actually running;
- the status dashboard shows the **SSID and its live state**;
- on a failed join the node **scans** and says which it was: `SSID not seen (2.4GHz only?)` vs `seen -63dBm, check password` — the ESP32 is 2.4GHz-only, so a dual-band router advertising one SSID is a common trap;
- `enroll()` reports the real HTTP result instead of always claiming success, so "joined Wi-Fi but the server is unreachable" is visible rather than silent.

### ESP32-S3 toolkit + CSI positioning + Network Map

`vera/mesh/mesh_toolkit_capabilities.py` turns a mesh of headless S3 nodes into a distributed RF sensor grid. Job types are in [PROTOCOL.md](../vera/mesh/PROTOCOL.md); the highlights:

- **Distributed sensing**: `mesh.sniff` (anonymous Wi-Fi frame/MAC density — foot-traffic), `mesh.csi.start` (device-free human motion via Channel State Information; Arduino/IDF firmware only), `mesh.ble.scan`, `mesh.channel.survey`.
- **RF positioning**: place anchor nodes with `mesh.node.position` (x,y metres), broadcast `mesh.rf.range` so several nodes report RSSI to a target MAC/BSSID, then `mesh.locate` multilaterates its (x,y) with a log-distance path-loss model + weighted least-squares. CSI presence per node via `mesh.presence`.
- **Network Map bridge**: WiFi scans (`netscan.wifi.ingest`) and BLE scans (`netscan.ble.ingest`) feed the same aux graph the Network Map renders; `mesh.netmap.sync` (also on a 4-min timer) projects every node as a `:NetHost` so the fleet + what each node hears appear on the map, and located targets drop in as `:LocatedTarget`.
- **Board bring-up**: `mesh.rgb` / `mesh.rgb.probe` (find an unknown NeoPixel pin), `mesh.i2c.scan`, `mesh.touch`, `mesh.sysinfo`, `mesh.deep_sleep`.

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

## Screenshots

<!-- VERA:AUTO:screenshots START -->
_No screenshots captured yet — run `docs.build` (or `operator.mission.run documentation`)._
<!-- VERA:AUTO:screenshots END -->

## Capabilities

<!-- VERA:AUTO:capabilities START -->
_No capabilities resolved for this domain._
<!-- VERA:AUTO:capabilities END -->
