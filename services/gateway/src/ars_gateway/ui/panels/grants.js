// Permissions / grants — what A.R.S may touch, and the revoke control.
//
// Revoke is optimistic: the row disappears immediately (that is the whole point — "instant"
// means instant, not "instant unless the network is slow"). If the DELETE fails, the row is
// restored and an error is shown, so state never quietly diverges from the server's.

import { t } from './i18n.js';

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

function fmtDate(ms, lang) {
  if (!ms) return null;
  try {
    return new Date(ms).toLocaleString(lang === 'ro' ? 'ro-RO' : 'en-US');
  } catch {
    return new Date(ms).toISOString();
  }
}

export function createGrantsPanel({ root, hud, getLang, baseUrl = '' }) {
  let panelHandle;
  if (hud && typeof hud.panel === 'function') {
    try {
      panelHandle = hud.panel({ id: 'panel-grants', title: t('grants.title', getLang()) });
    } catch (err) {
      console.warn('[grants] hud.panel failed, using plain container', err);
    }
  }
  if (!panelHandle) {
    const container = el('section', 'hud-panel', { id: 'panel-grants', 'aria-label': t('grants.title', getLang()) });
    const header = el('div', 'hud-panel__header');
    header.append(el('h2', 'hud-panel__title', { text: t('grants.title', getLang()) }));
    const body = el('div', 'hud-panel__body');
    container.append(header, body);
    panelHandle = { root: container, body, setTitle(text) { header.firstChild.textContent = text; } };
  }
  root.append(panelHandle.root);

  const messages = el('div', 'panel-messages', { role: 'status', 'aria-live': 'polite' });
  const list = el('ul', 'grants__list', { 'aria-label': t('grants.title', getLang()) });
  const empty = el('p', 'grants__empty', { text: t('grants.empty', getLang()) });
  panelHandle.body.append(messages, list, empty);

  let grants = [];

  function say(text, kind = 'info') {
    const line = el('div', `panel-message panel-message--${kind}`, { text });
    messages.append(line);
    setTimeout(() => line.remove(), 6000);
  }

  function renderList() {
    list.innerHTML = '';
    empty.hidden = grants.length > 0;
    for (const g of grants) {
      const li = el('li', 'grants__item');
      const cap = el('span', 'grants__cap', { text: g.capability });
      const scope = el('span', 'grants__scope', { text: (g.resource_patterns || ['*']).join(', ') });
      const confirm = el('span', 'grants__confirm', { text: g.confirm || '' });
      const expires = fmtDate(g.expires_at_ms, getLang());
      const expiryEl = el('span', 'grants__expiry', {
        text: `${t('grants.expires', getLang())}: ${expires || t('grants.never_expires', getLang())}`,
      });
      const revoke = el('button', 'grants__revoke', {
        type: 'button',
        text: t('grants.revoke', getLang()),
        'aria-label': `${t('grants.revoke', getLang())} ${g.capability}`,
      });
      revoke.addEventListener('click', () => revokeGrant(g));
      li.append(cap, scope, confirm, expiryEl, revoke);
      list.append(li);
    }
  }

  async function refresh() {
    try {
      const res = await fetch(`${baseUrl}/api/grants`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      grants = await res.json();
      renderList();
    } catch (err) {
      console.warn('[grants] refresh failed', err);
    }
  }

  async function revokeGrant(g) {
    const prev = grants;
    grants = grants.filter((x) => x.id !== g.id);
    renderList();
    try {
      const res = await fetch(`${baseUrl}/api/grants/${encodeURIComponent(g.id)}`, { method: 'DELETE' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      say(t('grants.revoked', getLang(), { cap: g.capability }));
    } catch (err) {
      grants = prev;
      renderList();
      say(t('grants.revoke_failed', getLang(), { cap: g.capability }), 'error');
    }
  }

  function retranslate() {
    if (panelHandle.setTitle) panelHandle.setTitle(t('grants.title', getLang()));
    empty.textContent = t('grants.empty', getLang());
    renderList();
  }

  refresh();

  return { panelRoot: panelHandle.root, refresh, retranslate };
}
