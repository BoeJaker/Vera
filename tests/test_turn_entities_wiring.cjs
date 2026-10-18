// canvas-enrichment slice 1: every reply is read by the NLP tier (turn.understand) and what it names lands as one
// `entities` item; a chip opens its sentences and looks the name up beside it (turn.entity.look)
//   node tests/test_turn_entities_wiring.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
const H = R('vera/chat/chat_panel.html'), C = R('vera/canvas/canvas_element.js'), CC = R('vera/canvas/canvas_capabilities.py'), E = R('vera/canvas/enrich_capabilities.py');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };

t('after a reply lands, the chat asks what it names', H.includes("try{ _cvEntities(_cvRelMid, String(fullText||'')); }catch(_){}") && H.includes("r=await _capCall('turn.understand',{ text:t.slice(0,12000), session_id:SID, mid:String(mid||''), place:true")   /* look:N fetches for the leading names (c2c70d6c) */);
t('...only for a reply with some prose, and the canvas switch turns it off', H.includes("if(!SID||!_cvEnrichOn()||t.replace(/```[\\s\\S]*?```/g,'').trim().length<160) return null;"));
t('...and the answer is kept with the message', H.includes('_MSG_REGISTRY[mid].entities=r.entities||[];'));
t('the stale "nlp.ner does not work" note is corrected', H.includes('nlp.ner answers on the CPU nodes in about half a'));
t('turn.understand: NER chunk by chunk in parallel, merged, placed as one item keyed by the turn', E.includes('"turn.understand", memory="off",') && E.includes('async def _ner(text: str)') && E.includes('key = "entities:" + (mid or')
  && E.includes('kind="entities", key=key'));
t('turn.entity.look: the fabric (and the web for things outside) as records browsers', E.includes('"turn.entity.look", memory="off",') && E.includes('steps = [("fabric.query", {"text": name, "top_k": 30}, "memory"') && E.includes('steps.append(("web.search", {"query": name, "limit": 20}, "web"'));
t('the canvas knows the `entities` kind', CC.includes('"entities": {"desc": "What a turn NAMES') && C.includes('entities: (c, size, key, el) => {'));
t('...chips in their groups; a pressed chip opens its sentences and can be looked up', C.includes('data-act="entopen" data-ent="${id}"') && C.includes('data-act="entlook"')
  && C.includes("if (act === 'entopen') {") && C.includes("this.callResult('turn.entity.look', { session_id: this._sid(), text: e.text, type: e.type, mid: c.mid || '' })"));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
