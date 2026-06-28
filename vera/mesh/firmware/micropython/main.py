# ============================================================================
# main.py  —  Vera ESP32 Mesh Node (MicroPython reference firmware)
# ============================================================================
#
# Enrolls into a Vera mesh and runs configurable modules over HTTP long-poll.
# Also speaks the same JSON-line envelopes on the USB serial REPL line so the
# browser (Web Serial) / server (pyserial) can read telemetry and provision it.
# Templated by GET /mesh/firmware?flavor=micropython.
#
# Flash MicroPython, copy this as main.py. Set WIFI_SSID/WIFI_PASS below, or
# provision over serial with: {"cmd":"provision","ssid":"..","pass":"..",...}
#
# Modules: sensor, web_fetch, watch, alert, control (+ kiosk stub).
# Wire protocol: see vera/mesh/PROTOCOL.md
# ============================================================================

import network, time, json, sys, uselect, machine
try:
    import urequests as requests
except ImportError:
    import requests

# ── Config (templated on download) ──────────────────────────────────────────
SERVER = "{{SERVER_URL}}"          # e.g. http://192.168.0.138:8000
TOKEN  = "{{MESH_TOKEN}}"          # "open" if no shared token
NODE   = "{{NODE_ID}}"

WIFI_SSID = ""                     # or provision over serial
WIFI_PASS = ""

# ── Pins (board-robust: a GPIO that doesn't exist on this chip must NOT crash
#    the firmware at import — ESP32 / S2 / S3 / C3 all differ). Pins are also
#    overridable at runtime from the server via config.io = {led, relay, adc}. ──
def _mkpin(n, mode=None):
    try:
        return machine.Pin(n, mode) if mode is not None else machine.Pin(n)
    except Exception:
        return None

def _mkadc(n):
    try:
        a = machine.ADC(machine.Pin(n))
        try: a.atten(machine.ADC.ATTN_11DB)
        except Exception: pass
        return a
    except Exception:
        return None

LED_PIN, RELAY_PIN, ADC_PIN = 2, 5, 34      # safe-ish defaults; override via config.io
LED   = _mkpin(LED_PIN, machine.Pin.OUT)
RELAY = _mkpin(RELAY_PIN, machine.Pin.OUT)
ADC   = _mkadc(ADC_PIN)

MODULES = ["sensor", "web_fetch", "watch", "alert", "control", "io", "worker"]
cfg = {}
telemetry_every = 30
watch_url = ""; watch_every = 0
_last_tele = 0; _last_watch = 0
_worker_last = {}
SERIAL_ONLY = False        # bridge mode: the host browser relays us to Vera over USB
                           # (set via {"cmd":"serial_mode","on":true}) — skip all Wi-Fi I/O.
_spoll = uselect.poll(); _spoll.register(sys.stdin, uselect.POLLIN)


def uid():
    import ubinascii
    return "esp32-" + ubinascii.hexlify(machine.unique_id()).decode()

def board_name():
    try:
        import os
        return os.uname().machine        # e.g. "ESP32C3 module with ESP32C3"
    except Exception:
        return "esp32"

def emit_serial(obj):
    try: print(json.dumps(obj))
    except Exception: pass

def headers():
    h = {"Content-Type": "application/json"}
    if TOKEN and TOKEN != "open": h["X-Mesh-Token"] = TOKEN
    return h

def post(path, obj):
    if SERIAL_ONLY:                 # bridged over USB — the browser relays our emit_serial output
        return {}
    try:
        r = requests.post(SERVER + path, data=json.dumps(obj), headers=headers())
        try: out = r.json()
        except Exception: out = {}
        r.close(); return out
    except Exception as e:
        return {"error": str(e)}

# ── WiFi ─────────────────────────────────────────────────────────────────────
wlan = network.WLAN(network.STA_IF); wlan.active(True)

def wifi_connect(timeout=12):
    # Never raise — a Wi-Fi hiccup must not kill the firmware (you'd lose the
    # serial REPL and the ability to re-provision). Returns True if connected.
    if not WIFI_SSID:
        return False
    try:
        if not wlan.active():
            wlan.active(True)
    except Exception:
        pass
    if wlan.isconnected():
        return True
    # Don't call connect() again while a connect is already in progress — that is
    # what raises 'sta is connecting, return error' / OSError: Wifi Internal Error.
    try:
        if wlan.status() == network.STAT_CONNECTING:
            connecting = True
        else:
            connecting = False
    except Exception:
        connecting = False
    if not connecting:
        try:
            wlan.connect(WIFI_SSID, WIFI_PASS)
        except OSError:
            try: wlan.disconnect()
            except Exception: pass
            return False
    t0 = time.time()
    while not wlan.isconnected() and time.time() - t0 < timeout:
        handle_serial(); time.sleep(0.3)
    return wlan.isconnected()

# ── Enroll / config ──────────────────────────────────────────────────────────
def enroll():
    global TOKEN
    d = {"kind": "hello", "node_id": NODE, "name": NODE, "board": board_name(),
         "fw": "1.0-mpy", "ip": wlan.ifconfig()[0] if wlan.isconnected() else "",
         "rssi": _rssi(), "modules": MODULES, "channels": ["http", "serial"]}
    if TOKEN and TOKEN != "open": d["token"] = TOKEN
    emit_serial(d)
    r = post("/mesh/hello", d)
    if isinstance(r, dict):
        if r.get("token"): TOKEN = r["token"]
        apply_config(r.get("config") or {})

def apply_config(c):
    global cfg, telemetry_every, watch_url, watch_every, LED, RELAY, ADC
    cfg = c or {}
    s = cfg.get("sensor") or {}
    if s.get("interval_s"): telemetry_every = int(s["interval_s"])
    w = cfg.get("watch") or {}
    watch_url = w.get("url", ""); watch_every = int(w.get("interval_s", 0) or 0)
    io = cfg.get("io") or {}                 # remap pins for this board, no reflash
    if "led" in io:   LED   = _mkpin(int(io["led"]), machine.Pin.OUT)
    if "relay" in io: RELAY = _mkpin(int(io["relay"]), machine.Pin.OUT)
    if "adc" in io:   ADC   = _mkadc(int(io["adc"]))
    _worker_last.clear()

def _worker_tick():
    # worker module: run recurring edge tasks assigned via mesh.worker.assign
    for i, tk in enumerate((cfg.get("worker") or {}).get("tasks") or []):
        iv = int(tk.get("interval_s", 60) or 60)
        if time.time() - _worker_last.get(i, 0) >= iv:
            _worker_last[i] = time.time()
            run_job({"job_id": "w%d-%d" % (i, int(time.time())),
                     "type": tk.get("type"), "payload": tk.get("payload") or {}})

def _rssi():
    try: return wlan.status("rssi")
    except Exception: return None

# ── Telemetry / result ───────────────────────────────────────────────────────
def send_telemetry():
    metrics = {"uptime": int(time.time())}
    if ADC is not None:
        try: metrics["adc"] = ADC.read()
        except Exception: pass
    try:
        import gc; metrics["mem"] = gc.mem_free()
    except Exception: pass
    d = {"kind": "telemetry", "node_id": NODE, "rssi": _rssi(), "metrics": metrics}
    emit_serial(d); post("/mesh/telemetry", d)

def send_result(job_id, status, result=None, error=""):
    d = {"kind": "result", "node_id": NODE, "job_id": job_id, "status": status}
    if result is not None: d["result"] = result
    if error: d["error"] = error
    emit_serial(d); post("/mesh/result", d)

# ── Job dispatch ─────────────────────────────────────────────────────────────
def run_job(job):
    jid = job.get("job_id"); t = job.get("type"); p = job.get("payload") or {}
    try:
        if t == "identify":
            for _ in range(10):
                if LED: LED.value(not LED.value())
                time.sleep_ms(120)
            send_result(jid, "done")
        elif t == "reboot":
            send_result(jid, "done"); time.sleep(0.2); machine.reset()
        elif t == "read_sensor":
            send_telemetry(); send_result(jid, "done")
        elif t == "wifi_scan":
            try:
                if not wlan.active(): wlan.active(True)
                nets = wlan.scan()                  # (ssid, bssid, channel, rssi, authmode, hidden)
            except Exception as e:
                send_result(jid, "error", error="scan: " + str(e)); return
            import ubinascii
            aps = []
            for n in nets:
                try:
                    ssid = n[0].decode() if isinstance(n[0], (bytes, bytearray)) else str(n[0])
                    bssid = ubinascii.hexlify(n[1], ":").decode() if len(n) > 1 else ""
                    aps.append({"ssid": ssid, "bssid": bssid, "channel": n[2], "rssi": n[3],
                                "auth": n[4], "hidden": bool(n[5]) if len(n) > 5 else False})
                except Exception:
                    pass
            aps.sort(key=lambda a: a.get("rssi", -999), reverse=True)
            send_result(jid, "done", {"count": len(aps), "aps": aps[:32], "rssi": _rssi(),
                                      "ip": wlan.ifconfig()[0] if wlan.isconnected() else ""})
        elif t == "config":
            apply_config(p.get("config") or {}); send_result(jid, "done")
        elif t == "io_set":
            pin = int(p.get("pin")); mode = p.get("mode", "digital")
            if mode == "pwm":
                pw = machine.PWM(machine.Pin(pin)); pw.freq(1000); pw.duty(int(p.get("value", 0)))
            else:
                g = machine.Pin(pin, machine.Pin.OUT); v = str(p.get("value"))
                on = v in ("1", "on", "true", "True")
                if v == "toggle": on = not g.value()
                g.value(1 if on else 0)
            send_result(jid, "done", {"pin": pin})
        elif t == "io_read":
            pin = int(p.get("pin"))
            if p.get("analog"):
                val = machine.ADC(machine.Pin(pin)).read()
            else:
                val = machine.Pin(pin, machine.Pin.IN).value()
            post("/mesh/telemetry", {"node_id": NODE, "metrics": {"pin%d" % pin: val}})
            send_result(jid, "done", {"pin": pin, "value": val})
        elif t == "ota":
            mode = p.get("mode", "file"); url = p.get("url", "")
            if url.startswith("/"): url = SERVER + url
            if mode == "file":
                fn = p.get("filename", "main.py")
                r = requests.get(url); data = r.text; r.close()
                with open(fn, "w") as f: f.write(data)
                send_result(jid, "done", {"wrote": fn, "bytes": len(data)})
                time.sleep(0.3); machine.reset()
            else:
                send_result(jid, "error", error="bin OTA needs an OTA-partitioned build; use mode=file")
        elif t == "web_fetch":
            r = requests.get(p.get("url", "")); body = r.text[:400]; code = r.status_code; r.close()
            send_result(jid, "done", {"status_code": code, "len": len(body), "body": body})
        elif t == "control_set":
            if RELAY is None:
                send_result(jid, "error", error="no relay pin (set config.io.relay)")
            else:
                v = str(p.get("value")); on = v in ("1", "on", "true", "True")
                if v == "toggle": on = not RELAY.value()
                RELAY.value(1 if on else 0)
                send_result(jid, "done", {"channel": p.get("channel"), "value": on})
        elif t == "alert":
            for _ in range(6):
                if LED: LED.value(1)
                time.sleep_ms(90)
                if LED: LED.value(0)
                time.sleep_ms(90)
            send_result(jid, "done")
        elif t == "kiosk_set":
            send_result(jid, "done", {"shown": p.get("text") or p.get("url")})   # add a display driver here
        else:
            send_result(jid, "error", error="unknown type " + str(t))
    except Exception as e:
        send_result(jid, "error", error=str(e))

# ── Long-poll ────────────────────────────────────────────────────────────────
def poll_once():
    url = SERVER + "/mesh/poll?node_id=" + NODE + "&wait=25"
    if TOKEN and TOKEN != "open": url += "&token=" + TOKEN
    try:
        r = requests.get(url)
        try: data = r.json()
        except Exception: data = {}
        r.close()
        for job in data.get("jobs", []): run_job(job)
    except Exception:
        time.sleep(1)

# ── Serial provisioning ──────────────────────────────────────────────────────
def serial_announce():
    # Always print a hello over USB at boot so the host browser can bridge us to
    # Vera even when Wi-Fi can't reach the server (firewall).
    emit_serial({"kind": "hello", "node_id": NODE, "name": NODE, "board": board_name(),
                 "fw": "1.0-mpy", "modules": MODULES, "channels": ["serial"]})

def handle_serial():
    global WIFI_SSID, WIFI_PASS, SERVER, NODE, SERIAL_ONLY
    if not _spoll.poll(0): return
    try:
        line = sys.stdin.readline()
    except Exception:
        return
    if not line: return
    try: d = json.loads(line.strip())
    except Exception: return
    if d.get("cmd") == "provision":
        WIFI_SSID = d.get("ssid", WIFI_SSID); WIFI_PASS = d.get("pass", WIFI_PASS)
        SERVER = d.get("server", SERVER); NODE = d.get("node_id", NODE)
        try:
            with open("vera_cfg.json", "w") as f:
                f.write(json.dumps({"ssid": WIFI_SSID, "pass": WIFI_PASS, "server": SERVER, "node": NODE}))
        except Exception: pass
        wifi_connect()
    elif d.get("cmd") == "serial_mode":
        SERIAL_ONLY = bool(d.get("on", True))      # host relays us to the server (firewalled);
        serial_announce()                          # keep Wi-Fi UP so web_fetch/watch still work
    elif d.get("cmd") == "ping":
        serial_announce()
    elif "jobs" in d:
        for job in d["jobs"]: run_job(job)

def load_saved():
    global WIFI_SSID, WIFI_PASS, SERVER, NODE
    try:
        with open("vera_cfg.json") as f:
            d = json.load(f)
        WIFI_SSID = d.get("ssid", WIFI_SSID); WIFI_PASS = d.get("pass", WIFI_PASS)
        SERVER = d.get("server", SERVER); NODE = d.get("node", NODE)
    except Exception:
        pass

# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    global NODE, _last_tele, _last_watch
    load_saved()
    if not NODE or NODE == "{{NODE_ID}}": NODE = uid()
    serial_announce()                      # let a USB host bridge us even with no Wi-Fi
    if wifi_connect(): enroll()
    _enrolled = wlan.isconnected()
    while True:
        # Whole-loop guard: a transient error (Wi-Fi, network, a bad job) must
        # never crash the firmware — stay alive so serial + re-provision keep working.
        try:
            handle_serial()
            if SERIAL_ONLY:
                # Host relays us to Vera over USB. Skip the firewalled server poll/post,
                # but keep Wi-Fi up so the board's OWN jobs (web_fetch/watch) still work.
                if WIFI_SSID and not wlan.isconnected():
                    wifi_connect()
                if time.time() - _last_tele > telemetry_every:
                    send_telemetry(); _last_tele = time.time()    # prints over serial; post() is a no-op
                _worker_tick()
                time.sleep(0.15)
                continue
            if wlan.isconnected():
                if not _enrolled:
                    enroll(); _enrolled = True       # (re)enroll after a reconnect
                poll_once()
                if time.time() - _last_tele > telemetry_every:
                    send_telemetry(); _last_tele = time.time()
                if watch_every and watch_url and time.time() - _last_watch > watch_every:
                    try:
                        r = requests.get(watch_url); c = r.status_code; r.close()
                        if c < 200 or c >= 400:
                            post("/mesh/telemetry", {"node_id": NODE, "metrics": {"watch_fail": c}})
                    except Exception:
                        post("/mesh/telemetry", {"node_id": NODE, "metrics": {"watch_fail": -1}})
                    _last_watch = time.time()
                _worker_tick()
            else:
                _enrolled = False
                wifi_connect(); time.sleep(1.5)
        except Exception as e:
            try: sys.print_exception(e)
            except Exception: pass
            time.sleep(1)

main()
