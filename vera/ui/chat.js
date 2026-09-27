/* vera/ui/chat.js — the chat UI as an ELEMENT (served /ui/chat.js): <vera-chat> places the real chat anywhere.
   ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────
   Owner, 2026-09-27: "can the chat UI itself be defined as a widget ... so they can be re-used elsewhere in the UI - ...
   there are other chat UIs in other panels and they could all use the same ui element but with different agents and
   system prompts per implementation."

   <vera-chat agent="aide" system="You answer about the estate only." session="estate-help" title="Estate helper"></vera-chat>

   It IS the chat - /chat_panel in its compact embed mode (the transcript and the composer; its streaming, capability
   cards, canvas hand-off, motion, themes and the one design come with it) - so every chat in the product is the same
   element and improves at once. Attributes (all optional; changing one re-points the chat):
     agent    the agent to talk to (default: the user's default agent)
     system   a system prompt for THIS placement - put before the agent's own (the chat's system_prefix)
     session  the conversation to open/keep (default: a new one); the same id resumes it
     title    the page title the chat shows
     base     the backend origin when the page is served from elsewhere
   Styling: give the element a size (it fills it). The frame is transparent over the page's own ground.
   Also: VeraChat.mount(host, {agent, system, session, title}) -> the element.                                          */
(function () {
  'use strict';
  if (window.customElements && customElements.get('vera-chat')) return;
  var ATTRS = ['agent', 'system', 'session', 'title'];
  function url(el){
    var p = new URLSearchParams({ only: 'chat', embed: '1' });
    ATTRS.forEach(function(k){ var v = el.getAttribute(k); if (v) p.set(k, v); });
    return (el.getAttribute('base') || '') + '/chat_panel?' + p.toString();
  }
  class VeraChatElement extends HTMLElement {
    static get observedAttributes(){ return ATTRS.concat(['base']); }
    connectedCallback(){ this._render(); }
    attributeChangedCallback(){ if (this.isConnected) this._render(); }
    _render(){
      var u = url(this);
      if (this._fr && this._fr.dataset.u === u) return;
      if (!this._fr){
        if (!this.style.display) this.style.display = 'block';
        this._fr = document.createElement('iframe');
        this._fr.setAttribute('title', 'chat');
        this._fr.setAttribute('allow', 'clipboard-read; clipboard-write; microphone');
        this._fr.style.cssText = 'width:100%;height:100%;border:0;display:block;background:transparent';
        this.appendChild(this._fr);
      }
      this._fr.dataset.u = u; this._fr.src = u;
    }
    get frame(){ return this._fr || null; }
  }
  customElements.define('vera-chat', VeraChatElement);
  window.VeraChat = {
    mount: function(host, o){ o = o || {}; var el = document.createElement('vera-chat'); ATTRS.concat(['base']).forEach(function(k){ if (o[k]) el.setAttribute(k, o[k]); }); if (host) host.appendChild(el); return el; },
    url: url
  };
})();
