// Status panel — model, backend, languages, and the tier ladder's track record. This is
// where "how much GPU did we actually avoid" becomes a number Alex can point at.

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

export function createStatusPanel({ root, hud, getLang, baseUrl = '' }) {
  let panelHandle;
  if (hud && typeof hud.panel === 'function') {
    try {
      panelHandle = hud.panel({ id: 'panel-status', title: t('status.title', getLang()) });
    } catch (err) {
      console.warn('[status] hud.panel failed, using plain container', err);
    }
  }
  if (!panelHandle) {
    const container = el('section', 'hud-panel', { id: 'panel-status', 'aria-label': t('status.title', getLang()) });
    const header = el('div', 'hud-panel__header');
    header.append(el('h2', 'hud-panel__title', { text: t('status.title', getLang()) }));
    const body = el('div', 'hud-panel__body');
    container.append(header, body);
    panelHandle = { root: container, body, setTitle(text) { header.firstChild.textContent = text; } };
  }
  root.append(panelHandle.root);

  const messages = el('div', 'panel-messages', { role: 'status', 'aria-live': 'polite' });
  const dl = el('dl', 'status__facts');
  const refreshBtn = el('button', 'status__refresh', { type: 'button', text: t('status.refresh', getLang()) });

  let gaugeHandle;
  const gaugeMount = el('div', 'status__gauge-mount');
  const tierTable = el('table', 'status__tiers');
  const tierBody = el('tbody');
  tierTable.append(
    el('thead', null, null),
    tierBody,
  );

  panelHandle.body.append(messages, dl, gaugeMount, tierTable, refreshBtn);

  function say(text, kind = 'info') {
    const line = el('div', `panel-message panel-message--${kind}`, { text });
    messages.append(line);
    setTimeout(() => line.remove(), 6000);
  }

  function fact(term, value) {
    const dt = el('dt', null, { text: term });
    const dd = el('dd', null, { text: value ?? '—' });
    return [dt, dd];
  }

  function renderStatus(data) {
    dl.innerHTML = '';
    dl.append(
      ...fact(t('status.model', getLang()), data.model),
      ...fact(t('status.backend', getLang()), data.backend),
      ...fact(t('status.languages', getLang()), (data.languages || []).join(', ')),
      ...fact(t('status.total_turns', getLang()), data.tiers ? data.tiers.total_turns : null),
    );

    const pct = data.tiers ? Math.round(data.tiers.gpu_avoided_pct || 0) : 0;
    if (!gaugeHandle && hud && typeof hud.gauge === 'function') {
      try {
        gaugeHandle = hud.gauge({
          id: 'status-gpu-gauge',
          label: t('status.gpu_avoided', getLang()),
          value: pct,
        });
        gaugeMount.append(gaugeHandle.root);
      } catch (err) { /* fall through */ }
    }
    if (gaugeHandle) {
      gaugeHandle.setValue(pct);
    } else {
      gaugeMount.textContent = `${t('status.gpu_avoided', getLang())}: ${pct}%`;
    }

    tierBody.innerHTML = '';
    const counts = (data.tiers && data.tiers.counts) || {};
    for (const [tier, n] of Object.entries(counts)) {
      const tr = el('tr');
      tr.append(el('td', null, { text: t(`tier.${tier}`, getLang()) || tier }), el('td', null, { text: String(n) }));
      tierBody.append(tr);
    }
  }

  async function refresh() {
    dl.textContent = '';
    dl.append(...fact(t('status.loading', getLang()), ''));
    try {
      const res = await fetch(`${baseUrl}/api/status`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      renderStatus(data);
    } catch (err) {
      dl.innerHTML = '';
      say(t('status.unavailable', getLang()), 'error');
    }
  }

  refreshBtn.addEventListener('click', refresh);

  function retranslate() {
    if (panelHandle.setTitle) panelHandle.setTitle(t('status.title', getLang()));
    refreshBtn.textContent = t('status.refresh', getLang());
    refresh();
  }

  refresh();

  return { panelRoot: panelHandle.root, refresh, retranslate };
}
