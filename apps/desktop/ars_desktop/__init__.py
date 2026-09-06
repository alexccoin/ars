"""A.R.S desktop shell.

A thin native wrapper: it starts the gateway (the real FastAPI app in
``ars_gateway.app``) on a loopback port and opens a native WKWebView window onto it.
No product logic lives here — that is the gateway's job. This package owns exactly
three things: process lifecycle (start/health/stop, no orphans), the window (native
chrome, geometry memory, splash), and first-run diagnostics for the two things that
live outside the repo and can legitimately be missing on a fresh machine: the voice
model weights and Ollama.
"""

from __future__ import annotations

__version__ = "0.1.0"
