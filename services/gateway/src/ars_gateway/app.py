"""The A.R.S gateway — one process that owns every service and serves the interface.

Deliberately a monolith. A.R.S runs on Alex's laptop, for Alex; splitting it into
containers that talk over a network would add latency to a 1.4-second budget and buy
nothing. The service boundaries are real and enforced in code — they just share a process.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from ars_auth.audit import AuditLog
from ars_auth.guard import PolicyGuardEngine
from ars_auth.store import SqliteGrantStore
from ars_compute.backends.ollama import OllamaBackend
from ars_compute.turn import TurnOrchestrator
from ars_core import ArsConfig
from ars_memory.config import MemoryConfig
from ars_memory.store import SqliteMemoryStore
from ars_protocol import (
    Capability, CapabilityGrant, ConfirmPolicy, Device, GrantSource, Language, Session,
    Transcript,
)
from ars_skills import GitHubSkill, InProcessSkillRuntime, WebSkill
from fastapi import FastAPI, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .brain import Answer, Tier, TieredBrain
from .documents import DocumentLibrary, UnsupportedDocument
from .voice import LadderTurnHandler, VoiceLoop

log = logging.getLogger("ars.gateway")
UI_DIR = Path(__file__).parent / "ui"

MAX_UPLOAD_BYTES = 64 * 1024 * 1024


class Ars:
    """Everything A.R.S is, assembled once at startup."""

    def __init__(self, config: ArsConfig | None = None) -> None:
        self.config = config or ArsConfig()
        self.session = Session(device=Device.DESKTOP)
        self.memory: Any = None
        self.grants: Any = None
        self.guard: Any = None
        self.backend: Any = None
        self.orchestrator: Any = None
        self.brain: Any = None
        self.library: Any = None
        self.skills: Any = None
        self.voice: Any = None
        """Built on the first press of the mic button, never at startup: the voice models
        are ~1.6 GB and a user who only types should not wait for them."""
        self.ready = False
        self.startup_note = ""

    async def start(self) -> None:
        data_dir = Path(self.config.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)

        self.memory = await SqliteMemoryStore.open(
            MemoryConfig(db_path_override=data_dir / "memory.db")
        )
        self.grants = SqliteGrantStore(data_dir / "grants")
        await self.grants.__aenter__()
        self.guard = PolicyGuardEngine(
            grants=self.grants,
            audit=AuditLog(Path(self.config.guard.guard_audit_log)),
            config=self.config.guard,
        )
        self.skills = InProcessSkillRuntime(guard_evaluate=self.guard.evaluate)
        for skill in (WebSkill(), GitHubSkill()):
            self.skills.register(skill)

        self.backend = OllamaBackend(
            model=self.config.llm.llm_local_model, host=self.config.llm.llm_local_host
        )
        self.orchestrator = TurnOrchestrator(
            backend=self.backend, guard=self.guard, skills=self.skills
        )
        self.brain = TieredBrain(memory=self.memory)
        self.library = DocumentLibrary(self.memory)
        learned = await self.library.rehydrate()
        if learned:
            log.info("%d documents already learned", learned)

        # Searching the web is low risk and is what an assistant is for. Everything that
        # touches Alex's own accounts stays ungranted until he grants it deliberately.
        for cap in (Capability.WEB_SEARCH, Capability.WEB_FETCH,
                    Capability.GITHUB_READ_PUBLIC, Capability.MEMORY_READ):
            if not await self.grants.find(cap, "*"):
                await self.grants.grant(CapabilityGrant(
                    capability=cap, resource_patterns=("*",),
                    confirm=ConfirmPolicy.NEVER, source=GrantSource.CONFIG,
                    note="default: reading the open web and A.R.S's own memory",
                ))

        # The embedding model loads lazily on first use, which put ~7 s of weight-loading
        # on the first question of every run — paid by the user, on the hot path, inside a
        # 1400 ms budget. Warming it here moves that cost to startup, where it belongs.
        # It also warms the cheap tiers: they cannot answer anything until it is loaded.
        try:
            await self.memory.recall("warm", limit=1)
        except Exception:
            log.warning("embedding warm-up failed; the first question will pay for it",
                        exc_info=True)

        try:
            await self.backend.warm([""])
            self.startup_note = "local model warm"
        except Exception as exc:
            self.startup_note = (
                f"local model unreachable ({type(exc).__name__}). A.R.S will answer from "
                "your documents and memory; start Ollama for full reasoning."
            )
            log.warning(self.startup_note)
        self.ready = True

    async def listen(self, on_event: Any) -> Any:
        """Start (or reuse) the voice loop, and press the button once."""
        if self.voice is None:
            self.voice = VoiceLoop(
                handler=LadderTurnHandler(answer_stream, cancel_backend=self.backend.cancel),
                session=self.session,
                on_event=on_event,
            )
        else:
            self.voice._on_event = on_event  # the socket that asked is the one that hears
        await self.voice.start()
        self.voice.press()
        return self.voice

    async def stop(self) -> None:
        if self.voice is not None:
            with contextlib.suppress(Exception):
                await self.voice.stop()
        with contextlib.suppress(Exception):
            await self.memory.close()
        with contextlib.suppress(Exception):
            await self.grants.__aexit__(None, None, None)


ars = Ars()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await ars.start()
    yield
    await ars.stop()


app = FastAPI(title="A.R.S", version="0.1.0", lifespan=lifespan)


# --------------------------------------------------------------------------- the turn

async def answer_stream(question: str, *, language: Language | None = None,
                        min_tier: Tier | None = None) -> AsyncIterator[dict]:
    """One question in, a stream of UI events out.

    The tier ladder lives here rather than in the orchestrator because it is a *product*
    decision — how much of Alex's machine a question is worth — not a reasoning one.
    """
    started = time.perf_counter()
    lang = language or Language.EN

    if min_tier is None or min_tier <= Tier.DOCUMENTS:
        cheap = await ars.brain.try_cheap_tiers(question, language=lang)
        if cheap is not None and (min_tier is None or cheap.tier >= min_tier):
            yield _tier_event(cheap)
            yield {"type": "reply_delta", "text": cheap.text, "language": lang.value}
            yield {"type": "reply_done", "text": cheap.text, "language": lang.value,
                   "tier": cheap.tier.label, "can_escalate": True}
            return

    # The model tier. Retrieved passages go in as context with provenance preserved.
    hits = await ars.memory.recall(question, limit=ars.brain.config.max_local_context)
    yield {"type": "state", "state": "thinking"}

    tier = Tier.LOCAL
    collected: list[str] = []
    finished: dict | None = None
    failed = False
    try:
        events = ars.orchestrator.run(
            session=ars.session,
            transcript=Transcript(text=question, language=lang, is_final=True),
            memories=tuple(hits),
            tools=await ars.skills.available_tools(),
            spoken=False,
        )
        async for event in events:
            payload = json.loads(event.model_dump_json())
            kind = payload.get("type")
            if kind == "reply_delta":
                collected.append(payload.get("text", ""))
            elif kind == "error":
                # The turn still produces text — "the local model isn't responding right
                # now" is a good thing to say. It is not a good thing to *learn*: cached,
                # it becomes the permanent answer to that question, served from tier 0 at
                # 100% confidence long after the model came back.
                failed = True
            elif kind == "state" and payload.get("state") == "idle":
                # The orchestrator's turn ends here; this one does not — the tier verdict
                # still has to go out. Exactly one `idle` may cross this socket per turn,
                # and it is the one the websocket handler sends last. Forwarding this one
                # makes a client that (rightly) treats idle as end-of-turn stop reading
                # early, and every later event lands on the *next* question.
                continue
            elif kind == "reply_done":
                # Held back until after the tier event: the UI closes the turn on
                # reply_done, and a tier badge that arrives after that is dropped.
                finished = payload
                continue
            yield payload
    except Exception as exc:
        log.exception("turn failed")
        yield {"type": "error", "code": type(exc).__name__,
               "message": f"the local model could not be reached: {exc}",
               "recoverable": True}
        return

    text = "".join(collected).strip()
    answer = Answer(text=text, tier=tier, language=lang,
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                    reason="answered by the local model on the GPU", can_escalate=False,
                    can_learn=not failed)
    ars.brain.stats.record(answer)
    yield _tier_event(answer)
    if finished is not None:
        yield finished
    if text:
        await ars.brain.learn_answer(question, answer, language=lang)


def _tier_event(answer: Answer) -> dict:
    return {
        "type": "tier",
        "tier": answer.tier.label,
        "score": round(answer.score, 4),
        "reason": answer.reason,
        "elapsed_ms": round(answer.elapsed_ms, 1),
        "citations": list(answer.citations),
        "uses_gpu": answer.tier.uses_gpu,
        "can_escalate": answer.can_escalate,
        "stats": _tier_stats(),
    }


def _tier_stats() -> dict:
    s = ars.brain.stats
    return {"counts": s.counts, "total_turns": s.total_turns,
            "gpu_turns": s.gpu_turns, "gpu_avoided_pct": round(s.gpu_avoided_pct, 1)}


# --------------------------------------------------------------------------- websocket

@app.websocket("/ws")
async def websocket(ws: WebSocket) -> None:
    await ws.accept()
    await ws.send_json({"type": "session_started", "session_id": ars.session.id,
                        "languages": [l.value for l in ars.config.languages],
                        "model": ars.config.llm.llm_local_model,
                        "note": ars.startup_note})
    current: asyncio.Task | None = None
    try:
        while True:
            message = await ws.receive_json()
            kind = message.get("type")

            if kind == "interrupt":
                if current and not current.done():
                    current.cancel()
                if ars.voice is not None and ars.voice.running:
                    # Barge-in: the pipeline owns the synthesiser and the speaker queue,
                    # and muting the speaker while the machine keeps working is not an
                    # interrupt, it is a lie.
                    with contextlib.suppress(Exception):
                        await ars.voice.interrupt()
                with contextlib.suppress(Exception):
                    await ars.backend.cancel()
                await ws.send_json({"type": "state", "state": "idle"})
                continue

            if kind == "listen":
                # The desktop shell's WKWebView has no getUserMedia, so the button does
                # not capture anything — it tells the gateway to, and the audio never
                # leaves this process.
                if message.get("on") is False:
                    if ars.voice is not None:
                        await ars.voice.stop()
                    await ws.send_json({"type": "listening", "on": False})
                    continue
                try:
                    await ws.send_json({"type": "listening", "on": True, "warming": True})
                    voice = await ars.listen(ws.send_json)
                    await ws.send_json({"type": "listening", "on": True, "warming": False,
                                        "warm_up_ms": voice.warm_up_ms})
                except Exception as exc:
                    log.exception("could not start listening")
                    await ws.send_json({
                        "type": "error", "code": type(exc).__name__,
                        "message": f"could not open the microphone: {exc}",
                        "recoverable": True,
                    })
                continue

            if kind != "text":
                continue

            question = (message.get("text") or "").strip()
            if not question:
                continue
            lang = Language(message["language"]) if message.get("language") else None
            floor = Tier(message["min_tier"]) if message.get("min_tier") is not None else None

            async def run(q: str = question, la: Language | None = lang,
                          ft: Tier | None = floor) -> None:
                try:
                    async for event in answer_stream(q, language=la, min_tier=ft):
                        await ws.send_json(event)
                    await ws.send_json({"type": "state", "state": "idle"})
                except asyncio.CancelledError:
                    await ws.send_json({"type": "state", "state": "idle"})
                    raise

            current = asyncio.create_task(run())
            await current
    except WebSocketDisconnect:
        if current and not current.done():
            current.cancel()
    except Exception:
        log.exception("websocket failed")


# --------------------------------------------------------------------------- documents

@app.post("/api/documents")
async def upload(file: UploadFile) -> JSONResponse:
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"{file.filename} is larger than 64 MB")
    name = file.filename or "untitled"
    try:
        # Parsing is CPU-bound and a 200-page PDF would stall every open WebSocket, so it
        # goes to a thread. The database writes stay on this loop — the SQLite connection
        # belongs to it.
        parsed = await asyncio.get_running_loop().run_in_executor(
            None, ars.library.parse, name, data
        )
        doc = await ars.library.store(parsed)
    except UnsupportedDocument as exc:
        raise HTTPException(415, str(exc)) from exc
    except Exception as exc:
        log.exception("ingest failed")
        raise HTTPException(500, f"could not learn {name}: {exc}") from exc
    return JSONResponse({"id": doc.id, "name": doc.name, "kind": doc.kind,
                         "chunks": doc.chunks, "chars": doc.chars, "pages": doc.pages})


@app.get("/api/documents")
async def documents() -> list[dict]:
    return ars.library.catalogue()


@app.delete("/api/documents/{doc_id}")
async def forget_document(doc_id: str) -> dict:
    removed = await ars.library.forget(doc_id)
    return {"id": doc_id, "chunks_removed": removed}


# --------------------------------------------------------------------------- escalation

@app.post("/api/escalate")
async def escalate(body: dict) -> dict:
    """"That answer was not good enough." Remember it, so the same question skips the
    cheap tiers next time. This is how the 85% threshold actually gets tuned."""
    question = (body.get("question") or "").strip()
    if not question:
        raise HTTPException(400, "question is required")
    ars.brain.remember_escalation(question, Tier.LOCAL)
    return {"question": question, "next_tier": Tier.LOCAL.label}


# --------------------------------------------------------------------------- status

@app.get("/api/status")
async def status() -> dict:
    ollama_up = True
    try:
        await ars.backend.capabilities()
    except Exception:
        ollama_up = False
    return {
        "ready": ars.ready,
        "note": ars.startup_note,
        "model": ars.config.llm.llm_local_model,
        "backend": ars.config.llm.llm_backend,
        "local_model_available": ollama_up,
        "languages": [l.value for l in ars.config.languages],
        "documents": len(ars.library.docs) if ars.library else 0,
        "tiers": _tier_stats(),
        "thresholds": {
            # Two different scales on purpose: tier 0 compares a question to a question
            # (raw cosine), tier 1 asks whether a passage answers one (calibrated).
            "recall_cosine": ars.brain.config.recall_cosine,
            "documents": ars.brain.config.document_threshold,
        },
    }


# --------------------------------------------------------------------------- grants

@app.get("/api/grants")
async def list_grants() -> list[dict]:
    return [json.loads(g.model_dump_json()) for g in await ars.grants.active_grants()]


@app.post("/api/grants")
async def add_grant(body: dict) -> dict:
    try:
        capability = Capability(body["capability"])
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, "unknown capability") from exc
    grant = await ars.grants.grant(CapabilityGrant(
        capability=capability,
        resource_patterns=tuple(body.get("resource_patterns") or ("*",)),
        confirm=ConfirmPolicy(body.get("confirm", ConfirmPolicy.EVERY_USE.value)),
        source=GrantSource.UI, note=body.get("note"),
    ))
    return json.loads(grant.model_dump_json())


@app.delete("/api/grants/{grant_id}")
async def revoke_grant(grant_id: str) -> dict:
    return {"id": grant_id, "revoked": await ars.grants.revoke(grant_id)}


@app.get("/api/capabilities")
async def capabilities() -> list[dict]:
    return [
        {"capability": c.value, "risk": c.risk.value,
         "private": c.touches_private_data, "effectful": c.is_effectful,
         "outbound": c.exfiltrates_outward}
        for c in Capability
    ]


@app.get("/api/audit")
async def audit(limit: int = 50) -> list[dict]:
    path = Path(ars.config.guard.guard_audit_log)
    if not path.exists():
        return []
    lines = path.read_text(errors="replace").splitlines()[-limit:]
    out = []
    for line in lines:
        with contextlib.suppress(json.JSONDecodeError):
            out.append(json.loads(line))
    return list(reversed(out))


# --------------------------------------------------------------------------- the UI

@app.get("/health")
async def health() -> dict:
    return {"ok": ars.ready}


@app.get("/")
async def index() -> Any:
    page = UI_DIR / "index.html"
    if page.exists():
        return FileResponse(page)
    return JSONResponse({"ars": "running", "ui": "not built yet", "docs": "/docs"})


# Mounted at the root, and last, on purpose.
#
# index.html is served from "/" and asks for `./theme.css` and `./app.js` — the natural
# spelling, and the one that keeps working if the interface is ever opened from a file or
# a different prefix. Mounted only under /ui, those resolve to /theme.css and /app.js and
# 404: the HUD renders as unstyled HTML with no JavaScript, no console and no WebSocket,
# while every API route still answers perfectly. Nothing catches that except opening it.
#
# Last, because a mount at "/" matches everything: every API route above is registered
# first and therefore still wins. Anything added below this line will not be reachable.
if UI_DIR.exists():
    app.mount("/", StaticFiles(directory=UI_DIR, html=True), name="ui")
