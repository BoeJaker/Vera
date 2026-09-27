// 2026-09-27 (owner): "make caps output to widgets" - the chat and its canvas draw an answer through the shared mapping
//   node tests/test_cap_views_wired.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const CHAT = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('the card leads with the widget the answer is', /const rs=VeraWidget\.fromCapResult\(capName, content, \{args:capArgs\}\);/.test(CHAT) && /<vera-widget bare size=/.test(CHAT));
t('a plain json/text/record keeps the older rule', /!\/\^\(json\|text\|record\|kv\)\$\/\.test\(r0\.form\)/.test(CHAT) && /if\(!ruleWidget\) try\{/.test(CHAT));
t('a string answer leads with its widget too', /: \`\$\{ruleWidget\}<pre style=/.test(CHAT));
t('the canvas maps through it after the chat-only kinds, before the old generics', /cvAdapter\('capview',/.test(CHAT) && CHAT.indexOf("cvAdapter('capview',") < CHAT.indexOf('what a result is when none of the above') && CHAT.indexOf("cvAdapter('capview',") > CHAT.indexOf("cvAdapter('prose',"));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
