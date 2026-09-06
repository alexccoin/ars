"""A.R.S auth — grants, the guard, consent, the OAuth token vault, and the audit log.

This service is the only thing standing between "an assistant with a microphone and my
credentials" and "an assistant with a microphone and my credentials that a stranger's
email can drive". Everything else in A.R.S trusts it, so it is deliberately small,
deliberately boring, and every decision it makes is written down before it takes effect.

Wiring it up::

    store = await SqliteGrantStore(config.data_dir).open()
    audit = AuditLog(config.guard.guard_audit_log)
    guard = PolicyGuardEngine(grants=store, audit=audit, config=config.guard)

    decision = await guard.evaluate(query)          # audit line already on disk
    if decision.verdict is Verdict.ALLOW:
        result = await runtime.invoke(call)
        await guard.record_outcome(query, decision, outcome="ok")
"""

from .audit import AuditLog, default_redactor
from .guard import PolicyGuardEngine
from .messages import Msg, safe_fragment
from .policy import (
    NOT_INTERACTIVELY_GRANTABLE,
    default_confirm_policy,
    is_interactively_grantable,
    requires_narrow_scope,
    sensitive,
    silent_use_permitted,
)
from .session import RateLimit, RateLimiter, RateLimitPolicy, SessionConsentLedger
from .store import GrantConflictError, SqliteGrantStore
from .vault import (
    Protection,
    TokenLeakError,
    TokenPresenter,
    TokenRef,
    TokenVault,
    TokenVaultError,
    VaultLockedError,
)

__all__ = [
    "NOT_INTERACTIVELY_GRANTABLE",
    "AuditLog",
    "GrantConflictError",
    "Msg",
    "PolicyGuardEngine",
    "Protection",
    "RateLimit",
    "RateLimitPolicy",
    "RateLimiter",
    "SessionConsentLedger",
    "SqliteGrantStore",
    "TokenLeakError",
    "TokenPresenter",
    "TokenRef",
    "TokenVault",
    "TokenVaultError",
    "VaultLockedError",
    "default_confirm_policy",
    "default_redactor",
    "is_interactively_grantable",
    "requires_narrow_scope",
    "safe_fragment",
    "sensitive",
    "silent_use_permitted",
]
