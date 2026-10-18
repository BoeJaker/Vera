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

t('the core is info.seek, web.research and web.fetch', JSON.stringify(api._INFO_CORE_CAPS) === '["info.seek","web.research","web.fetch"]');
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

// SK-2.1: injected memory lines carry their date and the block says what it is; the first line stays exactly
// "[Retrieved Context]" (memory_noise_core cuts the stored message there)
const ctx = new Function(grab(/const _CTX_INJ_NOTE=[\s\S]*?;\n/, '_CTX_INJ_NOTE') + grab(/function _ctxDateTag\(n\)\{[\s\S]*?\n  \}/, '_ctxDateTag')
  + '\nreturn {_CTX_INJ_NOTE, _ctxDateTag};')();
t('a dated record gets its day', ctx._ctxDateTag({created_at: '2026-09-14T10:22:00Z'}) === ' \u00b7 2026-09-14');
t('an undated record gets nothing (never "now")', ctx._ctxDateTag({ts: Date.now()}) === '' && ctx._ctxDateTag({created_at: 'yesterday'}) === '');
t('the note says it is not the user and not proof of now', /not typed by the user/.test(ctx._CTX_INJ_NOTE) && /not proof of how things are now/.test(ctx._CTX_INJ_NOTE));
t('the block still opens with the bare [Retrieved Context] line', /return'\\n\\n\[Retrieved Context\]\\n'\+_CTX_INJ_NOTE\+'\\n'\+items\.join\('\\n'\);/.test(CHAT));
t('each line is tagged with its date', /return`\[\$\{src\}\$\{_ctxDateTag\(n\)\}\] /.test(CHAT));

// SK-2.3: after a lookup the follow-up asks for [n] citations with dates and no gap-filling
const look = new Function(grab(/function _isLookupCap\(name\)\{[\s\S]*?\n  \}/, '_isLookupCap') + '\nreturn _isLookupCap;')();
t('lookup caps are recognised', ['web.research', 'web.fetch', 'reader.page', 'intel.lookup', 'info.seek'].every(look));
t('other caps are not', !look('cal.events.list') && !look('web.research.extra') && !look('mail.search'));
t('the follow-up after a lookup asks for dated [n] citations and no guesses',
  /\+ \(_isLookupCap\(capName\)/.test(CHAT) && /cite them as \[n\] with their date/.test(CHAT) && /do not `\s*\+ `fill the gap from memory or a guess/.test(CHAT));

// SK-3.7: a background report reaches the chat. 7 Oct: an 18-minute research job never did - the WebSocket failed and
// polling stopped at 5 minutes, silently. info.seek need=deep returns a job_id with no status, so nothing followed it.
t('polling lasts as long as a report can (maxWaitMs), not 5 minutes', !/Math\.min\(maxWaitMs,300000\)/.test(CHAT)
  && /while\(!cancelled&&Date\.now\(\)-start<maxWaitMs\)\{/.test(CHAT));
t('a report still running when the wait ends says so', /if\(!finished&&!cancelled\)\{\s*setStatus\(`Still running after/.test(CHAT));
t('info.seek deep: the report it started is followed into the chat, not awaited',
  /if\(capName==='info\.seek'&&content&&typeof content==='object'&&content\.job_id\) _followReport\(content\.job_id,agentName\);/.test(CHAT)
  && /function _followReport\(jobId, agentName\)\{[\s\S]{0,900}_streamResearchJob\(jobId,agentName,card\)\.catch\(/.test(CHAT));

// SK-1.1: the picker matches whole words - "out" (find out about) no longer matches output/about/without, "rate" no
// longer matches generate; plurals still match
{
  const pick = new Function('ALL_CAPS',
    grab(/const _CAP_STOPWORDS=new Set\(\[[\s\S]*?\]\);/, '_CAP_STOPWORDS') + '\n'
    + grab(/const _CAP_MIN_SCORE=\d+;/, '_CAP_MIN_SCORE') + '\n'
    + grab(/function _findRelevantCaps\(query, allowedCaps, maxN=24\)\{[\s\S]*?\n  \}/, '_findRelevantCaps')
    + '\nreturn _findRelevantCaps;');
  const caps = [
    {name: 'web.research', description: 'look something up on the web: a fact, the latest news, updates', tags: ['web', 'latest', 'news']},
    {name: 'media.export', description: 'writes output files about without limits', tags: ['media']},
    {name: 'image.generate', description: 'generate an image at any rate', tags: ['image']},
    {name: 'markets.rate', description: 'an interest rate series', tags: ['markets']},
  ];
  const f = pick(caps);
  const all = caps.map(c => c.name);
  const r1 = f('find out about the latest EU AI Act news', all, 10);
  t('whole words: "out"/"about" do not pull in an output-writing tool', r1.includes('web.research') && !r1.includes('media.export'), JSON.stringify(r1));
  const r2 = f('current interest rate figures', all, 10);
  t('"rate" matches the rate tool, not generate', r2.includes('markets.rate') && !r2.includes('image.generate') || r2.indexOf('markets.rate') < r2.indexOf('image.generate'), JSON.stringify(r2));
  t('plurals match ("updates" finds "update"-tagged text)', f('any news updates please', all, 10).includes('web.research'));
}

if (fails) { console.log(fails + ' failed'); process.exit(1); }
console.log('all passed');
