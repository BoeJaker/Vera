// WSU-45: a restored transcript keeps its tool cards. The stored follow-up turns ("The capability X ran and returned:
// ...") are read back into results for the reply that called the tool, and are not drawn as something the user said.
// Run: node tests/test_chat_restore_caps.cjs
const fs = require('fs');
const path = require('path');

const chat = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
let fails = 0;
const t = (name, ok) => { console.log((ok ? 'ok   ' : 'FAIL ') + name); if (!ok) fails++; };

const re = chat.match(/const _CAP_FOLLOWUP_RE=(\/.*\/);\n/);
const fn = chat.match(/  function _capFollowupParse\(txt\)\{[\s\S]*?\n  \}\n/);
t('the follow-up parser is in the chat page', !!re && !!fn);
const parse = new Function(`const _CAP_FOLLOWUP_RE=${re[1]};\n${fn[0]}\nreturn _capFollowupParse;`)();

const ok = parse('The capability ide.fs.write ran and returned:\n\n{\n  "path": "/workspace/lan_scanner.py",\n  "bytes": 1754\n}\n\nAnswer the user\'s question using this result. Be concise and complete.');
t('a result comes back as the object it was', ok && ok.name === 'ide.fs.write' && !ok.err && ok.content.bytes === 1754);

const txt = parse('The capability shell.run ran and returned:\n\ntotal 8\n-rw-r--r-- a.txt\n\nAnswer the user\'s question using this result. Be concise and complete.');
t('a text result stays text', txt && txt.content === 'total 8\n-rw-r--r-- a.txt');

const bad = parse('The capability "exec.python.run" was called but failed.\n\nHTTP status: 200\nError: unknown error\n\nArguments passed: {}');
t('a failure is an error card with its message', bad && bad.name === 'exec.python.run' && bad.err && bad.content.error === 'unknown error');

const rep = parse('The capability web.research produced a full research report, and that report is ALREADY DISPLAYED ...\n\nFor your reference, the report was:\n\n## Findings\nA');
t('a research report keeps its text', rep && rep.content.report === '## Findings\nA');

t('anything the user really said is not a follow-up', parse('get the latest in AI and ML') === null && parse('The capability list is long') === null);

t('loadSession skips follow-ups and draws replies with their cards (nothing runs)',
  chat.includes("if(rec._capFollowup){ HISTORY.push({role:'user',content:rec.full_text||rec.text}); continue; }")
  && chat.includes('m.body.appendChild(_restoreAgentBody(txt, _capRes.get(rec)||[]))')
  && !/function _restoreAgentBody[\s\S]{0,4000}executeCapInline/.test(chat));
t('the live call and the restore draw one card', chat.includes('const _rep=_capCardDraw(el, capName, capArgs, coercedArgs, content, isErr);')
  && chat.includes('_capCardDraw(el, name, args, _coerceCapArgs(name,args), r.content, r.err)'));
t('an injected studio block is not shown as the user\'s words', chat.includes("txt.includes('\\n\\n[STUDIO')"));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
