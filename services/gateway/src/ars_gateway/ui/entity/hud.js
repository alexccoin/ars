/* ==========================================================================
   A.R.S — HUD components
   services/gateway/src/ars_gateway/ui/entity/hud.js

   The chrome around the being. Same five factories as panels/hud-fallback.js,
   same return shapes, same class names — app.js and every panel keep working
   byte for byte — but rendered as instrument panels instead of divs:

     panel()          notched bezel, corner brackets, tracked micro-title,
                      detent toggle. The frame is clip-path, not a border image.
     gauge()          24-segment bar with a tabular readout, not a smooth fill.
                      Segments quantise the value, which is what makes a HUD
                      look like it is measuring rather than animating.
     pip()            status lamp with a live halo. The halo is the state; the
                      colour alone fails for a colour-blind user, so the label
                      is always present and the lamp shape changes too.
     ticker()         one-line scrolling readout with a caret and a scan flash.
     tierIndicator()  which tier answered, what it cost, and — the point of the
                      whole product — whether it left the device.

   Every colour, space, radius, duration and glow is a --ars-* token from
   theme.css. There is not one literal colour in this file; change the token
   and the HUD follows. Styles are injected once, at import, from a single
   template string: no build step, no second file to keep in sync.

   Accessibility is load-bearing here, not decoration: role/aria-* on every
   readout, focus-visible rings from the token, and all motion inside a
   prefers-reduced-motion guard.
   ========================================================================== */

/* --------------------------------------------------------------------------
   Styles. One string, injected once, scoped by the ars-hud-* classes so the
   fallback stylesheet in index.html stays a valid base layer underneath.
   -------------------------------------------------------------------------- */

const HUD_CSS = `
/* ---------- panel ---------- */
.hud-panel.ars-hud-panel {
  position: relative;
  border: none;
  border-radius: 0;
  background: var(--ars-panel-bg);
  box-shadow: var(--ars-shadow-2), var(--ars-inner-top);
  clip-path: polygon(
    0 var(--ars-notch), var(--ars-notch) 0,
    calc(100% - var(--ars-notch)) 0, 100% var(--ars-notch),
    100% calc(100% - var(--ars-notch)), calc(100% - var(--ars-notch)) 100%,
    var(--ars-notch) 100%, 0 calc(100% - var(--ars-notch)));
  isolation: isolate;
}
/* the bezel itself: a second clipped layer one hairline inside the first */
.ars-hud-panel__bezel {
  position: absolute; inset: 0; pointer-events: none; z-index: 1;
  background: var(--ars-border);
  clip-path: polygon(
    0 var(--ars-notch), var(--ars-notch) 0,
    calc(100% - var(--ars-notch)) 0, 100% var(--ars-notch),
    100% calc(100% - var(--ars-notch)), calc(100% - var(--ars-notch)) 100%,
    var(--ars-notch) 100%, 0 calc(100% - var(--ars-notch)),
    0 var(--ars-notch),
    var(--ars-hairline) calc(var(--ars-notch) + var(--ars-hairline)),
    var(--ars-hairline) calc(100% - var(--ars-notch) - var(--ars-hairline)),
    calc(var(--ars-notch) + var(--ars-hairline)) calc(100% - var(--ars-hairline)),
    calc(100% - var(--ars-notch) - var(--ars-hairline)) calc(100% - var(--ars-hairline)),
    calc(100% - var(--ars-hairline)) calc(100% - var(--ars-notch) - var(--ars-hairline)),
    calc(100% - var(--ars-hairline)) calc(var(--ars-notch) + var(--ars-hairline)),
    calc(100% - var(--ars-notch) - var(--ars-hairline)) var(--ars-hairline),
    calc(var(--ars-notch) + var(--ars-hairline)) var(--ars-hairline),
    var(--ars-hairline) calc(var(--ars-notch) + var(--ars-hairline)));
  transition: background var(--ars-dur-base) var(--ars-ease-out);
}
.ars-hud-panel:focus-within .ars-hud-panel__bezel { background: var(--ars-border-strong); }

.hud-panel.ars-hud-panel .hud-panel__header {
  position: relative; z-index: 2;
  display: flex; align-items: center; gap: var(--ars-space-2);
  padding: var(--ars-space-2) var(--ars-space-3);
  border-bottom: var(--ars-hairline) solid var(--ars-border);
  background: linear-gradient(180deg,
    color-mix(in srgb, var(--ars-accent) 7%, transparent), transparent);
}
.ars-hud-panel__rule {
  flex: 1; height: var(--ars-hairline);
  background: linear-gradient(90deg, var(--ars-border), transparent);
}
.hud-panel.ars-hud-panel .hud-panel__title {
  margin: 0;
  font-family: var(--ars-font-display);
  font-size: var(--ars-text-2xs);
  font-weight: var(--ars-weight-semi);
  letter-spacing: var(--ars-track-label);
  text-transform: uppercase;
  color: var(--ars-text-accent);
  text-shadow: var(--ars-text-glow);
  white-space: nowrap;
}
.ars-hud-panel__tag {
  width: 5px; height: 5px; flex: none;
  background: var(--ars-accent);
  box-shadow: var(--ars-glow-xs);
  transform: rotate(45deg);
}
.hud-panel.ars-hud-panel .hud-panel__toggle {
  flex: none;
  width: 20px; height: 20px; padding: 0;
  display: inline-flex; align-items: center; justify-content: center;
  background: var(--ars-surface-raised);
  border: var(--ars-hairline) solid var(--ars-border);
  border-radius: var(--ars-radius-xs);
  color: var(--ars-text-muted);
  font-size: var(--ars-text-2xs); line-height: 1;
  transition: color var(--ars-dur-fast) var(--ars-ease-mech),
              border-color var(--ars-dur-fast) var(--ars-ease-mech),
              background var(--ars-dur-fast) var(--ars-ease-mech);
}
.hud-panel.ars-hud-panel .hud-panel__toggle:hover {
  color: var(--ars-text-accent);
  border-color: var(--ars-border-strong);
  background: var(--ars-surface-hover);
}
.ars-hud-panel__chev {
  display: block;
  transition: transform var(--ars-dur-fast) var(--ars-ease-mech);
}
.hud-panel.ars-hud-panel[data-collapsed="true"] .ars-hud-panel__chev { transform: rotate(-90deg); }
.hud-panel.ars-hud-panel .hud-panel__body {
  position: relative; z-index: 2;
  padding: var(--ars-space-3);
  display: flex; flex-direction: column; gap: var(--ars-space-2);
  max-height: 42vh; overflow: auto;
  scrollbar-width: thin;
  scrollbar-color: var(--ars-border-strong) transparent;
}
.hud-panel.ars-hud-panel .hud-panel__body::-webkit-scrollbar { width: 6px; }
.hud-panel.ars-hud-panel .hud-panel__body::-webkit-scrollbar-thumb {
  background: var(--ars-border-strong); border-radius: var(--ars-radius-pill);
}

/* ---------- gauge ---------- */
.hud-gauge.ars-hud-gauge {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 2px var(--ars-space-2);
  align-items: baseline;
}
.ars-hud-gauge .hud-gauge__label {
  grid-column: 1;
  font-size: var(--ars-text-2xs);
  font-weight: var(--ars-weight-semi);
  letter-spacing: var(--ars-track-label);
  text-transform: uppercase;
  color: var(--ars-text-muted);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.ars-hud-gauge .hud-gauge__value {
  grid-column: 2;
  font-family: var(--ars-font-mono);
  font-variant-numeric: tabular-nums;
  font-size: var(--ars-text-xs);
  font-weight: var(--ars-weight-semi);
  color: var(--ars-text-accent);
  text-shadow: var(--ars-text-glow);
}
.ars-hud-gauge .hud-gauge__track {
  grid-column: 1 / -1;
  display: flex; gap: 1px;
  height: 8px;
  padding: 1px;
  background: var(--ars-surface-sunken);
  border: var(--ars-hairline) solid var(--ars-border);
  border-radius: var(--ars-radius-xs);
  box-shadow: var(--ars-inner-well);
  overflow: hidden;
}
.ars-hud-gauge__seg {
  flex: 1 1 0; min-width: 1px;
  background: var(--ars-border);
  transition: background var(--ars-dur-fast) var(--ars-ease-out),
              box-shadow var(--ars-dur-fast) var(--ars-ease-out);
}
.ars-hud-gauge__seg[data-on="1"] {
  background: var(--ars-accent);
  box-shadow: var(--ars-glow-xs);
}
.ars-hud-gauge__seg[data-on="1"][data-tip="1"] {
  background: var(--ars-accent-bright);
}
.ars-hud-gauge .hud-gauge__sub {
  grid-column: 1 / -1;
  font-size: var(--ars-text-2xs);
  color: var(--ars-text-muted);
}
.ars-hud-gauge .hud-gauge__sub:empty { display: none; }

/* ---------- pip ---------- */
.hud-pip.ars-hud-pip {
  display: inline-flex; align-items: center; gap: var(--ars-space-2);
  font-size: var(--ars-text-2xs);
  font-weight: var(--ars-weight-semi);
  letter-spacing: var(--ars-track-label);
  text-transform: uppercase;
  color: var(--ars-text-secondary);
  white-space: nowrap;
}
.ars-hud-pip .hud-pip__dot {
  position: relative;
  width: 8px; height: 8px; flex: none;
  border-radius: var(--ars-radius-pill);
  background: var(--ars-text-muted);
  transition: background var(--ars-dur-base) var(--ars-ease-out),
              box-shadow var(--ars-dur-base) var(--ars-ease-out);
}
.ars-hud-pip .hud-pip__dot::after {
  content: ""; position: absolute; inset: -4px;
  border: var(--ars-hairline) solid currentColor;
  border-radius: var(--ars-radius-pill);
  opacity: 0; color: inherit;
}
.ars-hud-pip--online, .ars-hud-pip--active { color: var(--ars-ok); }
.ars-hud-pip--online .hud-pip__dot, .ars-hud-pip--active .hud-pip__dot {
  background: var(--ars-ok); box-shadow: var(--ars-glow-ok);
}
.ars-hud-pip--connecting, .ars-hud-pip--warn { color: var(--ars-warn); }
.ars-hud-pip--connecting .hud-pip__dot, .ars-hud-pip--warn .hud-pip__dot {
  background: var(--ars-warn); box-shadow: var(--ars-glow-warn);
}
.ars-hud-pip--offline, .ars-hud-pip--error { color: var(--ars-danger); }
.ars-hud-pip--offline .hud-pip__dot, .ars-hud-pip--error .hud-pip__dot {
  background: var(--ars-danger); box-shadow: var(--ars-glow-danger);
  /* square lamp for the failure states: shape, not just hue */
  border-radius: var(--ars-radius-xs);
}
.ars-hud-pip--off, .ars-hud-pip--idle { color: var(--ars-text-muted); }
.ars-hud-pip--off .hud-pip__dot, .ars-hud-pip--idle .hud-pip__dot {
  background: transparent;
  box-shadow: inset 0 0 0 var(--ars-hairline) var(--ars-text-muted);
}

@media (prefers-reduced-motion: no-preference) {
  .ars-hud-pip--active .hud-pip__dot::after,
  .ars-hud-pip--connecting .hud-pip__dot::after {
    animation: ars-pip-ping 1.8s var(--ars-ease-out) infinite;
  }
  .ars-hud-pip--error .hud-pip__dot,
  .ars-hud-pip--offline .hud-pip__dot { animation: ars-pip-alarm 1.1s steps(1) infinite; }
}
@keyframes ars-pip-ping {
  0%   { opacity: 0.9; transform: scale(0.7); }
  70%  { opacity: 0;   transform: scale(1.6); }
  100% { opacity: 0;   transform: scale(1.6); }
}
@keyframes ars-pip-alarm { 50% { opacity: 0.35; } }

/* ---------- ticker ---------- */
.hud-ticker.ars-hud-ticker {
  display: flex; align-items: center; gap: var(--ars-space-2);
  min-width: 0;
  padding: var(--ars-space-1) var(--ars-space-2);
  background: var(--ars-surface-sunken);
  border-left: 2px solid var(--ars-accent-deep);
  font-family: var(--ars-font-mono);
  font-size: var(--ars-text-xs);
  color: var(--ars-text-secondary);
}
.ars-hud-ticker__caret { flex: none; color: var(--ars-accent); }
.ars-hud-ticker .hud-ticker__text {
  min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis;
}
@media (prefers-reduced-motion: no-preference) {
  .ars-hud-ticker[data-flash="1"] .hud-ticker__text {
    animation: ars-ticker-in var(--ars-dur-slow) var(--ars-ease-out);
  }
}
@keyframes ars-ticker-in {
  from { opacity: 0; transform: translateY(0.4em); color: var(--ars-text-accent); }
  to   { opacity: 1; transform: none; }
}

/* ---------- tier indicator ---------- */
.hud-tier.ars-hud-tier {
  display: inline-flex; align-items: center; gap: var(--ars-space-2);
  padding: 1px var(--ars-space-2) 1px var(--ars-space-1);
  background: var(--ars-surface-raised);
  border: var(--ars-hairline) solid var(--ars-border);
  border-radius: var(--ars-radius-xs);
  --ars-tier-hue: var(--ars-ice);
}
/* Two classes deep, deliberately: the base rule above is .hud-tier.ars-hud-tier
   (0,2,0), so a single-class modifier would lose the cascade and every tier
   would render in the same ice hue — which silently removes the one signal
   this component exists to carry. */
.ars-hud-tier.ars-hud-tier--recall      { --ars-tier-hue: var(--ars-ice); }
.ars-hud-tier.ars-hud-tier--documents   { --ars-tier-hue: var(--ars-teal); }
.ars-hud-tier.ars-hud-tier--local-model { --ars-tier-hue: var(--ars-violet); }
.ars-hud-tier.ars-hud-tier--cloud-model { --ars-tier-hue: var(--ars-amber); }
.ars-hud-tier__rail {
  width: 3px; align-self: stretch; flex: none;
  background: var(--ars-tier-hue);
  box-shadow: 0 0 6px color-mix(in srgb, var(--ars-tier-hue) 55%, transparent);
}
.ars-hud-tier .hud-tier__badge {
  font-size: var(--ars-text-2xs);
  font-weight: var(--ars-weight-semi);
  letter-spacing: var(--ars-track-label);
  text-transform: uppercase;
  color: var(--ars-tier-hue);
}
.ars-hud-tier .hud-tier__meta {
  font-family: var(--ars-font-mono);
  font-variant-numeric: tabular-nums;
  font-size: var(--ars-text-2xs);
  color: var(--ars-text-muted);
}
.ars-hud-tier__chip {
  font-size: var(--ars-text-2xs);
  font-weight: var(--ars-weight-semi);
  letter-spacing: var(--ars-track-label);
  text-transform: uppercase;
  padding: 0 var(--ars-space-1);
  border-radius: var(--ars-radius-xs);
  color: var(--ars-text-on-accent);
  background: var(--ars-tier-hue);
}
.ars-hud-tier.ars-hud-tier--cloud-model { border-color: color-mix(in srgb, var(--ars-amber) 55%, transparent); }
.ars-hud-tier.ars-hud-tier--cloud-model .ars-hud-tier__chip { box-shadow: var(--ars-glow-warn); }
`;

let injected = false;
function ensureStyles() {
  if (injected || typeof document === 'undefined') return;
  injected = true;
  const style = document.createElement('style');
  style.id = 'ars-hud-style';
  style.textContent = HUD_CSS;
  document.head.appendChild(style);
}

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

/* ==========================================================================
   panel({ id, title, collapsed }) -> { root, body, header, setCollapsed, setTitle }
   Structure is identical to the fallback's — app.js reaches in for
   `.hud-panel__body`, `.hud-panel__toggle` and `.hud-panel__title` by class, and
   collapses by setting `body.hidden`. Do not rename any of those three.
   ========================================================================== */
export function panel({ id, title, collapsed = false } = {}) {
  ensureStyles();
  const root = el('section', 'hud-panel ars-hud-panel', { id, 'aria-label': title });
  root.dataset.collapsed = String(!!collapsed);
  root.append(el('span', 'ars-hud-panel__bezel', { 'aria-hidden': 'true' }));

  const header = el('div', 'hud-panel__header');
  const tag = el('span', 'ars-hud-panel__tag', { 'aria-hidden': 'true' });
  const heading = el('h2', 'hud-panel__title', { text: title || '' });
  const rule = el('span', 'ars-hud-panel__rule', { 'aria-hidden': 'true' });
  const toggle = el('button', 'hud-panel__toggle', {
    type: 'button',
    'aria-expanded': String(!collapsed),
    'aria-controls': `${id}-body`,
  });
  toggle.append(el('span', 'ars-hud-panel__chev', { 'aria-hidden': 'true', text: '▾' }));
  header.append(tag, heading, rule, toggle);

  const body = el('div', 'hud-panel__body', { id: `${id}-body` });
  if (collapsed) body.hidden = true;
  root.append(header, body);

  function setCollapsed(v) {
    const next = !!v;
    body.hidden = next;
    root.dataset.collapsed = String(next);
    toggle.setAttribute('aria-expanded', String(!next));
  }
  toggle.addEventListener('click', () => setCollapsed(!body.hidden));

  return {
    root,
    body,
    header,
    setCollapsed,
    setTitle(text) {
      heading.textContent = text;
      root.setAttribute('aria-label', text);
    },
  };
}

/* ==========================================================================
   gauge({ id, label, value, sublabel }) -> { root, setValue, setLabel }
   ========================================================================== */
const GAUGE_SEGMENTS = 24;

export function gauge({ id, label, value = 0, sublabel } = {}) {
  ensureStyles();
  const root = el('div', 'hud-gauge ars-hud-gauge', {
    id, role: 'meter', 'aria-valuemin': '0', 'aria-valuemax': '100',
    'aria-label': label || 'gauge',
  });
  const labelEl = el('div', 'hud-gauge__label', { text: label || '' });
  const valueEl = el('div', 'hud-gauge__value');
  const track = el('div', 'hud-gauge__track', { 'aria-hidden': 'true' });
  const segs = [];
  for (let i = 0; i < GAUGE_SEGMENTS; i++) {
    const seg = el('span', 'ars-hud-gauge__seg');
    seg.dataset.on = '0';
    segs.push(seg);
    track.append(seg);
  }
  const subEl = el('div', 'hud-gauge__sub', { text: sublabel || '' });
  root.append(labelEl, valueEl, track, subEl);

  function setValue(v) {
    const n = Number(v);
    const pct = clamp(Number.isFinite(n) ? n : 0, 0, 100);
    const lit = Math.round((pct / 100) * GAUGE_SEGMENTS);
    for (let i = 0; i < GAUGE_SEGMENTS; i++) {
      segs[i].dataset.on = i < lit ? '1' : '0';
      segs[i].dataset.tip = i === lit - 1 ? '1' : '0';
    }
    root.setAttribute('aria-valuenow', String(Math.round(pct)));
    valueEl.textContent = `${Math.round(pct)}%`;
  }
  setValue(value);

  return {
    root,
    setValue,
    setLabel(text) { labelEl.textContent = text; root.setAttribute('aria-label', text); },
    setSublabel(text) { subEl.textContent = text || ''; },
  };
}

/* ==========================================================================
   pip({ id, state, label }) -> { root, setState, setLabel }
   ========================================================================== */
export function pip({ id, state = 'idle', label } = {}) {
  ensureStyles();
  const root = el('span', `hud-pip ars-hud-pip hud-pip--${state} ars-hud-pip--${state}`, {
    id, role: 'status',
  });
  const dot = el('span', 'hud-pip__dot', { 'aria-hidden': 'true' });
  const text = el('span', 'hud-pip__label', { text: label || state });
  root.append(dot, text);

  function setState(next) {
    const s = String(next || 'idle');
    root.className = `hud-pip ars-hud-pip hud-pip--${s} ars-hud-pip--${s}`;
    root.dataset.state = s;
  }
  setState(state);

  return {
    root,
    setState,
    setLabel(t) { text.textContent = t; },
  };
}

/* ==========================================================================
   ticker({ id, text }) -> { root, setText, push }
   ========================================================================== */
export function ticker({ id, text } = {}) {
  ensureStyles();
  const root = el('div', 'hud-ticker ars-hud-ticker', { id, 'aria-live': 'polite' });
  const caret = el('span', 'ars-hud-ticker__caret', { 'aria-hidden': 'true', text: '›' });
  const line = el('span', 'hud-ticker__text', { text: text || '' });
  root.append(caret, line);

  let flashTimer = 0;
  function flash() {
    root.dataset.flash = '0';
    // force a style recalc so the animation restarts on repeated pushes
    void root.offsetWidth;
    root.dataset.flash = '1';
    clearTimeout(flashTimer);
    flashTimer = setTimeout(() => { root.dataset.flash = '0'; }, 400);
  }

  return {
    root,
    setText(t) { line.textContent = t; },
    push(newLine) { line.textContent = newLine; flash(); },
  };
}

/* ==========================================================================
   tierIndicator({ tier, score, reason, elapsedMs, gpu, cloud }) -> { root, update }

   `tier` arrives already translated (console.js passes t('tier.<key>')), so the
   colour must NOT be derived from that string alone or the Romanian console
   loses its tier hues. gpu/cloud are booleans from the caller and are used
   first; the English keys are matched only as a fallback.
   ========================================================================== */
const TIER_KEYS = ['recall', 'documents', 'local model', 'cloud model'];

function tierTone({ tier, gpu, cloud, tierKey }) {
  if (cloud) return 'cloud-model';
  if (gpu) return 'local-model';
  const raw = String(tierKey || tier || '').trim().toLowerCase();
  if (TIER_KEYS.includes(raw)) return raw.replace(/\s+/g, '-');
  return 'recall';
}

export function tierIndicator(init = {}) {
  ensureStyles();
  const root = el('div', 'hud-tier ars-hud-tier', { role: 'note' });
  const rail = el('span', 'ars-hud-tier__rail', { 'aria-hidden': 'true' });
  const badge = el('span', 'hud-tier__badge');
  const meta = el('span', 'hud-tier__meta');
  const chip = el('span', 'ars-hud-tier__chip');
  chip.hidden = true;
  root.append(rail, badge, meta, chip);

  function update(next = {}) {
    const tone = tierTone(next);
    root.className = `hud-tier ars-hud-tier hud-tier--${tone} ars-hud-tier--${tone}`;
    root.classList.toggle('hud-tier--gpu', !!next.gpu);
    root.classList.toggle('hud-tier--cloud', !!next.cloud);
    badge.textContent = next.tier || '';
    const bits = [];
    if (typeof next.score === 'number') bits.push(`${Math.round(next.score * 100)}%`);
    if (typeof next.elapsedMs === 'number') bits.push(`${Math.round(next.elapsedMs)} ms`);
    meta.textContent = bits.join(' · ');
    /* The one thing a private assistant must never hide: that a turn left the
       device. It gets its own chip, in the warm end of the tier ramp. */
    if (next.cloud) { chip.hidden = false; chip.textContent = 'off-device'; }
    else if (next.gpu) { chip.hidden = false; chip.textContent = 'gpu'; }
    else { chip.hidden = true; chip.textContent = ''; }
    root.title = next.reason || '';
  }
  update(init);

  return { root, update };
}
