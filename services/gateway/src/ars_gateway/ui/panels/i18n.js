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
  // 'Vitalwerte' does not fit a sixth tab in a 340px strip; 'Werte' is what a
  // German speaker calls their own readings anyway. The panel title stays full.
  'deck.tab.vitals': { en: 'Vitals', ro: 'Vitale', de: 'Werte' },
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

  // ------------------------------------------------------------------ vitals
  // Wording here is load-bearing, not cosmetic. `packages/protocol/.../health.py`
  // says what A.R.S is allowed to say about a body: this is your number, this is
  // the range your reference gives, this one is outside it. Nothing on this panel
  // may read as a judgement about the person — no diagnosis, no condition, no
  // risk, in any of the three languages. "outside the cited range" is a fact
  // about two numbers; "high blood pressure" would be a claim about Alex.
  'vitals.title': { en: 'Vitals', ro: 'Semne vitale', de: 'Vitalwerte' },
  'vitals.intro': {
    en: 'Your own readings, kept on this machine. A.R.S can say a number sits outside a range and name whose range it is. What that means for you it cannot say — ask a clinician.',
    ro: 'Măsurătorile tale, păstrate pe această mașină. A.R.S poate spune că un număr se află în afara unui interval și poate numi sursa acelui interval. Ce înseamnă asta pentru tine nu poate spune — întreabă un medic.',
    de: 'Deine eigenen Messwerte, auf diesem Rechner gespeichert. A.R.S kann sagen, dass ein Wert außerhalb eines Bereichs liegt, und nennen, wessen Bereich das ist. Was das für dich bedeutet, kann es nicht sagen — frag eine Ärztin oder einen Arzt.' },
  'vitals.window.label': { en: 'Window', ro: 'Perioadă', de: 'Zeitraum' },
  'vitals.window.7': { en: '7 days', ro: '7 zile', de: '7 Tage' },
  'vitals.window.30': { en: '30 days', ro: '30 de zile', de: '30 Tage' },
  'vitals.window.90': { en: '90 days', ro: '90 de zile', de: '90 Tage' },
  'vitals.window.7.short': { en: '7d', ro: '7z', de: '7T' },
  'vitals.window.30.short': { en: '30d', ro: '30z', de: '30T' },
  'vitals.window.90.short': { en: '90d', ro: '90z', de: '90T' },
  'vitals.summary': {
    en: '{readings} readings · {kinds} measures',
    ro: '{readings} măsurători · {kinds} mărimi',
    de: '{readings} Messwerte · {kinds} Messgrößen' },
  'vitals.empty': {
    en: 'Nothing recorded in this window. Add a reading below — a number you type is a first-class reading.',
    ro: 'Nimic înregistrat în această perioadă. Adaugă o măsurătoare mai jos — un număr introdus de tine este o măsurătoare de rang întâi.',
    de: 'In diesem Zeitraum ist nichts erfasst. Trag unten einen Messwert ein — ein von dir eingegebener Wert ist ein vollwertiger Messwert.' },
  'vitals.loading': { en: 'Reading the store…', ro: 'Se citește depozitul…', de: 'Speicher wird gelesen…' },
  'vitals.unreachable': { en: 'The gateway did not answer — nothing loaded.', ro: 'Gateway-ul nu a răspuns — nu s-a încărcat nimic.', de: 'Das Gateway hat nicht geantwortet — nichts geladen.' },

  // the ten kinds the protocol defines, spelled the way a person says them
  'vitals.kind.heart_rate': { en: 'Heart rate', ro: 'Puls', de: 'Herzfrequenz' },
  'vitals.kind.bp_systolic': { en: 'Blood pressure · systolic', ro: 'Tensiune · sistolică', de: 'Blutdruck · systolisch' },
  'vitals.kind.bp_diastolic': { en: 'Blood pressure · diastolic', ro: 'Tensiune · diastolică', de: 'Blutdruck · diastolisch' },
  'vitals.kind.spo2': { en: 'Oxygen saturation', ro: 'Saturația oxigenului', de: 'Sauerstoffsättigung' },
  'vitals.kind.body_temperature': { en: 'Body temperature', ro: 'Temperatura corpului', de: 'Körpertemperatur' },
  'vitals.kind.blood_glucose': { en: 'Blood glucose', ro: 'Glicemie', de: 'Blutzucker' },
  'vitals.kind.weight': { en: 'Weight', ro: 'Greutate', de: 'Gewicht' },
  'vitals.kind.respiratory_rate': { en: 'Breathing rate', ro: 'Frecvență respiratorie', de: 'Atemfrequenz' },
  'vitals.kind.steps': { en: 'Steps', ro: 'Pași', de: 'Schritte' },
  'vitals.kind.sleep_minutes': { en: 'Sleep', ro: 'Somn', de: 'Schlaf' },

  // Units. mmHg, %, °C, mmol/L and kg are the same symbol in all three; the
  // word-shaped ones are not, and the store's unit string is English.
  'vitals.unit.heart_rate': { en: 'bpm', ro: 'bpm', de: 'S/min' },
  'vitals.unit.bp_systolic': { en: 'mmHg', ro: 'mmHg', de: 'mmHg' },
  'vitals.unit.bp_diastolic': { en: 'mmHg', ro: 'mmHg', de: 'mmHg' },
  'vitals.unit.spo2': { en: '%', ro: '%', de: '%' },
  'vitals.unit.body_temperature': { en: '°C', ro: '°C', de: '°C' },
  'vitals.unit.blood_glucose': { en: 'mmol/L', ro: 'mmol/L', de: 'mmol/L' },
  'vitals.unit.weight': { en: 'kg', ro: 'kg', de: 'kg' },
  'vitals.unit.respiratory_rate': { en: 'breaths/min', ro: 'resp/min', de: 'Atemzüge/min' },
  'vitals.unit.steps': { en: 'steps', ro: 'pași', de: 'Schritte' },
  'vitals.unit.sleep_minutes': { en: 'min', ro: 'min', de: 'min' },

  // Trend is a description of the numbers, never "better" or "worse" — whether a
  // rising number is welcome depends on which measure it is, and that is exactly
  // the judgement this subsystem may not make.
  'vitals.trend.rising': { en: 'rising', ro: 'în creștere', de: 'steigend' },
  'vitals.trend.falling': { en: 'falling', ro: 'în scădere', de: 'fallend' },
  'vitals.trend.stable': { en: 'level', ro: 'constant', de: 'gleichbleibend' },
  'vitals.trend.unknown': { en: 'too few to say', ro: 'prea puține date', de: 'zu wenige Werte' },
  'vitals.trend.explain': {
    en: 'The second half of the window compared with the first. A description of the numbers, nothing more.',
    ro: 'A doua jumătate a perioadei comparată cu prima. O descriere a numerelor, nimic mai mult.',
    de: 'Die zweite Hälfte des Zeitraums verglichen mit der ersten. Eine Beschreibung der Zahlen, mehr nicht.' },

  'vitals.latest': { en: 'Latest', ro: 'Ultima', de: 'Zuletzt' },
  'vitals.stat.mean': { en: 'avg {v}', ro: 'medie {v}', de: 'Ø {v}' },
  'vitals.stat.span': { en: 'low {min} · high {max}', ro: 'min. {min} · max. {max}', de: 'min. {min} · max. {max}' },
  'vitals.stat.count': { en: '{n} readings', ro: '{n} măsurători', de: '{n} Messwerte' },
  'vitals.stat.count_one': { en: '1 reading', ro: '1 măsurătoare', de: '1 Messwert' },
  'vitals.band.word': { en: 'cited', ro: 'citat', de: 'zitiert' },

  'vitals.today': { en: 'today', ro: 'azi', de: 'heute' },
  'vitals.yesterday': { en: 'yesterday', ro: 'ieri', de: 'gestern' },

  'vitals.outside.title': { en: 'Outside a cited range', ro: 'În afara unui interval citat', de: 'Außerhalb eines zitierten Bereichs' },
  'vitals.outside.count': { en: '{n} of {total} readings', ro: '{n} din {total} măsurători', de: '{n} von {total} Messwerten' },
  'vitals.outside.none': {
    en: 'Every reading here sits inside the ranges A.R.S has a source for.',
    ro: 'Toate măsurătorile de aici se află în intervalele pentru care A.R.S are o sursă.',
    de: 'Alle Messwerte hier liegen innerhalb der Bereiche, für die A.R.S eine Quelle hat.' },
  'vitals.outside.disclaimer': {
    en: 'A comparison with a published range — not a medical interpretation.',
    ro: 'O comparație cu un interval publicat — nu o interpretare medicală.',
    de: 'Ein Vergleich mit einem veröffentlichten Bereich — keine medizinische Deutung.' },
  'vitals.outside.above': { en: '{n} above {low}–{high} {unit}', ro: '{n} peste {low}–{high} {unit}', de: '{n} über {low}–{high} {unit}' },
  'vitals.outside.below': { en: '{n} below {low}–{high} {unit}', ro: '{n} sub {low}–{high} {unit}', de: '{n} unter {low}–{high} {unit}' },
  'vitals.outside.source': { en: 'Range published by {source}', ro: 'Interval publicat de {source}', de: 'Bereich veröffentlicht von {source}' },
  'vitals.outside.mark.above': { en: 'above the cited range', ro: 'peste intervalul citat', de: 'über dem zitierten Bereich' },
  'vitals.outside.mark.below': { en: 'below the cited range', ro: 'sub intervalul citat', de: 'unter dem zitierten Bereich' },
  'vitals.outside.more': { en: 'Show all {n}', ro: 'Arată toate cele {n}', de: 'Alle {n} anzeigen' },
  'vitals.outside.less': { en: 'Show fewer', ro: 'Arată mai puține', de: 'Weniger anzeigen' },

  'vitals.rows.show': { en: 'All {n} readings', ro: 'Toate cele {n} măsurători', de: 'Alle {n} Messwerte' },
  'vitals.rows.show_one': { en: 'The one reading', ro: 'Singura măsurătoare', de: 'Der eine Messwert' },
  'vitals.rows.hide': { en: 'Hide readings', ro: 'Ascunde măsurătorile', de: 'Messwerte ausblenden' },
  'vitals.source.manual': { en: 'typed in', ro: 'introdusă manual', de: 'von Hand' },
  'vitals.source.device': { en: 'from {name}', ro: 'de la {name}', de: 'von {name}' },

  'vitals.add.title': { en: 'Add a reading', ro: 'Adaugă o măsurătoare', de: 'Messwert hinzufügen' },
  'vitals.add.hint': {
    en: 'A number you type is not a lesser source — it is how the reading from a clinic’s machine gets in here.',
    ro: 'Un număr introdus de tine nu este o sursă inferioară — așa ajunge aici valoarea de la aparatul din clinică.',
    de: 'Ein von dir eingegebener Wert ist keine schlechtere Quelle — so kommt der Wert vom Gerät der Praxis hier herein.' },
  'vitals.add.kind': { en: 'Measure', ro: 'Mărime', de: 'Messgröße' },
  'vitals.add.value': { en: 'Value', ro: 'Valoare', de: 'Wert' },
  'vitals.add.note': { en: 'Note (optional)', ro: 'Notă (opțional)', de: 'Notiz (optional)' },
  'vitals.add.note_ph': { en: 'in your own words — “after the run”', ro: 'în cuvintele tale — „după alergare”', de: 'in deinen Worten — „nach dem Laufen“' },
  'vitals.add.submit': { en: 'Record', ro: 'Înregistrează', de: 'Erfassen' },
  'vitals.add.saving': { en: 'Recording…', ro: 'Se înregistrează…', de: 'Wird erfasst…' },
  'vitals.add.saved': { en: 'Recorded {value} {unit}.', ro: 'S-a înregistrat {value} {unit}.', de: '{value} {unit} erfasst.' },
  'vitals.add.saved_outside': {
    en: 'Recorded {value} {unit} — outside the range published by {source}.',
    ro: 'S-a înregistrat {value} {unit} — în afara intervalului publicat de {source}.',
    de: '{value} {unit} erfasst — außerhalb des von {source} veröffentlichten Bereichs.' },
  'vitals.add.need_value': { en: 'Enter a number.', ro: 'Introdu un număr.', de: 'Gib eine Zahl ein.' },
  'vitals.add.refused': { en: 'The store refused it: {reason}', ro: 'Depozitul a refuzat-o: {reason}', de: 'Der Speicher hat ihn abgelehnt: {reason}' },
  'vitals.add.failed': { en: 'Could not record it — the gateway did not answer.', ro: 'Nu s-a putut înregistra — gateway-ul nu a răspuns.', de: 'Konnte nicht erfasst werden — das Gateway hat nicht geantwortet.' },

  'vitals.delete': { en: 'Delete', ro: 'Șterge', de: 'Löschen' },
  'vitals.delete.aria': { en: 'Delete the reading {value} {unit} from {when}', ro: 'Șterge măsurătoarea {value} {unit} din {when}', de: 'Messwert {value} {unit} vom {when} löschen' },
  'vitals.delete.ask': {
    en: 'Delete {value} {unit} from {when}? It leaves the store for good.',
    ro: 'Ștergi {value} {unit} din {when}? Dispare definitiv din depozit.',
    de: '{value} {unit} vom {when} löschen? Der Wert verlässt den Speicher endgültig.' },
  'vitals.delete.confirm': { en: 'Delete it', ro: 'Șterge-o', de: 'Ja, löschen' },
  'vitals.delete.cancel': { en: 'Keep', ro: 'Păstrează', de: 'Behalten' },
  'vitals.delete.done': { en: 'Deleted {value} {unit} from {when}.', ro: 'S-a șters {value} {unit} din {when}.', de: '{value} {unit} vom {when} gelöscht.' },
  'vitals.delete.failed': { en: 'Could not delete it — it is still in the store.', ro: 'Nu s-a putut șterge — este încă în depozit.', de: 'Konnte nicht gelöscht werden — er ist noch im Speicher.' },

  // A refused record and an empty record are different facts. The guard fronts all
  // three health endpoints; 428 means "nobody has decided yet", 403 means "decided,
  // no". Neither is "you have no readings", and showing the empty state for either
  // would be the panel lying about why the screen is blank.
  'vitals.blocked.title': { en: 'Permission needed', ro: 'Este nevoie de permisiune', de: 'Erlaubnis erforderlich' },
  'vitals.blocked.title_denied': { en: 'Refused', ro: 'Refuzat', de: 'Verweigert' },
  'vitals.blocked.lead': {
    en: 'A.R.S has not been given permission to show your health record. This is not an empty record — it is one you have not unlocked.',
    ro: 'A.R.S nu a primit permisiunea să îți arate dosarul de sănătate. Nu este un dosar gol — este unul pe care nu l-ai deblocat.',
    de: 'A.R.S hat keine Erlaubnis, deine Gesundheitsakte anzuzeigen. Sie ist nicht leer — sie ist nur nicht freigegeben.' },
  'vitals.blocked.lead_denied': {
    en: 'A.R.S was refused access to your health record. This is not an empty record — it is one it may not read.',
    ro: 'Accesul A.R.S la dosarul tău de sănătate a fost refuzat. Nu este un dosar gol — este unul pe care nu îl poate citi.',
    de: 'A.R.S wurde der Zugriff auf deine Gesundheitsakte verweigert. Sie ist nicht leer — sie darf nur nicht gelesen werden.' },
  'vitals.blocked.said': { en: 'The guard says:', ro: 'Paznicul spune:', de: 'Der Wächter sagt:' },
  'vitals.blocked.where': {
    en: 'Permissions live in the {tab} tab.',
    ro: 'Permisiunile se află în fila {tab}.',
    de: 'Berechtigungen findest du im Reiter {tab}.' },
  'vitals.blocked.open': { en: 'Open {tab}', ro: 'Deschide {tab}', de: '{tab} öffnen' },
  'vitals.blocked.retry': { en: 'Check again', ro: 'Verifică din nou', de: 'Erneut prüfen' },
  'vitals.blocked.write': {
    en: 'Not recorded — A.R.S has not been given permission to write to your health record.',
    ro: 'Nu s-a înregistrat — A.R.S nu a primit permisiunea să scrie în dosarul tău de sănătate.',
    de: 'Nicht erfasst — A.R.S hat keine Erlaubnis, in deine Gesundheitsakte zu schreiben.' },
  'vitals.blocked.write_denied': {
    en: 'Not recorded — A.R.S was refused permission to write to your health record.',
    ro: 'Nu s-a înregistrat — permisiunea de a scrie în dosarul tău de sănătate a fost refuzată.',
    de: 'Nicht erfasst — A.R.S wurde die Erlaubnis verweigert, in deine Gesundheitsakte zu schreiben.' },
  'vitals.blocked.delete': {
    en: 'Still in the store — A.R.S has not been given permission to change your health record.',
    ro: 'Este încă în depozit — A.R.S nu a primit permisiunea să modifice dosarul tău de sănătate.',
    de: 'Noch im Speicher — A.R.S hat keine Erlaubnis, deine Gesundheitsakte zu ändern.' },
  'vitals.blocked.delete_denied': {
    en: 'Still in the store — A.R.S was refused permission to change your health record.',
    ro: 'Este încă în depozit — permisiunea de a modifica dosarul tău de sănătate a fost refuzată.',
    de: 'Noch im Speicher — A.R.S wurde die Erlaubnis verweigert, deine Gesundheitsakte zu ändern.' },

  'vitals.chart.aria': {
    en: '{kind}: {n} readings over {days} days, between {min} and {max} {unit}, {trend}.',
    ro: '{kind}: {n} măsurători în {days} zile, între {min} și {max} {unit}, {trend}.',
    de: '{kind}: {n} Messwerte über {days} Tage, zwischen {min} und {max} {unit}, {trend}.' },

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
