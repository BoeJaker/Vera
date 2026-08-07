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
# Modules: sensor, web_fetch, watch, alert, control, kiosk (ILI9488 shield),
#          storage (SD over SPI).
# Display: 3.5" Arduino-Uno TFT shield — ILI9488 320x480, 8-bit parallel bus.
#          Self-contained write-only driver below; uses a @micropython.viper
#          fast path when available and falls back to plain Pin writes.
# Wire protocol: see vera/mesh/PROTOCOL.md
# ============================================================================

import network, time, json, os, sys, uselect, machine
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

# ── Display / SD pins — ESP32-S3 with Arduino-Uno footprint ─────────────────
# Board→shield mapping (override at runtime via config.io.tft / config.io.sd —
# no reflash needed):
#   control : io4→RST  io5→CS  io6→RS(DC)  io7→WR  io1→RD  (io2 spare / A5)
#   data    : io18→D2 io17→D3 io19→D4 io20→D5 io3→D6 io14→D7
#   sd      : io12→CLK io13→MISO io11→MOSI io10→CS
# d[0]=21 (LCD_D0/Uno D8), d[1]=46 (LCD_D1/Uno D9) — confirmed from silkscreen.
# NB: d[1]=46 is >31, so the fast viper blit is auto-disabled (it needs all data
# pins on GPIO 0-31); the driver falls back to the slower Pin.value path — fine
# for a status display. Remap at runtime with config.io.tft if needed.
TFT_PINS = {"rst": 5, "cs": 6, "dc": 7, "wr": 1, "rd": 2,     # hardware-verified S3-Uno control map
            "d": [21, 46, 18, 17, 19, 20, 3, 14]}   # NOTE: d[4]=19 & d[5]=20 are the native USB pins (see TFT_FREE_USB_PINS)
SD_PINS  = {"clk": 12, "miso": 13, "mosi": 11, "cs": 10}
TFT_ENABLED = True                 # set False if no display shield fitted
SD_ENABLED  = True

# ── Reclaiming GPIO19/20 from USB-Serial-JTAG ───────────────────────────────
# On the S3-Uno shield LCD_D4/D5 land on GPIO19/20 — the chip's native USB D-/D+.
# While the USB PHY owns that pad those two data bits are stuck, the parallel bus
# writes garbage, and the panel stays white. The Arduino build dodges this by
# compiling with USB CDC On Boot: Disabled; MicroPython can't, because its REPL
# IS the USB-Serial-JTAG device.
#
# So we take the pad by force: clear USB_SERIAL_JTAG_CONF0.USB_PAD_ENABLE and the
# pins become ordinary GPIO. The cost is real — **the USB REPL dies the moment
# this runs** and the node is reachable only over Wi-Fi (and UART0, see below).
# Recovery is always possible: hold BOOT, tap RESET, and the ROM bootloader
# re-enables the pad, so re-flashing from the panel still works.
#
# Off by default — turn it on from the panel's "Free USB pins" bake option (or
# set it here). It only fires when a data pin actually sits on 19/20.
TFT_FREE_USB_PINS = False
# Before dropping USB, hand the REPL to UART0 (S3 pins 43/44 — the devkit's
# second USB port / the Uno header's TX-RX pins) so you keep a console.
TFT_USB_REPL_TO_UART0 = True
# Seconds to wait first. Press any key on the USB console in that window to
# ABORT the takeover — an escape hatch when main.py is misbehaving.
TFT_FREE_USB_GRACE = 3

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

# Defaults chosen to NOT collide with the TFT/SD shield bus above (the old
# relay=5 default is now the TFT CS line). Classic-ESP32 users: adc 34 works;
# on S3 use an ADC1 pin or remap via config.io.
LED_PIN, RELAY_PIN, ADC_PIN = 2, 21, 15     # override via config.io = {led, relay, adc}
LED   = _mkpin(LED_PIN, machine.Pin.OUT)
RELAY = _mkpin(RELAY_PIN, machine.Pin.OUT)
ADC   = _mkadc(ADC_PIN)

# Bump on every firmware change. The server reads this same constant from the
# served main.py; if a node reports an older FW_VERSION and its config.ota.auto
# is set, Vera auto-queues an OTA file update. Keep the literal on one line.
FW_VERSION = "1.5.0-mpy"

MODULES = ["sensor", "web_fetch", "watch", "alert", "control", "io", "worker",
           "rgb", "ble", "toolkit", "position", "ui"]
# Onboard WS2812/NeoPixel: pin varies across ESP32-S3 boards. Best-guess order;
# overridable via config.io.neopixel or the neopixel_set job's `pin`. Use the
# neo_probe job (mesh.rgb.probe) to discover it if unknown.
NEO_CANDIDATES = [48, 38, 47, 21, 18, 8]
NEO_PIN = None                              # resolved lazily / by config / probe
cfg = {}
telemetry_every = 30
watch_url = ""; watch_every = 0
_last_tele = 0; _last_watch = 0
_worker_last = {}
_kiosk_mode = "status"     # "status" | "text" | "ui" (server-pushed screen)
_last_note = "boot"
_ui_screen = None          # last screen dict pushed via ui_screen
_ui_buttons = []           # [(x,y,w,h,action)] hitboxes for touch (increment 2)
AUDIO_PINS = {}            # config.io.audio — I2S mic/speaker (audio increment)
# 4-wire resistive touch. These shields share the touch wires with LCD pins; the
# classic mcufriend mapping on THIS board's GPIOs is XP=D6(3) YM=D7(14) YP=RS(6)
# XM=WR(7). YP & XM must be ADC-capable (GPIO1-10 on the S3). Override + tune via
# config.io.touch = {xp,ym,yp,xm, x0,x1,y0,y1, zmin,zmax, swap,invx,invy}.
TOUCH_PINS = {"xp": 3, "ym": 14, "yp": 6, "xm": 7}
TOUCH_CAL = {"x0": 320, "x1": 3800, "y0": 320, "y1": 3800,   # raw ADC at screen edges
             "zmin": 350, "zmax": 4095,                      # pressure gate (tune via touch_raw)
             "swap": 1, "invx": 0, "invy": 1}                # axis orientation vs rotation=1
_touch_last = 0; _touch_down = False
SERIAL_ONLY = False        # bridge mode: the host browser relays us to Vera over USB
                           # (set via {"cmd":"serial_mode","on":true}) — skip all Wi-Fi I/O.
_spoll = uselect.poll(); _spoll.register(sys.stdin, uselect.POLLIN)


# ════════════════════════════════════════════════════════════════════════════
# ILI9488 — 8-bit parallel (MCU 8080) write-only driver
# Fast path: byte→W1TS/W1TC mask LUT streamed by a viper loop (all data pins
# must be GPIO 0-31 — true on S3-Uno boards). Compiled via exec() so a port
# without the viper emitter degrades to the slow Pin.value path instead of
# killing the firmware at import.
# ════════════════════════════════════════════════════════════════════════════
# viper allows max 4 args: luts = lset[256]+lclr[256] concatenated,
# regs = [W1TS_addr, W1TC_addr, wr_mask]
_VIPER_SRC = """
import micropython
@micropython.viper
def _vblit(buf: ptr8, n: int, luts: ptr32, regs: ptr32):
    w1ts = ptr32(regs[0]); w1tc = ptr32(regs[1]); wrm = int(regs[2])
    for i in range(n):
        v = int(buf[i])
        w1ts[0] = int(luts[v]); w1tc[0] = int(luts[256 + v])
        w1tc[0] = wrm; w1tc[0] = wrm
        w1ts[0] = wrm
"""
_vblit = None
try:
    _g = {}
    exec(_VIPER_SRC, _g)
    _vblit = _g["_vblit"]
except Exception:
    _vblit = None

def _gpio_base():
    # OUT_W1TS = base+0x08, OUT_W1TC = base+0x0C on every ESP32 family chip
    try:
        import os
        m = os.uname().machine.lower()
    except Exception:
        m = ""
    if "s3" in m or "c3" in m or "c6" in m: return 0x60004000
    if "s2" in m: return 0x3F404000
    return 0x3FF44000                        # classic ESP32

class ILI9488P8:
    def __init__(self, pins):
        self.ok = False
        self.rot = 1
        self.W, self.H = 480, 320
        try:
            self._build(pins)
        except Exception as e:
            try: sys.print_exception(e)
            except Exception: pass

    def _build(self, pins):
        P = machine.Pin
        self.rst = _mkpin(pins.get("rst", -1), P.OUT)
        self.cs  = _mkpin(pins.get("cs",  -1), P.OUT)
        self.dc  = _mkpin(pins.get("dc",  -1), P.OUT)
        self.wr  = _mkpin(pins.get("wr",  -1), P.OUT)
        self.rd  = _mkpin(pins.get("rd",  -1), P.OUT)
        self.dp  = [_mkpin(n, P.OUT) for n in pins.get("d", [])]
        if not self.dc or not self.wr or len(self.dp) != 8 or None in self.dp:
            return
        for p in (self.cs, self.wr, self.rd, self.dc, self.rst):
            if p: p.value(1)
        # byte→mask LUTs for the viper path (lset[0..255] + lclr[256..511])
        import array
        dnums = pins["d"]; wrn = pins["wr"]
        self.fast = (_vblit is not None and wrn < 32 and
                     all(0 <= n < 32 for n in dnums))
        if self.fast:
            allm = 0
            for n in dnums: allm |= 1 << n
            self.luts = array.array("L", [0] * 512)
            for v in range(256):
                s = 0
                for b in range(8):
                    if v & (1 << b): s |= 1 << dnums[b]
                self.luts[v] = s
                self.luts[256 + v] = allm & ~s
            base = _gpio_base()
            self.regs = array.array("L", [base + 0x08, base + 0x0C, 1 << wrn])
        # hardware reset + init sequence
        if self.rst:
            self.rst.value(1); time.sleep_ms(5)
            self.rst.value(0); time.sleep_ms(20)
            self.rst.value(1); time.sleep_ms(120)
        if self.cs: self.cs.value(0)         # keep selected — we own the bus
        c, d = self.cmd, self.dat
        c(0xE0)
        for b in b"\x00\x03\x09\x08\x16\x0a\x3f\x78\x4c\x09\x0a\x08\x16\x1a\x0f": d(b)
        c(0xE1)
        for b in b"\x00\x16\x19\x03\x0f\x05\x32\x45\x46\x04\x0e\x0d\x35\x37\x0f": d(b)
        c(0xC0); d(0x17); d(0x15)
        c(0xC1); d(0x41)
        c(0xC5); d(0x00); d(0x12); d(0x80)
        c(0x3A); d(0x55)                     # RGB565 (parallel bus supports 16-bit)
        c(0xB0); d(0x00)
        c(0xB1); d(0xA0)
        c(0xB4); d(0x02)
        c(0xB6); d(0x02); d(0x02); d(0x3B)
        c(0xB7); d(0xC6)
        c(0xF7); d(0xA9); d(0x51); d(0x2C); d(0x82)
        c(0x11); time.sleep_ms(120)
        self.set_rotation(self.rot)
        c(0x29); time.sleep_ms(20)
        self.ok = True

    def _wr8_slow(self, v):
        for b in range(8):
            self.dp[b].value((v >> b) & 1)
        self.wr.value(0); self.wr.value(1)

    def send(self, buf):
        """Stream a bytes/bytearray of raw bus bytes (DC already high)."""
        if self.fast:
            _vblit(buf, len(buf), self.luts, self.regs)
        else:
            w = self._wr8_slow
            for v in buf: w(v)

    def cmd(self, v):
        self.dc.value(0); self.send(bytes([v])); self.dc.value(1)

    def dat(self, v):
        self.send(bytes([v]))

    def read_id(self):
        # Read controller ID regs (needs RD wired): 0xD3 → dummy + 3 ID bytes
        # (ILI9488=00 94 88, ILI9486=00 94 86, ILI9341=00 93 41). All 0x00 or all
        # 0xFF ⇒ the bus/RD isn't reading (wrong pins or write-only wiring).
        if not self.dc or not self.wr:
            return None
        out = {}
        for reg in (0xD3, 0x04):
            self.cmd(reg)
            for p in self.dp:
                if p: p.init(machine.Pin.IN)
            vals = []
            for _ in range(4):
                if self.rd: self.rd.value(0)
                time.sleep_us(2)
                v = 0
                for b in range(8):
                    if self.dp[b] and self.dp[b].value(): v |= (1 << b)
                if self.rd: self.rd.value(1)
                time.sleep_us(2)
                vals.append(v)
            for p in self.dp:
                if p: p.init(machine.Pin.OUT)
            out["0x%02X" % reg] = vals
        return out

    def set_rotation(self, r):
        self.rot = r & 3
        self.cmd(0x36); self.dat((0x48, 0x28, 0x88, 0xE8)[self.rot])
        if self.rot & 1: self.W, self.H = 480, 320
        else:            self.W, self.H = 320, 480

    def window(self, x, y, w, h):
        x2, y2 = x + w - 1, y + h - 1
        self.cmd(0x2A); self.send(bytes([x >> 8, x & 255, x2 >> 8, x2 & 255]))
        self.cmd(0x2B); self.send(bytes([y >> 8, y & 255, y2 >> 8, y2 & 255]))
        self.cmd(0x2C)

    def fill_rect(self, x, y, w, h, color):
        if not self.ok: return
        if x < 0: w += x; x = 0
        if y < 0: h += y; y = 0
        w = min(w, self.W - x); h = min(h, self.H - y)
        if w <= 0 or h <= 0: return
        self.window(x, y, w, h)
        row = bytes([color >> 8, color & 255]) * w
        for _ in range(h): self.send(row)

    def fill(self, color):
        self.fill_rect(0, 0, self.W, self.H, color)

    def text(self, x, y, s, color, bg, scale=2):
        """Word-wrapped text via framebuf's built-in 8x8 font, integer scale."""
        if not self.ok: return y
        import framebuf
        sw = lambda c: ((c >> 8) | (c << 8)) & 0xFFFF   # LE framebuf → bus byte order
        maxc = max(1, (self.W - x - 2) // (8 * scale))
        lines = []
        for raw in str(s).split("\n"):
            while len(raw) > maxc:
                lines.append(raw[:maxc]); raw = raw[maxc:]
            lines.append(raw)
        for line in lines:
            if y + 8 * scale > self.H: break
            if line:
                wpx = 8 * len(line)
                buf = bytearray(wpx * 8 * 2)
                fb = framebuf.FrameBuffer(buf, wpx, 8, framebuf.RGB565)
                fb.fill(sw(bg)); fb.text(line, 0, 0, sw(color))
                if scale == 1:
                    self.window(x, y, wpx, 8); self.send(buf)
                else:
                    orow = bytearray(wpx * scale * 2)
                    for r in range(8):
                        o = 0
                        for px in range(wpx):
                            i = (r * wpx + px) * 2
                            for _ in range(scale):
                                orow[o] = buf[i]; orow[o + 1] = buf[i + 1]; o += 2
                        self.window(x, y + r * scale, wpx * scale, scale)
                        for _ in range(scale): self.send(orow)
            y += 8 * scale + scale
        return y

# RGB565 palette
C_BLACK, C_WHITE, C_GREEN, C_RED, C_YELL, C_GREY = 0x0000, 0xFFFF, 0x07E0, 0xF800, 0xFFE0, 0x8410
C_NAVY, C_CYAN, C_BLUE = 0x000F, 0x07FF, 0x001F      # used by the server-driven UI widgets

TFT = None
_usb_freed = None                    # None=not attempted, True=pad released, "…"=why not

# USB_SERIAL_JTAG_CONF0_REG per chip; bit 14 = USB_PAD_ENABLE (the switch that
# hands GPIO19/20 to the USB PHY). Only the chips whose REPL lives on USB-JTAG.
_USB_JTAG_CONF0 = {"esp32s3": 0x60038018, "esp32c3": 0x60043018, "esp32c6": 0x6000F018}
_USB_PAD_ENABLE = 1 << 14

def _usb_jtag_conf0():
    try:
        m = os.uname().machine.lower()
    except Exception:
        return 0
    for k, addr in _USB_JTAG_CONF0.items():
        if k[5:] in m:               # "s3" / "c3" / "c6"
            return addr
    return 0

def _free_usb_pads(pins):
    """Release GPIO19/20 from the USB-Serial-JTAG PHY so the parallel bus can
    drive them. Returns True on success, else a string saying why not. Read the
    TFT_FREE_USB_PINS notes above first — this kills the USB REPL."""
    global _usb_freed
    dpins = list(pins.get("d") or [])
    if not any(n in (19, 20) for n in dpins):
        _usb_freed = "not needed (no data pin on 19/20)"
        return _usb_freed
    if not TFT_FREE_USB_PINS:
        _usb_freed = "disabled (TFT_FREE_USB_PINS=False)"
        return _usb_freed
    addr = _usb_jtag_conf0()
    if not addr:
        _usb_freed = "unsupported chip"
        return _usb_freed

    # Escape hatch: any keypress on the console in the grace window aborts, so a
    # bad main.py can't lock you out of the REPL without a BOOT-button reflash.
    if TFT_FREE_USB_GRACE > 0:
        print("[usb] releasing GPIO19/20 from USB-Serial-JTAG in %ds — the USB REPL "
              "will DIE. Press any key to abort." % TFT_FREE_USB_GRACE)
        try:
            poll = uselect.poll(); poll.register(sys.stdin, uselect.POLLIN)
            if poll.poll(TFT_FREE_USB_GRACE * 1000):
                try: sys.stdin.read(1)
                except Exception: pass
                print("[usb] aborted — USB REPL kept, display will not work on 19/20")
                _usb_freed = "aborted by keypress"
                return _usb_freed
        except Exception:
            time.sleep(TFT_FREE_USB_GRACE)

    # Keep a console: move the REPL to UART0 before the USB pad goes away.
    if TFT_USB_REPL_TO_UART0:
        try:
            _u = machine.UART(0, 115200)
            os.dupterm(_u, 1)
            print("[usb] REPL also on UART0 (115200)")
        except Exception as e:
            print("[usb] UART0 REPL unavailable:", e)
    try:
        machine.mem32[addr] = machine.mem32[addr] & ~_USB_PAD_ENABLE
        _usb_freed = True
        print("[usb] USB pad disabled — GPIO19/20 are now plain GPIO")
    except Exception as e:
        _usb_freed = "failed: %s" % e
    return _usb_freed

def tft_init(pins=None):
    global TFT
    if not TFT_ENABLED: return
    pins = pins or TFT_PINS
    try:
        _free_usb_pads(pins)         # must happen BEFORE the data pins are claimed
    except Exception as e:
        print("[usb] pad release error:", e)
    try:
        TFT = ILI9488P8(pins)
        if not TFT.ok: TFT = None
    except Exception:
        TFT = None

def dsp_text(title, body, color=C_GREEN, bg=C_BLACK, size=2):
    if not TFT: return
    try:
        TFT.fill(bg)
        y = 8
        if title:
            y = TFT.text(8, y, title, C_YELL, bg, size + 1) + 6
            TFT.fill_rect(8, y, TFT.W - 16, 2, C_GREY); y += 8
        TFT.text(8, y, body, color, bg, size)
    except Exception:
        pass

def dsp_alert(msg):
    global _kiosk_mode
    if not TFT: return
    try:
        TFT.fill(C_RED)
        TFT.text(10, 12, "! ALERT", C_WHITE, C_RED, 4)
        TFT.text(10, 60, str(msg), C_WHITE, C_RED, 3)
        _kiosk_mode = "text"      # hold until the next kiosk_set
    except Exception:
        pass

def dsp_status():
    if not TFT: return
    try:
        TFT.fill(C_BLACK)
        TFT.text(8, 8, "VERA NODE", C_GREEN, C_BLACK, 3)
        TFT.fill_rect(8, 40, TFT.W - 16, 2, C_GREY)
        ip = wlan.ifconfig()[0] if wlan.isconnected() else "(offline)"
        sd = "none"
        if _sd_ok:
            try:
                import os
                st = os.statvfs("/sd")
                tot = st[0] * st[2] // 1048576; free = st[0] * st[3] // 1048576
                sd = "%d/%d MB" % (tot - free, tot)
            except Exception:
                pass
        body = ("node   %s\nip     %s  rssi %s\nserver %s\nuptime %ds\nsd     %s\nlast   %s"
                % (NODE, ip, _rssi(), SERVER, time.time(), sd, _last_note))
        TFT.text(8, 52, body, C_WHITE, C_BLACK, 2)
    except Exception:
        pass

def dsp_test():
    # Bring-up test pattern: colour bars + border + the live pin map. ANY output
    # other than a blank white screen proves the parallel bus is alive — use it to
    # dial in a new board's pins/rotation, then Save the working map. Swapped
    # colours = RGB/BGR flag; a mirrored image = wrong rotation (both fixable live).
    global _kiosk_mode
    if not TFT: return
    try:
        TFT.fill(C_BLACK)
        bars = (C_RED, C_GREEN, C_BLUE, C_YELL, C_CYAN, 0xF81F, C_WHITE, C_GREY)
        bw = TFT.W // 8
        for i in range(8):
            TFT.fill_rect(i * bw, 0, bw, TFT.H // 2, bars[i])
        TFT.fill_rect(0, 0, TFT.W, 2, C_WHITE); TFT.fill_rect(0, TFT.H - 2, TFT.W, 2, C_WHITE)
        TFT.fill_rect(0, 0, 2, TFT.H, C_WHITE);  TFT.fill_rect(TFT.W - 2, 0, 2, TFT.H, C_WHITE)
        y = TFT.H // 2 + 8
        TFT.text(8, y, "VERA DISPLAY TEST", C_WHITE, C_BLACK, 2); y += 26
        TFT.text(8, y, "%dx%d  rot %d" % (TFT.W, TFT.H, TFT.rot), C_GREEN, C_BLACK, 2); y += 24
        pn = TFT_PINS
        TFT.text(8, y, "WR%s DC%s CS%s RST%s RD%s" % (pn["wr"], pn["dc"], pn["cs"], pn["rst"], pn["rd"]),
                 C_YELL, C_BLACK, 1); y += 14
        TFT.text(8, y, "D:" + ",".join(str(d) for d in pn["d"]), C_YELL, C_BLACK, 1)
        _kiosk_mode = "text"      # hold the pattern until the next kiosk_set / status
    except Exception:
        pass

# ── SD card (shield slot, SPI) ───────────────────────────────────────────────
_sd = None; _sd_ok = False
def sd_mount(pins=None):
    global _sd, _sd_ok
    if not SD_ENABLED: return False
    p = pins or SD_PINS
    import os
    try:
        try: os.umount("/sd")
        except Exception: pass
        _sd = machine.SDCard(slot=2, sck=p["clk"], miso=p["miso"], mosi=p["mosi"], cs=p["cs"])
        os.mount(_sd, "/sd")
        _sd_ok = True
    except Exception:
        _sd_ok = False
    return _sd_ok

def _sd_path(p):
    p = str(p or "/")
    if not p.startswith("/"): p = "/" + p
    return p if p.startswith("/sd") else "/sd" + p


# ════════════════════════════════════════════════════════════════════════════
# ESP32 toolkit — RGB/NeoPixel, BLE, I2C, touch, temp, ESP-NOW, survey, sysinfo.
# Every handler is wrapped by run_job's try/except; each degrades gracefully if
# a feature is absent on this chip/build. (CSI + promiscuous sniff need the
# Arduino/IDF firmware — MicroPython doesn't expose them.)
# ════════════════════════════════════════════════════════════════════════════
_neo = None; _neo_pin = None

def _neo_get(pin=None, n=1):
    """Return a NeoPixel driver, resolving the data pin from arg/config/candidates."""
    global _neo, _neo_pin, NEO_PIN
    import neopixel
    want = pin if pin is not None else (NEO_PIN if NEO_PIN is not None else NEO_CANDIDATES[0])
    if _neo is not None and _neo_pin == want and _neo.n >= n:
        return _neo
    _neo = neopixel.NeoPixel(machine.Pin(int(want), machine.Pin.OUT), max(1, n))
    _neo_pin = want; NEO_PIN = want
    return _neo

def _neo_fill(np, r, g, b):
    for i in range(np.n): np[i] = (r, g, b)
    np.write()

def _neo_effect(np, r, g, b, effect, bright):
    r = r * bright // 255; g = g * bright // 255; b = b * bright // 255
    if effect == "off":
        _neo_fill(np, 0, 0, 0)
    elif effect == "blink":
        for _ in range(4):
            _neo_fill(np, r, g, b); time.sleep_ms(150)
            _neo_fill(np, 0, 0, 0); time.sleep_ms(150)
        _neo_fill(np, r, g, b)
    elif effect == "breathe":
        for k in list(range(0, 256, 32)) + list(range(255, -1, -32)):
            _neo_fill(np, r * k // 255, g * k // 255, b * k // 255); time.sleep_ms(40)
        _neo_fill(np, r, g, b)
    elif effect == "rainbow":
        for j in range(0, 256, 8):
            for i in range(np.n):
                h = (i * 256 // max(1, np.n) + j) & 255
                np[i] = _wheel(h, bright)
            np.write(); time.sleep_ms(20)
    else:                       # solid
        _neo_fill(np, r, g, b)

def _wheel(pos, bright=255):
    pos = 255 - pos
    if pos < 85:   c = (255 - pos * 3, 0, pos * 3)
    elif pos < 170: pos -= 85; c = (0, pos * 3, 255 - pos * 3)
    else:          pos -= 170; c = (pos * 3, 255 - pos * 3, 0)
    return tuple(x * bright // 255 for x in c)

def _ble_name(adv):
    i = 0
    try:
        while i + 1 < len(adv):
            ln = adv[i]
            if ln == 0: break
            t = adv[i + 1]
            if t in (0x08, 0x09):               # shortened / complete local name
                return bytes(adv[i + 2:i + 1 + ln]).decode("utf-8", "ignore")
            i += 1 + ln
    except Exception:
        pass
    return ""

def _ble_scan(seconds=5, active=False):
    import bluetooth
    ble = bluetooth.BLE(); ble.active(True)
    found = {}; done = [False]
    def _irq(event, data):
        if event == 5:                          # _IRQ_SCAN_RESULT
            addr_type, addr, adv_type, rssi, adv = data
            mac = ":".join("%02x" % x for x in bytes(addr))
            e = found.get(mac) or {"mac": mac, "rssi": rssi, "name": ""}
            e["rssi"] = rssi
            nm = _ble_name(bytes(adv))
            if nm: e["name"] = nm
            found[mac] = e
        elif event == 6:                        # _IRQ_SCAN_DONE
            done[0] = True
    ble.irq(_irq)
    try:
        ble.gap_scan(int(seconds * 1000), 30000, 30000, bool(active))
    except TypeError:
        ble.gap_scan(int(seconds * 1000))
    t0 = time.time()
    while not done[0] and time.time() - t0 < seconds + 1:
        time.sleep_ms(50)
    try: ble.gap_scan(None)
    except Exception: pass
    ble.active(False)
    return list(found.values())

def _wifi_aps():
    try:
        if not wlan.active(): wlan.active(True)
        out = []
        for n in wlan.scan():
            import ubinascii
            out.append({"ssid": n[0].decode() if isinstance(n[0], (bytes, bytearray)) else str(n[0]),
                        "bssid": ubinascii.hexlify(n[1], ":").decode(),
                        "channel": n[2], "rssi": n[3], "auth": n[4]})
        return out
    except Exception:
        return []

# ════════════════════════════════════════════════════════════════════════════
# Server-driven UI — Vera pushes a "screen" (list of widgets) via the ui_screen
# job; the node just renders it and (increment 2) reports touches as ui events.
# Widget types: label, rect, hline, button, bar. Colours are RGB565 ints.
# This keeps all app logic (sysmon, macropad, chat, companion) on Vera.
# ════════════════════════════════════════════════════════════════════════════
def _ui_frame(x, y, w, h, c):
    TFT.fill_rect(x, y, w, 1, c); TFT.fill_rect(x, y + h - 1, w, 1, c)
    TFT.fill_rect(x, y, 1, h, c); TFT.fill_rect(x + w - 1, y, 1, h, c)

def _ui_widget(wd, bg):
    t = wd.get("t"); x = int(wd.get("x", 0)); y = int(wd.get("y", 0))
    if t == "label":
        TFT.text(x, y, str(wd.get("text", "")), int(wd.get("color", C_WHITE)),
                 int(wd.get("bg", bg)), int(wd.get("size", 2)))
    elif t == "rect":
        w = int(wd.get("w", 10)); h = int(wd.get("h", 10)); c = int(wd.get("color", C_GREY))
        if wd.get("fill", True): TFT.fill_rect(x, y, w, h, c)
        else: _ui_frame(x, y, w, h, c)
    elif t == "hline":
        TFT.fill_rect(x, y, int(wd.get("w", TFT.W - x - 6)), int(wd.get("h", 2)),
                      int(wd.get("color", C_GREY)))
    elif t == "button":
        w = int(wd.get("w", 96)); h = int(wd.get("h", 36))
        TFT.fill_rect(x, y, w, h, int(wd.get("bg", C_NAVY)))
        _ui_frame(x, y, w, h, int(wd.get("color", C_GREEN)))
        TFT.text(x + 6, y + h // 2 - 4 * int(wd.get("size", 2)), str(wd.get("text", "")),
                 int(wd.get("color", C_WHITE)), int(wd.get("bg", C_NAVY)), int(wd.get("size", 2)))
        if wd.get("action"): _ui_buttons.append((x, y, w, h, wd["action"]))
    elif t == "bar":
        w = int(wd.get("w", 140)); h = int(wd.get("h", 12))
        val = max(0, min(100, int(wd.get("val", 0))))
        if wd.get("label"): TFT.text(x, y - 16, str(wd["label"]), C_WHITE, bg, 1)
        _ui_frame(x, y, w, h, C_GREY)
        TFT.fill_rect(x + 1, y + 1, (w - 2) * val // 100, h - 2, int(wd.get("color", C_GREEN)))

def ui_render(scr):
    """Render a pushed screen dict: {title?, bg?, widgets:[...]}."""
    global _ui_screen, _ui_buttons, _kiosk_mode
    if not TFT or not isinstance(scr, dict): return
    _ui_screen = scr; _ui_buttons = []; _kiosk_mode = "ui"
    bg = int(scr.get("bg", C_BLACK))
    TFT.fill(bg)
    y0 = 6
    if scr.get("title"):
        TFT.text(6, y0, str(scr["title"]), int(scr.get("title_color", C_YELL)), bg, 3)
        TFT.fill_rect(6, y0 + 28, TFT.W - 12, 2, C_GREY)
    for wd in scr.get("widgets", []):
        try: _ui_widget(wd, bg)
        except Exception: pass


# ── 4-wire resistive touch ───────────────────────────────────────────────────
# The touch wires share LCD pins, so a read reconfigures them; _touch_restore
# puts them back to OUTPUT afterwards so the next TFT render isn't corrupted.
def _adc(pin):
    a = machine.ADC(machine.Pin(pin))
    try: a.atten(machine.ADC.ATTN_11DB)
    except Exception: pass
    return a

def _touch_restore():
    for k in ("xp", "ym", "yp", "xm"):
        try: machine.Pin(int(TOUCH_PINS[k]), machine.Pin.OUT)
        except Exception: pass

def _touch_raw():
    """Return (x, y, z) raw ADC or None. z is a pressure proxy (higher = harder)."""
    try:
        P = machine.Pin
        xp = int(TOUCH_PINS["xp"]); yp = int(TOUCH_PINS["yp"])
        xm = int(TOUCH_PINS["xm"]); ym = int(TOUCH_PINS["ym"])
        # X: drive across the X plate (XP=1,XM=0), measure on YP
        P(xp, P.OUT).value(1); P(xm, P.OUT).value(0); P(ym, P.IN)
        a = _adc(yp); time.sleep_us(60); x = (a.read() + a.read()) // 2
        # Y: drive across the Y plate (YP=1,YM=0), measure on XM
        P(yp, P.OUT).value(1); P(ym, P.OUT).value(0); P(xp, P.IN)
        a = _adc(xm); time.sleep_us(60); y = (a.read() + a.read()) // 2
        # Z: XP=0, YM=1, pressure ∝ (z2 - z1)
        P(xp, P.OUT).value(0); P(ym, P.OUT).value(1)
        z1 = _adc(xm).read(); z2 = _adc(yp).read()
        # Inverted on purpose: z2-z1 is LARGE with nothing touching (open
        # circuit) and small under a press, so pressure is ADC_MAX - (z2-z1).
        # The raw difference made an untouched panel look like maximum force.
        z = 4095 - (z2 - z1)
        if z < 0: z = 0
        _touch_restore()
        return (x, y, z)
    except Exception:
        _touch_restore()
        return None

def _touch_point():
    """Map a raw read to screen (x, y) if pressed, else None."""
    r = _touch_raw()
    if not r or not TFT: return None
    x, y, z = r
    if z < TOUCH_CAL["zmin"] or z > TOUCH_CAL["zmax"]:
        return None
    def _sc(v, lo, hi, span):
        v = max(lo, min(hi, v))
        return int((v - lo) * span // max(1, (hi - lo)))
    sx = _sc(x, TOUCH_CAL["x0"], TOUCH_CAL["x1"], TFT.W)
    sy = _sc(y, TOUCH_CAL["y0"], TOUCH_CAL["y1"], TFT.H)
    if TOUCH_CAL.get("swap"): sx, sy = sy * TFT.W // max(1, TFT.H), sx * TFT.H // max(1, TFT.W)
    if TOUCH_CAL.get("invx"): sx = TFT.W - sx
    if TOUCH_CAL.get("invy"): sy = TFT.H - sy
    return (max(0, min(TFT.W - 1, sx)), max(0, min(TFT.H - 1, sy)))

def _ui_send_event(action):
    d = {"kind": "ui_event", "node_id": NODE, "action": action}
    emit_serial(d)
    if not SERIAL_ONLY:
        post("/mesh/ui/event", d)

def _touch_tick():
    """Poll touch in UI mode, hit-test buttons, emit one event per press."""
    global _touch_last, _touch_down
    if _kiosk_mode != "ui" or not TFT or not _ui_buttons:
        return
    if time.ticks_diff(time.ticks_ms(), _touch_last) < 90:
        return
    _touch_last = time.ticks_ms()
    pt = _touch_point()
    if not pt:
        _touch_down = False                          # released → re-arm
        return
    if _touch_down:
        return                                       # debounce: one event per press
    _touch_down = True
    sx, sy = pt
    for (bx, by, bw, bh, action) in _ui_buttons:
        if bx <= sx < bx + bw and by <= sy < by + bh:
            try:                                     # brief visual feedback
                TFT.fill_rect(bx, by, bw, 3, C_WHITE)
            except Exception: pass
            _ui_send_event(action)
            return


def _toolkit_job(t, p, jid):
    """Handle a toolkit job type. Returns True if it matched, else False."""
    global NEO_PIN, _kiosk_mode
    if t == "ui_screen":
        if TFT is None:
            send_result(jid, "error", error="no display"); return True
        ui_render(p.get("screen") or p)
        send_result(jid, "done", {"ok": True, "buttons": len(_ui_buttons)}); return True
    if t == "ui_clear":
        _kiosk_mode = "status"
        if TFT: dsp_status()
        send_result(jid, "done"); return True
    if t == "touch_raw":
        # Calibration helper: touch the screen while this runs to see raw values.
        samples = []
        for _ in range(int(p.get("samples", 5))):
            r = _touch_raw()
            if r: samples.append(r)
            time.sleep_ms(80)
        best = max(samples, key=lambda s: s[2]) if samples else None
        send_result(jid, "done", {"raw": best, "all": samples, "cal": TOUCH_CAL,
                                  "pins": TOUCH_PINS}); return True
    if t == "touch_cal":
        for k in ("x0", "x1", "y0", "y1", "zmin", "zmax", "swap", "invx", "invy"):
            if k in p: TOUCH_CAL[k] = int(p[k])
        send_result(jid, "done", {"cal": TOUCH_CAL}); return True
    if t == "neopixel_set":
        np = _neo_get(p.get("pin"), int(p.get("n", 1) or 1))
        _neo_effect(np, int(p.get("r", 0)), int(p.get("g", 0)), int(p.get("b", 0)),
                    p.get("effect", "solid"), int(p.get("brightness", 255)))
        send_result(jid, "done", {"pin": _neo_pin, "n": np.n}); return True
    if t == "neo_probe":
        pins = p.get("pins") or NEO_CANDIDATES
        dwell = int(p.get("dwell_ms", 1000))
        lit = []
        for pin in pins:
            try:
                np = neo = _neo_get(int(pin), 1)
                _neo_fill(np, 0, 80, 0); emit_serial({"kind": "probe", "node_id": NODE, "pin": pin})
                time.sleep_ms(dwell); _neo_fill(np, 0, 0, 0)
                lit.append(pin)
            except Exception:
                pass
        global _neo
        _neo = None                              # force re-init on next real use
        send_result(jid, "done", {"probed": lit, "hint": "note which pin lit the LED, then set config.io.neopixel"})
        return True
    if t == "ble_scan":
        devs = _ble_scan(int(p.get("seconds", 5)), bool(p.get("active")))
        send_result(jid, "done", {"devices": devs, "count": len(devs)}); return True
    if t == "i2c_scan":
        sda = int(p.get("sda", 8)); scl = int(p.get("scl", 9))
        i2c = machine.SoftI2C(scl=machine.Pin(scl), sda=machine.Pin(sda), freq=int(p.get("freq", 100000)))
        addrs = ["0x%02x" % a for a in i2c.scan()]
        send_result(jid, "done", {"sda": sda, "scl": scl, "addresses": addrs}); return True
    if t == "touch_read":
        pin = int(p.get("pin"))
        val = machine.TouchPad(machine.Pin(pin)).read()
        post("/mesh/telemetry", {"node_id": NODE, "metrics": {"touch%d" % pin: val}})
        send_result(jid, "done", {"pin": pin, "value": val}); return True
    if t == "temp_read":
        try:
            import esp32; temp = esp32.mcu_temperature()
        except Exception:
            import esp32; temp = (esp32.raw_temperature() - 32) * 5.0 / 9.0
        post("/mesh/telemetry", {"node_id": NODE, "metrics": {"mcu_temp": temp}})
        send_result(jid, "done", {"mcu_temp": temp}); return True
    if t == "channel_survey":
        chans = {}
        for ap in _wifi_aps():
            c = ap.get("channel") or 0; chans.setdefault(c, {"count": 0, "max_rssi": -999})
            chans[c]["count"] += 1; chans[c]["max_rssi"] = max(chans[c]["max_rssi"], ap.get("rssi", -999))
        # NB: no {**v} dict-unpacking here — MicroPython's parser rejects it.
        survey = [{"channel": c, "count": chans[c]["count"], "max_rssi": chans[c]["max_rssi"]}
                  for c in sorted(chans)]
        clear = min(survey, key=lambda s: (s["count"], s["max_rssi"]))["channel"] if survey else None
        send_result(jid, "done", {"survey": survey, "clearest": clear}); return True
    if t == "rf_range":
        target = (p.get("target") or "").lower(); kind = p.get("kind", "wifi")
        samples = int(p.get("samples", 4)); vals = []
        for _ in range(samples):
            if kind == "ble":
                for d in _ble_scan(2, False):
                    if d.get("mac", "").lower() == target: vals.append(d["rssi"])
            else:
                for ap in _wifi_aps():
                    if ap.get("bssid", "").lower() == target: vals.append(ap["rssi"])
            time.sleep_ms(120)
        if vals:
            rssi = sorted(vals)[len(vals) // 2]     # median
            send_result(jid, "done", {"target": target, "kind": kind, "rssi": rssi, "n": len(vals)})
        else:
            send_result(jid, "error", error="target not seen: " + target)
        return True
    if t == "espnow_ping":
        try:
            import espnow
        except Exception:
            send_result(jid, "error", error="espnow not available"); return True
        if not wlan.active(): wlan.active(True)
        e = espnow.ESPNow(); e.active(True)
        peer = p.get("peer", "broadcast")
        mac = b"\xff" * 6 if peer == "broadcast" else bytes(int(x, 16) for x in peer.replace(":", " ").split())
        try: e.add_peer(mac)
        except Exception: pass
        sent = 0
        for _ in range(int(p.get("count", 10))):
            try: e.send(mac, b"vera-range", False); sent += 1
            except Exception: pass
            time.sleep_ms(60)
        peers = []
        try:
            for pk, pv in (e.peers_table or {}).items():
                peers.append({"mac": ":".join("%02x" % x for x in pk), "rssi": pv[0]})
        except Exception:
            pass
        e.active(False)
        send_result(jid, "done", {"sent": sent, "peer": peer, "peers": peers}); return True
    if t == "sysinfo":
        import gc
        info = {"node_id": NODE, "board": board_name(), "mac": uid()}
        try: info["release"] = __import__("os").uname().release
        except Exception: pass
        try: info["freq_mhz"] = machine.freq() // 1000000
        except Exception: pass
        try: info["heap_free"] = gc.mem_free()
        except Exception: pass
        try:
            import esp32
            info["flash_mb"] = esp32.flash_size() // 1048576
        except Exception: pass
        # Whether GPIO19/20 were taken back off the USB PHY — the one fact that
        # decides if this build can drive an S3-Uno parallel TFT at all.
        info["usb_pads_freed"] = _usb_freed
        info["display"] = bool(TFT)
        send_result(jid, "done", info); return True
    if t == "deep_sleep":
        secs = int(p.get("seconds", 0))
        send_result(jid, "done", {"sleeping_s": secs})
        time.sleep_ms(200); machine.deepsleep(secs * 1000); return True
    if t in ("csi_start", "csi_stop", "sniff"):
        send_result(jid, "error", error=t + " needs the Arduino/IDF firmware (MicroPython can't do CSI/promiscuous)")
        return True
    return False


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
    global TOKEN, _last_note
    d = {"kind": "hello", "node_id": NODE, "name": NODE, "board": board_name(),
         "fw": FW_VERSION, "runtime": "micropython",   # picks the OTA artifact kind
         "ip": wlan.ifconfig()[0] if wlan.isconnected() else "",
         "rssi": _rssi(), "modules": MODULES, "channels": ["http", "serial"]}
    if TOKEN and TOKEN != "open": d["token"] = TOKEN
    emit_serial(d)
    r = post("/mesh/hello", d)
    if isinstance(r, dict):
        if r.get("token"): TOKEN = r["token"]
        apply_config(r.get("config") or {})
    _last_note = "enrolled"
    if _kiosk_mode == "status": dsp_status()

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
    if "neopixel" in io:
        global NEO_PIN, _neo
        NEO_PIN = int(io["neopixel"]); _neo = None    # re-init on next use
    # Spare S3 GPIOs on this Uno board (io35-42, 45, 16, 15, 47, 48) are free for
    # an I2S mic (INMP441) + I2S amp (MAX98357) and a touch controller. Pins are
    # captured here for the audio/touch increment; e.g.
    #   config.io.audio = {"i2s_bck":15, "i2s_ws":16, "i2s_din":42, "i2s_dout":45}
    #   config.io.touch = {"cs":47, "irq":48} (XPT2046) or {"yp":..,"xm":..,..}
    if isinstance(io.get("audio"), dict):
        global AUDIO_PINS; AUDIO_PINS = io["audio"]
    if isinstance(io.get("touch"), dict):
        tc = io["touch"]
        for k in ("xp", "ym", "yp", "xm"):
            if k in tc: TOUCH_PINS[k] = int(tc[k])
        for k in ("x0", "x1", "y0", "y1", "zmin", "zmax", "swap", "invx", "invy"):
            if k in tc: TOUCH_CAL[k] = int(tc[k])
    if isinstance(io.get("tft"), dict):      # remap the display bus, re-init
        t = io["tft"]
        for k in ("rst", "cs", "dc", "wr", "rd"):
            if k in t: TFT_PINS[k] = int(t[k])
        if "rs" in t: TFT_PINS["dc"] = int(t["rs"])   # shield naming
        if isinstance(t.get("d"), list) and len(t["d"]) == 8:
            TFT_PINS["d"] = [int(n) for n in t["d"]]
        tft_init()
        if _kiosk_mode == "status": dsp_status()
    if isinstance(io.get("sd"), dict):
        for k in ("clk", "miso", "mosi", "cs"):
            if k in io["sd"]: SD_PINS[k] = int(io["sd"][k])
        sd_mount()
    k = cfg.get("kiosk") or {}
    if TFT and "rotation" in k:
        try: TFT.set_rotation(int(k["rotation"]))
        except Exception: pass
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
    if _sd_ok:
        try:
            import os
            st = os.statvfs("/sd")
            metrics["sd_total_mb"] = st[0] * st[2] // 1048576
            metrics["sd_used_mb"]  = (st[0] * st[2] - st[0] * st[3]) // 1048576
        except Exception:
            pass
    d = {"kind": "telemetry", "node_id": NODE, "rssi": _rssi(), "metrics": metrics}
    emit_serial(d); post("/mesh/telemetry", d)
    if _kiosk_mode == "status": dsp_status()

def send_result(job_id, status, result=None, error=""):
    d = {"kind": "result", "node_id": NODE, "job_id": job_id, "status": status}
    if result is not None: d["result"] = result
    if error: d["error"] = error
    emit_serial(d); post("/mesh/result", d)

# ── Job dispatch ─────────────────────────────────────────────────────────────
def run_job(job):
    global _kiosk_mode, _last_note
    jid = job.get("job_id"); t = job.get("type"); p = job.get("payload") or {}
    _last_note = str(t)
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
            dsp_alert(p.get("message", "alert"))
            send_result(jid, "done")
        elif t == "kiosk_set":
            # payload: {text?, title?, url?, mode:"status"?, color?, bg?, size?, rotation?}
            if TFT is None:
                send_result(jid, "error", error="no display (kiosk disabled or init failed)")
                return
            if "rotation" in p:
                try: TFT.set_rotation(int(p["rotation"]))
                except Exception: pass
            if p.get("mode") == "status":
                _kiosk_mode = "status"; dsp_status()
                send_result(jid, "done", {"shown": "status"})
            elif p.get("mode") == "test":
                dsp_test()
                send_result(jid, "done", {"shown": "test-pattern"})
            else:
                _kiosk_mode = "text"
                txt = p.get("text") or (("URL:\n" + p["url"]) if p.get("url") else "")
                dsp_text(p.get("title", ""), txt,
                         int(p.get("color", C_GREEN)), int(p.get("bg", C_BLACK)),
                         int(p.get("size", 2)))
                send_result(jid, "done", {"shown": txt[:80]})
        elif t == "display_probe":
            if TFT is None:
                send_result(jid, "error", error="display disabled or init failed — re-flash with the [display] Display option enabled")
            else:
                ids = TFT.read_id()
                send_result(jid, "done", {"ids": ids, "enabled": True,
                                          "w": TFT.W, "h": TFT.H, "pins": TFT_PINS})
        elif t == "sd_list":
            if not _sd_ok and not sd_mount():
                send_result(jid, "error", error="sd not mounted"); return
            import os
            path = _sd_path(p.get("path", "/"))
            files = []
            for name in os.listdir(path)[:64]:
                fp = path.rstrip("/") + "/" + name
                try:
                    st = os.stat(fp)
                    files.append({"name": name, "size": st[6], "dir": bool(st[0] & 0x4000)})
                except Exception:
                    files.append({"name": name})
            st = os.statvfs("/sd")
            send_result(jid, "done", {"path": path, "files": files, "count": len(files),
                                      "total_mb": st[0] * st[2] // 1048576})
        elif t == "sd_read":
            if not _sd_ok and not sd_mount():
                send_result(jid, "error", error="sd not mounted"); return
            import os
            path = _sd_path(p.get("path")); maxb = min(int(p.get("max", 1024)), 1400)
            size = os.stat(path)[6]
            with open(path) as f:
                content = f.read(maxb)
            send_result(jid, "done", {"path": path, "size": size, "content": content,
                                      "truncated": size > len(content)})
        elif t == "sd_write":
            if not _sd_ok and not sd_mount():
                send_result(jid, "error", error="sd not mounted"); return
            path = _sd_path(p.get("path"))
            with open(path, "a" if p.get("append") else "w") as f:
                f.write(p.get("content", ""))
            send_result(jid, "done", {"path": path, "bytes": len(p.get("content", ""))})
        elif t == "sd_delete":
            if not _sd_ok and not sd_mount():
                send_result(jid, "error", error="sd not mounted"); return
            import os
            path = _sd_path(p.get("path"))
            try: os.remove(path)
            except Exception: os.rmdir(path)
            send_result(jid, "done", {"path": path, "deleted": True})
        elif t == "serial_write":
            # Forward ESC/POS (or any) bytes to a USB thermal printer / serial
            # peripheral wired to this node's UART. payload:
            #   {data_b64, baud?, tx?, rx?, port?}  (tx/rx default to UART1 pins)
            try:
                import ubinascii
                raw = ubinascii.a2b_base64(p.get("data_b64", ""))
            except Exception as e:
                send_result(jid, "error", error="bad data_b64: " + str(e)); return
            baud = int(p.get("baud", 9600) or 9600)
            try:
                uid = int(p.get("port", 1))
                if "tx" in p and "rx" in p:
                    su = machine.UART(uid, baudrate=baud,
                                      tx=int(p["tx"]), rx=int(p["rx"]))
                else:
                    su = machine.UART(uid, baudrate=baud)
                n = su.write(raw)
                try: su.deinit()
                except Exception: pass
                send_result(jid, "done", {"wrote": n if n is not None else len(raw),
                                          "baud": baud})
            except Exception as e:
                send_result(jid, "error", error="serial_write: " + str(e))
        elif _toolkit_job(t, p, jid):
            pass                                 # handled by the ESP32 toolkit
        else:
            send_result(jid, "error", error="unknown type " + str(t))
    except Exception as e:
        send_result(jid, "error", error=str(e))

# ── Long-poll ────────────────────────────────────────────────────────────────
def poll_once(wait=25):
    # Short wait when a touch UI is up so the loop cycles fast enough to be responsive.
    url = SERVER + "/mesh/poll?node_id=" + NODE + "&wait=" + str(int(wait))
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
                 "fw": FW_VERSION, "runtime": "micropython",
                 "modules": MODULES, "channels": ["serial"]})

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
    tft_init()
    if TFT:
        MODULES.append("kiosk")
        # Boot diagnostic on the serial console: controller ID + a verdict.
        # 00 94 88 = ILI9488 & the bus works; all 00/FF = the bus isn't talking
        # (on the S3 usually a data line on GPIO19/20 = native USB-Serial-JTAG).
        try:
            ids = TFT.read_id() or {}; d3 = ids.get("0xD3") or []
            code = ((d3[-2] << 8) | d3[-1]) if len(d3) >= 2 else 0
            verdict = ("ILI9488 OK" if code == 0x9488 else
                       "ILI9486 (wrong init!)" if code == 0x9486 else
                       "BUS DEAD - check D4=GPIO19/D5=GPIO20 (USB pins)" if code in (0, 0xFFFF) else "unknown")
            print("[tft] pins", TFT_PINS, "id_D3", d3, verdict, "usb_pads", _usb_freed)
            for i, n in enumerate(TFT_PINS.get("d", [])):
                if n in (19, 20) and _usb_freed is not True:
                    print("[tft] WARNING: data D%d is on GPIO%d = native USB pin, and the USB pad is "
                          "still enabled (%s) — that bit is stuck. Re-push with the 'Free USB pins' "
                          "option, or flash the Arduino build (USB CDC off)." % (i, n, _usb_freed))
        except Exception as e:
            print("[tft] id read failed:", e)
    else:
        print("[tft] init failed / disabled; pins", TFT_PINS)
    if sd_mount(): MODULES.append("storage")
    dsp_text("VERA NODE", "booting...\nconnecting Wi-Fi", C_WHITE, C_BLACK, 2)
    serial_announce()                      # let a USB host bridge us even with no Wi-Fi
    if wifi_connect(): enroll()
    if _kiosk_mode == "status": dsp_status()
    _enrolled = wlan.isconnected()
    while True:
        # Whole-loop guard: a transient error (Wi-Fi, network, a bad job) must
        # never crash the firmware — stay alive so serial + re-provision keep working.
        try:
            handle_serial()
            _touch_tick()                            # responsive touch in UI mode (all modes)
            if SERIAL_ONLY:
                # Host relays us to Vera over USB. Skip the firewalled server poll/post,
                # but keep Wi-Fi up so the board's OWN jobs (web_fetch/watch) still work.
                if WIFI_SSID and not wlan.isconnected():
                    wifi_connect()
                if time.time() - _last_tele > telemetry_every:
                    send_telemetry(); _last_tele = time.time()    # prints over serial; post() is a no-op
                _worker_tick()
                time.sleep(0.05)
                continue
            if wlan.isconnected():
                if not _enrolled:
                    enroll(); _enrolled = True       # (re)enroll after a reconnect
                poll_once(2 if _kiosk_mode == "ui" else 25)
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
