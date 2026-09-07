// A.R.S console — orchestrator.
//
// Wires the WebSocket protocol, the command console, the collapsible HUD panels, consent
// prompts, drag-and-drop document learning, and the tier readout together. Nothing here
// renders directly; it delegates to panels/*.js and to the artifact-engineer's entity/HUD
// modules when they exist.
//
// Contract this file codes against (see panels/hud-fallback.js for the documented fallback
// shape used until the real files land):
//   import { ArsEntity } from './entity/ars-entity.js';
//   import { panel, gauge, pip, ticker, tierIndicator } from './entity/hud.js';

import { t, LANGS } from './panels/i18n.js';
import { panel as fallbackPanel, gauge as fallbackGauge, pip as fallbackPip, ticker as fallbackTicker, tierIndicator as fallbackTierIndicator, ArsEntityFallback } from './panels/hud-fallback.js';
import { createConsole } from './panels/console.js';
import { createDeck } from './panels/deck.js';
import { createBrainPanel } from './panels/brain.js';
import { createDocumentsPanel } from './panels/documents.js';
import { createVitalsPanel } from './panels/vitals.js';
import { createGrantsPanel } from './panels/grants.js';
import { createAuditPanel } from './panels/audit.js';
import { createStatusPanel } from './panels/status.js';
import { createConsentQueue } from './panels/consent.js';

const LANG_KEY = 'ars.console.lang';
const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 15000;

// ---------------------------------------------------------------------- entity/HUD loading

async function loadEntityModule() {
  try {
    const mod = await import('./entity/ars-entity.js');
    if (mod && mod.ArsEntity) return { ArsEntity: mod.ArsEntity, isStub: false };
    throw new Error('entity/ars-entity.js loaded but did not export ArsEntity');
  } catch (err) {
    console.info('[ars] entity/ars-entity.js not available yet — using text/CSS fallback entity. This is the artifact-engineer\'s file.', err.message || err);
    return { ArsEntity: ArsEntityFallback, isStub: true };
  }
}

async function loadHudModule() {
  try {
    const mod = await import('./entity/hud.js');
    const required = ['panel', 'gauge', 'pip', 'ticker', 'tierIndicator'];
    const missing = required.filter((k) => typeof mod[k] !== 'function');
    if (missing.length) throw new Error(`entity/hud.js missing exports: ${missing.join(', ')}`);
    return { ...mod, isStub: false };
  } catch (err) {
    console.info('[ars] entity/hud.js not available yet — using plain-DOM HUD fallback. This is the artifact-engineer\'s file.', err.message || err);
    return {
      panel: fallbackPanel, gauge: fallbackGauge, pip: fallbackPip,
      ticker: fallbackTicker, tierIndicator: fallbackTierIndicator, isStub: true,
    };
  }
}

// ---------------------------------------------------------------------------------- state

const state = {
  lang: localStorage.getItem(LANG_KEY) || ((navigator.language || '').toLowerCase().startsWith('ro') ? 'ro' : 'en'),
  wsReady: false,
  networkOnline: navigator.onLine,
  reconnectDelay: RECONNECT_BASE_MS,
  session: { total: 0, saved: 0 },
  activeTurn: null, // { handle, question, turnId }
};
if (!LANGS.includes(state.lang)) state.lang = 'en';
function getLang() { return state.lang; }

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

// -------------------------------------------------------------------------------- banners

const bannerRegion = document.getElementById('banner-region');
const banners = new Map(); // key -> element

function setBanner(key, opts) {
  if (!opts || opts.text == null) {
    const existing = banners.get(key);
    if (existing) { existing.remove(); banners.delete(key); }
    return;
  }
  const { kind = 'info', text, dismissible = false } = opts;
  let banner = banners.get(key);
  if (!banner) {
    banner = el('div', `banner banner--${kind}`, { role: kind === 'offline' ? 'alert' : 'status' });
    bannerRegion.append(banner);
    banners.set(key, banner);
  }
  banner.className = `banner banner--${kind}`;
  banner.textContent = '';
  banner.append(el('span', null, { text }));
  if (dismissible) {
    const btn = el('button', 'banner__dismiss', { type: 'button', 'aria-label': 'Dismiss', text: '✕' });
    btn.addEventListener('click', () => setBanner(key, null));
    banner.append(btn);
  }
}

// ---------------------------------------------------------------------------------- main

async function main() {
  document.documentElement.lang = state.lang;
  document.getElementById('lang-select').value = state.lang;

  const [{ ArsEntity }, hud] = await Promise.all([loadEntityModule(), loadHudModule()]);

  const entity = new ArsEntity(document.getElementById('entity-mount'));

  const connPip = hud.pip({ id: 'conn-pip', state: 'connecting', label: t('conn.connecting', getLang()) });
  document.getElementById('conn-pip-mount').append(connPip.root);

  const statePip = hud.pip({ id: 'agent-state-pip', state: 'off', label: t('state.idle', getLang()) });
  document.getElementById('state-pip-mount').append(statePip.root);

  const gpuGauge = hud.gauge({ id: 'gpu-avoided-gauge', label: t('status.gpu_avoided', getLang()), value: 0 });
  document.getElementById('gpu-gauge-mount').append(gpuGauge.root);

  function updateGpuGauge() {
    const pct = state.session.total ? Math.round((100 * state.session.saved) / state.session.total) : 0;
    gpuGauge.setValue(pct);
    gpuGauge.root.title = t('status.gpu_avoided_of', getLang(), { saved: state.session.saved, total: state.session.total, pct });
  }

  // ------------------------------------------------------------------------------ panels

  // The side column is a deck of instruments now, not one long stack: one view
  // at a time, chosen from the tab strip. Each panel is constructed exactly as
  // before and still owns everything inside itself — the only change is which
  // element it mounts into.
  const panelsRoot = document.getElementById('panels-root');
  const deck = createDeck({ root: panelsRoot, getLang });

  const brainPanel = createBrainPanel({
    root: deck.view('brain', { labelKey: 'deck.tab.brain', titleKey: 'brain.title', icon: 'brain' }),
    hud, getLang, baseUrl: '',
  });
  const docsPanel = createDocumentsPanel({
    root: deck.view('documents', { labelKey: 'deck.tab.documents', titleKey: 'docs.title', icon: 'documents' }),
    hud, getLang, baseUrl: '',
  });
  const grantsPanel = createGrantsPanel({
    root: deck.view('grants', { labelKey: 'deck.tab.grants', titleKey: 'grants.title', icon: 'grants' }),
    hud, getLang, baseUrl: '',
  });
  const auditPanel = createAuditPanel({
    root: deck.view('audit', { labelKey: 'deck.tab.audit', titleKey: 'audit.title', icon: 'audit' }),
    hud, getLang, baseUrl: '',
  });
  const vitalsPanel = createVitalsPanel({
    root: deck.view('vitals', { labelKey: 'deck.tab.vitals', titleKey: 'vitals.title', icon: 'vitals' }),
    hud, getLang, baseUrl: '',
  });
  const statusPanel = createStatusPanel({
    root: deck.view('status', { labelKey: 'deck.tab.status', titleKey: 'status.title', icon: 'status' }),
    hud, getLang, baseUrl: '',
  });

  const allPanels = [brainPanel, docsPanel, grantsPanel, auditPanel, vitalsPanel, statusPanel];
  deck.retranslate();
  deck.restore();

  // What A.R.S knows is worth seeing even from another tab, so the counts ride
  // on the tabs themselves and ping when they change.
  brainPanel.setOnCounts((count, summary) => {
    deck.setBadge('brain', count);
    deck.setBadge('documents', summary.documents || 0);
  });

  // How many readings sit outside a range A.R.S has a source for. A count, not a
  // verdict — the tab says "look", the panel says whose range and nothing more.
  setInterval(() => deck.setBadge('vitals', vitalsPanel.outsideCount()), 4000);

  let auditCount = 0;

  function focusPanel(p) {
    const rootEl = p.panelRoot;
    deck.selectByChild(rootEl);
    const body = rootEl.querySelector('.hud-panel__body');
    if (body && body.hidden) {
      const toggle = rootEl.querySelector('.hud-panel__toggle');
      if (toggle) toggle.click();
    }
    rootEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    const heading = rootEl.querySelector('.hud-panel__title, h2');
    if (heading) { heading.setAttribute('tabindex', '-1'); heading.focus(); }
  }

  // ------------------------------------------------------------------------------- consent

  const consentQueue = createConsentQueue({
    root: document.getElementById('consent-root'),
    getLang,
    onRespond: ({ request_id, approved, remember }) => {
      sendMessage({ type: 'consent_response', request_id, approved, remember });
    },
  });

  // -------------------------------------------------------------------------------- ws

  let ws = null;
  let reconnectTimer = null;

  // A paired device follows a link carrying ?t=<token>. The gateway answers that request
  // with an HttpOnly cookie, which every later fetch sends automatically — but a
  // WebSocket handshake opened before that cookie lands would be refused, so the token is
  // carried explicitly on the socket URL for the first connection.
  const PAIRING_TOKEN = new URLSearchParams(location.search).get('t');

  function wsUrl() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const query = PAIRING_TOKEN ? `?t=${encodeURIComponent(PAIRING_TOKEN)}` : '';
    return `${proto}://${location.host}/ws${query}`;
  }

  function sendMessage(msg) {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      consoleUI.appendSystemLine(t('console.no_network', getLang()), 'warn');
      return false;
    }
    ws.send(JSON.stringify(msg));
    return true;
  }

  function connect() {
    clearTimeout(reconnectTimer);
    connPip.setState('connecting');
    connPip.setLabel(t('conn.connecting', getLang()));
    let socket;
    try {
      socket = new WebSocket(wsUrl());
    } catch (err) {
      scheduleReconnect();
      return;
    }
    ws = socket;

    socket.addEventListener('open', () => {
      state.wsReady = true;
      state.reconnectDelay = RECONNECT_BASE_MS;
      connPip.setState('online');
      connPip.setLabel(t('conn.online', getLang()));
      setBanner('offline', null);
    });

    socket.addEventListener('message', (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      handleServerEvent(msg);
    });

    socket.addEventListener('close', () => {
      state.wsReady = false;
      connPip.setState('offline');
      connPip.setLabel(t('conn.offline', getLang()));
      if (state.activeTurn) { state.activeTurn.handle.cancel(); state.activeTurn = null; }
      scheduleReconnect();
    });

    socket.addEventListener('error', () => {
      // 'close' fires right after in browsers; avoid double-scheduling by letting close handle it.
    });
  }

  function scheduleReconnect() {
    const seconds = Math.round(state.reconnectDelay / 1000);
    setBanner('offline', { kind: 'offline', text: t('conn.reconnecting', getLang(), { s: seconds }) });
    reconnectTimer = setTimeout(() => {
      state.reconnectDelay = Math.min(state.reconnectDelay * 1.6, RECONNECT_MAX_MS);
      connect();
    }, state.reconnectDelay);
  }

  // ------------------------------------------------------------------------- server events

  function bindTurn(turnId) {
    if (state.activeTurn && state.activeTurn.turnId == null) {
      state.activeTurn.turnId = turnId;
    }
    return state.activeTurn && (state.activeTurn.turnId === turnId || state.activeTurn.turnId == null)
      ? state.activeTurn
      : null;
  }

  function handleServerEvent(msg) {
    switch (msg.type) {
      case 'state': {
        const label = t(`state.${msg.state}`, getLang()) || msg.state;
        statePip.setState(mapAgentStateToPipState(msg.state));
        statePip.setLabel(label);
        entity.setState(msg.state);
        if (msg.turn_id) bindTurn(msg.turn_id);
        break;
      }
      case 'listening': {
        consoleUI.setListening(msg.on);
        if (msg.on && msg.warming) {
          consoleUI.appendSystemLine(t('console.mic_warming', getLang()), 'info');
        } else if (msg.on) {
          consoleUI.appendSystemLine(t('console.mic_ready', getLang()), 'info');
        } else {
          consoleUI.appendSystemLine(t('console.mic_off', getLang()), 'info');
        }
        break;
      }
      case 'transcript': {
        // Voice path isn't wired to this console yet (see console.js mic stub), but if a
        // transcript ever arrives — e.g. from another connected client — show it honestly,
        // partials included, rather than silently dropping it.
        if (msg.transcript && msg.transcript.text) {
          consoleUI.appendSystemLine(
            `${msg.transcript.is_final ? '' : '… '}${msg.transcript.text}`,
            msg.transcript.is_final ? 'info' : 'partial',
          );
        }
        break;
      }
      case 'tier': {
        const turn = state.activeTurn;
        if (turn) {
          turn.handle.setTier(msg);
          entity.setTier(msg.tier);
        }
        state.session.total += 1;
        if (msg.tier !== 'local model' && msg.tier !== 'cloud model') state.session.saved += 1;
        updateGpuGauge();
        break;
      }
      case 'reply_delta': {
        const turn = bindTurn(msg.turn_id);
        if (turn) turn.handle.appendDelta(msg.text);
        break;
      }
      case 'reply_done': {
        const turn = bindTurn(msg.turn_id);
        if (turn) {
          turn.handle.done();
          if (state.activeTurn === turn) state.activeTurn = null;
        }
        break;
      }
      case 'tool_call': {
        const turn = bindTurn(msg.turn_id);
        const verdict = msg.decision && msg.decision.verdict;
        if (turn) {
          turn.handle.addToolCall({
            tool: msg.call && msg.call.tool,
            verdict,
            explanation: msg.decision && msg.decision.explanation,
          });
        }
        auditCount += 1;
        deck.setBadge('audit', auditCount);
        auditPanel.pushRecord({
          id: msg.call && msg.call.id,
          at_ms: (msg.call && msg.call.requested_at_ms) || Date.now(),
          capability: msg.decision && msg.decision.capability,
          resource: msg.call && msg.call.tool,
          verdict,
          tainted: msg.decision && msg.decision.reason === 'tainted_turn',
        });
        break;
      }
      case 'tool_result': {
        const turn = bindTurn(msg.turn_id);
        if (turn) {
          turn.handle.addToolResult({ tool: msg.result && msg.result.tool, status: msg.result && msg.result.status });
        }
        break;
      }
      case 'consent_required': {
        statePip.setState('warn');
        statePip.setLabel(t('state.waiting_for_consent', getLang()));
        entity.setState('waiting_for_consent');
        consentQueue.show(msg);
        break;
      }
      case 'error': {
        const turn = msg.turn_id ? bindTurn(msg.turn_id) : state.activeTurn;
        const text = msg.code === 'model_unavailable'
          ? t('error.model_unavailable', getLang())
          : t('error.generic', getLang(), { message: msg.message || msg.code });
        if (turn) {
          turn.handle.fail(text);
          if (state.activeTurn === turn) state.activeTurn = null;
        } else {
          consoleUI.appendSystemLine(text, 'error');
        }
        if (!msg.recoverable) setBanner('fatal', { kind: 'offline', text, dismissible: true });
        break;
      }
      case 'document_learned': {
        docsPanel.handleDocumentLearned(msg);
        // Do not wait for the poll: the graph should visibly grow the moment
        // A.R.S says it learned something.
        brainPanel.refresh();
        break;
      }
      default:
        // Unhandled protocol events (session_started, wake, vad, audio_out,
        // grant_changed, observation_proposed) — not part of this text console's scope.
        break;
    }
  }

  function mapAgentStateToPipState(s) {
    switch (s) {
      case 'idle': return 'off';
      case 'error': return 'error';
      case 'waiting_for_consent': return 'warn';
      default: return 'active';
    }
  }

  // ---------------------------------------------------------------------------- console

  const consoleUI = createConsole({
    root: document.getElementById('console-root'),
    getLang,
    hud,
    onSubmit: handleInput,
    onListen: (on) => sendMessage({ type: 'listen', on }),
    onInterrupt: () => {
      if (state.activeTurn) {
        state.activeTurn.handle.cancel();
        state.activeTurn = null;
      }
      sendMessage({ type: 'interrupt', reason: 'user_cancel' });
    },
    onEscalate: async ({ question }) => {
      if (!question) {
        consoleUI.appendSystemLine(t('console.escalate_usage', getLang()), 'warn');
        return;
      }
      await escalate(question);
    },
  });

  function askQuestion(text) {
    state.lastQuestion = text;
    consoleUI.appendUserText(text);
    const localId = `local-${Date.now()}`;
    const handle = consoleUI.beginReply(localId, { question: text });
    state.activeTurn = { handle, question: text, turnId: null };
    const sent = sendMessage({ type: 'text', text, language: getLang() === 'ro' ? 'ro' : 'en' });
    if (!sent) {
      handle.fail(t('console.no_network', getLang()));
      state.activeTurn = null;
    }
  }

  async function escalate(question) {
    consoleUI.appendSystemLine(`/escalate: ${question}`, 'info');
    const localId = `local-escalate-${Date.now()}`;
    const handle = consoleUI.beginReply(localId, { question });
    try {
      const res = await fetch('/api/escalate', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ question }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const answer = await res.json();
      handle.appendDelta(answer.text || '');
      handle.setTier(answer);
      handle.done();
    } catch (err) {
      handle.fail(t('console.escalate_failed', getLang()));
    }
  }

  function handleInput(raw) {
    if (raw.startsWith('/')) {
      runCommand(raw);
    } else {
      askQuestion(raw);
    }
  }

  function lastEscalatableQuestion() {
    // The active turn if one is open, otherwise nothing — the escalate button on each
    // past turn already exists inline; /escalate is a shortcut for "the last thing I asked".
    return state.lastQuestion || null;
  }

  function runCommand(raw) {
    const [cmdRaw, ...rest] = raw.trim().split(/\s+/);
    const cmd = cmdRaw.toLowerCase();
    switch (cmd) {
      case '/brain':
      case '/network':
        focusPanel(brainPanel);
        brainPanel.refresh();
        consoleUI.appendSystemLine(t('brain.title', getLang()), 'info');
        break;
      case '/docs':
        focusPanel(docsPanel);
        consoleUI.appendSystemLine(t('docs.title', getLang()), 'info');
        break;
      case '/grants':
        focusPanel(grantsPanel);
        consoleUI.appendSystemLine(t('grants.title', getLang()), 'info');
        break;
      case '/audit':
        focusPanel(auditPanel);
        consoleUI.appendSystemLine(t('audit.title', getLang()), 'info');
        break;
      case '/status':
        focusPanel(statusPanel);
        statusPanel.refresh();
        break;
      case '/clear':
        consoleUI.clear();
        consoleUI.appendSystemLine(t('console.cleared', getLang()), 'info');
        break;
      case '/lang': {
        const target = (rest[0] || '').toLowerCase();
        if (!LANGS.includes(target)) {
          consoleUI.appendSystemLine(t('console.lang_usage', getLang()), 'warn');
          break;
        }
        setLang(target);
        consoleUI.appendSystemLine(t('console.lang_set', getLang(), { lang: target }), 'info');
        break;
      }
      case '/escalate': {
        const q = lastEscalatableQuestion();
        if (!q) {
          consoleUI.appendSystemLine(t('console.escalate_usage', getLang()), 'warn');
          break;
        }
        escalate(q);
        break;
      }
      default:
        consoleUI.appendSystemLine(t('console.unknown_command', getLang(), { cmd: cmdRaw }), 'warn');
    }
  }

  // -------------------------------------------------------------------------- language

  function setLang(lang) {
    state.lang = lang;
    localStorage.setItem(LANG_KEY, lang);
    document.documentElement.lang = lang;
    document.getElementById('lang-select').value = lang;
    document.getElementById('app-subtitle').textContent = t('app.subtitle', lang);
    document.querySelector('.skip-link').textContent = t('app.skip_to_console', lang);
    connPip.setLabel(state.wsReady ? t('conn.online', lang) : t('conn.offline', lang));
    updateGpuGauge();
    consoleUI.retranslate();
    deck.retranslate();
    for (const p of allPanels) p.retranslate();
  }

  document.getElementById('lang-select').addEventListener('change', (ev) => setLang(ev.target.value));
  setLang(state.lang);

  // ----------------------------------------------------------------------- drag & drop

  const dropzone = document.getElementById('dropzone-overlay');
  let dragDepth = 0;

  function hasFiles(ev) {
    return ev.dataTransfer && Array.from(ev.dataTransfer.types || []).includes('Files');
  }

  window.addEventListener('dragenter', (ev) => {
    if (!hasFiles(ev)) return;
    ev.preventDefault();
    dragDepth += 1;
    dropzone.hidden = false;
    dropzone.textContent = t('docs.drop_hint', getLang());
  });
  window.addEventListener('dragover', (ev) => { if (hasFiles(ev)) ev.preventDefault(); });
  window.addEventListener('dragleave', (ev) => {
    if (!hasFiles(ev)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) dropzone.hidden = true;
  });
  window.addEventListener('drop', (ev) => {
    if (!hasFiles(ev)) return;
    ev.preventDefault();
    dragDepth = 0;
    dropzone.hidden = true;
    for (const file of ev.dataTransfer.files) docsPanel.ingest(file);
    // The gateway does not announce a learned document on the socket, so the
    // network would otherwise wait for its next poll. Nudge it instead.
    for (const delay of [1200, 3000, 6000]) setTimeout(() => brainPanel.refresh(), delay);
  });

  // ------------------------------------------------------------------------- network

  function updateOfflineBanner() {
    if (!navigator.onLine) {
      setBanner('network', { kind: 'offline', text: t('conn.network_offline', getLang()) });
      docsPanel.setOffline(true);
      vitalsPanel.setOffline(true);
      // Stop polling /api/knowledge rather than logging a failure every few
      // seconds; the graph on screen stays, it just cannot grow.
      brainPanel.setOffline(true);
    } else {
      setBanner('network', null);
      docsPanel.setOffline(false);
      vitalsPanel.setOffline(false);
      brainPanel.setOffline(false);
      brainPanel.refresh();
    }
  }
  window.addEventListener('online', updateOfflineBanner);
  window.addEventListener('offline', updateOfflineBanner);
  updateOfflineBanner();

  // --------------------------------------------------------------------------- go

  consoleUI.appendSystemLine(t('console.help', getLang()), 'info');
  consoleUI.focusInput();
  connect();
}

main().catch((err) => {
  console.error('[ars] fatal init error', err);
  const region = document.getElementById('banner-region');
  const banner = document.createElement('div');
  banner.className = 'banner banner--offline';
  banner.textContent = `A.R.S console failed to start: ${err.message || err}`;
  region.append(banner);
});
