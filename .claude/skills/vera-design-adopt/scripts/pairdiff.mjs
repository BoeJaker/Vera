// The PAIR VERIFIER — a design render and a live render, compared by number, not by eye. Pure JS (node:zlib):
// decodes both PNGs (8-bit grey / grey+alpha / RGB / RGBA, non-interlaced — what headless Chrome writes), compares
// them over their common area, and reports the share of pixels that differ beyond a threshold, the mean absolute
// difference, a 4×4 grid of where the differences sit, and the bounding box of change; optionally writes a diff
// PNG (the design dimmed, differing pixels in red) and gates with --max <pct> (exit 1 above it).
//   node pairdiff.mjs <design.png> <live.png> [--out diff.png] [--threshold 24] [--max 100] [--json out.json]
import fs from 'node:fs';
import zlib from 'node:zlib';
const args = process.argv.slice(2); const flags = {}; const pos = [];
for (let i = 0; i < args.length; i++){ const a = args[i]; if (a.startsWith('--')) flags[a.slice(2)] = args[i + 1] && !args[i + 1].startsWith('--') ? args[++i] : true; else pos.push(a); }
const [A, B] = pos; if (!A || !B) { console.error('usage: node pairdiff.mjs <design.png> <live.png> [--out diff.png] [--threshold 24] [--max 100] [--json out.json]'); process.exit(2); }
const THR = +(flags.threshold || 24), MAX = +(flags.max || 100);

// ── PNG decode → { w, h, px: Uint8Array RGBA } ────────────────────────────────────────────────────────────────
const decode = (file) => {
  const buf = fs.readFileSync(file);
  if (buf.readUInt32BE(0) !== 0x89504e47) throw new Error(file + ': not a PNG');
  let p = 8, w = 0, h = 0, depth = 0, ctype = 0, interlace = 0; const idat = [];
  while (p < buf.length){ const len = buf.readUInt32BE(p); const type = buf.toString('latin1', p + 4, p + 8); const data = buf.subarray(p + 8, p + 8 + len);
    if (type === 'IHDR'){ w = data.readUInt32BE(0); h = data.readUInt32BE(4); depth = data[8]; ctype = data[9]; interlace = data[12]; }
    else if (type === 'IDAT') idat.push(data); else if (type === 'IEND') break; p += 12 + len; }
  if (depth !== 8) throw new Error(file + ': ' + depth + '-bit PNG — only 8-bit is read');
  if (interlace) throw new Error(file + ': interlaced PNG — not read');
  const ch = { 0:1, 2:3, 4:2, 6:4 }[ctype]; if (!ch) throw new Error(file + ': colour type ' + ctype + ' (palette) — not read');
  const raw = zlib.inflateSync(Buffer.concat(idat)); const stride = w * ch; const out = new Uint8Array(w * h * 4);
  let prev = new Uint8Array(stride), cur = new Uint8Array(stride), q = 0;
  for (let y = 0; y < h; y++){ const f = raw[q++]; for (let i = 0; i < stride; i++){ const x = raw[q++]; const a = i >= ch ? cur[i - ch] : 0, b = prev[i], c = i >= ch ? prev[i - ch] : 0; let v;
      if (f === 0) v = x; else if (f === 1) v = x + a; else if (f === 2) v = x + b; else if (f === 3) v = x + ((a + b) >> 1);
      else { const pp = a + b - c, pa = Math.abs(pp - a), pb = Math.abs(pp - b), pc = Math.abs(pp - c); v = x + (pa <= pb && pa <= pc ? a : pb <= pc ? b : c); }
      cur[i] = v & 255; }
    for (let x = 0; x < w; x++){ const o = (y * w + x) * 4, s = x * ch;
      if (ch === 1){ out[o] = out[o + 1] = out[o + 2] = cur[s]; out[o + 3] = 255; } else if (ch === 2){ out[o] = out[o + 1] = out[o + 2] = cur[s]; out[o + 3] = cur[s + 1]; }
      else if (ch === 3){ out[o] = cur[s]; out[o + 1] = cur[s + 1]; out[o + 2] = cur[s + 2]; out[o + 3] = 255; } else { out[o] = cur[s]; out[o + 1] = cur[s + 1]; out[o + 2] = cur[s + 2]; out[o + 3] = cur[s + 3]; } }
    [prev, cur] = [cur, prev]; }
  return { w, h, px:out };
};
// ── PNG encode (RGB, filter 0) ─────────────────────────────────────────────────────────────────────────────────
const CRC = new Int32Array(256); for (let n = 0; n < 256; n++){ let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; CRC[n] = c; }
const crc32 = (b) => { let c = -1; for (let i = 0; i < b.length; i++) c = CRC[(c ^ b[i]) & 255] ^ (c >>> 8); return (c ^ -1) >>> 0; };
const chunk = (type, data) => { const len = Buffer.alloc(4); len.writeUInt32BE(data.length); const td = Buffer.concat([Buffer.from(type, 'latin1'), data]); const crc = Buffer.alloc(4); crc.writeUInt32BE(crc32(td)); return Buffer.concat([len, td, crc]); };
const encode = (w, h, rgb) => { const raw = Buffer.alloc((w * 3 + 1) * h); for (let y = 0; y < h; y++){ raw[y * (w * 3 + 1)] = 0; rgb.copy ? rgb.copy(raw, y * (w * 3 + 1) + 1, y * w * 3, (y + 1) * w * 3) : raw.set(rgb.subarray(y * w * 3, (y + 1) * w * 3), y * (w * 3 + 1) + 1); }
  const ihdr = Buffer.alloc(13); ihdr.writeUInt32BE(w, 0); ihdr.writeUInt32BE(h, 4); ihdr[8] = 8; ihdr[9] = 2; ihdr[10] = 0; ihdr[11] = 0; ihdr[12] = 0;
  return Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), chunk('IHDR', ihdr), chunk('IDAT', zlib.deflateSync(raw)), chunk('IEND', Buffer.alloc(0))]); };

// ── compare ───────────────────────────────────────────────────────────────────────────────────────────────────
const a = decode(A), b = decode(B);
const w = Math.min(a.w, b.w), h = Math.min(a.h, b.h);
const grid = Array.from({ length:16 }, () => ({ n:0, d:0 }));
let diff = 0, sum = 0, minX = w, minY = h, maxX = -1, maxY = -1;
const out = flags.out ? Buffer.alloc(w * h * 3) : null;
for (let y = 0; y < h; y++) for (let x = 0; x < w; x++){
  const ia = (y * a.w + x) * 4, ib = (y * b.w + x) * 4;
  const d = Math.max(Math.abs(a.px[ia] - b.px[ib]), Math.abs(a.px[ia + 1] - b.px[ib + 1]), Math.abs(a.px[ia + 2] - b.px[ib + 2]));
  sum += d; const g = grid[((y * 4 / h) | 0) * 4 + ((x * 4 / w) | 0)]; g.n++;
  const hit = d > THR; if (hit){ diff++; g.d++; if (x < minX) minX = x; if (x > maxX) maxX = x; if (y < minY) minY = y; if (y > maxY) maxY = y; }
  if (out){ const o = (y * w + x) * 3; if (hit){ out[o] = 255; out[o + 1] = 40; out[o + 2] = 40; } else { out[o] = a.px[ia] >> 2; out[o + 1] = a.px[ia + 1] >> 2; out[o + 2] = a.px[ia + 2] >> 2; } }
}
const n = w * h, pct = +(100 * diff / n).toFixed(2);
const report = { design:A, live:B, sizes:{ design:[a.w, a.h], live:[b.w, b.h], compared:[w, h], sameSize:a.w === b.w && a.h === b.h }, threshold:THR, diffPct:pct, meanAbs:+(sum / n).toFixed(2),
  bbox:maxX < 0 ? null : [minX, minY, maxX - minX + 1, maxY - minY + 1], grid:grid.map((g) => +(100 * g.d / Math.max(1, g.n)).toFixed(1)), pass:pct <= MAX, max:MAX };
if (out) fs.writeFileSync(flags.out, encode(w, h, out));
if (flags.json) fs.writeFileSync(flags.json, JSON.stringify(report, null, 1));
const rows = [0, 1, 2, 3].map((r) => report.grid.slice(r * 4, r * 4 + 4).map((v) => String(v).padStart(5)).join(' '));
console.log('pair: ' + pct + '% of ' + w + '×' + h + ' differ beyond ' + THR + ' (mean |Δ| ' + report.meanAbs + ')' + (report.sizes.sameSize ? '' : ' · sizes differ: design ' + a.w + '×' + a.h + ' vs live ' + b.w + '×' + b.h) + (report.bbox ? ' · change within ' + report.bbox.join(',') : ' · identical') + (out ? ' → ' + flags.out : ''));
console.log('      4×4 grid (% differing):\n      ' + rows.join('\n      '));
if (!report.pass){ console.log('FAIL: above --max ' + MAX + '%'); process.exit(1); }
