// Minimal CDP driver: launch headless Chrome, load the seeded canvas, collect
// console/page errors from every frame, then report what each artboard did.
import net from "node:net";
import http from "node:http";
import crypto from "node:crypto";
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const DIR = process.argv[2];
const FILE = process.argv[3] || "vera-ui-redesign.html";
const WAIT = Number(process.argv[4] || 20000);
const SHOT = process.argv[5] || null;
const PORT = 9331 + (Number(process.env.CDP_OFFSET) || 0);
// CHROME_PATH overrides; the default is Chrome's usual Windows location
const CHROME = process.env.CHROME_PATH || "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";

const sleep = ms => new Promise(r => setTimeout(r, ms));

/* ---------- tiny websocket client ---------- */
class WS {
  constructor(url) {
    const u = new URL(url);
    this.onmsg = () => {};
    this.buf = Buffer.alloc(0);
    this.ready = new Promise((res, rej) => {
      this.sock = net.connect(Number(u.port), u.hostname, () => {
        const key = crypto.randomBytes(16).toString("base64");
        this.sock.write(
          `GET ${u.pathname}${u.search} HTTP/1.1\r\nHost: ${u.host}\r\nUpgrade: websocket\r\n` +
          `Connection: Upgrade\r\nSec-WebSocket-Key: ${key}\r\nSec-WebSocket-Version: 13\r\n\r\n`
        );
      });
      this.sock.on("error", rej);
      let handshook = false;
      this.sock.on("data", d => {
        this.buf = Buffer.concat([this.buf, d]);
        if (!handshook) {
          const i = this.buf.indexOf("\r\n\r\n");
          if (i < 0) return;
          this.buf = this.buf.subarray(i + 4);
          handshook = true;
          res();
        }
        this.drain();
      });
    });
  }
  drain() {
    for (;;) {
      const b = this.buf;
      if (b.length < 2) return;
      const op = b[0] & 0x0f;
      let len = b[1] & 0x7f, off = 2;
      if (len === 126) { if (b.length < 4) return; len = b.readUInt16BE(2); off = 4; }
      else if (len === 127) { if (b.length < 10) return; len = Number(b.readBigUInt64BE(2)); off = 10; }
      if (b.length < off + len) return;
      const payload = b.subarray(off, off + len);
      this.buf = b.subarray(off + len);
      if (op === 1) { try { this.onmsg(JSON.parse(payload.toString("utf8"))); } catch {} }
      if (op === 8) { this.sock.end(); return; }
    }
  }
  send(obj) {
    const data = Buffer.from(JSON.stringify(obj), "utf8");
    const mask = crypto.randomBytes(4);
    const n = data.length;
    let head;
    if (n < 126) { head = Buffer.alloc(2); head[1] = 0x80 | n; }
    else if (n < 65536) { head = Buffer.alloc(4); head[1] = 0x80 | 126; head.writeUInt16BE(n, 2); }
    else { head = Buffer.alloc(10); head[1] = 0x80 | 127; head.writeBigUInt64BE(BigInt(n), 2); }
    head[0] = 0x81;
    const out = Buffer.from(data);
    for (let i = 0; i < n; i++) out[i] ^= mask[i & 3];
    this.sock.write(Buffer.concat([head, mask, out]));
  }
}

/* ---------- driver ---------- */
// a fresh profile per run: a previous Chrome still holding the old one made
// the driver die on unlink, and killing browsers to clear it is not on
const profile = path.join(DIR, "_cdp_profile_" + process.pid + "_" + Date.now());
try { fs.rmSync(profile, { recursive: true, force: true }); } catch {}
const chrome = spawn(CHROME, [
  "--headless=new", "--disable-gpu", "--no-sandbox", "--allow-file-access-from-files",
  "--ignore-certificate-errors",   // Vera sandboxes serve self-signed TLS
  "--window-size=1900,1100", `--user-data-dir=${profile}`,
  `--remote-debugging-port=${PORT}`, "about:blank",
], { stdio: "ignore" });

const getJSON = p => new Promise((res, rej) => {
  http.get({ host: "127.0.0.1", port: PORT, path: p }, r => {
    let s = ""; r.on("data", c => s += c); r.on("end", () => { try { res(JSON.parse(s)); } catch (e) { rej(e); } });
  }).on("error", rej);
});

let targets = null;
for (let i = 0; i < 60 && !targets; i++) { try { targets = await getJSON("/json/list"); } catch { await sleep(300); } }
if (!targets) { console.log("could not reach chrome"); chrome.kill(); process.exit(1); }

const page = targets.find(t => t.type === "page");
const ws = new WS(page.webSocketDebuggerUrl);
await ws.ready;

let id = 0;
const pending = new Map();
const logs = [];
ws.onmsg = m => {
  if (m.id != null && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
  if (m.method === "Runtime.consoleAPICalled" && ["error", "warning"].includes(m.params.type))
    logs.push("console." + m.params.type + ": " + m.params.args.map(a => a.value ?? a.description ?? a.type).join(" "));
  if (m.method === "Runtime.exceptionThrown") {
    const d = m.params.exceptionDetails;
    logs.push("EXCEPTION: " + (d.exception?.description || d.text) + "  @" + (d.url || "?") + ":" + d.lineNumber);
  }
  if (m.method === "Log.entryAdded" && ["error", "warning"].includes(m.params.entry.level))
    logs.push("log." + m.params.entry.level + ": " + m.params.entry.text + " @" + (m.params.entry.url || "?"));
};
const cmd = (method, params = {}, sessionId) => new Promise(res => {
  const i = ++id; pending.set(i, res);
  ws.send(sessionId ? { id: i, method, params, sessionId } : { id: i, method, params });
});

await cmd("Runtime.enable");
await cmd("Log.enable");
await cmd("Page.enable");
await cmd("Target.setAutoAttach", { autoAttach: true, waitForDebuggerOnStart: false, flatten: true });
ws.onmsgOuter = ws.onmsg;

// FILE may be a page in DIR (a seeded design canvas) or a full http(s) URL (a live Vera page);
// the shot always lands in DIR
const url = /^https?:\/\//.test(FILE) ? FILE : "file:///" + path.join(DIR, FILE).replace(/\\/g, "/");
if (process.env.THROTTLE) await cmd("Emulation.setCPUThrottlingRate", { rate: Number(process.env.THROTTLE) });
await cmd("Page.navigate", { url });
await sleep(WAIT);
if (process.env.CLICK) {
  await cmd("Runtime.evaluate", { expression: `(()=>{const t=${JSON.stringify(process.env.CLICK)};
    const el=[...document.querySelectorAll("button,[role=button],a")].find(e=>(e.innerText||"").trim().includes(t));
    if(el){el.click();return "clicked";} return "not found";})()`, returnByValue: true })
    .then(x => console.log("click(" + process.env.CLICK + "): " + (x.result?.result?.value)));
  await sleep(Number(process.env.CLICK_WAIT || 20000));
}

if (process.env.WHEEL) {
  const [wx, wy, wd] = process.env.WHEEL.split(",").map(Number);
  for (let i = 0; i < 8; i++) {
    await cmd("Input.dispatchMouseEvent", { type: "mouseWheel", x: wx, y: wy,
      deltaX: 0, deltaY: wd, modifiers: 0, pointerType: "mouse" });
    await sleep(90);
  }
  await sleep(1200);
  console.log("wheel dispatched at " + wx + "," + wy);
}
if (process.env.CLICKXY) {
  for (const pair of process.env.CLICKXY.split(";")) {
    const [cx, cy, n] = pair.split(",").map(Number);
    for (let k = 0; k < (n || 1); k++) {
      for (const type of ["mousePressed", "mouseReleased"])
        await cmd("Input.dispatchMouseEvent", { type, x: cx, y: cy, button: "left",
          clickCount: 1, buttons: type === "mousePressed" ? 1 : 0 });
      await sleep(260);
    }
    console.log("clicked " + cx + "," + cy + " x" + (n || 1));
  }
  await sleep(1100);
}
/* DRAG="x1,y1,x2,y2[,steps]" — press at (x1,y1), move in steps to (x2,y2), release. The way to
   test pan by mouse: compare the shot against an undragged one. */
if (process.env.DRAG) {
  const [x1, y1, x2, y2, st] = process.env.DRAG.split(",").map(Number);
  const steps = st || 12;
  await cmd("Input.dispatchMouseEvent", { type: "mouseMoved", x: x1, y: y1 });
  await sleep(80);
  await cmd("Input.dispatchMouseEvent", { type: "mousePressed", x: x1, y: y1, button: "left", clickCount: 1, buttons: 1 });
  await sleep(80);
  for (let i = 1; i <= steps; i++) {
    await cmd("Input.dispatchMouseEvent", { type: "mouseMoved", x: x1 + (x2 - x1) * i / steps, y: y1 + (y2 - y1) * i / steps, button: "left", buttons: 1 });
    await sleep(40);
  }
  await cmd("Input.dispatchMouseEvent", { type: "mouseReleased", x: x2, y: y2, button: "left", clickCount: 1, buttons: 0 });
  await sleep(900);
  console.log("dragged " + x1 + "," + y1 + " -> " + x2 + "," + y2);
}
if (process.env.RCLICK) {
  const [rx, ry] = process.env.RCLICK.split(",").map(Number);
  for (const type of ["mousePressed", "mouseReleased"])
    await cmd("Input.dispatchMouseEvent", { type, x: rx, y: ry, button: "right",
      clickCount: 1, buttons: type === "mousePressed" ? 2 : 0 });
  await sleep(1000);
  console.log("right-clicked " + rx + "," + ry);
}
if (process.env.HOVERXY) {
  const [hx, hy] = process.env.HOVERXY.split(",").map(Number);
  await cmd("Input.dispatchMouseEvent", { type: "mouseMoved", x: hx - 40, y: hy - 40 });
  await sleep(120);
  await cmd("Input.dispatchMouseEvent", { type: "mouseMoved", x: hx, y: hy });
  await sleep(900);
  console.log("hovered " + hx + "," + hy);
}
const probe = `(() => {
  const out = { stopped: [], loadfail: [], frames: [], bodyText: "" };
  for (const el of document.querySelectorAll("div")) {
    const t = (el.textContent || "").trim();
    if (el.children.length === 0 || t.length < 400) {
      if (/^Preview stopped/.test(t)) out.stopped.push(t.slice(0, 300));
      if (/^Couldn't load this artboard/.test(t)) out.loadfail.push(t.slice(0, 300));
    }
  }
  out.stopped = [...new Set(out.stopped)];
  out.loadfail = [...new Set(out.loadfail)];
  out.frames = [...document.querySelectorAll("iframe")].map(f => f.title || "(untitled)");
  out.broken = [];
  for (const el of document.querySelectorAll("div")) {
    const t = (el.textContent || "").trim();
    if (/^(Preview stopped|Couldn.t load this artboard)/.test(t) && t.length < 400) {
      let p = el, name = "?";
      for (let i = 0; i < 12 && p; i++, p = p.parentElement) {
        const f = p.querySelector && p.querySelector("iframe");
        if (f && f.title) { name = f.title; break; }
        if (p.dataset && p.dataset.file) { name = p.dataset.file; break; }
      }
      out.broken.push(name + " :: " + t.slice(0, 160));
    }
  }
  out.broken = [...new Set(out.broken)];
  out.bodyText = (document.body.innerText || "").slice(0, 600);
  return JSON.stringify(out);
})()`;
const r = await cmd("Runtime.evaluate", { expression: probe, returnByValue: true });
const val = r.result?.result?.value;
console.log("=== probe ===");
console.log(val || JSON.stringify(r.result));
console.log("=== logs (" + logs.length + ") ===");
[...new Set(logs)].slice(0, 40).forEach(l => console.log("  " + l.slice(0, 320)));

/* WHEEL={"x":900,"y":500,"dy":120,"n":4} — scroll whatever is under the point
   before the shot. Input events hit-test through the sandboxed artboard iframe,
   which Runtime.evaluate cannot reach into. */
if (process.env.WHEEL && process.env.WHEEL.trim().startsWith("{")) {
  const w = JSON.parse(process.env.WHEEL);
  for (let i = 0; i < (w.n || 1); i++) {
    await cmd("Input.dispatchMouseEvent", { type: "mouseWheel", x: w.x, y: w.y,
      deltaX: 0, deltaY: w.dy || 120, pointerType: "mouse" });
    await sleep(90);
  }
  await sleep(700);
}

if (SHOT) {
  const clipEnv = process.env.CLIP ? JSON.parse(process.env.CLIP) : null;
  const s = await cmd("Page.captureScreenshot", clipEnv ? { format: "png", clip: clipEnv } : { format: "png" });
  if (s.result?.data) { fs.writeFileSync(path.join(DIR, SHOT), Buffer.from(s.result.data, "base64")); console.log("shot -> " + SHOT); }
}
chrome.kill();
// the profile is this run's own; drop it so runs never pile up on disk
setTimeout(() => { try { fs.rmSync(profile, { recursive: true, force: true }); } catch {} process.exit(0); }, 400);
