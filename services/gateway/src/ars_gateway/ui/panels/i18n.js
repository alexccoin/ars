// A.R.S console — every string it can say, in English, Romanian and German.
//
// Every user-facing string in the shell lives here. Nothing is machine-translated: all
// three columns were written by hand, including diacritics (ă â î ș ț) and German
// orthography (ä ö ü ß). If you add a UI string, add every language in the same commit —
// CLAUDE.md rule #5. A shell that falls back to English for one language is a shell that
// tells that user they were an afterthought.
//
// Usage: import { t } from './i18n.js'; t('console.send', lang)

export const LANGS = ['en', 'ro', 'de'];

export const LANG_LABEL = { en: 'English', ro: 'Română', de: 'Deutsch' };

const STRINGS = {
  'app.title': { en: 'A.R.S — console', ro: 'A.R.S — consolă', de: 'A.R.S — Konsole' },
  'app.subtitle': { en: 'Autonomous Reasoning System', ro: 'Sistem Autonom de Raționament', de: 'Autonomes Denksystem' },
  'app.skip_to_console': { en: 'Skip to console input', ro: 'Sari la câmpul consolei', de: 'Zur Konsoleneingabe springen' },

  // connection / state
  'state.idle': { en: 'idle', ro: 'inactiv', de: 'bereit' },
  'state.listening': { en: 'listening', ro: 'ascultă', de: 'hört zu' },
  'state.transcribing': { en: 'transcribing', ro: 'transcrie', de: 'transkribiert' },
  'state.thinking': { en: 'thinking', ro: 'gândește', de: 'denkt nach' },
  'state.acting': { en: 'acting', ro: 'acționează', de: 'handelt' },
  'state.speaking': { en: 'speaking', ro: 'vorbește', de: 'spricht' },
  'state.waiting_for_consent': { en: 'waiting for consent', ro: 'așteaptă acordul', de: 'wartet auf Zustimmung' },
  'state.error': { en: 'error', ro: 'eroare', de: 'Fehler' },
  'conn.connecting': { en: 'connecting…', ro: 'se conectează…', de: 'verbindet…' },
  'conn.online': { en: 'connected', ro: 'conectat', de: 'verbunden' },
  'conn.offline': { en: 'offline — no connection to the gateway', ro: 'offline — fără conexiune la gateway', de: 'offline — keine Verbindung zum Gateway' },
  'conn.reconnecting': { en: 'reconnecting in {s}s…', ro: 'reconectare în {s}s…', de: 'neuer Verbindungsversuch in {s} s…' },
  'conn.network_offline': { en: 'no network — browser reports offline', ro: 'fără rețea — browserul raportează offline', de: 'kein Netz — der Browser meldet offline' },

  // console
  'console.placeholder': { en: 'Type a command or a question… (/ for commands)', ro: 'Scrie o comandă sau o întrebare… (/ pentru comenzi)', de: 'Befehl oder Frage eingeben… (/ für Befehle)' },
  'console.input_label': { en: 'Command console input', ro: 'Câmp de comandă al consolei', de: 'Eingabe der Befehlskonsole' },
  'console.send': { en: 'Send', ro: 'Trimite', de: 'Senden' },
  'console.mic': { en: 'Voice input', ro: 'Intrare vocală', de: 'Spracheingabe' },
  'console.mic_warming': {
    en: 'Loading the voice models — about ten seconds the first time, then instant.',
    ro: 'Se încarcă modelele vocale — circa zece secunde prima dată, apoi instant.', de: 'Sprachmodelle werden geladen — beim ersten Mal etwa zehn Sekunden, danach sofort.' },
  'console.mic_ready': {
    en: 'Listening. Speak — A.R.S answers out loud, in the language you use.',
    ro: 'Ascult. Vorbește — A.R.S răspunde cu voce tare, în limba în care vorbești.', de: 'Ich höre zu. Sprich — A.R.S antwortet laut, in deiner Sprache.' },
  'console.mic_off': { en: 'Microphone off.', ro: 'Microfon oprit.', de: 'Mikrofon aus.' },
  'console.mic_denied': { en: 'Microphone permission denied — use the text console below instead.', ro: 'Permisiunea de microfon a fost refuzată — folosește consola de text de mai jos.', de: 'Mikrofonzugriff verweigert — nutze stattdessen die Textkonsole unten.' },
  'console.you': { en: 'you', ro: 'tu', de: 'du' },
  'console.ars': { en: 'A.R.S', ro: 'A.R.S', de: 'A.R.S' },
  'console.system': { en: 'system', ro: 'sistem', de: 'System' },
  'console.cancelled': { en: 'cancelled', ro: 'anulat', de: 'abgebrochen' },
  'console.interrupt': { en: 'Stop / interrupt', ro: 'Oprește / întrerupe', de: 'Stopp / unterbrechen' },
  'console.escalate': { en: 'Not good enough → escalate', ro: 'Nu e suficient de bun → escaladează', de: 'Nicht gut genug → eskalieren' },
  'console.escalating': { en: 'Escalating to a higher tier…', ro: 'Se escaladează la un nivel superior…', de: 'Wird an eine höhere Stufe weitergegeben…' },
  'console.escalate_failed': { en: 'Escalation failed — try again.', ro: 'Escaladarea a eșuat — încearcă din nou.', de: 'Eskalation fehlgeschlagen — versuch es noch einmal.' },
  'console.unknown_command': { en: 'Unknown command: {cmd}. Try /status for a list.', ro: 'Comandă necunoscută: {cmd}. Încearcă /status pentru o listă.', de: 'Unbekannter Befehl: {cmd}. Probier /status für eine Liste.' },
  'console.cleared': { en: 'Console cleared.', ro: 'Consola a fost golită.', de: 'Konsole geleert.' },
  'console.lang_set': { en: 'Language set to {lang}.', ro: 'Limba a fost setată la {lang}.', de: 'Sprache auf {lang} gesetzt.' },
  'console.lang_usage': { en: 'Usage: /lang ro|en', ro: 'Utilizare: /lang ro|en', de: 'Verwendung: /lang ro|en|de' },
  'console.escalate_usage': { en: 'Nothing to escalate yet — ask a question first.', ro: 'Nu există nimic de escaladat — pune mai întâi o întrebare.', de: 'Noch nichts zu eskalieren — stell zuerst eine Frage.' },
  'console.no_network': { en: 'Not connected — your message was not sent.', ro: 'Neconectat — mesajul tău nu a fost trimis.', de: 'Nicht verbunden — deine Nachricht wurde nicht gesendet.' },
  'console.help': {
    en: 'Commands: /brain /docs /grants /audit /status /clear /lang ro|en|de /escalate',
    ro: 'Comenzi: /brain /docs /grants /audit /status /clear /lang ro|en|de /escalate', de: 'Befehle: /brain /docs /grants /audit /status /clear /lang ro|en|de /escalate' },

  // tiers
  'tier.recall': { en: 'recall', ro: 'reamintire', de: 'Erinnerung' },
  'tier.documents': { en: 'documents', ro: 'documente', de: 'Dokumente' },
  'tier.local model': { en: 'local model', ro: 'model local' },
  'tier.cloud model': { en: 'cloud model', ro: 'model cloud' },
  'tier.cpu_badge': { en: 'CPU only', ro: 'doar CPU', de: 'nur CPU' },
  'tier.gpu_badge': { en: 'GPU spent', ro: 'GPU folosit', de: 'GPU genutzt' },
  'tier.cloud_badge': { en: 'left device', ro: 'a părăsit dispozitivul', de: 'Gerät verlassen' },
  'tier.score': { en: 'match {score}', ro: 'potrivire {score}', de: 'Treffer {score}' },
  'tier.elapsed': { en: '{ms} ms', ro: '{ms} ms', de: '{ms} ms' },
  'tier.citations': { en: 'sources: {list}', ro: 'surse: {list}', de: 'Quellen: {list}' },
  'tier.escalated_from': { en: 'escalated from {tier}', ro: 'escaladat din {tier}', de: 'eskaliert von {tier}' },

  // status / gpu counter
  'status.gpu_avoided': { en: 'GPU turns avoided', ro: 'Rulaje GPU evitate', de: 'vermiedene GPU-Züge' },
  'status.gpu_avoided_of': { en: '{saved} of {total} turns avoided the GPU ({pct}%)', ro: '{saved} din {total} rulaje au evitat GPU ({pct}%)', de: '{saved} von {total} Zügen kamen ohne GPU aus ({pct} %)' },
  'status.title': { en: 'Status', ro: 'Stare', de: 'Status' },
  'status.model': { en: 'Model', ro: 'Model', de: 'Modell' },
  'status.backend': { en: 'Backend', ro: 'Backend', de: 'Backend' },
  'status.languages': { en: 'Languages', ro: 'Limbi', de: 'Sprachen' },
  'status.tiers': { en: 'Tier usage', ro: 'Utilizare pe niveluri', de: 'Nutzung der Stufen' },
  'status.total_turns': { en: 'Total turns', ro: 'Total rulaje', de: 'Züge insgesamt' },
  'status.refresh': { en: 'Refresh', ro: 'Reîmprospătează', de: 'Aktualisieren' },
  'status.unavailable': { en: 'Status unavailable — could not reach /api/status.', ro: 'Starea nu este disponibilă — /api/status nu răspunde.', de: 'Status nicht verfügbar — /api/status war nicht erreichbar.' },
  'status.loading': { en: 'Loading status…', ro: 'Se încarcă starea…', de: 'Status wird geladen…' },

  // documents panel
  'docs.title': { en: 'Documents', ro: 'Documente', de: 'Dokumente' },
  'docs.empty': { en: 'No documents learned yet. Drop a file anywhere, or use the picker below.', ro: 'Niciun document învățat încă. Trage un fișier oriunde, sau folosește selectorul de mai jos.', de: 'Noch keine Dokumente gelernt. Zieh eine Datei hierher oder nutze die Auswahl unten.' },
  'docs.pick': { en: 'Choose file…', ro: 'Alege fișier…', de: 'Datei wählen…' },
  'docs.accept_hint': { en: 'PDF, TXT, DOCX, MD', ro: 'PDF, TXT, DOCX, MD', de: 'PDF, TXT, DOCX, MD' },
  'docs.drop_hint': { en: 'Drop a document anywhere in the window to teach A.R.S', ro: 'Trage un document oriunde în fereastră pentru a-l învăța pe A.R.S', de: 'Zieh ein Dokument irgendwohin ins Fenster, um es A.R.S beizubringen' },
  'docs.drop_active': { en: 'Release to upload {name}', ro: 'Eliberează pentru a încărca {name}', de: 'Loslassen, um {name} hochzuladen' },
  'docs.stage.uploading': { en: 'uploading {pct}%', ro: 'se încarcă {pct}%', de: 'lädt hoch {pct} %' },
  'docs.stage.parsing': { en: 'parsing…', ro: 'se analizează…', de: 'wird gelesen…' },
  'docs.stage.chunking': { en: 'chunking…', ro: 'se împarte în fragmente…', de: 'wird zerlegt…' },
  'docs.stage.learned': { en: 'learned', ro: 'învățat', de: 'gelernt' },
  'docs.stage.error': { en: 'failed', ro: 'eșuat', de: 'fehlgeschlagen' },
  'docs.chunks': { en: '{n} chunks', ro: '{n} fragmente', de: '{n} Abschnitte' },
  'docs.chars': { en: '{n} chars', ro: '{n} caractere', de: '{n} Zeichen' },
  'docs.delete': { en: 'Delete', ro: 'Șterge', de: 'Löschen' },
  'docs.delete_confirm': { en: 'Forget "{name}"? This cannot be undone.', ro: 'Uită „{name}”? Această acțiune nu poate fi anulată.', de: '„{name}“ vergessen? Das lässt sich nicht rückgängig machen.' },
  'docs.delete_failed': { en: 'Could not delete {name}.', ro: 'Nu s-a putut șterge {name}.', de: '{name} konnte nicht gelöscht werden.' },
  'docs.unsupported_type': { en: '{name}: unsupported file type — PDF, TXT, DOCX or MD only.', ro: '{name}: tip de fișier nesuportat — doar PDF, TXT, DOCX sau MD.', de: '{name}: nicht unterstützter Dateityp — nur PDF, TXT, DOCX oder MD.' },
  'docs.upload_failed': { en: '{name}: upload failed — {reason}', ro: '{name}: încărcarea a eșuat — {reason}', de: '{name}: Hochladen fehlgeschlagen — {reason}' },
  'docs.offline': { en: 'Cannot learn documents while offline.', ro: 'Nu se pot învăța documente cât timp ești offline.', de: 'Offline können keine Dokumente gelernt werden.' },

  // grants panel
  'grants.title': { en: 'Permissions', ro: 'Permisiuni', de: 'Berechtigungen' },
  'grants.empty': { en: 'No standing permissions granted.', ro: 'Nicio permisiune permanentă acordată.', de: 'Keine dauerhaften Berechtigungen erteilt.' },
  'grants.capability': { en: 'Capability', ro: 'Capabilitate', de: 'Fähigkeit' },
  'grants.resource': { en: 'Scope', ro: 'Domeniu', de: 'Geltungsbereich' },
  'grants.confirm': { en: 'Confirmation', ro: 'Confirmare', de: 'Bestätigung' },
  'grants.revoke': { en: 'Revoke', ro: 'Revocă', de: 'Entziehen' },
  'grants.revoked': { en: '{cap} revoked.', ro: '{cap} a fost revocat.', de: '{cap} entzogen.' },
  'grants.revoke_failed': { en: 'Could not revoke {cap} — restored.', ro: 'Nu s-a putut revoca {cap} — a fost restaurat.', de: '{cap} konnte nicht entzogen werden — wiederhergestellt.' },
  'grants.source': { en: 'Granted via', ro: 'Acordat prin', de: 'Erteilt über' },
  'grants.expires': { en: 'Expires', ro: 'Expiră', de: 'Läuft ab' },
  'grants.never_expires': { en: 'never', ro: 'niciodată', de: 'nie' },

  // audit panel
  'audit.title': { en: 'Audit log', ro: 'Jurnal de audit', de: 'Prüfprotokoll' },
  'audit.empty': { en: 'No actions recorded yet.', ro: 'Nicio acțiune înregistrată încă.', de: 'Noch keine Aktionen aufgezeichnet.' },
  'audit.live': { en: 'live', ro: 'în direct', de: 'live' },
  'audit.verdict.allow': { en: 'allowed', ro: 'permis', de: 'erlaubt' },
  'audit.verdict.ask': { en: 'asked', ro: 'a întrebat', de: 'nachgefragt' },
  'audit.verdict.deny': { en: 'denied', ro: 'refuzat', de: 'verweigert' },
  'audit.tainted': { en: 'tainted turn', ro: 'rulaj compromis', de: 'kontaminierter Zug' },

  // consent
  'consent.title': { en: 'A.R.S needs your permission', ro: 'A.R.S are nevoie de acordul tău', de: 'A.R.S braucht deine Erlaubnis' },
  'consent.approve': { en: 'Approve', ro: 'Aprobă', de: 'Erlauben' },
  'consent.deny': { en: 'Deny', ro: 'Refuză', de: 'Ablehnen' },
  'consent.approve_remember': { en: 'Approve and remember', ro: 'Aprobă și reține', de: 'Erlauben und merken' },
  'consent.capability': { en: 'Capability', ro: 'Capabilitate', de: 'Fähigkeit' },
  'consent.resource': { en: 'Resource', ro: 'Resursă', de: 'Ressource' },
  'consent.reason': { en: 'Reason given', ro: 'Motiv dat', de: 'Angegebener Grund' },

  // panel chrome
  'panel.collapse': { en: 'Collapse {title}', ro: 'Restrânge {title}', de: '{title} einklappen' },
  'panel.expand': { en: 'Expand {title}', ro: 'Extinde {title}', de: '{title} ausklappen' },

  // panel deck — the side column is a selector now, not a stack
  'deck.aria': { en: 'Panel selector', ro: 'Selector de panouri', de: 'Bereichsauswahl' },
  'deck.tab.brain': { en: 'Brain', ro: 'Creier', de: 'Gehirn' },
  'deck.tab.documents': { en: 'Docs', ro: 'Docs', de: 'Docs' },
  'deck.tab.grants': { en: 'Access', ro: 'Acces', de: 'Zugriff' },
  'deck.tab.audit': { en: 'Audit', ro: 'Audit', de: 'Audit' },
  'deck.tab.status': { en: 'System', ro: 'Sistem', de: 'System' },

  // brain network
  'brain.title': { en: 'Brain network', ro: 'Rețeaua creierului', de: 'Gehirnnetz' },
  'brain.empty': {
    en: 'Nothing learned yet. Drop a document anywhere in the window and watch the network grow.',
    ro: 'Nimic învățat încă. Trage un document oriunde în fereastră și privește cum crește rețeaua.',
    de: 'Noch nichts gelernt. Zieh ein Dokument irgendwohin ins Fenster und sieh zu, wie das Netz wächst.' },
  'brain.hint': {
    en: 'Drag to move · scroll to zoom · click a node to read it',
    ro: 'Trage pentru a muta · derulează pentru zoom · apasă un nod ca să-l citești',
    de: 'Ziehen zum Bewegen · Scrollen zum Zoomen · Knoten anklicken zum Lesen' },
  'brain.node.document': { en: 'document', ro: 'document', de: 'Dokument' },
  'brain.node.passage': { en: 'passage', ro: 'fragment', de: 'Abschnitt' },
  'brain.node.fact': { en: 'learned answer', ro: 'răspuns învățat', de: 'gelernte Antwort' },
  'brain.count.documents': { en: 'documents', ro: 'documente', de: 'Dokumente' },
  'brain.count.passages': { en: 'passages', ro: 'fragmente', de: 'Abschnitte' },
  'brain.count.facts': { en: 'answers', ro: 'răspunsuri', de: 'Antworten' },
  'brain.count.translations': { en: 'translations', ro: 'traduceri', de: 'Übersetzungen' },
  'brain.new': { en: '+{n} learned', ro: '+{n} învățate', de: '+{n} gelernt' },
  'brain.learned_one': { en: 'learned: {label}', ro: 'învățat: {label}', de: 'gelernt: {label}' },
  'brain.forgot': { en: 'forgotten: {n}', ro: 'uitate: {n}', de: 'vergessen: {n}' },
  'brain.fit': { en: 'Fit', ro: 'Încadrează', de: 'Einpassen' },
  'brain.expand': { en: 'Expand', ro: 'Extinde', de: 'Vergrößern' },
  'brain.close': { en: 'Close', ro: 'Închide', de: 'Schließen' },
  'brain.unavailable': {
    en: 'This browser has no canvas — listing what A.R.S knows instead.',
    ro: 'Acest browser nu are canvas — se afișează o listă cu ceea ce știe A.R.S.',
    de: 'Dieser Browser hat kein Canvas — stattdessen eine Liste dessen, was A.R.S weiß.' },
  'brain.unreachable': {
    en: 'Could not reach /api/knowledge — the network shown may be stale.',
    ro: 'Nu s-a putut contacta /api/knowledge — rețeaua afișată poate fi învechită.',
    de: '/api/knowledge war nicht erreichbar — das gezeigte Netz kann veraltet sein.' },
  'brain.nothing_selected': {
    en: 'Click a node to see what it is.',
    ro: 'Apasă un nod ca să vezi ce este.',
    de: 'Klick einen Knoten an, um zu sehen, was er ist.' },
  'brain.in_document': { en: 'in {name}', ro: 'în {name}', de: 'in {name}' },
  'brain.translated_from': { en: 'translated from {lang}', ro: 'tradus din {lang}', de: 'übersetzt aus dem {lang}' },
  'brain.derived': { en: 'derived by A.R.S', ro: 'derivat de A.R.S', de: 'von A.R.S abgeleitet' },
  'brain.original': { en: 'as you gave it', ro: 'așa cum l-ai dat', de: 'so wie du es gegeben hast' },
  'brain.fact_origin': {
    en: 'A.R.S worked this out and kept it, so the same question costs nothing next time',
    ro: 'A.R.S a dedus asta și a păstrat-o, ca aceeași întrebare să nu mai coste nimic data viitoare',
    de: 'A.R.S hat das hergeleitet und behalten, damit dieselbe Frage beim nächsten Mal nichts kostet' },
  'brain.links': { en: 'Connections', ro: 'Conexiuni', de: 'Verbindungen' },
  'brain.language': { en: 'Language', ro: 'Limbă', de: 'Sprache' },
  'brain.isolate': { en: 'Isolate {name}', ro: 'Izolează {name}', de: '{name} isolieren' },
  'brain.show_all': { en: 'Show everything', ro: 'Arată tot', de: 'Alles anzeigen' },
  'brain.a11y': {
    en: '{total} things known: {documents} documents, {passages} passages, {facts} learned answers.',
    ro: '{total} lucruri cunoscute: {documents} documente, {passages} fragmente, {facts} răspunsuri învățate.',
    de: '{total} bekannte Dinge: {documents} Dokumente, {passages} Abschnitte, {facts} gelernte Antworten.' },

  // errors
  'error.model_unavailable': { en: 'The model is unavailable right now.', ro: 'Modelul nu este disponibil momentan.', de: 'Das Modell ist gerade nicht verfügbar.' },
  'error.generic': { en: 'Something went wrong: {message}', ro: 'Ceva nu a mers bine: {message}', de: 'Etwas ist schiefgelaufen: {message}' },
  'error.ws_failed': { en: 'Could not reach the gateway at {url}.', ro: 'Gateway-ul nu a putut fi contactat la {url}.', de: 'Das Gateway war unter {url} nicht erreichbar.' },
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
