/* vera/ui/menus.js — the ONE context-menu registry (UI redesign, Notes/40 §2; the design's _lib/menus.js).
   ───────────────────────────────────────────────────────────────────────────────────────────────────────
   What a right-click OFFERS is decided by the KIND of thing under the pointer: an entity the fabric types
   (person, host, commit…), a memory (a recall, a vector hit, a graph hop…), or an element of the UI (a message,
   a tool call, a canvas item of some form, a widget…). Each kind lists the actions that suit it, then the
   capabilities worth staging against it, and every kind ends with the same tail: ask Vera, pin, provenance,
   print, copy. Every context menu in the app reads from here; the runner opens at the click, never in a corner.
   Served at /ui/menus.js (vera/ui/libs.py).

   Rows:  { t:'act', id, n, k, cls }   an action the host binds by id (a host that cannot act on an id still
                                        shows it, so the menu reads the same everywhere)
          { t:'cap', cap, arg }         a capability staged in the runner with these arguments
   API — window.MENUS = { rows(kind, name, opts?), kindOf(kind), label(kind), kinds }                          */
(function () {
  'use strict';
  if (window.MENUS) return;
  const A = (id, n, k, cls) => ({ t:'act', id, n, k:k || '', cls:cls || '' });
  const C = (cap, arg) => ({ t:'cap', cap, arg });
  const q = 'What is this, what depends on it, and what would break if it changed?';
  const K = {
    /* ── entities the fabric types ── */
    person:   (nm) => [A('open', 'Open the profile', '↵'), A('sessions', 'Sessions with them'), A('changes', 'What they changed'),
                A('message', 'Message them'), C('fabric.query', { entity:nm, rel:'ACTED_ON' })],
    host:     (nm) => [A('terminal', 'Open a terminal here', '⌘T'), A('files', 'Browse its files', '⌘F'), A('metrics', 'Metrics'),
                A('logs', 'Logs', '⌘L'), A('ops', 'Show in live operations'), C('nodes.probe', { node:nm }),
                C('sandbox.session.exec', { node:nm, cmd:'vera node status' }), C('sysmon.status', { node:nm })],
    ssh:      (nm) => [A('attach', 'Attach to the session', '↵'), A('canvas', 'Open on the canvas — shared'), A('files', 'Browse its files', '⌘F'),
                A('close', 'Close the session', '', 'danger'), C('sandbox.session.exec', { session:nm, cmd:'tail -n1 vera_start.log' })],
    container:(nm) => [A('logs', 'Logs', '⌘L'), A('terminal', 'Exec a shell', '⌘T'), A('files', 'Browse its files', '⌘F'),
                A('pause', 'Pause'), A('restart', 'Restart', '', 'danger'), C('evolve.sandbox_status', { name:nm }),
                C('evolve.sandbox_metrics', { name:nm })],
    service:  (nm) => [A('logs', 'Logs', '⌘L'), A('health', 'Health'), A('routes', 'Routing to it'),
                A('restart', 'Restart', '', 'danger'), C('obs.health', { service:nm }), C('obs.diagnostics', { service:nm })],
    node:     (nm) => [A('open', 'Open the node', '↵'), A('metrics', 'Metrics'), A('drain', 'Drain'),
                C('nodes.probe', { node:nm }), C('obs.node_temps', { node:nm })],
    file:     (nm) => [A('open', 'Open in the IDE', '↵'), A('history', 'History'), A('blame', 'Who wrote this'),
                A('canvas', 'Pin to the canvas', '⌘P'), C('code.read', { path:nm }),
                C('code.versions', { path:nm }), C('obs.provenance', { entity:nm, depth:2 })],
    commit:   (nm) => [A('diff', 'Show the diff', '↵'), A('files', 'Files changed'), A('pipeline', 'The pipeline that landed it'),
                A('revert', 'Revert', '', 'danger'), C('evolve.git_graph', { ref:nm }), C('code.diff', { ref:nm })],
    branch:   (nm) => [A('open', 'Open the branch', '↵'), A('graph', 'Show in the git graph'), A('compare', 'Compare to bleeding-edge'),
                C('evolve.git_status', { branch:nm }), C('evolve.pipeline_list', { branch:nm })],
    pipeline: (nm) => [A('open', 'Open the pipeline', '↵'), A('review', 'Review'), A('promote', 'Promote'),
                A('rollback', 'Roll back', '', 'danger'), C('evolve.pipeline_get', { id:nm }), C('evolve.pipeline_diff', { id:nm })],
    cap:      (nm) => [A('run', 'Run it', '↵'), A('docs', 'Capability docs'), A('routes', 'Route stats'), A('stage', 'Stage with a payload'),
                C('obs.provenance', { cap:nm }), C('evolve.cap_test', { cap:nm })],
    dataset:  (nm) => [A('sample', 'Sample rows', '↵'), A('schema', 'Schema'), A('canvas', 'Table on the canvas', '⌘P'),
                C('fabric.schema', { dataset:nm }), C('fabric.query', { dataset:nm, limit:20 }), C('fabric.validate', { dataset:nm })],
    model:    (nm) => [A('open', 'Open in the catalog', '↵'), A('load', 'Keep resident'), A('unload', 'Unload'),
                A('bench', 'Benchmark'), C('ollama.request', { model:nm, prompt:'ping' }), C('catalog.info', { model:nm })],
    skill:    (nm) => [A('open', 'Open the skill', '↵'), A('applies', 'What it applies to'), A('inject', 'Inject into this prompt', '⌘P'),
                C('kb.query', { q:nm })],
    loop:     (nm) => [A('open', 'Open the program', '↵'), A('pause', 'Pause'), A('step', 'Step once'), A('replan', 'Re-plan'),
                A('cancel', 'Cancel', '', 'danger'), C('loops.status', { id:nm }), C('obs.provenance', { loop:nm })],
    step:     (nm) => [A('jump', 'Jump to this step', '↵'), A('read', 'What it read'), A('made', 'What it produced'),
                A('replan', 'Re-plan from here'), A('skip', 'Skip', '', 'danger'), C('obs.provenance', { step:nm })],
    plan:     (nm) => [A('open', 'Open the plan', '↵'), A('replan', 'Re-plan'), A('canvas', 'Plan on the canvas', '⌘P'),
                C('dag.plan', { goal:nm })],
    goal:     (nm) => [A('open', 'Open the goal', '↵'), A('loop', 'Run a loop on it'), A('schedule', 'Schedule it'),
                C('goals.detail', { goal:nm })],
    dream:    (nm) => [A('open', 'Open the journal', '↵'), A('watch', 'Watch it live'), A('trigger', 'Run a cycle now'),
                C('dream.status', {})],
    sandbox:  (nm) => [A('attach', 'Attach', '↵'), A('terminal', 'Terminal', '⌘T'), A('diff', 'Diff against main'),
                A('pause', 'Pause'), A('stop', 'Stop', '', 'danger'), C('evolve.sandbox_status', { name:nm }),
                C('evolve.sandbox_logs', { name:nm })],
    /* ── memory, by what kind of memory it is ── */
    memory:   (nm) => [A('pin', 'Pin into the prompt', '⌘P'), A('why', 'Why was this recalled'), A('related', 'Related memories'),
                A('edit', 'Edit'), A('forget', 'Forget this memory', '', 'danger'), C('memory.select', { about:nm, k:6 })],
    vector:   (nm) => [A('pin', 'Pin into the prompt', '⌘P'), A('source', 'Open the source'), A('neighbours', 'Nearest neighbours'),
                A('score', 'Why this score'), C('memory.select', { about:nm, k:6, mode:'vector' })],
    hop:      (nm) => [A('pin', 'Pin into the prompt', '⌘P'), A('path', 'Show the path'), A('expand', 'Expand one more hop'),
                C('fabric.query', { entity:nm, hops:2 })],
    record:   (nm) => [A('open', 'Open the record', '↵'), A('history', 'History'), A('related', 'Related records'),
                A('canvas', 'Pin to the canvas', '⌘P'), C('fabric.query', { entity:nm }), C('fabric.upsert', { entity:nm })],
    ontology: (nm) => [A('open', 'Open the term', '↵'), A('instances', 'Instances'), A('parents', 'Broader terms'),
                C('kb.query', { q:nm })],
    session:  (nm) => [A('open', 'Open the session', '↵'), A('resume', 'Resume it'), A('summary', 'Summarise it'),
                A('export', 'Export'), C('memory.select', { about:nm, k:6 })],
    /* ── the UI's own elements ── */
    message:  (nm) => [A('reply', 'Reply to this', '↵'), A('edit', 'Edit'), A('retry', 'Retry from here'), A('branch', 'Branch the conversation here'),
                A('notebook', 'Send to the notebook'), A('speak', 'Speak it'), A('canvas', 'Pin to the canvas', '⌘P'),
                A('copy', 'Copy text', '⌘C')],
    turn:     (nm) => [A('explode', 'Explode this turn'), A('context', 'What it read'), A('made', 'What it produced'),
                A('pin', 'Pin into the prompt', '⌘P'), A('collapse', 'Collapse')],
    tool:     (nm) => [A('open', 'Open the result', '↵'), A('rerun', 'Run again'), A('payload', 'Show the payload'),
                A('canvas', 'Result on the canvas', '⌘P'), C('obs.provenance', { cap:nm })],
    cite:     (nm) => [A('open', 'Open the source', '↵'), A('range', 'Show the range read'), A('pin', 'Pin into the prompt', '⌘P'),
                C('code.read', { path:nm })],
    ask:      (nm) => [A('answer', 'Answer in the composer', '↵'), A('defer', 'Ask me on Telegram instead'), A('program', 'Open the program'),
                A('cancel', 'Cancel the run', '', 'danger')],
    /* canvas items, by form */
    note:     (nm) => [A('edit', 'Edit in place', '↵'), A('ctx', 'Toggle in context', '⌘I'), A('notebook', 'Send to the notebook'),
                A('dup', 'Duplicate', '⌘D'), A('remove', 'Remove from canvas', '⌫', 'danger'), C('content.edit', { item:nm, mode:'inline' })],
    terminal: (nm) => [A('attach', 'Attach', '↵'), A('open', 'Open across the canvas'), A('share', 'Share the session'),
                A('clear', 'Clear'), A('remove', 'Close and remove', '⌫', 'danger'), C('sandbox.session.exec', { session:nm, cmd:'' })],
    control:  (nm) => [A('open', 'Open across the canvas', '↵'), A('run', 'Run the default action'), A('who', 'Who surfaced this'),
                A('remove', 'Remove from canvas', '⌫', 'danger'), C('panel.dispatch', { control:nm })],
    artifact: (nm) => [A('open', 'Open', '↵'), A('print', 'Print'), A('deliver', 'Deliver again'), A('pin', 'Pin'),
                A('remove', 'Remove from canvas', '⌫', 'danger'), C('output.redeliver', { item:nm, channel:'telegram' })],
    chart:    (nm) => [A('open', 'Open across the canvas', '↵'), A('data', 'Show the data'), A('iso', 'Isometric'),
                A('ctx', 'Toggle in context', '⌘I'), A('remove', 'Remove from canvas', '⌫', 'danger'), C('report.html', { chart:nm })],
    table:    (nm) => [A('open', 'Open across the canvas', '↵'), A('sort', 'Sort…'), A('csv', 'Export CSV'),
                A('ctx', 'Toggle in context', '⌘I'), A('remove', 'Remove from canvas', '⌫', 'danger'), C('fabric.query', { dataset:nm })],
    code:     (nm) => [A('open', 'Open in the IDE', '↵'), A('run', 'Run it'), A('diff', 'Diff against main'),
                A('ctx', 'Toggle in context', '⌘I'), A('remove', 'Remove from canvas', '⌫', 'danger'), C('code.read', { path:nm })],
    diff:     (nm) => [A('open', 'Open the diff', '↵'), A('apply', 'Apply'), A('revert', 'Revert', '', 'danger'),
                A('remove', 'Remove from canvas', '⌫', 'danger'), C('code.diff', { ref:nm })],
    panel:    (nm) => [A('open', 'Open across the canvas', '↵'), A('standalone', 'Open standalone'), A('refresh', 'Refresh'),
                A('remove', 'Remove from canvas', '⌫', 'danger'), C('panel.dispatch', { panel:nm })],
    widget:   (nm) => [A('open', 'Deep dive', '↵'), A('iso', 'Isometric'), A('dash', 'Add to the dashboard'), A('resize', 'Resize…'),
                A('remove', 'Remove from canvas', '⌫', 'danger'), C('sysmon.status', {})],
    notebook: (nm) => [A('open', 'Open the notebook', '↵'), A('run', 'Run the cell'), A('ctx', 'Toggle in context', '⌘I'),
                A('remove', 'Remove from canvas', '⌫', 'danger'), C('notebook.append', { note:nm })],
    calendar: (nm) => [A('open', 'Open the calendar', '↵'), A('add', 'Add an event'), A('remove', 'Remove from canvas', '⌫', 'danger'),
                C('sched.list', {})],
    checklist:(nm) => [A('tick', 'Tick the next item', '↵'), A('loop', 'Run a loop on the rest'), A('ctx', 'Toggle in context', '⌘I'),
                A('remove', 'Remove from canvas', '⌫', 'danger'), C('goals.detail', { goal:nm })],
    stat:     (nm) => [A('open', 'Deep dive', '↵'), A('trend', 'Trend it'), A('alert', 'Alert on it'),
                A('remove', 'Remove from canvas', '⌫', 'danger'), C('obs.provenance', { entity:nm })],
    /* the surfaces themselves */
    canvas:   (nm) => [A('add', 'Add an item…'), A('fit', 'Fit to view'), A('edges', 'Toggle edges'), A('tidy', 'Tidy the layout'),
                A('export', 'Export'), C('notebook.append', { note:'canvas snapshot' })],
    layer:    (nm) => [A('focus', 'Focus this layer', '↵'), A('solo', 'Solo it'), A('hide', 'Hide it'), A('explain', 'What is on it')],
    view:     (nm) => [A('fit', 'Back to fit'), A('zoomin', 'Zoom in', '+'), A('zoomout', 'Zoom out', '−'), A('edges', 'Toggle relations'),
                A('explode', 'Explode / flatten')]
  };
  // the same tail on every kind
  const tail = (nm) => [A('ask', 'Ask Vera about this · with its context', '?'), A('pin', 'Pin to the canvas', '⌘P'),
    C('chat.ask', { about:nm, question:q }), C('obs.provenance', { entity:nm, depth:2 }),
    C('print.card', { item:nm, printer:'thermal-01', width:'58mm' }), A('copy', 'Copy reference', '⌘C')];
  const generic = (nm) => [A('open', 'Open', '↵'), A('expand', 'Expand'), A('trace', 'Trace to source'),
    A('filter', 'Filter to this'), C('fabric.query', { entity:nm, hops:2 })];
  // the names surfaces use for a kind → the registry's kinds
  const ALIAS = { 'canvas item':'note', 'context node':'record', entity:'record', term:'terminal', ssh:'ssh', ctl:'control',
    art:'artifact', kpi:'stat', series:'chart', nb:'notebook', cal:'calendar', check:'checklist', tree:'table',
    'graph-hop':'hop', 'fabric-record':'record', q:'message', a:'message', cap:'cap', lps:'step', lcard:'loop' };
  const kindOf = (k) => K[k] ? k : (ALIAS[k] || null);
  // every row for a kind, tailored ones first, the shared tail last. Rows the
  // host cannot bind still show, so the menu reads the same on every surface.
  const rows = (kind, nm, opts) => {
    const k = kindOf(kind);
    const own = k ? K[k](nm) : generic(nm);
    const tl = tail(nm).filter((r) => !(opts && opts.noPin && r.id === 'pin'));
    return own.concat(tl);
  };
  const label = (kind) => ({ record:'fabric record', hop:'graph hop', vector:'vector hit', message:'message', tool:'tool call',
    cite:'citation', ask:'decision', note:'canvas item', terminal:'terminal', control:'controls', artifact:'artifact',
    step:'loop step', loop:'agentic loop' })[kindOf(kind) || kind] || (kindOf(kind) || kind);
  window.MENUS = { rows, kindOf, label, kinds:Object.keys(K) };
})();
