// the DOM stub genfx_gitgraph.cjs ran the element in (tests/test_w3_git_graph.cjs runs the current element in the same one)
'use strict';
const vm = require('vm');
module.exports = function harness(src) {
  let Cls = null; const els = { wrap: { innerHTML: '' }, count: { textContent: '' } };
  class HE { attachShadow() { return (this.shadowRoot = { appendChild() {}, getElementById: (id) => els[id] }); } hasAttribute(n) { return !!(this._attrs && n in this._attrs); } getAttribute(n) { return this._attrs && n in this._attrs ? this._attrs[n] : null; } }
  const ctx = { window: {}, HTMLElement: HE, customElements: { get: () => false, define: (n, c) => { Cls = c; } }, document: { createElement: () => ({ innerHTML: '', content: { cloneNode: () => ({}) } }) }, Math, String, Object, JSON, parseInt };
  vm.createContext(ctx); vm.runInContext(src, ctx);
  return (commits, error, attrs) => { const g = new Cls(); g._attrs = attrs || {}; g._render(commits, error); return { rows: els.wrap.innerHTML, count: els.count.textContent }; };
};
