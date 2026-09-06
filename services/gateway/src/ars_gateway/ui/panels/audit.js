// Audit log — what A.R.S actually did, live-tailing. Every guard decision is here:
// what was asked, what was decided, and whether the turn that asked for it was tainted.

import { t } from './i18n.js';

const POLL_MS = 20000;
const MAX_ROWS = 200;

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

function fmtTime(ms, lang) {
  try {
    return new Date(ms).toLocaleTimeString(lang === 'ro' ? 'ro-RO' : 'en-US');
  } catch {
    return '';
  }
}

export function createAuditPanel({ root, hud, getLang, baseUrl = '' }) {
  let panelHandle;
  if (hud && typeof hud.panel === 'function') {
    try {
      panelHandle = hud.panel({ id: 'panel-audit', title: t('audit.title', getLang()) });
    } catch (err) {
      console.warn('[audit] hud.panel failed, using plain container', err);
    }
  }
  if (!panelHandle) {
    const container = el('section', 'hud-panel', { id: 'panel-audit', 'aria-label': t('audit.title', getLang()) });
    const header = el('div', 'hud-panel__header');
    header.append(el('h2', 'hud-panel__title', { text: t('audit.title', getLang()) }));
    const body = el('div', 'hud-panel__body');
    container.append(header, body);
    panelHandle = { root: container, body, setTitle(text) { header.firstChild.textContent = text; } };
  }
  root.append(panelHandle.root);

  let livePip;
  if (hud && typeof hud.pip === 'function') {
    try {
      livePip = hud.pip({ id: 'audit-live-pip', state: 'active', label: t('audit.live', getLang()) });
    } catch (err) { /* fall through to plain */ }
  }
  const liveIndicator = livePip ? livePip.root : el('span', 'hud-pip hud-pip--active', { text: t('audit.live', getLang()) });

  const list = el('ol', 'audit__list', { 'aria-label': t('audit.title', getLang()) });
  const empty = el('p', 'audit__empty', { text: t('audit.empty', getLang()) });
  panelHandle.body.append(liveIndicator, list, empty);

  let rows = [];
  const seenIds = new Set();

  function renderRow(rec) {
    const li = el('li', `audit__row audit__row--${rec.verdict || 'allow'}`);
    if (rec.tainted) li.classList.add('audit__row--tainted');
    const time = el('span', 'audit__time', { text: fmtTime(rec.at_ms || Date.now(), getLang()) });
    const cap = el('span', 'audit__cap', { text: rec.capability || '' });
    const resource = el('span', 'audit__resource', { text: rec.resource || '' });
    const verdict = el('span', 'audit__verdict', { text: t(`audit.verdict.${rec.verdict || 'allow'}`, getLang()) });
    li.append(time, cap, resource, verdict);
    if (rec.tainted) li.append(el('span', 'audit__tainted-tag', { text: t('audit.tainted', getLang()) }));
    if (rec.outcome) li.append(el('span', 'audit__outcome', { text: rec.outcome }));
    return li;
  }

  function renderAll() {
    list.innerHTML = '';
    empty.hidden = rows.length > 0;
    for (const rec of rows) list.append(renderRow(rec));
  }

  function pushRecord(rec, { front = true } = {}) {
    const id = rec.id || `${rec.at_ms || Date.now()}-${rec.capability}-${rec.resource || ''}`;
    if (seenIds.has(id)) return;
    seenIds.add(id);
    if (front) rows.unshift(rec); else rows.push(rec);
    rows = rows.slice(0, MAX_ROWS);
    empty.hidden = true;
    const rowEl = renderRow(rec);
    if (front) list.prepend(rowEl); else list.append(rowEl);
    while (list.children.length > MAX_ROWS) list.removeChild(list.lastChild);
  }

  async function refresh() {
    try {
      const res = await fetch(`${baseUrl}/api/audit?limit=50`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      rows = data;
      seenIds.clear();
      for (const r of rows) seenIds.add(r.id || `${r.at_ms}-${r.capability}-${r.resource || ''}`);
      renderAll();
    } catch (err) {
      console.warn('[audit] refresh failed', err);
    }
  }

  const timer = setInterval(refresh, POLL_MS);

  function retranslate() {
    if (panelHandle.setTitle) panelHandle.setTitle(t('audit.title', getLang()));
    empty.textContent = t('audit.empty', getLang());
    if (livePip && livePip.setLabel) livePip.setLabel(t('audit.live', getLang()));
    renderAll();
  }

  refresh();

  return {
    panelRoot: panelHandle.root,
    refresh,
    pushRecord,
    retranslate,
    destroy() { clearInterval(timer); },
  };
}
