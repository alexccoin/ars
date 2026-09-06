// Consent prompts — impossible to miss, and the guard's exact wording, never re-worded.
//
// `spoken_prompt` and `request.reason` are composed by the guard, in the user's language,
// specifically so an attacker who has tainted the turn cannot put words in the consent
// prompt's mouth. This module renders those two strings verbatim. Every other label on the
// dialog (headings, button text) is our own UI chrome and is translated normally.

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

const FOCUSABLE = 'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';

export function createConsentQueue({ root, getLang, onRespond }) {
  const overlay = el('div', 'consent-overlay');
  overlay.hidden = true;
  root.append(overlay);

  const queue = [];
  let current = null;
  let lastFocused = null;

  function trapFocus(ev) {
    if (ev.key !== 'Tab' || !current) return;
    const focusables = Array.from(current.dialog.querySelectorAll(FOCUSABLE)).filter((n) => !n.disabled);
    if (focusables.length === 0) return;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    if (ev.shiftKey && document.activeElement === first) {
      ev.preventDefault();
      last.focus();
    } else if (!ev.shiftKey && document.activeElement === last) {
      ev.preventDefault();
      first.focus();
    }
  }

  function onKeydown(ev) {
    if (!current) return;
    if (ev.key === 'Escape') {
      ev.preventDefault();
      current.denyBtn.click();
    } else {
      trapFocus(ev);
    }
  }

  function renderCurrent() {
    overlay.innerHTML = '';
    if (!current) {
      overlay.hidden = true;
      if (lastFocused && lastFocused.focus) lastFocused.focus();
      return;
    }
    overlay.hidden = false;
    overlay.append(current.dialog);
    lastFocused = document.activeElement;
    current.denyBtn.focus();
  }

  function next() {
    current = queue.shift() || null;
    renderCurrent();
  }

  function respond(item, approved, remember) {
    onRespond && onRespond({ request_id: item.event.request_id, approved, remember });
    if (current === item) next();
    else {
      const idx = queue.indexOf(item);
      if (idx >= 0) queue.splice(idx, 1);
    }
  }

  function show(event) {
    const dialog = el('div', 'consent-dialog', {
      role: 'alertdialog',
      'aria-modal': 'true',
      'aria-labelledby': 'consent-title',
      'aria-describedby': 'consent-prompt',
    });

    const title = el('h2', 'consent-dialog__title', { id: 'consent-title', text: t('consent.title', getLang()) });

    // The guard's exact wording. Never translated, never paraphrased — see module comment.
    const prompt = el('p', 'consent-dialog__prompt', { id: 'consent-prompt', text: event.spoken_prompt || '' });

    const facts = el('dl', 'consent-dialog__facts');
    function fact(term, value) {
      facts.append(el('dt', null, { text: term }), el('dd', null, { text: value ?? '—' }));
    }
    const req = event.request || {};
    fact(t('consent.capability', getLang()), req.capability);
    fact(t('consent.resource', getLang()), (req.resource_patterns || []).join(', '));
    // req.reason is also guard-composed and quoted verbatim — see module comment.
    fact(t('consent.reason', getLang()), req.reason);

    const actions = el('div', 'consent-dialog__actions');
    const denyBtn = el('button', 'consent-dialog__deny', { type: 'button', text: t('consent.deny', getLang()) });
    const approveBtn = el('button', 'consent-dialog__approve', { type: 'button', text: t('consent.approve', getLang()) });
    const rememberBtn = el('button', 'consent-dialog__approve-remember', { type: 'button', text: t('consent.approve_remember', getLang()) });
    actions.append(denyBtn, approveBtn, rememberBtn);

    dialog.append(title, prompt, facts, actions);

    const item = { event, dialog, denyBtn };
    denyBtn.addEventListener('click', () => respond(item, false, false));
    approveBtn.addEventListener('click', () => respond(item, true, false));
    rememberBtn.addEventListener('click', () => respond(item, true, true));

    if (current) queue.push(item);
    else { current = item; renderCurrent(); }
  }

  document.addEventListener('keydown', onKeydown);

  return { show };
}
