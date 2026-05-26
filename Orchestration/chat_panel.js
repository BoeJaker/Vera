/**
 * vera_chat_embed.js
 * ══════════════════
 * Utilities for embedding the Vera Chat Panel into other Vera UI panels.
 *
 * Usage modes:
 *   1. Full-feature chat tab    — all controls visible
 *   2. Agent-fixed sidebar      — fixed agent, no selector, no history
 *   3. IDE context-aware chat   — reads parent tab content, injects it
 *   4. Minimal floating widget  — just the message + input, no rails
 *
 * Quick embed:
 *   <div id="myChat"></div>
 *   <script>
 *     const mount = VeraChatEmbed.mount('#myChat', {
 *       agentFixed: 'code-reviewer',
 *       showHistory: false,
 *       showCtxGraph: false,
 *       height: '400px',
 *     });
 *     // Later, push parent tab context into the chat:
 *     mount.setContext(editor.getSelectedText());
 *   </script>
 */

window.VeraChatEmbed = (() => {

  /**
   * Mount a Vera Chat Panel into a container element.
   *
   * @param {string|Element} container  CSS selector or DOM element
   * @param {Object} opts
   *   baseUrl        {string}  Vera API base URL (default: localStorage vera_base or localhost:8000)
   *   agentFixed     {string}  Lock to a specific agent name — hides agent selector
   *   capSet         {Array}   Restrict DAG caps to this list
   *   showAgentSel   {boolean} Show agent selector bar (default true)
   *   showHistory    {boolean} Show left-rail history panel (default true)
   *   showCtxGraph   {boolean} Show right-rail context graph (default true)
   *   showTts        {boolean} Show TTS controls (default true)
   *   showDag        {boolean} Show DAG planning button (default true)
   *   sessionId      {string}  Reuse an existing session ID
   *   parentContext  {string}  Initial context text from parent tab
   *   height         {string}  CSS height for the iframe (default '100%')
   *   onMessage      {fn}      Callback when a message is sent: (role, text) => void
   *
   * @returns {{ setContext, setAgent, send, destroy, frame }}
   */
  function mount(container, opts = {}) {
    const el = typeof container === 'string' ? document.querySelector(container) : container;
    if (!el) throw new Error('VeraChatEmbed: container not found: ' + container);

    const iframe = document.createElement('iframe');
    iframe.src = (opts.panelUrl || '/chat_panel.html');
    iframe.style.cssText = `
      width:100%;
      height:${opts.height || '100%'};
      border:none;
      display:block;
      background:var(--bg0,#181614);
    `;
    iframe.setAttribute('allowtransparency', 'true');
    el.appendChild(iframe);

    // When the iframe loads, initialise CP with our options
    iframe.addEventListener('load', () => {
      const cp = iframe.contentWindow?.CP;
      if (!cp) return;

      // Mark as embedded so it doesn't auto-init
      iframe.contentWindow._veraEmbedded = true;

      cp.init({
        baseUrl:      opts.baseUrl || localStorage.getItem('vera_base') || 'http://localhost:8000',
        agentFixed:   opts.agentFixed,
        capSet:       opts.capSet,
        showAgentSel: opts.showAgentSel !== false,
        showHistory:  opts.showHistory  !== false,
        showCtxGraph: opts.showCtxGraph !== false,
        showTts:      opts.showTts      !== false,
        showDag:      opts.showDag      !== false,
        sessionId:    opts.sessionId,
        parentContext: opts.parentContext || '',
      });

      if (opts.onMessage) {
        // Patch the send function to fire the callback
        const origSend = cp.send.bind(cp);
        // (The panel fires messages internally; we observe via postMessage below)
      }
    });

    // Listen for postMessages from the panel (future extensibility)
    const onMsg = (e) => {
      if (e.source !== iframe.contentWindow) return;
      if (e.data?.type === 'vera:message' && opts.onMessage) {
        opts.onMessage(e.data.role, e.data.text);
      }
    };
    window.addEventListener('message', onMsg);

    return {
      frame: iframe,

      /** Push context text from the parent tab into the chat */
      setContext(text) {
        iframe.contentWindow?.CP?.setContext(text);
      },

      /** Switch to a different agent */
      setAgent(name) {
        iframe.contentWindow?.CP?.setAgent(name);
      },

      /** Programmatically send a message */
      send(msg) {
        iframe.contentWindow?.CP?.send(msg);
      },

      /** Change the base URL at runtime */
      setBase(url) {
        iframe.contentWindow?.CP?.setBase(url);
      },

      /** Trigger a context refresh */
      refreshContext(query) {
        iframe.contentWindow?.CP?.ctxFetch(true, query || '');
      },

      /** Remove the panel entirely */
      destroy() {
        window.removeEventListener('message', onMsg);
        el.removeChild(iframe);
      },
    };
  }

  // ── Preset configurations ──────────────────────────────────────────

  /**
   * Mount a minimal chat bar (no rails, no history, no ctx graph).
   * Good for: quick-ask widgets, floating assistants.
   */
  function mountMinimal(container, opts = {}) {
    return mount(container, {
      showHistory:  false,
      showCtxGraph: false,
      showDag:      false,
      height:       opts.height || '280px',
      ...opts,
    });
  }

  /**
   * Mount in IDE tab — reads context from an editor element.
   * Automatically pushes selected text / visible text into the chat context.
   *
   * @param {string|Element} container
   * @param {string|Element} editorEl   The editor element (CodeMirror, textarea, etc.)
   * @param {Object} opts
   */
  function mountForIde(container, editorEl, opts = {}) {
    const editor = typeof editorEl === 'string' ? document.querySelector(editorEl) : editorEl;

    const panel = mount(container, {
      agentFixed:  opts.agentFixed || 'code-reviewer',
      showHistory: opts.showHistory !== false,
      showCtxGraph: opts.showCtxGraph !== false,
      ...opts,
    });

    // Push context when editor content changes
    let _timer = null;
    const pushContext = () => {
      let text = '';
      // CodeMirror 6 / Monaco
      if (editor?.state?.doc) text = editor.state.doc.toString().slice(0, 4000);
      // CodeMirror 5
      else if (editor?.getValue) text = editor.getValue().slice(0, 4000);
      // Plain textarea
      else if (editor?.value !== undefined) text = editor.value.slice(0, 4000);
      // contenteditable
      else if (editor?.textContent) text = editor.textContent.slice(0, 4000);
      if (text) panel.setContext(text);
    };

    // Selection-based context
    const pushSelection = () => {
      const sel = window.getSelection()?.toString();
      if (sel && sel.length > 10) panel.setContext(sel.slice(0, 2000));
    };

    if (editor) {
      editor.addEventListener?.('input', () => {
        clearTimeout(_timer);
        _timer = setTimeout(pushContext, 1000);
      });
      editor.addEventListener?.('mouseup', pushSelection);
      editor.addEventListener?.('keyup', e => { if(e.shiftKey) pushSelection(); });
      pushContext(); // initial
    }

    return { ...panel, pushContext, pushSelection };
  }

  /**
   * Mount an agent-fixed sidebar chat (e.g., research assistant next to a results view).
   */
  function mountSidebar(container, agentName, opts = {}) {
    return mount(container, {
      agentFixed:   agentName,
      showHistory:  false,
      showCtxGraph: opts.showCtxGraph !== false,
      showDag:      opts.showDag !== false,
      height:       opts.height || '100%',
      ...opts,
    });
  }

  return { mount, mountMinimal, mountForIde, mountSidebar };
})();

/* ─────────────────────────────────────────────────────────────────
   INTEGRATION GUIDE: Adding to capability_orchestration.html
   ─────────────────────────────────────────────────────────────────

1. Add the Chat tab entry:

   <div class="tab" onclick="showPanel('panel-chat2')" id="tab-chat2">Chat</div>

2. Add the panel div:

   <div class="panel" id="panel-chat2" style="padding:0;overflow:hidden">
     <div id="chatPanelMount" style="height:100%"></div>
   </div>

3. In the onConnect / init block:

   const chatMount = VeraChatEmbed.mount('#chatPanelMount', {
     baseUrl: VERA_BASE,
     showHistory: true,
     showCtxGraph: true,
   });

4. For IDE tab integration, after the editor is set up:

   const ideChat = VeraChatEmbed.mountForIde(
     '#ideChatMount',
     document.getElementById('codeEditor'),
     { agentFixed: 'code-reviewer', baseUrl: VERA_BASE }
   );

5. For the existing fabric/research panels:

   const fabricChat = VeraChatEmbed.mountSidebar(
     '#fabricChatMount', 'analyst',
     { parentContext: 'You are assisting with data fabric queries.', baseUrl: VERA_BASE }
   );

─────────────────────────────────────────────────────────────────
   AGENT MODEL FIX — what changed in agents.py
─────────────────────────────────────────────────────────────────

The bug: When selecting an agent in the old chat UI, the model dropdown
defaulted to "" (agent default), but the streaming endpoint was reading
model_override="" and then falling back to OLLAMA_MODEL (the system default)
instead of the agent's own model field.

Fix applied in vera_chat_panel.html sendMessage():

  // OLD (broken): always sends empty string if user hasn't touched the dropdown
  const modelOverride = document.getElementById('cfgModel')?.value || '';

  // NEW (fixed): explicitly reads agent.model and uses it unless manually overridden
  const modelOverride = document.getElementById('cfgModel')?.value || '';
  const effectiveModel = modelOverride || (a?.model) || '';
  // effectiveModel is sent as model_override — the backend uses it if non-empty

On the backend side (agents.py agent_chat_stream_endpoint), ensure:

  if body.get("model_override"):
      agent.model = body["model_override"]
  # This means if effectiveModel = agent.model, the agent uses its own model ✓
  # If effectiveModel = "" (agent has no model set), falls back to OLLAMA_MODEL ✓
  # If effectiveModel = user-selected model, that takes priority ✓

─────────────────────────────────────────────────────────────────
   CONTEXT GRAPH ARCHITECTURE
─────────────────────────────────────────────────────────────────

The context graph panel queries two sources simultaneously:

  VECTOR (purple nodes/edges):
    POST /memory/query { retrieval_type: 'vector', ... }
    → Chroma/FAISS semantic similarity search
    → Nodes sized by similarity score
    → Dashed purple edges = SIMILAR relationship

  GRAPH (orange nodes/edges):
    POST /memory/query { retrieval_type: 'graph', ... }
    → Neo4j relationship traversal
    → Solid orange edges = explicit graph relationships
    → Node with both sources shown with green dot overlay

  FRAMES:
    Each context snapshot is saved as a frame.
    Frames are stored in memory per session.
    Loading a frame restores the node/edge state for that moment.
    The frame strip shows the 5 most recent frames.
    The Frames tab shows all frames with labels and node counts.

─────────────────────────────────────────────────────────────────
*/