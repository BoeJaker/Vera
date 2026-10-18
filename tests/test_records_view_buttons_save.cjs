// A records card's own view buttons (list / cards / table / graph) write the view to the item record, as the head's View
// menu does (WSU-58 S1): without it a choice made in the card is lost on reload.
const fs = require('fs'); const path = require('path'); const assert = require('assert');
const src = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
const i = src.indexOf('async _recAct(key, act, arg)'); assert(i > 0, '_recAct found');
const body = src.slice(i, i + 4000);
const line = body.slice(body.indexOf("if (act === 'sort' || act === 'view')"));
assert(/^if \(act === 'sort' \|\| act === 'view'\)[\s\S]{0,200}if \(act === 'view'\) viewSet\(this, key, arg\)/.test(line), 'view act calls viewSet');
assert(/const viewSet = \(el, key, v\) => \{[^\n]*el\._itemSet\(key, \{ view: v \}\)/.test(src), 'viewSet writes the record');
console.log('test_records_view_buttons_save: 2 passed');
