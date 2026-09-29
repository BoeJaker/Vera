// the ISO FRAME look, shared by the gallery and the tri-page (the tri-page copy is scoped)
export const FRAME_CSS = `
.xif{position:absolute;transform-origin:0 0;z-index:9;background:var(--s1);box-shadow:inset 0 0 0 1px var(--bd2);border-radius:2px;overflow:hidden;font-family:var(--f-mono);font-size:7.5px;line-height:1.35;color:var(--t2);pointer-events:none;display:flex;flex-direction:column}
.xif-hd{flex-shrink:0;display:flex;align-items:center;gap:3px;height:11px;padding:0 5px;background:var(--s2);color:var(--t3);font-size:6.5px;font-family:var(--f-mono)}
.xif-hd i{width:4px;height:4px;border-radius:50%;background:var(--bd2);flex-shrink:0}
.xif-hd i:nth-child(1){background:var(--ac4)}
.xif-hd i:nth-child(2){background:var(--ac3)}
.xif-hd i:nth-child(3){background:var(--ac2)}
.xif-hd span{margin-left:3px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.xif-bd{flex:1;min-height:0;padding:4px 6px;display:flex;flex-direction:column;gap:2px;overflow:hidden}
.xif-ln{display:flex;gap:6px;white-space:nowrap;overflow:hidden;flex-shrink:0}
.xif-ln .v{margin-left:auto;color:var(--t3);flex-shrink:0}
.xif-ln .v:empty{display:none}
.xif-ln.p{color:var(--ac2)}
.xif-ln.ac{color:var(--ac)}
.xif-ln.dim{color:var(--t3)}
.xif-ln.ok .v{color:var(--ac2)}
.xif-ln.bad .v{color:var(--ac4)}
.xif-ln.warn .v{color:var(--ac3)}
.xif-ln.cur{animation:xif-blink 1s steps(2) infinite}
@keyframes xif-blink{to{opacity:.2}}
.xif-ln.h{font-family:var(--f-ui);font-size:9px;font-weight:600;color:var(--t1);white-space:normal}
.xif-ln.sh{font-family:var(--f-ui);font-size:7.5px;font-weight:600;color:var(--t2)}
.xif.page,.xif.web,.xif.form{font-family:var(--f-ui);background:var(--surf)}
.xif.page .xif-ln,.xif.web .xif-ln{white-space:normal;display:block;font-size:6.8px;line-height:1.45}
.xif-ln.code{font-family:var(--f-mono);color:var(--t1);background:var(--s2);padding:1px 4px;border-radius:2px;border-left:2px solid var(--bd2)}
.xif-ln.run{border-left-color:var(--ac)}
.xif-ln.out{font-family:var(--f-mono);color:var(--ac2);padding-left:6px}
.xif-ln.url{font-family:var(--f-mono);background:var(--s2);border-radius:6px;padding:1px 6px;color:var(--t3);font-size:6.5px;margin-bottom:2px}
.xif-ln.row{padding:1px 0;box-shadow:0 1px 0 var(--bd)}
.xif-ln.row .k{display:flex;align-items:center;gap:4px}
.xif-ln.row .k::before{content:'';width:5px;height:5px;border-radius:50%;background:var(--t3);flex-shrink:0}
.xif-ln.ok .k::before{background:var(--ac2)}
.xif-ln.bad .k::before{background:var(--ac4)}
.xif-ln.warn .k::before{background:var(--ac3)}
.xif-ln.field{flex-direction:column;gap:1px;padding:1px 0}
.xif-ln.field .k{font-family:var(--f-ui);font-size:6.5px;color:var(--t3)}
.xif-ln.field .v{margin:0;height:9px;border-radius:2px;background:var(--s2);box-shadow:inset 0 0 0 1px var(--bd);color:var(--t1);padding:0 3px;font-size:6.5px;line-height:9px;overflow:hidden}
.xif-ln.field.lit .v{box-shadow:inset 0 0 0 1px var(--ac)}
.xif-ln.btn{align-self:flex-end;background:var(--ac);color:#0b0d11;border-radius:3px;padding:1px 6px;font-family:var(--f-ui);font-weight:600;font-size:6.5px}
.xif-ln.msg{background:var(--s2);border-radius:5px;padding:2px 5px;white-space:normal;max-width:84%;font-family:var(--f-ui);font-size:6.8px}
.xif-ln.me{align-self:flex-end;background:color-mix(in srgb,var(--ac) 22%,var(--s2))}
.xif-ln.col{flex-direction:column;gap:2px;flex:1;background:var(--s2);border-radius:3px;padding:3px 4px;overflow:hidden}
.xif-ln.col .k{font-family:var(--f-ui);font-size:6.5px;font-weight:600;color:var(--t3);text-transform:uppercase;letter-spacing:.06em}
.xif-ln.col .v{margin:0;display:flex;flex-direction:column;gap:2px}
.xif-cols{display:flex;gap:3px;flex:1}
.xif-bars{display:flex;align-items:flex-end;gap:3px;height:22px;margin-top:auto}
.xif-bars i{flex:1;background:var(--ac);border-radius:1px 1px 0 0;opacity:.85}
.xif-bars:empty{display:none}
.xif.chart .xif-bars{height:64px}
.xif.term{background:#0b0d11;color:#b9c2d0}
.xif.term .xif-hd{background:#151920}
.xif-tiles,.xif-img{display:none}
.xif.dash .xif-tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:3px;flex:1}
.xif-tiles i{background:var(--s2);border-radius:2px;box-shadow:inset 0 0 0 1px var(--bd)}
.xif-tiles i:nth-child(1){grid-column:span 2;background:color-mix(in srgb,var(--ac) 18%,var(--s2))}
.xif-tiles i:nth-child(4){background:color-mix(in srgb,var(--ac2) 18%,var(--s2))}
.xif.img .xif-img{display:block;flex:1;border-radius:2px;background:linear-gradient(135deg,color-mix(in srgb,var(--ac5) 45%,var(--s2)),color-mix(in srgb,var(--ac) 30%,var(--s2)) 60%,var(--s2))}
.xif-ln.bar{color:var(--ac);font-family:var(--f-mono)}
`.trim();
// the markup for one frame, with the field names of the object it reads from
export const frameTpl = (p) => `<div class="xif {{${p}k}}" style="left: {{${p}x}}; top: {{${p}y}}; width: {{${p}w}}; height: {{${p}h}}; transform: {{${p}tf}}"><div class="xif-hd"><i></i><i></i><i></i><span>{{${p}t}}</span></div><div class="xif-bd"><sc-for list="{{${p}lines}}" as="ln" hint-placeholder-count="6"><div class="xif-ln {{ln.c}}"><span class="k">{{ln.a}}</span><span class="v">{{ln.b}}</span></div></sc-for><div class="xif-tiles"><i></i><i></i><i></i><i></i><i></i><i></i></div><div class="xif-img"></div><div class="xif-bars"><sc-for list="{{${p}bars}}" as="bb" hint-placeholder-count="0"><i style="height: {{bb}}"></i></sc-for></div></div></div>`;
// lines as written in data ([text, cls] or [key, value, cls]) → what the template reads
export const LINES_JS = `(raw) => (raw || []).map((l) => l.length === 2 ? { a:l[0], b:'', c:l[1] } : { a:l[0], b:l[1], c:l[2] || '' })`;
