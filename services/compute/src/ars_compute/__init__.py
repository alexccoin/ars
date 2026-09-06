"""A.R.S reasoning layer.

  * `context`   — provenance-preserving prompt assembly, quarantine framing, taint, budget
  * `backends`  — Ollama (local), Anthropic (cloud), Scripted (tests), Router (policy)
  * `turn`      — the orchestrator: stream, guard, act, feed back, stream again
  * `learning`  — behaviour observations, proposed to the user, never self-confirmed
  * `language`  — per-utterance reply language, code-switching, Romanian diacritics
  * `prompts`   — versioned prompt assets; no prompt string exists in this Python source
"""

from .backends import (
    AnthropicBackend,
    OllamaBackend,
    Router,
    RoutingDecision,
    RoutingPolicy,
    Scene,
    ScriptedBackend,
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
from .sensitivity import SensitivityLedger
from .turn import TurnOrchestrator, TurnOutcome, TurnStats

__version__ = "0.1.0"

__all__ = [
    "AnthropicBackend", "AssembledContext", "BackendUnavailable", "BehaviourLearner",
    "CloudRoutingRefused", "ComputeError", "ContextAssembler", "ContextBudget",
    "ContextOverflow", "DropStep", "Evidence", "LanguageSource", "NoBackendAvailable",
    "OllamaBackend", "ReplyLanguage", "Router", "RoutingDecision", "RoutingPolicy",
    "Scene", "ScriptedBackend", "SensitivityLedger", "Slot", "TaintBackstopTriggered",
    "ToolBudgetExceeded", "TurnOrchestrator", "TurnOutcome", "TurnStats",
    "detect_injection_markers", "detect_matrix_language", "diacritics_report",
    "render_blocks", "resolve_reply_language", "restore_diacritics", "taint_of",
]
