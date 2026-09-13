/* ═══════════════════════════════════════════════════════════════════════════
 * vera-lhm.js — the ONE left-hand menu (UI redesign, Notes/40 §2 + §9; the
 * Canvas / ChatMenu / Harness boards).
 *
 * A quick-menu LHM is a 46px icon RAIL of menus beside a 272px DETAIL column:
 * a HEADER (title · meta · ✎), the menu's TAB STRIP, its panes, and one CTA.
 * ☰ at the top of the rail swaps the detail column to the TOP-LEVEL LIST —
 * every menu, and the panels open right now — IN PLACE; picking a row swaps
 * back. Every part is a widget: rail, header, tab strip, list, CTA carry
 * data-w="label · form" so edit mode (✎) can outline and name them, and a
 * later slice can give each a record.
 *
 * The same code serves two hosts:
 *   • a page that OWNS the menu (the chat): VeraLHM.mount({host, menus, …})
 *     builds the rail and the header around the page's existing tab strip and
 *     panes — nothing is re-implemented, nothing is removed; the existing
 *     tabs keep their handlers, they are only grouped under the rail's menus.
 *   • a page that HOSTS another page's menu (the harness with the chat open):
 *     VeraLHM.absorb(host, spec, pick) renders the same rail + tab strip from
 *     the spec the owner published, and pick() sends the choice back. The
 *     owner is told it is hosted (vera:panel:nav_hosted) and hides its own
 *     rail — one LHM, not two.
 *
 * Owner ↔ host protocol = the existing panel bridge messages (see
 * vera-panel-bridge.js): the owner publishes vera:panel:state with
 * state.nav = {items, active, lhm:spec}; the host answers nav_hosted /
 * nav_unhosted and dispatches vera:panel:action {action:'nav_select',
 * payload:{id}} with id = '<menu>' or '<menu>/<tab>'. The owner publishes
 * ONLY when it is embedded (window.parent !== window): a standalone chat
 * would otherwise receive its own message on the listener it keeps for the
 * panels IT hosts.
 * ═══════════════════════════════════════════════════════════════════════ */
(function(){
  if(window.VeraLHM) return;   // idempotent

  var CSS = [
    /* the rail */
    '.lhm-rail{width:46px;flex:0 0 46px;display:flex;flex-direction:column;align-items:center;gap:2px;padding:6px 0;background:var(--bg1);border-right:1px solid var(--border);box-sizing:border-box;overflow:hidden}',
    'html[data-blocks="off"] .lhm-rail{background:transparent;border-right-color:transparent}',
    '.lhm-rail .lhm-ico{position:relative;width:34px;height:34px;display:flex;align-items:center;justify-content:center;border-radius:var(--r-sm,7px);color:var(--dim2);font-size:15px;line-height:1;cursor:pointer;user-select:none;border:1px solid transparent;flex-shrink:0}',
    '.lhm-rail .lhm-ico:hover{color:var(--text);background:var(--bg2)}',
    '.lhm-rail .lhm-ico.on{color:var(--acc);background:var(--bg2);border-color:var(--border)}',
    '.lhm-rail .lhm-ico.top{font-size:16px;margin-bottom:4px}',
    '.lhm-rail .lhm-ico.top.on{color:var(--text)}',
    '.lhm-rail .lhm-ico .lhm-badge{position:absolute;top:-3px;right:-3px;min-width:14px;height:14px;padding:0 3px;border-radius:7px;background:var(--acc);color:var(--on-acc,#fff);font-family:var(--mono);font-size:8px;font-weight:700;line-height:14px;text-align:center;box-sizing:border-box}',
    '.lhm-rail .lhm-sp{flex:1}',
    /* the detail column: whatever the owner already had, wrapped */
    '.lhm-det{flex:1;display:flex;flex-direction:column;min-width:0;min-height:0;overflow:hidden}',
    '.lhm-hd{display:flex;align-items:baseline;gap:6px;padding:8px 9px 5px;border-bottom:1px solid var(--border);flex-shrink:0}',
    'html[data-blocks="off"] .lhm-hd{border-bottom-color:transparent}',
    '.lhm-hd h2{margin:0;font-family:var(--f-disp,var(--sans));font-size:12.5px;font-weight:600;color:var(--text);letter-spacing:-.1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
    '.lhm-hd .lhm-meta{flex:1;min-width:0;font-family:var(--mono);font-size:9px;color:var(--dim2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
    '.lhm-hd .lhm-edit{flex:0 0 auto;background:transparent;border:1px solid transparent;border-radius:var(--r-sm,4px);color:var(--dim2);font-size:11px;line-height:1;padding:2px 5px;cursor:pointer}',
    '.lhm-hd .lhm-edit:hover{color:var(--text);border-color:var(--border)}',
    '.lhm-hd .lhm-edit.on{color:var(--acc);border-color:var(--acc)}',
/* the quick body (the Canvas board: the menu as a list of widgets) and the switch to the menu's full pane, in place */
'.lhm-quick{flex:1;min-height:0;overflow:auto;padding:4px 9px 8px;display:flex;flex-direction:column;gap:7px}',
'.lhm-quickmode .lhm-det > :not(.lhm-hd):not(.lhm-quick):not(.lhm-cta):not(.lhm-top):not(.lhm-wcfg){display:none!important}',
'.lhm-hd .lhm-deep{display:none;flex:0 0 auto;background:transparent;border:1px solid transparent;border-radius:var(--r-sm,4px);color:var(--dim2);font-size:9.5px;line-height:1;padding:3px 6px;cursor:pointer;white-space:nowrap}',
'.lhm-hd .lhm-deep.has{display:inline-block}.lhm-hd .lhm-deep:hover{color:var(--text);border-color:var(--border)}',
'.lhm-topmode .lhm-hd .lhm-deep{display:none!important}',
    /* the tab strip the owner already had: only the current menu\'s tabs show */
    '.lhm-det .ctab.lhm-off{display:none!important}',
    /* the top-level list */
    '.lhm-top{display:none;flex-direction:column;gap:2px;padding:6px;overflow-y:auto;flex:1;min-height:0}',
    '.lhm-topmode .lhm-top{display:flex}',
    '.lhm-topmode .lhm-det > :not(.lhm-hd):not(.lhm-top){display:none!important}',
    '.lhm-top .lhm-sec{font-family:var(--mono);font-size:8px;text-transform:uppercase;letter-spacing:1px;color:var(--dim);padding:6px 4px 3px}',
    '.lhm-top .lhm-row{display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:var(--r-sm,6px);cursor:pointer;color:var(--text);font-size:11px;border:1px solid transparent}',
    '.lhm-top .lhm-row:hover{background:var(--bg2);border-color:var(--border)}',
    '.lhm-top .lhm-row.on{color:var(--acc)}',
    '.lhm-top .lhm-row .lhm-ri{width:18px;text-align:center;color:var(--dim2);font-size:13px;flex-shrink:0}',
    '.lhm-top .lhm-row .lhm-rn{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
    '.lhm-top .lhm-row .lhm-rm{font-family:var(--mono);font-size:8.5px;color:var(--dim2);white-space:nowrap}',
    '.lhm-top .lhm-row .lhm-rx{flex:0 0 auto;background:transparent;border:none;color:var(--dim2);font-size:11px;cursor:pointer;padding:0 2px}',
    '.lhm-top .lhm-row .lhm-rx:hover{color:var(--err)}',
    '.lhm-top .lhm-empty{padding:4px 8px;font-size:10px;color:var(--dim)}',
    /* the CTA */
    '.lhm-cta{flex-shrink:0;margin:6px 7px 7px;padding:6px 10px;border:1px solid var(--border);border-radius:var(--r-sm,6px);background:var(--bg2);color:var(--text);font-family:var(--sans);font-size:10.5px;text-align:left;cursor:pointer}',
    '.lhm-cta:hover{border-color:var(--acc);color:var(--acc)}',
    '.lhm-topmode .lhm-cta{display:none!important}',
'.lhm-hd .lhm-fold{flex:0 0 auto;width:22px;height:22px;border:1px solid transparent;border-radius:var(--r-sm,6px);background:transparent;color:var(--dim2);font:inherit;font-size:14px;line-height:1;cursor:pointer;padding:0}',
'.lhm-hd .lhm-fold:hover{color:var(--text);border-color:var(--border);background:var(--bg2)}',
    /* every part is a widget: edit mode outlines and names them; ⚙ opens the part's record, ⧉ saves it as a template */
    '.lhm-wbar{display:none;position:absolute;top:2px;right:4px;z-index:6;gap:2px}',
/* the quick body in edit mode (the ChatMenu board): outline + name on every widget, the bar ⋮⋮ ⚙ ⧉ ✕, the foot, the picker */
'.lhm-editing .lhm-quick .wid{outline:1px dashed color-mix(in srgb,var(--acc) 65%,transparent);outline-offset:4px;margin-top:10px;position:relative}',
'.lhm-editing .lhm-quick .wid::before{content:attr(data-w);position:absolute;left:4px;top:-11px;z-index:5;font-family:var(--mono);font-size:7.5px;letter-spacing:.04em;color:var(--acc);background:var(--bg1);padding:0 5px;border-radius:99px;white-space:nowrap;pointer-events:none}',
'.lhm-editing .lhm-quick .wid.lhm-removed{opacity:.35}',
'.lhm-quick .wid.lhm-off{display:none}',
'.lhm-wbar.lhm-qbar{top:-11px;right:2px;align-items:center;padding:1px 3px;border-radius:99px;background:var(--bg1);box-shadow:0 0 0 1px var(--border)}',
'.lhm-wbar.lhm-qbar b{font-size:9px;color:var(--dim2);cursor:grab;letter-spacing:-.1em;padding:0 3px}',
'.lhm-wbar.lhm-qbar button{width:18px;height:18px;border:none;border-radius:50%;background:transparent;color:var(--dim2);font-size:10px;display:inline-flex;align-items:center;justify-content:center}',
'.lhm-wbar.lhm-qbar button:hover{color:var(--acc);background:color-mix(in srgb,var(--acc) 14%,transparent)}',
'.lhm-wbar.lhm-qbar button.saved{color:var(--acc2,var(--acc))}',
'.lhm-quick .wid.lhm-dragging{opacity:.4}.lhm-quick .wid.lhm-dropover{outline-color:var(--acc);outline-style:solid}',
'.lhm-quick .lhm-added-grp{margin-top:8px}',
'.lhm-quick .wid.lhm-added{background:var(--bg2);border-radius:var(--r-sm,6px);padding:8px 10px;display:flex;flex-direction:column;gap:6px}',
'.lhm-quick .lhm-added-cap{display:flex;align-items:center;gap:7px;font-size:10.5px;color:var(--text)}.lhm-quick .lhm-added-cap b{font-weight:600;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.lhm-quick .lhm-added-cap .mono{font-family:var(--mono);font-size:9px;color:var(--dim2)}',
'.lhm-wedit{display:flex;flex-direction:column;gap:5px;margin-top:10px;flex-shrink:0}',
'.lhm-ebar{display:none;align-items:center;gap:6px;flex-shrink:0;margin:0 0 8px;padding:5px 6px 5px 9px;border-radius:var(--r-sm,6px);background:color-mix(in srgb,var(--acc) 12%,transparent);box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--acc) 45%,transparent);font-size:10.5px;color:var(--text)}',
'.lhm-editing .lhm-ebar,.lhm-editing > .lhm-side > .lhm-ebar{display:flex}',
'.lhm-ebar .lbl{flex:1;min-width:0;color:var(--dim2);font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.lhm-ebar .lbl b{color:var(--acc);font-weight:600}',
'.lhm-ebar button{height:24px;border:1px solid var(--border);border-radius:var(--r-sm,6px);background:var(--bg1);color:var(--text);font:inherit;font-size:10.5px;padding:0 9px;cursor:pointer;white-space:nowrap}',
'.lhm-ebar button:hover{border-color:var(--acc);color:var(--acc)}.lhm-ebar button.pri{background:var(--acc);border-color:var(--acc);color:var(--on-acc,#fff)}.lhm-ebar button.pri:hover{filter:brightness(1.08);color:var(--on-acc,#fff)}',
'.lhm-s-w.lhm-s-added .lhm-s-wbody vera-widget{display:block;width:100%}',
'.lhm-tsearch{display:flex;align-items:center;gap:6px;margin:6px 8px 4px;height:28px;padding:0 9px;border-radius:var(--r-sm,6px);background:var(--bg,var(--bg0,#0e0f12));box-shadow:inset 0 0 0 1px var(--border);color:var(--dim)}',
    '.lhm-tsearch input{flex:1;min-width:0;border:none;background:transparent;font:inherit;font-size:10.5px;color:var(--text);outline:none}',
    '.lhm-wadd{height:30px;border:none;border-radius:var(--r-sm,6px);font:inherit;font-size:10.5px;color:var(--dim2);text-align:left;padding:0 10px;background:transparent;outline:1px dashed color-mix(in srgb,var(--dim) 60%,transparent);outline-offset:-1px;cursor:pointer}',
'.lhm-wadd:hover{color:var(--acc);outline-color:var(--acc)}',
'.lhm-wnote-s{font-size:9px;color:var(--dim);padding:4px 2px 0;line-height:1.5}',
'.lhm-wfoot{display:flex;align-items:center;gap:6px;padding-top:2px}.lhm-wfoot .sp{flex:1}',
'.lhm-wfoot button,.lhm-wname button{height:23px;padding:0 10px;border:none;border-radius:99px;font:inherit;font-size:10px;color:var(--dim2);background:var(--bg2);cursor:pointer;white-space:nowrap}',
'.lhm-wfoot button.pri,.lhm-wname button.pri,.lhm-prow .padd{color:var(--on-acc,#fff);background:var(--acc);font-weight:600}',
'.lhm-wname{display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:var(--r-sm,6px);background:var(--bg2)}',
'.lhm-wname .lbl{font-size:9px;text-transform:uppercase;letter-spacing:.09em;color:var(--dim)}',
'.lhm-wname .inp{flex:1;min-width:0;height:24px;padding:0 8px;border:none;border-radius:var(--r-sm,6px);background:var(--bg0);font:inherit;font-size:11px;color:var(--text);box-shadow:inset 0 0 0 1px var(--acc)}',
'.lhm-pick{position:fixed;width:378px;z-index:2147483000;display:flex;flex-direction:column;border-radius:10px;background:var(--bg1);box-shadow:0 8px 24px rgba(0,0,0,.28),0 0 0 1px var(--border)}',
'.lhm-pick-hd{display:flex;align-items:center;gap:8px;padding:12px 15px 8px}.lhm-pick-hd h3{margin:0;font-size:12.5px;font-weight:600;color:var(--text)}.lhm-pick-hd .sp{flex:1}.lhm-pick-hd .lbl{font-family:var(--mono);font-size:9px;color:var(--dim)}',
'.lhm-pick-hd .x{margin-left:6px;border:none;background:transparent;color:var(--dim);font-size:11px;cursor:pointer}',
'.lhm-pick-s{margin:0 15px 8px;height:28px;display:flex;align-items:center;gap:6px;padding:0 10px;border-radius:var(--r-sm,6px);background:var(--bg0);font-size:10.5px;color:var(--dim);box-shadow:inset 0 0 0 1px var(--border)}',
'.lhm-pick-s input{flex:1;min-width:0;border:none;background:transparent;font:inherit;font-size:10.5px;color:var(--text);outline:none;padding:0}',
'.lhm-pick-bd{display:flex;flex-direction:column;gap:4px;padding:0 15px;overflow:auto;min-height:0;scrollbar-width:thin}',
'.lhm-pick-bd .grp{font-size:9px;text-transform:uppercase;letter-spacing:.09em;font-weight:600;color:var(--dim);display:flex;align-items:center;gap:7px;margin-top:6px}.lhm-pick-bd .grp::after{content:"";flex:1;height:1px;background:var(--border)}',
'.lhm-prow{display:grid;grid-template-columns:24px 1fr auto;gap:6px;align-items:center;padding:5px 6px;border-radius:var(--r-sm,6px);background:var(--bg2)}',
'.lhm-prow .wg{font-family:var(--mono);font-size:11px;color:var(--acc);text-align:center}.lhm-prow .wn{display:flex;flex-direction:column;min-width:0}.lhm-prow .wn b{font-size:10.5px;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.lhm-prow .wn span{font-family:var(--mono);font-size:8.5px;color:var(--dim2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
'.lhm-prow .padd{height:22px;padding:0 9px;border:none;border-radius:99px;font:inherit;font-size:10px;cursor:pointer}',
'.lhm-pick-note{padding:10px 15px 14px;font-size:10px;color:var(--dim2);line-height:1.5}',
    '.lhm-editing .lhm-wbar{display:flex}',
    '.lhm-wbar button{width:18px;height:18px;border:1px solid var(--border);border-radius:var(--r-sm,4px);background:var(--bg1);color:var(--dim2);font-size:10px;line-height:1;cursor:pointer;padding:0}',
    '.lhm-wbar button:hover{color:var(--acc);border-color:var(--acc)}',
    '.lhm-wcfg{display:none;flex-direction:column;gap:6px;padding:8px;overflow-y:auto;flex:1;min-height:0}',
    '.lhm-wcfgmode .lhm-wcfg{display:flex}',
    '.lhm-wcfgmode .lhm-det > :not(.lhm-hd):not(.lhm-wcfg){display:none!important}',
    '.lhm-wcfg .lhm-wr{display:grid;grid-template-columns:64px 1fr;border-bottom:1px solid var(--border);font-size:10.5px}',
    '.lhm-wcfg .lhm-wr .k{font-family:var(--mono);font-size:8px;text-transform:uppercase;letter-spacing:1px;color:var(--dim);padding:5px 0}',
    '.lhm-wcfg .lhm-wr .v{padding:4px 0 4px 6px;line-height:1.45;word-break:break-word;color:var(--text)}',
    '.lhm-wcfg .lhm-wr .v code{font-family:var(--mono);font-size:9.5px;color:var(--acc)}',
    '.lhm-wcfg .lhm-wacts{display:flex;gap:4px;flex-wrap:wrap;margin-top:4px}',
    '.lhm-wcfg .lhm-wacts button{font-size:10px;padding:3px 8px;border:1px solid var(--border);border-radius:var(--r-sm,5px);background:var(--bg2);color:var(--text);cursor:pointer}',
    '.lhm-wcfg .lhm-wacts button:hover{border-color:var(--acc);color:var(--acc)}',
    '.lhm-wcfg .lhm-wnote{font-family:var(--mono);font-size:8.5px;color:var(--dim2)}',
    '.lhm-editing [data-w]{outline:1px dashed var(--acc);outline-offset:-1px;position:relative}',
    '.lhm-editing [data-w]::before{content:attr(data-w);position:absolute;top:0;left:0;z-index:5;font-family:var(--mono);font-size:8px;line-height:1;padding:2px 4px;background:var(--acc);color:var(--on-acc,#fff);border-radius:0 0 4px 0;pointer-events:none;white-space:nowrap;max-width:100%;overflow:hidden;text-overflow:ellipsis}',
    /* hosted elsewhere (the harness draws the rail + tab strip): the owner keeps header, panes and CTA */
    'html.vpb-nav-hosted .lhm-rail,html.vpb-nav-hosted .lhm-det .ctx-tab-bar{display:none!important}',
    /* an absorbed menu in a host */
    '.lhm-absorbed{display:flex;flex-direction:row;min-height:0;flex:1}',
    '.lhm-absorbed .lhm-tabs{flex:1;display:flex;flex-direction:column;gap:1px;padding:6px 4px;min-width:0;overflow-y:auto}',
    '.lhm-absorbed .lhm-tab{padding:6px 8px;border-radius:var(--r-sm,5px);font-family:var(--mono);font-size:9.5px;letter-spacing:.3px;color:var(--dim2);cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;border-left:2px solid transparent}',
    '.lhm-absorbed .lhm-tab:hover{color:var(--text);background:var(--bg2)}',
    '.lhm-absorbed .lhm-tab.on{color:var(--acc);border-left-color:var(--acc);background:var(--bg2)}',
    '.lhm-absorbed .lhm-tabs .lhm-ttl{font-family:var(--sans);font-size:11px;font-weight:600;color:var(--text);padding:4px 8px 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
    /* the top-level SIDE menu — the harness\'s main LHM (the Harness board): search, Open now, the panels with their sections, widgets */
    '.lhm-side{flex:1;display:flex;flex-direction:column;min-height:0;min-width:0;overflow:hidden}',
    '.lhm-side .lhm-s-hd{padding:10px 10px 6px;flex-shrink:0;display:flex;align-items:center;gap:6px}',
    '.lhm-side .lhm-s-top{height:36px;flex-shrink:0;display:flex;align-items:center;gap:8px;padding:0 8px;border-bottom:1px solid var(--border)}',
    '.lhm-side .lhm-s-tb{width:26px;height:26px;border-radius:6px;border:none;background:transparent;color:var(--dim2);font:inherit;font-size:14px;display:flex;align-items:center;justify-content:center;cursor:pointer}',
    '.lhm-side .lhm-s-tb:hover{color:var(--text);background:var(--bg2)}.lhm-side .lhm-s-tb.on{color:var(--acc);background:color-mix(in srgb,var(--acc) 14%,transparent)}',
    '.lhm-side .lhm-s-top .nm{font-size:11px;font-weight:600;color:var(--text)}',
    '.lhm-side .lhm-s-top .mono{font-family:var(--mono);font-size:9px;color:var(--dim2);margin-left:auto}',
    '.lhm-side .lhm-s-wm{cursor:default}.lhm-side .lhm-s-wm + .lhm-s-wbar{margin-bottom:4px}',
    '.lhm-side .lhm-s-wbar{height:5px;border-radius:3px;background:var(--bg3,var(--bg0));overflow:hidden}',
    '.lhm-side .lhm-s-wbar i{display:block;height:100%;border-radius:3px;background:var(--acc)}',
    '.lhm-side .lhm-s-srch{flex:1;min-width:0}',
    '.lhm-side .lhm-s-edit{flex:0 0 auto;font:inherit;font-size:12px;height:28px;width:28px;border:1px solid var(--border);border-radius:var(--r-sm,6px);background:var(--bg2);color:var(--dim2);cursor:pointer}',
    '.lhm-side .lhm-s-edit.on{color:var(--acc);border-color:var(--acc)}',
    '.lhm-side > .lhm-wcfg{max-height:46%;border-top:1px solid var(--border)}',
    '.lhm-side .lhm-s-srch{display:flex;align-items:center;gap:8px;height:28px;padding:0 9px;border-radius:var(--r-sm,6px);background:var(--bg2);color:var(--dim2);font-size:10.5px;cursor:pointer;border:1px solid transparent}',
    '.lhm-side .lhm-s-srch:hover{color:var(--text);border-color:var(--border)}',
    '.lhm-side .lhm-s-srch .k{margin-left:auto;font-family:var(--mono);font-size:9px}',
    '.lhm-side .lhm-s-bd{flex:1;overflow-y:auto;overflow-x:hidden;padding:2px 8px 8px;display:flex;flex-direction:column;gap:1px;min-height:0}',
    '.lhm-side .lhm-s-grp{font-family:var(--mono);font-size:8px;text-transform:uppercase;letter-spacing:1px;font-weight:600;color:var(--dim);padding:12px 6px 4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
    '.lhm-side .lhm-s-row{display:flex;align-items:center;gap:8px;height:30px;padding:0 8px;border-radius:var(--r-sm,6px);color:var(--dim2);font-size:11px;white-space:nowrap;overflow:hidden;cursor:pointer;border-left:2px solid transparent}',
    '.lhm-side .lhm-s-row:hover{color:var(--text);background:var(--bg2)}',
    '.lhm-side .lhm-s-row.on{color:var(--text);background:var(--bg2);font-weight:600;border-left-color:var(--acc)}',
    '.lhm-side .lhm-s-row.open:not(.on){color:var(--acc2,var(--acc))}',
    '.lhm-side .lhm-s-row .ico{width:16px;text-align:center;color:var(--dim2);font-size:12px;flex-shrink:0}',
    '.lhm-side .lhm-s-pan.on .lhm-s-row .ico{color:var(--acc)}',
    '.lhm-side .lhm-s-row .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;display:flex;flex-direction:column;line-height:1.15}',
    '.lhm-side .lhm-s-row .nm em{font-style:normal;font-family:var(--mono);font-size:8px;color:var(--dim2);font-weight:400}',
    '.lhm-side .lhm-s-row .ct{margin-left:auto;font-family:var(--mono);font-size:8.5px;color:var(--dim2);background:var(--bg0);border-radius:8px;padding:1px 6px;flex-shrink:0}',
    '.lhm-side .lhm-s-row .x{margin-left:auto;flex-shrink:0;width:16px;height:16px;display:flex;align-items:center;justify-content:center;border-radius:3px;color:var(--dim2);font-size:11px}',
    '.lhm-side .lhm-s-row .x:hover{color:var(--err,#e06c75);background:var(--bg0)}',
    '.lhm-side .lhm-s-empty{padding:4px 8px;font-size:10px;color:var(--dim)}',
    '.lhm-side .lhm-s-pan{display:flex;flex-direction:column}',
    '.lhm-side .lhm-s-sec{display:flex;flex-direction:column;margin-left:18px;border-left:1px solid var(--border)}',
    '.lhm-side .lhm-s-sech{display:flex;align-items:center;gap:6px;height:24px;padding:0 8px;font-size:10.5px;color:var(--dim2);cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
    '.lhm-side .lhm-s-sech:hover{color:var(--text)}',
    '.lhm-side .lhm-s-sech i{width:0;height:0;border:4px solid transparent;border-left-color:var(--dim2);margin-right:2px;transition:transform .15s;flex-shrink:0}',
    '.lhm-side .lhm-s-sec.on .lhm-s-sech{color:var(--text)}',
    '.lhm-side .lhm-s-sec.on .lhm-s-sech i{transform:rotate(90deg);border-left-color:var(--acc)}',
    '.lhm-side .lhm-s-opt{height:22px;padding:0 8px 0 22px;font-size:10px;color:var(--dim2);display:flex;align-items:center;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;border-radius:var(--r-sm,6px)}',
    '.lhm-side .lhm-s-opt:hover,.lhm-side .lhm-s-opt.on{color:var(--text);background:var(--bg2)}',
    '.lhm-side .lhm-s-opt.on{color:var(--acc)}',
    '.lhm-side .lhm-s-reg{color:var(--dim2);font-family:var(--mono);font-size:9.5px}',
    '.lhm-side .lhm-s-w{background:var(--bg2);border-radius:var(--r-sm,6px);padding:7px 8px;display:flex;flex-direction:column;gap:4px;margin:3px 0;border:1px solid var(--border)}',
    '.lhm-side .lhm-s-wh{display:flex;align-items:center;font-family:var(--mono);font-size:8.5px;text-transform:uppercase;letter-spacing:.8px;font-weight:600;color:var(--dim2);cursor:pointer}',
    '.lhm-side .lhm-s-wh b{margin-left:auto;font-family:var(--mono);font-size:10px;color:var(--text);font-weight:400}',
    '.lhm-side .lhm-s-wrow{display:flex;align-items:center;gap:6px;font-size:9.5px;color:var(--dim2);height:18px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
    '.lhm-side .lhm-s-wrow i{width:5px;height:5px;border-radius:50%;flex-shrink:0}',
    '.lhm-side .lhm-s-wrow em{margin-left:auto;font-style:normal;font-family:var(--mono);font-size:8.5px;color:var(--dim)}',
    '.lhm-side .lhm-s-w.events .lhm-s-wbody{max-height:260px;display:flex;flex-direction:column;min-height:0}',
    '.lhm-side .lhm-s-note{padding:8px 10px;font-size:9.5px;color:var(--dim2);line-height:1.45;border-top:1px solid var(--border);flex-shrink:0}',
    /* the STRIPS under the tab bar (tabs mode): the active panel\'s sections, then the open section\'s tabs as pills */
    '.lhm-strips{display:flex;align-items:center;gap:3px;height:30px;padding:0 14px;flex-shrink:0;background:var(--bg1);border-bottom:1px solid var(--border);overflow:hidden}',
    '.lhm-strips .lhm-st-p{font-family:var(--mono);font-size:8.5px;text-transform:uppercase;letter-spacing:1px;font-weight:600;color:var(--dim);margin-right:8px;flex-shrink:0;white-space:nowrap}',
    '.lhm-strips .lhm-st{height:22px;padding:0 9px;border-radius:var(--r-sm,6px);font-size:10.5px;color:var(--dim2);white-space:nowrap;cursor:pointer;display:inline-flex;align-items:center;border:none;background:transparent;font-family:var(--sans)}',
    '.lhm-strips .lhm-st:hover{color:var(--text);background:var(--bg2)}',
    '.lhm-strips .lhm-st.on{color:var(--text);background:var(--bg2);font-weight:600;box-shadow:inset 0 -2px 0 var(--acc)}',
    '.lhm-strips .lhm-st-sep{width:1px;height:16px;background:var(--border);margin:0 8px;flex-shrink:0}',
    '.lhm-strips .lhm-st-o{height:20px;padding:0 8px;border-radius:10px;font-size:9.5px;color:var(--dim2);background:var(--bg2);white-space:nowrap;cursor:pointer;display:inline-flex;align-items:center;border:1px solid var(--border);font-family:var(--sans)}',
    '.lhm-strips .lhm-st-o:hover,.lhm-strips .lhm-st-o.on{color:var(--text);border-color:var(--acc)}',
  ].join('\n');

  function _css(doc){
    doc = doc || document;
    if(doc.getElementById('vera-lhm-css')) return;
    var s = doc.createElement('style'); s.id = 'vera-lhm-css'; s.textContent = CSS;
    (doc.head || doc.documentElement).appendChild(s);
  }
  function _el(tag, cls, text){ var e = document.createElement(tag); if(cls) e.className = cls; if(text != null) e.textContent = text; return e; }
  function _esc(s){ return String(s == null ? '' : s); }

  // ── the owner side ─────────────────────────────────────────────────────
  var _cfg = null, _host = null, _rail = null, _det = null, _hd = null, _top = null, _cta = null;
  var _active = '', _activeTab = '', _topMode = false, _editing = false;
  var _pid = '', _embedded = false, _hosted = false, _picking = false;   // _picking: the click is ours, not the user's
  var _quick = null;   // the quick body's host (a menu with quick(el) draws there; "Full ▸" swaps to its panes)
  var _wcfg = null, _wcfgOpen = false;   // the record sheet

  // the tab strip's elements, keyed by the tab id the owner gave the menu
  function _tabEl(id){
    if(!_cfg || !_cfg.tabEl) return null;
    try{ return _cfg.tabEl(id); }catch(e){ return null; }
  }
  function _menu(id){ return (_cfg && _cfg.menus || []).filter(function(m){ return m.id === id; })[0] || null; }
  function _menuOfTab(tabId){ return (_cfg && _cfg.menus || []).filter(function(m){ return (m.tabs || []).some(function(t){ return t.id === tabId; }); })[0] || null; }

  function _renderRail(){
    if(!_rail) return;
    _rail.innerHTML = '';
    var top = _el('div', 'lhm-ico top' + (_topMode ? ' on' : ''), '☰');
    top.title = 'Everything — every menu, and what is open now';
    var openN = _openNow().length; if(openN){ var b = _el('span', 'lhm-badge', String(openN)); top.appendChild(b); }
    top.addEventListener('click', function(){ toggleTop(); });
    _rail.appendChild(top);
    (_cfg.menus || []).forEach(function(m){
      var ico = _el('div', 'lhm-ico' + (m.id === _active && !_topMode ? ' on' : ''), m.iconHtml ? null : (m.icon || '•'));
      if(m.iconHtml) ico.innerHTML = m.iconHtml;   // the board's SVG glyph for this menu
      ico.title = m.label + (m.tabs && m.tabs.length > 1 ? ' — ' + m.tabs.map(function(t){ return t.label; }).join(' · ') : '');
      ico.setAttribute('data-menu', m.id);
      var badge = 0; try{ badge = m.badge ? +m.badge() : 0; }catch(e){}
      if(badge){ ico.appendChild(_el('span', 'lhm-badge', String(badge))); }
      ico.addEventListener('click', function(){ if(m.id === _active && !_topMode && _cfg.onCollapse && _det && _det.getClientRects().length){ try{ _cfg.onCollapse(); }catch(e){} return; } pick(m.id); });   // the active icon folds the menu to the rail
      _rail.appendChild(ico);
    });
    _rail.appendChild(_el('div', 'lhm-sp'));
  }
  function _renderHeader(){
    if(!_hd) return;
    var m = _menu(_active);
    var h2 = _hd.querySelector('h2'), meta = _hd.querySelector('.lhm-meta'), ed = _hd.querySelector('.lhm-edit');
    if(_topMode){ h2.textContent = _cfg.title || 'Vera'; var np = 0; try{ np = ((_cfg.panels && _cfg.panels()) || []).length; }catch(e){} meta.textContent = (np ? np + ' panels · ⌘K' : (_cfg.menus || []).length + ' menus') + ' · ' + _openNow().length + ' open'; }
    else { h2.textContent = m ? (m.title || m.label) : ''; var s = ''; try{ s = m && m.meta ? String(m.meta() || '') : ''; }catch(e){} meta.textContent = s; }
    ed.classList.toggle('on', _editing);
    var dp = _hd.querySelector('.lhm-deep'); if(dp){ var hasQ = !!(m && typeof m.quick === 'function' && !_topMode); dp.classList.toggle('has', hasQ); dp.textContent = (m && m._deep) ? '◂ Quick' : 'Full ▸'; dp.title = (m && m._deep) ? 'Back to the quick menu' : 'The full ' + (m ? (m.label || m.id) : '') + ' panel, in this same place'; }
  }
  function _renderTabs(){
    // only the current menu's tabs show in the owner's strip; the others stay in the DOM with their handlers
    var m = _menu(_active); var ids = {};
    if(m) (m.tabs || []).forEach(function(t){ ids[t.id] = 1; });
    (_cfg.menus || []).forEach(function(mm){ (mm.tabs || []).forEach(function(t){ var el = _tabEl(t.id); if(el) el.classList.toggle('lhm-off', !ids[t.id]); }); });
  }
  // the quick body: the active menu's quick(el, api) draws the board's widgets; a menu in "deep" shows its old panes
  function _renderQuick(){
    if(!_quick || !_host) return;
    var m = _menu(_active); var on = !!(m && typeof m.quick === 'function' && !m._deep && !_topMode);
    _host.classList.toggle('lhm-quickmode', on);
    if(!on){ _quick.style.display = 'none'; return; }
    _quick.style.display = '';
    if(_quick._for !== m.id){ _quick.innerHTML = ''; _quick._for = m.id; try{ delete _quick.dataset.sig; }catch(e){} }   // a menu of your own shares the host: its body must draw afresh
    try{ m.quick(_quick, { menu:m, render:render, pick:pick, deep:deep }); }catch(e){ _quick.innerHTML = '<div class="lhm-empty">' + _escH(e && e.message || e) + '</div>'; }
    _quickCompose(m);
  }
  // deep(on): the active menu's full pane (its old tabs and panes) in place of the quick body — and back
  // ── composing a quick menu (the ChatMenu board): every widget removable, reorderable, the picker adds more ──
  var _QK = 'vera:lhm:quick:';
  function _qspec(m){ if(m._qs) return m._qs; var s = null; try{ s = JSON.parse(localStorage.getItem(_QK + m.id) || 'null'); }catch(e){} m._qs = (s && typeof s === 'object') ? s : { removed:{}, order:[], added:[] }; m._qs.removed = m._qs.removed || {}; m._qs.order = m._qs.order || []; m._qs.added = m._qs.added || []; return m._qs; }
  function _qsave(m){ try{ localStorage.setItem(_QK + m.id, JSON.stringify(_qspec(m))); }catch(e){} }
  function _qkey(el){ return el.getAttribute('data-w') || ''; }
  // harvest a menu's widgets (name · tag · template) without showing it: its quick body into a detached element
  function _qparts(m){ if(m._parts && m._parts.length) return m._parts; if(typeof m.quick !== 'function') return []; var tmp = _el('div'); try{ m.quick(tmp, { menu:m, render:function(){}, pick:function(){}, deep:function(){} }); }catch(e){} m._parts = Array.prototype.map.call(tmp.querySelectorAll('.wid[data-w]'), function(w){ var k = _qkey(w); return { key:k, label:k.split(' · ')[0], tag:(k.split(' · ')[1] || ''), tpl:_tplOf(w) }; }); return m._parts; }
  function _quickCompose(m){
    var qs = _qspec(m);
    Array.prototype.forEach.call(_quick.querySelectorAll('.lhm-ebar, .lhm-wedit, .lhm-added-grp, .wid[data-added], .lhm-wbar.lhm-qbar'), function(x){ if(x.parentNode) x.parentNode.removeChild(x); });
    var wids = Array.prototype.slice.call(_quick.querySelectorAll('.wid[data-w]'));
    m._parts = wids.map(function(w){ var k = _qkey(w); return { key:k, label:k.split(' · ')[0], tag:(k.split(' · ')[1] || ''), tpl:_tplOf(w) }; });
    // removed widgets stay out (edit mode shows them dimmed so ✕ can be undone)
    wids.forEach(function(w){ var off = !!qs.removed[_qkey(w)]; w.classList.toggle('lhm-off', off && !_editing); w.classList.toggle('lhm-removed', off && _editing); });
    // the order the user gave
    if(qs.order.length){ var byKey = {}; wids.forEach(function(w){ byKey[_qkey(w)] = w; }); qs.order.forEach(function(k){ var w = byKey[k]; if(w) _quick.appendChild(w); }); }
    // what the picker added: another menu's widget, a widget form, a template of yours
    if(qs.added.length){ var grp = _el('div', 'grp lhm-added-grp', 'Added'); grp.setAttribute('data-w', 'added · group'); _quick.appendChild(grp);
      qs.added.forEach(function(a, i){ var w = _addedWidget(m, a, i); if(w) _quick.appendChild(w); }); }
    if(_editing){ _quickEbar(m); _quickBars(m); _quickFoot(m); }
  }
  // the edit bar (the top of the menu while editing): what you are doing, the way in, the way out
  function _ebar(title, onAdd, onDone){
    var bar = _el('div', 'lhm-ebar');   // the editor's own, not a part of the menu (no tag, no bar)
    var lbl = _el('span', 'lbl'); lbl.innerHTML = '<b>✎ Editing</b> ' + _escH(title || 'this menu') + ' — every part is a widget'; bar.appendChild(lbl);
    var add = _el('button', 'pri', '+ Add widget'); add.type = 'button'; add.title = 'Any widget form, another menu\'s element, a template of yours — with a preview'; add.addEventListener('click', function(ev){ ev.stopPropagation(); onAdd(); }); bar.appendChild(add);
    var done = _el('button', '', '✓ Done'); done.type = 'button'; done.title = 'Leave edit mode (what you changed stays)'; done.addEventListener('click', function(ev){ ev.stopPropagation(); onDone(); }); bar.appendChild(done);
    return bar;
  }
  function _quickEbar(m){ var bar = _ebar(m.title || m.label, function(){ openPicker(); }, function(){ toggleEdit(false); }); _quick.insertBefore(bar, _quick.firstChild); }
  function _addedWidget(m, a, i){
    var w = null;
    if(a.from){ var src = _menu(a.from); if(src && typeof src.quick === 'function'){ var tmp = _el('div'); try{ src.quick(tmp, { menu:src, render:render, pick:pick, deep:deep }); }catch(e){} var found = Array.prototype.filter.call(tmp.querySelectorAll('.wid[data-w]'), function(x){ return _qkey(x) === a.key; })[0]; if(found){ w = found; w.setAttribute('data-from', a.from); } } }
    // a widget RECORD (from the surface): its live face — the one renderer, reading its own source
    if(!w && a.record && typeof a.record === 'object' && window.customElements && customElements.get('vera-widget')){
      w = _el('div', 'wid lhm-added lhm-live'); w.setAttribute('data-w', (a.label || a.record.title || a.record.form || 'widget') + ' · ' + (a.record.form || 'widget'));
      var vw = document.createElement('vera-widget'); try{ vw.setAttribute('record', JSON.stringify(a.record)); }catch(e){} vw.setAttribute('size', (a.record.frame && a.record.frame.size) || a.record.size || 's'); w.appendChild(vw);
      var cap2 = _el('div', 'lhm-added-cap'); cap2.appendChild(_el('b', '', a.label || a.record.title || a.record.form || 'widget')); cap2.appendChild(_el('span', 'mono', a.c || a.record.source || a.record.form || '')); w.appendChild(cap2); }
    if(!w){ w = _el('div', 'wid lhm-added'); w.setAttribute('data-w', (a.label || a.form || 'widget') + ' · ' + (a.tpl ? 'template' : (a.form || 'widget'))); if(a.tpl) w.setAttribute('data-tpl', a.tpl);
      var body = _el('div', 'lhm-added-body'); var drawn = ''; try{ if(window.VeraWidget && a.form) drawn = window.VeraWidget.draw(a.form, a.data != null ? a.data : _sample(a.form), 'm', { bare:true, title:a.label }); }catch(e){}
      body.innerHTML = drawn || ('<span class="lhm-empty">' + _escH(a.label || a.form || 'widget') + '</span>'); w.appendChild(body);
      var cap = _el('div', 'lhm-added-cap'); cap.appendChild(_el('b', '', a.label || a.form || 'widget')); cap.appendChild(_el('span', 'mono', a.c || (a.tpl ? 'template · ' + a.tpl : (a.form || '')))); w.appendChild(cap); }
    w.setAttribute('data-added', String(i)); return w;
  }
  var _SAMPLE = { trace:[3,5,4,7,6,8,7], radial:{ value:62, max:100 }, counter:{ value:412 }, bar:{ value:62, max:100 }, bars:{ a:4, b:7, c:5, d:6 }, thermo:{ cpu:62, mem:48, gpu:71 }, donut:{ a:4, b:7, c:5 }, pills:{ ok:4, warn:1 }, kv:{ status:'online', node:'ct126' }, list:[{ name:'no reading yet', value:'' }], table:[{ name:'no reading yet', value:'' }], log:[{ t:'', text:'no reading yet' }], stepper:{ steps:[{ label:'no steps yet', status:'' }] }, string:'no reading yet', context_graph:{ nodes:[] } };
  function _sample(form){ return _SAMPLE[form] != null ? _SAMPLE[form] : 'no reading yet'; }
  // the bars: ⋮⋮ drag · ⚙ record · ⧉ template · ✕ remove
  function _quickBars(m){
    var qs = _qspec(m);
    Array.prototype.forEach.call(_quick.querySelectorAll('.wid[data-w]'), function(el){
      var bar = _el('div', 'lhm-wbar lhm-qbar'); var key = _qkey(el);
      var grip = _el('b', '', '⋮⋮'); grip.title = 'Drag to reorder'; bar.appendChild(grip);
      var cfgB = _el('button', '', '⚙'); cfgB.title = 'This widget\'s record'; cfgB.addEventListener('click', function(ev){ ev.stopPropagation();
        var ai = el.hasAttribute('data-added') ? qs.added[+el.getAttribute('data-added')] : null; var S = _surface();
        if(ai && ai.record && S){ try{ Promise.resolve(S.open({ mode:'edit', into:'lhm', record:ai.record, title:'Edit · ' + (ai.label || ai.record.form || 'widget'), anchor:el })).then(function(rec){ if(!rec) return; ai.record = rec; ai.label = rec.title || rec.form || ai.label; ai.c = rec.source || rec.form || ''; ai.form = rec.form; _qsave(m); render(); }).catch(function(){}); return; }catch(e){} }
        openRecord(_tplOf(el), key); }); bar.appendChild(cfgB);
      var saveB = _el('button', '', '⧉'); saveB.title = 'Save this configuration as a template'; saveB.addEventListener('click', function(ev){ ev.stopPropagation(); saveAsTemplate(_tplOf(el), key); saveB.classList.add('saved'); }); bar.appendChild(saveB);
      var rmB = _el('button', '', qs.removed[key] ? '↩' : '✕'); rmB.title = qs.removed[key] ? 'Put it back' : 'Remove from this menu';
      rmB.addEventListener('click', function(ev){ ev.stopPropagation(); if(el.hasAttribute('data-added')){ qs.added.splice(+el.getAttribute('data-added'), 1); } else { if(qs.removed[key]) delete qs.removed[key]; else qs.removed[key] = true; } _qsave(m); render(); }); bar.appendChild(rmB);
      if(getComputedStyle(el).position === 'static') el.style.position = 'relative';
      el.appendChild(bar);
      if(el._lhmDnd) return; el._lhmDnd = true;
      el.setAttribute('draggable', 'true');
      el.addEventListener('dragstart', function(ev){ _dragKey = key; el.classList.add('lhm-dragging'); try{ ev.dataTransfer.setData('text/plain', key); ev.dataTransfer.effectAllowed = 'move'; }catch(e){} });
      el.addEventListener('dragend', function(){ el.classList.remove('lhm-dragging'); _dragKey = ''; });
      el.addEventListener('dragover', function(ev){ if(!_dragKey || _dragKey === key) return; ev.preventDefault(); el.classList.add('lhm-dropover'); });
      el.addEventListener('dragleave', function(){ el.classList.remove('lhm-dropover'); });
      el.addEventListener('drop', function(ev){ ev.preventDefault(); el.classList.remove('lhm-dropover'); if(!_dragKey || _dragKey === key) return; var keys = Array.prototype.map.call(_quick.querySelectorAll('.wid[data-w]:not([data-added])'), _qkey); var from = keys.indexOf(_dragKey), to = keys.indexOf(key); if(from < 0 || to < 0) return; keys.splice(from, 1); keys.splice(to, 0, _dragKey); qs.order = keys; _qsave(m); render(); });
    });
  }
  var _dragKey = '';
  // the foot: + Add a widget, the note, Reset, Save as menu…
  function _quickFoot(m){
    var qs = _qspec(m); var foot = _el('div', 'lhm-wedit');   // the foot is the editor's, not a widget of the menu (no tag over its button)
    var add = _el('button', 'lhm-wadd', '+ Add a widget — from any menu, any widget form, or your templates'); add.type = 'button'; add.addEventListener('click', function(ev){ ev.stopPropagation(); openPicker(); }); foot.appendChild(add);
    var n = Object.keys(qs.removed).length, a = qs.added.length;
    foot.appendChild(_el('div', 'lhm-wnote-s', (a ? a + ' added' : 'nothing added') + ' · ' + (n ? n + ' removed' : 'nothing removed') + (qs.order.length ? ' · reordered' : '') + ' — every part is a widget: ⚙ its record, ⧉ a template, ✕ takes it out, ⋮⋮ moves it'));
    var row = _el('div', 'lhm-wfoot'); row.appendChild(_el('span', 'sp'));
    var reset = _el('button', '', 'Reset'); reset.type = 'button'; reset.title = 'Back to the menu as it came'; reset.addEventListener('click', function(ev){ ev.stopPropagation(); m._qs = { removed:{}, order:[], added:[] }; _qsave(m); render(); }); row.appendChild(reset);
    var save = _el('button', 'pri', 'Save as menu…'); save.type = 'button'; save.title = 'The menu as it stands, as a menu of your own in the rail'; save.addEventListener('click', function(ev){ ev.stopPropagation(); m._naming = !m._naming; render(); }); row.appendChild(save);
    foot.appendChild(row);
    if(m._naming){ var nm = _el('div', 'lhm-wname'); nm.appendChild(_el('span', 'lbl', 'Name')); var inp = _el('input', 'inp'); inp.type = 'text'; inp.placeholder = 'Ops glance'; inp.value = m._name || ''; inp.addEventListener('input', function(){ m._name = inp.value; }); inp.addEventListener('keydown', function(ev){ if(ev.key === 'Enter'){ ev.preventDefault(); go(); } }); nm.appendChild(inp);
      var ok = _el('button', 'pri', 'Save'); ok.type = 'button'; var go = function(){ var name = (inp.value || '').trim(); if(!name) return; m._naming = false; saveAsMenu(name); }; ok.addEventListener('click', function(ev){ ev.stopPropagation(); go(); }); nm.appendChild(ok); foot.appendChild(nm); setTimeout(function(){ try{ inp.focus(); }catch(e){} }, 0); }
    _quick.appendChild(foot);
  }
  // ── THE PICKER: everything a menu can be made of — the other menus' widgets, every widget form, your templates ──
  var _pick = null, _pickQ = '';
  // the other menus' elements, as the picker's first group
  function _pickOthers(m){ var others = []; (_cfg.menus || []).forEach(function(o){ if(o.id === m.id || typeof o.quick !== 'function') return; _qparts(o).forEach(function(p){ others.push({ g:'≡', n:p.label, c:o.label, add:{ from:o.id, key:p.key, label:p.label } }); }); }); return others; }
  // the shared widget surface (WidgetConfig board), here or in the host that embeds this menu
  // the widget sheet: the HOST's when this page is framed (the harness slot is a menu's width — a sheet inside it is clipped to nothing), else this page's
  function _surface(){ try{ var p = window.parent; if(window.frameElement && p && p !== window && p.VeraWidgetConfig && typeof p.VeraWidgetConfig.open === 'function') return p.VeraWidgetConfig; }catch(e){} try{ if(window.VeraWidgetConfig && typeof window.VeraWidgetConfig.open === 'function') return window.VeraWidgetConfig; }catch(e){} return null; }
  function openPicker(){
    var m = _menu(_active); if(!m || !_host) return; closePicker();
    var S = _surface();
    if(S){ var qs0 = _qspec(m);
      var items = _pickOthers(m).map(function(o){ return { g:o.g, n:o.n, c:o.c, record:{ _menuItem:true, from:o.add.from, key:o.add.key, label:o.add.label, c:o.c } }; });
      try{ Promise.resolve(S.open({ mode:'add', into:'lhm', title:'Add to ' + (m.title || m.label), templates:true, menuItems:items })).then(function(rec){
        if(!rec) return;
        if(rec._menuItem){ qs0.added.push({ label:rec.label, c:rec.c, from:rec.from, key:rec.key }); }
        else { qs0.added.push({ label:rec.title || rec.form || 'widget', c:rec.source || rec.form || '', form:rec.form, record:rec }); }
        _qsave(m); render(); }).catch(function(){}); return; }catch(e){}
    }
    _pick = _el('div', 'lhm-pick'); _pick.setAttribute('data-w', 'widget picker · sheet');
    var hd = _el('div', 'lhm-pick-hd'); hd.appendChild(_el('h3', '', 'Add to ' + (m.title || m.label))); hd.appendChild(_el('span', 'sp')); hd.appendChild(_el('span', 'lbl mono', 'everything is a widget')); var x = _el('button', 'x', '✕'); x.type = 'button'; x.addEventListener('click', closePicker); hd.appendChild(x); _pick.appendChild(hd);
    var s = _el('label', 'lhm-pick-s'); s.appendChild(_el('span', '', '⌕')); var q = _el('input'); q.type = 'search'; q.placeholder = 'search widgets, menus, templates…'; q.value = _pickQ; q.addEventListener('input', function(){ _pickQ = q.value; _pickRender(m); }); s.appendChild(q); _pick.appendChild(s);
    _pick.appendChild(_el('div', 'lhm-pick-bd'));
    _pick.appendChild(_el('div', 'lhm-pick-note', 'A widget record is a form + a config. Anything here can be saved as a template and placed anywhere — a dashboard, the canvas, this menu, a reply, a notebook, the ops map, an iso plate.'));
    document.body.appendChild(_pick); _pickPlace(); _pickRender(m);
    window.addEventListener('resize', _pickPlace);
    if(_cfg.pickerSources){ try{ Promise.resolve(_cfg.pickerSources()).then(function(groups){ _pickExtra = Array.isArray(groups) ? groups : []; if(_pick) _pickRender(m); }).catch(function(){}); }catch(e){} }
  }
  var _pickExtra = [];
  function _pickPlace(){ if(!_pick) return; var r = (_det || _host).getBoundingClientRect(); var left = r.right + 12; if(left + 378 > window.innerWidth - 8) left = Math.max(8, r.left - 390); _pick.style.left = left + 'px'; _pick.style.top = Math.max(8, r.top + 44) + 'px'; _pick.style.maxHeight = Math.max(200, window.innerHeight - r.top - 60) + 'px'; }
  function closePicker(){ if(_pick && _pick.parentNode) _pick.parentNode.removeChild(_pick); _pick = null; window.removeEventListener('resize', _pickPlace); }
  function _pickRender(m){
    var bd = _pick && _pick.querySelector('.lhm-pick-bd'); if(!bd) return; bd.innerHTML = '';
    var q = (_pickQ || '').toLowerCase(); var qs = _qspec(m);
    var groups = [];
    var others = []; (_cfg.menus || []).forEach(function(o){ if(o.id === m.id || typeof o.quick !== 'function') return; _qparts(o).forEach(function(p){ others.push({ g:'≡', n:p.label, c:o.label, add:{ from:o.id, key:p.key, label:p.label } }); }); });
    if(others.length) groups.push({ n:'From the other menus', items:others });
    _pickExtra.forEach(function(g){ groups.push(g); });
    var any = false;
    groups.forEach(function(g){ var items = (g.items || []).filter(function(it){ return !q || (String(it.n || '') + ' ' + String(it.c || '')).toLowerCase().indexOf(q) >= 0; }); if(!items.length) return; any = true;
      bd.appendChild(_el('div', 'grp', g.n));
      items.forEach(function(it){ var r = _el('div', 'lhm-prow'); r.appendChild(_el('span', 'wg mono', it.g || '▢')); var wn = _el('span', 'wn'); wn.appendChild(_el('b', '', it.n || '')); wn.appendChild(_el('span', '', it.c || '')); r.appendChild(wn);
        var b = _el('button', 'padd', '+ Add'); b.type = 'button'; b.addEventListener('click', function(ev){ ev.stopPropagation(); qs.added.push(Object.assign({ label:it.n, c:it.c }, it.add || {})); _qsave(m); closePicker(); render(); }); r.appendChild(b); bd.appendChild(r); }); });
    if(!any) bd.appendChild(_el('div', 'lhm-empty', groups.length ? 'Nothing matches.' : 'Loading the registry…'));
  }
  function deep(on){ var m = _menu(_active); if(!m) return false; m._deep = (on == null) ? !m._deep : !!on; render(); return m._deep; }
  function _renderCta(){
    if(!_cta) return;
    var m = _menu(_active);
    if(!m || !m.cta){ _cta.style.display = 'none'; return; }
    _cta.style.display = ''; _cta.textContent = m.cta.label || '→';
  }
  function _openNow(){
    var out = [];
    try{ if(_cfg && _cfg.openNow) out = _cfg.openNow() || []; }catch(e){}
    return out;
  }
  function _renderTop(){
    if(!_top) return;
    _top.innerHTML = '';
    _top.appendChild(_el('div', 'lhm-sec', 'Open now · ' + _openNow().length + ' · one set, one bridge'));
    var open = _openNow();
    if(!open.length) _top.appendChild(_el('div', 'lhm-empty', 'Nothing open beside the chat. A menu, the aide or you can open a panel here.'));
    open.forEach(function(o){
      var r = _el('div', 'lhm-row');
      r.appendChild(_el('span', 'lhm-ri', o.icon || '▭'));
      r.appendChild(_el('span', 'lhm-rn', o.label || o.id));
      r.appendChild(_el('span', 'lhm-rm', [o.origin || '', o.placement || ''].filter(Boolean).join(' · ')));
      if(o.close){ var x = _el('button', 'lhm-rx', '✕'); x.title = 'Close'; x.addEventListener('click', function(ev){ ev.stopPropagation(); try{ o.close(); }catch(e){} render(); }); r.appendChild(x); }
      if(o.focus) r.addEventListener('click', function(){ try{ o.focus(); }catch(e){} });
      _top.appendChild(r);
    });
    // every panel (the board's top-level list): a search box, then the rows; a row opens the panel beside the chat
    var panels = []; try{ panels = (_cfg.panels && _cfg.panels()) || []; }catch(e){}
    if(panels.length){
      var s = _el('label', 'lhm-tsearch'); s.appendChild(_el('span', '', '⌕')); var q = _el('input'); q.type = 'search'; q.placeholder = 'find a panel, a setting, a capability'; q.value = _topQ; s.appendChild(q); _top.appendChild(s);
      q.addEventListener('input', function(){ _topQ = q.value; _renderTop(); var i2 = _top.querySelector('.lhm-tsearch input'); if(i2){ i2.focus(); i2.selectionStart = i2.selectionEnd = i2.value.length; } });
      var qq = (_topQ || '').toLowerCase(); var shown = panels.filter(function(p){ return !qq || String(p.label || p.id).toLowerCase().indexOf(qq) >= 0 || String(p.id).toLowerCase().indexOf(qq) >= 0; });
      _top.appendChild(_el('div', 'lhm-sec', 'Panels · ' + panels.length + (qq ? ' · ' + shown.length + ' match' : '') + ' · ⌘K'));
      shown.slice(0, 120).forEach(function(p){ var r = _el('div', 'lhm-row'); r.appendChild(_el('span', 'lhm-ri', p.icon || '▭')); r.appendChild(_el('span', 'lhm-rn', p.label || p.id)); r.appendChild(_el('span', 'lhm-rm', p.id)); r.addEventListener('click', function(){ try{ if(p.open) p.open(); }catch(e){} }); _top.appendChild(r); });
      if(!shown.length) _top.appendChild(_el('div', 'lhm-empty', 'No panel matches.'));
    }
    _top.appendChild(_el('div', 'lhm-sec', 'Menus · ' + (_cfg.menus || []).length));
    (_cfg.menus || []).forEach(function(m){
      var r = _el('div', 'lhm-row' + (m.id === _active ? ' on' : ''));
      r.appendChild(_el('span', 'lhm-ri', m.icon || '•'));
      r.appendChild(_el('span', 'lhm-rn', m.label));
      r.appendChild(_el('span', 'lhm-rm', (m.tabs || []).map(function(t){ return t.label; }).join(' · ')));
      r.addEventListener('click', function(){ pick(m.id); });
      _top.appendChild(r);
    });
    if(_cfg.topExtra){ try{ _cfg.topExtra(_top); }catch(e){} }
  }
  function render(){
    if(!_cfg) return;
    if(_host) _host.classList.toggle('lhm-topmode', _topMode);
    if(_host) _host.classList.toggle('lhm-editing', _editing);
    _renderRail(); _renderHeader(); _renderTabs(); _renderCta(); _renderTop(); _renderQuick();
    if(_editing) _wireBars(); else closePicker();
    if(!_editing && _wcfgOpen) closeRecord();
    _publish();
  }

  // pick('<menu>') opens the menu on its remembered / first tab; pick('<menu>/<tab>') or pick('<tab>') a tab
  function pick(id, opts){
    id = String(id || ''); opts = opts || {};
    var menuId = id, tabId = '';
    if(id.indexOf('/') >= 0){ menuId = id.split('/')[0]; tabId = id.split('/')[1]; }
    var m = _menu(menuId);
    if(!m){ var mt = _menuOfTab(id); if(mt){ m = mt; menuId = mt.id; tabId = id; } }
    if(!m) return false;
    if(!tabId) tabId = (m._last && (m.tabs || []).some(function(t){ return t.id === m._last; })) ? m._last : ((m.tabs || [])[0] || {}).id || '';
    _active = menuId; _activeTab = tabId; m._last = tabId; _topMode = false;
    var el = _tabEl(tabId);
    if(el && !opts.silent){ _picking = true; try{ el.click(); }catch(e){} _picking = false; }
    if(_cfg.onPick){ try{ _cfg.onPick(menuId, tabId); }catch(e){} }
    render();
    return true;
  }
  // the owner's own UI switched tab (a direct click) — keep the rail and the header in step
  function setActiveTab(tabId){
    var m = _menuOfTab(tabId); if(!m) return;
    _active = m.id; _activeTab = tabId; m._last = tabId; _topMode = false; render();
  }
  var _topQ = '';
  function toggleTop(on){ _topMode = (on == null) ? !_topMode : !!on; render(); }
  function toggleEdit(on){ _editing = (on == null) ? !_editing : !!on; if(!_editing) closeRecord(); render(); }

  // ── composing a menu (the ChatMenu board's edit mode; the lhm.compose directive) ──────────────────────
  // compose(spec) edits a menu the way ✎ does - it turns edit mode ON, applies the spec, and leaves edit mode on for
  // the user to confirm or close; nothing is a second implementation. spec = { menu, add:[{id, label, tpl?, icon?}],
  // remove:[ids], order:[ids], open:true }. A menu id that does not exist yet is created (a menu of the user's own).
  var _menuHistory = [];   // for undo: the menus before each compose
  function compose(spec){
    spec = spec || {}; if(!_cfg) return false;
    var menus = _cfg.menus = (_cfg.menus || []);
    _menuHistory.push(JSON.parse(JSON.stringify(menus.map(function(m){ return { id:m.id, label:m.label, icon:m.icon, tabs:(m.tabs || []).map(function(t){ return { id:t.id, label:t.label, tpl:t.tpl }; }) }; }))));
    var id = String(spec.menu || _active || ''); if(!id) return false;
    var m = _menu(id);
    if(!m){ m = { id:id, label:spec.label || id, icon:spec.icon || '✦', tabs:[], own:true }; menus.push(m); }
    (spec.add || []).forEach(function(a){ if(!a || !a.id) return; if((m.tabs || []).some(function(t){ return t.id === a.id; })) return; (m.tabs = m.tabs || []).push({ id:String(a.id), label:String(a.label || a.id), tpl:a.tpl || '', icon:a.icon || '' }); });
    if(Array.isArray(spec.remove)) m.tabs = (m.tabs || []).filter(function(t){ return spec.remove.indexOf(t.id) < 0; });
    if(Array.isArray(spec.order) && spec.order.length){ var byId = {}; (m.tabs || []).forEach(function(t){ byId[t.id] = t; }); m.tabs = spec.order.map(function(k){ return byId[k]; }).filter(Boolean).concat((m.tabs || []).filter(function(t){ return spec.order.indexOf(t.id) < 0; })); }
    toggleEdit(true);
    if(spec.open !== false) pick(m.id, { silent:true });
    if(_cfg.onCompose){ try{ _cfg.onCompose(m, spec); }catch(e){} }
    render();
    return true;
  }
  function composeUndo(){ var prev = _menuHistory.pop(); if(!prev || !_cfg) return false; var byId = {}; prev.forEach(function(p){ byId[p.id] = p; }); _cfg.menus = _cfg.menus.filter(function(m){ return byId[m.id]; }).map(function(m){ var p = byId[m.id]; m.tabs = p.tabs; return m; }); render(); return true; }
  // Save as menu…: the current menu as a record of the user's own, handed to the owner (cfg.saveMenu(record) - the
  // chat stores it through lhm.menu.save) and to the rail; addMenus(list) puts saved menus back beneath the built-ins
  function saveAsMenu(name){
    var m = _menu(_active); if(!m) return null;
    var rec = { id:'menu:' + String(name || m.label).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 48), name:String(name || m.label), icon:m.icon || '✦',
      items:(m.tabs || []).map(function(t){ return { id:t.id, label:t.label, tpl:t.tpl || '' }; }), from:m.id, widgets:_qspec(m) };
    if(_cfg && _cfg.saveMenu){ try{ _cfg.saveMenu(rec); }catch(e){} }
    addMenus([rec]);
    return rec;
  }
  function addMenus(list){
    if(!_cfg || !Array.isArray(list)) return 0;
    var n = 0;
    list.forEach(function(rec){ if(!rec || !rec.id) return; if(_menu(rec.id)) return; var src = rec.from ? _menu(rec.from) : null;
      var mm = { id:rec.id, label:rec.name || rec.id, icon:rec.icon || '✦', own:true, tabs:(rec.items || []).map(function(t){ return { id:t.id, label:t.label || t.id, tpl:t.tpl || '' }; }) };
      // a menu of your own composed from a quick menu: the same quick body, with what you added, removed and reordered
      if(src && typeof src.quick === 'function'){ mm.quick = src.quick; mm.cta = src.cta; mm.meta = src.meta; mm.iconHtml = src.iconHtml; if(rec.widgets && typeof rec.widgets === 'object') mm._qs = { removed:rec.widgets.removed || {}, order:rec.widgets.order || [], added:rec.widgets.added || [] }; }
      _cfg.menus.push(mm); n++; });
    if(n) render();
    return n;
  }

  // ── every part's record: the widget registry's template behind it ──────
  function _base(){ try{ return (_cfg && _cfg.base) || window._veraBase || location.origin; }catch(e){ return ''; } }
  function _tplOf(el){ return el ? (el.getAttribute('data-tpl') || '') : ''; }
  function _wireBars(root){
    root = root || _host; if(!root) return;
    var parts = root.querySelectorAll('[data-w]');
    Array.prototype.forEach.call(parts, function(el){
      if(el.closest && el.closest('.lhm-quick')) return;   // the quick body's widgets carry their own bars (⋮⋮ ⚙ ⧉ ✕)
      if(el.querySelector(':scope > .lhm-wbar')) return;
      var bar = _el('div', 'lhm-wbar');
      var cfgB = _el('button', '', '⚙'); cfgB.title = 'This widget\'s record';
      cfgB.addEventListener('click', function(ev){ ev.stopPropagation();
        var S = _surface(); var box = el.classList.contains('lhm-s-added') ? el : null; var hostEl = _sideHost;
        if(box && S && hostEl){ var list = _sideAddedOf(hostEl), i = +box.getAttribute('data-added'); var rec = list[i]; if(rec){ try{ Promise.resolve(S.open({ mode:'edit', into:'side', record:rec, title:'Edit · ' + (rec.title || rec.form || 'widget'), anchor:box })).then(function(out){ if(!out) return; list[i] = out; _sideAddedSave(hostEl, list); _sideRedraw(hostEl); }).catch(function(){}); return; }catch(e){} } }
        openRecord(_tplOf(el), el.getAttribute('data-w')); });
      var saveB = _el('button', '', '⧉'); saveB.title = 'Save as a template of your own';
      saveB.addEventListener('click', function(ev){ ev.stopPropagation(); saveAsTemplate(_tplOf(el), el.getAttribute('data-w')); });
      bar.appendChild(cfgB); bar.appendChild(saveB);
      if(el.classList.contains('lhm-s-added')){ var rmB = _el('button', '', '✕'); rmB.title = 'Take it out of this menu'; rmB.addEventListener('click', function(ev){ ev.stopPropagation(); var hostEl = _sideHost; if(!hostEl) return; var list = _sideAddedOf(hostEl); list.splice(+el.getAttribute('data-added'), 1); _sideAddedSave(hostEl, list); _sideRedraw(hostEl); }); bar.appendChild(rmB); }
      if(getComputedStyle(el).position === 'static') el.style.position = 'relative';
      el.appendChild(bar);
    });
  }
  function _row(k, v){ var r = _el('div', 'lhm-wr'); r.appendChild(_el('span', 'k', k)); var vv = _el('span', 'v'); vv.innerHTML = v; r.appendChild(vv); return r; }
  function _escH(s){ return String(s == null ? '' : s).replace(/[&<>"]/g, function(c){ return { '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;' }[c]; }); }
  // a side menu's own record sheet (the harness has no mounted rail; its sheet lives inside the side menu)
  var _sideWcfg = null, _sideHost = null;
  function openRecord(tplId, label){
    if(_sideHost && _sideHost._lhmEditing && _sideWcfg) _wcfg = _sideWcfg;   // the side menu in edit mode owns the sheet
    if(!_wcfg) return;
    var hostEl = _wcfg === _sideWcfg ? _sideHost : _host;
    _wcfg.innerHTML = ''; _wcfgOpen = true; if(hostEl) hostEl.classList.add('lhm-wcfgmode');
    var hd = _el('div', 'lhm-wnote', (label || 'widget') + (tplId ? ' · ' + tplId : ' · no record yet')); _wcfg.appendChild(hd);
    var closeRow = _el('div', 'lhm-wacts'); var x = _el('button', '', '✕ close'); x.addEventListener('click', closeRecord); closeRow.appendChild(x); _wcfg.appendChild(closeRow);
    if(!tplId){ _wcfg.appendChild(_el('div', 'lhm-wnote', 'This part has no template in the registry yet — ⧉ saves it as one.')); return; }
    fetch(_base() + '/ui/widgets/template?id=' + encodeURIComponent(tplId)).then(function(r){ return r.json(); }).then(function(r){
      if(!r || !r.ok){ _wcfg.appendChild(_el('div', 'lhm-wnote', (r && r.error) || 'registry unavailable')); return; }
      var t = r.template, reads = t.reads || {}, draw = t.draw || {};
      _wcfg.appendChild(_row('template', _escH(t.id) + ' · v' + (t.version || 1)));
      _wcfg.appendChild(_row('form', _escH(t.form)));
      _wcfg.appendChild(_row('reads', reads.cap ? '<code>' + _escH(reads.cap) + '</code>' + (reads.args && Object.keys(reads.args).length ? ' ' + _escH(JSON.stringify(reads.args)) : '') + (reads.note ? ' · ' + _escH(reads.note) : '') : '—'));
      _wcfg.appendChild(_row('frame', _escH(t.frame || '—')));
      _wcfg.appendChild(_row('draw', _escH(draw.form || t.form) + ' · size ' + _escH(draw.size || 'M')));
      _wcfg.appendChild(_row('can', _escH((t.can || []).join(' · ') || '—')));
      _wcfg.appendChild(_row('placed', _escH((t.placements || []).map(function(p){ return p.where + (p.count > 1 ? ' ×' + p.count : ''); }).join(' · ') || '—')));
      var acts = _el('div', 'lhm-wacts');
      var sv = _el('button', '', '⧉ Save as my template'); sv.addEventListener('click', function(){ saveAsTemplate(tplId, label); }); acts.appendChild(sv);
      ['dashboard', 'canvas', 'LHM'].forEach(function(w){ var b = _el('button', '', '+ ' + w); b.title = 'Place into ' + w; b.addEventListener('click', function(){ placeInto(tplId, w); }); acts.appendChild(b); });
      _wcfg.appendChild(acts);
    }).catch(function(){ _wcfg.appendChild(_el('div', 'lhm-wnote', 'registry unavailable')); });
  }
  function closeRecord(){ _wcfgOpen = false; if(_host) _host.classList.remove('lhm-wcfgmode'); if(_sideHost) _sideHost.classList.remove('lhm-wcfgmode'); }
  // ── a side menu's edit mode (the Harness board's ✎): every part outlined and named, ⚙ its record, ⧉ a template ──
  var _sideEditOn = {};   // by menu id: a host re-made on every sync keeps its edit mode
  // the records added to a side menu, by menu id (the host is re-made on every sync; the records live in storage)
  function _sideAddedOf(host){ var key = host && host._lhmEditKey || ''; try{ var j = localStorage.getItem('vera.lhm.side.added.' + key); var a = j ? JSON.parse(j) : []; return Array.isArray(a) ? a : []; }catch(e){ return []; } }
  function _sideAddedSave(host, list){ var key = host && host._lhmEditKey || ''; try{ localStorage.setItem('vera.lhm.side.added.' + key, JSON.stringify(list || [])); }catch(e){} }
  function _sideRedraw(host){ if(host && host._lhmSideCfg) side(host, host._lhmSideCfg); }
  // + Add widget on a side menu: the WidgetConfig sheet into 'side'; the record is kept and drawn in the Widgets group
  function sideAdd(host, cfg){
    var S = _surface(); if(!S){ openRecord('', 'this menu'); return; }
    try{ Promise.resolve(S.open({ mode:'add', into:'side', title:'Add to ' + ((cfg && cfg.top && cfg.top.title) || 'this menu'), templates:true, sizes:['xs','s','m'] })).then(function(rec){ if(!rec) return; var list = _sideAddedOf(host); list.push(rec); _sideAddedSave(host, list); _sideRedraw(host); }).catch(function(){}); }catch(e){}
  }
  function sideEdit(host, on){
    if(!host) return false;
    var key = host._lhmEditKey || '';
    host._lhmEditing = (on == null) ? !host._lhmEditing : !!on; _sideEditOn[key] = host._lhmEditing;
    host.classList.toggle('lhm-editing', host._lhmEditing);
    var ed = host.querySelector('.lhm-s-edit'); if(ed) ed.classList.toggle('on', host._lhmEditing);
    if(host._lhmEditing){ _sideHost = host; _sideWcfg = host.querySelector('.lhm-side > .lhm-wcfg'); _wireBars(host); }
    else closeRecord();
    return host._lhmEditing;
  }
  function saveAsTemplate(tplId, label){
    var name = ''; try{ name = window.prompt('Name for your template', (label || 'widget').split(' · ')[0] + ' (mine)') || ''; }catch(e){}
    if(!name) return;
    var go = function(t){
      var copy = Object.assign({}, t || {}, { id: name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 64), name: name,
        source: { origin: 'you', from: 'the chat LHM', from_builtin: tplId || '', panel: (t && t.source && t.source.panel) || '' } });
      if(!copy.form){ copy.form = ((label || '').split(' · ')[1] || 'list').trim(); }
      if(!copy.reads) copy.reads = { cap: '', args: {} };
      copy.placed = (t && t.placements ? t.placements.map(function(p){ return p.where; }) : ['LHM']); delete copy.placements; delete copy.instances;
      return fetch(_base() + '/ui/widgets/templates/save', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ template: copy, force: !tplId }) })
        .then(function(r){ return r.json(); }).then(function(r){ if(_wcfg && _wcfgOpen) _wcfg.appendChild(_el('div', 'lhm-wnote', r && r.ok ? 'saved ' + r.template.id + ' · v' + r.template.version : 'save failed: ' + ((r && (r.error || (r.problems || []).join('; '))) || '?'))); });
    };
    if(tplId) fetch(_base() + '/ui/widgets/template?id=' + encodeURIComponent(tplId)).then(function(r){ return r.json(); }).then(function(r){ return go(r && r.ok ? r.template : null); }).catch(function(){ go(null); });
    else go(null);
  }
  function placeInto(tplId, where){
    var sid = ''; try{ sid = _cfg.sessionId ? String(_cfg.sessionId() || '') : ''; }catch(e){}
    fetch(_base() + '/ui/widgets/instantiate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: tplId, where: where, host: where === 'dashboard' ? 'main' : '', session_id: sid }) })
      .then(function(r){ return r.json(); }).then(function(r){ if(_wcfg && _wcfgOpen) _wcfg.appendChild(_el('div', 'lhm-wnote', r && r.ok ? 'placed into ' + where + ' · ' + r.instance.id : 'place failed: ' + ((r && r.error) || '?'))); });
  }

  function spec(){
    if(!_cfg) return null;
    return {
      title: _cfg.title || '', active: { menu: _active, tab: _activeTab, top: _topMode },
      menus: (_cfg.menus || []).map(function(m){ var b = 0; try{ b = m.badge ? +m.badge() : 0; }catch(e){} return { id: m.id, icon: m.icon || '•', label: m.label, title: m.title || m.label, badge: b, tabs: (m.tabs || []).map(function(t){ return { id: t.id, label: t.label }; }) }; }),
      open: _openNow().map(function(o){ return { id: o.id, label: o.label, icon: o.icon || '', origin: o.origin || '', placement: o.placement || '' }; })
    };
  }

  // ── owner ↔ host (the panel bridge's messages) ─────────────────────────
  var _lastSig = '';
  function _publish(force){
    if(!_embedded || !_cfg) return;
    var s = spec(); if(!s) return;
    var nav = { items: s.menus.map(function(m){ return { id: m.id, label: m.label }; }), active: s.active.menu, lhm: s };
    var sig = JSON.stringify(nav) + '|' + _pid;
    if(!force && sig === _lastSig) return; _lastSig = sig;
    try{ window.parent.postMessage({ type: 'vera:panel:state', panel_id: _pid, session_id: _cfg.sessionId ? String(_cfg.sessionId() || '') : '', state: { nav: nav, lhm: true } }, '*'); }catch(e){}
  }
  function _onMessage(ev){
    var d = ev.data; if(!d || typeof d !== 'object') return;
    var t = d.type || '';
    if(t === 'vera:panel:init'){ if(d.panel_id) _pid = String(d.panel_id); _publish(true); }
    else if(t === 'vera:panel:nav_hosted'){ _hosted = true; document.documentElement.classList.add('vpb-nav-hosted'); }
    else if(t === 'vera:panel:nav_unhosted'){ _hosted = false; document.documentElement.classList.remove('vpb-nav-hosted'); }
    else if(t === 'vera:panel:action' && d.action === 'nav_select'){
      var id = d.payload && d.payload.id != null ? String(d.payload.id) : '';
      var ok = id === '☰' ? (toggleTop(true), true) : pick(id);
      try{ window.parent.postMessage({ type: 'vera:panel:action_result', panel_id: _pid, action_id: d.action_id || '', action: 'nav_select', ok: !!ok, result: ok ? { menu: _active, tab: _activeTab } : null, error: ok ? null : 'no menu or tab ' + id }, '*'); }catch(e){}
    }
  }

  function mount(cfg){
    if(!cfg || !cfg.host) throw new Error('VeraLHM.mount: host required');
    _cfg = cfg; _host = cfg.host; _css();
    _embedded = (function(){ try{ return window.parent && window.parent !== window; }catch(e){ return true; } })();
    // wrap what the owner already has into the detail column, put the rail before it
    if(!_host.querySelector(':scope > .lhm-det')){
      _det = _el('div', 'lhm-det');
      while(_host.firstChild) _det.appendChild(_host.firstChild);
      _host.appendChild(_det);
    } else _det = _host.querySelector(':scope > .lhm-det');
    _rail = _el('div', 'lhm-rail'); _rail.setAttribute('data-w', 'rail · ' + (cfg.menus || []).length + ' icons · order · badges'); _rail.setAttribute('data-tpl', 'lhm:rail');
    _host.insertBefore(_rail, _det);
    _hd = _el('div', 'lhm-hd'); _hd.setAttribute('data-w', 'menu header · header'); _hd.setAttribute('data-tpl', 'lhm:header');
    _hd.appendChild(_el('h2', '', '')); _hd.appendChild(_el('span', 'lhm-meta mono', ''));
    var dp = _el('button', 'lhm-deep', 'Full ▸'); dp.type = 'button'; dp.addEventListener('click', function(){ deep(); }); _hd.appendChild(dp);
    var cl = _el('button', 'lhm-fold', '‹'); cl.type = 'button'; cl.title = 'Fold the menu to the rail'; cl.addEventListener('click', function(){ if(_cfg.onCollapse){ try{ _cfg.onCollapse(); }catch(e){} } }); _hd.appendChild(cl);
    var ed = _el('button', 'lhm-edit', '✎'); ed.title = 'Edit this menu — every part is a widget'; ed.addEventListener('click', function(){ toggleEdit(); }); _hd.appendChild(ed);
    _det.insertBefore(_hd, _det.firstChild);
    var strip = cfg.tabBar ? _det.querySelector(cfg.tabBar) : null;
    if(strip && !strip.getAttribute('data-w')) strip.setAttribute('data-w', 'tabs · strip');
    _top = _el('div', 'lhm-top'); _top.setAttribute('data-w', 'top list · list');
    _det.insertBefore(_top, _hd.nextSibling);
    _wcfg = _el('div', 'lhm-wcfg'); _wcfg.setAttribute('data-w', 'widget record · sheet');   // the record sheet, made before it is placed
    _det.insertBefore(_wcfg, _top.nextSibling);
    _quick = _el('div', 'lhm-quick'); _quick.setAttribute('data-w', 'quick body · widgets'); _det.insertBefore(_quick, _wcfg.nextSibling);
    _cta = _el('button', 'lhm-cta'); _cta.setAttribute('data-w', 'cta · button'); _cta.setAttribute('data-tpl', 'lhm:cta');
    _cta.addEventListener('click', function(){ var m = _menu(_active); if(m && m.cta && m.cta.run){ try{ m.cta.run(); }catch(e){} } });
    _det.appendChild(_cta);
    (cfg.panes || []).forEach(function(p){ var el = document.getElementById(p.id); if(!el) return; if(!el.getAttribute('data-w')) el.setAttribute('data-w', p.w || (p.id + ' · list')); if(p.tpl) el.setAttribute('data-tpl', p.tpl); });
    window.addEventListener('message', _onMessage);
    // the owner's tab strip may be clicked directly: follow it
    if(strip) strip.addEventListener('click', function(ev){ if(_picking) return; var t = ev.target && ev.target.closest ? ev.target.closest('.ctab') : null; if(!t || !cfg.tabIdOf) return; var id = ''; try{ id = cfg.tabIdOf(t) || ''; }catch(e){} if(id) setTimeout(function(){ setActiveTab(id); }, 0); });
    _host.classList.add('lhm-host');
    var first = cfg.initial || ((cfg.menus || [])[0] || {}).id;
    if(first) pick(first, { silent: !!cfg.silentInitial });
    return { pick: pick, render: render, spec: spec, toggleTop: toggleTop, toggleEdit: toggleEdit };
  }

  // ── the host side: draw another page's menu from its spec ──────────────
  // host: an element to fill; spec: what the owner published (state.nav.lhm); pick(id): send it back
  function absorb(host, spec, pickFn, opts){
    if(!host || !spec) return null;
    opts = opts || {}; _css(host.ownerDocument);
    host.innerHTML = '';
    var wrap = _el('div', 'lhm-absorbed');
    var rail = _el('div', 'lhm-rail'); rail.setAttribute('data-w', 'rail · absorbed · ' + (spec.menus || []).length + ' icons');
    var top = _el('div', 'lhm-ico top' + (opts.topOn ? ' on' : ''), '☰'); top.title = opts.topTitle || 'This page\'s own menu';
    top.addEventListener('click', function(){ if(opts.onTop) opts.onTop(); });
    rail.appendChild(top);
    var act = spec.active || {};
    (spec.menus || []).forEach(function(m){
      var ico = _el('div', 'lhm-ico' + (m.id === act.menu ? ' on' : ''), m.icon || '•'); ico.title = m.label;
      if(m.badge) ico.appendChild(_el('span', 'lhm-badge', String(m.badge)));
      ico.addEventListener('click', function(){ pickFn(m.id); });
      rail.appendChild(ico);
    });
    rail.appendChild(_el('div', 'lhm-sp'));
    wrap.appendChild(rail);
    var tabs = _el('div', 'lhm-tabs'); tabs.setAttribute('data-w', 'tabs · absorbed');
    var cur = (spec.menus || []).filter(function(m){ return m.id === act.menu; })[0];
    if(cur){
      tabs.appendChild(_el('div', 'lhm-ttl', cur.title || cur.label));
      (cur.tabs || []).forEach(function(t){
        var e = _el('div', 'lhm-tab' + (t.id === act.tab ? ' on' : ''), t.label);
        e.addEventListener('click', function(){ pickFn(cur.id + '/' + t.id); });
        tabs.appendChild(e);
      });
    }
    wrap.appendChild(tabs);
    host.appendChild(wrap);
    return wrap;
  }

  // ── the top-level SIDE menu: the harness's main LHM (Notes/40 §9; the Harness board) ─────────────
  // host: an element to fill. cfg: { search:{label, hint, open()}, open:[{id, label, icon, origin, placement, active,
  // close(), focus()}], panels:[{id, label, icon, ct, active, open, sections:[{id, label, on, nav, tabs:[{id, label,
  // on}]}]}], onPanel(id, ev), onSection(pid, sid, index), onTab(pid, section, tab), registered:{n, open()},
  // widgets:[{title, count, el, cls, open()}], note }. Every part carries data-w so edit mode can name it.
  function side(host, cfg){
    if(!host) return null; cfg = cfg || {}; _css(host.ownerDocument);
    host.innerHTML = '';
    var wrap = _el('div', 'lhm-side');
    host._lhmEditKey = cfg.id || ''; host._lhmSideCfg = cfg; if(_sideEditOn[host._lhmEditKey]) host._lhmEditing = true;
    // the top row (the Harness board): ☰ swaps this list for the open UI's own menu (or the tabs), the title, the count
    if(cfg.top){ var top = _el('div', 'lhm-s-top'); top.setAttribute('data-w', 'top row · header');
      var tb = _el('button', 'lhm-s-tb' + (cfg.top.on ? ' on' : ''), '☰'); tb.type = 'button'; tb.title = cfg.top.toggleTitle || 'Swap this menu'; tb.addEventListener('click', function(ev){ ev.stopPropagation(); if(cfg.top.toggle) cfg.top.toggle(ev); }); top.appendChild(tb);
      top.appendChild(_el('span', 'nm', cfg.top.title || 'Vera')); if(cfg.top.sub) top.appendChild(_el('span', 'mono', cfg.top.sub));
      wrap.appendChild(top); }
    var hd = _el('div', 'lhm-s-hd');
    if(cfg.search){
      var s = _el('div', 'lhm-s-srch'); s.setAttribute('data-w', 'search · search');
      s.innerHTML = '<svg width="12" height="12" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="6" cy="6" r="4.2"/><path d="m9.3 9.3 3 3"/></svg>';
      s.appendChild(_el('span', '', cfg.search.label || 'Find a panel')); s.appendChild(_el('span', 'k', cfg.search.hint || '⌘K'));
      s.addEventListener('click', function(){ if(cfg.search.open) cfg.search.open(); });
      hd.appendChild(s);
    }
    // ✎ — this menu's edit mode: every part is a widget (its record behind ⚙, ⧉ saves it as a template)
    if(cfg.edit !== false){ var ed = _el('button', 'lhm-s-edit' + (host._lhmEditing ? ' on' : ''), '✎'); ed.type = 'button'; ed.title = 'Edit this menu — every part is a widget: ⚙ its record, ⧉ save it as a template'; ed.addEventListener('click', function(){ sideEdit(host); }); hd.appendChild(ed); }
    if(hd.childNodes.length) wrap.appendChild(hd);
    if(cfg.edit !== false) wrap.appendChild(_ebar((cfg.top && cfg.top.title) || 'this menu', function(){ sideAdd(host, cfg); }, function(){ sideEdit(host, false); }));
    var bd = _el('div', 'lhm-s-bd');
    // ONE set of open panels: opened by you or by the aide, wherever they sit
    var open = cfg.open || [];
    var g1 = _el('div', 'lhm-s-grp', 'Open now · ' + open.length + ' · one set, one bridge'); g1.setAttribute('data-w', 'open now · list'); bd.appendChild(g1);
    if(!open.length) bd.appendChild(_el('div', 'lhm-s-empty', 'Nothing open. Pick a panel below, or the aide can open one here.'));
    open.forEach(function(o){
      var r = _el('div', 'lhm-s-row open' + (o.active ? ' on' : '')); r.title = 'opened by ' + (o.origin || 'you') + (o.placement ? ' · ' + o.placement : '');
      r.appendChild(_el('span', 'ico', o.icon || '▭'));
      var nm = _el('span', 'nm'); nm.appendChild(_el('span', '', o.label || o.id)); nm.appendChild(_el('em', '', [o.origin || 'you', o.placement || ''].filter(Boolean).join(' · '))); r.appendChild(nm);
      if(o.close){ var x = _el('span', 'x', '✕'); x.title = 'Close'; x.addEventListener('click', function(ev){ ev.stopPropagation(); try{ o.close(); }catch(e){} }); r.appendChild(x); }
      r.addEventListener('click', function(){ if(o.focus) o.focus(); });
      bd.appendChild(r);
    });
    // the panels, the active one opened as an accordion: its sections, the open section's tabs
    var g2 = _el('div', 'lhm-s-grp', 'Panels'); g2.setAttribute('data-w', 'panels · tree'); bd.appendChild(g2);
    (cfg.panels || []).forEach(function(p){
      var pan = _el('div', 'lhm-s-pan' + (p.active ? ' on' : ''));
      var r = _el('div', 'lhm-s-row' + (p.active ? ' on' : p.open ? ' open' : '')); r.setAttribute('data-w', 'panel · ' + (p.label || p.id));
      r.appendChild(_el('span', 'ico', p.icon || '▭')); var nm = _el('span', 'nm'); nm.appendChild(_el('span', '', p.label || p.id)); r.appendChild(nm);
      if(p.ct) r.appendChild(_el('span', 'ct', String(p.ct)));
      r.addEventListener('click', function(ev){ if(cfg.onPanel) cfg.onPanel(p.id, ev); });
      pan.appendChild(r);
      (p.sections || []).forEach(function(sec, i){
        var se = _el('div', 'lhm-s-sec' + (sec.on ? ' on' : ''));
        var h = _el('div', 'lhm-s-sech'); h.appendChild(_el('i')); h.appendChild(_el('span', '', sec.label || sec.id));
        h.addEventListener('click', function(ev){ ev.stopPropagation(); if(cfg.onSection) cfg.onSection(p.id, sec.id, i); });
        se.appendChild(h);
        if(sec.on) (sec.tabs || []).forEach(function(t){ var o = _el('div', 'lhm-s-opt' + (t.on ? ' on' : ''), t.label || t.id); o.addEventListener('click', function(ev){ ev.stopPropagation(); if(cfg.onTab) cfg.onTab(p.id, sec, t); }); se.appendChild(o); });
        pan.appendChild(se);
      });
      bd.appendChild(pan);
    });
    if(cfg.registered && cfg.registered.n){ var reg = _el('div', 'lhm-s-row lhm-s-reg'); reg.appendChild(_el('span', 'nm', cfg.registered.n + ' registered · ⌘K')); reg.title = 'Every registered panel — add one as a tab'; reg.addEventListener('click', function(){ if(cfg.registered.open) cfg.registered.open(); }); bd.appendChild(reg); }
    // the LHM is a widget host: live events, running loops, whatever the host hands it
    var ws = (cfg.widgets || []).filter(Boolean);
    if(ws.length){ var g3 = _el('div', 'lhm-s-grp', 'Widgets'); g3.setAttribute('data-w', 'widgets · host'); bd.appendChild(g3); }
    ws.forEach(function(w){
      var box = _el('div', 'lhm-s-w' + (w.cls ? ' ' + w.cls : '')); box.setAttribute('data-w', 'widget · ' + (w.title || ''));
      var h = _el('div', 'lhm-s-wh'); h.appendChild(_el('span', '', w.title || 'widget')); if(w.count != null) h.appendChild(_el('b', '', String(w.count)));
      if(w.open) h.addEventListener('click', function(){ w.open(); });
      box.appendChild(h);
      // meters: [{label, pct, value, col}] — a bar per row (the board's GPU · queue widget)
      if(Array.isArray(w.bars) && w.bars.length){ w.bars.forEach(function(m){ var mh = _el('div', 'lhm-s-wh lhm-s-wm'); mh.appendChild(_el('span', '', m.label || '')); mh.appendChild(_el('b', '', m.value == null ? '' : String(m.value))); box.appendChild(mh);
        var bar = _el('div', 'lhm-s-wbar'); var fill = _el('i'); fill.style.width = Math.max(0, Math.min(100, +m.pct || 0)) + '%'; if(m.col) fill.style.background = m.col; bar.appendChild(fill); box.appendChild(bar); }); }
      if(w.el){ var body = _el('div', 'lhm-s-wbody'); body.appendChild(w.el); box.appendChild(body); }
      bd.appendChild(box);
    });
    // the records you added to this menu (the sheet's), drawn live; kept per menu
    var added = _sideAddedOf(host);
    if(added.length){ if(!ws.length){ var g4 = _el('div', 'lhm-s-grp', 'Widgets'); g4.setAttribute('data-w', 'widgets · host'); bd.appendChild(g4); }
      added.forEach(function(rec, i){ var box = _el('div', 'lhm-s-w lhm-s-added'); box.setAttribute('data-w', (rec.title || rec.form || 'widget') + ' · ' + (rec.form || 'widget')); box.setAttribute('data-added', String(i));
        var h = _el('div', 'lhm-s-wh'); h.appendChild(_el('span', '', rec.title || rec.form || 'widget')); h.appendChild(_el('b', 'mono', rec.source || 'sample')); box.appendChild(h);
        var body = _el('div', 'lhm-s-wbody'); if(window.customElements && customElements.get('vera-widget')){ var vw = document.createElement('vera-widget'); try{ vw.setAttribute('record', JSON.stringify(rec)); }catch(e){} vw.setAttribute('size', (rec.frame && rec.frame.size) || 's'); body.appendChild(vw); }
        else { var drawn = ''; try{ if(window.VeraWidget && rec.form) drawn = window.VeraWidget.draw(rec.form, _sample(rec.form), 's', { bare:true, title:rec.title }); }catch(e){} body.innerHTML = drawn || _escH(rec.form || 'widget'); }
        box.appendChild(body); bd.appendChild(box); }); }
    wrap.appendChild(bd);
    if(cfg.note) wrap.appendChild(_el('div', 'lhm-s-note', cfg.note));
    if(cfg.edit !== false) wrap.appendChild(_el('div', 'lhm-wcfg'));   // the record sheet, opened by ⚙ in edit mode
    host.appendChild(wrap);
    if(host._lhmEditing) sideEdit(host, true);   // a re-render keeps the menu in edit mode
    return wrap;
  }
  // ── the STRIPS under a tab bar (tabs mode): the active panel's sections, then the open section's tabs ──
  // cfg: { title, sections:[{id, label, on}], tabs:[{id, label, on}], onSection(sid, index), onTab(tab) }
  function strips(host, cfg){
    if(!host) return null; cfg = cfg || {}; _css(host.ownerDocument);
    host.innerHTML = '';
    var secs = cfg.sections || [];
    if(!secs.length) return null;
    var row = _el('div', 'lhm-strips'); row.setAttribute('data-w', 'sub-tabs · strip');
    if(cfg.title) row.appendChild(_el('span', 'lhm-st-p', cfg.title));
    secs.forEach(function(s, i){ var b = _el('button', 'lhm-st' + (s.on ? ' on' : ''), s.label || s.id); b.type = 'button'; b.addEventListener('click', function(){ if(cfg.onSection) cfg.onSection(s.id, i); }); row.appendChild(b); });
    var tabs = cfg.tabs || [];
    if(tabs.length){ row.appendChild(_el('span', 'lhm-st-sep')); tabs.forEach(function(t){ var b = _el('button', 'lhm-st-o' + (t.on ? ' on' : ''), t.label || t.id); b.type = 'button'; b.addEventListener('click', function(){ if(cfg.onTab) cfg.onTab(t); }); row.appendChild(b); }); }
    host.appendChild(row);
    return row;
  }

  window.VeraLHM = { mount: mount, pick: pick, setActiveTab: setActiveTab, toggleTop: toggleTop, toggleEdit: toggleEdit, render: render, spec: spec, absorb: absorb, side: side, sideEdit: sideEdit, sideAdd: sideAdd, strips: strips, css: _css,
    openRecord: openRecord, closeRecord: closeRecord, saveAsTemplate: saveAsTemplate, placeInto: placeInto,
    compose: compose, composeUndo: composeUndo, saveAsMenu: saveAsMenu, addMenus: addMenus, deep: deep, openPicker: openPicker, closePicker: closePicker,
    get active(){ return { menu: _active, tab: _activeTab, top: _topMode, editing: _editing, hosted: _hosted, embedded: _embedded }; } };
})();
