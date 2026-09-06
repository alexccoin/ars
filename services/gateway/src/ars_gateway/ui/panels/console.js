// The command console — the primary interface. A text input with history, a streaming
// transcript, and an honest tier readout on every answer.
//
// This module is deliberately dumb about *what* commands mean: it only knows how to take a
// line of input and hand it to `onSubmit`, and how to render turns it is told about.
// Slash-command semantics and API calls live in app.js, which has access to every panel.

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

const HISTORY_KEY = 'ars.console.history';
const HISTORY_MAX = 200;

function loadHistory() {
  try {
    const raw = localStorage.getItem(HISTORY_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function saveHistory(list) {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(list.slice(-HISTORY_MAX)));
  } catch {
    /* storage full or disabled — history just won't persist across reloads */
  }
}

export function createConsole({ root, getLang, hud, onSubmit, onInterrupt, onEscalate, onListen }) {
  const history = loadHistory();
  let historyCursor = history.length;
  let draft = '';
  let pendingTurnId = null;

  root.innerHTML = '';
  root.className = 'console';

  const log = el('div', 'console__log', { id: 'console-log', role: 'log', 'aria-label': 'Conversation' });
  const announcer = el('div', 'sr-only', { 'aria-live': 'polite', 'aria-atomic': 'true', id: 'console-announcer' });
  const form = el('form', 'console__form', { autocomplete: 'off' });
  const inputLabel = el('label', 'sr-only', { for: 'console-input', text: t('console.input_label', getLang()) });
  const input = el('input', 'console__input', {
    id: 'console-input',
    type: 'text',
    name: 'command',
    placeholder: t('console.placeholder', getLang()),
    'aria-describedby': 'console-help',
    autocapitalize: 'off',
    spellcheck: 'false',
  });
  const help = el('div', 'console__help sr-only', { id: 'console-help', text: t('console.help', getLang()) });

  const sendBtn = el('button', 'console__send', { type: 'submit', text: t('console.send', getLang()) });
  const micBtn = el('button', 'console__mic', {
    type: 'button',
    'aria-pressed': 'false',
    title: t('console.mic', getLang()),
    'aria-label': t('console.mic', getLang()),
    text: '🎤',
  });
  const stopBtn = el('button', 'console__stop', {
    type: 'button',
    title: t('console.interrupt', getLang()),
    'aria-label': t('console.interrupt', getLang()),
    text: '■',
  });
  stopBtn.hidden = true;

  form.append(input, micBtn, stopBtn, sendBtn);
  root.append(log, announcer, help, form);
  // inputLabel intentionally not appended twice — associate via `for`, keep node in DOM once.
  form.prepend(inputLabel);

  function scrollToEnd() {
    log.scrollTop = log.scrollHeight;
  }

  function announce(text) {
    announcer.textContent = '';
    // Re-trigger even if the text is identical to the last announcement.
    requestAnimationFrame(() => { announcer.textContent = text; });
  }

  function appendSystemLine(text, kind = 'info') {
    const line = el('div', `console__line console__line--system console__line--${kind}`);
    line.textContent = text;
    log.append(line);
    scrollToEnd();
    return line;
  }

  function appendUserText(text) {
    const line = el('div', 'console__line console__line--user');
    const who = el('span', 'console__who', { text: t('console.you', getLang()) });
    const body = el('span', 'console__body', { text });
    line.append(who, body);
    log.append(line);
    scrollToEnd();
  }

  /** Begins a new assistant turn. Returns a handle the caller streams into. */
  function beginReply(turnId, { question } = {}) {
    pendingTurnId = turnId;
    stopBtn.hidden = false;

    const turn = el('div', 'console__turn');
    turn.dataset.turnId = turnId;
    const line = el('div', 'console__line console__line--ars');
    const who = el('span', 'console__who', { text: t('console.ars', getLang()) });
    const body = el('span', 'console__body', { text: '' });
    const cursor = el('span', 'console__cursor', { 'aria-hidden': 'true' });
    body.append(cursor);
    line.append(who, body);

    const activity = el('ul', 'console__activity', { 'aria-label': 'tool activity' });
    const tierSlot = el('div', 'console__tier-slot');
    const actions = el('div', 'console__actions');

    turn.append(line, activity, tierSlot, actions);
    log.append(turn);
    scrollToEnd();

    let fullText = '';
    let tierData = null;
    let settled = false;

    function appendDelta(text) {
      fullText += text;
      body.textContent = fullText;
      body.append(cursor);
      scrollToEnd();
    }

    function setTier(data) {
      tierData = data;
      tierSlot.innerHTML = '';
      actions.innerHTML = '';

      let indicator = null;
      if (hud && typeof hud.tierIndicator === 'function') {
        try {
          indicator = hud.tierIndicator({
            tier: t(`tier.${data.tier}`, getLang()) || data.tier,
            score: data.score,
            reason: data.reason,
            elapsedMs: data.elapsed_ms,
            gpu: data.tier === 'local model',
            cloud: data.tier === 'cloud model',
          });
        } catch (err) {
          console.warn('[console] hud.tierIndicator failed, using plain readout', err);
        }
      }
      if (indicator) {
        tierSlot.append(indicator.root);
      } else {
        const badge = el('span', `tier-badge tier-badge--${data.tier.replace(/\s+/g, '-')}`, {
          text: t(`tier.${data.tier}`, getLang()) || data.tier,
        });
        const score = el('span', 'tier-meta', {
          text: t('tier.score', getLang(), { score: `${Math.round((data.score || 0) * 100)}%` }),
        });
        const elapsed = el('span', 'tier-meta', { text: t('tier.elapsed', getLang(), { ms: Math.round(data.elapsed_ms || 0) }) });
        tierSlot.append(badge, score, elapsed);
      }

      const costBadge = el('span', 'tier-cost');
      if (data.tier === 'local model') costBadge.textContent = t('tier.gpu_badge', getLang());
      else if (data.tier === 'cloud model') costBadge.textContent = t('tier.cloud_badge', getLang());
      else costBadge.textContent = t('tier.cpu_badge', getLang());
      costBadge.classList.add(data.tier === 'local model' || data.tier === 'cloud model' ? 'tier-cost--spent' : 'tier-cost--free');
      tierSlot.append(costBadge);

      const reasonEl = el('div', 'tier-reason', { text: data.reason || '' });
      tierSlot.append(reasonEl);

      if (data.citations && data.citations.length) {
        const cite = el('div', 'tier-citations', {
          text: t('tier.citations', getLang(), { list: data.citations.join(', ') }),
        });
        tierSlot.append(cite);
      }

      if (data.tier !== 'local model' && data.tier !== 'cloud model' && data.can_escalate !== false) {
        const btn = el('button', 'console__escalate', { type: 'button', text: t('console.escalate', getLang()) });
        btn.addEventListener('click', () => {
          btn.disabled = true;
          btn.textContent = t('console.escalating', getLang());
          Promise.resolve(onEscalate && onEscalate({ turnId, question })).finally(() => {
            btn.disabled = false;
            btn.textContent = t('console.escalate', getLang());
          });
        });
        actions.append(btn);
      }
    }

    function addToolCall({ tool, verdict, explanation }) {
      const li = el('li', `console__activity-item console__activity-item--${verdict || 'allow'}`);
      li.dataset.tool = tool;
      li.textContent = `${tool} — ${explanation || verdict || ''}`;
      activity.append(li);
      scrollToEnd();
      return li;
    }

    function addToolResult({ tool, status }) {
      const li = activity.querySelector(`[data-tool="${CSS.escape(tool)}"]:last-of-type`);
      const tag = el('span', `console__activity-status console__activity-status--${status}`, { text: status });
      (li || activity).append(tag);
      scrollToEnd();
    }

    function done() {
      settled = true;
      cursor.remove();
      if (pendingTurnId === turnId) {
        pendingTurnId = null;
        stopBtn.hidden = true;
      }
      announce(fullText || t('console.cancelled', getLang()));
    }

    function fail(message) {
      settled = true;
      cursor.remove();
      turn.classList.add('console__turn--error');
      const err = el('div', 'console__error', { role: 'alert', text: message });
      turn.append(err);
      if (pendingTurnId === turnId) {
        pendingTurnId = null;
        stopBtn.hidden = true;
      }
      scrollToEnd();
    }

    function cancel() {
      if (settled) return;
      settled = true;
      cursor.remove();
      turn.classList.add('console__turn--cancelled');
      body.append(document.createTextNode(fullText ? '' : ` [${t('console.cancelled', getLang())}]`));
      const tag = el('span', 'console__cancelled-tag', { text: t('console.cancelled', getLang()) });
      line.append(tag);
      if (pendingTurnId === turnId) {
        pendingTurnId = null;
        stopBtn.hidden = true;
      }
      scrollToEnd();
    }

    return {
      appendDelta, setTier, done, fail, cancel, addToolCall, addToolResult,
      get text() { return fullText; },
      get tier() { return tierData; },
    };
  }

  function setMicState(state, label) {
    micBtn.dataset.state = state;
    micBtn.setAttribute('aria-pressed', String(state === 'active'));
    if (label) micBtn.title = label;
  }

  function submitLine(raw) {
    const text = raw.trim();
    if (!text) return;
    history.push(text);
    saveHistory(history);
    historyCursor = history.length;
    draft = '';
    input.value = '';
    onSubmit(text);
  }

  form.addEventListener('submit', (ev) => {
    ev.preventDefault();
    submitLine(input.value);
  });

  input.addEventListener('keydown', (ev) => {
    if (ev.key === 'ArrowUp') {
      if (history.length === 0) return;
      ev.preventDefault();
      if (historyCursor === history.length) draft = input.value;
      historyCursor = Math.max(0, historyCursor - 1);
      input.value = history[historyCursor] ?? '';
      // move caret to end
      requestAnimationFrame(() => input.setSelectionRange(input.value.length, input.value.length));
    } else if (ev.key === 'ArrowDown') {
      if (history.length === 0) return;
      ev.preventDefault();
      historyCursor = Math.min(history.length, historyCursor + 1);
      input.value = historyCursor === history.length ? draft : history[historyCursor];
    } else if (ev.key === 'Escape') {
      if (!stopBtn.hidden) {
        ev.preventDefault();
        onInterrupt && onInterrupt();
      }
    }
  });

  stopBtn.addEventListener('click', () => onInterrupt && onInterrupt());

  // The microphone is not the browser's. The desktop shell is a WKWebView, which does
  // not implement getUserMedia at all — asking for it here could only ever fail. Capture
  // happens in the gateway, in Python, where the ASR and the speaker already live, and
  // the audio never crosses this boundary in either direction. This button is a press.
  let listening = false;
  micBtn.addEventListener('click', () => {
    if (!onListen) return;
    listening = !listening;
    setMicState(listening ? 'listening' : 'idle');
    onListen(listening);
  });

  function retranslate() {
    input.placeholder = t('console.placeholder', getLang());
    inputLabel.textContent = t('console.input_label', getLang());
    help.textContent = t('console.help', getLang());
    sendBtn.textContent = t('console.send', getLang());
    micBtn.title = t('console.mic', getLang());
    micBtn.setAttribute('aria-label', t('console.mic', getLang()));
    stopBtn.title = t('console.interrupt', getLang());
    stopBtn.setAttribute('aria-label', t('console.interrupt', getLang()));
  }

  function clear() {
    log.innerHTML = '';
  }

  function focusInput() {
    input.focus();
  }

  return {
    appendSystemLine,
    appendUserText,
    beginReply,
    setMicState,
    setListening(on) {
      listening = !!on;
      setMicState(on ? 'listening' : 'idle');
    },
    retranslate,
    clear,
    focusInput,
    announce,
    get pendingTurnId() { return pendingTurnId; },
  };
}
