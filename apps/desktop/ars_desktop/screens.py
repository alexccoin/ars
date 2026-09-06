"""The two states the shell can be in before it hands off to the real gateway UI:
booting (splash) and something needs attention (diagnostics). Both render as plain
strings so they can be handed to ``webview.create_window(html=...)`` /
``window.load_html(...)`` with no file I/O and nothing that can fail to resolve.
"""

from __future__ import annotations

import html as _html

from . import preflight
from .theme import page


def splash_html(status: str = "waking up…") -> str:
    body = f"""
<div class="wrap" id="splash">
  <div class="core"></div>
  <h1>A.R.S<small>autonomous reasoning system</small></h1>
  <div class="status" id="status">{_html.escape(status)}</div>
</div>
<script>
  // Python calls window.evaluate_js("setStatus('...')") as boot proceeds.
  function setStatus(text, isErr) {{
    var el = document.getElementById('status');
    el.textContent = text;
    el.className = 'status' + (isErr ? ' err' : '');
  }}
</script>
"""
    return page(body, title="A.R.S")


def _model_rows(status: preflight.ModelStatus) -> str:
    labels = {"asr": "Speech recognition", "tts": "Voice synthesis (EN/RO)",
              "vad": "Voice activity", "wakeword": "Wake word"}
    rows = []
    for key, label in labels.items():
        ok = status.present.get(key, False)
        cls = "ok" if ok else "bad"
        detail = "found" if ok else "missing"
        rows.append(
            f'<div class="row"><div class="dot {cls}"></div>'
            f'<div class="label">{label}</div><div class="detail">{detail}</div></div>'
        )
    return "".join(rows)


def diagnostic_html(
    *,
    gateway_error: str | None,
    models: preflight.ModelStatus,
    ollama: preflight.OllamaStatus,
    fetch_cmd: str | None,
    port: int | None,
) -> str:
    """Shown instead of a blank window or a stack trace when something the app cannot
    fix on its own is missing or broken. Never the terminal state on purpose — a
    "Retry" always re-checks and, on the gateway module error, tries again."""
    sections = []

    if gateway_error:
        sections.append(f"""
<div class="panel">
  <h2>Gateway did not start</h2>
  <div class="row"><div class="dot bad"></div>
    <div><div class="label">ars_gateway.app</div>
    <div class="detail">could not be imported — this usually means the backend package
    is still being built, or a dependency failed to install.</div>
    <code>{_html.escape(gateway_error)}</code></div>
  </div>
</div>""")

    ollama_cls = "ok" if ollama.running else "bad"
    ollama_detail = preflight.ollama_advice(ollama)
    sections.append(f"""
<div class="panel">
  <h2>Local reasoning (Ollama)</h2>
  <div class="row"><div class="dot {ollama_cls}"></div>
    <div><div class="label">{"running" if ollama.running else ("installed" if ollama.installed else "not found")}</div>
    <div class="detail">{_html.escape(ollama_detail)}</div></div>
  </div>
</div>""")

    fetch_button = (
        '<button onclick="pywebview.api.fetch_models()">Fetch voice models (~1.6 GB)</button>'
        if not models.all_present and fetch_cmd
        else ""
    )
    models_note = (
        ""
        if models.all_present
        else (
            "" if fetch_cmd else
            f'<div class="footer">No A.R.S checkout found to run the fetch script from. '
            f"Set <code style='display:inline;padding:1px 5px'>ARS_REPO_ROOT</code> to your "
            f"checkout, or run <code style='display:inline;padding:1px 5px'>scripts/fetch_voice_models.sh"
            f"</code> yourself with <code style='display:inline;padding:1px 5px'>ARS_MODELS_DIR="
            f"{_html.escape(str(models.models_dir))}</code>.</div>"
        )
    )
    sections.append(f"""
<div class="panel">
  <h2>Voice models</h2>
  {_model_rows(models)}
  <div class="footer">looking in {_html.escape(str(models.models_dir))}</div>
  {models_note}
</div>""")

    port_line = f"listening on 127.0.0.1:{port}" if port else "not yet listening"
    body = f"""
<div class="wrap">
  <div class="core"></div>
  <h1>A.R.S<small>{_html.escape(port_line)}</small></h1>
  {''.join(sections)}
  <div class="actions">
    <button onclick="pywebview.api.retry()">Retry</button>
    {fetch_button}
    <button class="secondary" onclick="pywebview.api.quit()">Quit</button>
  </div>
</div>
"""
    return page(body, title="A.R.S — attention needed")


def fetch_started_html(command: str) -> str:
    body = f"""
<div class="wrap">
  <div class="core"></div>
  <h1>A.R.S<small>fetching voice models</small></h1>
  <div class="panel">
    <h2>Running in Terminal</h2>
    <div class="detail" style="color:var(--ink-dim)">A Terminal window opened to download
    the voice models (~1.6&nbsp;GB). This can take a few minutes depending on your
    connection. Close this window and reopen A.R.S once it finishes, or click Retry.</div>
    <code>{_html.escape(command)}</code>
  </div>
  <div class="actions">
    <button onclick="pywebview.api.retry()">Retry</button>
  </div>
</div>
"""
    return page(body, title="A.R.S — fetching models")
