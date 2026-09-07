// Fallback implementations of the artifact-engineer's contract.
//
// Real modules (owned by the artifact-engineer, DO NOT edit from here):
//   ./entity/ars-entity.js  -> export class ArsEntity { setState, setIntensity, setTier, destroy }
//   ./entity/hud.js         -> export function panel, gauge, pip, ticker, tierIndicator
//
// Neither file exists yet at the time this shell was built. app.js tries to import the
// real modules first and falls back to the plain-DOM implementations below so the console
// is fully usable today. The exact call signatures below are what app.js and the panels/
// call — if the real hud.js lands with a different shape, only the two `load*()` functions
// in app.js need an adapter, nothing else in this file touches app logic.
//
// Assumed contract (documented here because no spec file exists elsewhere):
//   panel({ id, title, collapsed=false })
//     -> { root, body, header, setCollapsed(bool), setTitle(str) }
//   gauge({ id, label, value /* 0..100 */, sublabel })
//     -> { root, setValue(number), setLabel(str) }
//   pip({ id, state /* 'idle'|'active'|'warn'|'error'|'off' */, label })
//     -> { root, setState(str), setLabel(str) }
//   ticker({ id, text })
//     -> { root, setText(str), push(line) }
//   tierIndicator({ tier, score, reason, elapsedMs, gpu, cloud })
//     -> { root, update({ tier, score, reason, elapsedMs, gpu, cloud }) }
//   class ArsEntity(mountEl)
//     .setState(AgentState string)
//     .setIntensity(0..1)
//     .setTier(tierLabel string | null)
//     .destroy()

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

export function panel({ id, title, collapsed = false } = {}) {
  const root = el('section', 'hud-panel', { id, 'aria-label': title });
  const header = el('div', 'hud-panel__header');
  const heading = el('h2', 'hud-panel__title', { text: title });
  const toggle = el('button', 'hud-panel__toggle', {
    type: 'button',
    'aria-expanded': String(!collapsed),
    'aria-controls': `${id}-body`,
    text: collapsed ? '▸' : '▾',
  });
  header.append(heading, toggle);
  const body = el('div', 'hud-panel__body', { id: `${id}-body` });
  if (collapsed) body.hidden = true;
  root.append(header, body);

  function setCollapsed(v) {
    body.hidden = !!v;
    toggle.setAttribute('aria-expanded', String(!v));
    toggle.textContent = v ? '▸' : '▾';
  }
  toggle.addEventListener('click', () => setCollapsed(!body.hidden ? true : false));

  return {
    root,
    body,
    header,
    setCollapsed,
    setTitle(text) { heading.textContent = text; },
  };
}

export function gauge({ id, label, value = 0, sublabel } = {}) {
  const root = el('div', 'hud-gauge', { id, role: 'meter', 'aria-valuemin': '0', 'aria-valuemax': '100' });
  const track = el('div', 'hud-gauge__track');
  const fill = el('div', 'hud-gauge__fill');
  track.append(fill);
  const labelEl = el('div', 'hud-gauge__label', { text: label || '' });
  const valueEl = el('div', 'hud-gauge__value');
  const subEl = el('div', 'hud-gauge__sub', { text: sublabel || '' });
  root.append(labelEl, track, valueEl, subEl);

  function setValue(v) {
    const clamped = Math.max(0, Math.min(100, v));
    fill.style.width = `${clamped}%`;
    root.setAttribute('aria-valuenow', String(Math.round(clamped)));
    valueEl.textContent = `${Math.round(clamped)}%`;
  }
  setValue(value);

  return {
    root,
    setValue,
    setLabel(text) { labelEl.textContent = text; },
  };
}

export function pip({ id, state = 'idle', label } = {}) {
  const root = el('span', `hud-pip hud-pip--${state}`, { id, role: 'status' });
  const dot = el('span', 'hud-pip__dot', { 'aria-hidden': 'true' });
  const text = el('span', 'hud-pip__label', { text: label || state });
  root.append(dot, text);

  function setState(next) {
    root.className = `hud-pip hud-pip--${next}`;
  }
  return {
    root,
    setState,
    setLabel(text2) { text.textContent = text2; },
  };
}

export function ticker({ id, text } = {}) {
  const root = el('div', 'hud-ticker', { id, 'aria-live': 'polite' });
  const line = el('span', 'hud-ticker__text', { text: text || '' });
  root.append(line);
  return {
    root,
    setText(t2) { line.textContent = t2; },
    push(newLine) { line.textContent = newLine; },
  };
}

export function tierIndicator({ tier, score, reason, elapsedMs, gpu, cloud } = {}) {
  const root = el('div', 'hud-tier', { role: 'note' });
  const badge = el('span', 'hud-tier__badge');
  const meta = el('span', 'hud-tier__meta');
  root.append(badge, meta);

  function update(next) {
    root.className = `hud-tier hud-tier--${(next.tier || 'recall').replace(/\s+/g, '-')}`;
    root.classList.toggle('hud-tier--gpu', !!next.gpu);
    root.classList.toggle('hud-tier--cloud', !!next.cloud);
    badge.textContent = next.tier || '';
    const bits = [];
    if (typeof next.score === 'number') bits.push(`${Math.round(next.score * 100)}%`);
    if (typeof next.elapsedMs === 'number') bits.push(`${Math.round(next.elapsedMs)} ms`);
    meta.textContent = bits.join(' · ');
    root.title = next.reason || '';
  }
  update({ tier, score, reason, elapsedMs, gpu, cloud });

  return { root, update };
}

/** Minimal stand-in for the real HUD entity. Renders an honest text/CSS state readout,
 * nothing fancy — the artifact-engineer's canvas/WebGL entity replaces this visual, not
 * the state contract it exposes. */
export class ArsEntityFallback {
  constructor(mount) {
    this.mount = mount;
    this.root = el('div', 'entity-fallback', {
      role: 'img',
      'aria-label': 'A.R.S status indicator (fallback renderer)',
    });
    this.core = el('div', 'entity-fallback__core');
    this.label = el('div', 'entity-fallback__label', { text: 'idle' });
    this.root.append(this.core, this.label);
    mount.appendChild(this.root);
    this._tier = null;
  }
  setState(state) {
    this.root.dataset.state = state;
    this.label.textContent = state;
  }
  setIntensity(v) {
    const clamped = Math.max(0, Math.min(1, v));
    this.root.style.setProperty('--entity-intensity', String(clamped));
  }
  setTier(tierLabel) {
    this._tier = tierLabel;
    this.root.dataset.tier = tierLabel ? tierLabel.replace(/\s+/g, '-') : '';
  }
  // The text fallback cannot change shape, but it must not throw when asked to:
  // app.js calls setPersona on whatever entity it got.
  setPersona(persona) {
    this._persona = persona === 'companion' ? 'companion' : 'default';
    this.root.dataset.persona = this._persona;
  }
  destroy() {
    this.root.remove();
  }
}
