/* ============================================================================
 * VeraFlowCaps  —  shared capabilities palette for <vera-flow-builder>
 * ============================================================================
 * Loads the Vera capability registry (GET /workshop/cap_tree) once and exposes
 * it as flow-builder palette groups + per-cap schemas, so ANY panel's provider
 * can let users drop capabilities onto the canvas (just like the DAG Workshop).
 *
 * USAGE (inside a panel's provider.loadPalette):
 *   const caps = await window.VeraFlowCaps.paletteGroups();   // [{group,items}]
 *   return [...domainGroups, ...caps];
 *   // and in provider.schemaFor: return window.VeraFlowCaps.schemaFor(type) || ...
 *
 * Cap nodes carry the cap's param schema, so the element's generic wiring
 * section (inputs ← state, output key, output_map, condition) works on them and
 * el.toDag() turns the graph into runnable DAG tuples for /workshop/dag/run_stream.
 *
 * API
 *   VeraFlowCaps.setBase(url)            — orchestrator base (default same-origin)
 *   await VeraFlowCaps.paletteGroups(f)  — palette groups (f=true forces reload)
 *   VeraFlowCaps.schemaFor(type)         — cached cap descriptor or null
 *   VeraFlowCaps.isCap(type)             — true if `type` is a known cap
 *   await VeraFlowCaps.ioSchema(name)    — lazy /workshop/cap_io_schema (enums/output_keys)
 * ============================================================================ */
(function(){
  if(window.VeraFlowCaps) return;

  let _base  = (window._veraBase || '');
  let _cache = null;   // { groups:[...], byName:{name:cap} }
  let _io    = {};     // name -> io schema (lazy)
  let _inflight = null;

  async function _json(path, opts){
    const r = await fetch(_base + path, opts);
    if(!r.ok) throw new Error('HTTP '+r.status);
    return r.json();
  }

  async function _load(force){
    if(_cache && !force) return _cache;
    if(_inflight && !force) return _inflight;
    _inflight = (async ()=>{
      const res = await _json('/workshop/cap_tree');
      const byName = {};
      const groups = ((res && res.groups) || []).map(g=>({
        group: g.prefix || g.group || 'caps',
        items: (g.caps || []).map(c=>{
          byName[c.name] = c;
          return { type:c.name, label:c.name, description:c.description||'',
                   schema:c, long_running:!!c.long_running, isCap:true };
        })
      })).filter(g=>g.items.length);
      _cache = { groups, byName };
      _inflight = null;
      return _cache;
    })();
    return _inflight;
  }

  window.VeraFlowCaps = {
    setBase(url){ _base = url || ''; },
    async paletteGroups(force){ try { return (await _load(force)).groups; } catch(e){ return []; } },
    schemaFor(type){ return _cache ? (_cache.byName[type] || null) : null; },
    isCap(type){ return !!(_cache && _cache.byName[type]); },
    async ioSchema(name){
      if(_io[name]) return _io[name];
      try{
        const io = await _json('/workshop/cap_io_schema', {
          method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name})
        });
        _io[name] = io; return io;
      }catch(_){ return null; }
    }
  };
})();
