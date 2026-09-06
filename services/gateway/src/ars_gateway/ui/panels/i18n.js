// A.R.S console — bilingual strings (English / Română).
//
// Every user-facing string in the shell lives here. Nothing is machine-translated: both
// columns were written by hand, including diacritics (ă â î ș ț). If you add a UI string,
// add both languages in the same commit — CLAUDE.md rule #5.
//
// Usage: import { t } from './i18n.js'; t('console.send', lang)

export const LANGS = ['en', 'ro'];

export const LANG_LABEL = { en: 'English', ro: 'Română' };

const STRINGS = {
  'app.title': { en: 'A.R.S — console', ro: 'A.R.S — consolă' },
  'app.subtitle': { en: 'Autonomous Reasoning System', ro: 'Sistem Autonom de Raționament' },
  'app.skip_to_console': { en: 'Skip to console input', ro: 'Sari la câmpul consolei' },

  // connection / state
  'state.idle': { en: 'idle', ro: 'inactiv' },
  'state.listening': { en: 'listening', ro: 'ascultă' },
  'state.transcribing': { en: 'transcribing', ro: 'transcrie' },
  'state.thinking': { en: 'thinking', ro: 'gândește' },
  'state.acting': { en: 'acting', ro: 'acționează' },
  'state.speaking': { en: 'speaking', ro: 'vorbește' },
  'state.waiting_for_consent': { en: 'waiting for consent', ro: 'așteaptă acordul' },
  'state.error': { en: 'error', ro: 'eroare' },
  'conn.connecting': { en: 'connecting…', ro: 'se conectează…' },
  'conn.online': { en: 'connected', ro: 'conectat' },
  'conn.offline': { en: 'offline — no connection to the gateway', ro: 'offline — fără conexiune la gateway' },
  'conn.reconnecting': { en: 'reconnecting in {s}s…', ro: 'reconectare în {s}s…' },
  'conn.network_offline': { en: 'no network — browser reports offline', ro: 'fără rețea — browserul raportează offline' },

  // console
  'console.placeholder': { en: 'Type a command or a question… (/ for commands)', ro: 'Scrie o comandă sau o întrebare… (/ pentru comenzi)' },
  'console.input_label': { en: 'Command console input', ro: 'Câmp de comandă al consolei' },
  'console.send': { en: 'Send', ro: 'Trimite' },
  'console.mic': { en: 'Voice input', ro: 'Intrare vocală' },
  'console.mic_warming': {
    en: 'Loading the voice models — about ten seconds the first time, then instant.',
    ro: 'Se încarcă modelele vocale — circa zece secunde prima dată, apoi instant.',
  },
  'console.mic_ready': {
    en: 'Listening. Speak — A.R.S answers out loud, in the language you use.',
    ro: 'Ascult. Vorbește — A.R.S răspunde cu voce tare, în limba în care vorbești.',
  },
  'console.mic_off': { en: 'Microphone off.', ro: 'Microfon oprit.' },
  'console.mic_denied': { en: 'Microphone permission denied — use the text console below instead.', ro: 'Permisiunea de microfon a fost refuzată — folosește consola de text de mai jos.' },
  'console.you': { en: 'you', ro: 'tu' },
  'console.ars': { en: 'A.R.S', ro: 'A.R.S' },
  'console.system': { en: 'system', ro: 'sistem' },
  'console.cancelled': { en: 'cancelled', ro: 'anulat' },
  'console.interrupt': { en: 'Stop / interrupt', ro: 'Oprește / întrerupe' },
  'console.escalate': { en: 'Not good enough → escalate', ro: 'Nu e suficient de bun → escaladează' },
  'console.escalating': { en: 'Escalating to a higher tier…', ro: 'Se escaladează la un nivel superior…' },
  'console.escalate_failed': { en: 'Escalation failed — try again.', ro: 'Escaladarea a eșuat — încearcă din nou.' },
  'console.unknown_command': { en: 'Unknown command: {cmd}. Try /status for a list.', ro: 'Comandă necunoscută: {cmd}. Încearcă /status pentru o listă.' },
  'console.cleared': { en: 'Console cleared.', ro: 'Consola a fost golită.' },
  'console.lang_set': { en: 'Language set to {lang}.', ro: 'Limba a fost setată la {lang}.' },
  'console.lang_usage': { en: 'Usage: /lang ro|en', ro: 'Utilizare: /lang ro|en' },
  'console.escalate_usage': { en: 'Nothing to escalate yet — ask a question first.', ro: 'Nu există nimic de escaladat — pune mai întâi o întrebare.' },
  'console.no_network': { en: 'Not connected — your message was not sent.', ro: 'Neconectat — mesajul tău nu a fost trimis.' },
  'console.help': {
    en: 'Commands: /docs /grants /audit /status /clear /lang ro|en /escalate',
    ro: 'Comenzi: /docs /grants /audit /status /clear /lang ro|en /escalate',
  },

  // tiers
  'tier.recall': { en: 'recall', ro: 'reamintire' },
  'tier.documents': { en: 'documents', ro: 'documente' },
  'tier.local model': { en: 'local model', ro: 'model local' },
  'tier.cloud model': { en: 'cloud model', ro: 'model cloud' },
  'tier.cpu_badge': { en: 'CPU only', ro: 'doar CPU' },
  'tier.gpu_badge': { en: 'GPU spent', ro: 'GPU folosit' },
  'tier.cloud_badge': { en: 'left device', ro: 'a părăsit dispozitivul' },
  'tier.score': { en: 'match {score}', ro: 'potrivire {score}' },
  'tier.elapsed': { en: '{ms} ms', ro: '{ms} ms' },
  'tier.citations': { en: 'sources: {list}', ro: 'surse: {list}' },
  'tier.escalated_from': { en: 'escalated from {tier}', ro: 'escaladat din {tier}' },

  // status / gpu counter
  'status.gpu_avoided': { en: 'GPU turns avoided', ro: 'Rulaje GPU evitate' },
  'status.gpu_avoided_of': { en: '{saved} of {total} turns avoided the GPU ({pct}%)', ro: '{saved} din {total} rulaje au evitat GPU ({pct}%)' },
  'status.title': { en: 'Status', ro: 'Stare' },
  'status.model': { en: 'Model', ro: 'Model' },
  'status.backend': { en: 'Backend', ro: 'Backend' },
  'status.languages': { en: 'Languages', ro: 'Limbi' },
  'status.tiers': { en: 'Tier usage', ro: 'Utilizare pe niveluri' },
  'status.total_turns': { en: 'Total turns', ro: 'Total rulaje' },
  'status.refresh': { en: 'Refresh', ro: 'Reîmprospătează' },
  'status.unavailable': { en: 'Status unavailable — could not reach /api/status.', ro: 'Starea nu este disponibilă — /api/status nu răspunde.' },
  'status.loading': { en: 'Loading status…', ro: 'Se încarcă starea…' },

  // documents panel
  'docs.title': { en: 'Documents', ro: 'Documente' },
  'docs.empty': { en: 'No documents learned yet. Drop a file anywhere, or use the picker below.', ro: 'Niciun document învățat încă. Trage un fișier oriunde, sau folosește selectorul de mai jos.' },
  'docs.pick': { en: 'Choose file…', ro: 'Alege fișier…' },
  'docs.accept_hint': { en: 'PDF, TXT, DOCX, MD', ro: 'PDF, TXT, DOCX, MD' },
  'docs.drop_hint': { en: 'Drop a document anywhere in the window to teach A.R.S', ro: 'Trage un document oriunde în fereastră pentru a-l învăța pe A.R.S' },
  'docs.drop_active': { en: 'Release to upload {name}', ro: 'Eliberează pentru a încărca {name}' },
  'docs.stage.uploading': { en: 'uploading {pct}%', ro: 'se încarcă {pct}%' },
  'docs.stage.parsing': { en: 'parsing…', ro: 'se analizează…' },
  'docs.stage.chunking': { en: 'chunking…', ro: 'se împarte în fragmente…' },
  'docs.stage.learned': { en: 'learned', ro: 'învățat' },
  'docs.stage.error': { en: 'failed', ro: 'eșuat' },
  'docs.chunks': { en: '{n} chunks', ro: '{n} fragmente' },
  'docs.chars': { en: '{n} chars', ro: '{n} caractere' },
  'docs.delete': { en: 'Delete', ro: 'Șterge' },
  'docs.delete_confirm': { en: 'Forget "{name}"? This cannot be undone.', ro: 'Uită „{name}”? Această acțiune nu poate fi anulată.' },
  'docs.delete_failed': { en: 'Could not delete {name}.', ro: 'Nu s-a putut șterge {name}.' },
  'docs.unsupported_type': { en: '{name}: unsupported file type — PDF, TXT, DOCX or MD only.', ro: '{name}: tip de fișier nesuportat — doar PDF, TXT, DOCX sau MD.' },
  'docs.upload_failed': { en: '{name}: upload failed — {reason}', ro: '{name}: încărcarea a eșuat — {reason}' },
  'docs.offline': { en: 'Cannot learn documents while offline.', ro: 'Nu se pot învăța documente cât timp ești offline.' },

  // grants panel
  'grants.title': { en: 'Permissions', ro: 'Permisiuni' },
  'grants.empty': { en: 'No standing permissions granted.', ro: 'Nicio permisiune permanentă acordată.' },
  'grants.capability': { en: 'Capability', ro: 'Capabilitate' },
  'grants.resource': { en: 'Scope', ro: 'Domeniu' },
  'grants.confirm': { en: 'Confirmation', ro: 'Confirmare' },
  'grants.revoke': { en: 'Revoke', ro: 'Revocă' },
  'grants.revoked': { en: '{cap} revoked.', ro: '{cap} a fost revocat.' },
  'grants.revoke_failed': { en: 'Could not revoke {cap} — restored.', ro: 'Nu s-a putut revoca {cap} — a fost restaurat.' },
  'grants.source': { en: 'Granted via', ro: 'Acordat prin' },
  'grants.expires': { en: 'Expires', ro: 'Expiră' },
  'grants.never_expires': { en: 'never', ro: 'niciodată' },

  // audit panel
  'audit.title': { en: 'Audit log', ro: 'Jurnal de audit' },
  'audit.empty': { en: 'No actions recorded yet.', ro: 'Nicio acțiune înregistrată încă.' },
  'audit.live': { en: 'live', ro: 'în direct' },
  'audit.verdict.allow': { en: 'allowed', ro: 'permis' },
  'audit.verdict.ask': { en: 'asked', ro: 'a întrebat' },
  'audit.verdict.deny': { en: 'denied', ro: 'refuzat' },
  'audit.tainted': { en: 'tainted turn', ro: 'rulaj compromis' },

  // consent
  'consent.title': { en: 'A.R.S needs your permission', ro: 'A.R.S are nevoie de acordul tău' },
  'consent.approve': { en: 'Approve', ro: 'Aprobă' },
  'consent.deny': { en: 'Deny', ro: 'Refuză' },
  'consent.approve_remember': { en: 'Approve and remember', ro: 'Aprobă și reține' },
  'consent.capability': { en: 'Capability', ro: 'Capabilitate' },
  'consent.resource': { en: 'Resource', ro: 'Resursă' },
  'consent.reason': { en: 'Reason given', ro: 'Motiv dat' },

  // panel chrome
  'panel.collapse': { en: 'Collapse {title}', ro: 'Restrânge {title}' },
  'panel.expand': { en: 'Expand {title}', ro: 'Extinde {title}' },

  // errors
  'error.model_unavailable': { en: 'The model is unavailable right now.', ro: 'Modelul nu este disponibil momentan.' },
  'error.generic': { en: 'Something went wrong: {message}', ro: 'Ceva nu a mers bine: {message}' },
  'error.ws_failed': { en: 'Could not reach the gateway at {url}.', ro: 'Gateway-ul nu a putut fi contactat la {url}.' },
};

export function t(key, lang, vars) {
  const entry = STRINGS[key];
  if (!entry) {
    console.warn(`[i18n] missing key "${key}"`);
    return key;
  }
  let text = entry[lang] || entry.en || key;
  if (vars) {
    for (const [k, v] of Object.entries(vars)) {
      text = text.replaceAll(`{${k}}`, String(v));
    }
  }
  return text;
}
