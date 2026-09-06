"""A.R.S reasoning layer.

  * `context`   — provenance-preserving prompt assembly, quarantine framing, taint, budget
  * `backends`  — Ollama (local), Anthropic (cloud), Scripted (tests), Router (policy)
  * `turn`      — the orchestrator: stream, guard, act, feed back, stream again
  * `learning`  — behaviour observations, proposed to the user, never self-confirmed
  * `language`  — per-utterance reply language, code-switching, Romanian diacritics
  * `reasoning` — how much the model may think before it speaks, and what that costs
  * `prompts`   — versioned prompt assets; no prompt string exists in this Python source

Calling a backend: `LlmBackend.complete` is declared `async def` returning an
`AsyncIterator`, so the contract-correct call is `async for x in await be.complete(...)`.
Backends here also support the direct `async for x in be.complete(...)`, and
`stream_reply()` works with either kind. See `backends/base.py`.
"""

from .backends import (
    DEFAULT_THINK_POLICY,
    AnthropicBackend,
    OllamaBackend,
    Router,
    RoutingDecision,
    RoutingPolicy,
    Scene,
    ScriptedBackend,
    ThinkPolicy,
)
from .context import (
    AssembledContext,
    ContextAssembler,
    ContextBudget,
    DropStep,
    Slot,
    detect_injection_markers,
    render_blocks,
    taint_of,
)
from .errors import (
    BackendUnavailable,
    CloudRoutingRefused,
    ComputeError,
    ContextOverflow,
    NoBackendAvailable,
    TaintBackstopTriggered,
    ToolBudgetExceeded,
)
from .language import (
    LanguageSource,
    ReplyLanguage,
    detect_matrix_language,
    diacritics_report,
    resolve_reply_language,
    restore_diacritics,
)
from .learning import BehaviourLearner, Evidence
from .reasoning import ReasoningMode
from .sensitivity import SensitivityLedger
from .stream import stream_reply
from .turn import TurnOrchestrator, TurnOutcome, TurnStats

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_THINK_POLICY",
    "AnthropicBackend",
    "AssembledContext",
    "BackendUnavailable",
    "BehaviourLearner",
    "CloudRoutingRefused",
    "ComputeError",
    "ContextAssembler",
    "ContextBudget",
    "ContextOverflow",
    "DropStep",
    "Evidence",
    "LanguageSource",
    "NoBackendAvailable",
    "OllamaBackend",
    "ReasoningMode",
    "ReplyLanguage",
    "Router",
    "RoutingDecision",
    "RoutingPolicy",
    "Scene",
    "ScriptedBackend",
    "SensitivityLedger",
    "Slot",
    "TaintBackstopTriggered",
    "ThinkPolicy",
    "ToolBudgetExceeded",
    "TurnOrchestrator",
    "TurnOutcome",
    "TurnStats",
    "detect_injection_markers",
    "detect_matrix_language",
    "diacritics_report",
    "render_blocks",
    "resolve_reply_language",
    "restore_diacritics",
    "stream_reply",
    "taint_of",
]
