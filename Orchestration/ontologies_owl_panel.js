/* ============================================================================
 * vera_ontologies_owl_panel.js
 * ----------------------------------------------------------------------------
 * Append to the bottom of ontologies_panel.html (after the existing OP IIFE).
 * Adds an OWL import/export tab to the Ontologies panel.
 * ==========================================================================*/
(function(){
  'use strict';
  if (!window.OP) {
    console.warn('[OntoOWL] OP not found — patch skipped');
    return;
  }

  function $(id){ return document.getElementById(id); }
  function api(path, method, body){
    const opts = {method:method||'GET'};
    if (body){ opts.headers={'Content-Type':'application/json'}; opts.body=JSON.stringify(body); }
    return fetch((window._veraBase||'') + path, opts).then(r=>r.json()).catch(e=>({error:e.message}));
  }
  function escapeHtml(s){
    return String(s||'').replace(/[&<>"']/g, c =>
      ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  function injectTab(){
    // Find the tab bar in the panel — it lives inside a .tab-row near the
    // top of the editor.  Locate by querying for buttons with onclick that
    // toggles tabs (apply / infer / graph).
    const tabBar = document.querySelector('.tab-row, .tabs');
    if (!tabBar || $('tab-owl')) return;

    // Add a tab button.  We lean on whatever pattern the panel already
    // uses — guess by inspecting siblings.
    const existing = tabBar.querySelector('button, .tab');
    const tag = existing?.tagName?.toLowerCase() || 'button';
    const cls = existing?.className || 'tab';

    const btn = document.createElement(tag);
    btn.className = cls;
    btn.textContent = 'OWL';
    btn.title = 'Import / export the selected ontology as OWL/RDF';
    btn.onclick = () => owlSwitch();
    tabBar.appendChild(btn);

    // Build the tab pane.  Append after the last existing tab pane so it
    // shares the same container.
    const root = document.querySelector('#tab-graph')?.parentElement
              || tabBar.parentElement;
    if (!root) return;
    const pane = document.createElement('div');
    pane.id = 'tab-owl';
    pane.style.display = 'none';
    pane.innerHTML = `
      <div style="background:var(--bg2);border:1px solid var(--border);border-radius:5px;padding:12px">
        <div style="font-size:11px;font-weight:600;color:var(--acc);margin-bottom:6px">Export to OWL / RDF</div>
        <p style="font-size:10.5px;color:var(--dim2);line-height:1.6;margin-bottom:8px">
          Render the selected ontology as a W3C-compatible OWL document.
          Entities map to <code>owl:Class</code>, relationships to
          <code>owl:ObjectProperty</code> with rdfs:domain/range, attributes
          to <code>owl:DatatypeProperty</code>, processing rules and memory
          slots to vera-vocab individuals.
        </p>
        <div style="display:flex;gap:6px;align-items:center;margin-bottom:8px">
          <label style="font-size:10px;color:var(--dim2)">Format</label>
          <select id="owl-format" style="font-size:10.5px">
            <option value="turtle">Turtle (.ttl)</option>
            <option value="rdfxml">RDF/XML (.owl)</option>
            <option value="json-ld">JSON-LD</option>
            <option value="ntriples">N-Triples</option>
          </select>
          <button class="btn pri" onclick="window.OPX.owlExport()">Export</button>
          <button class="btn"     onclick="window.OPX.owlDownload()">Download</button>
          <span class="status" id="owl-export-status" style="margin-left:6px"></span>
        </div>
        <textarea id="owl-export-output" rows="14" style="width:100%;font-family:var(--mono);font-size:10.5px;background:var(--bg0);color:var(--text);border:1px solid var(--border);border-radius:3px;padding:8px"></textarea>
      </div>

      <div style="background:var(--bg2);border:1px solid var(--border);border-radius:5px;padding:12px;margin-top:14px">
        <div style="font-size:11px;font-weight:600;color:var(--acc);margin-bottom:6px">Import OWL / RDF</div>
        <p style="font-size:10.5px;color:var(--dim2);line-height:1.6;margin-bottom:8px">
          Paste any valid OWL/RDF document.  Format will be auto-detected if
          left as <em>auto</em>.  Existing ontology id (optional) updates in
          place; otherwise a new ontology is created.
        </p>
        <div style="display:flex;gap:6px;align-items:center;margin-bottom:6px">
          <label style="font-size:10px;color:var(--dim2)">Format</label>
          <select id="owl-import-format" style="font-size:10.5px">
            <option value="">Auto-detect</option>
            <option value="turtle">Turtle</option>
            <option value="rdfxml">RDF/XML</option>
            <option value="json-ld">JSON-LD</option>
            <option value="ntriples">N-Triples</option>
          </select>
          <label style="font-size:10px;color:var(--dim2);margin-left:10px">Update id (optional)</label>
          <input id="owl-import-id" placeholder="leave blank for new" style="flex:1;font-size:10.5px">
          <input id="owl-import-name" placeholder="Fallback name" style="flex:1;font-size:10.5px">
        </div>
        <div style="display:flex;gap:6px;align-items:center;margin-bottom:6px">
          <input type="file" id="owl-import-file" accept=".ttl,.owl,.rdf,.jsonld,.nt,.n3,.xml,application/rdf+xml,text/turtle"
                 onchange="window.OPX.owlReadFile(this)">
          <button class="btn pri" onclick="window.OPX.owlImport()">Import</button>
          <span class="status" id="owl-import-status"></span>
        </div>
        <textarea id="owl-import-content" rows="10" placeholder="Paste OWL/RDF here…" style="width:100%;font-family:var(--mono);font-size:10.5px;background:var(--bg0);color:var(--text);border:1px solid var(--border);border-radius:3px;padding:8px"></textarea>
      </div>

      <div id="owl-fallback-warning" style="display:none;margin-top:8px;padding:8px;background:rgba(200,100,80,.08);border:1px solid var(--err);border-radius:3px;font-size:10.5px;color:var(--err)">
        ⚠ rdflib is not installed on the server — OWL import/export is unavailable.
        Install with <code>pip install rdflib</code> and reload.
      </div>
    `;
    root.appendChild(pane);

    // Probe for rdflib availability
    api('/ontologies/owl/formats').then(r=>{
      if (r && r.rdflib_available === false) {
        $('owl-fallback-warning').style.display = 'block';
      }
    });
  }

  function owlSwitch(){
    document.querySelectorAll('[id^="tab-"]').forEach(p=>{
      if (['tab-edit','tab-list','tab-apply','tab-infer','tab-graph','tab-owl'].includes(p.id))
        p.style.display = 'none';
    });
    const t = $('tab-owl'); if (t) t.style.display = 'block';
  }

  async function owlExport(){
    const id = OP._activeId || $('f-id')?.value;
    const fmt = $('owl-format').value;
    const out = $('owl-export-output');
    const st  = $('owl-export-status');
    if (!id) { st.textContent='Select an ontology first'; st.className='status err'; return; }
    st.textContent='Exporting…'; st.className='status';
    const r = await api('/ontologies/owl/export', 'POST', {id, format:fmt});
    if (!r || r.error){ st.textContent='Failed: '+(r?.error||'?'); st.className='status err'; return; }
    out.value = r.content || '';
    st.textContent = `${r.triples||0} triples · ${r.length||0} chars`; st.className='status ok';
  }

  function owlDownload(){
    const fmt = $('owl-format').value;
    const id  = OP._activeId || $('f-id')?.value || 'ontology';
    const content = $('owl-export-output').value;
    if (!content) return;
    const ext = ({turtle:'ttl', rdfxml:'owl', 'json-ld':'jsonld', ntriples:'nt'})[fmt] || 'txt';
    const mime = ({turtle:'text/turtle', rdfxml:'application/rdf+xml',
                   'json-ld':'application/ld+json', ntriples:'application/n-triples'})[fmt] || 'text/plain';
    const blob = new Blob([content], {type:mime});
    const url  = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = id + '.' + ext;
    a.click();
    URL.revokeObjectURL(url);
  }

  function owlReadFile(input){
    const f = input.files?.[0]; if (!f) return;
    const r = new FileReader();
    r.onload = e => {
      $('owl-import-content').value = e.target.result;
      // Guess format from filename if user hasn't picked one.
      const sel = $('owl-import-format');
      if (sel && !sel.value){
        const n = f.name.toLowerCase();
        if (n.endsWith('.ttl'))                sel.value = 'turtle';
        else if (n.endsWith('.owl') || n.endsWith('.rdf') || n.endsWith('.xml'))
                                                sel.value = 'rdfxml';
        else if (n.endsWith('.jsonld') || n.endsWith('.json'))
                                                sel.value = 'json-ld';
        else if (n.endsWith('.nt'))            sel.value = 'ntriples';
      }
      if ($('owl-import-name') && !$('owl-import-name').value){
        $('owl-import-name').value = f.name.replace(/\.[^.]+$/,'');
      }
    };
    r.readAsText(f);
  }

  async function owlImport(){
    const content = $('owl-import-content').value;
    const fmt     = $('owl-import-format').value;
    const id      = $('owl-import-id').value;
    const name    = $('owl-import-name').value;
    const st      = $('owl-import-status');
    if (!content.trim()) { st.textContent='Paste or pick a file'; st.className='status err'; return; }
    st.textContent='Importing…'; st.className='status';
    const r = await api('/ontologies/owl/import', 'POST',
      {content, format:fmt, id, name});
    if (!r || r.error){ st.textContent='Failed: '+(r?.error||'?'); st.className='status err'; return; }
    st.textContent='✓ Imported'; st.className='status ok';
    if (typeof OP.load === 'function') OP.load();
  }

  window.OPX = {owlExport, owlDownload, owlImport, owlReadFile};

  if (document.readyState === 'loading')
    document.addEventListener('DOMContentLoaded', injectTab);
  else
    setTimeout(injectTab, 100);
})();