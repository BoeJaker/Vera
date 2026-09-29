// 2026-09-27 (owner): "can the chat UI itself be defined as a widget ... the same ui element but with different agents and
// system prompts per implementation"
//   node tests/test_chat_element.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', ...p.split('/')), 'utf8');
const EL = R('vera/ui/chat.js'), CHAT = R('vera/chat/chat_panel.html'), AP = R('vera/agents/agent_panel.html'), LIBS = R('vera/ui/libs.py'), D = R('vera/ui/design.css');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

t('<vera-chat> is the chat, framed in its embed mode', /customElements\.define\('vera-chat', VeraChatElement\)/.test(EL) && /new URLSearchParams\(\{ only: 'chat', embed: '1' \}\)/.test(EL));
t('agent, system, session, title are its attributes, and re-point it', /var ATTRS = \['agent', 'system', 'session', 'title'\];/.test(EL) && /attributeChangedCallback\(\)\{ if \(this\.isConnected\) this\._render\(\); \}/.test(EL));
t('served', /@APP\.get\("\/ui\/chat\.js"/.test(LIBS));
t('the chat takes them from its URL', /var _EMBED_CHAT=\(function\(\)\{ try\{ const q=new URLSearchParams\(location\.search\); return \{ agent:q\.get\('agent'\)/.test(CHAT));
t('the placement\'s agent wins', /const target=\(_EMBED_CHAT\.agent && AGENTS\.find\(a=>a\.name===_EMBED_CHAT\.agent\)\)\?_EMBED_CHAT\.agent/.test(CHAT));
t('its system prompt goes before the agent\'s own', /system_prefix:\(\(_EMBED_CHAT\.system\?_EMBED_CHAT\.system\+'\\n\\n':''\)\+\(ctxBlock\|\|''\)\)\|\|undefined/.test(CHAT));
t('its session opens', /initSid\(opts\.sessionId\|\|_EMBED_CHAT\.session\|\|undefined\);/.test(CHAT));
t('compact when placed', /html\[data-embed\] #topBar\{display:none!important\}/.test(CHAT) && /setAttribute\('data-embed',''\)/.test(CHAT));
t('the Agents panel\'s test chat is one', /<vera-chat id="test-chat" session="agent-panel-test"/.test(AP) && /getElementById\('test-chat'\)\?\.setAttribute\('agent', a\.name\|\|'assistant'\)/.test(AP) && /<script src="\/ui\/chat\.js"><\/script>/.test(AP));
t('dashboard tiles are blocks', /\.dash-grid > \.widget\) \{/.test(D));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
