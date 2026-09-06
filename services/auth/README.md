# `services/auth` — the guard

Grants, consent, the OAuth token vault, and the audit log. Everything else in A.R.S trusts
this service, so it is deliberately small and every decision it makes is written down
before it takes effect.

| Piece | Class | Implements |
|---|---|---|
| Consent record | `SqliteGrantStore` | `ars_core.interfaces.GrantStore` |
| The decision | `PolicyGuardEngine` | `ars_core.interfaces.GuardEngine` |
| The record of it | `AuditLog` | — |
| Credentials | `TokenVault` | — |
| User-facing text | `messages.py` | EN + RO, guard-owned |

## Decision order

Six stages. Each may only make the answer **more** restrictive than the stage before it
(ALLOW > ASK > DENY), so the outcome is always the strictest rule that applies.

1. **Guard disabled** — anything private or effectful is `DENY(guard_disabled_capability)`.
   The kill switch removes access, not checking.
2. **Grant resolution** — no grant at all: ASK if the capability may be requested
   interactively, else `DENY(no_grant)`. All grants revoked: `DENY(grant_revoked)`. All
   expired: `DENY(grant_expired)`.
3. **Resource scope** — an active grant exists but none covers this resource:
   `DENY(resource_out_of_scope)`. A grant is never widened to fit the call in front of it.
4. **Taint** — tainted turn + (private or effectful): CRITICAL is `DENY(tainted_turn)`,
   everything else becomes ASK with a guard-composed prompt naming the taint source. This
   is the prompt-injection defence.
5. **Confirm policy** — `every_use` asks; `once_per_session` asks once per session;
   `never` allows, but only for LOW risk.
6. **Rate limit** — per capability, per turn and per session. Denials do not consume budget.

## Wiring

```python
store = await SqliteGrantStore(config.data_dir).open()
audit = AuditLog(config.guard.guard_audit_log)
guard = PolicyGuardEngine(grants=store, audit=audit, config=config.guard)
guard.set_session_language(session.id, Language.RO)   # bilingual, both always present

decision = await guard.evaluate(query)          # audit line is already fsync'd on disk
if decision.verdict is Verdict.ALLOW:
    result = await runtime.invoke(call)
    await guard.record_outcome(query, decision, outcome="ok")
```

Tokens are never handed out. A skill spends one inside a callback:

```python
async def call_github(presenter):
    headers = presenter.authorize({"Accept": "application/vnd.github+json"})
    ...
    return parsed          # scanned; TokenLeakError if it contains the credential

await vault.use(ref.id, decision=decision, fn=call_github)
```

## Layout

```
src/ars_auth/
  guard.py       PolicyGuardEngine — the decision pipeline
  store.py       SqliteGrantStore — append-only, in-memory hot path
  audit.py       AuditLog — append-only JSONL, fsync'd, written before the action
  vault.py       TokenVault — Keychain / Fernet, no read path to the reasoning layer
  messages.py    EN + RO templates; the guard never asks the model for words
  policy.py      the judgement calls (what may be granted mid-task, when `never` is honoured)
  session.py     session consents and rate counters, in memory only
  migrations.py  applies data/migrations/<component>/*.sql
data/migrations/ the schema, reviewable without reading Python
tests/unit/      adversarial tests
```

Docs: [`security/threat-models/guard.md`](../../security/threat-models/guard.md),
[`security/policies/capabilities.md`](../../security/policies/capabilities.md).

```
.venv/bin/python -m pytest services/auth/tests -q
```
