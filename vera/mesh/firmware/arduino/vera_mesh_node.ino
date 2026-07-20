/* ============================================================================
 * vera_mesh_node.ino  —  Vera ESP32 Mesh Node (Arduino reference firmware)
 * ============================================================================
 *
 * Enrolls into a Vera mesh and runs configurable modules. Talks to Vera over
 * HTTP long-poll (always), and prints/accepts the same JSON envelopes over the
 * USB serial line so the browser (Web Serial) or the server (pyserial) can also
 * drive it. Templated by GET /mesh/firmware?flavor=arduino — {{SERVER_URL}},
 * {{MESH_TOKEN}} and {{NODE_ID}} are filled in when you download it.
 *
 * Modules (compile-time toggles below): sensor, web_fetch, watch, alert,
 * kiosk (built-in ILI9488 parallel driver OR TFT_eSPI), storage (SD),
 * control (GPIO/relay).
 *
 * Libraries: WiFi, HTTPClient, SD, SPI (bundled with the ESP32 core),
 * ArduinoJson, Preferences. No display library needed for the ILI9488 shield —
 * the 8-bit parallel driver below is self-contained.
 *
 * Wire protocol: see vera/mesh/PROTOCOL.md.
 * ========================================================================== */

#include <WiFi.h>
#include <HTTPClient.h>
#include <HTTPUpdate.h>      // OTA "program over Wi-Fi"
#include <ArduinoJson.h>
#include <Preferences.h>
#include <Wire.h>
#include <math.h>
#include "esp_wifi.h"        // CSI + promiscuous sniffer (core esp_wifi)
#include "esp_sleep.h"       // deep sleep

// ── ESP32-S3 toolkit feature flags ──────────────────────────────────────────
// Each is self-contained; flip one to 0 if it won't compile on your core/board
// (e.g. CSI struct fields moved between Arduino-ESP32 2.x/IDF4 and 3.x/IDF5).
#define TOOLKIT_RGB    1     // on-board WS2812 via rgbLedWrite() (Arduino-ESP32 3.x)
#define TOOLKIT_CSI    1     // Wi-Fi Channel State Information (device-free motion sensing)
#define TOOLKIT_SNIFF  1     // Wi-Fi promiscuous frame/MAC density counter
#define TOOLKIT_ESPNOW 1     // ESP-NOW ranging
#define TOOLKIT_BLE    0     // BLE scan — pulls in the (large) BLE library; enable if you use it

// Arduino-ESP32 3.x exposes rgbLedWrite(pin,r,g,b); 2.x used neopixelWrite().
// Swap the macro if you're on the older core.
#define RGB_WRITE rgbLedWrite

#if TOOLKIT_BLE
#include <BLEDevice.h>
#include <BLEScan.h>
#include <BLEAdvertisedDevice.h>
#endif

// ── Module toggles ──────────────────────────────────────────────────────────
#define MOD_SENSOR    1
#define MOD_WEB_FETCH 1
#define MOD_WATCH     1
#define MOD_ALERT     1
#define MOD_CONTROL   1

// Display backends — enable AT MOST one:
//  • USE_TFT_ILI9488_P8 — 3.5" Arduino-Uno TFT shield (ILI9488, 320x480,
//    8-bit parallel MCU bus + SD slot) on an ESP32-S3 with Uno footprint.
//    Self-contained driver, no library install, pins configured below.
//  • USE_TFT            — legacy TFT_eSPI kiosk (install TFT_eSPI + edit its
//    User_Setup.h yourself).
#define USE_TFT_ILI9488_P8 1
// #define USE_TFT

// SD card (the shield's SD slot runs over SPI)
#define USE_SD_SPI 1

// ── Pins (adjust to your board) ─────────────────────────────────────────────
// NOTE: defaults avoid the TFT/SD shield bus below (the old relay=26 / adc=34
// values don't exist on the S3; classic-ESP32 users can put them back).
#define LED_PIN     2        // onboard LED (identify / alert)
#define BUZZER_PIN  -1       // set to a GPIO for an active buzzer, else -1
#define SENSOR_ADC  15       // an analog sensor (e.g. LDR / pot) for demo telemetry
#define RELAY_PIN   21       // a relay / output for the control module

/* ── ILI9488 shield pins — ESP32-S3 (Arduino-Uno footprint) ──────────────────
 * Defaults follow this board→shield mapping:
 *   control : io4→RST  io5→CS  io6→RS(DC)  io7→WR  io1→RD   (io2 spare / A5)
 *   data    : io21→D0  io46→D1  io18→D2  io17→D3  io19→D4  io20→D5  io3→D6  io14→D7
 *   sd      : io12→SD_CLK  io13→SD_D0(MISO)  io11→SD_D1(MOSI)  io10→SD_SS(CS)
 *
 *   (D0/D1 sit on Uno pins D8/D9 — confirmed from this board's silkscreen.)
 *   All pins can also be remapped at runtime (no reflash) from the mesh
 *   manager via config.io.tft = {rst,cs,dc,wr,rd,d:[d0..d7]} and
 *   config.io.sd = {clk,miso,mosi,cs}.
 */
#define TFT_P8_RST  5        // hardware-verified S3-Uno control map (was 4/5/6/7/1 — an untested guess)
#define TFT_P8_CS   6
#define TFT_P8_DC   7        // shield "RS" (register select) = DC
#define TFT_P8_WR   1
#define TFT_P8_RD   2        // needed HIGH for writes + LOW to read the controller ID; -1 if unwired
#define TFT_P8_D0   21       // shield LCD_D0 = Uno D8
#define TFT_P8_D1   46       // shield LCD_D1 = Uno D9
#define TFT_P8_D2   18
#define TFT_P8_D3   17
#define TFT_P8_D4   19
#define TFT_P8_D5   20
#define TFT_P8_D6   3
#define TFT_P8_D7   14

#define SD_PIN_CLK  12
#define SD_PIN_MISO 13
#define SD_PIN_MOSI 11
#define SD_PIN_CS   10

// ── Server config (templated on download) ──────────────────────────────────
String SERVER = "{{SERVER_URL}}";          // e.g. http://192.168.0.138:8000
String TOKEN  = "{{MESH_TOKEN}}";          // "open" if no shared token
String NODE   = "{{NODE_ID}}";

// ── State ───────────────────────────────────────────────────────────────────
Preferences prefs;
String wifiSsid, wifiPass;
String nodeToken = "";                       // per-node token issued at enroll
unsigned long lastTelemetry = 0;
unsigned long telemetryEvery = 30000;        // ms — overridden by sensor config
int  watchEvery = 0; String watchUrl = "";   // watch module config
unsigned long lastWatch = 0;
String workerTasksJson = "[]";               // worker module: recurring edge tasks
unsigned long workerLast[8] = {0};
String kioskMode = "status";                 // "status" | "text" — what the screen shows
String lastNote  = "boot";                   // last job/message, shown on status screen

// On-board WS2812/NeoPixel: pin varies across S3 boards. Resolved from config
// (config.io.neopixel) or the neopixel_set job's `pin`; neo_probe walks these.
int  neoPin = 48;                            // common S3 default; override at runtime
const int NEO_CANDIDATES[] = {48, 38, 47, 21, 18, 8};

// ── CSI (Channel State Information) motion-sensing state ────────────────────
volatile bool  csiOn        = false;
volatile double csiWinSum   = 0;             // Σ per-packet subcarrier-amplitude variance
volatile uint32_t csiWinCnt = 0;
double csiBaseline          = 0;             // slow EMA of channel variance (the "quiet room")
double csiThresh            = 0.35;          // relative deviation that trips presence
unsigned long csiEvery      = 2000;          // ms between presence reports
unsigned long csiLast       = 0;

#ifdef USE_TFT
#include <TFT_eSPI.h>
TFT_eSPI tft = TFT_eSPI();
#endif

#if USE_SD_SPI
#include <SPI.h>
#include <SD.h>
SPIClass sdSPI(HSPI);
bool sdOk = false;
int sdPinClk = SD_PIN_CLK, sdPinMiso = SD_PIN_MISO, sdPinMosi = SD_PIN_MOSI, sdPinCs = SD_PIN_CS;
#endif

/* ════════════════════════════════════════════════════════════════════════════
 * ILI9488 — 8-bit parallel (MCU 8080) write-only driver
 * Fast path uses the GPIO W1TS/W1TC set/clear registers with a 256-entry
 * byte→mask LUT (works when every data pin is GPIO 0-31, which is the case on
 * S3-Uno boards). Any pin ≥32 falls back to digitalWrite (slow but correct).
 * ══════════════════════════════════════════════════════════════════════════ */
#if USE_TFT_ILI9488_P8
#include "soc/gpio_reg.h"

struct ILI9488P8 {
  int pRST = TFT_P8_RST, pCS = TFT_P8_CS, pDC = TFT_P8_DC, pWR = TFT_P8_WR, pRD = TFT_P8_RD;
  int pD[8] = {TFT_P8_D0, TFT_P8_D1, TFT_P8_D2, TFT_P8_D3, TFT_P8_D4, TFT_P8_D5, TFT_P8_D6, TFT_P8_D7};
  uint32_t lutSet[256], lutClr[256], wrMask = 0;
  bool fast = false, ok = false;
  int16_t W = 480, H = 320;
  uint8_t rot = 1;
  int16_t curX = 0, curY = 0;

  void buildLut() {
    fast = (pWR >= 0 && pWR < 32);
    uint32_t all = 0;
    for (int b = 0; b < 8; b++) { if (pD[b] < 0 || pD[b] > 31) fast = false; else all |= (1UL << pD[b]); }
    if (!fast) return;
    wrMask = 1UL << pWR;
    for (int v = 0; v < 256; v++) {
      uint32_t s = 0;
      for (int b = 0; b < 8; b++) if (v & (1 << b)) s |= (1UL << pD[b]);
      lutSet[v] = s; lutClr[v] = all & ~s;
    }
  }
  inline void wr8(uint8_t v) {
    if (fast) {
      REG_WRITE(GPIO_OUT_W1TS_REG, lutSet[v]);
      REG_WRITE(GPIO_OUT_W1TC_REG, lutClr[v]);
      REG_WRITE(GPIO_OUT_W1TC_REG, wrMask);
      REG_WRITE(GPIO_OUT_W1TC_REG, wrMask);   // stretch WR-low a few ns
      REG_WRITE(GPIO_OUT_W1TS_REG, wrMask);
    } else {
      for (int b = 0; b < 8; b++) digitalWrite(pD[b], (v >> b) & 1);
      digitalWrite(pWR, LOW); digitalWrite(pWR, HIGH);
    }
  }
  void cmd(uint8_t c) { digitalWrite(pDC, LOW); wr8(c); digitalWrite(pDC, HIGH); }
  void dat(uint8_t d) { wr8(d); }

  // Read a controller register (needs RD wired): send the command, flip the data
  // pins to input, then strobe RD low/high per byte. 0xD3 returns a dummy byte +
  // 3 ID bytes (ILI9488 = 00 94 88, ILI9486 = 00 94 86, ILI9341 = 00 93 41). All
  // 0x00 or all 0xFF ⇒ the bus/RD isn't reading (wrong pins or write-only wiring).
  uint32_t readReg(uint8_t reg, int nbytes) {
    cmd(reg);
    for (int b = 0; b < 8; b++) if (pD[b] >= 0) pinMode(pD[b], INPUT);
    uint32_t val = 0;
    for (int i = 0; i < nbytes; i++) {
      if (pRD >= 0) digitalWrite(pRD, LOW);
      delayMicroseconds(2);
      uint8_t v = 0;
      for (int b = 0; b < 8; b++) if (pD[b] >= 0 && digitalRead(pD[b])) v |= (1 << b);
      if (pRD >= 0) digitalWrite(pRD, HIGH);
      delayMicroseconds(2);
      val = (val << 8) | v;
    }
    for (int b = 0; b < 8; b++) if (pD[b] >= 0) { pinMode(pD[b], OUTPUT); }
    return val;
  }

  void init() {
    int ctl[5] = {pRST, pCS, pDC, pWR, pRD};
    for (int i = 0; i < 5; i++) if (ctl[i] >= 0) { pinMode(ctl[i], OUTPUT); digitalWrite(ctl[i], HIGH); }
    for (int b = 0; b < 8; b++) if (pD[b] >= 0) { pinMode(pD[b], OUTPUT); digitalWrite(pD[b], LOW); }
    buildLut();
    if (pRST >= 0) { digitalWrite(pRST, HIGH); delay(5); digitalWrite(pRST, LOW); delay(20); digitalWrite(pRST, HIGH); delay(120); }
    digitalWrite(pCS, LOW);                       // keep selected — we own the bus
    static const uint8_t GP[] = {0x00,0x03,0x09,0x08,0x16,0x0A,0x3F,0x78,0x4C,0x09,0x0A,0x08,0x16,0x1A,0x0F};
    static const uint8_t GN[] = {0x00,0x16,0x19,0x03,0x0F,0x05,0x32,0x45,0x46,0x04,0x0E,0x0D,0x35,0x37,0x0F};
    cmd(0xE0); for (int i = 0; i < 15; i++) dat(GP[i]);
    cmd(0xE1); for (int i = 0; i < 15; i++) dat(GN[i]);
    cmd(0xC0); dat(0x17); dat(0x15);              // power control 1
    cmd(0xC1); dat(0x41);                          // power control 2
    cmd(0xC5); dat(0x00); dat(0x12); dat(0x80);    // VCOM
    cmd(0x3A); dat(0x55);                          // 16-bit RGB565 (parallel supports it)
    cmd(0xB0); dat(0x00);
    cmd(0xB1); dat(0xA0);                          // frame rate
    cmd(0xB4); dat(0x02);                          // 2-dot inversion
    cmd(0xB6); dat(0x02); dat(0x02); dat(0x3B);
    cmd(0xB7); dat(0xC6);
    cmd(0xF7); dat(0xA9); dat(0x51); dat(0x2C); dat(0x82);
    cmd(0x11); delay(120);                         // sleep out
    setRotation(rot);
    cmd(0x29); delay(20);                          // display on
    ok = true;
  }
  void setRotation(uint8_t r) {
    static const uint8_t MAD[4] = {0x48, 0x28, 0x88, 0xE8};
    rot = r & 3;
    cmd(0x36); dat(MAD[rot]);
    if (rot & 1) { W = 480; H = 320; } else { W = 320; H = 480; }
  }
  void window(int x, int y, int w, int h) {
    int x2 = x + w - 1, y2 = y + h - 1;
    cmd(0x2A); dat(x >> 8); dat(x); dat(x2 >> 8); dat(x2);
    cmd(0x2B); dat(y >> 8); dat(y); dat(y2 >> 8); dat(y2);
    cmd(0x2C);
  }
  void fillRect(int x, int y, int w, int h, uint16_t c) {
    if (!ok || w <= 0 || h <= 0) return;
    if (x < 0) { w += x; x = 0; } if (y < 0) { h += y; y = 0; }
    if (x + w > W) w = W - x;    if (y + h > H) h = H - y;
    if (w <= 0 || h <= 0) return;
    window(x, y, w, h);
    uint8_t hi = c >> 8, lo = c & 0xFF;
    uint32_t n = (uint32_t)w * h;
    while (n--) { wr8(hi); wr8(lo); }
  }
  void fillScreen(uint16_t c) { fillRect(0, 0, W, H, c); }

  // 5x7 ASCII font (0x20..0x7E)
  void drawChar(int x, int y, char ch, uint16_t fg, uint16_t bg, uint8_t s) {
    static const uint8_t F[] = {
      0x00,0x00,0x00,0x00,0x00, 0x00,0x00,0x5F,0x00,0x00, 0x00,0x07,0x00,0x07,0x00,
      0x14,0x7F,0x14,0x7F,0x14, 0x24,0x2A,0x7F,0x2A,0x12, 0x23,0x13,0x08,0x64,0x62,
      0x36,0x49,0x55,0x22,0x50, 0x00,0x05,0x03,0x00,0x00, 0x00,0x1C,0x22,0x41,0x00,
      0x00,0x41,0x22,0x1C,0x00, 0x14,0x08,0x3E,0x08,0x14, 0x08,0x08,0x3E,0x08,0x08,
      0x00,0x50,0x30,0x00,0x00, 0x08,0x08,0x08,0x08,0x08, 0x00,0x60,0x60,0x00,0x00,
      0x20,0x10,0x08,0x04,0x02, 0x3E,0x51,0x49,0x45,0x3E, 0x00,0x42,0x7F,0x40,0x00,
      0x42,0x61,0x51,0x49,0x46, 0x21,0x41,0x45,0x4B,0x31, 0x18,0x14,0x12,0x7F,0x10,
      0x27,0x45,0x45,0x45,0x39, 0x3C,0x4A,0x49,0x49,0x30, 0x01,0x71,0x09,0x05,0x03,
      0x36,0x49,0x49,0x49,0x36, 0x06,0x49,0x49,0x29,0x1E, 0x00,0x36,0x36,0x00,0x00,
      0x00,0x56,0x36,0x00,0x00, 0x08,0x14,0x22,0x41,0x00, 0x14,0x14,0x14,0x14,0x14,
      0x00,0x41,0x22,0x14,0x08, 0x02,0x01,0x51,0x09,0x06, 0x32,0x49,0x79,0x41,0x3E,
      0x7E,0x11,0x11,0x11,0x7E, 0x7F,0x49,0x49,0x49,0x36, 0x3E,0x41,0x41,0x41,0x22,
      0x7F,0x41,0x41,0x22,0x1C, 0x7F,0x49,0x49,0x49,0x41, 0x7F,0x09,0x09,0x09,0x01,
      0x3E,0x41,0x49,0x49,0x7A, 0x7F,0x08,0x08,0x08,0x7F, 0x00,0x41,0x7F,0x41,0x00,
      0x20,0x40,0x41,0x3F,0x01, 0x7F,0x08,0x14,0x22,0x41, 0x7F,0x40,0x40,0x40,0x40,
      0x7F,0x02,0x0C,0x02,0x7F, 0x7F,0x04,0x08,0x10,0x7F, 0x3E,0x41,0x41,0x41,0x3E,
      0x7F,0x09,0x09,0x09,0x06, 0x3E,0x41,0x51,0x21,0x5E, 0x7F,0x09,0x19,0x29,0x46,
      0x46,0x49,0x49,0x49,0x31, 0x01,0x01,0x7F,0x01,0x01, 0x3F,0x40,0x40,0x40,0x3F,
      0x1F,0x20,0x40,0x20,0x1F, 0x3F,0x40,0x38,0x40,0x3F, 0x63,0x14,0x08,0x14,0x63,
      0x07,0x08,0x70,0x08,0x07, 0x61,0x51,0x49,0x45,0x43, 0x00,0x7F,0x41,0x41,0x00,
      0x02,0x04,0x08,0x10,0x20, 0x00,0x41,0x41,0x7F,0x00, 0x04,0x02,0x01,0x02,0x04,
      0x40,0x40,0x40,0x40,0x40, 0x00,0x01,0x02,0x04,0x00, 0x20,0x54,0x54,0x54,0x78,
      0x7F,0x48,0x44,0x44,0x38, 0x38,0x44,0x44,0x44,0x20, 0x38,0x44,0x44,0x48,0x7F,
      0x38,0x54,0x54,0x54,0x18, 0x08,0x7E,0x09,0x01,0x02, 0x0C,0x52,0x52,0x52,0x3E,
      0x7F,0x08,0x04,0x04,0x78, 0x00,0x44,0x7D,0x40,0x00, 0x20,0x40,0x44,0x3D,0x00,
      0x7F,0x10,0x28,0x44,0x00, 0x00,0x41,0x7F,0x40,0x00, 0x7C,0x04,0x18,0x04,0x78,
      0x7C,0x08,0x04,0x04,0x78, 0x38,0x44,0x44,0x44,0x38, 0x7C,0x14,0x14,0x14,0x08,
      0x08,0x14,0x14,0x18,0x7C, 0x7C,0x08,0x04,0x04,0x08, 0x48,0x54,0x54,0x54,0x20,
      0x04,0x3F,0x44,0x40,0x20, 0x3C,0x40,0x40,0x20,0x7C, 0x1C,0x20,0x40,0x20,0x1C,
      0x3C,0x40,0x30,0x40,0x3C, 0x44,0x28,0x10,0x28,0x44, 0x0C,0x50,0x50,0x50,0x3C,
      0x44,0x64,0x54,0x4C,0x44, 0x00,0x08,0x36,0x41,0x00, 0x00,0x00,0x7F,0x00,0x00,
      0x00,0x41,0x36,0x08,0x00, 0x08,0x08,0x2A,0x1C,0x08, 0x08,0x1C,0x2A,0x08,0x08,
    };
    if (ch < 0x20 || ch > 0x7E) ch = '?';
    if (x < 0 || y < 0 || x + 6 * s > W || y + 8 * s > H) return;
    const uint8_t *g = &F[(ch - 0x20) * 5];
    // One window per glyph cell, pixels streamed row-by-row (6th column = spacing)
    window(x, y, 6 * s, 8 * s);
    for (int row = 0; row < 8; row++) {
      for (int rs = 0; rs < s; rs++) {
        for (int col = 0; col < 6; col++) {
          uint16_t c = (col < 5 && (g[col] & (1 << row))) ? fg : bg;
          for (int cs = 0; cs < s; cs++) { wr8(c >> 8); wr8(c & 0xFF); }
        }
      }
    }
  }
  // Word-wrapping text writer; honours '\n'. Returns final Y.
  int drawText(int x, int y, const String &txt, uint16_t fg, uint16_t bg, uint8_t s) {
    int cw = 6 * s, chh = 8 * s, cx = x;
    for (unsigned i = 0; i < txt.length(); i++) {
      char ch = txt[i];
      if (ch == '\r') continue;
      if (ch == '\n' || cx + cw > W - 2) { cx = x; y += chh + s; if (ch == '\n') continue; }
      if (y + chh > H) break;
      drawChar(cx, y, ch, fg, bg, s);
      cx += cw;
    }
    return y;
  }
};
ILI9488P8 p8;
#endif // USE_TFT_ILI9488_P8

// RGB565 palette
#define C_BLACK 0x0000
#define C_WHITE 0xFFFF
#define C_GREEN 0x07E0
#define C_RED   0xF800
#define C_BLUE  0x001F
#define C_YELL  0xFFE0
#define C_GREY  0x8410
#define C_NAVY  0x000F

// ── Display abstraction (works for both backends; no-op when neither) ───────
bool dspAvailable() {
#if USE_TFT_ILI9488_P8
  return p8.ok;
#elif defined(USE_TFT)
  return true;
#else
  return false;
#endif
}

void dspClear(uint16_t bg) {
#if USE_TFT_ILI9488_P8
  p8.fillScreen(bg);
#elif defined(USE_TFT)
  tft.fillScreen(bg);
#endif
}

void dspText(const String &title, const String &body, uint16_t fg, uint16_t bg, int size) {
#if USE_TFT_ILI9488_P8
  if (!p8.ok) return;
  p8.fillScreen(bg);
  int y = 8;
  if (title.length()) {
    y = p8.drawText(8, y, title, C_YELL, bg, size + 1) + 8 * (size + 1) + 6;
    p8.fillRect(8, y, p8.W - 16, 2, C_GREY); y += 8;
  }
  p8.drawText(8, y, body, fg, bg, size);
#elif defined(USE_TFT)
  tft.fillScreen(bg); tft.setCursor(6, 10); tft.setTextColor(fg);
  if (title.length()) { tft.println(title); tft.println(); }
  tft.print(body);
#endif
}

void dspAlert(const String &msg) {
#if USE_TFT_ILI9488_P8
  if (!p8.ok) return;
  p8.fillScreen(C_RED);
  p8.drawText(10, 12, "! ALERT", C_WHITE, C_RED, 4);
  p8.drawText(10, 60, msg, C_WHITE, C_RED, 3);
#elif defined(USE_TFT)
  tft.fillScreen(TFT_RED); tft.setCursor(6, 10); tft.setTextColor(TFT_WHITE); tft.print(msg);
#endif
  kioskMode = "text";     // hold the alert until the next kiosk_set / status switch
}

// Status dashboard: identity, network, storage, last activity.
void dspStatus() {
#if USE_TFT_ILI9488_P8
  if (!p8.ok) return;
  p8.fillScreen(C_BLACK);
  p8.drawText(8, 8, "VERA NODE", C_GREEN, C_BLACK, 3);
  p8.fillRect(8, 36, p8.W - 16, 2, C_GREY);
  String ip  = (WiFi.status() == WL_CONNECTED) ? WiFi.localIP().toString() : String("(offline)");
  String sd  = "none";
#if USE_SD_SPI
  if (sdOk) sd = String((unsigned long)(SD.usedBytes() / 1048576UL)) + "/" +
                 String((unsigned long)(SD.totalBytes() / 1048576UL)) + " MB";
#endif
  String body;
  body += "node   " + NODE + "\n";
  body += "ip     " + ip + "  rssi " + String(WiFi.RSSI()) + "\n";
  body += "server " + SERVER + "\n";
  body += "uptime " + String(millis() / 1000) + "s   heap " + String((int)(ESP.getFreeHeap() / 1024)) + "k\n";
  body += "sd     " + sd + "\n";
  body += "last   " + lastNote;
  p8.drawText(8, 48, body, C_WHITE, C_BLACK, 2);
#endif
}

// Display bring-up test pattern: colour bars + border + the live pin map. ANY
// output other than a blank white screen proves the parallel bus is alive — use
// it to dial in a new board's pins/rotation, then Save the working map. Wrong
// colours (e.g. red↔blue swapped) mean an RGB/BGR flag; a mirrored/rotated image
// means the rotation needs changing — both fixable without reflashing.
void dspTest() {
#if USE_TFT_ILI9488_P8
  if (!p8.ok) return;
  p8.fillScreen(C_BLACK);
  const uint16_t bars[8] = {C_RED, C_GREEN, C_BLUE, C_YELL,
                            0x07FF /*cyan*/, 0xF81F /*magenta*/, C_WHITE, C_GREY};
  int bw = p8.W / 8;
  for (int i = 0; i < 8; i++) p8.fillRect(i * bw, 0, bw, p8.H / 2, bars[i]);
  // 2px white border reveals the true screen bounds + rotation
  p8.fillRect(0, 0, p8.W, 2, C_WHITE); p8.fillRect(0, p8.H - 2, p8.W, 2, C_WHITE);
  p8.fillRect(0, 0, 2, p8.H, C_WHITE);  p8.fillRect(p8.W - 2, 0, 2, p8.H, C_WHITE);
  int y = p8.H / 2 + 8;
  p8.drawText(8, y, "VERA DISPLAY TEST", C_WHITE, C_BLACK, 2); y += 26;
  p8.drawText(8, y, String(p8.W) + "x" + String(p8.H) + "  rot " + String(p8.rot),
              C_GREEN, C_BLACK, 2); y += 24;
  p8.drawText(8, y, "WR" + String(p8.pWR) + " DC" + String(p8.pDC) + " CS" + String(p8.pCS)
                    + " RST" + String(p8.pRST) + " RD" + String(p8.pRD), C_YELL, C_BLACK, 1); y += 14;
  String dp = "D:"; for (int i = 0; i < 8; i++) { dp += String(p8.pD[i]); if (i < 7) dp += ","; }
  p8.drawText(8, y, dp, C_YELL, C_BLACK, 1);
  kioskMode = "text";   // hold the pattern until the next kiosk_set / status switch
#endif
}

// Draw a 24-bit uncompressed BMP from SD, top-left anchored, clipped to screen.
#if USE_TFT_ILI9488_P8 && USE_SD_SPI
bool dspBmp(const String &path) {
  if (!p8.ok || !sdOk) return false;
  File f = SD.open(path);
  if (!f) return false;
  uint8_t hdr[54];
  if (f.read(hdr, 54) != 54 || hdr[0] != 'B' || hdr[1] != 'M') { f.close(); return false; }
  uint32_t dataOfs = hdr[10] | (hdr[11] << 8) | (hdr[12] << 16) | ((uint32_t)hdr[13] << 24);
  int32_t  bw = hdr[18] | (hdr[19] << 8) | (hdr[20] << 16) | ((uint32_t)hdr[21] << 24);
  int32_t  bh = hdr[22] | (hdr[23] << 8) | (hdr[24] << 16) | ((uint32_t)hdr[25] << 24);
  uint16_t bpp = hdr[28] | (hdr[29] << 8);
  if (bpp != 24 || bw <= 0) { f.close(); return false; }
  bool flip = bh > 0; if (bh < 0) bh = -bh;
  int w = min((int)bw, (int)p8.W), h = min((int)bh, (int)p8.H);
  uint32_t rowSize = ((uint32_t)bw * 3 + 3) & ~3;
  static uint8_t row[480 * 3];
  p8.fillScreen(C_BLACK);
  p8.window(0, 0, w, h);
  for (int y = 0; y < h; y++) {
    uint32_t srcRow = flip ? (bh - 1 - y) : y;
    f.seek(dataOfs + srcRow * rowSize);
    f.read(row, min(rowSize, (uint32_t)sizeof(row)));
    for (int x = 0; x < w; x++) {
      uint8_t b = row[x * 3], g = row[x * 3 + 1], r = row[x * 3 + 2];
      uint16_t c = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3);
      p8.wr8(c >> 8); p8.wr8(c & 0xFF);
    }
  }
  f.close();
  return true;
}
#endif

// ── SD helpers ───────────────────────────────────────────────────────────────
#if USE_SD_SPI
bool sdMount() {
  sdSPI.end();
  sdSPI.begin(sdPinClk, sdPinMiso, sdPinMosi, sdPinCs);
  sdOk = SD.begin(sdPinCs, sdSPI, 20000000);
  if (!sdOk) sdOk = SD.begin(sdPinCs, sdSPI, 4000000);   // marginal wiring fallback
  return sdOk;
}
#endif

// ── Helpers ─────────────────────────────────────────────────────────────────
String chipId(){ uint64_t m=ESP.getEfuseMac(); char b[20]; sprintf(b,"%04X%08X",(uint16_t)(m>>32),(uint32_t)m); return String(b); }

void emitSerial(const JsonDocument& doc){ serializeJson(doc, Serial); Serial.println(); }

// POST helper; returns response body ("" on failure)
String httpPost(const String& path, const String& body){
  if(WiFi.status()!=WL_CONNECTED) return "";
  HTTPClient http; http.begin(SERVER+path); http.addHeader("Content-Type","application/json");
  if(TOKEN.length() && TOKEN!="open") http.addHeader("X-Mesh-Token", TOKEN);
  int code = http.POST(body); String out = (code>0)? http.getString() : "";
  http.end(); return out;
}

// ── Enroll ──────────────────────────────────────────────────────────────────
void enroll(){
  StaticJsonDocument<512> d;
  d["kind"]="hello"; d["node_id"]=NODE; d["name"]=NODE; d["board"]="esp32";
  d["fw"]="1.1"; d["mac"]=WiFi.macAddress(); d["ip"]=WiFi.localIP().toString();
  d["rssi"]=WiFi.RSSI(); if(TOKEN.length()&&TOKEN!="open") d["token"]=TOKEN;
  JsonArray mods=d.createNestedArray("modules");
#if MOD_SENSOR
  mods.add("sensor");
#endif
#if MOD_WEB_FETCH
  mods.add("web_fetch");
#endif
#if MOD_WATCH
  mods.add("watch");
#endif
#if MOD_ALERT
  mods.add("alert");
#endif
#if USE_TFT_ILI9488_P8 || defined(USE_TFT)
  mods.add("kiosk");
#endif
#if USE_SD_SPI
  mods.add("storage");
#endif
#if MOD_CONTROL
  mods.add("control");
#endif
#if TOOLKIT_RGB
  mods.add("rgb");
#endif
#if TOOLKIT_BLE
  mods.add("ble");
#endif
  mods.add("io"); mods.add("worker");           // always: generic IO + autonomous edge tasks
  mods.add("toolkit"); mods.add("position");    // ESP32 toolkit + RF positioning
  JsonArray ch=d.createNestedArray("channels"); ch.add("http"); ch.add("serial");
  String body; serializeJson(d,body);
  emitSerial(d);                                 // also announce over USB serial
  String resp = httpPost("/mesh/hello", body);
  if(resp.length()){
    StaticJsonDocument<1024> r; if(!deserializeJson(r,resp)){
      if(r.containsKey("token")){ nodeToken=r["token"].as<String>(); TOKEN=nodeToken; }
      if(r["heartbeat"].is<int>()) {/* could adjust poll cadence */}
      applyConfig(r["config"]);
    }
  }
  lastNote = "enrolled";
  if (kioskMode == "status") dspStatus();
}

// ── Apply server config (per-module settings) ───────────────────────────────
void applyConfig(JsonVariant cfg){
  if(cfg.isNull()) return;
  if(cfg["sensor"]["interval_s"].is<int>())
    telemetryEvery = (unsigned long)cfg["sensor"]["interval_s"].as<int>()*1000UL;
  if(cfg["watch"]["url"].is<const char*>()) watchUrl = cfg["watch"]["url"].as<String>();
  if(cfg["watch"]["interval_s"].is<int>()) watchEvery = cfg["watch"]["interval_s"].as<int>()*1000;
  if(cfg["worker"]["tasks"].is<JsonArray>()){ workerTasksJson=""; serializeJson(cfg["worker"]["tasks"], workerTasksJson);
    for(int i=0;i<8;i++) workerLast[i]=0; }
  if(cfg["io"]["neopixel"].is<int>()) neoPin = cfg["io"]["neopixel"].as<int>();   // WS2812 data pin
#if USE_TFT_ILI9488_P8
  // Runtime pin remap — no reflash needed: config.io.tft = {rst,cs,dc,wr,rd,d:[8]}
  JsonVariant t = cfg["io"]["tft"];
  if(!t.isNull()){
    bool changed=false;
    if(t["rst"].is<int>()){ p8.pRST=t["rst"].as<int>(); changed=true; }
    if(t["cs"].is<int>()) { p8.pCS =t["cs"].as<int>();  changed=true; }
    if(t["dc"].is<int>()) { p8.pDC =t["dc"].as<int>();  changed=true; }
    if(t["rs"].is<int>()) { p8.pDC =t["rs"].as<int>();  changed=true; }   // shield naming
    if(t["wr"].is<int>()) { p8.pWR =t["wr"].as<int>();  changed=true; }
    if(t["rd"].is<int>()) { p8.pRD =t["rd"].as<int>();  changed=true; }
    if(t["d"].is<JsonArray>()){
      int i=0; for(JsonVariant v : t["d"].as<JsonArray>()){ if(i<8&&v.is<int>()) p8.pD[i]=v.as<int>(); i++; }
      changed=true;
    }
    if(changed){ p8.init(); dspStatus(); }
  }
  if(cfg["kiosk"]["rotation"].is<int>()) { p8.setRotation(cfg["kiosk"]["rotation"].as<int>()); dspStatus(); }
#endif
#if USE_SD_SPI
  JsonVariant s = cfg["io"]["sd"];
  if(!s.isNull()){
    if(s["clk"].is<int>())  sdPinClk =s["clk"].as<int>();
    if(s["miso"].is<int>()) sdPinMiso=s["miso"].as<int>();
    if(s["mosi"].is<int>()) sdPinMosi=s["mosi"].as<int>();
    if(s["cs"].is<int>())   sdPinCs  =s["cs"].as<int>();
    sdMount();
  }
#endif
}

// ── Telemetry ───────────────────────────────────────────────────────────────
void sendTelemetry(){
  StaticJsonDocument<512> d; d["kind"]="telemetry"; d["node_id"]=NODE; d["rssi"]=WiFi.RSSI();
  JsonObject m = d.createNestedObject("metrics");
#if MOD_SENSOR
  m["adc"]    = analogRead(SENSOR_ADC);
  m["heap"]   = (int)ESP.getFreeHeap();
  m["uptime"] = (int)(millis()/1000);
#endif
#if USE_SD_SPI
  if(sdOk){
    m["sd_total_mb"] = (int)(SD.totalBytes()/1048576UL);
    m["sd_used_mb"]  = (int)(SD.usedBytes()/1048576UL);
  }
#endif
  String body; serializeJson(d,body);
  emitSerial(d);
  httpPost("/mesh/telemetry", body);
  if (kioskMode == "status") dspStatus();     // keep the dashboard fresh
}

// ── Result reporting ────────────────────────────────────────────────────────
void sendResult(const String& jobId, const char* status, JsonVariant result, const String& err){
  StaticJsonDocument<2560> d; d["kind"]="result"; d["node_id"]=NODE; d["job_id"]=jobId;   // roomy: wifi_scan/sysinfo results
  d["status"]=status; if(!result.isNull()) d["result"]=result; if(err.length()) d["error"]=err;
  String body; serializeJson(d,body); emitSerial(d); httpPost("/mesh/result", body);
}

/* ════════════════════════════════════════════════════════════════════════════
 * ESP32-S3 toolkit — RGB/NeoPixel, CSI motion sensing, promiscuous sniffer,
 * BLE scan, ESP-NOW ranging, I2C scan, touch, temp, channel survey, sysinfo.
 * ════════════════════════════════════════════════════════════════════════════ */

#if TOOLKIT_RGB
void rgbEffect(int r, int g, int b, const String& effect, int bright, int pin){
  int R=r*bright/255, G=g*bright/255, B=b*bright/255;
  if(effect=="off"){ RGB_WRITE(pin,0,0,0); }
  else if(effect=="blink"){ for(int i=0;i<4;i++){ RGB_WRITE(pin,R,G,B); delay(150); RGB_WRITE(pin,0,0,0); delay(150);} RGB_WRITE(pin,R,G,B); }
  else if(effect=="breathe"){ for(int k=0;k<=255;k+=32) { RGB_WRITE(pin,R*k/255,G*k/255,B*k/255); delay(40);} for(int k=255;k>=0;k-=32){ RGB_WRITE(pin,R*k/255,G*k/255,B*k/255); delay(40);} RGB_WRITE(pin,R,G,B); }
  else if(effect=="rainbow"){ for(int j=0;j<256;j+=8){ int p=255-j; int rr,gg,bb;
      if(p<85){rr=255-p*3;gg=0;bb=p*3;} else if(p<170){p-=85;rr=0;gg=p*3;bb=255-p*3;} else {p-=170;rr=p*3;gg=255-p*3;bb=0;}
      RGB_WRITE(pin,rr*bright/255,gg*bright/255,bb*bright/255); delay(18);} RGB_WRITE(pin,R,G,B); }
  else { RGB_WRITE(pin,R,G,B); }               // solid
}
#endif

#if TOOLKIT_CSI
// CSI RX callback — runs in the Wi-Fi task. Compute the variance of subcarrier
// amplitudes for this packet; movement in the room perturbs the multipath so
// the variance jumps vs a quiet baseline. Buffer is int8 (imag,real) pairs.
void csiRxCb(void* ctx, wifi_csi_info_t* info){
  if(!csiOn || !info || !info->buf || info->len < 4) return;
  int8_t* d = info->buf; int len = info->len;
  double sum=0, sumsq=0; int n=0;
  for(int i=0;i+1<len;i+=2){ double im=d[i], re=d[i+1]; double amp=sqrt(im*im+re*re); sum+=amp; sumsq+=amp*amp; n++; }
  if(n<1) return;
  double mean=sum/n, var=sumsq/n - mean*mean;
  csiWinSum += var; csiWinCnt++;
}
void csiStart(){
  wifi_csi_config_t c = {};
  // IDF4 / Arduino-ESP32 2.x field names. On IDF5 / core 3.x these moved —
  // if this block won't compile set TOOLKIT_CSI 0 (rest of the toolkit is fine).
  c.lltf_en = true; c.htltf_en = true; c.stbc_htltf2_en = true;
  c.ltf_merge_en = true; c.channel_filter_en = true;
  c.manu_scale = false; c.shift = false;
  esp_wifi_set_csi_config(&c);
  esp_wifi_set_csi_rx_cb(&csiRxCb, NULL);
  esp_wifi_set_csi(true);
  csiOn = true; csiBaseline = 0; csiWinSum = 0; csiWinCnt = 0; csiLast = millis();
}
void csiStop(){ csiOn = false; esp_wifi_set_csi(false); }
// Called from loop(): fold the window into a motion metric + presence telemetry.
void csiTick(){
  if(!csiOn || millis()-csiLast < csiEvery) return;
  csiLast = millis();
  double cnt = csiWinCnt; double winVar = cnt>0 ? (csiWinSum/cnt) : 0;
  csiWinSum = 0; csiWinCnt = 0;
  if(csiBaseline<=0){ csiBaseline = winVar>0?winVar:1; return; }   // learn quiet baseline first
  double motion = fabs(winVar - csiBaseline) / (csiBaseline>0?csiBaseline:1);
  bool present = motion > csiThresh;
  // adapt baseline slowly only while quiet, so presence doesn't bleed in
  if(!present) csiBaseline = 0.98*csiBaseline + 0.02*winVar;
  StaticJsonDocument<192> d; d["kind"]="telemetry"; d["node_id"]=NODE;
  JsonObject m=d.createNestedObject("metrics");
  m["csi_motion"]=motion; m["csi_present"]=present?1:0; m["csi_metric"]=winVar;
  m["csi_packets"]=(int)cnt;
  String b; serializeJson(d,b); httpPost("/mesh/telemetry", b);
}
#endif

#if TOOLKIT_SNIFF
volatile uint32_t snMgmt=0, snData=0, snCtrl=0, snTotal=0;
#define SN_MAX_MACS 64
uint8_t snMacs[SN_MAX_MACS][6]; volatile int snMacN=0;
void snReset(){ snMgmt=snData=snCtrl=snTotal=0; snMacN=0; }
void snAddMac(const uint8_t* mac){
  for(int i=0;i<snMacN;i++) if(!memcmp(snMacs[i],mac,6)) return;
  if(snMacN<SN_MAX_MACS){ memcpy(snMacs[snMacN],mac,6); snMacN++; }
}
void snCb(void* buf, wifi_promiscuous_pkt_type_t type){
  snTotal++;
  if(type==WIFI_PKT_MGMT) snMgmt++; else if(type==WIFI_PKT_DATA) snData++; else snCtrl++;
  const wifi_promiscuous_pkt_t* pkt=(wifi_promiscuous_pkt_t*)buf;
  const uint8_t* p=pkt->payload;                 // 802.11 hdr: addr2 (source) at offset 10
  if(pkt->rx_ctrl.sig_len>=16) snAddMac(p+10);
}
#endif

// Returns true if this job type was a toolkit job (handled + result sent).
bool toolkitJob(const String& id, const String& type, JsonObject p){
  StaticJsonDocument<1024> res; JsonVariant rv = res.to<JsonVariant>();

#if TOOLKIT_RGB
  if(type=="neopixel_set"){
    int pin = p["pin"].is<int>() ? p["pin"].as<int>() : neoPin; neoPin=pin;
    rgbEffect(p["r"].as<int>(), p["g"].as<int>(), p["b"].as<int>(),
              p["effect"].as<String>(), p["brightness"].is<int>()?p["brightness"].as<int>():255, pin);
    res["pin"]=pin; sendResult(id,"done",rv,""); return true;
  }
  if(type=="neo_probe"){
    int dwell = p["dwell_ms"].is<int>()?p["dwell_ms"].as<int>():1000;
    JsonArray probed=res.createNestedArray("probed");
    JsonArray pins = p["pins"].as<JsonArray>();
    if(pins.isNull()){ for(unsigned i=0;i<sizeof(NEO_CANDIDATES)/sizeof(int);i++){
        int pin=NEO_CANDIDATES[i]; RGB_WRITE(pin,0,80,0); delay(dwell); RGB_WRITE(pin,0,0,0); probed.add(pin);} }
    else { for(JsonVariant v:pins){ int pin=v.as<int>(); RGB_WRITE(pin,0,80,0); delay(dwell); RGB_WRITE(pin,0,0,0); probed.add(pin);} }
    res["hint"]="note which pin lit the LED, then set config.io.neopixel";
    sendResult(id,"done",rv,""); return true;
  }
#endif

  if(type=="i2c_scan"){
    int sda = p["sda"].is<int>()?p["sda"].as<int>():8;
    int scl = p["scl"].is<int>()?p["scl"].as<int>():9;
    Wire.begin(sda, scl, p["freq"].is<int>()?p["freq"].as<int>():100000);
    JsonArray addrs=res.createNestedArray("addresses");
    for(uint8_t a=1;a<127;a++){ Wire.beginTransmission(a); if(Wire.endTransmission()==0){ char h[6]; sprintf(h,"0x%02x",a); addrs.add(h);} }
    res["sda"]=sda; res["scl"]=scl; sendResult(id,"done",rv,""); return true;
  }
  if(type=="touch_read"){
    int pin=p["pin"].as<int>();
    #if defined(SOC_TOUCH_SENSOR_SUPPORTED) || defined(ESP32)
    uint32_t v=touchRead(pin);
    res["pin"]=pin; res["value"]=(int)v;
    StaticJsonDocument<96> td; td["kind"]="telemetry"; td["node_id"]=NODE;
    td["metrics"][String("touch")+pin]=(int)v; String tb; serializeJson(td,tb); httpPost("/mesh/telemetry",tb);
    sendResult(id,"done",rv,"");
    #else
    sendResult(id,"error",rv,"no touch on this chip");
    #endif
    return true;
  }
  if(type=="temp_read"){
    float t = temperatureRead();                 // internal sensor (°C)
    res["mcu_temp"]=t;
    StaticJsonDocument<96> td; td["kind"]="telemetry"; td["node_id"]=NODE;
    td["metrics"]["mcu_temp"]=t; String tb; serializeJson(td,tb); httpPost("/mesh/telemetry",tb);
    sendResult(id,"done",rv,""); return true;
  }
  if(type=="channel_survey"){
    int n=WiFi.scanNetworks(false,true); int cnt[15]={0}; int best[15];
    for(int i=0;i<15;i++) best[i]=-999;
    for(int i=0;i<n;i++){ int ch=WiFi.channel(i); if(ch>=1&&ch<=14){ cnt[ch]++; if(WiFi.RSSI(i)>best[ch]) best[ch]=WiFi.RSSI(i);} }
    WiFi.scanDelete();
    JsonArray sv=res.createNestedArray("survey"); int clearest=1, cmin=99999;
    for(int ch=1;ch<=13;ch++){ JsonObject o=sv.createNestedObject(); o["channel"]=ch; o["count"]=cnt[ch]; o["max_rssi"]=best[ch];
      if(cnt[ch]<cmin){ cmin=cnt[ch]; clearest=ch; } }
    res["clearest"]=clearest; sendResult(id,"done",rv,""); return true;
  }
  if(type=="wifi_scan"){
    int n=WiFi.scanNetworks(false,true);           // sync scan, include hidden
    DynamicJsonDocument wd(3072);                   // own buffer (APs won't fit the shared 1KB res)
    JsonArray aps=wd.createNestedArray("aps");
    for(int i=0;i<n && i<12;i++){                   // top 12 keeps the result envelope < sendResult's buffer
      JsonObject o=aps.createNestedObject();
      o["ssid"]=WiFi.SSID(i); o["bssid"]=WiFi.BSSIDstr(i);
      o["channel"]=WiFi.channel(i); o["rssi"]=WiFi.RSSI(i);
      o["auth"]=(int)WiFi.encryptionType(i);
    }
    wd["count"]=(n<0?0:n); WiFi.scanDelete();
    sendResult(id,"done",wd.as<JsonVariant>(),""); return true;
  }
  if(type=="rf_range"){
    String target=p["target"].as<String>(); target.toLowerCase();
    String kind=p["kind"].is<const char*>()?p["kind"].as<String>():String("wifi");
    int samples=p["samples"].is<int>()?p["samples"].as<int>():4;
    long acc=0; int hits=0;
    for(int s=0;s<samples;s++){
      int n=WiFi.scanNetworks(false,true);
      for(int i=0;i<n;i++){ String b=WiFi.BSSIDstr(i); b.toLowerCase(); if(b==target){ acc+=WiFi.RSSI(i); hits++; } }
      WiFi.scanDelete(); delay(120);
    }
    if(hits){ res["target"]=target; res["kind"]="wifi"; res["rssi"]=(int)(acc/hits); res["n"]=hits; sendResult(id,"done",rv,""); }
    else sendResult(id,"error",rv,"target not seen: "+target);
    return true;
  }
  if(type=="sysinfo"){
    res["node_id"]=NODE; res["chip"]=ESP.getChipModel(); res["cores"]=ESP.getChipCores();
    res["rev"]=ESP.getChipRevision(); res["cpu_mhz"]=ESP.getCpuFreqMHz();
    res["flash_mb"]=(int)(ESP.getFlashChipSize()/1048576); res["psram_mb"]=(int)(ESP.getPsramSize()/1048576);
    res["heap_free"]=(int)ESP.getFreeHeap(); res["mac"]=WiFi.macAddress(); res["sdk"]=ESP.getSdkVersion();
    sendResult(id,"done",rv,""); return true;
  }
  if(type=="deep_sleep"){
    int secs=p["seconds"].as<int>(); res["sleeping_s"]=secs; sendResult(id,"done",rv,"");
    delay(200); esp_sleep_enable_timer_wakeup((uint64_t)secs*1000000ULL); esp_deep_sleep_start(); return true;
  }

#if TOOLKIT_CSI
  if(type=="csi_start"){
    if(p["interval_s"].is<int>()) csiEvery=(unsigned long)p["interval_s"].as<int>()*1000UL;
    if(p["threshold"].is<float>()||p["threshold"].is<int>()) csiThresh=p["threshold"].as<float>();
    csiStart(); res["interval_s"]=(int)(csiEvery/1000); res["threshold"]=csiThresh;
    sendResult(id,"done",rv,""); return true;
  }
  if(type=="csi_stop"){ csiStop(); sendResult(id,"done",rv,""); return true; }
#endif

#if TOOLKIT_SNIFF
  if(type=="sniff"){
    int ch=p["channel"].is<int>()?p["channel"].as<int>():0;
    int secs=p["seconds"].is<int>()?p["seconds"].as<int>():8;
    bool wasCsi=false;
    #if TOOLKIT_CSI
    wasCsi=csiOn; if(csiOn) csiStop();
    #endif
    snReset();
    esp_wifi_set_promiscuous_rx_cb(&snCb);
    esp_wifi_set_promiscuous(true);
    if(ch>0){ esp_wifi_set_channel(ch, WIFI_SECOND_CHAN_NONE); }
    unsigned long t0=millis(); int hop=1;
    while(millis()-t0 < (unsigned long)secs*1000UL){
      if(ch==0){ esp_wifi_set_channel(hop, WIFI_SECOND_CHAN_NONE); hop=hop>=13?1:hop+1; }
      delay(ch==0?250:200);
    }
    esp_wifi_set_promiscuous(false);
    #if TOOLKIT_CSI
    if(wasCsi) csiStart();
    #endif
    res["channel"]=ch; res["seconds"]=secs; res["frames"]=(int)snTotal;
    res["mgmt"]=(int)snMgmt; res["data"]=(int)snData; res["ctrl"]=(int)snCtrl;
    res["unique_macs"]=snMacN;
    StaticJsonDocument<96> td; td["kind"]="telemetry"; td["node_id"]=NODE;
    td["metrics"]["rf_density"]=snMacN; td["metrics"]["rf_frames"]=(int)snTotal;
    String tb; serializeJson(td,tb); httpPost("/mesh/telemetry",tb);
    sendResult(id,"done",rv,""); return true;
  }
#endif

#if TOOLKIT_ESPNOW
  if(type=="espnow_ping"){
    sendResult(id,"error",rv,"espnow_ping: enable + pair peers in firmware (stub)"); return true;
  }
#endif

#if TOOLKIT_BLE
  if(type=="ble_scan"){
    int secs=p["seconds"].is<int>()?p["seconds"].as<int>():5;
    BLEDevice::init("");
    BLEScan* sc=BLEDevice::getScan(); sc->setActiveScan(p["active"].as<bool>());
    BLEScanResults r=sc->start(secs,false);
    JsonArray devs=res.createNestedArray("devices");
    for(int i=0;i<r.getCount();i++){ BLEAdvertisedDevice d=r.getDevice(i);
      JsonObject o=devs.createNestedObject(); o["mac"]=d.getAddress().toString().c_str();
      o["rssi"]=d.getRSSI(); if(d.haveName()) o["name"]=d.getName().c_str(); }
    res["count"]=r.getCount(); sc->clearResults(); BLEDevice::deinit(false);
    sendResult(id,"done",rv,""); return true;
  }
#endif

  return false;
}

// ── Job dispatch ────────────────────────────────────────────────────────────
void runJob(JsonObject job){
  String id   = job["job_id"].as<String>();
  String type = job["type"].as<String>();
  JsonObject p = job["payload"].as<JsonObject>();
  StaticJsonDocument<2048> res; JsonVariant rv = res.to<JsonVariant>();
  lastNote = type;

  if(type=="identify"){
    for(int i=0;i<10;i++){ digitalWrite(LED_PIN,!digitalRead(LED_PIN)); delay(120);}
    sendResult(id,"done",rv,"");
  }
  else if(type=="reboot"){ sendResult(id,"done",rv,""); delay(200); ESP.restart(); }
  else if(type=="read_sensor"){ sendTelemetry(); sendResult(id,"done",rv,""); }
  else if(type=="config"){ applyConfig(p["config"]); sendResult(id,"done",rv,""); }
  else if(type=="io_set"){
    int pin = p["pin"].as<int>(); String mode = p["mode"].as<String>();
    if(mode=="pwm"){ analogWrite(pin, p["value"].as<int>()); }
    else { pinMode(pin, OUTPUT);
      String v = p["value"].as<String>(); bool on = (v=="1"||v=="on"||v=="true");
      if(v=="toggle") on = !digitalRead(pin);
      digitalWrite(pin, on?HIGH:LOW); }
    res["pin"]=pin; sendResult(id,"done",rv,"");
  }
  else if(type=="io_read"){
    int pin = p["pin"].as<int>(); bool analog = p["analog"].as<bool>();
    int val = analog ? analogRead(pin) : (pinMode(pin,INPUT), digitalRead(pin));
    res["pin"]=pin; res["value"]=val;
    // also push as telemetry so it charts
    StaticJsonDocument<128> td; td["kind"]="telemetry"; td["node_id"]=NODE;
    td["metrics"][String("pin")+pin]=val; String tb; serializeJson(td,tb); httpPost("/mesh/telemetry",tb);
    sendResult(id,"done",rv,"");
  }
  else if(type=="ota"){
    String mode = p["mode"].as<String>(); String url = p["url"].as<String>();
    if(url.startsWith("/")) url = SERVER + url;
    if(mode=="file"){ sendResult(id,"error",rv,"file OTA is MicroPython-only; use a .bin for Arduino"); }
    else {
      sendResult(id,"done",rv,"OTA starting");           // report before we reboot
      WiFiClient client; t_httpUpdate_return r = httpUpdate.update(client, url);
      if(r==HTTP_UPDATE_FAILED) sendResult(id,"error",rv,String("OTA failed: ")+httpUpdate.getLastErrorString());
      // HTTP_UPDATE_OK reboots automatically into the new image
    }
  }
#if MOD_WEB_FETCH
  else if(type=="web_fetch"){
    String url=p["url"].as<String>(); HTTPClient h; h.begin(url);
    int code=h.GET(); String b=(code>0)?h.getString():""; h.end();
    res["status_code"]=code; res["len"]=b.length(); res["body"]=b.substring(0,400);
    sendResult(id, code>0?"done":"error", rv, code>0?"":"fetch failed");
  }
#endif
#if MOD_CONTROL
  else if(type=="control_set"){
    String v=p["value"].as<String>();
    bool on = (v=="1"||v=="on"||v=="true");
    if(v=="toggle") on=!digitalRead(RELAY_PIN);
    digitalWrite(RELAY_PIN, on?HIGH:LOW); res["channel"]=p["channel"]; res["value"]=on;
    sendResult(id,"done",rv,"");
  }
#endif
#if MOD_ALERT
  else if(type=="alert"){
    String msg=p["message"].as<String>();
    for(int i=0;i<6;i++){ digitalWrite(LED_PIN,HIGH);
      if(BUZZER_PIN>=0) digitalWrite(BUZZER_PIN,HIGH); delay(90);
      digitalWrite(LED_PIN,LOW); if(BUZZER_PIN>=0) digitalWrite(BUZZER_PIN,LOW); delay(90);}
    dspAlert(msg);
    sendResult(id,"done",rv,"");
  }
#endif
#if USE_TFT_ILI9488_P8 || defined(USE_TFT)
  else if(type=="kiosk_set"){
    // payload: {text?, title?, url?, mode:"status"?, bmp?, color?, bg?, size?, rotation?}
    String mode = p["mode"].as<String>();
#if USE_TFT_ILI9488_P8
    if(p["rotation"].is<int>()) p8.setRotation(p["rotation"].as<int>());
#endif
    if(mode=="status"){ kioskMode="status"; dspStatus(); }
    else if(mode=="test"){ dspTest(); res["shown"]="test-pattern"; }
#if USE_TFT_ILI9488_P8 && USE_SD_SPI
    else if(p.containsKey("bmp")){
      kioskMode="text";
      bool okb = dspBmp(p["bmp"].as<String>());
      if(!okb){ sendResult(id,"error",rv,"bmp: not found / not 24-bit uncompressed"); return; }
      res["shown"]=p["bmp"];
    }
#endif
    else {
      kioskMode="text";
      uint16_t fg = p["color"].is<int>() ? (uint16_t)p["color"].as<int>() : C_GREEN;
      uint16_t bg = p["bg"].is<int>()    ? (uint16_t)p["bg"].as<int>()    : C_BLACK;
      int size    = p["size"].is<int>()  ? p["size"].as<int>()            : 2;
      String txt  = p.containsKey("text") ? p["text"].as<String>()
                  : p.containsKey("url")  ? ("URL:\n" + p["url"].as<String>()) : String("");
      dspText(p["title"].as<String>(), txt, fg, bg, size);
      res["shown"]=txt.substring(0,80);
    }
    sendResult(id,"done",rv,"");
  }
#endif
  else if(type=="display_probe"){
#if USE_TFT_ILI9488_P8
    if(!p8.ok){ sendResult(id,"error",rv,"display init failed (bus not responding)"); }
    else {
      char h1[9], h2[9];
      sprintf(h1, "%08X", (unsigned)p8.readReg(0xD3, 4));
      sprintf(h2, "%08X", (unsigned)p8.readReg(0x04, 4));
      res["id_D3"]=h1; res["id_04"]=h2; res["w"]=p8.W; res["h"]=p8.H; res["enabled"]=true;
      sendResult(id,"done",rv,"");
    }
#else
    sendResult(id,"error",rv,"display disabled at flash time (USE_TFT_ILI9488_P8=0) — re-flash with Display enabled");
#endif
  }
#if USE_SD_SPI
  else if(type=="sd_list"){
    if(!sdOk && !sdMount()){ sendResult(id,"error",rv,"sd not mounted"); return; }
    String path = p.containsKey("path") ? p["path"].as<String>() : String("/");
    File dir = SD.open(path);
    if(!dir || !dir.isDirectory()){ sendResult(id,"error",rv,"not a directory: "+path); return; }
    JsonArray files = res.createNestedArray("files");
    int n=0;
    for(File f=dir.openNextFile(); f && n<64; f=dir.openNextFile(), n++){
      JsonObject e=files.createNestedObject();
      e["name"]=String(f.name()); e["size"]=(long)f.size(); e["dir"]=f.isDirectory();
      f.close();
    }
    dir.close(); res["count"]=n; res["path"]=path;
    res["total_mb"]=(int)(SD.totalBytes()/1048576UL); res["used_mb"]=(int)(SD.usedBytes()/1048576UL);
    sendResult(id,"done",rv,"");
  }
  else if(type=="sd_read"){
    if(!sdOk && !sdMount()){ sendResult(id,"error",rv,"sd not mounted"); return; }
    String path=p["path"].as<String>();
    int maxB = p["max"].is<int>() ? p["max"].as<int>() : 1024;
    if(maxB>1400) maxB=1400;                       // keep the result JSON small
    File f=SD.open(path);
    if(!f){ sendResult(id,"error",rv,"open failed: "+path); return; }
    String out; out.reserve(maxB);
    while(f.available() && (int)out.length()<maxB) out += (char)f.read();
    res["path"]=path; res["size"]=(long)f.size(); res["content"]=out;
    res["truncated"] = (long)f.size() > (long)out.length();
    f.close(); sendResult(id,"done",rv,"");
  }
  else if(type=="sd_write"){
    if(!sdOk && !sdMount()){ sendResult(id,"error",rv,"sd not mounted"); return; }
    String path=p["path"].as<String>(); String content=p["content"].as<String>();
    bool append = p["append"].as<bool>();
    File f=SD.open(path, append?FILE_APPEND:FILE_WRITE);
    if(!f){ sendResult(id,"error",rv,"open failed: "+path); return; }
    f.print(content); f.close();
    res["path"]=path; res["bytes"]=content.length();
    sendResult(id,"done",rv,"");
  }
  else if(type=="sd_delete"){
    if(!sdOk && !sdMount()){ sendResult(id,"error",rv,"sd not mounted"); return; }
    String path=p["path"].as<String>();
    bool okd = SD.remove(path); if(!okd) okd = SD.rmdir(path);
    res["path"]=path; res["deleted"]=okd;
    sendResult(id, okd?"done":"error", rv, okd?"":"delete failed");
  }
#endif
  else if(toolkitJob(id, type, p)){ /* handled by the ESP32 toolkit */ }
  else { sendResult(id,"error",rv,String("unknown type ")+type); }
}

// ── Long-poll ───────────────────────────────────────────────────────────────
void pollOnce(){
  if(WiFi.status()!=WL_CONNECTED) return;
  HTTPClient http;
  String url = SERVER+"/mesh/poll?node_id="+NODE+"&wait=25";
  if(TOKEN.length()&&TOKEN!="open") url += "&token="+TOKEN;
  http.begin(url); http.setTimeout(30000);
  int code=http.GET();
  if(code==200){
    String body=http.getString(); StaticJsonDocument<4096> d;
    if(!deserializeJson(d,body)){
      for(JsonObject job : d["jobs"].as<JsonArray>()) runJob(job);
    }
  }
  http.end();
}

// ── Worker module — run recurring edge tasks set via mesh.worker.assign ─────
void runWorkerTasks(){
  StaticJsonDocument<1024> d;
  if(deserializeJson(d, workerTasksJson)) return;
  int i=0;
  for(JsonObject t : d.as<JsonArray>()){
    if(i>=8) break;
    unsigned long iv=(unsigned long)t["interval_s"].as<int>()*1000UL; if(iv==0) iv=60000;
    if(millis()-workerLast[i]>=iv){
      workerLast[i]=millis();
      StaticJsonDocument<256> job;
      job["job_id"]=String("w")+i+String(millis());
      job["type"]=t["type"]; job["payload"]=t["payload"];
      runJob(job.as<JsonObject>());                 // reuse the dispatcher
    }
    i++;
  }
}

// ── Serial provisioning (Web Serial / pyserial) ─────────────────────────────
void handleSerialInput(){
  if(!Serial.available()) return;
  String line=Serial.readStringUntil('\n'); line.trim(); if(!line.length()) return;
  StaticJsonDocument<512> d; if(deserializeJson(d,line)) return;
  if(d["cmd"]=="provision"){
    wifiSsid=d["ssid"].as<String>(); wifiPass=d["pass"].as<String>();
    if(d.containsKey("server")) SERVER=d["server"].as<String>();
    if(d.containsKey("node_id")) NODE=d["node_id"].as<String>();
    prefs.begin("vera",false); prefs.putString("ssid",wifiSsid); prefs.putString("pass",wifiPass);
    prefs.putString("server",SERVER); prefs.putString("node",NODE); prefs.end();
    WiFi.begin(wifiSsid.c_str(), wifiPass.c_str());
  } else if(d["kind"]=="poll" || d["jobs"].is<JsonArray>()){
    for(JsonObject job : d["jobs"].as<JsonArray>()) runJob(job);   // jobs delivered over serial
  }
}

// ── Setup / loop ────────────────────────────────────────────────────────────
void setup(){
  Serial.begin(115200); delay(200);
  pinMode(LED_PIN,OUTPUT);
  if(BUZZER_PIN>=0) pinMode(BUZZER_PIN,OUTPUT);
#if MOD_CONTROL
  pinMode(RELAY_PIN,OUTPUT);
#endif
#if USE_TFT_ILI9488_P8
  p8.init();
  // Boot diagnostic (visible on the serial console you're already watching): read
  // the controller ID over the bus and print a verdict. 00 94 88 = ILI9488 & the
  // bus works. All 00/FF = the bus isn't talking — the usual cause on an S3 is a
  // data/control line on GPIO19/20 (native USB-Serial-JTAG) or a strapping pin.
  { uint32_t d3 = p8.readReg(0xD3, 4);
    Serial.printf("[tft] WR%d DC%d CS%d RST%d RD%d  D0..7=%d,%d,%d,%d,%d,%d,%d,%d  id_D3=%08X %s\n",
      p8.pWR, p8.pDC, p8.pCS, p8.pRST, p8.pRD,
      p8.pD[0],p8.pD[1],p8.pD[2],p8.pD[3],p8.pD[4],p8.pD[5],p8.pD[6],p8.pD[7],
      (unsigned)d3, ((d3 & 0xFFFF) == 0x9488) ? "ILI9488 OK" :
                    ((d3 & 0xFFFF) == 0x9486) ? "ILI9486 (wrong init!)" :
                    (((d3 & 0xFFFFFF) == 0) || ((d3 & 0xFFFF) == 0xFFFF)) ? "BUS DEAD - check D4=GPIO19/D5=GPIO20 (USB pins)" : "unknown");
    for (int b = 0; b < 8; b++) if (p8.pD[b] == 19 || p8.pD[b] == 20)
      Serial.printf("[tft] WARNING: data D%d is on GPIO%d = native USB pin; disable USB-CDC (console on UART0) or that bit is stuck\n", b, p8.pD[b]);
  }
  dspText("VERA NODE", "booting…\nconnecting Wi-Fi", C_WHITE, C_BLACK, 2);
#elif defined(USE_TFT)
  tft.init(); tft.setRotation(1); tft.fillScreen(TFT_BLACK);
#endif
#if USE_SD_SPI
  sdMount();       // non-fatal if absent — telemetry just skips sd_* metrics
#endif
  prefs.begin("vera",true);
  wifiSsid=prefs.getString("ssid",""); wifiPass=prefs.getString("pass","");
  if(prefs.getString("server","").length()) SERVER=prefs.getString("server",SERVER);
  if(prefs.getString("node","").length())   NODE=prefs.getString("node",NODE);
  prefs.end();
  if(NODE=="{{NODE_ID}}"||NODE.length()==0) NODE="esp32-"+chipId();

  if(wifiSsid.length()) WiFi.begin(wifiSsid.c_str(), wifiPass.c_str());
  unsigned long t0=millis();
  while(WiFi.status()!=WL_CONNECTED && millis()-t0<12000){ handleSerialInput(); delay(200); }
  if(WiFi.status()==WL_CONNECTED) enroll();
  if (kioskMode == "status") dspStatus();
}

void loop(){
  handleSerialInput();
  if(WiFi.status()==WL_CONNECTED){
    pollOnce();                                  // blocks up to ~25s (long-poll)
    if(millis()-lastTelemetry>telemetryEvery){ sendTelemetry(); lastTelemetry=millis(); }
#if MOD_WATCH
    if(watchEvery>0 && watchUrl.length() && millis()-lastWatch>(unsigned long)watchEvery){
      HTTPClient h; h.begin(watchUrl); int c=h.GET(); h.end(); lastWatch=millis();
      if(c<200||c>=400){ StaticJsonDocument<128> a; a["kind"]="telemetry"; a["node_id"]=NODE;
        a["metrics"]["watch_fail"]=c; String b; serializeJson(a,b); httpPost("/mesh/telemetry",b); }
    }
#endif
    runWorkerTasks();                            // autonomous edge tasks
#if TOOLKIT_CSI
    csiTick();                                   // fold CSI window → presence telemetry
#endif
  } else {
    if(wifiSsid.length()) WiFi.begin(wifiSsid.c_str(), wifiPass.c_str());
    delay(1500);
  }
}
