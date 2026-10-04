// The chat LHM's Agents menu: pick, edit (only the fields changed are saved), create, use in this chat - all widgets.
//   node tests/test_chat_lhm_agents_menu.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const H = fs.readFileSync(path.join(__dirname, '..', 'vera/chat/chat_panel.html'), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('a menu of the LHM library, with its description and primary action', H.includes("{id:'agents',   icon:'◈', label:'Agents',    desc:'Pick, edit or create an agent',") && H.includes("cta:{label:'+ New agent', run:()=>_agentNew()}"));
t('its tab and pane exist and the tab renders the list', H.includes(`onclick="CH.railTab('Agents',this)">Agents</div>`) && H.includes('<div class="rpane" id="rpAgents">') && H.includes("if(name==='Agents')_renderRailAgents();"));
t('the list and the editor are docked into the menu', H.includes("agents:   { panes:[{ id:'rpAgentList', n:'Agents', tab:'Agents', cls:'dk-list', load:()=>_renderRailAgents() },") && H.includes("{ id:'rpAgentEdit', n:'Edit agent', tab:'Agents', cls:'dk-agent-edit' }] },"));
t('the list is a widget over agent.list, a card per agent by name; a pick opens the editor', H.includes("JSON.stringify({ form:'cards', title:'Agents', source:'agent.list',") && H.includes("read:{ map:{ rows:'agents', fields:['label','model'] } }, draw:{ limit:80, columns:2 } }));") && H.includes("ev.preventDefault(); _agentEdit(name);"));
t('the menu has its quick body and so its dock', H.includes('ops:_quickOps, agents:_quickAgents, settings:_quickSettings};') && H.includes('function _quickAgents(el){'));
t('the editor is a form over agent.get that saves through agent.patch, only what changed', H.includes("source:'agent.get', read:{ args:{ name, force_refresh:true } },") && H.includes("save:{ cap:'agent.patch', from:{ agent_id:'id' }, edits_as:'fields', label:'Save changes' }"));
t('a new agent is the same form, saved through agent.create', H.includes("save:{ cap:'agent.create', label:'Create agent' }"));
t('use in this chat goes through the chat\'s own agent picker', H.includes("sel.value=name; try{ agentBarChanged(); }catch(_){}"));
t('the editor\'s section grows with its form', H.includes('#rightRail.lhm-host .lhm-quick .lhm-dk.dk-agent-edit .lhm-dk-b > *{height:auto!important;'));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
