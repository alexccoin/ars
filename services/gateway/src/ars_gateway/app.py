"""The A.R.S gateway — one process that owns every service and serves the interface.

Deliberately a monolith. A.R.S runs on Alex's laptop, for Alex; splitting it into
containers that talk over a network would add latency to a 1.4-second budget and buy
nothing. The service boundaries are real and enforced in code — they just share a process.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import secrets
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from ars_auth.audit import AuditLog
from ars_auth.guard import PolicyGuardEngine
from ars_auth.store import SqliteGrantStore
from ars_compute.backends.ollama import OllamaBackend
from ars_compute.translation import LlmTranslationEngine
from ars_compute.turn import TurnOrchestrator
from ars_core import ArsConfig
from ars_medical import MedicalConfig, SqliteMedicalStore
from ars_memory.config import MemoryConfig
from ars_memory.store import SqliteMemoryStore
from ars_protocol import (
    Capability,
    new_id,
    GuardQuery,
    Verdict,
    RangeFinding,
    VitalKind,
    VitalReading,
    MemoryKind, CapabilityGrant, ConfirmPolicy, Device, GrantSource, Language, Session,
    Transcript,
)
from ars_skills import GitHubSkill, InProcessSkillRuntime, MedicalSkill, WebSkill
from fastapi import (
    FastAPI, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .access import (
    COOKIE, QUERY_PARAM, DeviceTokenMiddleware, is_loopback, lan_address,
    load_or_create_token, origin_is_own_page, tokens_match,
)
from .brain import Answer, Tier, TieredBrain
from .persona import PersonaPicker
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
        self.translator: Any = None
        self.personas: Any = None
        self.vitals: Any = None
        self.voice: Any = None
        """Built on the first press of the mic button, never at startup: the voice models
        are ~1.6 GB and a user who only types should not wait for them."""
        self.ready = False
        self.warm = False
        """Ready is "safe to answer"; warm is "fast to answer". They are different states
        and the interface says which one it is in."""
        self._warming: asyncio.Task | None = None
        self.startup_note = ""

    async def start(self) -> None:
        data_dir = Path(self.config.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)

        self.memory = await SqliteMemoryStore.open(
            MemoryConfig(db_path_override=data_dir / "memory.db")
        )
        # Health readings live in their own database, not in memory.db. They are
        # SENSITIVE, they have a different retention story, and "delete my health data"
        # has to be a thing someone can do without touching everything else A.R.S knows.
        self.vitals = await SqliteMedicalStore.open(
            MedicalConfig(db_path_override=data_dir / "medical.db")
        )
        self.grants = SqliteGrantStore(data_dir / "grants")
        await self.grants.__aenter__()
        self.guard = PolicyGuardEngine(
            grants=self.grants,
            audit=AuditLog(Path(self.config.guard.guard_audit_log)),
            config=self.config.guard,
        )
        self.skills = InProcessSkillRuntime(guard_evaluate=self.guard.evaluate)
        skills = [WebSkill(), GitHubSkill()]
        # The clinical reference is registered only when one is configured. A skill whose
        # every call fails with "no reference configured" is worse than an absent one: the
        # model sees the tool, offers it, calls it, and reports an error the user cannot
        # act on.
        if os.environ.get("ARS_MEDICAL_URL", "").strip():
            skills.append(MedicalSkill())
            log.info("clinical reference configured; medical tools available")
        for skill in skills:
            self.skills.register(skill)

        self.backend = OllamaBackend(
            model=self.config.llm.llm_local_model, host=self.config.llm.llm_local_host
        )
        self.orchestrator = TurnOrchestrator(
            backend=self.backend, guard=self.guard, skills=self.skills
        )
        self.personas = PersonaPicker(self.config.companion_name)
        if self.personas.enabled:
            log.info("companion persona active for %s", self.personas.name)
        self.brain = TieredBrain(memory=self.memory)
        # Documents are indexed in every language A.R.S speaks, not only their own. The
        # embedding model cannot match a question to a passage across languages — measured,
        # and a bigger model does not fix it — so the passage is bridged at ingest instead,
        # where it costs nothing on the hot path.
        self.translator = (
            LlmTranslationEngine(self.backend, languages=self.config.languages)
            if self.config.translate_documents else None
        )
        self.library = DocumentLibrary(
            self.memory, translator=self.translator, languages=self.config.languages
        )
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

        # Ready means "the guard, the grants and the store are open", which is everything
        # required to answer safely. Warming the models is NOT part of it: they take tens
        # of seconds, and blocking on them here meant the window sat on a splash screen
        # and then showed a diagnostic page saying A.R.S could not start — while the
        # gateway came up healthy seconds later. A shell that gives up on a working
        # process is worse than a slow first question.
        self.ready = True
        self.startup_note = "warming the models"
        self._warming = asyncio.create_task(self._warm(), name="ars-warm")

    async def _warm(self) -> None:
        """Load the models, after the interface is already up.

        Both of these are worth doing eagerly — the embedding model is ~7 s of weight
        loading that would otherwise land on the user's first question, inside a 1400 ms
        budget — but neither is worth making them look at a splash screen for.
        """
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
        self.warm = True
        log.info("models warm")

    async def listen(self, on_event: Any) -> Any:
        """Start (or reuse) the voice loop, and press the button once."""
        if self.voice is None:
            self.voice = VoiceLoop(
                handler=LadderTurnHandler(answer_stream, cancel_backend=self.backend.cancel,
                                          personas=self.personas),
                session=self.session,
                on_event=on_event,
            )
        else:
            self.voice.add_listener(on_event)
        await self.voice.start()
        self.voice.press()
        return self.voice

    async def stop(self) -> None:
        with contextlib.suppress(Exception):
            if self.vitals is not None:
                await self.vitals.close()
        if self._warming is not None and not self._warming.done():
            self._warming.cancel()
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

# Registered here, not inside `start`: middleware has to wrap the app before it serves its
# first request, and a gateway that is briefly open while it boots is a gateway that is
# open. The token is created on import for the same reason.
_DATA_DIR = Path(ars.config.data_dir).expanduser()
_DATA_DIR.mkdir(parents=True, exist_ok=True)
DEVICE_TOKEN = load_or_create_token(_DATA_DIR)
app.add_middleware(DeviceTokenMiddleware, token=DEVICE_TOKEN)


# --------------------------------------------------------------------------- the turn

async def answer_stream(question: str, *, language: Language | None = None,
                        min_tier: Tier | None = None) -> AsyncIterator[dict]:
    """One question in, a stream of UI events out.

    The tier ladder lives here rather than in the orchestrator because it is a *product*
    decision — how much of Alex's machine a question is worth — not a reasoning one.
    """
    started = time.perf_counter()
    lang = language or Language.EN

    # Announced before anything else in the turn: the interface changes the entity's form
    # on this, and a face that changes after the answer has already been spoken is a face
    # that changed for no visible reason.
    if ars.personas is not None and ars.personas.enabled:
        choice = ars.personas.choose(question, lang)
        yield {"type": "persona", "persona": choice.persona.value, "reason": choice.reason}

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
    used_tools = False
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
            elif kind == "tool_call":
                # A turn that consulted the world outside is a turn whose answer expires
                # with it. The temperature in New York was cached once already, and served
                # back later as though it were a fact.
                used_tools = True
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
                    can_learn=not failed and not used_tools)
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
    # Starlette's HTTP middleware does not see WebSocket handshakes, so the check is
    # repeated here rather than assumed. This socket is the one that can ask A.R.S to act,
    # so it is the last place an unpaired device should be able to reach.
    # Checked BEFORE loopback, because loopback is exactly where this attack comes from:
    # a page open in any browser on this Mac can open a socket to 127.0.0.1 and would
    # otherwise be treated as the owner.
    if not origin_is_own_page(ws.headers.get("origin"), ws.headers.get("host")):
        log.warning("refused a websocket from origin %r", ws.headers.get("origin"))
        await ws.close(code=4403, reason="this page is not allowed to talk to A.R.S")
        return

    if not is_loopback(ws.client.host if ws.client else None):
        presented = (
            ws.query_params.get(QUERY_PARAM)
            or ws.cookies.get(COOKIE)
            or (ws.headers.get("authorization", "")[7:].strip()
                if ws.headers.get("authorization", "").lower().startswith("bearer ") else None)
        )
        if presented is None or not tokens_match(presented, DEVICE_TOKEN):
            log.warning("refused a websocket from %s",
                        ws.client.host if ws.client else "unknown")
            await ws.close(code=4401, reason="this device is not paired with A.R.S")
            return
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
                        ars.voice.release()
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
        "warm": ars.warm,
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


# --------------------------------------------------------------------------- health

def _consent(request: Request) -> dict:
    """Consent and language, read off the request.

    `X-Ars-Consent: yes` means the interface showed the guard's own question and the user
    said yes — it is not a bypass, it is the answer to the question the guard asked.
    """
    header = (request.headers.get("accept-language") or "").split(",")[0].strip()[:2]
    language = next((l for l in ars.config.languages if l.value == header), None)
    return {
        "consented": request.headers.get("x-ars-consent", "").lower() == "yes",
        "language": language,
    }


async def _guard_health(capability: Capability, resource: str | None, summary: str,
                        *, consented: bool = False, language: Language | None = None) -> None:
    """Put a health request through the guard, and record the outcome.

    These endpoints called the store directly. `HEALTH_READ` (HIGH, private) and
    `HEALTH_WRITE` (CRITICAL, effectful) were therefore decorative: no grant required, no
    consent asked, and — the part that matters most — no line in the audit log. "Who read
    my health data, and when" had no answer, and neither did "where did this reading come
    from". Non-negotiable #3 says nothing reaches private data without a guard ALLOW, and
    this was private data with no guard at all.

    A DENY raises 403 rather than returning empty: an empty series and a refused series
    are different facts, and a health record that quietly shows nothing is worse than one
    that says it will not tell you.
    """
    if language is not None:
        # The guard writes its refusals in the user's language; without this it answered
        # a Romanian interface in English, which is the one sentence that must not be in
        # the wrong language — it is what the user is being asked to agree to.
        ars.guard.set_session_language(ars.session.id, language)

    query = GuardQuery(
        session_id=ars.session.id,
        # A fresh id per request, not a constant. With a fixed turn_id the per-TURN rate
        # limit became a per-PROCESS one: four reads and exactly one write for the life of
        # the gateway, after which every health request was permanently denied with
        # "something is looping". Each HTTP request is its own turn.
        turn_id=new_id("trn"),
        capability=capability,
        resource=resource,
        summary=summary,
    )
    decision = await ars.guard.evaluate(query)
    if decision.verdict is Verdict.DENY:
        await ars.guard.record_outcome(query, decision, outcome="refused")
        raise HTTPException(403, decision.explanation)
    if decision.verdict is Verdict.ASK and consented:
        # The interface showed the guard's own question and the user said yes. That is
        # what an ASK is for; refusing it anyway would make consent unreachable and the
        # whole health record permanently unopenable.
        await ars.guard.record_outcome(query, decision, outcome="performed",
                                       user_confirmed=True)
        return
    if decision.verdict is Verdict.ASK:
        await ars.guard.record_outcome(query, decision, outcome="asked")
        # 428 Precondition Required: the request is well formed and refused only for
        # want of a decision the user has not made yet. The interface turns this into the
        # consent prompt the guard already wrote, in the user's language.
        raise HTTPException(
            428, decision.explanation or "A.R.S needs your permission for this."
        )


@app.post("/api/health/readings")
async def record_reading(body: dict, request: Request) -> dict:
    """Record one measurement.

    Implausible values are refused rather than stored: a cuff that slipped reports 20/10,
    and one of those silently corrupts every average computed afterwards. Alarming but
    possible values are stored exactly as measured — filtering by what is healthy would
    delete the readings that matter most.
    """
    try:
        kind = VitalKind(body["kind"])
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, f"kind must be one of: {', '.join(k.value for k in VitalKind)}") from exc
    try:
        value = float(body["value"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(400, "value must be a number") from exc

    # No English summary: the guard composes the whole sentence from its own EN/RO/DE
    # phrasing for the capability and the resource. Handing it an English clause produced
    # "Dafür brauche ich deine Erlaubnis: … look at your health readings" — a consent
    # prompt half in a language the reader did not choose, which is the single worst
    # sentence in the product to get wrong, because it is what they are agreeing to.
    await _guard_health(
        Capability.HEALTH_WRITE, f"{kind.display_name} {value} {kind.unit}", "",
        **_consent(request),
    )
    reading = VitalReading(kind=kind, value=value, note=body.get("note") or None)
    if body.get("measured_at_ms"):
        reading = reading.model_copy(update={"measured_at_ms": int(body["measured_at_ms"])})
    try:
        stored = await ars.vitals.record(reading)
    except Exception as exc:
        raise HTTPException(422, str(exc)) from exc
    findings = await ars.vitals.findings([stored])
    return {"reading": json.loads(stored.model_dump_json()),
            "outside_range": [json.loads(f.model_dump_json()) for f in findings]}


@app.get("/api/health/readings")
async def read_readings(request: Request, kind: str | None = None,
                        days: int = 30) -> dict:
    """Your readings, and which of them sit outside a cited range.

    `outside_range` is a comparison against a named source, never a judgement: the
    protocol has no type for a diagnosis and this endpoint cannot invent one.
    """
    await _guard_health(
        # None, not "all": with no resource the guard uses its own "everything it covers"
        # phrasing, which it has in every language. "all" is an English word wearing a
        # translation's clothes.
        Capability.HEALTH_READ, kind or None, "", **_consent(request),
    )
    since = int(time.time() * 1000) - days * 86_400_000
    if kind:
        try:
            kinds = [VitalKind(kind)]
        except ValueError as exc:
            raise HTTPException(400, f"unknown kind {kind!r}") from exc
    else:
        kinds = [r.kind for r in await ars.vitals.latest_all()]

    out: dict[str, Any] = {"days": days, "series": {}}
    everything: list[VitalReading] = []
    for k in kinds:
        readings = await ars.vitals.series(k, since_ms=since)
        if not readings:
            continue
        everything.extend(readings)
        aggregate = await ars.vitals.aggregate(k, since_ms=since)
        out["series"][k.value] = {
            "unit": k.unit,
            "readings": [json.loads(r.model_dump_json()) for r in readings],
            # `Aggregate` is a slotted dataclass from services/medical, not a pydantic
            # model — it has no __dict__ and no model_dump_json. asdict() handles both
            # rather than guessing at which shape a service happens to use today.
            "aggregate": dataclasses.asdict(aggregate),
        }
    findings: tuple[RangeFinding, ...] = await ars.vitals.findings(everything)
    out["outside_range"] = [json.loads(f.model_dump_json()) for f in findings]
    return out


@app.delete("/api/health/readings/{reading_id}")
async def forget_reading(reading_id: str, request: Request) -> dict:
    """Really delete it. Non-negotiable #7 applies here with more force than anywhere."""
    await _guard_health(Capability.HEALTH_WRITE, reading_id, "", **_consent(request))
    # `reading_id`, not `record_id`. It was the latter, so every delete was a 500 and
    # non-negotiable #7 did not work through the API at all — found by the interface that
    # tried to use it.
    return {"id": reading_id, "removed": await ars.vitals.forget(reading_id=reading_id)}


# -------------------------------------------------------------------------- pairing

@app.get("/api/pairing")
async def pairing(request: Request) -> dict:
    """The link to open on another device. Loopback only.

    Handing the token to anything that already has the token would be pointless; handing
    it to anything that does not would defeat the middleware. So this answers on the
    machine A.R.S runs on, and nowhere else.
    """
    if not is_loopback(request.client.host if request.client else None):
        raise HTTPException(404)
    host = lan_address()
    port = request.url.port or 8787
    reachable = ars.config.listen_host not in ("127.0.0.1", "localhost", "::1")
    return {
        "reachable_from_other_devices": reachable,
        "url": f"http://{host}:{port}/?{QUERY_PARAM}={DEVICE_TOKEN}" if host else None,
        "listen_host": ars.config.listen_host,
        "hint": (
            "Open this link on your phone or another computer on the same network."
            if reachable else
            "A.R.S is only listening on this machine. Set ARS_LISTEN_HOST=0.0.0.0 and "
            "restart to let your other devices in."
        ),
    }


# ------------------------------------------------------------------------ knowledge

@app.get("/api/knowledge")
async def knowledge() -> dict:
    """Everything A.R.S knows, as a graph.

    Not a debug endpoint. A private assistant that learns from your files is asking for
    a lot of trust, and "what does it actually know about me" should be answerable by
    looking rather than by reading a database. Every node here is something the user put
    in or something A.R.S derived from it, and the edges say which.
    """
    records = await ars.memory.all_records()
    docs = {d["id"]: d for d in ars.library.catalogue()} if ars.library else {}

    nodes: list[dict] = []
    edges: list[dict] = []
    for doc_id, doc in docs.items():
        nodes.append({"id": doc_id, "kind": "document", "label": doc["name"],
                      "weight": max(doc["chunks"], 1), "language": None})

    for record in records:
        uri = record.provenance.uri or ""
        parent = uri[len("doc:"):].split("#")[0] if uri.startswith("doc:") else None
        kind = "fact" if record.kind is MemoryKind.FACT else "passage"
        label = record.text.split("\n", 1)[0][:70]
        if kind == "fact" and label.startswith("Q: "):
            label = label[3:]
        nodes.append({
            "id": record.id, "kind": kind, "label": label,
            "language": record.language.value, "weight": 1,
            "derived": record.origin_id is not None,
        })
        if parent and parent in docs:
            edges.append({"from": parent, "to": record.id, "kind": "contains"})
        if record.origin_id:
            # A translation is drawn as an edge, not a second document: it is the same
            # passage wearing another language.
            edges.append({"from": record.origin_id, "to": record.id, "kind": "translation"})

    by_language: dict[str, int] = {}
    for record in records:
        by_language[record.language.value] = by_language.get(record.language.value, 0) + 1

    return {
        "nodes": nodes,
        "edges": edges,
        "summary": {
            "documents": len(docs),
            "passages": sum(1 for n in nodes if n["kind"] == "passage"),
            "facts": sum(1 for n in nodes if n["kind"] == "fact"),
            "translations": sum(1 for e in edges if e["kind"] == "translation"),
            "by_language": by_language,
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
