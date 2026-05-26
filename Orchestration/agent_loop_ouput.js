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
      let vs;
      if(typeof v === 'string'){
        vs = v.length > 80 ? v.slice(0,77)+'…' : v;
      } else if(typeof v === 'boolean' || typeof v === 'number'){
        vs = String(v);
      } else {
        vs = JSON.stringify(v);
        if(vs.length > 80) vs = vs.slice(0,77)+'…';
      }
      const part = `<span class="alo-arg-pill"><span class="alo-arg-key">${_esc(k)}</span><span class="alo-arg-val">${_esc(vs)}</span></span>`;
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

  // ───────────────────────── Stylesheet (one shared <style>) ────────────────
  // Class names use an `.alo-` prefix to avoid conflicts with host pages that
  // also use `.al-*` classes (the original DAG workshop). All colours come
  // from CSS custom properties so the host theme drives the appearance.
  const STYLE = `
:host{display:block;width:100%;color:var(--text,#ddd5c8);font-family:var(--mono,'IBM Plex Mono',monospace);font-size:11px}
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

/* Toolkit chips */
.alo-toolkit{background:var(--bg1,#1f1d1a);border:1px solid var(--border,#3a3530);border-radius:3px;padding:8px 10px;font-size:10px;display:none;flex-shrink:0;max-height:120px;overflow-y:auto}
.alo-toolkit.open{display:block}
.alo-toolkit-h{font-size:10px;color:var(--acc2,#a8c87a);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px;display:flex;align-items:center;justify-content:space-between}
.alo-toolkit-list{display:flex;flex-wrap:wrap;gap:3px;font-family:var(--mono,monospace);font-size:9.5px;color:var(--text2,#bfb6a8)}
.alo-tag-chip{font-size:9px;padding:1px 6px;border-radius:8px;background:var(--bg3,#2a2622);color:var(--text2,#bfb6a8);font-family:var(--mono,monospace)}

/* Cycles list */
.alo-cycles{flex:1;background:var(--bg1,#1f1d1a);border:1px solid var(--border,#3a3530);border-radius:3px;overflow-y:auto;padding:8px;display:flex;flex-direction:column;gap:6px;font-family:var(--mono,monospace);font-size:10.5px;min-height:60px}
.alo-cycle{padding:7px 10px;background:var(--bg2,#252220);border:1px solid var(--border,#3a3530);border-radius:3px}
.alo-cycle.error{border-color:var(--err,#c75a5a);background:rgba(199,90,90,.05)}
.alo-cycle.done{border-color:var(--acc,#5a9e8f);background:rgba(90,158,143,.05)}
.alo-cycle.expand{border-color:var(--acc4,#a07ec1);background:rgba(160,126,193,.05)}
.alo-cycle.warn{border-color:#c9a45a;background:rgba(201,164,90,.06)}
.alo-cycle.handover{border-color:var(--acc,#5a9e8f);border-left-width:3px;background:rgba(90,158,143,.04)}

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
.alo-cycle-preview{font-size:9.5px;color:var(--dim,#a89f92);background:var(--bg0,#181614);padding:5px 7px;border-radius:3px;max-height:80px;overflow-y:auto;white-space:pre-wrap;line-height:1.4}
.alo-cycle-result{margin-top:6px;border:1px solid var(--border,#3a3530);border-radius:3px;background:var(--bg0,#181614);overflow:hidden}
.alo-result-h{font-size:9px;text-transform:uppercase;letter-spacing:.4px;padding:3px 6px;font-weight:500}
.alo-result-h.ok{background:rgba(90,158,143,.13);color:var(--ok,var(--acc,#5a9e8f))}
.alo-result-h.err{background:rgba(199,90,90,.13);color:var(--err,#c75a5a)}
.alo-result-h.empty{background:rgba(201,164,90,.13);color:#c9a45a}
.alo-result-body{margin:0;padding:6px 8px;font-family:var(--mono,monospace);font-size:10px;color:var(--text2,#bfb6a8);max-height:200px;overflow:auto;white-space:pre-wrap;line-height:1.6;word-break:break-word;display:flex;flex-wrap:wrap;gap:3px 5px;align-items:flex-start}
.alo-result-body.err{color:#ff9999}
.alo-result-body.empty{color:#c9a45a}

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
.alo-version-pill.openclaw{background:rgba(143,184,122,.18);color:var(--acc2,#a8c87a)}

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

/* Final pane */
.alo-final-pane{display:none;flex-direction:column;gap:8px;background:var(--bg1,#1f1d1a);border:1px solid var(--acc,#5a9e8f);border-left:3px solid var(--acc,#5a9e8f);border-radius:3px;padding:10px;margin-top:6px}
.alo-final-pane.show{display:flex}
.alo-final-h{display:flex;align-items:center;gap:8px;justify-content:space-between;flex-wrap:wrap}
.alo-final-title{font-size:11px;font-weight:600;color:var(--acc,#5a9e8f);text-transform:uppercase;letter-spacing:.5px}
.alo-final-actions{display:flex;gap:5px;flex-wrap:wrap}
.alo-final-row{display:grid;grid-template-columns:90px 1fr;gap:8px;align-items:flex-start;font-size:10.5px;padding:3px 0;border-top:1px solid var(--border,#3a3530)}
.alo-final-row:first-of-type{border-top:none}
.alo-final-lbl{color:var(--dim2,#8a7e70);text-transform:uppercase;letter-spacing:.4px;font-size:9.5px;padding-top:2px}
.alo-final-val{color:var(--text2,#bfb6a8);font-family:var(--mono,monospace);font-size:10.5px;overflow-wrap:anywhere;line-height:1.5}
.alo-final-val.summary{color:var(--text,#ddd5c8);font-family:system-ui,-apple-system,Segoe UI,sans-serif;line-height:1.55;font-size:11.5px;white-space:pre-wrap}
.alo-final-cat{display:inline-block;background:rgba(160,126,193,.18);color:var(--acc4,#a07ec1);padding:2px 8px;border-radius:8px;font-family:var(--mono,monospace);font-size:10px;margin-right:5px}
.alo-final-tools{display:flex;flex-wrap:wrap;gap:3px;margin-top:3px}
.alo-final-tool{font-family:var(--mono,monospace);font-size:9.5px;background:var(--bg2,#252220);color:var(--text2,#bfb6a8);padding:1px 7px;border-radius:8px}
.alo-final-tool.ok{background:rgba(90,158,143,.12);color:var(--acc,#5a9e8f)}
.alo-final-tool.err{background:rgba(199,90,90,.12);color:var(--err,#c75a5a);text-decoration:line-through}
.alo-final-step{display:flex;flex-direction:column;gap:2px;padding:5px 7px;background:var(--bg2,#252220);border-left:2px solid var(--border2,#4a4540);border-radius:0 3px 3px 0;margin-bottom:3px;font-family:var(--mono,monospace);font-size:10px}
.alo-final-step.ok{border-left-color:var(--acc,#5a9e8f)}
.alo-final-step.err{border-left-color:var(--err,#c75a5a);opacity:.7}
.alo-final-step-h{display:flex;align-items:center;gap:6px}
.alo-final-step-tool{color:var(--acc2,#a8c87a);font-weight:500}
.alo-final-step-ms{margin-left:auto;font-size:9px;color:var(--dim2,#8a7e70)}
.alo-final-step-args{font-size:9.5px;color:var(--dim,#a89f92);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.alo-final-raw{margin-top:6px;border-top:1px dashed var(--border,#3a3530);padding-top:6px}
.alo-final-raw summary{font-size:10px;color:var(--dim,#a89f92);cursor:pointer;user-select:none}
.alo-final-raw summary:hover{color:var(--text2,#bfb6a8)}
.alo-final-raw pre{margin:5px 0 0;font-size:9.5px;max-height:240px;overflow:auto;background:var(--bg0,#181614);padding:6px;border-radius:3px;white-space:pre-wrap;word-break:break-word}

.alo-empty{color:var(--dim,#a89f92);font-style:italic;font-size:10px;padding:8px;text-align:center}
.alo-cur{display:inline-block;width:6px;height:10px;background:var(--acc2,#a8c87a);animation:alo-blink .85s step-end infinite;vertical-align:text-bottom}

/* Compact variant — slimmer paddings for chat-message embedding */
:host([compact]) .alo-cycle{padding:5px 8px;font-size:10px}
:host([compact]) .alo-cycles{padding:5px;gap:4px}
:host([compact]) .alo-final-pane{padding:7px}
:host([compact]) .alo-triage,:host([compact]) .alo-toolkit{padding:5px 8px}

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
    }

    connectedCallback(){
      if(this._mounted) return;
      this._mounted = true;
      this._render();
      // Pull initial attribute values
      if(this.hasAttribute('show-thinking')){
        this._showThinking = this.getAttribute('show-thinking') !== 'false';
      }
      this._applyMaxHeight();
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
      const mh = this.getAttribute('max-height');
      const cy = this._sr.querySelector('.alo-cycles');
      if(cy && mh) cy.style.maxHeight = (parseInt(mh,10)||400) + 'px';
    }

    _render(){
      const showTriage  = this.getAttribute('show-triage')  !== 'false';
      const showToolkit = this.getAttribute('show-toolkit') !== 'false';
      const showFinal   = this.getAttribute('show-final')   !== 'false';
      this._sr.innerHTML = `
        <style>${STYLE}</style>
        <div class="alo-root">
          <div class="alo-triage" data-part="triage" ${showTriage?'':'hidden'}>
            <div class="alo-triage-h">Triage</div>
            <div class="alo-triage-row"><span class="lbl">Category:</span><span data-part="tri-cat" style="font-family:var(--mono,monospace);color:var(--acc4,#a07ec1)">—</span></div>
            <div class="alo-triage-row"><span class="lbl">Keywords:</span><span data-part="tri-kws"></span></div>
            <div class="alo-triage-row" style="align-items:flex-start"><span class="lbl">Reasoning:</span><span data-part="tri-reason" style="flex:1;color:var(--text2,#bfb6a8);font-size:10.5px;font-style:italic">—</span></div>
          </div>
          <div class="alo-toolkit" data-part="toolkit" ${showToolkit?'':'hidden'}>
            <div class="alo-toolkit-h">
              <span>Visible toolkit</span>
              <span data-part="toolkit-count" style="color:var(--dim,#a89f92);font-size:9.5px"></span>
            </div>
            <div class="alo-toolkit-list" data-part="toolkit-list"></div>
          </div>
          <div class="alo-cycles" data-part="cycles">
            <div class="alo-empty">Waiting for events…</div>
          </div>
          <div class="alo-final-pane" data-part="final" ${showFinal?'':'hidden'}>
            <div class="alo-final-h">
              <span class="alo-final-title">Run complete</span>
              <div class="alo-final-actions" data-part="final-actions"></div>
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
      const tri = this._sr.querySelector('.alo-triage');
      if(tri) tri.classList.remove('open');
      const tk = this._sr.querySelector('.alo-toolkit');
      if(tk) tk.classList.remove('open');
      const fp = this._sr.querySelector('.alo-final-pane');
      if(fp) fp.classList.remove('show');
      const fb = this._sr.querySelector('[data-part="final-body"]');
      if(fb) fb.innerHTML = '';
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

      // think events (any variant)
      if(t === 'agent_loop.think' || t === 'agent_loop_v2.think' || t === 'agent_loop_openclaw.think'){
        if(!this._showThinking) return;
        const ref = this._cycleRefs.get(ev.cycle);
        if(!ref || !ref.el) return;
        this._appendThink(ref, ev.thought || '');
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
          if(ref){ ref.tokenBuffer=''; ref._inThinkBlock=false; ref._thinkBuffer=''; }
        });
        const el = this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-n">cycle ${ev.cycle}</span>
          <span class="alo-cycle-tool">planning…</span>
        </div>`);
        this._cycleRefs.set(ev.cycle, {el, progressEl:null, tokenBuffer:''});
        this.dispatchEvent(new CustomEvent('alo:cycle-start', {detail:{cycle:ev.cycle}, bubbles:true}));
        return;
      }

      // HITL request — pause card
      if(t === 'agent_loop_openclaw.hitl_request'){
        if(this._hitlPending.has(ev.step)) return;
        this._hitlPending.add(ev.step);
        this._showHitlPause(ev);
        this.dispatchEvent(new CustomEvent('alo:hitl-request', {detail:{cycle:ev.cycle, step:ev.step, tool:ev.tool}, bubbles:true}));
        return;
      }
      if(t === 'agent_loop_openclaw.hitl_resolved'){
        this._hitlPending.delete((ev.cycle||0) - 1);
        return;
      }

      // tool_call (any variant)
      if(t.endsWith('.tool_call')){
        this._renderToolCall(ev);
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
        const formatted = ok ? _fmtOutput(text, 600) : _esc(text||'(empty)');
        body.innerHTML = `<div class="alo-result-h ${cls}">${label}</div>
          <div class="alo-result-body ${cls}">${formatted}</div>`;
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

      // done (any variant)
      if(t.endsWith('.done')){
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">Done</span>
          ${ev.reason?`<span class="alo-cycle-status">via ${_esc(ev.reason)}</span>`:''}
        </div><div class="alo-cycle-thought">${_esc(ev.summary||'(no summary)')}</div>
        <div class="alo-cycle-preview">cycles: ${ev.cycles||'?'}</div>`, 'done');
        this.dispatchEvent(new CustomEvent('alo:done', {detail:{summary:ev.summary||'', cycles:ev.cycles, ok:!ev.error}, bubbles:true}));
        return;
      }

      // Repetition block
      if(t === 'agent_loop_openclaw.repetition_block'){
        this._cycleEl(`<div class="alo-cycle-h">
          <span class="alo-cycle-tool">Repetition blocked</span>
          <span class="alo-cycle-status" style="color:var(--warn,#c9a45a)">cycle ${ev.cycle}</span>
        </div>
        <div class="alo-cycle-thought">Forced loop break — agent was about to call <code>${_esc(ev.tool||'')}</code> again with identical args.</div>`, 'warn');
        return;
      }

      // Args coerced
      if(t === 'agent_loop_openclaw.args_coerced'){
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
    _cycleEl(html, cls){
      const host = this._sr.querySelector('.alo-cycles');
      const empty = host.querySelector('.alo-empty');
      if(empty) host.innerHTML = '';
      const d = document.createElement('div');
      d.className = 'alo-cycle' + (cls?' '+cls:'');
      d.innerHTML = html;
      host.appendChild(d);
      host.scrollTop = host.scrollHeight;
      return d;
    }

    _renderStartCard(ev){
      const version = ev.version || '';
      const versionCls = (version === 'v1' || version === 'v2' || version === 'openclaw') ? version : '';
      const goalLine = ev.goal ? `Goal: ${_esc(ev.goal)}` : '';
      const agentLine = ev.agent_name ? ` · agent: ${_esc(ev.agent_name)}` : '';
      this._cycleEl(`<div class="alo-cycle-h">
        <span class="alo-cycle-tool">Starting</span>
        ${version ? `<span class="alo-version-pill ${versionCls}">${_esc(version.toUpperCase())}</span>` : ''}
        <span class="alo-cycle-status">…</span>
      </div>${(goalLine || agentLine) ? `<div class="alo-cycle-thought">${goalLine}${agentLine}</div>` : ''}`);
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
      tk.classList.add('open');
      this._sr.querySelector('[data-part="toolkit-count"]').textContent = (list||[]).length + ' caps';
      this._sr.querySelector('[data-part="toolkit-list"]').innerHTML =
        (list||[]).map(n => `<span class="alo-tag-chip">${_esc(n)}</span>`).join('');
    }

    _appendThink(ref, text){
      let thinkEl = ref.el.querySelector('.alo-cycle-think');
      if(!thinkEl){
        thinkEl = document.createElement('details');
        thinkEl.className = 'alo-cycle-think';
        const summary = document.createElement('summary');
        summary.textContent = '💭 model thinking';
        thinkEl.appendChild(summary);
        ref.el.insertBefore(thinkEl, ref.el.querySelector('.alo-cycle-args')||ref.el.querySelector('.alo-cycle-result')||null);
      }
      const pre = document.createElement('pre');
      pre.textContent = text;
      thinkEl.appendChild(pre);
    }

    _renderToolCall(ev){
      const ref = this._cycleRefs.get(ev.cycle);
      if(!ref) return;
      const isLong = !!ev.long_running;
      const argsHtml = ev.args ? _fmtArgs(ev.args, 300) : '';
      ref.el.classList.toggle('long', isLong);

      // Update header in place — DON'T innerHTML-replace the cycle (would orphan progressEl)
      let hdr = ref.el.querySelector(':scope > .alo-cycle-h');
      const hdrHtml = `<span class="alo-cycle-n">cycle ${ev.cycle}</span>
          <span class="alo-cycle-tool">${_esc(ev.tool)}</span>
          ${isLong?`<span class="alo-version-pill openclaw" style="padding:1px 5px;font-size:8.5px">long-running</span>`:''}
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
      const ref = this._cycleRefs.get(ev.cycle);
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
        const formatted = ok ? _fmtOutput(text, 600) : _esc(text||'(empty)');
        body.innerHTML = `<div class="alo-result-h ${cls}">${label}</div>
          <div class="alo-result-body ${cls}">${formatted}</div>`;
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
      area.className = 'alo-progress-tokens';
      area.dataset.researchStream = jobId;
      strip.appendChild(area);
      return area;
    }

    _appendProgressLine(strip, kindClass, kindLabel, bodyHtml){
      const line = document.createElement('div');
      line.className = 'alo-progress-line ' + (kindClass||'');
      line.innerHTML = `<span class="pkind">${_esc(kindLabel||'')}</span><span class="pbody">${bodyHtml||''}</span>`;
      strip.appendChild(line);
      strip.scrollTop = strip.scrollHeight;
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
            area.textContent += tok;
            area.scrollTop = area.scrollHeight;
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
          if(ref._thinkBuffer && this._showThinking){
            this._appendThink(ref, ref._thinkBuffer);
          }
          ref._inThinkBlock = false;
          ref._thinkBuffer = '';
        }
        if(ref._inThinkBlock){
          ref._thinkBuffer = (ref._thinkBuffer||'') + tok;
          return;
        }
        // Suppress duplicate marker echoes
        if(tok.match(/^\n\[(plan|exec|done|auto-done|recovered) #?\d*\]/)){ return; }
        let tokenEl = strip.querySelector('.alo-progress-tokens');
        if(!tokenEl){
          tokenEl = document.createElement('div');
          tokenEl.className = 'alo-progress-tokens';
          strip.appendChild(tokenEl);
        }
        ref.tokenBuffer = (ref.tokenBuffer||'') + tok;
        tokenEl.textContent = ref.tokenBuffer.slice(-1500);
        tokenEl.scrollTop = tokenEl.scrollHeight;
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
          body = `<span style="color:var(--err,#c75a5a)">\u2717 research error: ${_esc(data.error||data.text||data.message||'unknown')}</span>`;
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
        thinkEl.scrollTop = thinkEl.scrollHeight;
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
        this._appendProgressLine(strip, '', lbl, body);
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
      streamArea.className = 'alo-progress-tokens';
      streamArea.dataset.researchJob = ev.job_id;
      streamArea.style.minHeight = '140px';
      const streamHdr = document.createElement('div');
      streamHdr.className = 'alo-progress-row';
      streamHdr.innerHTML = `<span class="alo-progress-tag" style="background:#1a2d3a;color:#5a9edd">stream</span>
        <span style="color:var(--info,#7eb8d9);font-size:9px">live research stream · job <code>${_esc((ev.job_id||'').slice(0,8))}</code><span class="alo-cur"></span></span>`;
      strip.appendChild(streamHdr);
      strip.appendChild(streamArea);
      try{
        const ws = new WebSocket(ev.ws_url);
        const _wsJobId = ev.job_id;
        this._activeWsJobs.add(_wsJobId);
        ws.onmessage = e => {
          try{
            const m = JSON.parse(e.data);
            if(m.type === 'token' || m.type === 'thinking'){
              streamArea.textContent += (m.text||'');
              streamArea.scrollTop = streamArea.scrollHeight;
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
              streamArea.textContent += '\n⚠ '+_esc(m.text||'stream error');
              this._activeWsJobs.delete(_wsJobId);
              ws.close();
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
      thinkEl.scrollTop = thinkEl.scrollHeight;
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
      pause.innerHTML = `
        <div class="alo-hitl-pause-h">
          <span class="pulse"></span>
          <span>Approval required — cycle ${ev.cycle}</span>
          <span class="alo-hitl-pause-meta" style="margin-left:auto">
            timeout in <span class="countdown">${ev.timeout_secs||300}s</span>
          </span>
        </div>
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
      if(cycles) cycles.scrollTop = cycles.scrollHeight;
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

      if(ev.handover_output){
        html += `<div class="alo-final-row">
          <div class="alo-final-lbl" style="color:var(--acc,#5a9e8f)">★ Synthesised answer</div>
          <div class="alo-final-val">
            <div class="alo-handover-body">${_renderMarkdown(ev.handover_output)}</div>
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