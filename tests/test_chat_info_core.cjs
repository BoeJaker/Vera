// 2026-10-10 (owner): "if i ask chat to get the latest updates on a topic it often reaches for the research family of
// caps ... sometimes it uses memories and fills in the gaps with guesses instead of going and finding the facts".
// Found: for "what's the latest on X" the chat's word scoring offered NO tools (no web cap's text says "latest"), so the
// model answered from training. The information core (web.research, web.fetch) is now offered on every non-trivial
// message (info-seeking plan SK-1.1, shared-planning/intelligence/PLAN-INFO-SEEKING.md).
//   node tests/test_chat_info_core.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const CHAT = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

// lift the real definitions out of the page and run them
const grab = (re, what) => { const m = CHAT.match(re); if (!m) { t('found ' + what, false); process.exit(1); } return m[0]; };
const src = [
  grab(/const _INFO_CORE_CAPS=\[[^\]]*\];/, '_INFO_CORE_CAPS'),
  grab(/const _INFO_ASK_RE=\/.*\/i;/, '_INFO_ASK_RE'),
  grab(/function _wantsInfoCore\(text\)\{[\s\S]*?\n  \}/, '_wantsInfoCore'),
  grab(/const _TRIVIAL_CHAT_RE=\/.*\/iu;/, '_TRIVIAL_CHAT_RE'),
  grab(/function _isTrivialChat\(text\)\{[\s\S]*?\n  \}/, '_isTrivialChat'),
].join('\n');
const api = new Function(src + '\nreturn {_INFO_CORE_CAPS, _wantsInfoCore};')();

t('the core is web.research and web.fetch', JSON.stringify(api._INFO_CORE_CAPS) === '["web.research","web.fetch"]');
for (const q of ["what's the latest on OpenAI", 'latest on MCP', 'updates on UK house prices?', 'who won?',
                 'find out about the EU AI Act', 'any news about n8n', 'what is the base rate now'])
  t('offers the core: ' + q, api._wantsInfoCore(q));
for (const q of ['hi', 'thanks!', 'ok', 'good morning', 'lol', ''])
  t('small talk gets no core: ' + JSON.stringify(q), !api._wantsInfoCore(q));

// the seed sits in buildCapHint before the "nothing relevant → no hint" return, and only for caps the agent has
const hint = CHAT.slice(CHAT.indexOf('if(_wantsInfoCore(query||\'\')){'), CHAT.indexOf("if(!relevantNames.length) return '';"));
t('buildCapHint seeds the core before giving up on an empty manifest', hint.length > 0 && hint.length < 600, hint.length);
t('the seed is limited to caps the agent allows', /_INFO_CORE_CAPS\.filter\(n=>allowedCaps\.includes\(n\)&&ALL_CAPS\.some/.test(hint));

// the hint tells the model a current fact is not a "simple question"
t('the cap hint says current facts are looked up, cited and dated', /is NOT simple: look it up first if a web cap is listed, cite what you use as \[n\] with its date/.test(CHAT));

if (fails) { console.log(fails + ' failed'); process.exit(1); }
console.log('all passed');
