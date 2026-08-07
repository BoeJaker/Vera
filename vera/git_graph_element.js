/**
 * <vera-git-graph> — a real commit DAG for a repo: every commit (across ALL
 * branches, not just HEAD's ancestry), laned and connected exactly like
 * gitk/GitHub's graph view. This is deliberately NOT the same thing as
 * <vera-branch-pipeline mode="lanes"> (a race-chart of Loop Lab pipeline RUNS
 * over time) — this shows the actual shape of the repository's history.
 *
 * Data: GET /evolve/git/graph?repo=&limit= — newest-first (git's natural log
 * order), each commit with its real parent hashes and ref decorations. No
 * synthetic/guessed edges: every line drawn corresponds to a real parent
 * pointer; every branch/tag label comes straight from `%D`.
 *
 * Lane assignment (standard gitk-style walk, newest → oldest): each active
 * "lane" holds the hash it's waiting for. A commit occupies whichever lane is
 * waiting for its hash (or opens a new one if none is). Its parent hashes are
 * queued as pending edges, resolved — and drawn — the moment a later (older)
 * commit in the walk matches that hash. A merge commit (2+ parents) draws one
 * edge per parent, so the fork/merge shape is exact, not approximated.
 *
 * Truthful animation: none — a commit graph is a historical record, not a
 * live process; it simply refreshes (poll + the same evolve.branch created/
 * deleted and evolve.pipeline.promoted event types <vera-branch-pipeline>
 * already listens for, since those are exactly the actions that change repo
 * history).
 *
 * Attributes: repo (default 'vera'), limit (default 150)
 * Public API: setApiBase(url), setRepo(id), refresh()
 */
(function () {
  if (customElements.get('vera-git-graph')) return;

  const LANE_W = 16;
  const ROW_H = 22;
  const DOT_R = 4;
  const LANE_COLORS = ['#5a9e8f', '#d9a441', '#c96b6b', '#6d9ac9', '#a889c9',
    '#8fb96d', '#c98fae', '#7e7562'];

  const TMPL = document.createElement('template');
  TMPL.innerHTML = `
<style>
:host{display:block;width:100%;font-family:var(--sans,system-ui,sans-serif);font-size:11px;
  color:var(--text,var(--t1,#ddd5c8))}
*,*::before,*::after{box-sizing:border-box}
.hdr{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.hdr .ttl{font-weight:600;color:var(--dim2,var(--t3,#8a7e70));text-transform:uppercase;
  font-size:10px;letter-spacing:.04em}
.hdr .count{margin-left:auto;color:var(--dim2,var(--t3,#8a7e70));font-size:9.5px}
.wrap{max-height:420px;overflow:auto;position:relative}
.row{display:flex;align-items:center;gap:8px;height:${ROW_H}px}
.row svg{flex-shrink:0;display:block;overflow:visible}
.row .hash{font-family:var(--mono,monospace);color:var(--dim2,var(--t3,#8a7e70));
  flex-shrink:0;font-size:9.5px}
.row .subj{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.row .refs{display:flex;gap:3px;flex-shrink:0}
.ref{font-size:8.5px;padding:1px 5px;border-radius:8px;border:1px solid var(--border,var(--bd2,#3a3530));
  color:var(--dim2,var(--t3,#8a7e70));white-space:nowrap}
.ref.looplab{border-color:var(--acc,var(--ac,#5a9e8f));color:var(--acc,var(--ac,#5a9e8f))}
.ref.main{border-color:var(--ok,var(--ac2,#6db87a));color:var(--ok,var(--ac2,#6db87a))}
.empty{padding:14px;text-align:center;color:var(--dim2,var(--t3,#8a7e70))}
</style>
<div class="hdr"><span class="ttl">Commit graph</span><span class="count" id="count"></span></div>
<div class="wrap" id="wrap"></div>`;

  class VeraGitGraph extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: 'open' });
      this.shadowRoot.appendChild(TMPL.content.cloneNode(true));
      this._base = '';
      this._pollTimer = null;
      this._ws = null;
    }

    connectedCallback() {
      this._repo = this.getAttribute('repo') || 'vera';
      this._limit = parseInt(this.getAttribute('limit') || '150', 10);
      this._connectWs();
      this.refresh();
      this._pollTimer = setInterval(() => this.refresh(), 20000);
    }

    disconnectedCallback() {
      if (this._pollTimer) clearInterval(this._pollTimer);
      try { this._ws && this._ws.close(); } catch (_) {}
    }

    setApiBase(url) { this._base = (url || '').replace(/\/$/, ''); }
    _getBase() {
      return this._base || window._veraBase || window.location.origin ||
        (window.__VERA_BASE__ || ('http://' + location.hostname + ':8999'));
    }
    setRepo(id) { this._repo = id || 'vera'; this.refresh(); }

    _connectWs() {
      try {
        const wsUrl = this._getBase().replace(/^http/, 'ws') + '/ws';
        this._ws = new WebSocket(wsUrl);
        this._ws.onopen = () => { try { this._ws.send(JSON.stringify({ action: 'subscribe', stream: 'vera:events' })); } catch (_) {} };
        this._ws.onmessage = e => {
          try {
            const ev = JSON.parse(e.data);
            const t = (ev && ev.type) || '';
            if (t === 'evolve.pipeline.promoted' || t === 'evolve.branch.created' ||
                t === 'evolve.branch.deleted') this.refresh();
          } catch (_) {}
        };
        this._ws.onclose = () => { setTimeout(() => this._connectWs(), 3000); };
        this._ws.onerror = () => { try { this._ws.close(); } catch (_) {} };
      } catch (_) { setTimeout(() => this._connectWs(), 5000); }
    }

    async refresh() {
      let d;
      try {
        const r = await fetch(this._getBase() + '/evolve/git/graph?repo=' +
          encodeURIComponent(this._repo) + '&limit=' + this._limit);
        d = await r.json();
      } catch (_) { d = null; }
      const commits = (d && d.commits) || [];
      this._render(commits, d && d.error);
    }

    // Standard gitk-style lane walk over NEWEST-FIRST commits. See file header.
    _layout(commits) {
      const lanes = [];              // lanes[i] = hash that lane is waiting for, or null
      const pending = [];            // [{fromRow, fromLane, wantHash}]
      const rows = [];               // [{lane, commit}]
      const edges = [];              // [{fromRow, fromLane, toRow, toLane}]
      commits.forEach((c, row) => {
        let li = lanes.indexOf(c.hash);
        if (li === -1) {
          li = lanes.indexOf(null);
          if (li === -1) { li = lanes.length; lanes.push(null); }
        }
        rows.push({ lane: li, commit: c });
        // resolve any earlier (newer) commit's parent pointer that wanted THIS hash
        for (let i = pending.length - 1; i >= 0; i--) {
          if (pending[i].wantHash === c.hash) {
            edges.push({ fromRow: pending[i].fromRow, fromLane: pending[i].fromLane, toRow: row, toLane: li });
            pending.splice(i, 1);
          }
        }
        const parents = c.parents || [];
        if (!parents.length) {
          lanes[li] = null; // lane terminates here (root commit)
        } else {
          lanes[li] = parents[0];
          parents.forEach(p => {
            if (lanes.indexOf(p) === -1) {
              // a parent not already claimed by an active lane needs one reserved
              // now (a merge target further down), otherwise two different rows
              // could independently claim the same free slot.
              let pli = lanes.indexOf(null);
              if (pli === -1 || pli === li) { pli = lanes.length; lanes.push(p); }
              else lanes[pli] = p;
            }
            pending.push({ fromRow: row, fromLane: li, wantHash: p });
          });
        }
      });
      return { rows, edges, laneCount: lanes.length };
    }

    _render(commits, error) {
      const wrap = this.shadowRoot.getElementById('wrap');
      this.shadowRoot.getElementById('count').textContent = commits.length ? (commits.length + ' commits') : '';
      if (error) { wrap.innerHTML = '<div class="empty">' + this._esc(error) + '</div>'; return; }
      if (!commits.length) { wrap.innerHTML = '<div class="empty">No commits.</div>'; return; }
      const { rows, edges, laneCount } = this._layout(commits);
      const svgW = Math.max(40, laneCount * LANE_W + LANE_W);
      const totalH = rows.length * ROW_H;
      // one edge layer sized to the whole scrollable list, positioned under the rows
      let edgeSvg = '<svg width="' + svgW + '" height="' + totalH + '" style="position:absolute;left:0;top:0;pointer-events:none">';
      edges.forEach(e => {
        const x1 = LANE_W / 2 + e.fromLane * LANE_W, y1 = e.fromRow * ROW_H + ROW_H / 2;
        const x2 = LANE_W / 2 + e.toLane * LANE_W, y2 = e.toRow * ROW_H + ROW_H / 2;
        const col = LANE_COLORS[e.fromLane % LANE_COLORS.length];
        edgeSvg += '<path d="M' + x1 + ',' + y1 + ' C' + x1 + ',' + ((y1 + y2) / 2) +
          ' ' + x2 + ',' + ((y1 + y2) / 2) + ' ' + x2 + ',' + y2 +
          '" stroke="' + col + '" stroke-width="1.5" fill="none" opacity="0.75"/>';
      });
      edgeSvg += '</svg>';
      const rowsHtml = rows.map(({ lane, commit: c }) => {
        const col = LANE_COLORS[lane % LANE_COLORS.length];
        const refs = (c.refs || []).map(r => {
          const name = r.replace(/^HEAD -> /, '').replace(/^origin\//, '');
          const cls = name.startsWith('loop-lab/') ? 'looplab' : (name === 'main' || name === 'master') ? 'main' : '';
          return '<span class="ref ' + cls + '">' + this._esc(name) + '</span>';
        }).join('');
        return '<div class="row">' +
          '<svg width="' + svgW + '" height="' + ROW_H + '" style="flex-shrink:0">' +
          '<circle cx="' + (LANE_W / 2 + lane * LANE_W) + '" cy="' + (ROW_H / 2) + '" r="' + DOT_R +
          '" fill="' + col + '"/></svg>' +
          '<span class="hash">' + this._esc((c.hash || '').slice(0, 7)) + '</span>' +
          (refs ? '<span class="refs">' + refs + '</span>' : '') +
          '<span class="subj">' + this._esc(c.subject || '') + '</span>' +
          '</div>';
      }).join('');
      wrap.innerHTML = '<div style="position:relative">' + edgeSvg +
        '<div style="position:relative">' + rowsHtml + '</div></div>';
    }

    _esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
  }

  customElements.define('vera-git-graph', VeraGitGraph);
})();
