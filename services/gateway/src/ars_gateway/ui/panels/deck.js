/* ==========================================================================
   A.R.S — panel deck
   services/gateway/src/ars_gateway/ui/panels/deck.js

   The side column used to be one unbroken stack: documents, permissions,
   audit, status, all present all the time, all fighting for the same 340px.
   This turns that stack into a selector — one instrument shown at a time, the
   rest one click away.

   It is a *container*, not a rewrite. Every panel still gets a plain element
   to append itself to and still owns everything inside it; the deck only
   decides which of those elements is on screen. `createDocumentsPanel({ root:
   deck.view('documents') })` is the whole integration.

   Styling is injected once at import from a single token-only string, same
   pattern as entity/hud.js. No build step, no stylesheet to keep in sync.
   ========================================================================== */

import { t } from './i18n.js';

const TAB_KEY = 'ars.console.tab';

const DECK_CSS = `
/* The side column was a scrolling stack of fixed-height panels; a deck needs one
   growing box instead. Stated here rather than in index.html so the container
   change travels with the component that needs it. */
.app__side { min-height: 0; }
.app__panels { flex: 1 1 auto; min-height: 0; overflow: visible; }
.ars-deck {
  display: flex; flex-direction: column;
  flex: 1 1 auto; min-height: 0;
  gap: var(--ars-space-2);
}
.ars-deck__tabs {
  display: flex; align-items: stretch; gap: 2px;
  flex: none;
  padding: 2px;
  background: var(--ars-surface-sunken);
  border: var(--ars-hairline) solid var(--ars-border);
  border-radius: var(--ars-radius-sm);
  box-shadow: var(--ars-inner-well);
  overflow-x: auto; overflow-y: hidden;
  scrollbar-width: none;
}
.ars-deck__tabs::-webkit-scrollbar { height: 0; }
.ars-deck__tab {
  position: relative;
  flex: 1 1 0; min-width: 0;
  display: flex; flex-direction: column; align-items: center; gap: 1px;
  padding: var(--ars-space-1) 2px 5px;
  background: transparent;
  border: none;
  border-radius: var(--ars-radius-xs);
  color: var(--ars-text-muted);
  font-family: var(--ars-font-display);
  font-size: var(--ars-text-2xs);
  font-weight: var(--ars-weight-semi);
  letter-spacing: var(--ars-track-label);
  text-transform: uppercase;
  cursor: pointer;
  transition: color var(--ars-dur-fast) var(--ars-ease-mech),
              background var(--ars-dur-fast) var(--ars-ease-mech);
}
.ars-deck__tab:hover { color: var(--ars-text-secondary); background: var(--ars-surface-hover); }
.ars-deck__tab[aria-selected="true"] {
  color: var(--ars-text-accent);
  background: var(--ars-surface-raised);
  text-shadow: var(--ars-text-glow);
}
/* the lit rail under the selected tab — the detent that says "you are here" */
.ars-deck__tab::after {
  content: ""; position: absolute; left: 18%; right: 18%; bottom: 1px; height: 2px;
  background: var(--ars-accent);
  box-shadow: var(--ars-glow-sm);
  transform: scaleX(0);
  transition: transform var(--ars-dur-base) var(--ars-ease-mech);
}
.ars-deck__tab[aria-selected="true"]::after { transform: scaleX(1); }
.ars-deck__tab svg { width: 15px; height: 15px; flex: none; display: block; }
.ars-deck__label {
  max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  line-height: 1.1;
}
.ars-deck__badge {
  position: absolute; top: 0; right: 3px;
  min-width: 13px; height: 13px; padding: 0 3px;
  display: none; align-items: center; justify-content: center;
  font-family: var(--ars-font-mono);
  font-variant-numeric: tabular-nums;
  font-size: 9px; font-weight: var(--ars-weight-bold); letter-spacing: 0;
  color: var(--ars-text-on-accent);
  background: var(--ars-accent-deep);
  border-radius: var(--ars-radius-pill);
}
.ars-deck__badge[data-show="1"] { display: inline-flex; }
.ars-deck__tab[aria-selected="true"] .ars-deck__badge {
  background: var(--ars-accent); box-shadow: var(--ars-glow-xs);
}
/* a badge that just changed pings once, so growth is visible from another tab */
@media (prefers-reduced-motion: no-preference) {
  .ars-deck__badge[data-ping="1"] { animation: ars-deck-ping var(--ars-dur-slower) var(--ars-ease-out) 2; }
}
@keyframes ars-deck-ping {
  0%   { transform: scale(1); }
  35%  { transform: scale(1.45); }
  100% { transform: scale(1); }
}

.ars-deck__views { flex: 1 1 auto; min-height: 0; display: flex; }
.ars-deck__view {
  flex: 1 1 auto; min-width: 0; min-height: 0;
  display: flex; flex-direction: column; gap: var(--ars-space-3);
  overflow-y: auto;
  scrollbar-width: thin;
  scrollbar-color: var(--ars-border-strong) transparent;
}
.ars-deck__view::-webkit-scrollbar { width: 6px; }
.ars-deck__view::-webkit-scrollbar-thumb { background: var(--ars-border-strong); border-radius: var(--ars-radius-pill); }
.ars-deck__view > .hud-panel { flex: 0 0 auto; }
.ars-deck__view > .hud-panel.ars-brain { flex: 1 1 auto; display: flex; flex-direction: column; min-height: 0; }
@media (prefers-reduced-motion: no-preference) {
  .ars-deck__view[data-entering="1"] { animation: ars-deck-in var(--ars-dur-base) var(--ars-ease-out); }
}
@keyframes ars-deck-in {
  from { opacity: 0; transform: translateY(4px); }
  to   { opacity: 1; transform: none; }
}
`;

/* 15px line icons. Inline, stroke: currentColor, so they take the tab's state
   colour for free and there is nothing to download. */
const ICONS = {
  brain: '<path d="M2 8.5h3M10 4.5h3M10 11h3M5.6 7.4 9 5M5.6 9.6 9 12"/><circle cx="6.6" cy="8.5" r="1.7"/><circle cx="13.2" cy="4" r="1.5"/><circle cx="13.2" cy="11.6" r="1.5"/><circle cx="1.8" cy="8.5" r="1.3"/>',
  documents: '<path d="M3.5 1.5h6l3 3v9h-9z"/><path d="M9.5 1.5v3h3M5.5 7h5M5.5 9.5h5M5.5 12h3"/>',
  grants: '<path d="M7.5 1.5 2.5 3.6v4.2c0 2.7 2 4.7 5 5.7 3-1 5-3 5-5.7V3.6z"/><path d="M5.4 7.6 6.9 9.2l3-3.4"/>',
  audit: '<path d="M2 3h11M2 6h7M2 9h11M2 12h5"/><circle cx="11.4" cy="11.6" r="2.1"/>',
  status: '<path d="M1.8 11a5.7 5.7 0 1 1 11.4 0"/><path d="M7.5 11 10.6 7"/><path d="M1.8 11h1.6M11.6 11h1.6M7.5 5.3v-1"/>',
};

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

function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 15 15');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.1');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  svg.innerHTML = ICONS[name] || '';
  return svg;
}

let injected = false;
function ensureStyles() {
  if (injected || typeof document === 'undefined') return;
  injected = true;
  const style = document.createElement('style');
  style.id = 'ars-deck-style';
  style.textContent = DECK_CSS;
  document.head.appendChild(style);
}

/**
 * createDeck({ root, getLang })
 *   .view(id, { labelKey, titleKey, icon })  -> HTMLElement to mount a panel into
 *   .select(id)
 *   .selectByChild(node)   -> selects whichever view contains `node`
 *   .setBadge(id, n)
 *   .active()              -> id of the visible view
 *   .onChange(fn)
 *   .retranslate()
 */
export function createDeck({ root, getLang }) {
  ensureStyles();

  const deck = el('div', 'ars-deck');
  const tabs = el('div', 'ars-deck__tabs', { role: 'tablist', 'aria-label': t('deck.aria', getLang()) });
  const views = el('div', 'ars-deck__views');
  deck.append(tabs, views);
  root.append(deck);

  const entries = new Map(); // id -> { tab, view, labelEl, badgeEl, labelKey, titleKey, badge }
  const listeners = [];
  let activeId = null;
  // Read the remembered tab *before* anything is registered. The first view to
  // register is shown provisionally so the deck is never blank, and that
  // provisional choice must not overwrite what the user last picked.
  let saved = null;
  try { saved = localStorage.getItem(TAB_KEY); } catch { /* private mode */ }

  function select(id, { focus = false, persist = true } = {}) {
    if (!entries.has(id) || id === activeId) {
      if (focus && entries.has(id)) entries.get(id).tab.focus();
      return;
    }
    activeId = id;
    for (const [key, e] of entries) {
      const on = key === id;
      e.tab.setAttribute('aria-selected', String(on));
      e.tab.tabIndex = on ? 0 : -1;
      e.view.hidden = !on;
      if (on) {
        e.view.dataset.entering = '1';
        setTimeout(() => { delete e.view.dataset.entering; }, 260);
        e.badgeEl.removeAttribute('data-ping');
      }
    }
    if (persist) { try { localStorage.setItem(TAB_KEY, id); } catch { /* private mode */ } }
    if (focus) entries.get(id).tab.focus();
    for (const fn of listeners) fn(id);
  }

  function view(id, { labelKey, titleKey, icon: iconName } = {}) {
    if (entries.has(id)) return entries.get(id).view;

    const tab = el('button', 'ars-deck__tab', {
      type: 'button', role: 'tab', id: `deck-tab-${id}`,
      'aria-controls': `deck-view-${id}`, 'aria-selected': 'false',
    });
    tab.tabIndex = -1;
    if (iconName) tab.append(icon(iconName));
    const labelEl = el('span', 'ars-deck__label', { text: labelKey ? t(labelKey, getLang()) : id });
    const badgeEl = el('span', 'ars-deck__badge', { 'aria-hidden': 'true' });
    tab.append(labelEl, badgeEl);

    const viewEl = el('div', 'ars-deck__view', {
      role: 'tabpanel', id: `deck-view-${id}`, 'aria-labelledby': `deck-tab-${id}`, tabindex: '-1',
    });
    viewEl.hidden = true;

    tab.addEventListener('click', () => select(id));
    tab.addEventListener('keydown', (ev) => {
      const ids = [...entries.keys()];
      const i = ids.indexOf(id);
      if (ev.key === 'ArrowRight' || ev.key === 'ArrowDown') {
        ev.preventDefault(); select(ids[(i + 1) % ids.length], { focus: true });
      } else if (ev.key === 'ArrowLeft' || ev.key === 'ArrowUp') {
        ev.preventDefault(); select(ids[(i - 1 + ids.length) % ids.length], { focus: true });
      } else if (ev.key === 'Home') {
        ev.preventDefault(); select(ids[0], { focus: true });
      } else if (ev.key === 'End') {
        ev.preventDefault(); select(ids[ids.length - 1], { focus: true });
      }
    });

    tabs.append(tab);
    views.append(viewEl);
    entries.set(id, { tab, view: viewEl, labelEl, badgeEl, labelKey, titleKey, badge: null });

    // First view registered is the default until restore() runs.
    if (activeId === null) select(id, { persist: false });
    return viewEl;
  }

  function setBadge(id, n) {
    const e = entries.get(id);
    if (!e) return;
    const value = (n === null || n === undefined || n === 0) ? null : n;
    const text = value === null ? '' : (value > 999 ? '999+' : String(value));
    const changed = e.badge !== null && e.badge !== value;
    e.badge = value;
    e.badgeEl.textContent = text;
    e.badgeEl.dataset.show = value === null ? '0' : '1';
    if (changed && activeId !== id) {
      e.badgeEl.dataset.ping = '1';
      setTimeout(() => e.badgeEl.removeAttribute('data-ping'), 1400);
    }
  }

  function restore() {
    if (saved && entries.has(saved)) select(saved);
  }

  function selectByChild(node) {
    for (const [id, e] of entries) {
      if (e.view.contains(node)) { select(id); return id; }
    }
    return null;
  }

  function retranslate() {
    tabs.setAttribute('aria-label', t('deck.aria', getLang()));
    for (const e of entries.values()) {
      if (e.labelKey) e.labelEl.textContent = t(e.labelKey, getLang());
      if (e.titleKey) e.tab.title = t(e.titleKey, getLang());
      e.tab.setAttribute('aria-label', e.titleKey ? t(e.titleKey, getLang()) : e.labelEl.textContent);
    }
  }

  return {
    root: deck,
    view,
    select,
    selectByChild,
    setBadge,
    restore,
    retranslate,
    active: () => activeId,
    onChange: (fn) => { listeners.push(fn); },
  };
}
