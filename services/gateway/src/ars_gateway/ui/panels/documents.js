// Documents library — upload (drag-and-drop or picker), real progress, delete.
//
// Uploads go through XMLHttpRequest (not fetch) specifically so we get real upload
// progress events for the "uploading -> parsing -> chunking -> learned" state machine
// the brief asks for. The request itself is async I/O handled by the browser network
// stack, so a 200-page PDF never touches the JS main thread — the only main-thread work
// per file is building a FormData and updating a progress bar.

import { t } from './i18n.js';

const ACCEPTED_EXT = ['pdf', 'txt', 'docx', 'md'];

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

function extOf(name) {
  const m = /\.([a-z0-9]+)$/i.exec(name || '');
  return m ? m[1].toLowerCase() : '';
}

function formatBytes(n) {
  if (!n && n !== 0) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export function createDocumentsPanel({ root, hud, getLang, baseUrl = '' }) {
  let panelHandle;
  if (hud && typeof hud.panel === 'function') {
    try {
      panelHandle = hud.panel({ id: 'panel-documents', title: t('docs.title', getLang()) });
    } catch (err) {
      console.warn('[documents] hud.panel failed, using plain container', err);
    }
  }
  if (!panelHandle) {
    const container = el('section', 'hud-panel', { id: 'panel-documents', 'aria-label': t('docs.title', getLang()) });
    const header = el('div', 'hud-panel__header');
    header.append(el('h2', 'hud-panel__title', { text: t('docs.title', getLang()) }));
    const body = el('div', 'hud-panel__body');
    container.append(header, body);
    panelHandle = { root: container, body, setTitle(text) { header.firstChild.textContent = text; } };
  }
  root.append(panelHandle.root);

  const hint = el('p', 'panel-hint', { text: `${t('docs.drop_hint', getLang())} — ${t('docs.accept_hint', getLang())}` });
  const messages = el('div', 'panel-messages', { role: 'status', 'aria-live': 'polite' });
  const pickForm = el('div', 'docs__pick');
  const fileInput = el('input', 'sr-only', { type: 'file', id: 'docs-file-input', accept: '.pdf,.txt,.docx,.md' });
  const pickLabel = el('label', 'docs__pick-btn', { for: 'docs-file-input', text: t('docs.pick', getLang()) });
  pickForm.append(fileInput, pickLabel);

  const uploadsList = el('div', 'docs__uploads', { 'aria-live': 'polite' });
  const list = el('ul', 'docs__list', { 'aria-label': t('docs.title', getLang()) });
  const empty = el('p', 'docs__empty', { text: t('docs.empty', getLang()) });

  panelHandle.body.append(hint, messages, pickForm, uploadsList, list, empty);

  let documents = [];
  let offline = false;

  function say(text, kind = 'info') {
    const line = el('div', `panel-message panel-message--${kind}`, { text });
    messages.append(line);
    setTimeout(() => line.remove(), 8000);
  }

  function renderList() {
    list.innerHTML = '';
    empty.hidden = documents.length > 0;
    for (const doc of documents) {
      const li = el('li', 'docs__item');
      const name = el('span', 'docs__name', { text: doc.name });
      const meta = el('span', 'docs__meta', {
        text: [
          doc.kind,
          typeof doc.chunks === 'number' ? t('docs.chunks', getLang(), { n: doc.chunks }) : null,
          typeof doc.chars === 'number' ? t('docs.chars', getLang(), { n: doc.chars }) : null,
        ].filter(Boolean).join(' · '),
      });
      const del = el('button', 'docs__delete', { type: 'button', text: t('docs.delete', getLang()), 'aria-label': `${t('docs.delete', getLang())} ${doc.name}` });
      del.addEventListener('click', () => removeDocument(doc));
      li.append(name, meta, del);
      list.append(li);
    }
  }

  async function refresh() {
    try {
      const res = await fetch(`${baseUrl}/api/documents`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      documents = await res.json();
      renderList();
    } catch (err) {
      console.warn('[documents] refresh failed', err);
    }
  }

  async function removeDocument(doc) {
    // eslint-disable-next-line no-alert
    if (!window.confirm(t('docs.delete_confirm', getLang(), { name: doc.name }))) return;
    const prev = documents;
    documents = documents.filter((d) => d.id !== doc.id);
    renderList();
    try {
      const res = await fetch(`${baseUrl}/api/documents/${encodeURIComponent(doc.id)}`, { method: 'DELETE' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (err) {
      documents = prev;
      renderList();
      say(t('docs.delete_failed', getLang(), { name: doc.name }), 'error');
    }
  }

  function addUploadRow(name) {
    const row = el('div', 'docs__upload-row');
    const label = el('span', 'docs__upload-name', { text: name });
    const bar = el('div', 'docs__upload-bar');
    const fill = el('div', 'docs__upload-fill');
    bar.append(fill);
    const stage = el('span', 'docs__upload-stage', { text: t('docs.stage.uploading', getLang(), { pct: 0 }) });
    row.append(label, bar, stage);
    uploadsList.append(row);
    return {
      setPct(pct) {
        fill.style.width = `${pct}%`;
        stage.textContent = t('docs.stage.uploading', getLang(), { pct });
      },
      setStage(key) {
        row.dataset.stage = key;
        stage.textContent = t(`docs.stage.${key}`, getLang());
      },
      remove(delay = 1500) {
        setTimeout(() => row.remove(), delay);
      },
    };
  }

  function ingest(file) {
    if (offline) {
      say(t('docs.offline', getLang()), 'warn');
      return;
    }
    const ext = extOf(file.name);
    if (!ACCEPTED_EXT.includes(ext)) {
      say(t('docs.unsupported_type', getLang(), { name: file.name }), 'error');
      return;
    }

    const rowHandle = addUploadRow(`${file.name} (${formatBytes(file.size)})`);
    const form = new FormData();
    form.append('file', file, file.name);

    const xhr = new XMLHttpRequest();
    xhr.open('POST', `${baseUrl}/api/documents`);

    xhr.upload.addEventListener('progress', (ev) => {
      if (!ev.lengthComputable) return;
      const pct = Math.round((ev.loaded / ev.total) * 100);
      rowHandle.setPct(pct);
      if (pct >= 100) rowHandle.setStage('parsing');
    });

    xhr.addEventListener('load', () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        rowHandle.setStage('chunking');
        let doc;
        try {
          doc = JSON.parse(xhr.responseText);
        } catch {
          doc = { id: `local-${Date.now()}`, name: file.name, kind: ext, chunks: null, chars: null };
        }
        rowHandle.setStage('learned');
        documents = documents.filter((d) => d.id !== doc.id).concat(doc);
        renderList();
        rowHandle.remove();
      } else {
        rowHandle.setStage('error');
        say(t('docs.upload_failed', getLang(), { name: file.name, reason: `HTTP ${xhr.status}` }), 'error');
        rowHandle.remove(4000);
      }
    });

    xhr.addEventListener('error', () => {
      rowHandle.setStage('error');
      say(t('docs.upload_failed', getLang(), { name: file.name, reason: 'network error' }), 'error');
      rowHandle.remove(4000);
    });

    xhr.send(form);
  }

  function handleDocumentLearned(evt) {
    // Gateway event: {type:'document_learned', ...doc}. Keep the library in sync even
    // when a document is learned by a path other than this panel's own uploads.
    if (!evt || !evt.id) return;
    documents = documents.filter((d) => d.id !== evt.id).concat(evt);
    renderList();
  }

  function setOffline(v) {
    offline = v;
    fileInput.disabled = v;
    pickLabel.classList.toggle('is-disabled', v);
  }

  fileInput.addEventListener('change', () => {
    for (const file of fileInput.files) ingest(file);
    fileInput.value = '';
  });

  function retranslate() {
    if (panelHandle.setTitle) panelHandle.setTitle(t('docs.title', getLang()));
    hint.textContent = `${t('docs.drop_hint', getLang())} — ${t('docs.accept_hint', getLang())}`;
    pickLabel.textContent = t('docs.pick', getLang());
    empty.textContent = t('docs.empty', getLang());
    renderList();
  }

  refresh();

  return { panelRoot: panelHandle.root, ingest, refresh, handleDocumentLearned, setOffline, retranslate };
}

export { ACCEPTED_EXT };
