// Split the master canvas.json into three artifact sets, each laid out compactly on its own
// canvas (rows of ≤3 boards, ≥120px gaps) and carrying the annotations that belong to its boards.
//   node _split.mjs <dir>   → writes canvas-chat.json, canvas-widgets.json, canvas-system.json
import fs from 'node:fs';
import path from 'node:path';
const D = process.argv[2];
const master = JSON.parse(fs.readFileSync(path.join(D, 'canvas.json'), 'utf8'));
const SETS = {
  chat: ['Main.dc.html', 'Canvas.dc.html', 'ChatMenu.dc.html', 'Graph.dc.html', 'GraphViews.dc.html', 'Paste.dc.html', 'Arrivals.dc.html', 'JoinedUp.dc.html', 'QC.dc.html'],
  widgets: ['Widgets.dc.html', 'WidgetsMotion.dc.html', 'WidgetsIso.dc.html', 'WidgetConfig.dc.html', 'WidgetSpec.dc.html', 'Globes.dc.html', 'Sizes.dc.html', 'Formats.dc.html', 'Dashboard.dc.html', 'WidgetAdoption.dc.html'],
  system: ['Ops.dc.html', 'Harness.dc.html', 'Settings.dc.html', 'Coverage.dc.html', 'StylePacks.dc.html', 'Driven.dc.html'],
};
const byFile = Object.fromEntries(master.artboards.map((a) => [a.file, a]));
// an annotation belongs to the board whose frame it sits above (same x, y just above)
const noteOwner = (n) => {
  let best = null, bd = 1e9;
  for (const a of master.artboards){
    const dx = Math.abs(n.x - a.x), dy = a.y - (n.y + 80);
    const d = dx + (dy >= -40 && dy < 400 ? dy : 1e6);
    if (d < bd){ bd = d; best = a; }
  }
  return bd < 1e5 ? best.file : null;
};
const GAP_X = 120, GAP_Y = 200, NOTE_H = 110;
for (const [set, files] of Object.entries(SETS)){
  const present = files.filter((f) => byFile[f] && fs.existsSync(path.join(D, f)));
  const missing = files.filter((f) => !present.includes(f));
  if (missing.length) console.error(set + ': skipping absent boards ' + missing.join(', '));
  const boards = [], notes = [];
  let x = 0, y = 0, rowH = 0, col = 0;
  for (const f of present){
    const a = Object.assign({}, byFile[f]);
    if (col === 3 || (col > 0 && x + a.w > 5200)){ col = 0; x = 0; y += rowH + GAP_Y; rowH = 0; }
    a.x = x; a.y = y + NOTE_H; delete a.page;
    boards.push(a);
    for (const n of master.annotations || []){
      if (noteOwner(n) === f) notes.push(Object.assign({}, n, { x:a.x, y:a.y - NOTE_H }));
    }
    x += a.w + GAP_X; rowH = Math.max(rowH, a.h + NOTE_H); col++;
  }
  const out = { artboards:boards, annotations:notes, launch:{ view:'canvas' } };
  fs.writeFileSync(path.join(D, 'canvas-' + set + '.json'), JSON.stringify(out, null, 2) + '\n');
  console.log(set + ': ' + boards.length + ' boards · ' + notes.length + ' notes → canvas-' + set + '.json');
}
