/* ============================================================================
 * <vera-agent-loop-output>  —  Reusable agentic loop output renderer
 * ============================================================================
 *
 * A self-contained custom element that renders the full event stream of an
 * agentic loop run (DAG-Workshop-grade UI) — triage banner, dynamic toolkit,
 * cycle cards with thinking / args / live progress / research streams /
 * error-recovery boxes / long-running awaits, HITL pause cards, handover
 * synthesis output, and a structured final-result pane.
 *
 * This is the SAME renderer used by the DAG Workshop's Agent Loop tab,
 * lifted into a registered injectable element so it can be reused anywhere
 * (chat UI, capability_orchestration sub-panels, dream panel, etc.) without
 * duplicating the implementation.
 *
 * USAGE
 * ─────
 *   <script src="/cap_hub/elements.js"><\/script>
 *   <vera-agent-loop-output></vera-agent-loop-output>
 *
 * Then either:
 *   (a) Feed it raw SSE events:
 *         el.appendEvent({type:'agent_loop_v2.cycle_planning', cycle:1, ...});
 *   (b) Bind it to a stream URL:
 *         el.bindStream('/workshop/agent_loop/stream', requestBody);
 *   (c) Reset between runs:
 *         el.reset();
 *
 * PUBLIC API
 * ──────────
 *   el.appendEvent(ev)         — feed one parsed SSE event
 *   el.appendEvents(arr)       — bulk-feed an array of events
 *   el.reset()                 — clear everything (cycles, triage, toolkit, final)
 *   el.bindStream(url, body)   — fetch SSE from url with POST body, stream events
 *   el.abort()                 — abort any in-flight bound stream
 *   el.getResult()             — returns the last `result`/`done` payload or null
 *   el.setSessionId(sid)       — used for HITL respond callbacks
 *   el.setHitlEndpoint(url)    — override the HITL respond endpoint (default
 *                                "/workshop/agent_loop/hitl/respond")
 *   el.setApiBase(url)         — override the API base (default _veraBase or origin)
 *   el.setShowThinking(bool)   — toggle the model-thinking blocks
 *   el.setMaxResultPreview(n)  — char cap for the inline final-result preview
 *
 * ATTRIBUTES (all optional)
 * ─────────────────────────
 *   compact="true"             — slimmer styling for chat-message contexts
 *   show-final="true|false"    — render the structured final pane (default true)
 *   show-toolkit="true|false"  — show toolkit chips strip (default true)
 *   show-triage="true|false"   — show triage banner (default true)
 *   show-thinking="true|false" — show model-thinking <details> blocks (default true)
 *   max-height="400"           — pixels; height of the cycles list area
 *
 * EVENTS DISPATCHED (CustomEvents on the element, all bubble:true)
 * ────────────────────────────────────────────────────────────────
 *   alo:cycle-start    {cycle}                — new cycle card created
 *   alo:tool-call      {cycle, tool, args}    — tool invocation rendered
 *   alo:tool-done      {cycle, tool, ok}      — tool finished
 *   alo:hitl-request   {cycle, step, tool}    — pause card shown
 *   alo:hitl-resolved  {step, decision}       — HITL decision sent
 *   alo:done           {summary, cycles, ok}  — loop finished
 *   alo:final          {payload}              — structured final result arrived
 *   alo:error          {error}                — error event
 * ============================================================================
 */
(function(){
  if(window.customElements && window.customElements.get('vera-agent-loop-output')) return;

  // ───────────────────── Helpers (scoped to this module) ────────────────────
  function _esc(s){
    return String(s==null?'':s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  }

  /* Sticky-bottom auto-scroll. Only follows new content when the user is already
     pinned to (near) the bottom of `el`; if they've scrolled up to read, their
     position is preserved. A scroll listener (wired once per element) tracks the
     pinned state, so appending content never yanks the viewport away from the
     user. Replaces the old unconditional `el.scrollTop = el.scrollHeight`. */
  function _follow(el){
    if(!el) return;
    if(!el.__aloStickWired){
      el.__aloStickWired = true;
      el.__aloStick = true;   // start pinned so the first cards auto-follow
      el.addEventListener('scroll', function(){
        el.__aloStick = (el.scrollHeight - el.scrollTop - el.clientHeight) <= 48;
      }, {passive:true});
    }
    if(el.__aloStick !== false) el.scrollTop = el.scrollHeight;
  }

  /* Balance a PARTIAL JSON stream so it renders as (near-)valid while it builds:
     close any open string and unmatched {} / [] — the same idea as auto-closing
     a code block mid-stream. Display only; the real plan replaces it when done. */
  function _autoCloseStructured(s){
    if(!s) return '';
    let inStr=false, esc=false; const stack=[];
    for(let i=0;i<s.length;i++){
      const c=s[i];
      if(esc){esc=false;continue;}
      if(c==='\\'&&inStr){esc=true;continue;}
      if(c==='"'){inStr=!inStr;continue;}
      if(inStr) continue;
      if(c==='{'||c==='[') stack.push(c);
      else if(c==='}'||c===']') stack.pop();
    }
    let out=s;
    if(inStr) out+='"';
    for(let i=stack.length-1;i>=0;i--) out += (stack[i]==='{'?'}':']');
    return out;
  }

  /** Rolling "thinking" phrases shown during planning / long waits.
      Shares the chat panel's localStorage list (vera_think_phrases) so a
      user-themed set applies to both the chat throbber and the loop. */
  const ALO_PHRASE_DEFAULTS = [
    'thinking through the plan','weighing the options','lining up the steps',
    'consulting the capability catalog','sketching the route','sequencing the work',
    'sizing up the goal','checking what tools fit','tracing dependencies',
    'letting it percolate','sharpening the plan','mapping the terrain',
  ];
  function ALO_THINK_PHRASES(){
    try{
      const l = JSON.parse(localStorage.getItem('vera_think_phrases')||'null');
      if(Array.isArray(l) && l.length > 2) return l;
    }catch(_){}
    return ALO_PHRASE_DEFAULTS;
  }

  /** Pretty-format args as readable pills/spans instead of raw JSON. */
  function _fmtArgs(args, maxLen){
    maxLen = maxLen || 280;
    if(!args || typeof args !== 'object') return '';
    const keys = Object.keys(args);
    if(!keys.length) return '';
    const parts = [];
    let total = 0;
    for(const k of keys){
      let v = args[k];
      if(v === undefined || v === null) continue;
      let vfull, vs;
      if(typeof v === 'string'){
        vfull = v;
      } else if(typeof v === 'boolean' || typeof v === 'number'){
        vfull = String(v);
      } else {
        vfull = JSON.stringify(v);
      }
      vs = vfull.length > 80 ? vfull.slice(0,77)+'…' : vfull;
      const isTrunc = vfull.length > vs.length;
      // Truncated pills carry the full value + short form so a delegated click
      // handler can toggle them open (key info is often past the 80-char cut).
      const attrs = isTrunc
        ? ` class="alo-arg-pill trunc" data-full="${_esc(vfull.slice(0,6000))}" data-short="${_esc(vs)}" title="click to expand"`
        : ' class="alo-arg-pill"';
      const part = `<span${attrs}><span class="alo-arg-key">${_esc(k)}</span><span class="alo-arg-val">${_esc(vs)}</span></span>`;
      total += k.length + vs.length + 4;
      if(total > maxLen && parts.length > 0){ parts.push('<span class="alo-arg-ellip">…</span>'); break; }
      parts.push(part);
    }
    return parts.join(' ');
  }

  /** Try to parse and pretty-format a JSON string as pill layout.
      For objects with short values → pills. For objects with any large text
      value (>120 chars) → pills for short keys + a text block for the long one.
      Falls back to escaped text if not JSON. */
  function _fmtOutput(text, maxLen){
    if(!text || typeof text !== 'string') return _esc(text||'(empty)');
    const trimmed = text.trim();
    if(trimmed.startsWith('{') || trimmed.startsWith('[')){
      try{
        const parsed = JSON.parse(trimmed);
        if(typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)){
          const keys = Object.keys(parsed);
          // Check if any value is a large text body
          let longKey = null, longVal = '';
          for(const k of keys){
            const v = parsed[k];
            if(typeof v === 'string' && v.length > 120){
              longKey = k;
              longVal = v;
              break;
            }
          }
          if(longKey){
            // Render short keys as pills, long key as a text block below
            const shortObj = {};
            for(const k of keys){ if(k !== longKey) shortObj[k] = parsed[k]; }
            const pillsHtml = _fmtArgs(shortObj, maxLen || 400);
            const blockHtml = `<div style="width:100%;margin-top:3px"><span class="alo-arg-pill" style="margin-bottom:2px"><span class="alo-arg-key">${_esc(longKey)}</span></span><pre style="margin:2px 0 0;padding:4px 6px;background:var(--bg2,#252220);border-radius:3px;font-size:9.5px;white-space:pre-wrap;word-break:break-word;max-height:180px;overflow-y:auto;color:var(--text2,#bfb6a8);font-family:var(--mono,monospace)">${_esc(longVal)}</pre></div>`;
            return (pillsHtml ? pillsHtml + ' ' : '') + blockHtml;
          }
          const html = _fmtArgs(parsed, maxLen || 600);
          if(html) return html;
        }
      }catch(_){}
    }
    // Not JSON or array — return as pre-formatted text block
    if(text.length > 120){
      return `<pre style="margin:0;padding:4px 6px;background:var(--bg2,#252220);border-radius:3px;font-size:9.5px;white-space:pre-wrap;word-break:break-word;max-height:180px;overflow-y:auto;color:var(--text2,#bfb6a8);font-family:var(--mono,monospace);width:100%">${_esc(text)}</pre>`;
    }
    return _esc(text);
  }

  function _apiBase(){
    try{
      if(window._veraBase) return String(window._veraBase).replace(/\/$/,'');
      if(window.parent && window.parent._veraBase) return String(window.parent._veraBase).replace(/\/$/,'');
    }catch(_){}
    return location.origin;
  }

  // Lightweight markdown → HTML renderer used for handover output and
  // final synthesised answers. Intentionally minimal — covers what the
  // agent loop emits without pulling in a full parser.
  function _renderMarkdown(md){
    if(!md) return '';
    let s = _esc(md);
    s = s.replace(/```(\w*)\n([\s\S]*?)```/g,
      (_, lang, body) => `<pre class="md-code"${lang?` data-lang="${_esc(lang)}"`:''}>${body}</pre>`);
    s = s.replace(/`([^`]+?)`/g, '<code class="md-inline">$1</code>');
    s = s.replace(/^####\s+(.+)$/gm, '<h4 class="md-h4">$1</h4>');
    s = s.replace(/^###\s+(.+)$/gm,  '<h3 class="md-h3">$1</h3>');
    s = s.replace(/^##\s+(.+)$/gm,   '<h2 class="md-h2">$1</h2>');
    s = s.replace(/^#\s+(.+)$/gm,    '<h1 class="md-h1">$1</h1>');
    s = s.replace(/\*\*([^\*]+?)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(?:^|[\s_])_([^_\n]+?)_(?=[\s.,!?:;)]|$)/g, ' <em>$1</em>');
    s = s.replace(/(?:^|\s)\*([^*\n]+?)\*(?=[\s.,!?:;)]|$)/g, ' <em>$1</em>');
    s = s.replace(/\[([^\]]+?)\]\(([^)]+?)\)/g,
      (_, text, url) => `<a class="md-link" href="${_esc(url)}" target="_blank" rel="noopener">${text}</a>`);
    // GitHub-style tables: a header row, a |---|---| separator, then body rows.
    // Runs after inline formatting so cell contents (bold/code/links) are ready;
    // the paragraph splitter below skips blocks that already start with <table>.
    s = s.replace(/(?:^\|.+\|[ \t]*\n)(?:^\|[ \t:|\-]+\|[ \t]*\n)(?:^\|.*\|[ \t]*\n?)*/gm, block => {
      const rows = block.trim().split('\n').filter(r => r.trim());
      if(rows.length < 2) return block;
      const cells = r => r.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim());
      const th = cells(rows[0]).map(c => `<th>${c}</th>`).join('');
      const body = rows.slice(2).map(r => `<tr>${cells(r).map(c => `<td>${c}</td>`).join('')}</tr>`).join('');
      return `<table class="md-table"><thead><tr>${th}</tr></thead><tbody>${body}</tbody></table>\n`;
    });
    s = s.replace(/^(?:[-*•]\s+.+(?:\n|$))+/gm, m => {
      const items = m.trim().split(/\n/).map(line => {
        const content = line.replace(/^[-*•]\s+/, '');
        return `<li>${content}</li>`;
      }).join('');
      return `<ul class="md-ul">${items}</ul>`;
    });
    s = s.replace(/^(?:\d+\.\s+.+(?:\n|$))+/gm, m => {
      const items = m.trim().split(/\n/).map(line => {
        const content = line.replace(/^\d+\.\s+/, '');
        return `<li>${content}</li>`;
      }).join('');
      return `<ol class="md-ol">${items}</ol>`;
    });
    s = s.split(/\n\n+/).map(block => {
      if(/^<(h\d|ul|ol|pre|blockquote|table|div)/.test(block.trim())) return block;
      if(!block.trim()) return '';
      return `<p>${block.replace(/\n/g, '<br>')}</p>`;
    }).join('\n');
    return s;
  }

  /* Syntax-highlight an already-pretty-printed JSON string. Wraps keys, strings,
     numbers, booleans and null in coloured spans. Input is raw JSON text; it is
     HTML-escaped here so callers pass the plain string. */
  function _highlightJson(jsonStr){
    return _esc(jsonStr).replace(
      /(&quot;(?:\\.|[^\\&]|&(?!quot;))*&quot;)(\s*:)?|\b(true|false)\b|\b(null)\b|(-?\d+(?:\.\d+)?(?:[eE][+\-]?\d+)?)/g,
      (m, str, colon, bool, nul, num) => {
        if(str != null) return `<span class="${colon ? 'j-key' : 'j-str'}">${str}</span>${colon || ''}`;
        if(bool != null) return `<span class="j-bool">${bool}</span>`;
        if(nul != null)  return `<span class="j-null">${nul}</span>`;
        if(num != null)  return `<span class="j-num">${num}</span>`;
        return m;
      });
  }

  /* Smart-render a step / tool OUTPUT the way chat does: pretty-printed +
     highlighted JSON, markdown (incl. fenced code), or plain text — always the
     FULL content (no truncation). The caller places the result inside a
     capped-height, internally-scrollable container so long output stays in-card
     instead of overflowing. */
  function _smartRender(text){
    if(text == null) return '';
    const raw = String(text);
    const trimmed = raw.trim();
    // 1) A whole-body JSON object/array → pretty-print + highlight, BUT if the
    //    object is really just a wrapper around one big text/markdown field
    //    (the common {"text":"# …\n…"} shape), render THAT field as markdown and
    //    show the remaining scalar keys as pills — so prose/tables/code inside a
    //    JSON envelope read properly instead of as an escaped one-line blob.
    if((trimmed.startsWith('{') && trimmed.endsWith('}')) ||
       (trimmed.startsWith('[') && trimmed.endsWith(']'))){
      try{
        const parsed = JSON.parse(trimmed);
        if(parsed && typeof parsed === 'object' && !Array.isArray(parsed)){
          const TEXT_KEYS = ['text','output','content','answer','summary','result','stdout','markdown','body','message','report'];
          let bodyKey = null;
          for(const k of TEXT_KEYS){
            if(typeof parsed[k] === 'string' && parsed[k].length > 40){ bodyKey = k; break; }
          }
          if(bodyKey){
            const meta = {};
            for(const k of Object.keys(parsed)){
              if(k === bodyKey) continue;
              const v = parsed[k];
              if(v !== null && typeof v !== 'object') meta[k] = v;
            }
            const pills = Object.keys(meta).length
              ? `<div class="alo-arg-row" style="margin-bottom:4px">${_fmtArgs(meta, 600)}</div>` : '';
            return pills + `<div class="alo-md">${_renderMarkdown(String(parsed[bodyKey]))}</div>`;
          }
        }
        const pretty = JSON.stringify(parsed, null, 2);
        return `<pre class="alo-json">${_highlightJson(pretty)}</pre>`;
      }catch(_){ /* not valid JSON — fall through to markdown/plain */ }
    }
    // 2) Markdown signals (headings, lists, fenced code, bold, tables, links).
    if(/(^|\n)\s{0,3}(#{1,6}\s|[-*+]\s|\d+\.\s|>\s|```)/.test(raw)
       || /```/.test(raw) || /\*\*[^*\n]+\*\*/.test(raw)
       || /\[[^\]]+\]\([^)]+\)/.test(raw) || /(^|\n)\|.+\|/.test(raw)){
      return `<div class="alo-md">${_renderMarkdown(raw)}</div>`;
    }
    // 3) Plain text — preserve newlines, escape everything.
    return `<div class="alo-plain">${_esc(raw)}</div>`;
  }

  // ───────────────────────── Stylesheet (one shared <style>) ────────────────
  // Class names use an `.alo-` prefix to avoid conflicts with host pages that
  // also use `.al-*` classes (the original DAG workshop). All colours come
  // from CSS custom properties so the host theme drives the appearance.
  const STYLE = `
:host{display:block;width:100%;max-width:100%;box-sizing:border-box;min-width:0;color:var(--text,#ddd5c8);font-family:var(--mono,'IBM Plex Mono',monospace);font-size:11px}
:host([hidden]){display:none}
.alo-root{display:flex;flex-direction:column;gap:6px;min-height:0;width:100%}

/* Minimalist scrollbars */
:host *::-webkit-scrollbar{width:5px;height:5px}
:host *::-webkit-scrollbar-track{background:transparent}
:host *::-webkit-scrollbar-thumb{background:var(--border2,#4a4540);border-radius:3px}
:host *::-webkit-scrollbar-thumb:hover{background:var(--dim,#a89f92)}
:host *{scrollbar-width:thin;scrollbar-color:var(--border2,#4a4540) transparent}

/* Triage banner */
.alo-triage{background:var(--bg1,#1f1d1a);border:1px solid var(--border,#3a3530);border-left:3px solid var(--acc4,#a07ec1);border-radius:3px;padding:8px 10px;font-size:10.5px;display:none;flex-shrink:0}
.alo-triage.open{display:block}
.alo-triage-h{font-size:10px;color:var(--acc4,#a07ec1);text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px}
.alo-triage-row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:2px}
.alo-triage-row .lbl{color:var(--dim2,#8a7e70);font-size:9.5px;min-width:70px}
.alo-triage-kw{display:inline-block;background:var(--bg3,#2a2622);padding:1px 6px;border-radius:8px;font-family:var(--mono,monospace);font-size:9.5px;color:var(--text2,#bfb6a8);margin-right:3px}

/* Toolkit chips — nested as a section inside the Triage card (border-top
   separator, no own box) so they read as one combined Triage+Toolkit card. */
.alo-toolkit{display:none;margin-top:8px;padding-top:8px;border-top:1px solid var(--border2,#3a3530);font-size:10px}
.alo-toolkit.open{display:block}
.alo-toolkit-h{font-size:10px;color:var(--acc2,#a8c87a);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px;display:flex;align-items:center;justify-content:space-between}
.alo-toolkit-list{display:flex;flex-wrap:wrap;gap:3px;font-family:var(--mono,monospace);font-size:9.5px;color:var(--text2,#bfb6a8)}
.alo-tag-chip{font-size:9px;padding:1px 6px;border-radius:8px;background:var(--bg3,#2a2622);color:var(--text2,#bfb6a8);font-family:var(--mono,monospace)}

/* Cycles list */
.alo-cycles{flex:1;background:var(--bg1,#1f1d1a);border:1px solid var(--border,#3a3530);border-radius:3px;overflow-y:auto;padding:8px 70px 8px 8px;display:flex;flex-direction:column;gap:6px;font-family:var(--mono,monospace);font-size:10.5px;min-height:60px;position:relative}
.alo-cycle{padding:7px 10px;background:var(--bg2,#252220);border:1px solid var(--border,#3a3530);border-radius:3px;position:relative}
.alo-cycle.error{border-color:var(--err,#c75a5a);background:rgba(199,90,90,.05)}
.alo-cycle.done{border-color:var(--acc,#5a9e8f);background:rgba(90,158,143,.05)}
.alo-cycle.expand{border-color:var(--acc4,#a07ec1);background:rgba(160,126,193,.05)}
.alo-cycle.warn{border-color:#c9a45a;background:rgba(201,164,90,.06)}
.alo-cycle.handover{border-color:var(--acc,#5a9e8f);border-left-width:3px;background:rgba(90,158,143,.04)}
/* v5: a specialist sub-agent step — a labelled section the step's cycle cards
   appear under. Left accent bar distinguishes it from ordinary cycle cards. */
.alo-cycle.step{border-left:3px solid var(--acc2,#a8c87a);background:rgba(168,200,122,.05)}
.alo-cycle.step .alo-cycle-dot{border-color:var(--acc2,#a8c87a);background:var(--acc2,#a8c87a)}
.alo-step-meta{font-family:var(--mono,monospace);font-size:9px;color:var(--dim,#a89f92);margin-top:3px}
/* v5: exact-context reveal — the verbatim prompt + layered breakdown a step got */
.alo-ctx-reveal{margin-top:5px;border-top:1px dashed var(--border2,#3a3530);padding-top:4px}
.alo-ctx-reveal>summary{cursor:pointer;font-size:9px;color:var(--acc4,#a07ec1);user-select:none;list-style:none;display:flex;align-items:center;gap:5px}
.alo-ctx-reveal>summary::-webkit-details-marker{display:none}
.alo-ctx-reveal>summary::before{content:"▸";display:inline-block;transition:transform .15s;opacity:.7}
.alo-ctx-reveal[open]>summary::before{transform:rotate(90deg)}
.alo-ctx-body{margin-top:5px;display:flex;flex-direction:column;gap:5px}
.alo-ctx-layer{border:1px solid var(--border,#33312c);border-radius:4px;overflow:hidden;background:var(--bg0,#181614)}
.alo-ctx-layer.pulse{animation:aloCtxPulse 1.1s ease-in-out infinite;border-color:var(--acc,#5a9e8f)}
@keyframes aloCtxPulse{0%,100%{box-shadow:0 0 0 0 rgba(90,158,143,0)}50%{box-shadow:0 0 0 3px rgba(90,158,143,.28)}}
.alo-ctx-layer>.h{font-size:8.5px;text-transform:uppercase;letter-spacing:.4px;color:var(--acc,#5a9e8f);padding:3px 7px;background:var(--panel3,#211f1b);display:flex;justify-content:space-between;align-items:center;gap:6px}
.alo-ctx-layer>.h .src{color:var(--info,#7eb8d9);font-family:var(--mono,monospace);text-transform:none;letter-spacing:0}
.alo-ctx-pre{font-family:var(--mono,monospace);font-size:9px;line-height:1.45;color:var(--text2,#bfb6a8);background:var(--bg0,#181614);padding:6px 8px;margin:0;max-height:260px;overflow:auto;white-space:pre-wrap;word-break:break-word}
.alo-ctx-copy{cursor:pointer;font-size:8px;color:var(--dim,#a89f92);border:1px solid var(--border,#33312c);border-radius:3px;padding:1px 5px;background:transparent}
.alo-ctx-copy:hover{color:var(--text,#ddd5c8);border-color:var(--acc,#5a9e8f)}
/* v5: joined thought-only "reasoning" card — muted, not an error/result. */
.alo-cycle.thinking{border-style:dashed;border-color:var(--border2,#3a3530);background:transparent}
.alo-cycle.thinking .alo-cycle-dot{border-color:var(--dim2,#8a7e70)}
.alo-think-join{margin-top:4px;font-size:10px;line-height:1.45;color:var(--text2,#bfb6a8);white-space:pre-wrap;font-style:italic}
/* Timeline: a dot + timestamp per card on a rail down the right side. The rail
   is composed of per-card connector segments (::after) rather than one long
   container line, so it spans the full content height even when the scroll
   container grows. Each segment runs from this card's dot to the next card's
   dot (card height + the 6px flex gap); the last card omits it. */
.alo-cycle::after{content:'';position:absolute;top:13px;right:-59px;width:2px;height:calc(100% + 6px);background:var(--border2,#3a3530);z-index:0}
.alo-cycle:last-child::after{display:none}
.alo-cycle-dot{position:absolute;top:13px;right:-62px;width:8px;height:8px;border-radius:50%;background:var(--bg1,#1f1d1a);border:2px solid var(--acc2,#a8c87a);box-sizing:border-box;z-index:1}
.alo-cycle-time{position:absolute;top:12px;right:-50px;width:48px;text-align:right;font-size:8px;color:var(--dim2,#8a7e70);font-family:var(--mono,monospace);letter-spacing:.3px}
.alo-cycle.error .alo-cycle-dot{border-color:var(--err,#c75a5a)}
.alo-cycle.done .alo-cycle-dot{border-color:var(--acc,#5a9e8f)}
.alo-cycle.expand .alo-cycle-dot{border-color:var(--acc4,#a07ec1)}
.alo-cycle.warn .alo-cycle-dot{border-color:#c9a45a}
.alo-cycle.handover .alo-cycle-dot{border-color:var(--acc,#5a9e8f);background:var(--acc,#5a9e8f)}

/* ── Expand / compact card controls ──────────────────────────────────── */
.alo-card-expand{margin-left:auto;flex:0 0 auto;background:var(--bg2,#252220);border:1px solid var(--border,#3a3530);color:var(--dim2,#8a7e70);border-radius:3px;cursor:pointer;font-size:12px;line-height:1;padding:2px 6px}
.alo-card-expand:hover{color:var(--text,#ddd5c8);border-color:var(--border2,#4a4540)}
.alo-card-expand.expanded{color:var(--acc2,#a8c87a);border-color:var(--acc2,#a8c87a)}
/* ── Per-step context meter + GPU-spill badge ────────────────────────── */
.alo-ctxbar{display:flex;align-items:center;gap:6px;padding:3px 6px;border-top:1px solid var(--border,#3a3530);font-size:9px;color:var(--dim2,#8a7e70)}
.alo-ctx-lbl{flex:0 0 auto;text-transform:uppercase;letter-spacing:.05em}
.alo-ctx-track{flex:0 0 70px;height:5px;background:var(--border,#3a3530);border-radius:3px;overflow:hidden}
.alo-ctx-fill{display:block;height:100%;width:0%;background:var(--acc2,#a8c87a);border-radius:3px;transition:width .4s,background .3s}
.alo-ctx-txt{flex:0 0 auto;font-variant-numeric:tabular-nums}
.alo-ctx-spill{flex:0 0 auto;margin-left:auto;color:var(--err,#c96a5a);font-weight:600}
/* ── Files-produced card: list + inline preview/source ───────────────── */
.alo-file-list{display:flex;flex-direction:column;gap:2px;margin-top:4px}
.alo-file-row{display:flex;align-items:center;gap:8px;font-size:10px;padding:2px 4px;border-radius:3px}
.alo-file-row:hover{background:var(--bg2,#252220)}
.alo-file-name{flex:1 1 auto;color:var(--acc2,#a8c87a);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.alo-file-size{flex:0 0 auto;color:var(--dim2,#8a7e70);font-variant-numeric:tabular-nums}
.alo-file-btn{flex:0 0 auto;background:var(--bg2,#252220);border:1px solid var(--border,#3a3530);color:var(--dim2,#8a7e70);border-radius:3px;cursor:pointer;font-size:9px;padding:1px 5px;text-decoration:none}
.alo-file-btn:hover{color:var(--text,#ddd5c8);border-color:var(--border2,#4a4540)}
.alo-file-view{margin-top:6px;border-top:1px solid var(--border,#3a3530);padding-top:6px}
.alo-file-vh{font-size:9.5px;color:var(--dim2,#8a7e70);margin-bottom:4px}
.alo-file-src{margin:0;max-height:340px;overflow:auto;background:var(--bg2,#252220);border:1px solid var(--border,#3a3530);border-radius:4px;padding:6px;font-size:10px;line-height:1.45;white-space:pre-wrap;word-break:break-word}
.alo-file-more{font-size:9px;color:var(--dim2,#8a7e70);margin-top:3px}
/* Timeline card: header bar (outside the scroll region) above the cycles list */
.alo-cycles-card{display:flex;flex-direction:column;min-height:0;background:var(--bg1,#1f1d1a);border:1px solid var(--border,#3a3530);border-radius:3px;overflow:hidden}
.alo-cycles-h{display:flex;align-items:center;gap:8px;padding:6px 9px;border-bottom:1px solid var(--border,#3a3530);background:var(--bg1,#1f1d1a);flex:0 0 auto}
.alo-cycles-title{font-size:10px;color:var(--acc2,#a8c87a);text-transform:uppercase;letter-spacing:.5px;font-weight:600}
.alo-cycles-count{font-size:9.5px;color:var(--dim,#a89f92)}
.alo-cycles-card > .alo-cycles{flex:0 1 auto;background:transparent;border:none;border-radius:0}
.alo-cycles-card.compact > .alo-cycles{max-height:var(--alo-cycles-maxh,440px);overflow-y:auto}
.alo-cycles-card:not(.compact) > .alo-cycles{max-height:none;overflow:visible}
/* Run-complete pane: header is a sibling above the scrollable body */
.alo-final-pane.compact [data-part="final-body"]{max-height:460px;overflow-y:auto}
.alo-final-h{flex:0 0 auto}

.alo-cycle-h{display:flex;align-items:center;gap:8px;margin-bottom:3px}
.alo-cycle-n{color:var(--dim2,#8a7e70);font-size:10px}
.alo-cycle-tool{color:var(--acc2,#a8c87a);font-weight:500}
.alo-cycle-status{margin-left:auto;font-size:9.5px;color:var(--dim,#a89f92)}
.alo-cycle-thought{font-size:10px;color:var(--text2,#bfb6a8);margin-bottom:3px;font-style:italic}
.alo-cycle-args{font-size:9.5px;color:var(--dim,#a89f92);background:var(--bg0,#181614);padding:3px 6px;border-radius:3px;margin:3px 0;font-family:var(--mono,monospace);overflow-wrap:anywhere;line-height:1.6;display:flex;flex-wrap:wrap;gap:3px 5px;align-items:center}
.alo-arg-pill{display:inline-flex;align-items:center;background:var(--bg2,#252220);border:1px solid var(--border,#3a3530);border-radius:4px;overflow:hidden;font-size:9px;line-height:1.3}
.alo-arg-key{padding:1px 4px;background:var(--bg3,#2a2622);color:var(--acc2,#a8c87a);font-weight:500;border-right:1px solid var(--border,#3a3530)}
.alo-arg-val{padding:1px 5px;color:var(--text2,#bfb6a8);max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.alo-arg-ellip{color:var(--dim2,#8a7e70);font-size:9px}
/* Truncated arg pills expand on click to reveal the full value (wraps, full width). */
.alo-arg-pill.trunc{cursor:pointer}
.alo-arg-pill.trunc:hover{border-color:var(--acc,#5a9e8f)}
.alo-arg-pill.expanded{flex-basis:100%;max-width:100%;align-items:flex-start}
.alo-arg-pill.expanded .alo-arg-val{max-width:none;white-space:pre-wrap;word-break:break-word;overflow:visible;text-overflow:clip}
.alo-cycle-preview{font-size:9.5px;color:var(--dim,#a89f92);background:var(--bg0,#181614);padding:5px 7px;border-radius:3px;max-height:80px;overflow-y:auto;white-space:pre-wrap;line-height:1.4}
/* Step OUTPUT summary: shows the FULL smart-rendered output, capped in height
   with its own scroll so long output stays inside the card instead of spilling
   out. Overrides the tiny 80px preview cap it inherits. */
.alo-step-summary{max-height:var(--alo-output-maxh,320px);white-space:normal;color:var(--text2,#bfb6a8)}
.alo-step-summary .alo-plain{white-space:pre-wrap;word-break:break-word}
/* Pretty-printed + highlighted JSON output block. */
.alo-json{margin:2px 0;padding:6px 8px;background:var(--bg2,#252220);border-radius:3px;font-family:var(--mono,monospace);font-size:9.5px;line-height:1.5;white-space:pre;overflow-x:auto;color:var(--text2,#bfb6a8)}
.alo-json .j-key{color:var(--acc4,#a07ec1)}
.alo-json .j-str{color:var(--acc2,#a8c87a)}
.alo-json .j-num{color:var(--acc,#5a9e8f)}
.alo-json .j-bool{color:#c79a5a}
.alo-json .j-null{color:var(--dim,#a89f92);font-style:italic}
/* Markdown-rendered output block (mirrors the handover body's styling). */
.alo-md{font-family:system-ui,-apple-system,Segoe UI,sans-serif;font-size:10.5px;line-height:1.5;color:var(--text2,#bfb6a8);word-break:break-word}
.alo-md h1,.alo-md .md-h1{font-size:13px;font-weight:600;color:var(--acc,#5a9e8f);margin:6px 0 3px}
.alo-md h2,.alo-md .md-h2{font-size:12px;font-weight:600;color:var(--acc2,#a8c87a);margin:6px 0 3px}
.alo-md h3,.alo-md .md-h3,.alo-md h4,.alo-md .md-h4{font-size:11px;font-weight:600;color:var(--text,#ddd5c8);margin:5px 0 2px}
.alo-md p{margin:3px 0}
.alo-md ul,.alo-md ol,.alo-md .md-ul,.alo-md .md-ol{margin:3px 0 3px 16px;padding:0}
.alo-md li{margin:1px 0}
.alo-md code,.alo-md .md-inline{background:var(--bg2,#252220);padding:0 3px;border-radius:2px;font-family:var(--mono,monospace);font-size:9.5px}
.alo-md pre,.alo-md .md-code{background:var(--bg2,#252220);padding:6px 8px;border-radius:3px;font-family:var(--mono,monospace);font-size:9.5px;color:var(--text2,#bfb6a8);margin:4px 0;overflow-x:auto;white-space:pre;line-height:1.4}
.alo-md a,.alo-md .md-link{color:var(--acc4,#a07ec1)}
.alo-cycle-result{margin-top:6px;border:1px solid var(--border,#3a3530);border-radius:3px;background:var(--bg0,#181614);overflow:hidden}
.alo-result-h{font-size:9px;text-transform:uppercase;letter-spacing:.4px;padding:3px 6px;font-weight:500}
.alo-result-h.ok{background:rgba(90,158,143,.13);color:var(--ok,var(--acc,#5a9e8f))}
.alo-result-h.err{background:rgba(199,90,90,.13);color:var(--err,#c75a5a)}
.alo-result-h.empty{background:rgba(201,164,90,.13);color:#c9a45a}
.alo-result-body{margin:0;padding:6px 8px;font-family:var(--mono,monospace);font-size:10px;color:var(--text2,#bfb6a8);max-height:200px;overflow:auto;white-space:pre-wrap;line-height:1.6;word-break:break-word;display:flex;flex-wrap:wrap;gap:3px 5px;align-items:flex-start}
.alo-result-body.err{color:#ff9999}
.alo-result-body.empty{color:#c9a45a}
/* Smart-rendered tool OUTPUT: full content (no truncation), height-capped with
   its own scroll, and user-draggable taller via the native resize handle. */
.alo-result-render{margin:0;padding:6px 8px;font-size:10px;color:var(--text2,#bfb6a8);max-height:300px;min-height:22px;overflow:auto;resize:vertical;line-height:1.5;word-break:break-word}
.alo-result-render.err{color:#ff9999;white-space:pre-wrap;font-family:var(--mono,monospace)}
.alo-result-render.empty{color:#c9a45a;white-space:pre-wrap}
.alo-result-render .alo-plain{white-space:pre-wrap;word-break:break-word}
/* Markdown tables (GitHub-style) inside rendered output. */
.alo-md .md-table{border-collapse:collapse;margin:5px 0;font-size:9.5px;max-width:100%;display:block;overflow-x:auto}
.alo-md .md-table th,.alo-md .md-table td{border:1px solid var(--border,#3a3530);padding:2px 6px;text-align:left;vertical-align:top}
.alo-md .md-table th{background:var(--bg2,#252220);color:var(--text,#ddd5c8);font-weight:600;white-space:nowrap}

/* Cycle thinking block */
.alo-cycle-think{margin-top:3px;font-size:9px;color:var(--dim2,#8a7e70)}
.alo-cycle-think summary{cursor:pointer;color:var(--acc4,#a07ec1);font-size:9px;user-select:none}
.alo-cycle-think pre{margin:3px 0 0;padding:4px 6px;background:var(--bg0,#181614);border:1px solid var(--border2,#4a4540);border-radius:3px;font-size:8.5px;white-space:pre-wrap;word-break:break-word;max-height:160px;overflow-y:auto;color:var(--text2,#bfb6a8);font-family:var(--mono,monospace)}

/* Args coerce note */
.alo-coerce{display:flex;align-items:center;gap:6px;margin-top:5px;padding:4px 6px;background:rgba(174,222,126,.06);border-left:2px solid #aede7e;border-radius:2px;font-family:var(--mono,monospace)}

/* Version pill */
.alo-version-pill{display:inline-flex;align-items:center;gap:4px;padding:2px 6px;border-radius:8px;font-size:9.5px;font-family:var(--mono,monospace)}
.alo-version-pill.v1{background:rgba(122,130,144,.15);color:var(--dim,#a89f92)}
.alo-version-pill.v2{background:rgba(160,126,193,.18);color:var(--acc4,#a07ec1)}
.alo-version-pill.v3{background:rgba(143,184,122,.18);color:var(--acc2,#a8c87a)}

/* Progress strip (live tool progress) */
.alo-progress{margin-top:5px;padding:5px 8px;background:var(--bg0,#181614);border:1px dashed var(--border2,#4a4540);border-radius:3px;display:flex;flex-direction:column;gap:3px;max-height:520px;overflow-y:auto}
.alo-progress-h{font-size:9px;color:var(--dim2,#8a7e70);text-transform:uppercase;letter-spacing:.5px;display:flex;align-items:center;gap:6px}
.alo-progress-h .alo-spinner{width:9px;height:9px;border-radius:50%;border:1.5px solid var(--acc4,#a07ec1);border-top-color:transparent;animation:alo-spin 1s linear infinite}
.alo-progress-tag{display:inline-block;padding:1px 5px;font-size:8.5px;border-radius:2px;background:#1d2c3a;color:#7eb8d9;text-transform:uppercase;letter-spacing:.4px;margin-right:6px;font-weight:500}
.alo-progress-row{display:flex;align-items:center;gap:6px;font-size:10px;color:var(--text2,#bfb6a8);margin:2px 0;padding:2px 0;line-height:1.4}
.alo-progress-row code{background:var(--bg0,#181614);padding:0 4px;border-radius:2px;font-size:9.5px;color:var(--acc2,#a8c87a)}
.alo-progress-line{font-family:var(--mono,monospace);font-size:9.5px;color:var(--text2,#bfb6a8);padding:1px 0;line-height:1.4;display:flex;align-items:flex-start;gap:6px}
.alo-progress-line .pkind{flex:0 0 auto;color:var(--dim2,#8a7e70);background:var(--bg2,#252220);padding:0 5px;border-radius:6px;font-size:8.5px}
.alo-progress-line .pbody{flex:1;overflow-wrap:anywhere}
.alo-progress-line.token .pbody{color:var(--text,#ddd5c8)}
.alo-progress-line.research .pkind{color:var(--info,#7eb8d9);background:rgba(90,142,184,.12)}
.alo-progress-line.exec .pkind{color:var(--acc2,#a8c87a);background:rgba(143,184,122,.12)}
.alo-progress-line.train .pkind{color:var(--acc3,#c5a572);background:rgba(197,165,114,.12)}
.alo-progress-tokens{font-family:var(--mono,monospace);font-size:9.5px;color:var(--text,#ddd5c8);background:var(--bg2,#252220);padding:4px 6px;border-radius:3px;white-space:pre-wrap;line-height:1.4;max-height:280px;min-height:60px;overflow-y:auto;word-break:break-word;flex-shrink:0}
.alo-research-thinking{margin-top:4px;font-size:8.5px;color:var(--dim2,#8a7e70);font-style:italic;max-height:120px;overflow-y:auto;white-space:pre-wrap;border-left:2px solid var(--acc4,#a07ec1);padding-left:5px}

/* HITL pause card */
.alo-hitl-pause{margin:6px 0;padding:9px 11px;background:rgba(217,119,87,.08);border:1.5px solid var(--warn,#c9a45a);border-radius:3px;display:flex;flex-direction:column;gap:7px}
.alo-hitl-pause-h{display:flex;align-items:center;gap:8px;font-size:11px;color:var(--warn,#c9a45a);font-weight:600}
.alo-hitl-pause-h .pulse{width:7px;height:7px;border-radius:50%;background:var(--warn,#c9a45a);animation:alo-pulse 1.4s ease-in-out infinite}
/* Rolling thinking phrase (planning / long waits) */
@keyframes aloPhraseIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}
.alo-phrase{font-style:italic;display:inline-block;animation:aloPhraseIn .5s ease}
.alo-phrase.swap{animation:aloPhraseIn .5s ease}
.alo-hitl-pause-thought{font-size:10.5px;color:var(--text2,#bfb6a8);font-style:italic;padding:4px 6px;background:var(--bg2,#252220);border-radius:3px;border-left:2px solid var(--warn,#c9a45a)}
.alo-hitl-pause-tool{font-family:var(--mono,monospace);font-size:11px;color:var(--acc2,#a8c87a)}
.alo-hitl-pause-args{font-family:var(--mono,monospace);font-size:10px;color:var(--text,#ddd5c8);background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);border-radius:3px;padding:6px 8px;min-height:60px;width:100%;resize:vertical;white-space:pre-wrap;box-sizing:border-box}
.alo-hitl-pause-actions{display:flex;gap:5px;flex-wrap:wrap}
.alo-hitl-pause-meta{font-size:9.5px;color:var(--dim,#a89f92);font-family:var(--mono,monospace)}
.alo-hitl-pause-meta .countdown{color:var(--warn,#c9a45a);font-weight:600}
.alo-hitl-btn{padding:3px 9px;font-size:10.5px;border:1px solid var(--border,#3a3530);background:var(--bg2,#252220);color:var(--text,#ddd5c8);border-radius:3px;cursor:pointer;font-family:var(--mono,monospace)}
.alo-hitl-btn:hover{border-color:var(--acc,#5a9e8f)}
.alo-hitl-btn.primary{background:var(--acc,#5a9e8f);color:#fff;border-color:var(--acc,#5a9e8f)}
.alo-hitl-btn.warn{background:var(--warn,#c9a45a);color:#fff;border-color:var(--warn,#c9a45a)}
.alo-hitl-btn.danger{background:var(--err,#c75a5a);color:#fff;border-color:var(--err,#c75a5a)}
.alo-hitl-btn:disabled{opacity:.55;cursor:not-allowed}

/* Handover synthesis output */
.alo-handover-stream{margin-top:6px;padding:6px;background:var(--bg0,#181614);border-radius:3px;color:var(--dim,#a89f92);font-style:italic;font-size:10px;font-family:system-ui,-apple-system,Segoe UI,sans-serif;min-height:24px}
.alo-handover-stream::before{content:"⋯ generating answer ⋯";opacity:.6}
.alo-handover-body{margin-top:6px;padding:8px 10px;background:var(--bg0,#181614);border-radius:3px;color:var(--text,#ddd5c8);font-family:system-ui,-apple-system,Segoe UI,sans-serif;font-size:11px;line-height:1.55;max-height:380px;overflow-y:auto}
.alo-handover-body p{margin:6px 0}
.alo-handover-body .md-h1{font-size:14px;font-weight:600;color:var(--acc,#5a9e8f);margin:8px 0 4px;border-bottom:1px solid var(--border,#3a3530);padding-bottom:3px}
.alo-handover-body .md-h2{font-size:12.5px;font-weight:600;color:var(--acc2,#a8c87a);margin:8px 0 3px}
.alo-handover-body .md-h3{font-size:11.5px;font-weight:600;color:var(--text,#ddd5c8);margin:6px 0 2px}
.alo-handover-body .md-h4{font-size:11px;font-weight:500;color:var(--text2,#bfb6a8);margin:5px 0 2px}
.alo-handover-body strong{color:var(--text,#ddd5c8);font-weight:600}
.alo-handover-body em{color:var(--text2,#bfb6a8);font-style:italic}
.alo-handover-body .md-inline{background:var(--bg2,#252220);padding:0 4px;border-radius:2px;font-family:var(--mono,monospace);font-size:10px;color:var(--acc2,#a8c87a)}
.alo-handover-body .md-code{background:var(--bg2,#252220);padding:6px 8px;border-radius:3px;font-family:var(--mono,monospace);font-size:10px;color:var(--text2,#bfb6a8);margin:5px 0;overflow-x:auto;white-space:pre;line-height:1.4}
.alo-handover-body .md-ul,.alo-handover-body .md-ol{margin:4px 0 4px 18px;padding:0}
.alo-handover-body .md-ul li,.alo-handover-body .md-ol li{margin:2px 0}
.alo-handover-body .md-link{color:var(--acc,#5a9e8f);text-decoration:underline}

/* Research report card — collapsible, mirrors .alo-handover-body's markdown styling */
.alo-research-report{margin-top:6px;border:1px solid var(--border2,#3a3530);border-radius:3px;background:var(--bg0,#181614);overflow:hidden}
.alo-research-report > summary{cursor:pointer;list-style:none;padding:5px 8px;font-size:9.5px;color:var(--info,#7eb8d9);text-transform:uppercase;letter-spacing:.4px;display:flex;align-items:center;gap:6px;user-select:none;background:#1a2d3a}
.alo-research-report > summary::-webkit-details-marker{display:none}
.alo-research-report > summary::before{content:"▸";display:inline-block;transition:transform .15s}
.alo-research-report[open] > summary::before{transform:rotate(90deg)}
.alo-research-report-body{padding:8px 10px;color:var(--text,#ddd5c8);font-family:system-ui,-apple-system,Segoe UI,sans-serif;font-size:11px;line-height:1.55;max-height:420px;overflow-y:auto}
.alo-research-report-body p{margin:6px 0}
.alo-research-report-body .md-h1{font-size:14px;font-weight:600;color:var(--acc,#5a9e8f);margin:8px 0 4px;border-bottom:1px solid var(--border,#3a3530);padding-bottom:3px}
.alo-research-report-body .md-h2{font-size:12.5px;font-weight:600;color:var(--acc2,#a8c87a);margin:8px 0 3px}
.alo-research-report-body .md-h3{font-size:11.5px;font-weight:600;color:var(--text,#ddd5c8);margin:6px 0 2px}
.alo-research-report-body .md-h4{font-size:11px;font-weight:500;color:var(--text2,#bfb6a8);margin:5px 0 2px}
.alo-research-report-body strong{color:var(--text,#ddd5c8);font-weight:600}
.alo-research-report-body em{color:var(--text2,#bfb6a8);font-style:italic}
.alo-research-report-body .md-inline{background:var(--bg2,#252220);padding:0 4px;border-radius:2px;font-family:var(--mono,monospace);font-size:10px;color:var(--acc2,#a8c87a)}
.alo-research-report-body .md-code{background:var(--bg2,#252220);padding:6px 8px;border-radius:3px;font-family:var(--mono,monospace);font-size:10px;color:var(--text2,#bfb6a8);margin:5px 0;overflow-x:auto;white-space:pre;line-height:1.4}
.alo-research-report-body .md-ul,.alo-research-report-body .md-ol{margin:4px 0 4px 18px;padding:0}
.alo-research-report-body .md-ul li,.alo-research-report-body .md-ol li{margin:2px 0}
.alo-research-report-body .md-link{color:var(--acc,#5a9e8f);text-decoration:underline}
.alo-research-report-cites{margin-top:6px;padding-top:6px;border-top:1px solid var(--border2,#3a3530);font-size:9px;color:var(--dim2,#8a7e70)}
.alo-research-report-cites ol{margin:3px 0 0 16px;padding:0}
.alo-research-report-cites li{margin:2px 0;word-break:break-all}
.alo-research-report-cites a{color:var(--acc2,#a8c87a)}

/* Final pane */
.alo-final-pane{display:none;flex-direction:column;gap:8px;background:var(--bg1,#1f1d1a);border:1px solid var(--acc,#5a9e8f);border-left:3px solid var(--acc,#5a9e8f);border-radius:3px;padding:10px;margin-top:6px}
.alo-final-pane.show{display:flex}
.alo-final-h{display:flex;align-items:center;gap:8px;justify-content:space-between;flex-wrap:wrap}
.alo-final-title{font-size:11px;font-weight:600;color:var(--acc,#5a9e8f);text-transform:uppercase;letter-spacing:.5px}
.alo-final-actions{display:flex;gap:5px;flex-wrap:wrap}
.alo-final-row{display:grid;grid-template-columns:90px minmax(0,1fr);gap:8px;align-items:flex-start;font-size:10.5px;padding:3px 0;border-top:1px solid var(--border,#3a3530)}
.alo-final-row:first-of-type{border-top:none}
.alo-final-lbl{color:var(--dim2,#8a7e70);text-transform:uppercase;letter-spacing:.4px;font-size:9.5px;padding-top:2px}
.alo-final-val{min-width:0;color:var(--text2,#bfb6a8);font-family:var(--mono,monospace);font-size:10.5px;overflow-wrap:anywhere;line-height:1.5}
.alo-final-val.summary{color:var(--text,#ddd5c8);font-family:system-ui,-apple-system,Segoe UI,sans-serif;line-height:1.55;font-size:11.5px;white-space:pre-wrap}
.alo-final-cat{display:inline-block;background:rgba(160,126,193,.18);color:var(--acc4,#a07ec1);padding:2px 8px;border-radius:8px;font-family:var(--mono,monospace);font-size:10px;margin-right:5px}
.alo-final-tools{display:flex;flex-wrap:wrap;gap:3px;margin-top:3px}
.alo-final-tool{font-family:var(--mono,monospace);font-size:9.5px;background:var(--bg2,#252220);color:var(--text2,#bfb6a8);padding:1px 7px;border-radius:8px}
.alo-final-tool.ok{background:rgba(90,158,143,.12);color:var(--acc,#5a9e8f)}
.alo-final-tool.err{background:rgba(199,90,90,.12);color:var(--err,#c75a5a);text-decoration:line-through}
.alo-final-step{display:flex;flex-direction:column;gap:2px;padding:5px 7px;background:var(--bg2,#252220);border-left:2px solid var(--border2,#4a4540);border-radius:0 3px 3px 0;margin-bottom:3px;font-family:var(--mono,monospace);font-size:10px;min-width:0}
.alo-final-step.ok{border-left-color:var(--acc,#5a9e8f)}
.alo-final-step.err{border-left-color:var(--err,#c75a5a);opacity:.7}
.alo-final-step-h{display:flex;align-items:center;gap:6px;min-width:0}
.alo-final-step-tool{color:var(--acc2,#a8c87a);font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.alo-final-step-ms{margin-left:auto;font-size:9px;color:var(--dim2,#8a7e70);flex:0 0 auto}
.alo-final-step-args{font-size:9.5px;color:var(--dim,#a89f92);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%}
.alo-final-raw{margin-top:6px;border-top:1px dashed var(--border,#3a3530);padding-top:6px}
.alo-final-raw summary{font-size:10px;color:var(--dim,#a89f92);cursor:pointer;user-select:none}
.alo-final-raw summary:hover{color:var(--text2,#bfb6a8)}
.alo-final-raw pre{margin:5px 0 0;font-size:9.5px;max-height:240px;overflow:auto;background:var(--bg0,#181614);padding:6px;border-radius:3px;white-space:pre-wrap;word-break:break-word}

.alo-empty{color:var(--dim,#a89f92);font-style:italic;font-size:10px;padding:8px;text-align:center}
.alo-cur{display:inline-block;width:6px;height:10px;background:var(--acc2,#a8c87a);animation:alo-blink .85s step-end infinite;vertical-align:text-bottom}

/* Compact variant — slimmer paddings for chat-message embedding */
:host([compact]) .alo-cycle{padding:5px 8px;font-size:10px}
:host([compact]) .alo-cycles{padding:5px 24px 5px 5px;gap:4px}
:host([compact]) .alo-cycle-dot{right:-18px;top:9px;width:6px;height:6px}
:host([compact]) .alo-cycle::after{top:9px;right:-16px;height:calc(100% + 4px)}
:host([compact]) .alo-cycle-time{display:none}
:host([compact]) .alo-final-pane{padding:7px}
/* Triage + toolkit: noticeably slimmer for chat embedding — they're a
   header banner, not the focus, so trim padding, row gaps and the toolkit
   separator that made the box too tall. */
:host([compact]) .alo-triage{padding:4px 8px;font-size:10px}
:host([compact]) .alo-triage-h{margin-bottom:1px}
:host([compact]) .alo-triage-row{margin-bottom:0;gap:6px}
:host([compact]) .alo-toolkit{margin-top:4px;padding-top:4px}
:host([compact]) .alo-toolkit-h{margin-bottom:2px}

@keyframes alo-spin{to{transform:rotate(360deg)}}
@keyframes alo-pulse{0%,100%{opacity:1}50%{opacity:.55}}
@keyframes alo-blink{0%,100%{opacity:1}50%{opacity:0}}
`;

  // ──────────────────────────── The Element ─────────────────────────────────
  class VeraAgentLoopOutput extends HTMLElement {
    static get observedAttributes(){
      return ['compact','show-final','show-toolkit','show-triage','show-thinking','max-height'];
    }
    constructor(){
      super();
      this._sr = this.attachShadow({mode:'open'});
      this._cycleRefs = new Map();        // cycle index → {el, progressEl, tokenBuffer, _inThinkBlock, _thinkBuffer}
      this._hitlPending = new Set();
      this._lastResult = null;
      this._sessionId = '';
      this._hitlEndpoint = '/workshop/agent_loop/hitl/respond';
      this._apiBase = '';
      this._abort = null;
      this._maxResultPreview = 800;
      this._showThinking = true;
      this._activeWsJobs = new Set();     // job_ids with an active WS — SSE tokens for these are suppressed
      this._startCardEl = null;           // the single "Starting" card — re-used if `start` fires twice
    }

    connectedCallback(){
      // (Re)start the phrase rotator every time we (re)enter the DOM — a
      // plain appendChild move disconnects/reconnects the element, and the
      // timer is cleared on disconnect.
      this._startPhraseTimer();
      if(this._mounted) return;
      this._mounted = true;
      this._render();
      // Delegated handler for the ⤢ expand/compact toggles on the Timeline,
      // Triage and Run-complete cards (shadow DOM — inline onclick can't reach
      // component methods, so delegate from the shadow root).
      this._sr.addEventListener('click', (e) => {
        // Files card: preview / source / refresh. Checked BEFORE the generic
        // expand handler because the refresh control reuses .alo-card-expand.
        const fbtn = e.target.closest('.alo-file-btn, [data-act="files-refresh"]');
        if(fbtn){
          const act = fbtn.getAttribute('data-act');
          if(act === 'files-refresh'){ this._renderFilesCard().catch(()=>{}); return; }
          const row = fbtn.closest('.alo-file-row');
          if(row && act){ this._showFile(row.getAttribute('data-file'), act).catch(()=>{}); }
          return;
        }
        const btn = e.target.closest('.alo-card-expand');
        if(btn){ this._toggleCardExpand(btn); return; }
        // Expand/collapse a truncated argument pill to reveal its full value.
        const pill = e.target.closest('.alo-arg-pill.trunc');
        if(pill){
          const val = pill.querySelector('.alo-arg-val');
          if(!val) return;
          const open = pill.classList.toggle('expanded');
          val.textContent = open ? (pill.getAttribute('data-full') || val.textContent)
                                  : (pill.getAttribute('data-short') || val.textContent);
        }
      });
      // Pull initial attribute values
      if(this.hasAttribute('show-thinking')){
        this._showThinking = this.getAttribute('show-thinking') !== 'false';
      }
      this._applyMaxHeight();
    }

    disconnectedCallback(){
      if(this._phraseTimer){ clearInterval(this._phraseTimer); this._phraseTimer = null; }
    }

    // Rolling thinking phrases — any .alo-phrase inside this instance fades
    // through the shared phrase list (same localStorage list the chat's
    // throbber uses, so a themed set applies everywhere) while planning /
    // long waits are on screen.
    _startPhraseTimer(){
      if(this._phraseTimer) return;
      this._phraseTimer = setInterval(() => {
        const els = this._sr.querySelectorAll('.alo-phrase');
        if(!els.length) return;
        const pool = ALO_THINK_PHRASES();
        els.forEach(el => {
          const next = pool[Math.floor(Math.random()*pool.length)];
          if(!next || next === el.textContent) return;
          el.classList.remove('swap'); void el.offsetWidth;
          el.textContent = next; el.classList.add('swap');
        });
      }, 2600);
    }

    // Toggle a single card between compact (fixed-height scroll) and full
    // (expand to show all). `btn` carries data-card = cycles|final.
    // (Triage+Toolkit has no toggle — it stays fully expanded.)
    _toggleCardExpand(btn){
      const which = btn.dataset.card;
      const sel = which === 'cycles' ? '[data-part="cycles-card"]'
                : '.alo-final-pane';
      const card = this._sr.querySelector(sel);
      if(!card) return;
      const nowCompact = card.classList.toggle('compact');
      btn.classList.toggle('expanded', !nowCompact);
      btn.title = nowCompact ? 'Expand to full height' : 'Collapse to compact scroll';
    }

    attributeChangedCallback(name, _oldVal, newVal){
      if(!this._mounted) return;
      if(name === 'show-final'){
        const pane = this._sr.querySelector('.alo-final-pane');
        if(pane && newVal === 'false') pane.style.display = 'none';
      } else if(name === 'show-toolkit'){
        const tk = this._sr.querySelector('.alo-toolkit');
        if(tk && newVal === 'false') tk.style.display = 'none';
      } else if(name === 'show-triage'){
        const tr = this._sr.querySelector('.alo-triage');
        if(tr && newVal === 'false') tr.style.display = 'none';
      } else if(name === 'show-thinking'){
        this._showThinking = newVal !== 'false';
      } else if(name === 'max-height'){
        this._applyMaxHeight();
      }
    }

    _applyMaxHeight(){
      // Drives the compact-mode cap via a custom property so the .compact CSS
      // rule and the expand toggle stay in charge of when it applies.
      const mh = this.getAttribute('max-height');
      if(mh) this.style.setProperty('--alo-cycles-maxh', (parseInt(mh,10)||440) + 'px');
    }

    _render(){
      const showTriage  = this.getAttribute('show-triage')  !== 'false';
      const showToolkit = this.getAttribute('show-toolkit') !== 'false';
      const showFinal   = this.getAttribute('show-final')   !== 'false';
      this._sr.innerHTML = `
        <style>${STYLE}</style>
        <div class="alo-root">
          <!-- Combined Triage + Toolkit card (always fully expanded, no toggle) -->
          <div class="alo-triage" data-part="triage" ${showTriage?'':'hidden'}>
            <div class="alo-triage-h">Triage</div>
            <div class="alo-triage-row"><span class="lbl">Category:</span><span data-part="tri-cat" style="font-family:var(--mono,monospace);color:var(--acc4,#a07ec1)">—</span></div>
            <div class="alo-triage-row"><span class="lbl">Keywords:</span><span data-part="tri-kws"></span></div>
            <div class="alo-triage-row" style="align-items:flex-start"><span class="lbl">Reasoning:</span><span data-part="tri-reason" style="flex:1;color:var(--text2,#bfb6a8);font-size:10.5px;font-style:italic">—</span></div>
            <div class="alo-toolkit" data-part="toolkit" ${showToolkit?'':'hidden'}>
              <div class="alo-toolkit-h">
                <span>Visible toolkit</span>
                <span data-part="toolkit-count" style="color:var(--dim,#a89f92);font-size:9.5px"></span>
              </div>
              <div class="alo-toolkit-list" data-part="toolkit-list"></div>
            </div>
          </div>
          <div class="alo-cycles-card" data-part="cycles-card">
            <div class="alo-cycles-h">
              <span class="alo-cycles-title">Timeline</span>
              <span class="alo-cycles-count" data-part="cycles-count"></span>
              <button class="alo-card-expand expanded" data-card="cycles" title="Collapse to compact scroll">⤢</button>
            </div>
            <div class="alo-cycles" data-part="cycles">
              <div class="alo-empty">Waiting for events…</div>
            </div>
          </div>
          <div class="alo-final-pane" data-part="final" ${showFinal?'':'hidden'}>
            <div class="alo-final-h">
              <span class="alo-final-title">Run complete</span>
              <div class="alo-final-actions" data-part="final-actions">
                <button class="alo-card-expand expanded" data-card="final" title="Collapse to compact scroll">⤢</button>
              </div>
            </div>
            <div data-part="final-body"></div>
          </div>
        </div>`;
    }

    // ───────────────────── Public API ────────────────────────────────
    setSessionId(sid){ this._sessionId = sid || ''; }
    setHitlEndpoint(url){ this._hitlEndpoint = url || this._hitlEndpoint; }
    setApiBase(url){ this._apiBase = (url||'').replace(/\/$/, ''); }
    setShowThinking(b){ this._showThinking = !!b; this.setAttribute('show-thinking', b?'true':'false'); }
    setMaxResultPreview(n){ this._maxResultPreview = parseInt(n,10) || 800; }
    getResult(){ return this._lastResult; }

    reset(){
      this._cycleRefs.clear();
      this._hitlPending.clear();
      this._activeWsJobs.clear();
      this._lastResult = null;
      const cycles = this._sr.querySelector('.alo-cycles');
      if(cycles) cycles.innerHTML = '<div class="alo-empty">Waiting for events…</div>';
      const cc = this._sr.querySelector('[data-part="cycles-count"]');
      if(cc) cc.textContent = '';
      const tri = this._sr.querySelector('.alo-triage');
      if(tri) tri.classList.remove('open');
      const tk = this._sr.querySelector('.alo-toolkit');
      if(tk) tk.classList.remove('open');
      const fp = this._sr.querySelector('.alo-final-pane');
      if(fp) fp.classList.remove('show');
      const fb = this._sr.querySelector('[data-part="final-body"]');
      if(fb) fb.innerHTML = '';
      this._startCardEl = null;
    }

    appendEvent(ev){
      if(!ev || typeof ev !== 'object') return;
      try{ this._handleEvent(ev); }
      catch(e){ /* swallow render errors so a bad event doesn't break the stream */ console && console.warn && console.warn('alo: render error', e, ev); }
    }
    appendEvents(arr){
      if(!Array.isArray(arr)) return;
      arr.forEach(e => this.appendEvent(e));
    }

    abort(){
      if(this._abort){ try{ this._abort.abort(); }catch(_){} this._abort = null; }
    }

    /**
     * Fetch /POST a streaming JSON-SSE endpoint and feed events into this
     * element. The body is sent as JSON; events arrive as `data: {…}\n\n`.
     * Returns when the stream closes or [DONE] is received.
     */
    async bindStream(url, body, opts){
      this.abort();
      this._abort = new AbortController();
      const abort = this._abort;
      const base = this._apiBase || _apiBase();
      const fullUrl = url.startsWith('http') ? url : (base + url);
      let resp;
      try{
        resp = await fetch(fullUrl, {
          method: (opts && opts.method) || 'POST',
          headers: Object.assign({'Content-Type':'application/json'}, (opts && opts.headers) || {}),
          body: body == null ? undefined : (typeof body === 'string' ? body : JSON.stringify(body)),
          signal: abort.signal,
        });
      }catch(e){
        this.appendEvent({type:'error', error:String(e && e.message ? e.message : e)});
        return;
      }
      if(!resp.ok){
        let txt = '';
        try{ txt = await resp.text(); }catch(_){}
        this.appendEvent({type:'error', error:'HTTP '+resp.status+(txt?': '+txt.slice(0,200):'')});
        return;
      }
      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      while(true){
        let chunk;
        try{ chunk = await reader.read(); }catch(_){ break; }
        if(chunk.done) break;
        buf += dec.decode(chunk.value, {stream:true});
        const lines = buf.split('\n');
        buf = lines.pop();
        for(const line of lines){
          if(!line.startsWith('data:')) continue;
          const raw = line.slice(5).trim();
          if(raw === '[DONE]'){ return; }
          let ev;
          try{ ev = JSON.parse(raw); }catch(_){ continue; }
          this.appendEvent(ev);
        }
      }
    }

    // ───────────────────── Internal: event dispatch ──────────────────
    _handleEvent(ev){
      const t = ev.type || '';

      // start
      if(t === 'start'){
        this._renderStartCard(ev);
        return;
      }

      // top-level error
      if(t === 'error'){
        this._cycleEl(`<div class="alo-cycle-h"><span class="alo-cycle-tool">Error</span></div>
          <div class="alo-cycle-preview">${_esc(ev.error||'')}</div>`, 'error');
        this.dispatchEvent(new CustomEvent('alo:error', {detail:{error: ev.error||''}, bubbles:true}));
        return;
      }

      // v5 planning failure — orchestrator produced no usable plan. Shown as a
      // dedicated error card (the final pane also renders the error row) so the
      // failure is explicit rather than silently falling through to a default plan.
      if(t === 'agent_loop_v5.error' || t === 'agent_loop_v6.error'){
        const pc = this._sr.querySelector('.alo-cycle.planning[data-planning="1"]');
        if(pc) pc.remove();
        this._cycleEl(`<div class="alo-cycle-h"><span class="alo-cycle-tool">⚠ Planning failed</span></div>
          <div class="alo-cycle-preview">${_esc(ev.error||'')}</div>`, 'error');
        this.dispatchEvent(new CustomEvent('alo:error', {detail:{error: ev.error||''}, bubbles:true}));
        return;
      }

      // think events (any variant)
      if(t === 'agent_loop.think' || t === 'agent_loop_v2.think'
         || t === 'agent_loop_v3.think' || t === 'agent_loop_v4.think'
         || t === 'agent_loop_v5.think'){
        if(!this._showThinking) return;
        const ref = this._cycleRefs.get(ev.cycle);
        if(!ref || !ref.el) return;
        // Structured think event is the fuller copy (up to 2000 chars vs the
        // 600-char [think #N] stream token). Mark it so the stream-token flush
        // below won't overwrite it with the truncated version.
        ref._thinkFromEvent = true;
        this._appendThink(ref, ev.thought || '');
        return;
      }

      // ── v4-only: LIVE thought streaming (token-by-token during planning) ──
      // The backend emits the cumulative reasoning as it generates; _appendThink
      // replaces the block content, so repeated deltas render live without dupes.
      if(t === 'agent_loop_v4.think_delta'){
        if(!this._showThinking) return;
        const ref = this._cycleRefs.get(ev.cycle);
        if(!ref || !ref.el) return;
        ref._thinkFromEvent = true;  // suppress the post-hoc [think #N] blob flush
        this._appendThink(ref, ev.text || '');
        // Auto-expand the think block so the reasoning is visible as it streams.
        const d = ref.el.querySelector('.alo-cycle-think');
        if(d) d.open = true;
        return;
      }

      // ── Routed Ollama node — which instance/model served this call ──────
      if(t === 'ollama.request'){
        const node = ev.instance_id || '';
        if(!node) return;
        // Attach to the most recent cycle card, else the Starting card.
        let lastCycle = 0;
        this._cycleRefs.forEach((_v, k) => { if(k > lastCycle) lastCycle = k; });
        const ref = this._cycleRefs.get(lastCycle);
        const host = (ref && ref.el) ? ref.el
                   : (this._startCardEl && this._startCardEl.isConnected ? this._startCardEl : null);
        if(!host) return;
        let badge = host.querySelector(':scope > .alo-node-badge');
        if(!badge){
          badge = document.createElement('div');
          badge.className = 'alo-node-badge';
          badge.style.cssText = 'display:inline-flex;align-items:center;gap:4px;font-size:8.5px;'
            + 'color:var(--dim2,#8a7e70);margin-top:3px;font-family:var(--mono,monospace)';
          host.appendChild(badge);
        }
        const rt = ev.routing || {};
        const jt = rt.job_type ? ` · ${rt.job_type}` : '';
        const esc2 = rt.escalated ? ' · ⇧len-escalated' : '';
        const est = (rt.est_seconds !== undefined && rt.est_seconds !== null) ? ` · ~${rt.est_seconds}s` : '';
        badge.textContent = `⚙ node: ${node}${ev.model ? ' · ' + ev.model : ''}${jt}${esc2}${est}`;
        badge.title = 'Ollama routing — instance that served this request'
          + (ev.instance_url ? ' — ' + ev.instance_url : '')
          + (rt.rule_source ? '\nrule: ' + rt.rule_source : '')
          + (rt.prompt_chars ? '\nprompt: ' + rt.prompt_chars + ' chars' : '')
          + ((rt.reason && rt.reason.length) ? '\n' + rt.reason.join('\n') : '');
        return;
      }

      // ── v4-only: step selection (which phases will run) ──────────────
      if(t === 'agent_loop_v4.step_plan'){
        const steps = (ev.steps||[]).map(_esc).join(' › ') || '(none)';
        const reason = ev.reason ? `<div class="alo-cycle-preview">${_esc(ev.reason)}</div>` : '';
        const single = ev.single_action
          ? ` <span class="alo-version-pill" style="padding:1px 5px;font-size:8px;background:#2a3a1d;color:#aede7e">single-action</span>`
          : '';
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">⚙ Steps selected</span>
          <span class="alo-cycle-status">${steps}${single}</span>
        </div>${reason}`, 'expand');
        return;
      }

      // ── v4-only: plan / todo list (re-emitted as items complete) ─────
      if(t === 'agent_loop_v4.plan'){
        const todos = ev.todos||[];
        const rows = todos.map(td =>
          `<div style="font-family:var(--mono,monospace);font-size:10px;color:${td.done?'var(--ok,#5a9e8f)':'var(--text2,#bfb6a8)'}">`
          + `${td.done?'✓':'☐'} ${_esc(td.task||'')}</div>`).join('');
        const done = todos.filter(td=>td.done).length;
        // Update the existing plan card in place if present, else create one.
        let card = this._sr.querySelector('.alo-cycle.plan[data-plan="1"]');
        const inner = `<div class="alo-cycle-h">
            <span class="alo-cycle-tool">▤ Plan</span>
            <span class="alo-cycle-status">${done}/${todos.length} done</span>
          </div><div class="alo-cycle-preview">${rows||'(empty plan)'}</div>`;
        if(card){ card.innerHTML = inner; }
        else {
          card = this._cycleEl(inner, 'plan');
          if(card) card.setAttribute('data-plan','1');
        }
        return;
      }

      // ── v4-only: strict completion check before final ────────────────
      // The check belongs to the cycle that *proposed* the final answer — render
      // it inside that cycle's card (which would otherwise sit empty showing only
      // "planning…") rather than as a detached standalone card.
      if(t === 'agent_loop_v4.completion_check'){
        const passed = !!ev.passed;
        const miss = (ev.missing||[]).map(_esc).map(m=>`<div>• ${m}</div>`).join('');
        const label = passed ? '✓ Completion check passed' : '⚠ Completion check failed';
        const ref = this._cycleRefs.get(ev.cycle);
        if(ref && ref.el){
          ref.el.classList.add(passed ? 'done' : 'error');
          const toolEl = ref.el.querySelector(':scope > .alo-cycle-h > .alo-cycle-tool');
          if(toolEl){ toolEl.textContent = label; }
          if(!passed && miss){
            let body = ref.el.querySelector('.alo-cc-miss');
            if(!body){
              body = document.createElement('div');
              body.className = 'alo-cycle-preview alo-cc-miss';
              ref.el.appendChild(body);
            }
            body.innerHTML = miss;
          }
          return;
        }
        // Fallback: no cycle ref for this event (shouldn't normally happen).
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">${label}</span>
        </div>${passed?'':`<div class="alo-cycle-preview">${miss}</div>`}`,
          passed?'expand':'error');
        return;
      }

      // ── v5-only: orchestrator still planning (heartbeat during the long
      //    blocking planner generation) — one self-updating "planning…" card ──
      if(t === 'agent_loop_v5.planning'){
        // Once the plan is streaming live, its builder card supersedes the
        // heartbeat — don't add a second "planning…" card alongside it.
        if(this._sr.querySelector('.alo-cycle.plan[data-plan-v5="1"]')) return;
        let card = this._sr.querySelector('.alo-cycle.planning[data-planning="1"]');
        const secs = ev.elapsed_s||0;
        if(card){
          // Update the elapsed counter only — the rolling .alo-phrase keeps
          // its own rhythm (rebuilding innerHTML each heartbeat froze it).
          const st = card.querySelector('.alo-cycle-status');
          if(st) st.textContent = secs + 's';
          return;
        }
        const pool = ALO_THINK_PHRASES();
        const first = pool[Math.floor(Math.random()*pool.length)];
        const inner = `<div class="alo-cycle-h">
            <span class="alo-spinner"></span>
            <span class="alo-cycle-tool">▤ Orchestrator planning…</span>
            <span class="alo-cycle-status">${secs}s</span>
          </div><div class="alo-cycle-thought"><span class="alo-phrase">${_esc(first)}</span></div>`;
        card = this._cycleEl(inner, 'planning'); if(card) card.setAttribute('data-planning','1');
        return;
      }

      // ── v6/V7: the plan STREAMING live, token by token (auto-closed JSON) ──
      if(t === 'agent_loop_v6.plan_token'){
        const body = _autoCloseStructured(ev.text||'');
        const inner = `<div class="alo-cycle-h">
            <span class="alo-cycle-tool">▤ Orchestrator planning…</span>
            <span class="alo-cycle-status"><span class="alo-spinner"></span> building</span>
          </div><pre class="alo-cycle-preview" style="white-space:pre-wrap;max-height:240px;overflow:auto;margin:0;font-size:9.5px">${_esc(body)}</pre>`;
        let card = this._sr.querySelector('.alo-cycle.plan[data-plan-v5="1"]');
        if(card){ card.innerHTML = inner; }
        else {
          // Replace the transient heartbeat card with the live plan builder.
          const pc = this._sr.querySelector('.alo-cycle.planning[data-planning="1"]');
          if(pc) pc.remove();
          card = this._cycleEl(inner, 'plan');
          if(card) card.setAttribute('data-plan-v5','1');
        }
        return;
      }

      // ── v5/v6: orchestrator step plan (step shape, not todos) ────────
      if(t === 'agent_loop_v5.plan' || t === 'agent_loop_v6.plan'){
        // The plan is ready — drop the transient "planning…" heartbeat card.
        const pc = this._sr.querySelector('.alo-cycle.planning[data-planning="1"]');
        if(pc) pc.remove();
        const steps = ev.steps||[];
        const rows = steps.map(s => {
          const caps = (s.caps||[]).map(_esc).join(', ');
          const sk = (s.skills||[]).length
            ? ` <span style="color:var(--acc2,#a8c87a)">+skills: ${(s.skills||[]).map(_esc).join(', ')}</span>` : '';
          const cx = s.complex
            ? ` <span style="color:var(--warn,#c9a45a)" title="expands into its own sub-plan">⧉ sub-plan</span>` : '';
          const ph = (s.phases&&s.phases.length)
            ? ` <span style="color:var(--acc2,#a8c87a)" title="runs as scoped phases">▤ ${s.phases.map(_esc).join('→')}</span>` : '';
          return `<div style="font-family:var(--mono,monospace);font-size:10px;color:var(--text2,#bfb6a8);margin:2px 0">`
            + `<b>${s.id}.</b> ${_esc(s.title||'')}${cx}${ph}`
            + (caps?`<div style="color:var(--dim,#a89f92);font-size:9px;margin-left:12px">caps: ${caps}${sk}</div>`:'')
            + `</div>`;
        }).join('');
        const reason = ev.reason ? `<div class="alo-cycle-thought" style="font-style:italic">${_esc(ev.reason)}</div>` : '';
        // v6 also declares a whole-goal completion criterion.
        const dw = ev.done_when ? `<div class="alo-cycle-thought" style="color:var(--acc2,#a8c87a)">🎯 done when: ${_esc(ev.done_when)}</div>` : '';
        let card = this._sr.querySelector('.alo-cycle.plan[data-plan-v5="1"]');
        const inner = `<div class="alo-cycle-h">
            <span class="alo-cycle-tool">▤ Orchestrator plan</span>
            <span class="alo-cycle-status">${steps.length} step${steps.length===1?'':'s'}</span>
          </div>${reason}${dw}<div class="alo-cycle-preview">${rows||'(empty plan)'}</div>`;
        if(card){ card.innerHTML = inner; }
        else { card = this._cycleEl(inner, 'plan'); if(card) card.setAttribute('data-plan-v5','1'); }
        return;
      }

      // ── v6: generic "stage starting" heartbeat — a slow LLM-backed section
      //    (verify, assess, gate, deliverable) gets a spinner + cycling-phrase
      //    placeholder card the MOMENT it starts, same idea as the orchestrator
      //    planning heartbeat, instead of just looking frozen while the judge
      //    call is in flight. Removed by the real card (matched on stage+key,
      //    see each handler below) once that arrives. ──
      if(t === 'agent_loop_v6.stage_start'){
        const key = `${ev.stage}:${ev.key}`;
        if(this._sr.querySelector(`.alo-cycle.stage-hb[data-stage-key="${key}"]`)) return;
        const pool = ALO_THINK_PHRASES();
        const first = pool[Math.floor(Math.random()*pool.length)];
        const card = this._cycleEl(`<div class="alo-cycle-h">
            <span class="alo-spinner"></span>
            <span class="alo-cycle-tool">${_esc(ev.label||'Working…')}</span>
          </div><div class="alo-cycle-thought"><span class="alo-phrase">${_esc(first)}</span></div>`, 'stage-hb');
        if(card) card.setAttribute('data-stage-key', key);
        return;
      }

      // ── v6-only: adaptive controller assessment after a step ─────────
      if(t === 'agent_loop_v6.assess'){
        this._sr.querySelector(`.alo-cycle.stage-hb[data-stage-key="assess:${ev.after_step}"]`)?.remove();
        const ACT = {
          continue: {ic:'→', lbl:'continue', cls:'expand'},
          insert:   {ic:'⊕', lbl:'insert step(s)', cls:'warn'},
          replan:   {ic:'↻', lbl:'replan remaining', cls:'warn'},
          stop:     {ic:'■', lbl:'stop — goal met', cls:'done'},
        };
        const a = ACT[ev.action] || {ic:'•', lbl:ev.action||'', cls:'expand'};
        const stepRows = (ev.steps||[]).map(s =>
          `<div style="font-family:var(--mono,monospace);font-size:10px;color:var(--text2,#bfb6a8);margin:1px 0">`
          + `<b>${_esc(String(s.id))}.</b> ${_esc(s.title||'')}`
          + ((s.caps&&s.caps.length)?`<span style="color:var(--dim,#a89f92);font-size:9px"> — ${(s.caps||[]).map(_esc).join(', ')}</span>`:'')
          + `</div>`).join('');
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">🧭 Controller · ${a.ic} ${a.lbl}</span>
          <span class="alo-cycle-status">after step ${_esc(String(ev.after_step||'?'))}</span>
        </div>${ev.assessment?`<div class="alo-cycle-thought" style="font-style:italic">${_esc(ev.assessment)}</div>`:''}${stepRows?`<div class="alo-cycle-preview">${stepRows}</div>`:''}`, a.cls);
        return;
      }

      // ── v6-only: per-step success-criterion verification verdict ─────
      if(t === 'agent_loop_v6.verify'){
        this._sr.querySelector(`.alo-cycle.stage-hb[data-stage-key="verify:${ev.step_id}"]`)?.remove();
        const met = !!ev.met;
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">🎯 Success check · step ${_esc(String(ev.step_id||'?'))} — ${met?'met':'NOT met'}</span>
          <span class="alo-cycle-status">${met?'✓':'⚠'}</span>
        </div>${ev.criterion?`<div class="alo-cycle-thought">bar: ${_esc(ev.criterion)}</div>`:''}${ev.reason?`<div class="alo-cycle-preview" style="font-style:italic">${_esc(ev.reason)}</div>`:''}`,
          met?'done':'warn');
        return;
      }

      // ── v6-only: shared ledger snapshot (progress across the run) ────
      if(t === 'agent_loop_v6.ledger'){
        const done = ev.done||[];
        const ok = done.filter(d=>d.ok && d.met!==false).length;
        const unmet = done.filter(d=>d.ok && d.met===false).length;
        const rows = done.map(d => {
          const mark = !d.ok ? '✗' : (d.met===false ? '⚠' : '✓');
          const col  = !d.ok ? 'var(--err,#c75a5a)'
                     : (d.met===false ? 'var(--warn,#c7a15a)' : 'var(--ok,#5a9e8f)');
          const tip  = (d.title||'') + (d.met===false ? ' — success bar NOT met' : '');
          return `<span title="${_esc(tip)}" style="color:${col}">${mark}${_esc(String(d.id))}</span>`;
        }).join(' ');
        const pend = (ev.pending||[]).map(s=>`${s.id}`).join(' ') || '—';
        let card = this._sr.querySelector('.alo-cycle.ledger[data-ledger="1"]');
        const inner = `<div class="alo-cycle-h">
            <span class="alo-cycle-tool">🗒 Ledger</span>
            <span class="alo-cycle-status">${ok}/${done.length} ok${unmet?` · ${unmet} unmet`:''} · ${(ev.pending||[]).length} pending</span>
          </div><div class="alo-cycle-preview" style="font-family:var(--mono,monospace);font-size:10px">done: ${rows||'—'}<br>pending: ${_esc(pend)}</div>`;
        if(card){ card.innerHTML = inner; }
        else { card = this._cycleEl(inner, 'ledger'); if(card) card.setAttribute('data-ledger','1'); }
        return;
      }

      // ── v6-only: delivery agent's final markdown deliverable ─────────
      if(t === 'agent_loop_v6.deliverable'){
        this._sr.querySelector(`.alo-cycle.stage-hb[data-stage-key="deliverable:final"]`)?.remove();
        const md = ev.markdown||'';
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">📦 Deliverable</span>
          <span class="alo-cycle-status">${md.length} chars</span>
        </div><div class="alo-handover-body" style="margin-top:4px">${_renderMarkdown(md)}</div>`, 'done');
        return;
      }

      // ── v6-only: final completion gate ───────────────────────────────
      if(t === 'agent_loop_v6.gate'){
        this._sr.querySelector(`.alo-cycle.stage-hb[data-stage-key="gate:final"]`)?.remove();
        const complete = !!ev.complete;
        const miss = (ev.missing||[]).map(m=>`<div>• ${_esc(m)}</div>`).join('');
        const fu = (ev.follow_up||[]).map(s=>`${s.id}. ${_esc(s.title||'')}`).join(' › ');
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">${complete?'✓ Completion gate passed':'⚠ Completion gate — goal not fully met'}</span>
        </div>${(!complete&&miss)?`<div class="alo-cycle-preview">${miss}</div>`:''}${fu?`<div class="alo-cycle-thought">follow-up: ${_esc(fu)}</div>`:''}`,
          complete?'done':'warn');
        return;
      }

      // ── v5-only: a specialist sub-agent step begins (section header) ──
      if(t === 'agent_loop_v5.step_start'){
        const caps = (ev.caps||[]).map(_esc).join(', ') || '(reasoning only)';
        const sk = (ev.skills||[]).length
          ? `<div class="alo-step-meta">🛠 skills: ${(ev.skills||[]).map(_esc).join(', ')}</div>` : '';
        const PH = {explore:'🔍 explore', think:'💭 think', act:'⚙ act', verify:'✓ verify'};
        const phaseBadge = ev.phase
          ? ` <span style="color:var(--acc2,#a8c87a);font-size:9px">· ${PH[ev.phase]||_esc(ev.phase)}</span>` : '';
        const phasesMeta = (ev.phases&&ev.phases.length)
          ? `<div class="alo-step-meta">▤ phases: ${ev.phases.map(p=>_esc(p)).join(' → ')}</div>` : '';
        const el = this._cycleEl(`<div class="alo-cycle-h">
            <span class="alo-cycle-tool">▶ Step ${_esc(String(ev.step_id))} — ${_esc(ev.title||'')}${phaseBadge}</span>
            <span class="alo-cycle-status"><span class="alo-spinner"></span> running…</span>
          </div>
          ${ev.goal?`<div class="alo-cycle-thought">${_esc(ev.goal)}</div>`:''}
          <div class="alo-step-meta">🧰 scoped caps: ${caps}</div>${sk}${phasesMeta}`, 'step');
        if(el) el.setAttribute('data-step', String(ev.step_id));
        return;
      }

      // ── v5-only: the EXACT context/configuration a step's specialist got ──
      // Attaches a reveal to the step card showing the verbatim system prompt
      // plus a structured breakdown (caps, skills, prior-step context). Stored
      // so the loop graph / context layer can link to it.
      if(t === 'agent_loop_v5.step_context'){
        this._stepContext = this._stepContext || {};
        this._stepContext[ev.step_id] = ev;
        const card = this._sr.querySelector(`.alo-cycle.step[data-step="${ev.step_id}"]`);
        if(card && !card.querySelector('.alo-ctx-reveal')){
          card.appendChild(this._buildCtxReveal(ev));
        }
        this.dispatchEvent(new CustomEvent('alo:step-context', {detail:ev, bubbles:true}));
        return;
      }

      // ── v5-only: a step's v4-style phase cadence (explore/think/act/verify) ──
      if(t === 'agent_loop_v5.phases'){
        const PH = {explore:'🔍 explore (recon)', think:'💭 think', act:'⚙ act', verify:'✓ verify'};
        const seq = (ev.phases||[]).map(p=>PH[p]||_esc(p)).join('  →  ') || '(none)';
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">▤ Phased step ${_esc(String(ev.parent_id||'?'))} — ${_esc(ev.title||'')}</span>
          <span class="alo-cycle-status">${(ev.phases||[]).length} phase${(ev.phases||[]).length===1?'':'s'}</span>
        </div><div class="alo-cycle-preview">${seq}</div>`, 'plan');
        return;
      }

      // ── v6/V7: the long-form master plan STREAMING live, token by token ──
      if(t === 'agent_loop_v6.master_plan_token'){
        const inner = `<div class="alo-cycle-h">
            <span class="alo-cycle-tool">🧠 Strategising…</span>
            <span class="alo-cycle-status"><span class="alo-spinner"></span> ${_esc(String((ev.text||'').length))} chars</span>
          </div>${ev.persona?`<div class="alo-cycle-thought" style="font-style:italic">${_esc(ev.persona)}</div>`:''}
          <div class="alo-cycle-preview alo-md" style="max-height:260px;overflow:auto">${_renderMarkdown(ev.text||'')}</div>`;
        let card = this._sr.querySelector('.alo-cycle.masterplan[data-mp="1"]');
        if(card){ card.innerHTML = inner; }
        else { card = this._cycleEl(inner, 'masterplan'); if(card) card.setAttribute('data-mp','1'); }
        return;
      }

      // ── v5/v6: extreme-goal long-form master plan (specialist planner) ──
      if(t === 'agent_loop_v5.master_plan' || t === 'agent_loop_v6.master_plan'){
        const lf = ev.long_form||'';
        const inner = lf
          ? `<div class="alo-cycle-h">
              <span class="alo-cycle-tool">🧠 Master plan (specialist planner)</span>
              <span class="alo-cycle-status">${lf.length} chars</span>
            </div>${ev.persona?`<div class="alo-cycle-thought" style="font-style:italic">${_esc(ev.persona)}</div>`:''}
            <details style="margin-top:4px" open><summary style="cursor:pointer;font-size:9.5px;color:var(--dim,#a89f92)">long-form strategy</summary>
            <div class="alo-md" style="max-height:380px;overflow:auto;resize:vertical;padding:2px 2px 2px 0">${_renderMarkdown(lf)}</div></details>`
          : `<div class="alo-cycle-h">
              <span class="alo-cycle-tool">🧠 Master plan — no strategy produced</span>
              <span class="alo-cycle-status" style="color:var(--warn,#c7a15a)">⚠ empty</span>
            </div><div class="alo-cycle-thought" style="font-style:italic">${_esc(ev.note||'the strategic planner returned nothing (generation failed or timed out) — proceeding with the structured plan')}</div>`;
        // Reuse the live streaming card if it exists, else make a fresh one.
        let card = this._sr.querySelector('.alo-cycle.masterplan[data-mp="1"]');
        if(card){ card.innerHTML = inner; card.removeAttribute('data-mp'); }
        else { this._cycleEl(inner, 'plan'); }
        return;
      }

      // ── v5/v6/V7: piecewise master planning — the plan splits into ordered
      //    pieces, each expanded into steps with the full plan in context ──
      if(t === 'agent_loop_v5.master_plan_pieces' || t === 'agent_loop_v6.master_plan_pieces'){
        const ps = (ev.pieces||[]).map(p=>{
          const meta = [];
          if(p.deliverable) meta.push(`<div><b>▸ deliverable:</b> ${_esc(p.deliverable)}</div>`);
          if(p.success_metric) meta.push(`<div><b>◎ success:</b> ${_esc(p.success_metric)}</div>`);
          if(p.dependencies && p.dependencies.length) meta.push(`<div><b>⇢ depends on piece(s):</b> ${_esc((p.dependencies||[]).join(', '))}</div>`);
          if(p.timescale) meta.push(`<div><b>⏱</b> ${_esc(p.timescale)}</div>`);
          const caps = (p.caps&&p.caps.length)?`<div style="color:var(--acc,#9ecb6b)">🧰 ${_esc((p.caps||[]).join(', '))}</div>`:'';
          const defer = p.deferred?` <span style="color:var(--warn,#c7a15a)" title="deferred to a later dream cycle">· deferred</span>`:'';
          return `<div style="margin:3px 0"><b>${_esc(String(p.id))}.</b> ${_esc(p.title||'')}${defer}`
            +(p.objective?`<div style="color:var(--dim,#a89f92);font-size:9px;margin-left:12px">${_esc(p.objective)}</div>`:'')
            +((meta.length||caps)?`<div style="color:var(--dim,#a89f92);font-size:8.5px;margin-left:12px">${meta.join('')}${caps}</div>`:'')
            +`</div>`;
        }).join('');
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">🧩 Piecewise planning · ${_esc(String((ev.pieces||[]).length))} pieces</span>
          <span class="alo-cycle-status">one piece at a time, full plan in context</span>
        </div><div class="alo-cycle-preview">${ps}</div>`, 'plan');
        return;
      }
      if(t === 'agent_loop_v5.master_plan_piece_planned' || t === 'agent_loop_v6.master_plan_piece_planned'){
        const ss = (ev.steps||[]).map(s=>
          `<div style="margin:1px 0">${_esc(String(s.id))}. ${_esc(s.title||'')}`
          +((s.caps&&s.caps.length)?` <span style="color:var(--dim,#a89f92);font-size:8.5px">[${_esc((s.caps||[]).join(', '))}]</span>`:'')
          +`</div>`).join('') || '<div style="color:var(--warn,#c7a15a)">no steps produced for this piece</div>';
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">🧩 Piece ${_esc(String(ev.piece||'?'))} planned — ${_esc(ev.title||'')}</span>
          <span class="alo-cycle-status">${_esc(String((ev.steps||[]).length))} step(s)</span>
        </div><div class="alo-cycle-preview">${ss}</div>`, 'plan');
        return;
      }

      // ── v5/v6/V7: agent-authored cap chain — a pipeline run in one turn, each
      //    hop's output piped into the next. The hops themselves render as normal
      //    tool cards; this is just the header marking them as chained. ──
      if(t === 'agent_loop_v5.chain' || t === 'agent_loop_v6.chain'){
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">🔗 Chained ${_esc(String((ev.hops||[]).length))} caps</span>
          <span class="alo-cycle-status">${_esc((ev.hops||[]).join(' → '))}</span>
        </div>`, 'plan');
        return;
      }
      if(t === 'agent_loop_v5.chain_skip' || t === 'agent_loop_v6.chain_skip'){
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">⤿ Chain hop skipped</span>
          <span class="alo-cycle-status">${_esc(ev.tool||'')} · when ${_esc(ev.when||'')} = false</span>
        </div>`, 'expand');
        return;
      }

      // ── v6/V7: planning-tier classification (drives the strategic planner) ──
      if(t === 'agent_loop_v6.tier'){
        const TI = {single:{ic:'⚡',lbl:'single-cap'}, simple:{ic:'▸',lbl:'simple'},
                    complex:{ic:'▤',lbl:'complex'}, strategic:{ic:'🧭',lbl:'strategic · multi-day'}};
        const ti = TI[ev.tier] || {ic:'•', lbl:ev.tier||'?'};
        const sup = ev.escalation_suppressed
          ? ` <span style="color:var(--warn,#c7a15a)" title="auto-escalate off — held at complex">↯ escalation held</span>` : '';
        const src = [];
        if(ev.heuristic) src.push('heuristic: '+_esc(ev.heuristic));
        if(ev.llm) src.push('llm: '+_esc(ev.llm));
        const sug = (ev.suggested && ev.suggested !== ev.tier) ? ('suggested '+_esc(ev.suggested)) : '';
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">${ti.ic} Tier · ${_esc(ti.lbl)}${sup}</span>
          <span class="alo-cycle-status">${sug}</span>
        </div>${ev.reason?`<div class="alo-cycle-thought" style="font-style:italic">${_esc(ev.reason)}</div>`:''}${src.length?`<div class="alo-cycle-preview" style="font-size:9px;color:var(--dim,#a89f92)">${src.join(' · ')}</div>`:''}`, 'plan');
        return;
      }

      // ── V7 fast path: a 'single' goal resolved by ONE cap, no orchestration ──
      if(t === 'agent_loop_v6.fast_path'){
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">⚡ Fast path · ${_esc(ev.cap||'')}</span>
          <span class="alo-cycle-status">single cap</span>
        </div>${ev.preview?`<div class="alo-cycle-preview" style="white-space:pre-wrap">${_esc(String(ev.preview))}</div>`:''}`, 'done');
        return;
      }

      // ── V7 consultation: clarifying questions asked before planning ──
      if(t === 'agent_loop_v6.clarify_request'){
        const qs = (ev.questions||[]).map(q=>`<div style="margin:2px 0">• ${_esc(q)}</div>`).join('');
        const viaComms = ev.comms_address ? ` · also sent to ${_esc(ev.channel||'comms')}` : '';
        const el = this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">❓ Clarify before planning</span>
          <span class="alo-cycle-status">${_esc(String((ev.questions||[]).length))} question(s) · ${_esc(String(ev.timeout_secs||''))}s${viaComms}</span>
        </div><div class="alo-cycle-preview">${qs}</div>
        <div class="alo-clarify-answer" style="margin-top:5px">
          <textarea class="alo-clarify-input" rows="2" placeholder="Answer (or leave blank to let it proceed on assumptions)…" style="width:100%;box-sizing:border-box;font-size:10px;background:var(--bg0,#181614);color:var(--text,#e8e0d4);border:1px solid var(--border,#3a352e);border-radius:4px;padding:4px;resize:vertical"></textarea>
          <div style="display:flex;gap:5px;margin-top:3px">
            <button class="alo-clarify-send" style="font-size:10px;padding:3px 9px;border-radius:4px;border:1px solid var(--acc,#9ecb6b);background:transparent;color:var(--acc,#9ecb6b);cursor:pointer">Send answer</button>
            <button class="alo-clarify-skip" style="font-size:10px;padding:3px 9px;border-radius:4px;border:1px solid var(--border,#3a352e);background:transparent;color:var(--dim,#a89f92);cursor:pointer">Proceed without</button>
          </div>
        </div>`, 'warn');
        if(el){
          const step = (typeof ev.step === 'number') ? ev.step : -424242;
          const send = (answer) => {
            const box = el.querySelector('.alo-clarify-answer');
            if(box) box.querySelectorAll('button,textarea').forEach(x=>x.disabled=true);
            const base = this._apiBase || _apiBase();
            fetch(base + this._hitlEndpoint, {
              method:'POST', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({session_id:this._sessionId, step, decision:'continue', comment:answer||''}),
            }).catch(()=>{});
            if(box) box.innerHTML = `<span style="font-size:10px;color:var(--dim,#a89f92);font-style:italic">${answer?'answer sent':'proceeding without answers'}</span>`;
          };
          const ta = el.querySelector('.alo-clarify-input');
          el.querySelector('.alo-clarify-send')?.addEventListener('click', ()=>send((ta&&ta.value||'').trim()));
          el.querySelector('.alo-clarify-skip')?.addEventListener('click', ()=>send(''));
        }
        return;
      }
      if(t === 'agent_loop_v6.clarify_resolved'){
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">❓ Clarification ${ev.answered?'received':'skipped'}</span>
          <span class="alo-cycle-status">${_esc(ev.decision||'')}</span>
        </div>`, ev.answered?'done':'expand');
        return;
      }
      // ── V7: a RUNNING STEP asks the user a question (ask_user) — same answer
      //    affordance as the pre-plan clarify, keyed to the step's own HITL id. ──
      if(t === 'agent_loop_v6.step_question'){
        const viaComms = ev.comms_address ? ` · also sent to ${_esc(ev.channel||'comms')}` : '';
        const el = this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">🙋 Step needs your input</span>
          <span class="alo-cycle-status">step ${_esc(String(ev.step_id||'?'))} · ${_esc(String(ev.timeout_secs||''))}s${viaComms}</span>
        </div><div class="alo-cycle-preview"><div style="margin:2px 0">• ${_esc(ev.question||'')}</div></div>
        <div class="alo-clarify-answer" style="margin-top:5px">
          <textarea class="alo-clarify-input" rows="2" placeholder="Answer (or leave blank to let the step proceed on assumptions)…" style="width:100%;box-sizing:border-box;font-size:10px;background:var(--bg0,#181614);color:var(--text,#e8e0d4);border:1px solid var(--border,#3a352e);border-radius:4px;padding:4px;resize:vertical"></textarea>
          <div style="display:flex;gap:5px;margin-top:3px">
            <button class="alo-clarify-send" style="font-size:10px;padding:3px 9px;border-radius:4px;border:1px solid var(--acc,#9ecb6b);background:transparent;color:var(--acc,#9ecb6b);cursor:pointer">Send answer</button>
            <button class="alo-clarify-skip" style="font-size:10px;padding:3px 9px;border-radius:4px;border:1px solid var(--border,#3a352e);background:transparent;color:var(--dim,#a89f92);cursor:pointer">Proceed without</button>
          </div>
        </div>`, 'warn');
        if(el){
          const step = (typeof ev.step === 'number') ? ev.step : -424242;
          const send = (answer) => {
            const box = el.querySelector('.alo-clarify-answer');
            if(box) box.querySelectorAll('button,textarea').forEach(x=>x.disabled=true);
            const base = this._apiBase || _apiBase();
            fetch(base + this._hitlEndpoint, {
              method:'POST', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({session_id:this._sessionId, step, decision:'continue', comment:answer||''}),
            }).catch(()=>{});
            if(box) box.innerHTML = `<span style="font-size:10px;color:var(--dim,#a89f92);font-style:italic">${answer?'answer sent':'proceeding without answers'}</span>`;
          };
          const ta = el.querySelector('.alo-clarify-input');
          el.querySelector('.alo-clarify-send')?.addEventListener('click', ()=>send((ta&&ta.value||'').trim()));
          el.querySelector('.alo-clarify-skip')?.addEventListener('click', ()=>send(''));
        }
        return;
      }
      if(t === 'agent_loop_v6.step_question_resolved'){
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">🙋 Step input ${ev.answered?'received':'skipped'}</span>
          <span class="alo-cycle-status">${_esc(ev.decision||'')}</span>
        </div>`, ev.answered?'done':'expand');
        return;
      }
      // ── V7 clarify mode = auto-raise: plan harder instead of asking ──
      if(t === 'agent_loop_v6.clarify_auto_raise'){
        const qs = (ev.questions||[]).map(q=>`<div style="margin:2px 0">• ${_esc(q)}</div>`).join('');
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">⤴ Auto-raise (uncertain — planning harder)</span>
          <span class="alo-cycle-status">${_esc(String((ev.questions||[]).length))} open question(s)</span>
        </div>${qs?`<div class="alo-cycle-preview">${qs}</div>`:''}`, 'plan');
        return;
      }
      // ── V7 clarify mode = auto-accept: assume answers and proceed ──
      if(t === 'agent_loop_v6.clarify_auto_accept'){
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">✔ Auto-accepted assumptions</span>
          <span class="alo-cycle-status">proceeding</span>
        </div>${ev.assumptions?`<div class="alo-cycle-preview" style="white-space:pre-wrap">${_esc(String(ev.assumptions))}</div>`:''}`, 'done');
        return;
      }
      // ── V7 pre-step info gathering: gaps to fill before this step ──
      if(t === 'agent_loop_v6.prestep_info'){
        const gaps = (ev.gaps||[]).map(g=>`<div style="margin:1px 0">• ${_esc(g)}</div>`).join('');
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">🔎 Gather first · step ${_esc(String(ev.step_id||'?'))}</span>
          <span class="alo-cycle-status">${_esc(String((ev.gaps||[]).length))} missing</span>
        </div><div class="alo-cycle-preview">${gaps}</div>`, 'expand');
        return;
      }
      // ── V7 multi-day escalation snapshot: notes + artifacts + thought loop ──
      if(t === 'agent_loop_v6.strategic_escalated'){
        const bits = [];
        if(ev.notes) bits.push('agent notes');
        if(ev.artifacts) bits.push('artifact snapshot');
        if(ev.thought_loop) bits.push('thought loop');
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">📌 Handed to Dream · multi-day</span>
          <span class="alo-cycle-status">${_esc(ev.slug||'')}</span>
        </div><div class="alo-cycle-thought" style="font-style:italic">froze ${_esc(bits.join(' · ')||'escalation state')}; track it in 🎯 Goals</div>`, 'plan');
        return;
      }
      // ── V7 progress report sent out over comms ──
      if(t === 'agent_loop_v6.progress_report'){
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">📊 Progress reported · ${_esc(ev.channel||'')}</span>
          <span class="alo-cycle-status">${_esc(ev.tier||'')}</span>
        </div>`, 'done');
        return;
      }

      // ── v6/V7: a strategic (multi-day) goal persisted as a dream project ──
      if(t === 'agent_loop_v6.strategic_persisted'){
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">🌙 Strategic → dream project</span>
          <span class="alo-cycle-status">${_esc(ev.slug||'')}</span>
        </div><div class="alo-cycle-thought" style="font-style:italic">${_esc(ev.note||'documented plan persisted; the dream system will continue it over days')}</div>`, 'plan');
        return;
      }

      // ── v6/V7: this session's progress folded back into the dream project ──
      if(t === 'agent_loop_v6.strategic_progress'){
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">🌙 Progress saved to dream project</span>
          <span class="alo-cycle-status">${_esc(ev.slug||'')} · ${_esc(String(ev.steps_run||0))} steps</span>
        </div><div class="alo-cycle-thought" style="font-style:italic">the next dream cycle continues the remaining work</div>`, 'done');
        return;
      }

      // ── v6/V7: a step's distilled, relevant-only finalised output ────
      if(t === 'agent_loop_v6.step_finalized'){
        const card = this._sr.querySelector(`.alo-cycle.step[data-step="${ev.step_id}"]`);
        if(card && ev.summary){
          let body = card.querySelector('.alo-step-summary');
          if(!body){ body = document.createElement('div'); body.className='alo-cycle-preview alo-step-summary'; card.appendChild(body); }
          // Tuck the raw step output (already shown by step_done) behind a reveal.
          const prevRaw = body.getAttribute('data-raw') || (body.classList.contains('alo-finalised') ? '' : (body.textContent||''));
          body.classList.add('alo-finalised');
          body.setAttribute('data-raw', prevRaw);
          body.innerHTML = `<div style="color:var(--acc2,#a8c87a);font-size:9px;margin-bottom:2px">✦ finalised · relevant-only</div>`
            + `<div class="alo-output-body">${_smartRender(ev.summary)}</div>`
            + (prevRaw ? `<details style="margin-top:3px"><summary style="cursor:pointer;font-size:9px;color:var(--dim,#a89f92)">show raw step output</summary><div class="alo-output-body" style="margin-top:3px">${_smartRender(prevRaw)}</div></details>` : '');
        }
        return;
      }

      // ── v6/V7: extra_step failure recovery — the failed step is ADJUSTED to
      //    navigate the problem the verifier found (new tactic / caps / phases),
      //    seeded with its own output, and inserted to run next. ────────────────
      if(t === 'agent_loop_v6.recovery_step'){
        const s = ev.step || {};
        const adjusted = !!ev.adjusted;
        const caps = (ev.caps||[]);
        const phases = (ev.phases||[]);
        const label = adjusted ? '⟳ Adjust' : '↻ Recover';
        const meta = [];
        if(caps.length) meta.push(`caps: ${caps.map(_esc).join(', ')}`);
        if(phases.length) meta.push(`phases: ${phases.map(_esc).join('→')}`);
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">${label} · step ${_esc(String(s.id||'?'))}</span>
          <span class="alo-cycle-status" style="color:var(--warn,#c7a15a)">${adjusted?'new tactic — ':''}from step ${_esc(String(ev.from_step||'?'))}</span>
        </div>`
        + `<div class="alo-cycle-preview">${_esc(s.title||'')}`
        + (meta.length ? `<div style="margin-top:2px;color:var(--dim,#a89f92);font-size:9px">${_esc(meta.join(' · '))}</div>` : '')
        + (ev.reason ? `<div style="margin-top:2px;color:var(--dim,#a89f92);font-style:italic">${_esc(ev.reason)}</div>` : '')
        + `</div>`, 'plan');
        return;
      }

      // ── v6/V7: a step's structured JOURNAL record — the goal-relevant outputs,
      //    files/paths, tools and entities distilled for reuse (persisted to the
      //    data fabric + a journal.json mirror). Complex/long-term runs only. ────
      if(t === 'agent_loop_v6.journal'){
        const e = ev.entry || {};
        const rows = [];
        (e.key_outputs||[]).slice(0,6).forEach(k => rows.push(`<div style="margin:1px 0">• ${_esc(k)}</div>`));
        if((e.files||[]).length) rows.push(`<div style="margin:1px 0;color:var(--acc2,#a8c87a)">📄 ${(e.files||[]).slice(0,6).map(_esc).join(', ')}</div>`);
        if((e.entities||[]).length) rows.push(`<div style="margin:1px 0;color:var(--dim,#a89f92)">◇ ${(e.entities||[]).slice(0,8).map(_esc).join(', ')}</div>`);
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">🗒 Journal · step ${_esc(String(ev.step_id||'?'))}</span>
          <span class="alo-cycle-status">${_esc(String(ev.count||0))} entr${(ev.count===1)?'y':'ies'} → fabric</span>
        </div>${rows.length?`<div class="alo-cycle-preview">${rows.join('')}</div>`:''}`, 'done');
        return;
      }

      // ── Mid-run USER MESSAGE — a message the user sent into a running loop is
      //    queued (agent_loop.user_message, pending) then folded into context at
      //    the next step boundary (agent_loop_v5/v6.user_message). ─────────────
      if(t.endsWith('.user_message')){
        const msgs = (ev.messages && ev.messages.length) ? ev.messages
                     : (ev.text ? [ev.text] : []);
        const rows = msgs.map(m => `<div style="margin:1px 0">💬 ${_esc(m)}</div>`).join('');
        const pending = !!ev.pending;
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">✉ User update${pending ? ' (queued)' : ''}</span>
          <span class="alo-cycle-status" style="color:var(--acc,#c7a15a)">${pending ? 'waiting for next step' : 'folded into context'}</span>
        </div>${rows ? `<div class="alo-cycle-preview">${rows}</div>` : ''}`, 'plan');
        return;
      }

      // ── v6/V7 git-tree: a failed step forks an alternate-approach branch ──
      if(t === 'agent_loop_v6.branch_open'){
        const rows = (ev.steps||[]).map(s =>
          `<div style="font-family:var(--mono,monospace);font-size:9.5px;color:var(--text2,#bfb6a8);margin:1px 0">`
          + `<b>${_esc(String(s.id))}.</b> ${_esc(s.title||'')}`
          + ((s.caps&&s.caps.length)?`<span style="color:var(--dim,#a89f92);font-size:9px"> — ${(s.caps||[]).map(_esc).join(', ')}</span>`:'')
          + `</div>`).join('');
        const el = this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">⑃ Branch · ${_esc(ev.label||'')}</span>
          <span class="alo-cycle-status"><span class="alo-spinner"></span> from step ${_esc(String(ev.fork_step||'?'))}</span>
        </div><div class="alo-cycle-preview">${rows||''}</div>`, 'branch');
        if(el){
          el.style.marginLeft = '16px';
          el.style.borderLeft = '2px solid var(--warn,#c7a15a)';
          this._branchCards = this._branchCards || {};
          this._branchCards[`${ev.fork_step}::${ev.label}`] = el;
        }
        return;
      }

      // ── v6/V7 git-tree: a branch met the bar and MERGES back ─────────
      if(t === 'agent_loop_v6.branch_merge'){
        const el = (this._branchCards||{})[`${ev.fork_step}::${ev.label}`];
        if(el){
          el.classList.add('done');
          el.style.borderLeft = '2px solid var(--ok,#5a9e8f)';
          const st = el.querySelector('.alo-cycle-status');
          if(st) st.innerHTML = `<span style="color:var(--ok,#5a9e8f)">⇄ merged ✓</span>`;
          if(ev.summary){
            const b = document.createElement('div');
            b.className = 'alo-cycle-preview'; b.style.marginTop = '3px';
            b.textContent = ev.summary;
            el.appendChild(b);
          }
        }
        return;
      }

      // ── v6/V7 git-tree: a branch failed and is PRUNED to a reason ────
      if(t === 'agent_loop_v6.branch_prune'){
        const el = (this._branchCards||{})[`${ev.fork_step}::${ev.label}`];
        if(el){
          el.classList.add('warn');
          el.style.borderLeft = '2px solid var(--dim,#a89f92)';
          el.style.opacity = '0.68';
          const st = el.querySelector('.alo-cycle-status');
          if(st) st.innerHTML = `<span style="color:var(--dim,#a89f92)">✕ pruned</span>`;
          const tool = el.querySelector('.alo-cycle-tool');
          if(tool) tool.style.textDecoration = 'line-through';
          if(ev.reason){
            const b = document.createElement('div');
            b.className = 'alo-cycle-thought'; b.style.fontStyle = 'italic';
            b.textContent = 'pruned: ' + ev.reason;
            el.appendChild(b);
          }
        }
        return;
      }

      // ── v5-only: a specialist sub-agent step finishes ────────────────
      if(t === 'agent_loop_v5.step_done'){
        const card = this._sr.querySelector(`.alo-cycle.step[data-step="${ev.step_id}"]`);
        if(card){
          card.classList.add(ev.ok ? 'done' : 'error');
          const status = card.querySelector('.alo-cycle-status');
          if(status) status.innerHTML = ev.ok
            ? `<span style="color:var(--ok,#5a9e8f)">✓ done</span>`
            : `<span style="color:var(--err,#c75a5a)">✗ incomplete</span>`;
          if(ev.summary){
            let body = card.querySelector('.alo-step-summary');
            if(!body){ body = document.createElement('div'); body.className='alo-cycle-preview alo-step-summary'; card.appendChild(body); }
            // step_finalized may later replace this with a distilled version; keep
            // the raw output recoverable via data-raw so its reveal still works.
            if(!body.classList.contains('alo-finalised')){
              body.innerHTML = _smartRender(ev.summary);
              body.setAttribute('data-raw', ev.summary);
            }
          }
        }
        return;
      }

      // ── v5-only: LIVE reasoning stream (token-by-token while the turn's JSON
      //    action is still generating). A transient per-turn card fills in live,
      //    then is dropped by think_stream_end once the turn is parsed into its
      //    real cycle card (tool_call / thinking). Keyed by step:turn. ──
      if(t === 'agent_loop_v5.think_delta'){
        if(!this._showThinking) return;
        const key = `${ev.step_id}:${ev.turn}`;
        let card = this._sr.querySelector(`.alo-cycle.think-stream[data-ts="${key}"]`);
        if(!card){
          card = this._cycleEl(`<div class="alo-cycle-h">
              <span class="alo-cycle-tool">💭 reasoning…</span>
              <span class="alo-cycle-status"><span class="alo-spinner"></span></span>
            </div><div class="alo-think-join"></div>`, 'think-stream');
          if(card) card.setAttribute('data-ts', key);
        }
        const body = card && card.querySelector('.alo-think-join');
        if(body) body.textContent = ev.text || '';
        return;
      }
      if(t === 'agent_loop_v5.think_stream_end'){
        const key = `${ev.step_id}:${ev.turn}`;
        const card = this._sr.querySelector(`.alo-cycle.think-stream[data-ts="${key}"]`);
        if(card) card.remove();
        return;
      }

      // ── v5-only: thought-only turns, joined into ONE reasoning card ──
      // A specialist that reasons without acting is NOT an error and does not
      // consume a real cycle. Each thinking streak reuses one card (keyed by
      // its cycle id) and shows the accumulated reasoning.
      if(t === 'agent_loop_v5.thinking'){
        if(!this._showThinking) return;
        let card = this._sr.querySelector(`.alo-cycle.thinking[data-think-cycle="${ev.cycle}"]`);
        if(!card){
          card = this._cycleEl(`<div class="alo-cycle-h">
              <span class="alo-cycle-tool">💭 reasoning</span>
              <span class="alo-cycle-status" style="color:var(--dim,#a89f92)">no action — thinking</span>
            </div><div class="alo-think-join"></div>`, 'thinking');
          if(card) card.setAttribute('data-think-cycle', String(ev.cycle));
        }
        const body = card && card.querySelector('.alo-think-join');
        if(body) body.textContent = ev.thought || '';
        return;
      }

      // ── v5-only: orchestrator re-planned the remaining steps ─────────
      if(t === 'agent_loop_v5.replan'){
        const rem = (ev.remaining||[]).map(s=>`${s.id}. ${_esc(s.title||'')}`).join(' › ') || '(none)';
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">↻ Re-planned remaining work</span>
          <span class="alo-cycle-status">after step ${ev.after_step||'?'}</span>
        </div><div class="alo-cycle-preview">${rem}</div>
        ${ev.reason?`<div class="alo-cycle-thought" style="font-style:italic">${_esc(ev.reason)}</div>`:''}`, 'warn');
        return;
      }

      // ── v5-only: pre-plan READ-ONLY recon (start → done, runs sequentially) ──
      if(t === 'agent_loop_v5.recon'){
        if(ev.phase === 'done'){
          const open = this._sr.querySelectorAll('.alo-cycle.recon:not(.recon-done)');
          const card = open[open.length - 1];
          if(card){
            card.classList.add('recon-done', ev.ok ? 'done' : 'error');
            const status = card.querySelector('.alo-cycle-status');
            if(status) status.innerHTML = ev.ok
              ? `<span style="color:var(--ok,#5a9e8f)">✓</span>`
              : `<span style="color:var(--err,#c75a5a)">✗</span>`;
            if(ev.preview){
              const p = document.createElement('div');
              p.className = 'alo-cycle-preview'; p.textContent = ev.preview;
              card.appendChild(p);
            }
          }
          return;
        }
        const rnd = (ev.round && ev.max_rounds && ev.max_rounds > 1)
          ? ` <span style="opacity:.6">· round ${ev.round}/${ev.max_rounds}</span>` : '';
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">🔍 recon · ${_esc(ev.cap||'')}${rnd}</span>
          <span class="alo-cycle-status"><span class="alo-spinner"></span> gathering…</span>
        </div>${ev.why?`<div class="alo-cycle-thought">${_esc(ev.why)}</div>`:''}`, 'recon');
        return;
      }

      // ── v5+: a recoverable malformed call — retry the SAME cap with fixed
      //    args instead of widening scope (keeps the prior step's work). ──
      if(t === 'agent_loop_v5.arg_correction'){
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">↺ Retry ${_esc(String(ev.tool||''))} with corrected args${ev.step_id?` · step ${_esc(String(ev.step_id))}`:''}</span>
          <span class="alo-cycle-status">no widen</span>
        </div>${ev.reason?`<div class="alo-cycle-preview" style="color:var(--dim,#a89f92)">${_esc(ev.reason)}</div>`:''}`, 'expand');
        return;
      }

      // ── v5-only: a step's capability scope was widened (request or auto) ──
      if(t === 'agent_loop_v5.scope_widened'){
        const added = (ev.added||[]).map(_esc).join(', ');
        const n = (ev.added||[]).length;
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">⊕ Scope widened${ev.step_id?` · step ${_esc(String(ev.step_id))}`:''}</span>
          <span class="alo-cycle-status">${n} cap${n===1?'':'s'}</span>
        </div><div class="alo-cycle-preview">${added}${ev.reason?` <span style="color:var(--dim,#a89f92)">(${_esc(ev.reason)})</span>`:''}</div>`, 'expand');
        return;
      }

      // ── v5-only: a `complex` step expanded into its own sub-plan ─────
      if(t === 'agent_loop_v5.subplan'){
        const steps = ev.steps||[];
        const rows = steps.map(s => {
          const caps = (s.caps||[]).map(_esc).join(', ');
          return `<div style="font-family:var(--mono,monospace);font-size:10px;color:var(--text2,#bfb6a8);margin:2px 0">`
            + `<b>${_esc(String(s.id))}.</b> ${_esc(s.title||'')}`
            + (caps?`<div style="color:var(--dim,#a89f92);font-size:9px;margin-left:12px">caps: ${caps}</div>`:'')
            + `</div>`;
        }).join('');
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">▤ Sub-plan · step ${_esc(String(ev.parent_id||'?'))} — ${_esc(ev.title||'')}</span>
          <span class="alo-cycle-status">${steps.length} sub-step${steps.length===1?'':'s'}</span>
        </div>${ev.reason?`<div class="alo-cycle-thought" style="font-style:italic">${_esc(ev.reason)}</div>`:''}<div class="alo-cycle-preview">${rows||'(empty)'}</div>`, 'plan');
        return;
      }

      // ── v5-only: generated code auto-saved & versioned to the code store ──
      if(t === 'agent_loop_v5.code_saved'){
        const files = ev.files||[];
        const rows = files.map(f => {
          const g = f.gitea ? ` <a href="${_esc(f.gitea)}" target="_blank" style="color:var(--acc,#5a9e8f)">gitea↗</a>` : '';
          const u = f.unchanged ? ` <span style="color:var(--dim,#a89f92)">(unchanged)</span>` : '';
          return `<div style="font-family:var(--mono,monospace);font-size:10px;color:var(--text2,#bfb6a8);margin:1px 0">`
            + `💾 ${_esc(f.path||'')} <b>v${_esc(String(f.version||'?'))}</b> `
            + `<span style="color:var(--dim,#a89f92)">${f.bytes||0}b ${_esc(f.lang||'')}</span>${u}${g}</div>`;
        }).join('');
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">💾 Saved &amp; versioned</span>
          <span class="alo-cycle-status">${files.length} file${files.length===1?'':'s'}</span>
        </div><div class="alo-cycle-preview">${rows||'(none)'}</div>`, 'expand');
        return;
      }

      // ── generated document written to the run's working directory ──────────
      // The loop persists a generative cap's output itself, so the step never
      // needs a second write call. Surfaced as its own card: the file IS the
      // deliverable, and the path is what later steps refer to.
      if(t === 'agent_loop_v5.output_saved'){
        const rel = ev.rel || ev.path || '';
        const chars = Number(ev.chars||0).toLocaleString();
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">📄 Output saved</span>
          <span class="alo-cycle-status">${_esc(String(ev.tool||''))} · ${chars} chars</span>
        </div><div class="alo-cycle-preview" style="font-family:var(--mono,monospace);font-size:10px">`
          + `./${_esc(rel)}</div>`, 'expand');
        return;
      }

      // handover synthesis
      if(t === 'agent_loop.handover_start'){
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">★ Handover synthesis</span>
          <span class="alo-cycle-status" style="color:var(--acc,#5a9e8f)"><span class="alo-spinner"></span> writing answer from ${ev.history_len||0} steps…</span>
        </div>
        <div class="alo-cycle-thought" style="font-style:italic;color:var(--dim,#a89f92)">A separate LLM is reviewing all tool results to produce a polished final answer.</div>
        <div class="alo-handover-stream" data-handover="1"></div>`, 'handover');
        return;
      }
      if(t === 'agent_loop.handover_done'){
        const cards = this._sr.querySelectorAll('.alo-cycle.handover');
        if(cards.length){
          const card = cards[cards.length - 1];
          const status = card.querySelector('.alo-cycle-status');
          if(status) status.innerHTML = `<span style="color:var(--ok,#5a9e8f)">✓ ${ev.length||0} chars synthesised</span>`;
          const stream = card.querySelector('[data-handover="1"]');
          if(stream){
            stream.classList.remove('alo-handover-stream');
            stream.classList.add('alo-handover-body');
            stream.innerHTML = _renderMarkdown(ev.output||'');
          }
        }
        return;
      }
      if(t === 'agent_loop.handover_error'){
        const cards = this._sr.querySelectorAll('.alo-cycle.handover');
        if(cards.length){
          const card = cards[cards.length - 1];
          card.classList.add('error');
          const status = card.querySelector('.alo-cycle-status');
          if(status) status.innerHTML = `<span style="color:var(--err,#c75a5a)">✗ failed</span>`;
          const stream = card.querySelector('[data-handover="1"]');
          if(stream) stream.innerHTML = `<div style="color:var(--err,#c75a5a);font-family:var(--mono,monospace);font-size:10px">${_esc(ev.error||'unknown error')}</div>`;
        }
        return;
      }

      // triage_done (any variant)
      if(t.endsWith('.triage_done')){
        this._showTriage(ev.triage);
        return;
      }
      // toolkit (any variant)
      if(t.endsWith('.toolkit')){
        this._showToolkit(ev.toolkit);
        if(ev.added && ev.added.length){
          this._cycleEl(`<div class="alo-cycle-h">
            <span class="alo-cycle-tool">+ Toolkit expanded</span>
            <span class="alo-cycle-status">${ev.added.length} cap${ev.added.length===1?'':'s'} added</span>
          </div><div class="alo-cycle-preview">${ev.added.map(_esc).join(', ')}</div>`, 'expand');
        }
        return;
      }

      // cycle_planning (any variant)
      if(t.endsWith('.cycle_planning')){
        // Reset stale buffers on prior cycle refs
        this._cycleRefs.forEach((ref) => {
          if(ref){ ref.tokenBuffer=''; ref._inThinkBlock=false; ref._thinkBuffer=''; ref._thinkFromEvent=false; }
        });
        const el = this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-n">cycle ${ev.cycle}</span>
          <span class="alo-cycle-tool">planning…</span>
        </div>`);
        this._cycleRefs.set(ev.cycle, {el, progressEl:null, tokenBuffer:''});
        this.dispatchEvent(new CustomEvent('alo:cycle-start', {detail:{cycle:ev.cycle}, bubbles:true}));
        return;
      }

      // phase transition (any variant) — badge on the current cycle
      if(t.endsWith('.phase')){
        this._showPhase(ev);
        return;
      }

      // budget pause — continue / wrap card (any variant)
      if(t.endsWith('.budget_pause')){
        this._showBudgetPause(ev);
        return;
      }
      if(t.endsWith('.budget_continue')){
        // Resolve any open budget card and note the extension.
        const card = this._sr.querySelector('.alo-budget-pause:not(.resolved)');
        if(card){
          if(card._tick) clearInterval(card._tick);
          card.classList.add('resolved');
          const actions = card.querySelector('.alo-budget-actions');
          if(actions) actions.innerHTML =
            `<span style="font-size:10.5px;color:var(--ok,#5a9e8f);font-style:italic">continued ${ev.mode==='auto'?'(auto)':''} → budget now ${ev.max_cycles}</span>`;
          card.style.opacity = '0.7';
        }
        return;
      }

      // HITL request — pause card (any variant)
      if(t.endsWith('.hitl_request')){
        if(this._hitlPending.has(ev.step)) return;
        this._hitlPending.add(ev.step);
        this._showHitlPause(ev);
        this.dispatchEvent(new CustomEvent('alo:hitl-request', {detail:{cycle:ev.cycle, step:ev.step, tool:ev.tool, reason:ev.reason||''}, bubbles:true}));
        return;
      }
      if(t.endsWith('.hitl_resolved')){
        this._hitlPending.delete((ev.cycle||0) - 1);
        return;
      }

      // tool_call (any variant)
      if(t.endsWith('.tool_call')){
        this._renderToolCall(ev);
        return;
      }

      // tool_stream_delta — live text for a long-running LLM-backed cap
      // (code.author, llm.generate, ...) while it's still generating, same
      // idea as think_delta but scoped to the actual tool-call card (keyed by
      // cycle, same as tool_call/tool_done) instead of a transient reasoning
      // card. Removed by tool_stream_end once the real result replaces it.
      if(t.endsWith('.tool_stream_delta')){
        if(!this._showThinking) return;
        const ref = this._cycleRefs.get(ev.cycle);
        if(!ref || !ref.el) return;
        let wrap = ref.el.querySelector('.alo-cycle-stream');
        if(!wrap){
          wrap = document.createElement('div');
          wrap.className = 'alo-cycle-stream';
          wrap.innerHTML = `<div class="alo-cycle-status"><span class="alo-spinner"></span> generating…</div>
            <div class="alo-cycle-preview alo-stream-text"></div>`;
          ref.el.appendChild(wrap);
        }
        const txt = wrap.querySelector('.alo-stream-text');
        if(txt) txt.textContent = ev.text || '';
        return;
      }
      if(t.endsWith('.tool_stream_end')){
        const ref = this._cycleRefs.get(ev.cycle);
        const wrap = ref && ref.el && ref.el.querySelector('.alo-cycle-stream');
        if(wrap) wrap.remove();
        return;
      }

      // tool_done (any variant)
      if(t.endsWith('.tool_done')){
        this._renderToolDone(ev);
        return;
      }

      // workshop.tool_invoked / tool_finished — supplemental for v1/v2 cycles
      if(t === 'workshop.tool_invoked'){
        if(this._cycleRefs.size === 0) return;
        const last = Array.from(this._cycleRefs.values()).pop();
        if(!last || !last.el) return;
        if(last.el.querySelector('.alo-cycle-args')) return;
        const argsHtml = ev.args ? _fmtArgs(ev.args, 300) : '';
        if(argsHtml){
          const argsEl = document.createElement('div');
          argsEl.className = 'alo-cycle-args';
          argsEl.innerHTML = argsHtml;
          last.el.appendChild(argsEl);
        }
        return;
      }
      if(t === 'workshop.tool_finished'){
        if(this._cycleRefs.size === 0) return;
        const last = Array.from(this._cycleRefs.values()).pop();
        if(!last || !last.el) return;
        if(last.el.querySelector('.alo-cycle-result')) return;
        if(!ev.preview && !ev.error && !ev.empty_search) return;
        const ok = ev.ok !== false;
        const cls = ev.empty_search ? 'empty' : (ok ? 'ok' : 'err');
        const label = ev.empty_search ? 'no results' : (ok ? 'result' : 'error');
        const text = ev.empty_search
          ? 'Search returned 0 results — change the query or stop searching.'
          : (ok ? (ev.preview || '') : (ev.error || ev.preview || ''));
        const body = document.createElement('div');
        body.className = 'alo-cycle-result';
        const formatted = ok ? _smartRender(text) : _esc(text||'(empty)');
        body.innerHTML = `<div class="alo-result-h ${cls}">${label}</div>
          <div class="alo-result-render ${cls}">${formatted}</div>`;
        last.el.appendChild(body);
        return;
      }

      // Long-running await skipped
      if(t === 'agent_loop.long_running_await_skipped'){
        if(this._cycleRefs.size === 0) return;
        const last = Array.from(this._cycleRefs.values()).pop();
        if(!last || !last.el) return;
        const note = document.createElement('div');
        note.className = 'alo-progress-row';
        note.innerHTML = `<span class="alo-progress-tag" style="background:#3a2a10;color:#ffb74d">await skipped</span>
          <span>no job_id returned by <code>${_esc(ev.tool||'')}</code> — likely an arg error. Keys present: ${_esc((ev.result_keys||[]).join(', ')||'(none)')}</span>`;
        last.el.appendChild(note);
        return;
      }

      // done (any variant) — no longer renders a separate "Done" summary card;
      // the final pane already shows the summary, cycle count and stats, so the
      // extra card was redundant. The alo:done event is still dispatched.
      // Per-step context fill: step_context carries the EXACT system prompt the
      // ephemeral specialist was given, so this is the real number, not a guess.
      if(t.endsWith('.step_context')){
        const sp = (ev.system_prompt||'').length
                 + ((ev.parts&&ev.parts.inline_files)||'').length;
        if(sp) this._updCtx(sp, ev.num_ctx || this._ctxWindow);
      }
      // Spill telemetry from the router (ollama.cpu_spill / request_done).
      if(t === 'ollama.cpu_spill'){ this._markSpill(true, ev.resident_pct); }
      else if(t === 'ollama.request_done' && ev.gpu_resident_pct != null){
        this._markSpill(!!ev.cpu_spill, ev.gpu_resident_pct);
        if(ev.num_ctx) this._ctxWindow = ev.num_ctx;
      }

      if(t.endsWith('.done')){
        // List whatever the run actually left on disk. Deliberately fire-and-
        // forget: a failed/slow listing must never hold up the done event.
        this._renderFilesCard().catch(()=>{});
        this.dispatchEvent(new CustomEvent('alo:done', {detail:{summary:ev.summary||'', cycles:ev.cycles, ok:!ev.error}, bubbles:true}));
        return;
      }

      // Repetition block
      if(t.endsWith('.repetition_block')){
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">Repetition blocked</span>
          <span class="alo-cycle-status" style="color:var(--warn,#c9a45a)">cycle ${ev.cycle}</span>
        </div>
        <div class="alo-cycle-thought">Forced loop break — agent was about to call <code>${_esc(ev.tool||'')}</code> again with identical args.</div>`, 'warn');
        return;
      }

      // Args coerced
      if(t.endsWith('.args_coerced')){
        const ref = this._cycleRefs.get(ev.cycle);
        if(!ref) return;
        let host = ref.el.querySelector('.alo-coerce');
        if(!host){
          host = document.createElement('div');
          host.className = 'alo-coerce';
          ref.el.appendChild(host);
        }
        const notes = (ev.notes || []).slice(0, 6);
        host.innerHTML = `<span class="alo-progress-tag" style="background:#2a3a1d;color:#aede7e">auto-fix</span>
          <span style="font-size:9.5px;color:var(--text2,#bfb6a8)">${_esc(ev.tool||'')}: ${notes.map(_esc).join(' · ')}${ev.notes && ev.notes.length>6?' · …':''}</span>`;
        return;
      }

      // Long-running await events
      if(t === 'agent_loop.long_running_await_start'){
        this._renderAwaitStart(ev);
        return;
      }
      if(t === 'agent_loop.long_running_await_tick'){
        this._renderAwaitTick(ev);
        return;
      }
      if(t === 'agent_loop.long_running_await_done'){
        this._renderAwaitDone(ev);
        return;
      }
      if(t === 'agent_loop.long_running_await_timeout'){
        this._renderAwaitTimeout(ev);
        return;
      }

      // Research stream events (top-level)
      if(t === 'agent_loop.research_stream_hint'){ this._renderResearchHint(ev); return; }
      if(t === 'agent_loop.research_stream_open'){ this._renderResearchOpen(ev); return; }
      if(t === 'agent_loop.research_step'){ this._renderResearchStep(ev); return; }
      if(t === 'agent_loop.research_thinking'){ this._renderResearchThinking(ev); return; }
      if(t === 'agent_loop.research_citations'){ this._renderResearchCitations(ev); return; }
      if(t === 'agent_loop.research_file'){ this._renderResearchFile(ev); return; }
      if(t === 'agent_loop.research_stream_done'){ this._renderResearchStreamDone(ev); return; }
      if(t === 'agent_loop.research_stream_failed'){ this._renderResearchStreamFailed(ev); return; }

      // Error recovery
      if(t === 'agent_loop.error_recovery_start'){ this._renderRecoveryStart(ev); return; }
      if(t === 'agent_loop.error_recovery_attempt'){ this._renderRecoveryAttempt(ev); return; }
      if(t === 'agent_loop.error_recovery_done'){ this._renderRecoveryDone(ev); return; }

      // tool_progress — generic long-running tool live updates
      if(t === 'tool_progress'){
        this._addProgress(ev);
        return;
      }

      // unprefixed research/exec/stream — append to most recent cycle
      if(/^(research|exec|ml_training|stream)\./.test(t)){
        const lastCycle = Array.from(this._cycleRefs.keys()).pop();
        if(lastCycle != null) this._addProgress({cycle:lastCycle, raw_type:t, data:ev});
        return;
      }

      // Final structured result
      if(t === 'result'){
        this._lastResult = ev;
        if(ev.toolkit) this._showToolkit(ev.toolkit);
        if(ev.triage)  this._showTriage(ev.triage);
        this._renderFinalPane(ev);
        this.dispatchEvent(new CustomEvent('alo:final', {detail:{payload:ev}, bubbles:true}));
        return;
      }
    }

    // ───────────────────── Render helpers ────────────────────────────
    // ── Per-step context meter + GPU-spill badge ─────────────────────────────
    // The chat header's context meter tracks the CHAT conversation only — it is
    // fed from the chat stream's `done` event, which a loop never emits, so it
    // sits frozen while a loop runs. A loop also isn't ONE context: every step is
    // a fresh ephemeral specialist with its own window. So the meaningful number
    // is per step — how full THIS step's prompt is against the model's window —
    // and it belongs pinned to the loop, not the chat header.
    _ctxBar(){
      let el = this._sr.querySelector('.alo-ctxbar');
      if(!el){
        el = document.createElement('div');
        el.className = 'alo-ctxbar';
        el.innerHTML = `<span class="alo-ctx-lbl">context</span>
          <span class="alo-ctx-track"><span class="alo-ctx-fill"></span></span>
          <span class="alo-ctx-txt">—</span>
          <span class="alo-ctx-spill" hidden title="The model is only partly resident in VRAM — generation is running partly on CPU and is roughly 3x slower.">⚠ GPU→CPU</span>`;
        (this._sr.querySelector('.alo-cycles-card') || this._sr).appendChild(el);
      }
      return el;
    }
    // Chars→tokens: matches the router's estimate (deliberately low so the bar
    // reads slightly pessimistic rather than falsely comfortable).
    _updCtx(promptChars, windowTokens){
      const bar = this._ctxBar();
      const used = Math.round((promptChars||0) / 3.4);
      const max  = windowTokens || this._ctxWindow || 0;
      if(!max){ return; }
      this._ctxWindow = max;
      const pct = Math.min(100, Math.round(used/max*100));
      const fill = bar.querySelector('.alo-ctx-fill');
      fill.style.width = pct + '%';
      fill.style.background = pct>85 ? 'var(--err,#c96a5a)'
                            : pct>65 ? 'var(--warn,#c9a45a)' : 'var(--acc2,#a8c87a)';
      bar.querySelector('.alo-ctx-txt').textContent =
        `${(used/1000).toFixed(1)}k / ${(max/1000).toFixed(0)}k`;
    }
    _markSpill(on, pct){
      const b = this._ctxBar().querySelector('.alo-ctx-spill');
      if(!b) return;
      b.hidden = !on;
      if(on && pct != null) b.textContent = `⚠ GPU→CPU ${pct}%`;
    }

    // ── Files produced by the run ────────────────────────────────────────────
    // A loop writes its real deliverables into its session sandbox, which the
    // chat UI cannot reach — so a generated page/report/script was effectively
    // write-only: the cards could NAME a file but not open it. This lists the
    // run's working directory and gives each file a preview (rendered) and a
    // source view, plus a download, the way the artifact browser does.
    static _fmtSize(n){
      if(n == null || n < 0) return '';
      if(n < 1024) return n + ' B';
      if(n < 1024*1024) return (n/1024).toFixed(1) + ' KB';
      return (n/1048576).toFixed(1) + ' MB';
    }
    _fileUrl(name){
      return '/exec/artifacts/download?session_id=' + encodeURIComponent(this._sessionId||'')
           + '&rel=' + encodeURIComponent(name);
    }
    async _renderFilesCard(){
      if(!this._sessionId) return;
      let data;
      try{
        const r = await fetch('/exec/artifacts/list?session_id='
                              + encodeURIComponent(this._sessionId));
        data = await r.json();
      }catch(e){ return; }
      const files = (data && data.files || []).filter(f => !f.is_dir);
      if(!files.length) return;
      // Reuse one card across refreshes so a re-render doesn't stack copies.
      let card = this._filesCard;
      if(!card || !card.isConnected){
        card = this._cycleEl('', 'files');
        this._filesCard = card;
      }
      const rows = files.map(f => `
        <div class="alo-file-row" data-file="${_esc(f.name)}">
          <span class="alo-file-name">${_esc(f.name)}</span>
          <span class="alo-file-size">${_esc(VeraAgentLoopOutput._fmtSize(f.size))}</span>
          <button class="alo-file-btn" data-act="preview">preview</button>
          <button class="alo-file-btn" data-act="source">source</button>
          <a class="alo-file-btn" href="${this._fileUrl(f.name)}" download>download</a>
        </div>`).join('');
      card.innerHTML = `<div class="alo-cycle-h">
          <span class="alo-cycle-tool">📁 Files produced</span>
          <span class="alo-cycle-status">${files.length} file${files.length===1?'':'s'}</span>
          <button class="alo-card-expand" data-act="files-refresh" title="Refresh">⟳</button>
        </div>
        <div class="alo-file-list">${rows}</div>
        <div class="alo-file-view" hidden></div>`;
    }
    async _showFile(name, mode){
      const view = this._filesCard && this._filesCard.querySelector('.alo-file-view');
      if(!view) return;
      const ext = (name.split('.').pop() || '').toLowerCase();
      const url = this._fileUrl(name);
      view.hidden = false;
      view.innerHTML = `<div class="alo-file-vh">${_esc(name)} · ${mode}</div>
                        <div class="alo-file-body">loading…</div>`;
      const body = view.querySelector('.alo-file-body');
      const IMG = ['png','jpg','jpeg','gif','svg','webp'];
      if(mode === 'preview' && IMG.includes(ext)){
        body.innerHTML = `<img src="${url}" style="max-width:100%;border-radius:4px">`;
        return;
      }
      let text = '';
      try{
        const r = await fetch(url);
        text = await r.text();
      }catch(e){ body.textContent = 'could not read: ' + e; return; }
      if(mode === 'preview' && (ext === 'html' || ext === 'htm')){
        // srcdoc + sandbox: render it, but never let a generated page reach the
        // parent document or the network on our origin.
        body.innerHTML = '';
        const fr = document.createElement('iframe');
        fr.setAttribute('sandbox', '');
        fr.style.cssText = 'width:100%;height:420px;border:1px solid var(--border,#3a3530);border-radius:4px;background:#fff';
        fr.srcdoc = text;
        body.appendChild(fr);
        return;
      }
      if(mode === 'preview' && ext === 'md'){
        body.innerHTML = `<pre class="alo-file-src">${_esc(text.slice(0, 20000))}</pre>`;
        return;
      }
      body.innerHTML = `<pre class="alo-file-src">${_esc(text.slice(0, 20000))}</pre>`
        + (text.length > 20000 ? `<div class="alo-file-more">… ${text.length-20000} more chars — use download for the full file</div>` : '');
    }

    _cycleEl(html, cls){
      const host = this._sr.querySelector('.alo-cycles');
      const empty = host.querySelector('.alo-empty');
      if(empty) host.innerHTML = '';
      const d = document.createElement('div');
      d.className = 'alo-cycle' + (cls?' '+cls:'');
      d.innerHTML = html;
      // Timeline marker: a dot + timestamp on the rail running down the
      // right side of .alo-cycles, positioned absolutely so they don't
      // disturb the existing direct-child layout.
      const dot = document.createElement('span');
      dot.className = 'alo-cycle-dot';
      d.appendChild(dot);
      const time = document.createElement('span');
      time.className = 'alo-cycle-time';
      time.textContent = new Date().toLocaleTimeString([], {hour12:false});
      d.appendChild(time);
      host.appendChild(d);
      _follow(host);
      const countEl = this._sr.querySelector('[data-part="cycles-count"]');
      if(countEl){
        const n = host.querySelectorAll('.alo-cycle').length;
        countEl.textContent = n + (n===1?' card':' cards');
      }
      return d;
    }

    // Build the per-step "exact context" reveal: a collapsible holding the
    // verbatim system prompt plus each context layer (caps, skills, prior-step
    // context). Each layer carries data-layer + data-step so the host (loop
    // graph / context layer) can cross-link and pulse the active one.
    _buildCtxReveal(ev){
      const p = ev.parts || {};
      const det = document.createElement('details');
      det.className = 'alo-ctx-reveal';
      det.setAttribute('data-step-ctx', String(ev.step_id));

      const layer = (key, title, text, srcHtml) => {
        if(!text) return '';
        return `<div class="alo-ctx-layer" data-layer="${key}" data-step="${_esc(String(ev.step_id))}">
            <div class="h"><span>${_esc(title)}</span>${srcHtml||''}</div>
            <pre class="alo-ctx-pre">${_esc(text)}</pre>
          </div>`;
      };
      const capsTxt = (p.caps&&p.caps.length) ? p.caps.join('\n') : '';
      const skillsTxt = (p.skills&&p.skills.length) ? p.skills.join('\n') : '';
      const recTxt = (p.recovery_caps&&p.recovery_caps.length) ? p.recovery_caps.join(', ') : '';
      // Prior-step context: tag each source step so the host can link them.
      const srcChips = (p.context_sources||[]).map(s =>
        `<span class="src" data-ctx-src="${_esc(String(s.step_id))}">step ${_esc(String(s.step_id))}</span>`).join(' ');

      det.innerHTML = `<summary>⚙ exact agent context (verbatim)</summary>
        <div class="alo-ctx-body">
          <div class="alo-ctx-layer" data-layer="system_prompt" data-step="${_esc(String(ev.step_id))}">
            <div class="h"><span>Full system prompt</span>
              <button class="alo-ctx-copy" data-copy>copy</button></div>
            <pre class="alo-ctx-pre">${_esc(ev.system_prompt||'')}</pre>
          </div>
          ${layer('caps','Scoped capabilities', capsTxt)}
          ${layer('cap_schemas','Capability schemas', p.cap_schemas)}
          ${layer('skills','Skills', skillsTxt)}
          ${layer('skill_prompt','Skill instructions', p.skill_prompt)}
          ${layer('prior_context','Context from prior steps', p.prior_context, srcChips)}
          ${layer('phase_guide','Phase guidance', p.phase_guide)}
          ${layer('model_block','Model guidance', p.model_block)}
          ${layer('code_note','Code autosave note', p.code_note)}
          ${recTxt ? layer('recovery_caps','Requestable recovery caps', recTxt) : ''}
        </div>`;

      const copyBtn = det.querySelector('[data-copy]');
      if(copyBtn){
        copyBtn.addEventListener('click', (e) => {
          e.preventDefault(); e.stopPropagation();
          try{ navigator.clipboard.writeText(ev.system_prompt||''); copyBtn.textContent='copied'; }
          catch(_){ copyBtn.textContent='select+copy'; }
          setTimeout(()=>{ copyBtn.textContent='copy'; }, 1400);
        });
      }
      return det;
    }

    // Public: the exact context payload(s) the specialist(s) were given.
    getStepContext(stepId){ return (this._stepContext||{})[stepId] || null; }
    getStepContexts(){ return {...(this._stepContext||{})}; }

    // Pulse a step's context layer(s) on/off — used to show the active step is
    // consuming its context (and to visually link the two in the host UI).
    pulseStepContext(stepId, on){
      const det = this._sr.querySelector(`.alo-ctx-reveal[data-step-ctx="${stepId}"]`);
      if(!det) return;
      det.querySelectorAll('.alo-ctx-layer').forEach(l => l.classList.toggle('pulse', !!on));
    }

    _renderStartCard(ev){
      const version = ev.version || '';
      const versionCls = (version === 'v1' || version === 'v2' || version === 'v3') ? version : '';
      const goalLine = ev.goal ? `Goal: ${_esc(ev.goal)}` : '';
      const agentLine = ev.agent_name ? ` · agent: ${_esc(ev.agent_name)}` : '';
      const html = `<div class="alo-cycle-h">
        <span class="alo-cycle-tool">Starting</span>
        ${version ? `<span class="alo-version-pill ${versionCls}">${_esc(version.toUpperCase())}</span>` : ''}
        <span class="alo-cycle-status">…</span>
      </div>${(goalLine || agentLine) ? `<div class="alo-cycle-thought">${goalLine}${agentLine}</div>` : ''}`;
      // The host page may push a synthetic `start` event for instant feedback
      // before the real SSE `start` frame arrives — update the same card in
      // place rather than rendering a second "Starting" header.
      if(this._startCardEl && this._startCardEl.isConnected){
        this._startCardEl.innerHTML = html;
        return;
      }
      this._startCardEl = this._cycleEl(html);
    }

    _showTriage(triage){
      triage = triage || {};
      const tri = this._sr.querySelector('.alo-triage');
      if(!tri || this.getAttribute('show-triage') === 'false') return;
      this._sr.querySelector('[data-part="tri-cat"]').textContent = triage.category || '—';
      const kwsHost = this._sr.querySelector('[data-part="tri-kws"]');
      kwsHost.innerHTML = (triage.keywords||[]).map(k => `<span class="alo-triage-kw">${_esc(k)}</span>`).join('')
        || '<span style="color:var(--dim,#a89f92)">(none)</span>';
      this._sr.querySelector('[data-part="tri-reason"]').textContent = triage.reasoning || '—';
      tri.classList.add('open');
    }

    _showToolkit(list){
      const tk = this._sr.querySelector('.alo-toolkit');
      if(!tk || this.getAttribute('show-toolkit') === 'false') return;
      // The toolkit is nested inside the Triage card — make sure that card is
      // visible even if toolkit data arrives before triage_done.
      const tri = this._sr.querySelector('.alo-triage');
      if(tri && this.getAttribute('show-triage') !== 'false') tri.classList.add('open');
      tk.classList.add('open');
      this._sr.querySelector('[data-part="toolkit-count"]').textContent = (list||[]).length + ' caps';
      this._sr.querySelector('[data-part="toolkit-list"]').innerHTML =
        (list||[]).map(n => `<span class="alo-tag-chip">${_esc(n)}</span>`).join('');
    }

    _appendThink(ref, text){
      if(!text) return;
      let thinkEl = ref.el.querySelector('.alo-cycle-think');
      if(!thinkEl){
        thinkEl = document.createElement('details');
        thinkEl.className = 'alo-cycle-think';
        const summary = document.createElement('summary');
        summary.textContent = '💭 model thinking';
        thinkEl.appendChild(summary);
        // Place the thinking block at the TOP of the cycle: right after the
        // header and the brief .alo-cycle-thought (if present), before args /
        // result / progress. (insertBefore(…, null) appends at the end, which
        // is correct when the anchor is the last child.)
        const hdr   = ref.el.querySelector(':scope > .alo-cycle-h');
        const brief = ref.el.querySelector(':scope > .alo-cycle-thought');
        const anchor = brief || hdr;
        ref.el.insertBefore(thinkEl, anchor ? anchor.nextSibling : ref.el.firstChild);
      }
      // Replace the content (single <pre>) instead of appending. The backend
      // emits each cycle's reasoning BOTH as a structured think event and as a
      // [think #N] stream-token blob; appending rendered it twice.
      let pre = thinkEl.querySelector('pre');
      if(!pre){ pre = document.createElement('pre'); thinkEl.appendChild(pre); }
      pre.textContent = text;
      // Keep the latest reasoning in view while it streams in.
      _follow(pre);
      this._dedupeCycleThought(ref);
    }

    // A productive cycle receives its reasoning twice: as the brief inline
    // .alo-cycle-thought (from the tool_call event) AND inside the
    // .alo-cycle-think dropdown (from the structured think event) — identical
    // text in v4/v5. Show it once: a concise one-liner stays inline (always
    // visible); longer reasoning keeps the collapsible dropdown. Idempotent —
    // safe to call from both the think and tool_call handlers in any order.
    _dedupeCycleThought(ref){
      if(!ref || !ref.el) return;
      const brief = ref.el.querySelector(':scope > .alo-cycle-thought');
      const dd    = ref.el.querySelector(':scope > .alo-cycle-think');
      if(!brief || !dd) return;
      const pre = dd.querySelector('pre');
      const norm = s => String(s==null?'':s).replace(/\s+/g,' ').trim();
      const a = norm(brief.textContent);
      const b = pre ? norm(pre.textContent) : '';
      if(!a || !b) return;
      // Same reasoning if one is a prefix of the other (covers the think
      // event's char-cap truncation of a longer inline thought).
      if(!(a === b || a.startsWith(b) || b.startsWith(a))) return;
      if(Math.max(a.length, b.length) > 240) brief.remove();
      else dd.remove();
    }

    // Lazily materialise a cycle card when a tool_call/tool_done arrives without a
    // preceding cycle_planning — e.g. agent-chained cap hops (which don't emit one),
    // or a reattach/restore where the planning event was trimmed from the replay
    // list. This is what keeps EVERY tool cycle visible after a disconnect.
    _ensureCycle(cycle){
      if(cycle == null) return null;
      let ref = this._cycleRefs.get(cycle);
      if(ref) return ref;
      const el = this._cycleEl(`<div class="alo-cycle-h">
        <span class="alo-cycle-n">cycle ${_esc(String(cycle))}</span>
        <span class="alo-cycle-tool">…</span>
      </div>`);
      ref = {el, progressEl:null, tokenBuffer:''};
      this._cycleRefs.set(cycle, ref);
      return ref;
    }

    _renderToolCall(ev){
      const ref = this._ensureCycle(ev.cycle);
      if(!ref) return;
      const isLong = !!ev.long_running;
      const argsHtml = ev.args ? _fmtArgs(ev.args, 300) : '';
      ref.el.classList.toggle('long', isLong);

      // Update header in place — DON'T innerHTML-replace the cycle (would orphan progressEl)
      let hdr = ref.el.querySelector(':scope > .alo-cycle-h');
      const hdrHtml = `<span class="alo-cycle-n">cycle ${ev.cycle}</span>
          <span class="alo-cycle-tool">${_esc(ev.tool)}</span>
          ${isLong?`<span class="alo-version-pill v3" style="padding:1px 5px;font-size:8.5px">long-running</span>`:''}
          <span class="alo-cycle-status">running…</span>`;
      if(hdr){ hdr.innerHTML = hdrHtml; }
      else{
        hdr = document.createElement('div');
        hdr.className = 'alo-cycle-h';
        hdr.innerHTML = hdrHtml;
        ref.el.insertBefore(hdr, ref.el.firstChild);
      }

      // Thought
      let thought = ref.el.querySelector(':scope > .alo-cycle-thought');
      if(ev.thought){
        if(!thought){
          thought = document.createElement('div');
          thought.className = 'alo-cycle-thought';
          if(hdr.nextSibling) ref.el.insertBefore(thought, hdr.nextSibling);
          else ref.el.appendChild(thought);
        }
        thought.textContent = ev.thought;
      } else if(thought){
        thought.remove();
      }
      this._dedupeCycleThought(ref);

      // Args
      let argsEl = ref.el.querySelector(':scope > .alo-cycle-args');
      if(argsHtml){
        if(!argsEl){
          argsEl = document.createElement('div');
          argsEl.className = 'alo-cycle-args';
          const ps = ref.el.querySelector(':scope > .alo-progress');
          if(ps) ref.el.insertBefore(argsEl, ps);
          else ref.el.appendChild(argsEl);
        }
        argsEl.innerHTML = argsHtml;
      } else if(argsEl){
        argsEl.remove();
      }
      this.dispatchEvent(new CustomEvent('alo:tool-call', {detail:{cycle:ev.cycle, tool:ev.tool, args:ev.args}, bubbles:true}));
    }

    _renderToolDone(ev){
      const ref = this._ensureCycle(ev.cycle);
      if(!ref) return;
      const ok = ev.ok !== false;
      ref.el.classList.toggle('error', !ok);
      const status = ref.el.querySelector('.alo-cycle-status');
      if(status){
        status.textContent = (ok?'✓':'✗') + ' ' + (ev.elapsed_ms||0) + 'ms';
        status.style.color = ok ? 'var(--ok,var(--acc,#5a9e8f))' : 'var(--err,#c75a5a)';
      }
      // Stop spinner on progress strip if any
      const ph = ref.progressEl?.querySelector('.alo-progress-h');
      if(ph){
        const sp = ph.querySelector('.alo-spinner');
        if(sp) sp.remove();
        const note = document.createElement('span');
        note.style.cssText = 'color:var(--dim2,#8a7e70);font-size:9px';
        note.textContent = '· complete';
        ph.appendChild(note);
      }
      // Inline preview/error/empty card
      if(ev.preview || ev.error || ev.empty_search){
        let body = ref.el.querySelector('.alo-cycle-result');
        if(!body){
          body = document.createElement('div');
          body.className = 'alo-cycle-result';
          ref.el.appendChild(body);
        }
        const cls   = ev.empty_search ? 'empty' : (ok ? 'ok' : 'err');
        const label = ev.empty_search ? 'no results' : (ok ? 'result' : 'error');
        const text  = ev.empty_search
          ? 'Search returned 0 results — change the query or stop searching.'
          : (ok ? (ev.preview || '') : (ev.error || ev.preview || ''));
        const formatted = ok ? _smartRender(text) : _esc(text||'(empty)');
        body.innerHTML = `<div class="alo-result-h ${cls}">${label}</div>
          <div class="alo-result-render ${cls}">${formatted}</div>`;
      }
      this.dispatchEvent(new CustomEvent('alo:tool-done', {detail:{cycle:ev.cycle, tool:ev.tool, ok:ok}, bubbles:true}));
    }

    _ensureProgressStrip(cycle){
      const ref = this._cycleRefs.get(cycle);
      if(!ref) return null;
      // Defense in depth: if progressEl was orphaned by an innerHTML replacement
      if(ref.progressEl && !ref.el.contains(ref.progressEl)){
        ref.progressEl = null;
      }
      if(ref.progressEl) return ref.progressEl;
      const strip = document.createElement('div');
      strip.className = 'alo-progress';
      strip.innerHTML = `<div class="alo-progress-h">
        <span class="alo-spinner"></span>
        <span>live progress</span>
      </div>`;
      ref.el.appendChild(strip);
      ref.progressEl = strip;
      return strip;
    }

    _ensureResearchArea(strip, jobId){
      if(!strip) return null;
      let area = strip.querySelector('[data-research-stream="'+jobId+'"]');
      if(area) return area;
      const hdr = document.createElement('div');
      hdr.className = 'alo-progress-row';
      hdr.innerHTML = `<span class="alo-progress-tag" style="background:#1a2d3a;color:#5a9edd">stream</span>
        <span style="color:var(--info,#7eb8d9);font-size:9px">server-streamed research · job <code>${_esc((jobId||'').slice(0,8))}</code><span class="alo-cur"></span></span>`;
      strip.appendChild(hdr);
      area = document.createElement('div');
      // alo-md → the streamed research text renders as formatted markdown
      // (headings/lists/code/links) rather than a raw monospace dump. The
      // accumulated source markdown lives on area._raw so each token re-renders
      // the whole buffer (see the stream.token handler + _appendResearchToken).
      area.className = 'alo-progress-tokens alo-md';
      area.style.whiteSpace = 'normal';
      area.dataset.researchStream = jobId;
      strip.appendChild(area);
      return area;
    }

    /** Append a research-stream token and re-render the area as markdown.
     *  Keeps the raw source on `area._raw` so partial markdown formats live. */
    _appendResearchToken(area, tok){
      if(!area) return;
      area._raw = (area._raw || '') + (tok || '');
      area.innerHTML = _renderMarkdown(area._raw);
      _follow(area);
    }

    /** Renders a completed research job's full report inline as a collapsible,
     *  markdown-formatted card (with a citations list when available).
     *  Triggered by the agent_loop.research_report event emitted once a
     *  research.* cap finishes — the full result, distinct from the
     *  truncated one-line tool_done preview. */
    _renderResearchReport(strip, data){
      const report = data.report || '';
      if(!report.trim()) return;
      const details = document.createElement('details');
      details.className = 'alo-research-report';
      details.open = true;
      const jobShort = (data.job_id||'').slice(0,8);
      const cites = Array.isArray(data.citations) ? data.citations : [];
      let citesHtml = '';
      if(cites.length){
        const items = cites.map(c => {
          const url = typeof c === 'string' ? c : (c.url || c.link || '');
          const title = typeof c === 'string' ? c : (c.title || c.name || url);
          return url
            ? `<li><a href="${_esc(url)}" target="_blank" rel="noopener">${_esc(title)}</a></li>`
            : `<li>${_esc(title)}</li>`;
        }).join('');
        citesHtml = `<div class="alo-research-report-cites">${cites.length} citation${cites.length===1?'':'s'}<ol>${items}</ol></div>`;
      }
      details.innerHTML = `<summary>research report${data.tool?` · ${_esc(data.tool)}`:''}${jobShort?` · job <code>${_esc(jobShort)}</code>`:''}</summary>
        <div class="alo-research-report-body">${_renderMarkdown(report)}${citesHtml}</div>`;
      strip.appendChild(details);
      _follow(strip);
    }

    _appendProgressLine(strip, kindClass, kindLabel, bodyHtml){
      const line = document.createElement('div');
      line.className = 'alo-progress-line ' + (kindClass||'');
      line.innerHTML = `<span class="pkind">${_esc(kindLabel||'')}</span><span class="pbody">${bodyHtml||''}</span>`;
      strip.appendChild(line);
      _follow(strip);
    }

    /** Like _appendProgressLine, but updates an existing line (keyed by `key`)
     *  in place instead of appending a new one — used for repeating events
     *  like polling ticks so they don't spam a line per poll. */
    _upsertProgressLine(strip, key, kindLabel, bodyHtml){
      let line = strip.querySelector(`.alo-progress-line[data-lr="${key}"]`);
      if(!line){
        line = document.createElement('div');
        line.className = 'alo-progress-line';
        line.dataset.lr = key;
        line.innerHTML = `<span class="pkind"></span><span class="pbody"></span>`;
        strip.appendChild(line);
      }
      line.querySelector('.pkind').textContent = kindLabel||'';
      line.querySelector('.pbody').innerHTML = bodyHtml||'';
      _follow(strip);
    }

    _addProgress(ev){
      const strip = this._ensureProgressStrip(ev.cycle);
      if(!strip) return;
      const data = ev.data || {};
      const rt = ev.raw_type || data.type || 'event';

      // 1) LLM token stream
      if(rt === 'stream.token'){
        let activeCycle = 0;
        this._cycleRefs.forEach((_v, k) => { if(k > activeCycle) activeCycle = k; });
        if(ev.cycle && ev.cycle < activeCycle) return;
        let ref = this._cycleRefs.get(ev.cycle);
        if(!ref && activeCycle > 0) ref = this._cycleRefs.get(activeCycle);
        if(!ref) return;

        // Deduplicate by seq — stream_append_token emits via pub/sub AND the
        // SSE generator may also forward the same token event (wrapped as
        // tool_progress). If we've seen this seq already, skip it.
        const seq = data.seq || ev.seq;
        if(seq != null){
          if(!ref._seenSeqs) ref._seenSeqs = new Set();
          if(ref._seenSeqs.has(seq)) return;
          ref._seenSeqs.add(seq);
          // Keep set bounded — clear old entries when it grows large
          if(ref._seenSeqs.size > 2000){
            const arr = [...ref._seenSeqs];
            ref._seenSeqs = new Set(arr.slice(-500));
          }
        }

        const tok = data.token || data.text || '';
        // Research-source tokens go to dedicated area — but SKIP if a
        // WebSocket is already streaming tokens for this job (prevents doubling)
        if(data.source === 'research' && data.job_id){
          if(this._activeWsJobs.has(data.job_id)) return;  // WS is handling it
          const area = this._ensureResearchArea(strip, data.job_id);
          if(area){
            this._appendResearchToken(area, tok);
            return;
          }
        }
        // Also suppress general token buffer for any job_id with active WS
        if(data.job_id && this._activeWsJobs.has(data.job_id)) return;
        // Route [think #N] tokens
        const thinkMatch = tok.match(/^\n\[think #(\d+)\]\s*([\s\S]*)$/);
        if(thinkMatch){
          ref._inThinkBlock = true;
          ref._thinkBuffer = thinkMatch[2] || '';
          return;
        }
        if(ref._inThinkBlock && tok.match(/^\n\[plan #\d+\]/)){
          // Skip if the structured think event already rendered this cycle's
          // reasoning (fuller copy) — avoids the duplicate thought block.
          if(ref._thinkBuffer && this._showThinking && !ref._thinkFromEvent){
            this._appendThink(ref, ref._thinkBuffer);
          }
          ref._inThinkBlock = false;
          ref._thinkBuffer = '';
        }
        if(ref._inThinkBlock){
          ref._thinkBuffer = (ref._thinkBuffer||'') + tok;
          return;
        }
        // Strip [plan #N] marker prefix but keep the plan content that follows;
        // suppress [exec/done/auto-done/recovered] entirely (covered by structured events).
        const _planM = tok.match(/^\n\[plan #?\d*\]\s*/);
        if(_planM){ tok = tok.slice(_planM[0].length).trim(); if(!tok) return; }
        else if(tok.match(/^\n\[(exec|done|auto-done|recovered) #?\d*\]/)){ return; }
        let tokenEl = strip.querySelector('.alo-progress-tokens');
        if(!tokenEl){
          tokenEl = document.createElement('div');
          tokenEl.className = 'alo-progress-tokens';
          strip.appendChild(tokenEl);
        }
        ref.tokenBuffer = (ref.tokenBuffer||'') + tok;
        tokenEl.textContent = ref.tokenBuffer.slice(-1500);
        _follow(tokenEl);
        return;
      }
      if(rt === 'stream.complete'){
        this._appendProgressLine(strip, 'token', '', '✓ stream complete');
        return;
      }

      // 2) Research events
      if(rt.startsWith('research.')){
        let body = '';
        if(rt==='research.submitted' || rt==='research.job_started'){
          body = `submitted job <b>${_esc(data.job_id||'')}</b> · mode ${_esc(data.mode||'?')} · ${_esc(data.output_mode||'?')}`;
        }else if(rt==='research.job_progress'){
          body = `job <b>${_esc(data.job_id||'')}</b> · status: ${_esc(data.status||'')}`;
        }else if(rt==='research.completed'){
          body = `✓ job <b>${_esc(data.job_id||'')}</b> done · ${data.elapsed?Math.round(data.elapsed)+'s':''} · ${data.cit_count||0} citations`;
        }else if(rt==='research.error'){
          // Non-fatal: the job keeps running. Collapse repeats of the same
          // message into one line with a \u00d7N counter instead of spamming.
          const msg = (data.error||data.text||data.message||'unknown').trim();
          let h = 0; for(let i=0;i<msg.length;i++) h = (h*31 + msg.charCodeAt(i))|0;
          const key = 'res-err-'+(h>>>0);
          const counts = strip._resErrCounts || (strip._resErrCounts = {});
          const n = counts[key] = (counts[key]||0) + 1;
          this._upsertProgressLine(strip, key, 'error',
            `<span style="color:var(--err,#c75a5a)">\u2717 research error${n>1?' \u00d7'+n:''}: ${_esc(msg)}</span>`);
          return;
        }else{
          body = _esc(JSON.stringify(data).slice(0,180));
        }
        this._appendProgressLine(strip, 'research', rt.split('.')[1], body);
        return;
      }

      // 3) Exec events
      if(rt.startsWith('exec.')){
        let body = '';
        if(rt==='exec.stdout' || rt==='exec.stderr' || rt==='exec.line'){
          body = _esc((data.line || data.text || '').slice(0,240));
        }else if(rt==='exec.complete'){
          body = `✓ exit=${data.exit_code ?? '?'} · ${data.elapsed_ms||0}ms`;
        }else if(rt==='exec.error'){
          body = `<span style="color:var(--err,#c75a5a)">${_esc(data.error||'?')}</span>`;
        }else{
          body = _esc(JSON.stringify(data).slice(0,180));
        }
        this._appendProgressLine(strip, 'exec', rt.split('.')[1], body);
        return;
      }

      // 4) ML training
      if(rt.startsWith('ml_training.')){
        let body = '';
        if(rt==='ml_training.epoch'){
          body = `epoch ${data.epoch}/${data.total_epochs||'?'} · loss=${data.loss?.toFixed?.(4)||'?'}`;
        }else if(rt==='ml_training.metric'){
          body = `${_esc(data.name||'metric')}=${_esc(String(data.value))}`;
        }else if(rt==='ml_training.complete'){
          body = `✓ training done`;
        }else{
          body = _esc(JSON.stringify(data).slice(0,180));
        }
        this._appendProgressLine(strip, 'train', rt.split('.')[1], body);
        return;
      }

      // 5) Server-streamed research events wrapped as tool_progress
      if(rt === 'agent_loop.research_stream_open'){
        this._ensureResearchArea(strip, data.job_id || '');
        return;
      }
      if(rt === 'agent_loop.research_step'){
        const stepEl = document.createElement('div');
        stepEl.style.cssText = 'font-size:8.5px;color:var(--acc3,#c5a572);margin:2px 0;padding-left:4px';
        stepEl.textContent = '▸ '+(data.label||'')+(data.detail?' — '+data.detail:'');
        const area = strip.querySelector('[data-research-stream]');
        if(area) strip.insertBefore(stepEl, area);
        else strip.appendChild(stepEl);
        return;
      }
      if(rt === 'agent_loop.research_thinking'){
        if(!this._showThinking) return;
        let thinkEl = strip.querySelector('.alo-research-thinking');
        if(!thinkEl){
          thinkEl = document.createElement('div');
          thinkEl.className = 'alo-research-thinking';
          strip.appendChild(thinkEl);
        }
        thinkEl.textContent += (data.text||'');
        _follow(thinkEl);
        return;
      }
      if(rt === 'agent_loop.research_citations'){
        this._appendProgressLine(strip, 'research', 'cite',
          `${data.count||0} citation${data.count===1?'':'s'} gathered`);
        return;
      }
      if(rt === 'agent_loop.research_file'){
        this._appendProgressLine(strip, 'research', 'file', `<code>${_esc(data.path||'')}</code>`);
        return;
      }
      if(rt === 'agent_loop.research_stream_done'){
        this._appendProgressLine(strip, 'research', 'done',
          `✓ stream complete · ${data.tokens||0} tokens · ${data.steps||0} steps · ${data.citations||0} cites · ${data.elapsed||0}s`);
        return;
      }
      if(rt === 'agent_loop.research_stream_failed'){
        this._appendProgressLine(strip, 'research', 'fail',
          `<span style="color:var(--err,#c75a5a)">${_esc(data.error||'')}</span> — falling back to polling`);
        return;
      }
      if(rt === 'agent_loop.research_stream_hint'){
        this._appendProgressLine(strip, 'research', 'hint',
          `live stream available · job <code>${_esc((data.job_id||'').slice(0,8))}</code>`);
        return;
      }
      if(rt === 'agent_loop.research_report'){
        this._renderResearchReport(strip, data);
        return;
      }
      if(rt === 'agent_loop.long_running_await_start'
         || rt === 'agent_loop.long_running_await_tick'
         || rt === 'agent_loop.long_running_await_done'
         || rt === 'agent_loop.long_running_await_timeout'
         || rt === 'agent_loop.long_running_await_skipped'){
        const lbl = rt.split('.').pop().replace('long_running_await_','');
        let body;
        if(lbl === 'start') body = `awaiting job <code>${_esc((data.job_id||'').slice(0,8))}</code> via <code>${_esc(data.status_cap||'')}</code>`;
        else if(lbl === 'tick') body = `polling… ${data.polls||0} checks · ${data.elapsed||0}s · status=${_esc(data.status||'?')}`;
        else if(lbl === 'done') body = `✓ job <code>${_esc((data.job_id||'').slice(0,8))}</code> finished · ${data.elapsed||0}s · ${data.polls||0} polls`;
        else if(lbl === 'timeout') body = `<span style="color:var(--err,#c75a5a)">⌛ timeout after ${data.elapsed||0}s</span>`;
        else body = `skipped: ${_esc(data.reason||'?')}`;
        // Polling ticks update a single line in place (keyed by job_id)
        // instead of appending a fresh line for every poll.
        this._upsertProgressLine(strip, 'await-'+(data.job_id||''), lbl, body);
        return;
      }

      // 6) Generic fallback
      this._appendProgressLine(strip, '', rt, _esc(JSON.stringify(data).slice(0,200)));
    }

    // ───────────────────── Long-running await renderers (top-level) ──
    _renderAwaitStart(ev){
      const ref = this._cycleRefs.get(ev.cycle);
      if(!ref) return;
      const status = ref.el.querySelector('.alo-cycle-status');
      if(status){
        status.textContent = `awaiting job ${(ev.job_id||'').slice(0,8)}…`;
        status.style.color = 'var(--warn,#c9a45a)';
      }
      const strip = this._ensureProgressStrip(ev.cycle);
      if(strip){
        const row = document.createElement('div');
        row.className = 'alo-progress-row';
        row.dataset.lr = 'await-'+(ev.job_id||'');
        row.innerHTML = `<span class="alo-progress-tag" style="background:#3a2a10;color:#ffb74d">await</span>
          <span>polling <code>${_esc(ev.status_cap||'')}</code> for job <code>${_esc((ev.job_id||'').slice(0,8))}</code></span>`;
        strip.appendChild(row);
      }
    }
    _renderAwaitTick(ev){
      const strip = this._cycleRefs.get(ev.cycle)?.progressEl;
      if(!strip) return;
      let row = strip.querySelector(`.alo-progress-row[data-lr="await-${ev.job_id||''}"]`);
      if(!row){
        row = document.createElement('div');
        row.className = 'alo-progress-row';
        row.dataset.lr = 'await-'+(ev.job_id||'');
        row.innerHTML = `<span class="alo-progress-tag" style="background:#3a2a10;color:#ffb74d">await</span><span></span>`;
        strip.appendChild(row);
      }
      const tail = row.querySelector('span:last-child');
      if(tail) tail.textContent = `polling… ${ev.polls||0} checks · ${ev.elapsed||0}s · status=${_esc(ev.status||'?')}`;
    }
    _renderAwaitDone(ev){
      const strip = this._cycleRefs.get(ev.cycle)?.progressEl;
      if(!strip) return;
      const row = strip.querySelector(`.alo-progress-row[data-lr="await-${ev.job_id||''}"]`);
      if(row){
        row.innerHTML = `<span class="alo-progress-tag" style="background:#1d3a1d;color:#7ed99e">done</span>
          <span>job <code>${_esc((ev.job_id||'').slice(0,8))}</code> finished after ${ev.elapsed||0}s (${ev.polls||0} polls)</span>`;
      }
    }
    _renderAwaitTimeout(ev){
      const strip = this._cycleRefs.get(ev.cycle)?.progressEl;
      if(!strip) return;
      const row = strip.querySelector(`.alo-progress-row[data-lr="await-${ev.job_id||''}"]`);
      if(row){
        row.innerHTML = `<span class="alo-progress-tag" style="background:#3a1313;color:#ff7676">timeout</span>
          <span>job <code>${_esc((ev.job_id||'').slice(0,8))}</code> exceeded ${ev.elapsed||0}s</span>`;
      }
    }

    // ───────────────────── Research stream renderers (top-level) ──────
    _renderResearchHint(ev){
      // Optional WS opening — we attempt but it's tolerant of failure.
      const ref = this._cycleRefs.get(ev.cycle);
      if(!ev.ws_url || !ev.job_id) return;
      const strip = this._ensureProgressStrip(ev.cycle);
      if(!strip) return;
      const streamArea = document.createElement('div');
      // alo-md → render the live research stream as formatted markdown (buffer
      // on streamArea._raw; see _appendResearchToken).
      streamArea.className = 'alo-progress-tokens alo-md';
      streamArea.dataset.researchJob = ev.job_id;
      streamArea.style.minHeight = '140px';
      streamArea.style.whiteSpace = 'normal';
      const streamHdr = document.createElement('div');
      streamHdr.className = 'alo-progress-row';
      streamHdr.innerHTML = `<span class="alo-progress-tag" style="background:#1a2d3a;color:#5a9edd">stream</span>
        <span style="color:var(--info,#7eb8d9);font-size:9px">live research stream · job <code>${_esc((ev.job_id||'').slice(0,8))}</code><span class="alo-cur"></span></span>`;
      strip.appendChild(streamHdr);
      strip.appendChild(streamArea);
      // The server builds ws_url from ITS OWN vantage point (often
      // ws://localhost:<port>) — useless from the browser, and ws:// is
      // mixed-content-blocked on an https page. The stream route lives on
      // the same app that serves this UI, so rebuild it same-origin.
      let wsUrl = ev.ws_url;
      try{
        const u = new URL(ev.ws_url);
        if(location.host && (u.hostname === 'localhost' || u.hostname === '127.0.0.1')){
          wsUrl = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + u.pathname;
        }else if(location.protocol === 'https:' && u.protocol === 'ws:'){
          wsUrl = 'wss://' + u.host + u.pathname;
        }
      }catch(_){}
      try{
        const ws = new WebSocket(wsUrl);
        const _wsJobId = ev.job_id;
        this._activeWsJobs.add(_wsJobId);
        ws.onmessage = e => {
          try{
            const m = JSON.parse(e.data);
            if(m.type === 'token' || m.type === 'thinking'){
              this._appendResearchToken(streamArea, m.text||'');
            } else if(m.type === 'step'){
              const stepEl = document.createElement('div');
              stepEl.style.cssText = 'font-size:8.5px;color:var(--acc3,#c5a572);margin:2px 0';
              stepEl.textContent = '▸ '+(m.label||'')+(m.detail?' — '+m.detail:'');
              strip.insertBefore(stepEl, streamArea);
            } else if(m.type === 'done'){
              const doneEl = streamHdr.querySelector('span:last-child');
              if(doneEl) doneEl.innerHTML = `✓ research complete · <code>${_esc((ev.job_id||'').slice(0,8))}</code>`;
              this._activeWsJobs.delete(_wsJobId);
              ws.close();
            } else if(m.type === 'error'){
              // Non-fatal sub-step failure (one source 500'd, an instance is
              // down, …) — the job keeps running and will still send `done`.
              // Closing here killed the live stream on the first hiccup.
              this._appendResearchToken(streamArea, '\n\n⚠ '+(m.text||'stream error')+'\n\n');
            }
          }catch(_){}
        };
        ws.onerror = () => {
          this._activeWsJobs.delete(_wsJobId);
          const errEl = document.createElement('div');
          errEl.style.cssText = 'font-size:8.5px;color:var(--err,#c75a5a)';
          errEl.textContent = '⚠ stream connection failed (WS unavailable)';
          strip.appendChild(errEl);
        };
        ws.onclose = () => {
          this._activeWsJobs.delete(_wsJobId);
        };
      }catch(_){}
    }
    _renderResearchOpen(ev){
      const strip = this._ensureProgressStrip(ev.cycle);
      if(!strip) return;
      this._ensureResearchArea(strip, ev.job_id);
    }
    _renderResearchStep(ev){
      const strip = this._cycleRefs.get(ev.cycle)?.progressEl;
      if(!strip) return;
      const stepEl = document.createElement('div');
      stepEl.style.cssText = 'font-size:8.5px;color:var(--acc3,#c5a572);margin:2px 0;padding-left:4px';
      stepEl.textContent = '▸ '+(ev.label||'')+(ev.detail?' — '+ev.detail:'');
      const area = strip.querySelector('[data-research-stream]');
      if(area) strip.insertBefore(stepEl, area);
      else strip.appendChild(stepEl);
    }
    _renderResearchThinking(ev){
      if(!this._showThinking) return;
      const strip = this._cycleRefs.get(ev.cycle)?.progressEl;
      if(!strip) return;
      let thinkEl = strip.querySelector('.alo-research-thinking');
      if(!thinkEl){
        thinkEl = document.createElement('div');
        thinkEl.className = 'alo-research-thinking';
        strip.appendChild(thinkEl);
      }
      thinkEl.textContent += (ev.text||'');
      _follow(thinkEl);
    }
    _renderResearchCitations(ev){
      const strip = this._cycleRefs.get(ev.cycle)?.progressEl;
      if(!strip) return;
      const row = document.createElement('div');
      row.className = 'alo-progress-row';
      row.innerHTML = `<span class="alo-progress-tag" style="background:#1d2e3a;color:#7eb6dd">cite</span>
        <span>${ev.count||0} citation${ev.count===1?'':'s'} gathered</span>`;
      strip.appendChild(row);
    }
    _renderResearchFile(ev){
      const strip = this._cycleRefs.get(ev.cycle)?.progressEl;
      if(!strip) return;
      const row = document.createElement('div');
      row.className = 'alo-progress-row';
      row.innerHTML = `<span class="alo-progress-tag" style="background:#2a3a1d;color:#a8c87a">file</span>
        <span><code>${_esc(ev.path||'')}</code></span>`;
      strip.appendChild(row);
    }
    _renderResearchStreamDone(ev){
      const strip = this._cycleRefs.get(ev.cycle)?.progressEl;
      if(!strip) return;
      const row = document.createElement('div');
      row.className = 'alo-progress-row';
      row.innerHTML = `<span class="alo-progress-tag" style="background:#1d3a1d;color:#7ed99e">done</span>
        <span>✓ stream complete · ${ev.tokens||0} tokens · ${ev.steps||0} steps · ${ev.citations||0} cites · ${ev.elapsed||0}s</span>`;
      strip.appendChild(row);
    }
    _renderResearchStreamFailed(ev){
      const strip = this._cycleRefs.get(ev.cycle)?.progressEl;
      if(!strip) return;
      const row = document.createElement('div');
      row.className = 'alo-progress-row';
      row.innerHTML = `<span class="alo-progress-tag" style="background:#3a1313;color:#ff7676">stream fail</span>
        <span>${_esc(ev.error||'')} — falling back to polling</span>`;
      strip.appendChild(row);
    }

    // ───────────────────── Error recovery renderers ─────────────────
    _renderRecoveryStart(ev){
      const strip = this._cycleRefs.get(ev.cycle)?.progressEl;
      if(!strip) return;
      let box = strip.querySelector('.alo-recovery-box');
      if(!box){
        box = document.createElement('div');
        box.className = 'alo-recovery-box';
        box.style.cssText = 'margin-top:5px;padding:5px 7px;background:#2d2010;border:1px solid #4d3010;border-radius:3px';
        box.innerHTML = `<div style="font-size:9px;color:#ffb074;font-weight:600;text-transform:uppercase;letter-spacing:.4px">Recovering tool error</div>
          <div style="font-size:9.5px;color:var(--text2,#bfb6a8);margin-top:2px">${_esc(ev.error||'').slice(0,200)}</div>
          <div class="alo-recovery-attempts"></div>`;
        strip.appendChild(box);
      }
    }
    _renderRecoveryAttempt(ev){
      const strip = this._cycleRefs.get(ev.cycle)?.progressEl;
      if(!strip) return;
      const attempts = strip.querySelector('.alo-recovery-attempts');
      if(!attempts) return;
      const row = document.createElement('div');
      row.style.cssText = 'font-family:var(--mono,monospace);font-size:8.5px;color:var(--text2,#bfb6a8);padding:2px 0';
      let argsStr;
      try { argsStr = JSON.stringify(ev.args||{}); } catch(_){ argsStr = String(ev.args||''); }
      row.innerHTML = `<span style="color:#ffb074">attempt ${ev.attempt||'?'}</span> · <code style="color:var(--text2,#bfb6a8)">${_esc(argsStr.slice(0,200))}</code>`;
      attempts.appendChild(row);
    }
    _renderRecoveryDone(ev){
      const strip = this._cycleRefs.get(ev.cycle)?.progressEl;
      if(!strip) return;
      const box = strip.querySelector('.alo-recovery-box');
      if(!box) return;
      const summary = document.createElement('div');
      if(ev.recovered){
        summary.style.cssText = 'margin-top:3px;color:#7ed99e;font-size:8.5px;font-weight:600';
        summary.textContent = `✓ recovered after ${ev.attempts||0} attempt${ev.attempts===1?'':'s'}`;
        box.style.borderColor = '#1d4d2d';
        box.style.background = '#0d2415';
      } else {
        summary.style.cssText = 'margin-top:3px;color:#ff7676;font-size:8.5px;font-weight:600';
        summary.textContent = ev.gave_up
          ? `✗ agent gave up: ${(ev.reason||'').slice(0,150)}`
          : `✗ recovery failed after ${ev.attempts||0} attempt${ev.attempts===1?'':'s'}: ${(ev.reason||'').slice(0,120)}`;
        box.style.borderColor = '#5a1a1a';
        box.style.background = '#2d1010';
      }
      box.appendChild(summary);
    }

    // ───────────────────── HITL pause card ──────────────────────────
    _showHitlPause(ev){
      const cycRef = this._cycleRefs.get(ev.cycle);
      const host = cycRef ? cycRef.el : this._sr.querySelector('.alo-cycles');
      const pause = document.createElement('div');
      pause.className = 'alo-hitl-pause';
      pause.dataset.step = ev.step;
      const argsStr = JSON.stringify(ev.args || {}, null, 2);
      const timeoutAt = Date.now() + (ev.timeout_secs||300)*1000;
      const reasonTxt = ev.reason === 'long_running_pre_explore'
        ? '⏳ Long-running tool requested before exploring — approve to run it now, or skip and let the agent gather context first.'
        : '';
      pause.innerHTML = `
        <div class="alo-hitl-pause-h">
          <span class="pulse"></span>
          <span>Approval required — cycle ${ev.cycle}</span>
          <span class="alo-hitl-pause-meta" style="margin-left:auto">
            timeout in <span class="countdown">${ev.timeout_secs||300}s</span>
          </span>
        </div>
        ${reasonTxt?`<div class="alo-hitl-pause-reason" style="font-size:10px;color:var(--warn,#c79a5a);margin:2px 0 4px">${_esc(reasonTxt)}</div>`:''}
        ${ev.thought?`<div class="alo-hitl-pause-thought">${_esc(ev.thought)}</div>`:''}
        <div>
          <div style="font-size:9.5px;color:var(--dim2,#8a7e70);text-transform:uppercase;letter-spacing:.4px;margin-bottom:3px">Tool</div>
          <div class="alo-hitl-pause-tool">${_esc(ev.tool)}</div>
        </div>
        <div>
          <div style="font-size:9.5px;color:var(--dim2,#8a7e70);text-transform:uppercase;letter-spacing:.4px;margin-bottom:3px">Arguments (editable JSON)</div>
          <textarea class="alo-hitl-pause-args" data-step="${ev.step}">${_esc(argsStr)}</textarea>
        </div>
        <div class="alo-hitl-pause-actions">
          <button class="alo-hitl-btn primary" data-action="approve" data-step="${ev.step}">Approve</button>
          <button class="alo-hitl-btn"         data-action="edit"    data-step="${ev.step}">Apply edit + run</button>
          <button class="alo-hitl-btn warn"    data-action="reject"  data-step="${ev.step}">Skip step</button>
          <button class="alo-hitl-btn danger"  data-action="abort"   data-step="${ev.step}">Abort run</button>
        </div>`;
      host.appendChild(pause);

      // Wire buttons
      pause.querySelectorAll('button[data-action]').forEach(btn => {
        btn.addEventListener('click', () => this._hitlRespond(parseInt(btn.dataset.step,10), btn.dataset.action, btn));
      });

      // Countdown
      const countdownEl = pause.querySelector('.countdown');
      const tick = setInterval(() => {
        const left = Math.max(0, Math.round((timeoutAt - Date.now())/1000));
        if(countdownEl) countdownEl.textContent = left+'s';
        if(left<=0 || !pause.isConnected) clearInterval(tick);
      }, 1000);
      pause._tick = tick;

      const cycles = this._sr.querySelector('.alo-cycles');
      _follow(cycles);
    }

    async _hitlRespond(step, decision, btn){
      if(btn) btn.disabled = true;
      // Disable sibling buttons too
      const card = this._sr.querySelector(`.alo-hitl-pause[data-step="${step}"]`);
      if(card) card.querySelectorAll('button').forEach(b => b.disabled = true);
      let args = {};
      if(decision === 'edit' && card){
        const ta = card.querySelector('.alo-hitl-pause-args');
        try{ args = JSON.parse(ta.value || '{}'); }
        catch(e){
          if(card) card.querySelectorAll('button').forEach(b => b.disabled = false);
          alert('Args JSON invalid: '+e.message);
          return;
        }
      }
      try{
        const base = this._apiBase || _apiBase();
        await fetch(base + this._hitlEndpoint, {
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({session_id: this._sessionId, step, decision, args}),
        });
      }catch(_){}
      if(card){
        if(card._tick) clearInterval(card._tick);
        const actions = card.querySelector('.alo-hitl-pause-actions');
        if(actions) actions.innerHTML =
          `<span style="font-size:10.5px;color:var(--dim,#a89f92);font-style:italic">resolved: ${_esc(decision)}</span>`;
        card.style.opacity = '0.65';
      }
      this._hitlPending.delete(step);
      this.dispatchEvent(new CustomEvent('alo:hitl-resolved', {detail:{step, decision}, bubbles:true}));
    }

    // ───────────────────── Phase badge ───────────────────────────────
    _showPhase(ev){
      const phase = ev.phase || '';
      // Map phase → label + colour. Skip the noisy intermediate "explore"
      // re-emits unless they carry a reason worth showing.
      const COLORS = {think:'#8a7ec7', explore:'#5a9e8f', act:'#c79a5a', validate:'#5a8fc7', verify:'#5a8fc7'};
      const reason = ev.reason || '';
      let label = phase.toUpperCase();
      if(reason === 'act_blocked') label = 'EXPLORE FIRST';
      else if(reason === 'long_running_pre_explore') label = 'ACT · approval needed';
      else if(reason === 'validation_required' || reason === 'verify_required') label = 'VERIFY';
      const ref = this._cycleRefs.get(ev.cycle);
      const host = (ref && ref.el) ? ref.el : this._sr.querySelector('.alo-cycles');
      if(!host) return;
      const badge = document.createElement('div');
      badge.className = 'alo-phase-badge';
      badge.style.cssText = `display:inline-flex;align-items:center;gap:5px;font-size:9px;`
        + `text-transform:uppercase;letter-spacing:.5px;margin:3px 0;padding:2px 7px;border-radius:9px;`
        + `background:${COLORS[phase]||'#777'}22;color:${COLORS[phase]||'#999'};border:1px solid ${COLORS[phase]||'#777'}55`;
      const explore = (ev.explore_done!=null && ev.min_explore!=null) ? ` ${ev.explore_done}/${ev.min_explore}` : '';
      badge.textContent = `◇ ${label}${explore}`;
      host.appendChild(badge);
      this.dispatchEvent(new CustomEvent('alo:phase', {detail:{phase, cycle:ev.cycle, reason}, bubbles:true}));
    }

    // ───────────────────── Budget pause (continue) ───────────────────
    _showBudgetPause(ev){
      // One open card at a time.
      if(this._sr.querySelector('.alo-budget-pause:not(.resolved)')) return;
      const host = this._sr.querySelector('.alo-cycles') || this._sr;
      const inc = ev.increment || 8;
      const stepId = (ev.step != null) ? ev.step : -1;
      const card = document.createElement('div');
      card.className = 'alo-budget-pause alo-hitl-pause';
      card.innerHTML = `
        <div class="alo-hitl-pause-h">
          <span class="pulse"></span>
          <span>Cycle budget reached (${ev.cycles}/${ev.max_cycles})</span>
        </div>
        <div style="font-size:10.5px;color:var(--dim,#a89f92);margin:4px 0">
          The agent has used its cycle budget. Continue to give it ${inc} more cycles, or wrap up with what it has so far.
        </div>
        <div class="alo-budget-actions alo-hitl-pause-actions">
          <button class="alo-hitl-btn primary" data-budget="continue">Continue (+${inc})</button>
          <button class="alo-hitl-btn warn"    data-budget="wrap">Wrap up now</button>
        </div>`;
      host.appendChild(card);
      card.querySelectorAll('button[data-budget]').forEach(btn=>{
        btn.addEventListener('click', ()=> this._budgetRespond(card, btn.dataset.budget, inc, stepId));
      });
      const cycles = this._sr.querySelector('.alo-cycles');
      _follow(cycles);
      this.dispatchEvent(new CustomEvent('alo:budget-pause', {detail:{cycles:ev.cycles, max_cycles:ev.max_cycles}, bubbles:true}));
    }

    async _budgetRespond(card, decision, increment, stepId){
      card.querySelectorAll('button').forEach(b=> b.disabled = true);
      try{
        const base = this._apiBase || _apiBase();
        await fetch(base + this._hitlEndpoint, {
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({session_id:this._sessionId, step:(stepId!=null?stepId:-1), decision, increment}),
        });
      }catch(_){}
      // The runner emits budget_continue on continue; for wrap we resolve here.
      if(decision === 'wrap'){
        card.classList.add('resolved');
        const actions = card.querySelector('.alo-budget-actions');
        if(actions) actions.innerHTML = `<span style="font-size:10.5px;color:var(--dim,#a89f92);font-style:italic">wrapping up…</span>`;
        card.style.opacity = '0.65';
      }
      this.dispatchEvent(new CustomEvent('alo:budget-resolved', {detail:{decision}, bubbles:true}));
    }

    // ───────────────────── Final pane render ────────────────────────
    _renderFinalPane(ev){
      if(this.getAttribute('show-final') === 'false') return;
      const pane = this._sr.querySelector('.alo-final-pane');
      const body = this._sr.querySelector('[data-part="final-body"]');
      if(!pane || !body) return;
      pane.classList.add('show');

      if(ev.error){
        body.innerHTML = `<div class="alo-final-row">
          <div class="alo-final-lbl">Error</div>
          <div class="alo-final-val" style="color:var(--err,#c75a5a)">${_esc(ev.error)}</div>
        </div>`;
        return;
      }

      const goal    = ev.goal || '';
      const summary = ev.summary || ev.final || '';
      const triage  = ev.triage || {};
      const history = ev.history || [];
      const cycles  = ev.cycles ?? '?';
      const done    = !!ev.done;

      const usedTools = {};
      history.forEach(h => {
        if(!h || !h.tool) return;
        if(h.tool.startsWith('(')) return;
        usedTools[h.tool] = (usedTools[h.tool]||0) + 1;
      });
      const okSteps = history.filter(h => h && h.ok===true && !String(h.tool||'').startsWith('('));
      const errSteps= history.filter(h => h && h.ok===false);

      let html = '';
      if(goal){
        html += `<div class="alo-final-row">
          <div class="alo-final-lbl">Goal</div>
          <div class="alo-final-val summary">${_esc(goal)}</div>
        </div>`;
      }

      const deliv = ev.deliverable || ev.handover_output;
      if(deliv){
        html += `<div class="alo-final-row">
          <div class="alo-final-lbl" style="color:var(--acc,#5a9e8f)">${ev.deliverable?'📦 Deliverable':'★ Synthesised answer'}</div>
          <div class="alo-final-val">
            <div class="alo-handover-body">${_renderMarkdown(deliv)}</div>
            ${summary?`<details style="margin-top:6px"><summary style="cursor:pointer;font-size:9.5px;color:var(--dim,#a89f92)">show original raw answer</summary>
              <div class="alo-final-val summary" style="margin-top:4px;font-size:10px">${_esc(summary)}</div>
            </details>`:''}
          </div>
        </div>`;
      } else if(summary){
        html += `<div class="alo-final-row">
          <div class="alo-final-lbl">${done?'Answer':'Result'}</div>
          <div class="alo-final-val summary">${_esc(summary)}</div>
        </div>`;
      }

      if(triage.category || (triage.keywords||[]).length){
        html += `<div class="alo-final-row">
          <div class="alo-final-lbl">Triage</div>
          <div class="alo-final-val">
            <span class="alo-final-cat">${_esc(triage.category||'?')}</span>
            ${(triage.keywords||[]).map(k => `<span class="alo-final-tool">${_esc(k)}</span>`).join(' ')}
            ${triage.reasoning?`<div style="margin-top:4px;font-style:italic;color:var(--dim,#a89f92)">${_esc(triage.reasoning)}</div>`:''}
          </div>
        </div>`;
      }

      html += `<div class="alo-final-row">
        <div class="alo-final-lbl">Stats</div>
        <div class="alo-final-val">
          <span class="alo-final-cat" style="background:rgba(90,158,143,.18);color:var(--acc,#5a9e8f)">${cycles} cycle${cycles===1?'':'s'}</span>
          <span class="alo-final-tool ok">${okSteps.length} ok</span>
          ${errSteps.length?`<span class="alo-final-tool err">${errSteps.length} errored</span>`:''}
          <span class="alo-final-tool">${Object.keys(usedTools).length} unique tool${Object.keys(usedTools).length===1?'':'s'}</span>
        </div>
      </div>`;

      if(Object.keys(usedTools).length){
        html += `<div class="alo-final-row">
          <div class="alo-final-lbl">Tools used</div>
          <div class="alo-final-val">
            <div class="alo-final-tools">
              ${Object.entries(usedTools).sort((a,b)=>b[1]-a[1]).map(([t,c]) =>
                `<span class="alo-final-tool ok">${_esc(t)}${c>1?' ×'+c:''}</span>`).join('')}
            </div>
          </div>
        </div>`;
      }

      if(history.length){
        const realSteps = history.filter(h => h && h.tool && !h.tool.startsWith('('));
        const metaSteps = history.filter(h => h && h.tool && h.tool.startsWith('('));
        html += `<div class="alo-final-row">
          <div class="alo-final-lbl">Steps</div>
          <div class="alo-final-val">`;
        let stepNum = 0;
        realSteps.forEach(h => {
          stepNum++;
          const tool = h.tool || '?';
          const cls  = h.ok===false ? 'err' : 'ok';
          const argSnippet = h.args ? _fmtArgs(h.args, 120) : '';
          html += `<div class="alo-final-step ${cls}">
            <div class="alo-final-step-h">
              <span class="alo-final-step-tool">${stepNum}. ${_esc(tool)}</span>
              ${h.ms?`<span class="alo-final-step-ms">${h.ms}ms</span>`:''}
            </div>
            ${argSnippet?`<div class="alo-final-step-args">${argSnippet}</div>`:''}
          </div>`;
        });
        if(metaSteps.length){
          const grouped = {};
          metaSteps.forEach(h => { grouped[h.tool] = (grouped[h.tool]||0) + 1; });
          const summaryHtml = Object.entries(grouped)
            .map(([t,c]) => `<span class="alo-final-tool" style="opacity:.7">${_esc(t)}${c>1?' ×'+c:''}</span>`)
            .join(' ');
          html += `<details style="margin-top:5px;font-size:10px">
            <summary style="cursor:pointer;color:var(--dim,#a89f92)">${metaSteps.length} non-tool event${metaSteps.length===1?'':'s'} (parse errors, blocks, etc.)</summary>
            <div style="margin-top:4px;display:flex;flex-wrap:wrap;gap:3px">${summaryHtml}</div>
          </details>`;
        }
        html += `</div></div>`;
      }

      const rawCopy = Object.assign({}, ev); delete rawCopy.type;
      html += `<details class="alo-final-raw">
        <summary>Raw payload</summary>
        <pre>${_esc(JSON.stringify(rawCopy, null, 2))}</pre>
      </details>`;

      body.innerHTML = html;
    }
  }

  customElements.define('vera-agent-loop-output', VeraAgentLoopOutput);
})();