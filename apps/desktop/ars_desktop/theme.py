"""Shared look for the desktop shell's own screens (splash + diagnostics).

Deliberately not the product UI — that lives in ``ars_gateway.ui`` and owns the real
visual language. This is just enough sci-fi to not show a white flash or a stack
trace: a dark field, a glowing core, monospace status text. Self-contained (no
external CSS/font files) so it works identically from a raw ``html=`` string, a
frozen bundle, or a source checkout, with nothing that can fail to resolve.
"""

from __future__ import annotations

CSS = """
:root {
  --bg: #05070d;
  --bg-2: #0b1120;
  --core: #57e8d8;
  --core-2: #7c6bf2;
  --ink: #d9e6f2;
  --ink-dim: #7c8aa0;
  --danger: #ff6b6b;
  --ok: #57e8d8;
}
* { box-sizing: border-box; }
html, body {
  margin: 0; padding: 0; height: 100%;
  background: radial-gradient(ellipse at 50% 35%, var(--bg-2), var(--bg) 70%);
  color: var(--ink);
  font: 13px/1.5 -apple-system, "SF Pro Text", "Helvetica Neue", sans-serif;
  overflow: hidden;
  user-select: none;
  -webkit-user-select: none;
}
.wrap {
  height: 100%; display: flex; flex-direction: column; align-items: center;
  justify-content: center; gap: 22px; padding: 40px; text-align: center;
}
.core {
  width: 96px; height: 96px; border-radius: 50%; position: relative;
  background: radial-gradient(circle at 40% 35%, #eafffb, var(--core) 30%, var(--core-2) 75%, transparent 78%);
  box-shadow: 0 0 40px 6px rgba(87, 232, 216, 0.45), 0 0 90px 20px rgba(124, 107, 242, 0.25);
  animation: pulse 2.4s ease-in-out infinite;
}
.core::before, .core::after {
  content: ""; position: absolute; border-radius: 50%;
  border: 1px solid rgba(87, 232, 216, 0.35); inset: -14px;
  animation: spin 7s linear infinite;
}
.core::after { inset: -28px; border-color: rgba(124, 107, 242, 0.25); animation-duration: 11s; animation-direction: reverse; }
@keyframes pulse {
  0%, 100% { transform: scale(1); filter: brightness(1); }
  50% { transform: scale(1.08); filter: brightness(1.15); }
}
@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
h1 {
  font: 600 20px/1.2 -apple-system, "SF Pro Display", sans-serif;
  letter-spacing: 0.14em; margin: 0; color: #f3f8ff;
}
h1 small { display: block; margin-top: 6px; font-size: 11px; letter-spacing: 0.08em; color: var(--ink-dim); font-weight: 400; }
.status { color: var(--ink-dim); min-height: 18px; font-size: 12.5px; letter-spacing: 0.02em; }
.status.err { color: var(--danger); }
.panel {
  max-width: 560px; text-align: left; background: rgba(255,255,255,0.03);
  border: 1px solid rgba(255,255,255,0.08); border-radius: 12px; padding: 18px 22px;
}
.panel h2 { margin: 0 0 10px; font-size: 13px; letter-spacing: 0.06em; color: #f3f8ff; text-transform: uppercase; }
.row { display: flex; align-items: flex-start; gap: 10px; padding: 6px 0; border-top: 1px solid rgba(255,255,255,0.05); }
.row:first-of-type { border-top: none; }
.dot { width: 9px; height: 9px; border-radius: 50%; margin-top: 4px; flex: none; }
.dot.ok { background: var(--ok); box-shadow: 0 0 8px var(--ok); }
.dot.bad { background: var(--danger); box-shadow: 0 0 8px var(--danger); }
.row .label { font-weight: 600; color: var(--ink); min-width: 92px; }
.row .detail { color: var(--ink-dim); white-space: pre-wrap; font: 12px/1.5 ui-monospace, "SF Mono", Menlo, monospace; }
.actions { display: flex; gap: 10px; margin-top: 16px; flex-wrap: wrap; }
button {
  font: 600 12px -apple-system, sans-serif; letter-spacing: 0.03em; color: #04211d;
  background: linear-gradient(180deg, #7cf2e4, #4bcabb); border: none; border-radius: 8px;
  padding: 9px 16px; cursor: pointer;
}
button.secondary { background: rgba(255,255,255,0.08); color: var(--ink); }
button:active { transform: translateY(1px); }
code {
  display: block; margin-top: 6px; padding: 8px 10px; border-radius: 6px;
  background: rgba(0,0,0,0.35); color: #9be8ff; font: 11.5px/1.5 ui-monospace, "SF Mono", Menlo, monospace;
  white-space: pre-wrap; word-break: break-word;
}
.footer { color: var(--ink-dim); font-size: 11px; margin-top: 4px; }
"""


def page(body: str, *, title: str = "A.R.S") -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{CSS}</style>
</head><body>{body}</body></html>"""
