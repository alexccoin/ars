/* ==========================================================================
   A.R.S — the brain network
   services/gateway/src/ars_gateway/ui/panels/brain.js

   What A.R.S knows, drawn as a living network instead of a list.

     document   a file you gave it            hexagon, cyan
     passage    a slice of that file           circle,  teal
     fact       an answer it worked out         diamond, violet
     contains   document -> passage             solid hairline
     translation passage -> the same passage
                in another language             dashed, travelling

   The point is growth. This never redraws from scratch: it polls
   /api/knowledge, diffs by id, and *adds* what is new. A new node is born at
   its parent, flies out on a force-directed layout and settles, with a ring
   pulse and a line in the ticker. Watching a document get translated in the
   background — three passages becoming nine — is the whole feature.

   Rendering is 2D canvas, hand-written, no dependencies. A graph this size
   (hundreds of nodes) does not need WebGL, and canvas gives crisp hairlines
   and real text at any DPR, which a HUD needs more than it needs shaders. If
   the browser has no 2D context at all, the panel degrades to a nested list of
   the same data — still legible, still honest.

   Budget: the layout is O(n) against a uniform grid, not O(n^2); the whole
   loop stops when the graph settles, when the tab is not selected and when
   the document is hidden, so it costs nothing while A.R.S is answering. Frame
   time is measured every frame and the renderer drops its halos before it
   drops frames. `stage.__brain.stats()` reports what it measured.

   Every colour is a --ars-* token from theme.css. Reduced motion is respected
   by simulating to rest in one burst and then drawing only on change.
   ========================================================================== */

import { t, LANG_LABEL } from './i18n.js';

const POLL_MS = 4000;

/* ---- layout constants (world units) ---------------------------------- */
const LINK_CONTAINS = 86;
const LINK_TRANSLATION = 52;
const CHARGE = 2400;
const ALPHA_DECAY = 0.0165;
const ALPHA_MIN = 0.006;
const VELOCITY_DECAY = 0.45;
const GRID_CELL = 120;

const KIND_ORDER = { document: 0, passage: 1, fact: 2 };
const LANG_ANGLE = { en: -Math.PI / 2, ro: Math.PI / 6, de: (5 * Math.PI) / 6 };

/* ---- tiny helpers ----------------------------------------------------- */

function el(tag, className, attrs) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null) continue;
      if (k === 'text') node.textContent = v;
      else node.setAttribute(k, v);
    }
  }
  return node;
}

const clamp = (v, a, b) => (v < a ? a : v > b ? b : v);
const approach = (cur, target, tau, dt) => cur + (target - cur) * (1 - Math.exp(-dt / Math.max(tau, 1e-4)));
const easeOut = (x) => 1 - Math.pow(1 - clamp(x, 0, 1), 3);

function parseColor(str, fallback) {
  const s = String(str || '').trim();
  let m = s.match(/^#([0-9a-f]{3,8})$/i);
  if (m) {
    let h = m[1];
    if (h.length === 3 || h.length === 4) h = h.split('').map((c) => c + c).join('');
    const n = parseInt(h.slice(0, 6), 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  m = s.match(/^rgba?\(([^)]+)\)$/i);
  if (m) {
    const p = m[1].split(/[\s,/]+/).filter(Boolean).map(parseFloat);
    if (p.length >= 3) return [p[0], p[1], p[2]];
  }
  if (s && typeof document !== 'undefined') {
    try {
      const probe = document.createElement('span');
      probe.style.cssText = 'display:none';
      probe.style.color = s;
      document.body.appendChild(probe);
      const resolved = getComputedStyle(probe).color;
      probe.remove();
      if (resolved && resolved !== s) return parseColor(resolved, fallback);
    } catch { /* detached document */ }
  }
  return fallback;
}

const rgba = (c, a) => `rgba(${c[0]|0}, ${c[1]|0}, ${c[2]|0}, ${a})`;

/* ---- styles ----------------------------------------------------------- */

const BRAIN_CSS = `
.ars-brain .hud-panel__body {
  max-height: none;
  min-height: 0;
  flex: 1 1 auto;
  gap: var(--ars-space-2);
  overflow: hidden;
}
.ars-brain__stage {
  position: relative;
  flex: 1 1 auto;
  min-height: 260px;
  border: var(--ars-hairline) solid var(--ars-border);
  border-radius: var(--ars-radius-sm);
  background: var(--ars-surface-sunken);
  box-shadow: var(--ars-inner-well);
  overflow: hidden;
  touch-action: none;
}
.ars-brain__canvas { display: block; width: 100%; height: 100%; cursor: grab; outline: none; }
.ars-brain__canvas[data-grabbing="1"] { cursor: grabbing; }
.ars-brain__canvas[data-over="1"] { cursor: pointer; }
.ars-brain__canvas:focus-visible { box-shadow: var(--ars-focus-ring); }
.ars-brain__empty {
  position: absolute; inset: 0;
  display: flex; align-items: center; justify-content: center;
  padding: var(--ars-space-5);
  text-align: center;
  font-size: var(--ars-text-xs);
  color: var(--ars-text-muted);
  pointer-events: none;
}
.ars-brain__tools {
  position: absolute; right: var(--ars-space-2); top: var(--ars-space-2);
  display: flex; gap: var(--ars-space-1);
}
.ars-brain__btn {
  display: inline-flex; align-items: center; justify-content: center;
  min-width: 22px; height: 20px; padding: 0 var(--ars-space-1);
  background: color-mix(in srgb, var(--ars-surface-raised) 82%, transparent);
  border: var(--ars-hairline) solid var(--ars-border);
  border-radius: var(--ars-radius-xs);
  color: var(--ars-text-muted);
  font-family: var(--ars-font-display);
  font-size: var(--ars-text-2xs);
  font-weight: var(--ars-weight-semi);
  letter-spacing: var(--ars-track-label);
  text-transform: uppercase;
  cursor: pointer;
  transition: color var(--ars-dur-fast) var(--ars-ease-mech),
              border-color var(--ars-dur-fast) var(--ars-ease-mech);
}
.ars-brain__btn:hover { color: var(--ars-text-accent); border-color: var(--ars-border-strong); }

.ars-brain__legend { display: flex; flex-wrap: wrap; gap: var(--ars-space-1); flex: none; }
.ars-brain__chip {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 1px var(--ars-space-2) 1px var(--ars-space-1);
  background: var(--ars-surface-raised);
  border: var(--ars-hairline) solid var(--ars-border);
  border-radius: var(--ars-radius-xs);
  color: var(--ars-text-secondary);
  font-size: var(--ars-text-2xs);
  font-weight: var(--ars-weight-semi);
  letter-spacing: var(--ars-track-label);
  text-transform: uppercase;
  cursor: pointer;
  transition: border-color var(--ars-dur-fast) var(--ars-ease-mech),
              background var(--ars-dur-fast) var(--ars-ease-mech);
}
.ars-brain__chip:hover { border-color: var(--ars-border-strong); background: var(--ars-surface-hover); }
.ars-brain__chip[aria-pressed="true"] {
  border-color: var(--ars-border-accent);
  background: var(--ars-surface-active);
  color: var(--ars-text);
}
.ars-brain__chip-mark { width: 8px; height: 8px; flex: none; background: currentColor; }
.ars-brain__chip--document .ars-brain__chip-mark { color: var(--ars-cyan-400); clip-path: polygon(25% 0, 75% 0, 100% 50%, 75% 100%, 25% 100%, 0 50%); }
.ars-brain__chip--passage .ars-brain__chip-mark { color: var(--ars-teal); border-radius: var(--ars-radius-pill); }
.ars-brain__chip--fact .ars-brain__chip-mark { color: var(--ars-violet); clip-path: polygon(50% 0, 100% 50%, 50% 100%, 0 50%); }
.ars-brain__chip--lang .ars-brain__chip-mark { border-radius: var(--ars-radius-pill); background: none; box-shadow: inset 0 0 0 2px currentColor; }
.ars-brain__chip--en .ars-brain__chip-mark { color: var(--ars-ice); }
.ars-brain__chip--ro .ars-brain__chip-mark { color: var(--ars-amber); }
.ars-brain__chip--de .ars-brain__chip-mark { color: var(--ars-ink); }
.ars-brain__chip-n {
  font-family: var(--ars-font-mono); font-variant-numeric: tabular-nums;
  letter-spacing: 0; color: var(--ars-text-accent);
}

.ars-brain__detail {
  flex: none;
  display: flex; flex-direction: column; gap: 3px;
  padding: var(--ars-space-2);
  min-height: 62px;
  background: var(--ars-surface-raised);
  border-left: 2px solid var(--ars-border-strong);
  border-radius: 0 var(--ars-radius-xs) var(--ars-radius-xs) 0;
}
.ars-brain__detail[data-kind="document"] { border-left-color: var(--ars-cyan-400); }
.ars-brain__detail[data-kind="passage"]  { border-left-color: var(--ars-teal); }
.ars-brain__detail[data-kind="fact"]     { border-left-color: var(--ars-violet); }
.ars-brain__detail-head { display: flex; align-items: center; gap: var(--ars-space-2); flex-wrap: wrap; }
.ars-brain__detail-kind {
  font-size: var(--ars-text-2xs); font-weight: var(--ars-weight-semi);
  letter-spacing: var(--ars-track-label); text-transform: uppercase;
  color: var(--ars-text-accent);
}
.ars-brain__detail-lang {
  font-family: var(--ars-font-mono); font-size: 9px; letter-spacing: 0.08em;
  padding: 0 4px; border-radius: var(--ars-radius-xs);
  color: var(--ars-text-on-accent); background: var(--ars-text-muted);
}
.ars-brain__detail-lang[data-lang="en"] { background: var(--ars-ice); }
.ars-brain__detail-lang[data-lang="ro"] { background: var(--ars-amber); }
.ars-brain__detail-lang[data-lang="de"] { background: var(--ars-ink); }
.ars-brain__detail-text {
  margin: 0; font-size: var(--ars-text-sm); line-height: var(--ars-leading-snug);
  color: var(--ars-text); word-break: break-word;
}
.ars-brain__detail-meta { margin: 0; font-size: var(--ars-text-2xs); color: var(--ars-text-muted); }
.ars-brain__detail-links { display: flex; flex-wrap: wrap; gap: var(--ars-space-1); }
.ars-brain__link {
  padding: 0 var(--ars-space-1);
  background: transparent;
  border: var(--ars-hairline) solid var(--ars-border);
  border-radius: var(--ars-radius-xs);
  color: var(--ars-text-secondary);
  font-size: var(--ars-text-2xs);
  max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  cursor: pointer;
}
.ars-brain__link:hover { color: var(--ars-text-accent); border-color: var(--ars-border-strong); }

.ars-brain__hint { margin: 0; font-size: var(--ars-text-2xs); color: var(--ars-text-muted); flex: none; }
.ars-brain__list { margin: 0; padding-left: var(--ars-space-4); font-size: var(--ars-text-xs); color: var(--ars-text-secondary); }

/* expanded: the network takes the whole window, because 340px is not enough
   room to read what a machine knows about you */
.ars-brain-overlay {
  position: fixed; inset: 0; z-index: var(--ars-z-overlay);
  display: flex; padding: var(--ars-space-5);
  background: color-mix(in srgb, var(--ars-bg) 88%, transparent);
  backdrop-filter: blur(2px);
}
.ars-brain-overlay > .hud-panel {
  flex: 1 1 auto; display: flex; flex-direction: column; min-height: 0;
  box-shadow: var(--ars-shadow-4);
}
.ars-brain-overlay > .hud-panel .hud-panel__body { flex: 1 1 auto; min-height: 0; }
.ars-brain-overlay .ars-brain__detail { min-height: 0; }
`;

let injected = false;
function ensureStyles() {
  if (injected || typeof document === 'undefined') return;
  injected = true;
  const style = document.createElement('style');
  style.id = 'ars-brain-style';
  style.textContent = BRAIN_CSS;
  document.head.appendChild(style);
}

/* ==========================================================================
   createBrainPanel({ root, hud, getLang, baseUrl })
   ========================================================================== */

export function createBrainPanel({ root, hud, getLang, baseUrl = '', pollMs = POLL_MS } = {}) {
  /* ---------------------------------------------------------------- chrome */
  let panelHandle;
  if (hud && typeof hud.panel === 'function') {
    try {
      panelHandle = hud.panel({ id: 'panel-brain', title: t('brain.title', getLang()) });
    } catch (err) {
      console.warn('[brain] hud.panel failed, using plain container', err);
    }
  }
  if (!panelHandle) {
    const container = el('section', 'hud-panel', { id: 'panel-brain', 'aria-label': t('brain.title', getLang()) });
    const header = el('div', 'hud-panel__header');
    header.append(el('h2', 'hud-panel__title', { text: t('brain.title', getLang()) }));
    const body = el('div', 'hud-panel__body');
    container.append(header, body);
    panelHandle = { root: container, body, header, setTitle(text) { header.firstChild.textContent = text; } };
  }
  panelHandle.root.classList.add('ars-brain');
  // After hud.panel(), deliberately: entity/hud.js injects its stylesheet on
  // first use, and `.hud-panel .hud-panel__body { max-height: 42vh }` ties with
  // this file's rule on specificity. Later wins, and the network needs the
  // height, so this file has to land second.
  ensureStyles();
  root.append(panelHandle.root);

  const stage = el('div', 'ars-brain__stage');
  const canvas = el('canvas', 'ars-brain__canvas', { tabindex: '0', role: 'img' });
  const emptyNote = el('p', 'ars-brain__empty', { text: t('brain.empty', getLang()) });
  const tools = el('div', 'ars-brain__tools');
  const fitBtn = el('button', 'ars-brain__btn', { type: 'button', text: t('brain.fit', getLang()) });
  const expandBtn = el('button', 'ars-brain__btn', { type: 'button', text: t('brain.expand', getLang()) });
  tools.append(fitBtn, expandBtn);
  stage.append(canvas, emptyNote, tools);

  const legend = el('div', 'ars-brain__legend');
  const detail = el('div', 'ars-brain__detail', { role: 'status', 'aria-live': 'polite' });
  const hint = el('p', 'ars-brain__hint', { text: t('brain.hint', getLang()) });

  let tickerHandle = null;
  if (hud && typeof hud.ticker === 'function') {
    try { tickerHandle = hud.ticker({ id: 'brain-ticker', text: '' }); } catch { /* fallback below */ }
  }

  panelHandle.body.append(stage, legend, detail);
  if (tickerHandle) panelHandle.body.append(tickerHandle.root);
  panelHandle.body.append(hint);

  const ctx = canvas.getContext ? canvas.getContext('2d', { alpha: false }) : null;

  /* ------------------------------------------------------------ graph state */
  const nodes = new Map();   // id -> node
  const edges = [];          // { from, to, kind }
  const adjacency = new Map();
  let summary = { documents: 0, passages: 0, facts: 0, translations: 0, by_language: {} };
  let selectedId = null;
  let hoverId = null;
  let filter = null;         // { type: 'kind'|'lang', value }
  let alpha = 1;
  let pulses = [];
  const cam = { x: 0, y: 0, scale: 1, tx: 0, ty: 0, tscale: 1 };
  let autoFit = true;
  let offline = false;
  let lastError = false;
  let onCounts = null;

  /* ------------------------------------------------------------- theme read */
  let C = {};
  let FONT_UI = 'system-ui, sans-serif';
  let FONT_MONO = 'ui-monospace, monospace';
  function readTheme() {
    const cs = getComputedStyle(panelHandle.root);
    const tok = (name, fb) => parseColor((cs.getPropertyValue(name) || '').trim(), fb);
    C = {
      document: tok('--ars-cyan-400', [94, 230, 255]),
      passage: tok('--ars-teal', [63, 220, 160]),
      fact: tok('--ars-violet', [176, 140, 255]),
      en: tok('--ars-ice', [159, 238, 255]),
      ro: tok('--ars-amber', [255, 178, 63]),
      de: tok('--ars-ink', [221, 235, 244]),
      line: tok('--ars-line', [36, 56, 74]),
      lineHi: tok('--ars-line-hi', [74, 117, 146]),
      ink: tok('--ars-ink', [221, 235, 244]),
      inkMute: tok('--ars-ink-mute', [122, 148, 166]),
      bg: tok('--ars-surface-sunken', [6, 10, 16]),
      accent: tok('--ars-cyan-500', [34, 211, 240]),
    };
    const ui = (cs.getPropertyValue('--ars-font-ui') || '').trim();
    const mono = (cs.getPropertyValue('--ars-font-mono') || '').trim();
    if (ui) FONT_UI = ui;
    if (mono) FONT_MONO = mono;
    haloCache.clear();
  }

  const haloCache = new Map();
  function halo(colour) {
    const key = colour.join(',');
    let sprite = haloCache.get(key);
    if (sprite) return sprite;
    const size = 64;
    const c = document.createElement('canvas');
    c.width = size; c.height = size;
    const g = c.getContext('2d');
    const grad = g.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
    grad.addColorStop(0, rgba(colour, 0.5));
    grad.addColorStop(0.35, rgba(colour, 0.16));
    grad.addColorStop(1, rgba(colour, 0));
    g.fillStyle = grad;
    g.fillRect(0, 0, size, size);
    haloCache.set(key, c);
    return c;
  }

  const reduceMotion = window.matchMedia
    ? window.matchMedia('(prefers-reduced-motion: reduce)')
    : { matches: false, addEventListener() {} };

  /* -------------------------------------------------------------- ingestion */

  function radiusFor(n) {
    if (n.kind === 'document') return 13 + Math.min(10, Math.log2((n.weight || 1) + 1) * 3.5);
    if (n.kind === 'fact') return 9;
    return 7;
  }

  /** Merge a /api/knowledge payload. Only the difference is applied — existing
   *  nodes keep their position and momentum, which is what makes the network
   *  grow instead of jumping. */
  function ingest(data) {
    const seen = new Set();
    const fresh = [];
    for (const raw of data.nodes || []) {
      seen.add(raw.id);
      const existing = nodes.get(raw.id);
      if (existing) {
        existing.label = raw.label || existing.label;
        existing.weight = raw.weight || existing.weight;
        existing.language = raw.language || existing.language;
        existing.derived = !!raw.derived;
        existing.r = radiusFor(existing);
        continue;
      }
      const node = {
        id: raw.id, kind: raw.kind || 'passage', label: raw.label || raw.id,
        language: raw.language || null, weight: raw.weight || 1, derived: !!raw.derived,
        x: 0, y: 0, vx: 0, vy: 0, r: 8, born: performance.now(), appear: 0, fixed: false,
      };
      node.r = radiusFor(node);
      nodes.set(node.id, node);
      fresh.push(node);
    }

    // Rebuild the edge list (it is small and the server is the source of truth),
    // then place newborn nodes on top of whichever node they hang from.
    edges.length = 0;
    adjacency.clear();
    for (const e of data.edges || []) {
      if (!nodes.has(e.from) || !nodes.has(e.to)) continue;
      edges.push({ from: e.from, to: e.to, kind: e.kind || 'contains' });
      if (!adjacency.has(e.from)) adjacency.set(e.from, []);
      if (!adjacency.has(e.to)) adjacency.set(e.to, []);
      adjacency.get(e.from).push({ id: e.to, kind: e.kind, dir: 'out' });
      adjacency.get(e.to).push({ id: e.from, kind: e.kind, dir: 'in' });
    }

    for (const node of fresh) {
      const parentId = (adjacency.get(node.id) || []).find((l) => l.dir === 'in');
      const parent = parentId ? nodes.get(parentId.id) : null;
      const angle = Math.random() * Math.PI * 2;
      const spread = parent ? 14 : 200 + Math.random() * 90;
      const ox = parent ? parent.x : 0;
      const oy = parent ? parent.y : 0;
      node.x = ox + Math.cos(angle) * spread;
      node.y = oy + Math.sin(angle) * spread;
      if (nodes.size > 1) {
        pulses.push({ x: ox, y: oy, t0: performance.now(), colour: C[node.kind] || C.passage, r0: parent ? parent.r : 6 });
      }
    }

    let removed = 0;
    for (const id of [...nodes.keys()]) {
      if (!seen.has(id)) { nodes.delete(id); removed += 1; if (selectedId === id) selectedId = null; }
    }

    summary = data.summary || summary;

    if (fresh.length || removed) {
      alpha = Math.max(alpha, fresh.length ? 0.75 : 0.4);
      autoFit = autoFit || nodes.size <= 2;
      renderLegend();
      if (fresh.length) announceGrowth(fresh);
      if (removed) say(t('brain.forgot', getLang(), { n: removed }));
      if (onCounts) onCounts(nodes.size, summary);
      wake();
    }
    emptyNote.hidden = nodes.size > 0;
    canvas.setAttribute('aria-label', t('brain.a11y', getLang(), {
      total: nodes.size,
      documents: summary.documents || 0,
      passages: summary.passages || 0,
      facts: summary.facts || 0,
    }));
    if (reduceMotion.matches) settleNow();
  }

  function announceGrowth(fresh) {
    if (fresh.length === 1) {
      say(t('brain.learned_one', getLang(), { label: fresh[0].label.slice(0, 40) }));
    } else {
      say(t('brain.new', getLang(), { n: fresh.length }));
    }
    for (const n of fresh) n.flash = performance.now();
  }

  function say(text) {
    if (tickerHandle) tickerHandle.push(text);
    else hint.textContent = text;
  }

  /* ----------------------------------------------------------------- forces */

  function step(dt) {
    if (nodes.size === 0) return;
    alpha += (0 - alpha) * ALPHA_DECAY * (dt * 60);
    if (alpha < ALPHA_MIN) alpha = 0;
    if (alpha === 0) return;

    const list = [...nodes.values()];

    // Repulsion + hard collision, against a uniform grid: each node only ever
    // looks at the nine cells around it, so this stays linear as Alex's library
    // grows. A full n^2 pass at 400 nodes is 160k distance tests a frame; this
    // is about 4k.
    const grid = new Map();
    for (const n of list) {
      const key = `${Math.floor(n.x / GRID_CELL)},${Math.floor(n.y / GRID_CELL)}`;
      let cell = grid.get(key);
      if (!cell) { cell = []; grid.set(key, cell); }
      cell.push(n);
    }
    for (const n of list) {
      const cx = Math.floor(n.x / GRID_CELL);
      const cy = Math.floor(n.y / GRID_CELL);
      for (let gx = cx - 1; gx <= cx + 1; gx++) {
        for (let gy = cy - 1; gy <= cy + 1; gy++) {
          const cell = grid.get(`${gx},${gy}`);
          if (!cell) continue;
          for (const m of cell) {
            if (m === n) continue;
            let dx = n.x - m.x;
            let dy = n.y - m.y;
            let d2 = dx * dx + dy * dy;
            if (d2 > GRID_CELL * GRID_CELL * 2.25) continue;
            if (d2 < 1e-4) { dx = (Math.random() - 0.5) * 0.6; dy = (Math.random() - 0.5) * 0.6; d2 = dx * dx + dy * dy + 1e-4; }
            const d = Math.sqrt(d2);
            const f = (CHARGE * alpha) / d2;
            n.vx += (dx / d) * f * dt;
            n.vy += (dy / d) * f * dt;
            const minD = n.r + m.r + 8;
            if (d < minD) {
              const push = ((minD - d) / minD) * 40 * dt;
              n.vx += (dx / d) * push;
              n.vy += (dy / d) * push;
            }
          }
        }
      }
    }

    // Links.
    for (const e of edges) {
      const a = nodes.get(e.from);
      const b = nodes.get(e.to);
      if (!a || !b) continue;
      const rest = e.kind === 'translation' ? LINK_TRANSLATION : LINK_CONTAINS;
      const k = e.kind === 'translation' ? 3.4 : 2.2;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const d = Math.hypot(dx, dy) || 1e-4;
      const f = ((d - rest) / d) * k * alpha * dt;
      a.vx += dx * f; a.vy += dy * f;
      b.vx -= dx * f; b.vy -= dy * f;
    }

    // Gravity. Documents are the spine and are held near the middle; facts are
    // answers A.R.S worked out on its own, so they are allowed to float.
    for (const n of list) {
      const g = n.kind === 'document' ? 1.5 : n.kind === 'fact' ? 0.28 : 0.75;
      n.vx -= n.x * g * alpha * dt;
      n.vy -= n.y * g * alpha * dt;
    }

    const decay = Math.pow(VELOCITY_DECAY, dt * 60);
    for (const n of list) {
      if (n.fixed) { n.vx = 0; n.vy = 0; continue; }
      n.vx *= decay;
      n.vy *= decay;
      const speed = Math.hypot(n.vx, n.vy);
      if (speed > 900) { n.vx = (n.vx / speed) * 900; n.vy = (n.vy / speed) * 900; }
      n.x += n.vx * dt;
      n.y += n.vy * dt;
    }
  }

  /** Reduced motion, or a fresh load: run the layout to rest in one burst and
   *  draw the result, instead of animating it. */
  function settleNow(steps = 220) {
    alpha = 1;
    for (let i = 0; i < steps; i++) step(1 / 60);
    alpha = 0;
    fitCamera(true);
  }

  /* ----------------------------------------------------------------- camera */

  function bounds() {
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const n of nodes.values()) {
      minX = Math.min(minX, n.x - n.r); maxX = Math.max(maxX, n.x + n.r);
      minY = Math.min(minY, n.y - n.r); maxY = Math.max(maxY, n.y + n.r);
    }
    if (!Number.isFinite(minX)) return { minX: -100, minY: -100, maxX: 100, maxY: 100 };
    return { minX, minY, maxX, maxY };
  }

  function fitCamera(immediate = false) {
    const b = bounds();
    const w = canvas.clientWidth || 320;
    const h = canvas.clientHeight || 260;
    const pad = 58;
    const s = clamp(Math.min((w - pad) / Math.max(b.maxX - b.minX, 1), (h - pad) / Math.max(b.maxY - b.minY, 1)), 0.22, 1.9);
    cam.tx = (b.minX + b.maxX) / 2;
    cam.ty = (b.minY + b.maxY) / 2;
    cam.tscale = s;
    if (immediate) { cam.x = cam.tx; cam.y = cam.ty; cam.scale = cam.tscale; }
  }

  const sx = (n) => (n.x - cam.x) * cam.scale + canvas.clientWidth / 2;
  const sy = (n) => (n.y - cam.y) * cam.scale + canvas.clientHeight / 2;
  const worldX = (px) => (px - canvas.clientWidth / 2) / cam.scale + cam.x;
  const worldY = (py) => (py - canvas.clientHeight / 2) / cam.scale + cam.y;

  /* ------------------------------------------------------------------ paint */

  let dpr = 1;
  let quality = 2;           // 2 = halos + grid, 1 = no halos, 0 = flat
  let frameEma = 0;
  let slowFrames = 0;
  let lastFps = 0;

  function resize() {
    const w = Math.max(1, Math.round(canvas.clientWidth));
    const h = Math.max(1, Math.round(canvas.clientHeight));
    dpr = clamp(window.devicePixelRatio || 1, 1, 2);
    const bw = Math.round(w * dpr);
    const bh = Math.round(h * dpr);
    if (canvas.width !== bw || canvas.height !== bh) {
      canvas.width = bw;
      canvas.height = bh;
      needsDraw = true;
    }
  }

  function dimOf(n) {
    if (!filter) return 1;
    const match = filter.type === 'kind' ? n.kind === filter.value : n.language === filter.value;
    return match ? 1 : 0.14;
  }

  function nodeColour(n) { return C[n.kind] || C.passage; }
  function langColour(n) { return (n.language && C[n.language]) || C.inkMute; }

  function shapePath(g, n, px, py, r) {
    g.beginPath();
    if (n.kind === 'document') {
      for (let i = 0; i < 6; i++) {
        const a = (Math.PI / 3) * i - Math.PI / 6;
        const x = px + Math.cos(a) * r;
        const y = py + Math.sin(a) * r;
        if (i === 0) g.moveTo(x, y); else g.lineTo(x, y);
      }
      g.closePath();
    } else if (n.kind === 'fact') {
      g.moveTo(px, py - r); g.lineTo(px + r, py); g.lineTo(px, py + r); g.lineTo(px - r, py);
      g.closePath();
    } else {
      g.arc(px, py, r, 0, Math.PI * 2);
    }
  }

  function truncate(s, max) {
    const str = String(s || '');
    return str.length > max ? `${str.slice(0, max - 1)}…` : str;
  }

  function draw(now) {
    if (!ctx) return;
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = rgba(C.bg, 1);
    ctx.fillRect(0, 0, w, h);

    // faint instrument grid, anchored to the world so panning reads as motion
    if (quality >= 2) {
      const gstep = 64 * cam.scale;
      if (gstep > 14) {
        ctx.strokeStyle = rgba(C.line, 0.32);
        ctx.lineWidth = 1;
        ctx.beginPath();
        const ox = ((w / 2 - cam.x * cam.scale) % gstep + gstep) % gstep;
        const oy = ((h / 2 - cam.y * cam.scale) % gstep + gstep) % gstep;
        for (let x = ox; x < w; x += gstep) { ctx.moveTo(Math.round(x) + 0.5, 0); ctx.lineTo(Math.round(x) + 0.5, h); }
        for (let y = oy; y < h; y += gstep) { ctx.moveTo(0, Math.round(y) + 0.5); ctx.lineTo(w, Math.round(y) + 0.5); }
        ctx.stroke();
      }
    }

    const selected = selectedId ? nodes.get(selectedId) : null;
    const hovered = hoverId ? nodes.get(hoverId) : null;
    const focusId = (hovered && hovered.id) || (selected && selected.id) || null;
    const near = new Set();
    if (focusId) {
      near.add(focusId);
      for (const l of adjacency.get(focusId) || []) near.add(l.id);
    }

    /* ---- edges ---- */
    ctx.lineCap = 'round';
    for (const e of edges) {
      const a = nodes.get(e.from);
      const b = nodes.get(e.to);
      if (!a || !b) continue;
      const dim = Math.min(dimOf(a), dimOf(b));
      const lit = focusId && (near.has(a.id) && near.has(b.id));
      const translation = e.kind === 'translation';
      const colour = translation ? langColour(b) : nodeColour(a);
      const alphaLine = (translation ? 0.4 : 0.34) * dim * (lit ? 2.1 : 1) * Math.min(a.appear, b.appear);
      ctx.strokeStyle = rgba(colour, clamp(alphaLine, 0, 0.85));
      ctx.lineWidth = lit ? 1.6 : 1;
      if (translation) {
        ctx.setLineDash([3, 5]);
        ctx.lineDashOffset = reduceMotion.matches ? 0 : -((now / 44) % 8);
      } else {
        ctx.setLineDash([]);
      }
      ctx.beginPath();
      ctx.moveTo(sx(a), sy(a));
      ctx.lineTo(sx(b), sy(b));
      ctx.stroke();
    }
    ctx.setLineDash([]);

    /* ---- halos ---- */
    if (quality >= 2) {
      ctx.globalCompositeOperation = 'lighter';
      for (const n of nodes.values()) {
        const r = clamp(n.r * cam.scale, 3.5, 46) * n.appear;
        const sprite = halo(nodeColour(n));
        const size = r * 7;
        const flash = n.flash ? clamp(1 - (now - n.flash) / 1400, 0, 1) : 0;
        ctx.globalAlpha = clamp((0.5 + flash * 0.9) * dimOf(n), 0, 1);
        ctx.drawImage(sprite, sx(n) - size / 2, sy(n) - size / 2, size, size);
      }
      ctx.globalAlpha = 1;
      ctx.globalCompositeOperation = 'source-over';
    }

    /* ---- spawn pulses ---- */
    if (!reduceMotion.matches) {
      pulses = pulses.filter((p) => now - p.t0 < 1100);
      for (const p of pulses) {
        const k = (now - p.t0) / 1100;
        const r = (p.r0 + 6 + k * 70) * cam.scale;
        ctx.strokeStyle = rgba(p.colour, (1 - k) * 0.5);
        ctx.lineWidth = 1.4 * (1 - k) + 0.4;
        ctx.beginPath();
        ctx.arc((p.x - cam.x) * cam.scale + w / 2, (p.y - cam.y) * cam.scale + h / 2, r, 0, Math.PI * 2);
        ctx.stroke();
      }
    }

    /* ---- nodes ---- */
    for (const n of nodes.values()) {
      const px = sx(n);
      const py = sy(n);
      const r = clamp(n.r * cam.scale, 3.5, 46) * n.appear;
      if (px < -80 || py < -80 || px > w + 80 || py > h + 80) continue;
      const dim = dimOf(n);
      const colour = nodeColour(n);
      const isFocus = n.id === focusId;
      const isSelected = n.id === selectedId;

      shapePath(ctx, n, px, py, r);
      ctx.fillStyle = rgba(C.bg, 0.92 * dim);
      ctx.fill();
      ctx.fillStyle = rgba(colour, (n.derived ? 0.16 : 0.3) * dim);
      ctx.fill();
      ctx.strokeStyle = rgba(colour, (isFocus ? 1 : 0.85) * dim);
      ctx.lineWidth = isFocus ? 2 : 1.25;
      if (n.derived) ctx.setLineDash([3, 3]);
      ctx.stroke();
      ctx.setLineDash([]);

      // language collar: colour AND position, so it survives a colour-blind eye
      if (n.language && r > 4.5) {
        const a0 = LANG_ANGLE[n.language] !== undefined ? LANG_ANGLE[n.language] : 0;
        ctx.strokeStyle = rgba(langColour(n), 0.95 * dim);
        ctx.lineWidth = 2.2;
        ctx.beginPath();
        ctx.arc(px, py, r + 3.4, a0 - 0.62, a0 + 0.62);
        ctx.stroke();
      }

      if (isSelected) {
        ctx.strokeStyle = rgba(C.accent, 0.9);
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.arc(px, py, r + 8, 0, Math.PI * 2);
        ctx.stroke();
      }
    }

    /* ---- labels ---- */
    const passageCount = summary.passages || 0;
    ctx.textBaseline = 'middle';
    for (const n of nodes.values()) {
      const r = clamp(n.r * cam.scale, 3.5, 46) * n.appear;
      const always = n.kind === 'document' || n.kind === 'fact'
        || (passageCount <= 14 && cam.scale > 0.55);
      const show = always || n.id === focusId || (focusId && near.has(n.id)) || n.id === selectedId;
      if (!show || n.appear < 0.5) continue;
      const px = sx(n);
      const py = sy(n) + r + 11;
      if (px < -140 || py < -20 || px > w + 140 || py > h + 20) continue;
      const dim = dimOf(n);
      const size = n.kind === 'document' ? 11.5 : 10.5;
      ctx.font = `${n.kind === 'document' ? '600 ' : ''}${size}px ${FONT_UI}`;
      const text = truncate(n.label, n.id === focusId ? 46 : 26);
      const tw = ctx.measureText(text).width;
      ctx.fillStyle = rgba(C.bg, 0.78 * dim);
      ctx.fillRect(px - tw / 2 - 3, py - 8, tw + 6, 16);
      ctx.fillStyle = rgba(n.kind === 'document' ? C.ink : C.inkMute, (n.id === focusId ? 1 : 0.92) * dim);
      ctx.textAlign = 'center';
      ctx.fillText(text, px, py);
      if (n.language && (n.id === focusId || n.kind === 'fact')) {
        ctx.font = `9px ${FONT_MONO}`;
        ctx.fillStyle = rgba(langColour(n), 0.95 * dim);
        ctx.fillText(n.language.toUpperCase(), px, py + 12);
      }
    }
    ctx.textAlign = 'left';
  }

  /* ------------------------------------------------------------------- loop */

  let raf = 0;
  let last = 0;
  let running = false;
  let needsDraw = true;
  let onScreen = true;
  let fpsAcc = 0;
  let fpsFrames = 0;

  function frame(now) {
    raf = 0;
    if (!running) return;
    const dt = last ? clamp((now - last) / 1000, 0, 0.05) : 1 / 60;
    last = now;
    const t0 = performance.now();

    resize();

    // appearance easing — a node arrives, it does not blink into being
    let animating = false;
    for (const n of nodes.values()) {
      if (n.appear < 1) {
        n.appear = reduceMotion.matches ? 1 : clamp(easeOut((now - n.born) / 620), 0, 1);
        animating = true;
      }
    }

    if (!reduceMotion.matches) {
      step(dt);
      if (autoFit) fitCamera();
      cam.x = approach(cam.x, cam.tx, 0.22, dt);
      cam.y = approach(cam.y, cam.ty, 0.22, dt);
      cam.scale = approach(cam.scale, cam.tscale, 0.22, dt);
    }

    draw(now);

    const cost = performance.now() - t0;
    frameEma = frameEma ? frameEma * 0.9 + cost * 0.1 : cost;
    fpsAcc += dt; fpsFrames += 1;
    if (fpsAcc > 0.5) { lastFps = fpsFrames / fpsAcc; fpsAcc = 0; fpsFrames = 0; }
    if (frameEma > 9) { slowFrames += 1; } else if (slowFrames > 0) { slowFrames -= 1; }
    if (slowFrames > 45 && quality > 0) { quality -= 1; slowFrames = 0; }

    const idle = alpha === 0 && !animating && pulses.length === 0
      && Math.abs(cam.scale - cam.tscale) < 0.001 && !dragging;
    if (reduceMotion.matches) {
      if (needsDraw) { needsDraw = false; }
      if (!needsDraw && idle) { running = false; return; }
    }
    raf = requestAnimationFrame(frame);
  }

  function wake() {
    needsDraw = true;
    if (running) return;
    if (!onScreen || document.hidden || !ctx) return;
    running = true;
    last = 0;
    raf = requestAnimationFrame(frame);
  }

  function sleep() {
    running = false;
    if (raf) cancelAnimationFrame(raf);
    raf = 0;
  }

  /* -------------------------------------------------------------- interaction */

  let dragging = null;         // { node } | { pan: true }
  let pointerStart = null;
  let moved = false;

  function nodeAt(px, py) {
    let best = null;
    let bestD = Infinity;
    for (const n of nodes.values()) {
      const r = clamp(n.r * cam.scale, 3.5, 46) + 5;
      const d = Math.hypot(px - sx(n), py - sy(n));
      if (d < r && d < bestD) { best = n; bestD = d; }
    }
    return best;
  }

  function localPoint(ev) {
    const rect = canvas.getBoundingClientRect();
    return [ev.clientX - rect.left, ev.clientY - rect.top];
  }

  canvas.addEventListener('pointerdown', (ev) => {
    canvas.setPointerCapture(ev.pointerId);
    const [px, py] = localPoint(ev);
    const hit = nodeAt(px, py);
    moved = false;
    pointerStart = { px, py, camX: cam.x, camY: cam.y };
    if (hit) {
      hit.fixed = true;
      dragging = { node: hit };
    } else {
      dragging = { pan: true };
      canvas.dataset.grabbing = '1';
    }
    wake();
  });

  canvas.addEventListener('pointermove', (ev) => {
    const [px, py] = localPoint(ev);
    if (dragging && dragging.node) {
      moved = true;
      dragging.node.x = worldX(px);
      dragging.node.y = worldY(py);
      dragging.node.vx = 0; dragging.node.vy = 0;
      alpha = Math.max(alpha, 0.28);
      autoFit = false;
      wake();
      return;
    }
    if (dragging && dragging.pan) {
      moved = true;
      autoFit = false;
      cam.x = pointerStart.camX - (px - pointerStart.px) / cam.scale;
      cam.y = pointerStart.camY - (py - pointerStart.py) / cam.scale;
      cam.tx = cam.x; cam.ty = cam.y;
      wake();
      return;
    }
    const hit = nodeAt(px, py);
    const id = hit ? hit.id : null;
    if (id !== hoverId) {
      hoverId = id;
      canvas.dataset.over = hit ? '1' : '0';
      canvas.title = hit ? hit.label : '';
      wake();
    }
  });

  function endDrag(ev) {
    if (!dragging) return;
    const wasNode = dragging.node;
    if (wasNode) {
      wasNode.fixed = false;
      if (!moved) select(wasNode.id);
      alpha = Math.max(alpha, 0.2);
    } else if (!moved) {
      select(null);
    }
    dragging = null;
    canvas.dataset.grabbing = '0';
    try { canvas.releasePointerCapture(ev.pointerId); } catch { /* already gone */ }
    wake();
  }
  canvas.addEventListener('pointerup', endDrag);
  canvas.addEventListener('pointercancel', endDrag);
  canvas.addEventListener('pointerleave', () => {
    if (hoverId) { hoverId = null; canvas.dataset.over = '0'; wake(); }
  });

  canvas.addEventListener('wheel', (ev) => {
    ev.preventDefault();
    const [px, py] = localPoint(ev);
    const before = [worldX(px), worldY(py)];
    const factor = Math.exp(-ev.deltaY * 0.0016);
    cam.scale = clamp(cam.scale * factor, 0.18, 3.2);
    cam.tscale = cam.scale;
    const after = [worldX(px), worldY(py)];
    cam.x += before[0] - after[0];
    cam.y += before[1] - after[1];
    cam.tx = cam.x; cam.ty = cam.y;
    autoFit = false;
    wake();
  }, { passive: false });

  canvas.addEventListener('keydown', (ev) => {
    const list = [...nodes.values()].sort((a, b) => (KIND_ORDER[a.kind] - KIND_ORDER[b.kind]) || a.label.localeCompare(b.label));
    if (!list.length) return;
    const i = list.findIndex((n) => n.id === selectedId);
    if (ev.key === 'ArrowRight' || ev.key === 'ArrowDown') {
      ev.preventDefault(); select(list[(i + 1 + list.length) % list.length].id, true);
    } else if (ev.key === 'ArrowLeft' || ev.key === 'ArrowUp') {
      ev.preventDefault(); select(list[(i - 1 + list.length) % list.length].id, true);
    } else if (ev.key === 'Escape') {
      select(null);
    } else if (ev.key === 'f' || ev.key === 'F') {
      autoFit = true; fitCamera(); wake();
    }
  });

  fitBtn.addEventListener('click', () => { autoFit = true; fitCamera(); wake(); });

  /* ------------------------------------------------------------- expansion */

  let overlay = null;
  function setExpanded(on) {
    if (on && !overlay) {
      overlay = el('div', 'ars-brain-overlay');
      overlay.addEventListener('pointerdown', (ev) => { if (ev.target === overlay) setExpanded(false); });
      document.body.append(overlay);
      overlay.append(panelHandle.root);
      expandBtn.textContent = t('brain.close', getLang());
      document.addEventListener('keydown', onOverlayKey);
      requestAnimationFrame(() => { resize(); autoFit = true; fitCamera(); wake(); });
    } else if (!on && overlay) {
      root.append(panelHandle.root);
      overlay.remove();
      overlay = null;
      expandBtn.textContent = t('brain.expand', getLang());
      document.removeEventListener('keydown', onOverlayKey);
      requestAnimationFrame(() => { resize(); autoFit = true; fitCamera(); wake(); });
    }
  }
  function onOverlayKey(ev) { if (ev.key === 'Escape') setExpanded(false); }
  expandBtn.addEventListener('click', () => setExpanded(!overlay));

  /* ------------------------------------------------------------ selection UI */

  function select(id, centre = false) {
    selectedId = id;
    renderDetail();
    if (centre && id && nodes.get(id)) {
      const n = nodes.get(id);
      autoFit = false;
      cam.tx = n.x; cam.ty = n.y;
    }
    wake();
  }

  function renderDetail() {
    detail.textContent = '';
    const n = selectedId ? nodes.get(selectedId) : null;
    if (!n) {
      detail.removeAttribute('data-kind');
      detail.append(el('p', 'ars-brain__detail-meta', { text: t('brain.nothing_selected', getLang()) }));
      return;
    }
    detail.dataset.kind = n.kind;
    const head = el('div', 'ars-brain__detail-head');
    head.append(el('span', 'ars-brain__detail-kind', { text: t(`brain.node.${n.kind}`, getLang()) }));
    if (n.language) {
      head.append(el('span', 'ars-brain__detail-lang', {
        text: n.language.toUpperCase(),
        'data-lang': n.language,
        title: LANG_LABEL[n.language] || n.language,
      }));
    }
    detail.append(head);
    detail.append(el('p', 'ars-brain__detail-text', { text: n.label }));

    const links = adjacency.get(n.id) || [];
    const parts = [];
    const parent = links.find((l) => l.dir === 'in' && l.kind === 'contains');
    if (parent && nodes.get(parent.id)) {
      parts.push(t('brain.in_document', getLang(), { name: nodes.get(parent.id).label }));
    }
    const origin = links.find((l) => l.dir === 'in' && l.kind === 'translation');
    if (origin && nodes.get(origin.id)) {
      const from = nodes.get(origin.id).language;
      parts.push(t('brain.translated_from', getLang(), { lang: LANG_LABEL[from] || from || '?' }));
    }
    parts.push(n.derived ? t('brain.derived', getLang()) : t('brain.original', getLang()));
    detail.append(el('p', 'ars-brain__detail-meta', { text: parts.join(' · ') }));

    if (links.length) {
      const row = el('div', 'ars-brain__detail-links');
      row.append(el('span', 'ars-brain__detail-meta', { text: `${t('brain.links', getLang())}:` }));
      for (const l of links.slice(0, 8)) {
        const target = nodes.get(l.id);
        if (!target) continue;
        const btn = el('button', 'ars-brain__link', {
          type: 'button',
          text: truncate(target.label, 22) + (target.language ? ` · ${target.language.toUpperCase()}` : ''),
        });
        btn.addEventListener('click', () => select(target.id, true));
        row.append(btn);
      }
      detail.append(row);
    }
  }

  /* ----------------------------------------------------------------- legend */

  function chip(className, label, count, onToggle, pressed) {
    const b = el('button', `ars-brain__chip ${className}`, {
      type: 'button', 'aria-pressed': String(!!pressed), title: label,
    });
    b.append(el('span', 'ars-brain__chip-mark', { 'aria-hidden': 'true' }));
    b.append(el('span', null, { text: label }));
    b.append(el('span', 'ars-brain__chip-n', { text: String(count) }));
    b.addEventListener('click', onToggle);
    return b;
  }

  function toggleFilter(type, value) {
    filter = (filter && filter.type === type && filter.value === value) ? null : { type, value };
    renderLegend();
    wake();
  }

  function renderLegend() {
    legend.textContent = '';
    const counts = {
      document: summary.documents || 0,
      passage: summary.passages || 0,
      fact: summary.facts || 0,
    };
    for (const kind of ['document', 'passage', 'fact']) {
      legend.append(chip(
        `ars-brain__chip--${kind}`,
        t(`brain.count.${kind === 'document' ? 'documents' : kind === 'passage' ? 'passages' : 'facts'}`, getLang()),
        counts[kind],
        () => toggleFilter('kind', kind),
        filter && filter.type === 'kind' && filter.value === kind,
      ));
    }
    const byLang = summary.by_language || {};
    for (const lang of Object.keys(byLang).sort()) {
      legend.append(chip(
        `ars-brain__chip--lang ars-brain__chip--${lang}`,
        lang.toUpperCase(),
        byLang[lang],
        () => toggleFilter('lang', lang),
        filter && filter.type === 'lang' && filter.value === lang,
      ));
    }
  }

  /* ------------------------------------------------------------------ data */

  let timer = 0;
  let inflight = false;

  async function refresh() {
    if (inflight || offline) return;
    inflight = true;
    try {
      const res = await fetch(`${baseUrl}/api/knowledge`, { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      lastError = false;
      ingest(data);
    } catch (err) {
      if (!lastError) {
        lastError = true;
        console.warn('[brain] /api/knowledge failed', err);
        say(t('brain.unreachable', getLang()));
      }
    } finally {
      inflight = false;
    }
  }

  function startPolling() {
    stopPolling();
    timer = setInterval(() => { if (!document.hidden) refresh(); }, pollMs);
  }
  function stopPolling() { if (timer) clearInterval(timer); timer = 0; }

  /* ------------------------------------------------------- visibility hooks */

  const io = typeof IntersectionObserver !== 'undefined'
    ? new IntersectionObserver((entries) => {
      onScreen = entries.some((e) => e.isIntersecting);
      if (onScreen) { resize(); wake(); } else sleep();
    }, { threshold: 0 })
    : null;
  if (io) io.observe(stage);

  const ro = typeof ResizeObserver !== 'undefined'
    ? new ResizeObserver(() => { resize(); if (autoFit) fitCamera(); wake(); })
    : null;
  if (ro) ro.observe(stage);

  function onVisibility() {
    if (document.hidden) sleep();
    else { wake(); refresh(); }
  }
  document.addEventListener('visibilitychange', onVisibility);
  if (reduceMotion.addEventListener) {
    reduceMotion.addEventListener('change', () => { settleNow(); wake(); });
  }

  /* -------------------------------------------------------------- no canvas */

  if (!ctx) {
    canvas.remove();
    tools.remove();
    const note = el('p', 'ars-brain__hint', { text: t('brain.unavailable', getLang()) });
    const list = el('ul', 'ars-brain__list');
    stage.append(note, list);
    const renderList = () => {
      list.textContent = '';
      for (const n of [...nodes.values()].sort((a, b) => KIND_ORDER[a.kind] - KIND_ORDER[b.kind])) {
        list.append(el('li', null, {
          text: `${t(`brain.node.${n.kind}`, getLang())}${n.language ? ` (${n.language})` : ''}: ${n.label}`,
        }));
      }
    };
    const originalIngest = ingest;
    // eslint-disable-next-line no-func-assign
    ingest = (data) => { originalIngest(data); renderList(); };
  }

  /* ------------------------------------------------------------------ init */

  readTheme();
  renderLegend();
  renderDetail();
  resize();
  refresh();
  startPolling();
  wake();

  const api = {
    panelRoot: panelHandle.root,
    refresh,
    setOffline(v) { offline = !!v; },
    setOnCounts(fn) { onCounts = fn; },
    select,
    expand: setExpanded,
    stats: () => ({
      nodes: nodes.size, edges: edges.length, fps: Math.round(lastFps),
      frameMs: Math.round(frameEma * 100) / 100, quality, running, alpha,
    }),
    retranslate() {
      if (panelHandle.setTitle) panelHandle.setTitle(t('brain.title', getLang()));
      fitBtn.textContent = t('brain.fit', getLang());
      expandBtn.textContent = overlay ? t('brain.close', getLang()) : t('brain.expand', getLang());
      emptyNote.textContent = t('brain.empty', getLang());
      hint.textContent = t('brain.hint', getLang());
      readTheme();
      renderLegend();
      renderDetail();
      wake();
    },
    destroy() {
      sleep();
      stopPolling();
      if (io) io.disconnect();
      if (ro) ro.disconnect();
      document.removeEventListener('visibilitychange', onVisibility);
      setExpanded(false);
      panelHandle.root.remove();
    },
  };
  stage.__brain = api;
  return api;
}
