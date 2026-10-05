// canvas-enrichment slice 4: the canvas before the answer - turn.prepare (surface.find, every read at once, what lands
// within the budget placed and told to the model, the rest landing while it writes); the chat stream hook; the chat's fields
//   node tests/test_turn_prepare_wiring.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
const E = R('vera/canvas/enrich_capabilities.py'), A = R('vera/agents/agents.py'), H = R('vera/chat/chat_panel.html');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('turn.prepare: the index, every read at once, what lands within the budget', E.includes('"turn.prepare", memory="off",') && E.includes('found = sc.find(await _surface_index(), text, ents,')
  && E.includes('done, pending = await asyncio.wait(tasks, timeout=left)'));
t('...the slower ones keep landing (DURING), held so they are not collected', E.includes('_DURING.add(t)') && E.includes('t.add_done_callback(_DURING.discard)'));
t('...keyed as canvas.enrich keys them (the after-reply pass brings the same item back)', E.includes('key = ec.item_key(cap, args, kind)'));
t('...a panel only for a domain that brought nothing else', E.includes('chosen = [s for s in surfaces if s["kind"] != "panel" or s["domain"] not in has_reads]'));
t('...and the note for the model', E.includes('"prompt": sc.prompt_block(placed, pending=len(pending)) if tell else ""'));
t('the chat stream runs it for the chat\'s own sends, within 1.5 s + margin, and adds the note', A.includes('if body.get("canvas_enrich") and session_id and "## On the canvas" not in _sys_prefix:')
  && A.includes('_cv_budget = 1.5') && A.includes('_cv_budget + 1.0)') && A.includes('_sys_prefix = (_sys_prefix + "\\n\\n" + _cvr["prompt"]).strip()'));
t('the chat sends its switch, the bare question and the turn', H.includes("canvas_enrich:_cvEnrichOn()||undefined, canvas_text:msg, canvas_mid:_cvRelMid||'',") && H.includes("localStorage.getItem('vera:canvas:tell')!=='0'"));
t('telling the model is a switch beside enrich on the canvas column, sent with each turn', H.includes('id="cvTellBtn" onclick="CH._cvTellToggle(this)"')
  && H.includes('canvas_tell:_cvTellOn(),') && H.includes('_cvEnrichToggle,_cvTellToggle,')
  && H.includes('try{ _cvEnrichPaint(); _cvTellPaint(); }catch(_){}'));
// owner, 2026-10-04: "im not seeing any of the markets info ... perhaps it did but i cant see it" - every enrich item
// started folded to its header line; what was placed for the question (and told to the model) shows open
{ const CV = R('vera/canvas/canvas_element.js');
  t('items placed for the question are from=prepare and show open; the unasked after-reply ones still start folded',
    E.includes('anchor = {"turn": mid, "mid": mid, "from": "prepare"} if mid else {"from": "prepare"}')
    && CV.includes("x.from === 'prepare'") && CV.includes('if (prepared) return false;') && CV.includes("return from === 'enrich' || key.indexOf('enrich:') === 0;"));
  t('the note leads with what the turn named', E.includes('summary=sc.summarise(res, focus=tickers)')); }
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);