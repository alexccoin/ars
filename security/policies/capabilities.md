# Capability policy

*Owner: security-engineer. Last reviewed: 2026-09-06 against `services/auth` @ initial.*

The authoritative list of capabilities is `packages/protocol/src/ars_protocol/capability.py`.
Risk, "touches private data" and "is effectful" are **read from that enum**, never restated
here — this document explains the consequences and records the judgement calls that the
protocol does not encode. The table below is generated from the running code
(`ars_auth.policy`, `ars_auth.session.RateLimitPolicy`); if it disagrees with the code, the
code is right and this file is stale.

## The table

| Capability | Risk | Private | Effectful | Default confirm | Grantable mid-task | `never` honoured | Rate limit (turn / session) |
|---|---|---|---|---|---|---|---|
| `web.search` | low | no | no | `never` | yes | yes | 4 / 64 |
| `web.fetch` | low | no | no | `never` | yes | yes | 4 / 64 |
| `github.read_public` | low | no | no | `never` | yes | yes | 4 / 64 |
| `email.read` | high | yes | no | `every_use` | yes | no | 4 / 64 |
| `email.search` | high | yes | no | `every_use` | yes | no | 4 / 64 |
| `calendar.read` | medium | yes | no | `once_per_session` | yes | no | 4 / 64 |
| `contacts.read` | high | yes | no | `every_use` | yes | no | 4 / 64 |
| `github.read_private` | high | yes | no | `every_use` | yes | no | 4 / 64 |
| `files.read` | high | yes | no | `every_use` | yes | no | 4 / 64 |
| `email.send` | critical | no | yes | `every_use` | yes | no | 1 / 8 |
| `calendar.write` | high | no | yes | `every_use` | yes | no | 2 / 24 |
| `files.write` | high | no | yes | `every_use` | yes | no | 2 / 24 |
| `github.write` | critical | no | yes | `every_use` | yes | no | 1 / 8 |
| `app.control` | high | yes | yes | `every_use` | yes | no | 2 / 24 |
| `shell.exec` | critical | no | yes | `every_use` | **no** | no | 1 / 8 |
| `network.egress` | medium | no | no | `once_per_session` | **no** | no | 4 / 64 |
| `memory.read` | low | yes | no | `never` | yes | yes | 4 / 64 |
| `memory.write` | medium | no | yes | `every_use` | yes | no | 2 / 24 |

**Default confirm** is what the UI pre-selects when the user creates a grant. It is not
enforcement — the stored grant is. Defaults matter anyway, because most grants end up being
the default.

**Grantable mid-task** — whether the guard will raise a consent prompt for this capability
when no grant exists, or refuse with `DENY(no_grant)` and require a permissions screen.

**`never` honoured** — whether a stored grant with `confirm=never` actually buys silent
use. Per `capability.py`, `never` is "only ever appropriate for LOW risk", and
`ars_auth.policy.silent_use_permitted` enforces exactly that: a `confirm=never` grant on a
MEDIUM-or-higher capability is upgraded to ASK at decision time rather than obeyed. A bad
grant — from a config file, a migration, a UI bug, or someone talking the user through
"just set it to never" — cannot buy silent access to anything that matters.
(`test_confirm_never_is_ignored_above_low_risk`.)

## The judgement calls

### `shell.exec` and `network.egress` are never granted mid-task

These are the two capabilities that turn a scoped assistant into a general-purpose remote
shell and a general-purpose exfiltration channel. Everything else in the table has a
bounded blast radius described by its resource pattern; these two do not.

A mid-task consent prompt is a bad place to approve them, for two reasons. It can be
answered by anyone the microphone can hear — `GrantSource.VOICE` exists because voice
grants are convenient and therefore suspect. And it arrives while the user is thinking
about the task, not about permissions, which is precisely the state in which people
approve things.

So the guard refuses with `DENY(no_grant)` and does not offer to ask. Granting them
requires the UI or config, with the user looking at a permissions screen.
(`ars_auth.policy.NOT_INTERACTIVELY_GRANTABLE`, `test_nothing_implies_shell_exec`.)

### Tainted turns: CRITICAL denies, everything else asks

If a turn has consumed `EXTERNAL` content and the capability touches private data or is
effectful, it can never end in a silent ALLOW:

| Risk in a tainted turn | Verdict |
|---|---|
| CRITICAL | `DENY(tainted_turn)` — outright, regardless of the grant |
| HIGH | `ASK`, prompt composed by the guard, naming the taint source |
| MEDIUM | `ASK`, same |
| LOW *and* private or effectful (`memory.read`) | `ASK`, same |
| LOW and neither (`web.search`, `web.fetch`, `github.read_public`) | unaffected |

The spec for this service said "HIGH/MEDIUM → ASK". LOW-risk-but-private is extended to ASK
as well, because the rule that matters is "a tainted turn never gets a silent ALLOW on
anything private or effectful", and `memory.read` in a tainted turn is the first step of an
exfiltration, not a harmless lookup. Taint is not a general stop: a turn that read a web
page can still search the web (`test_tainted_turn_does_not_block_harmless_capabilities`).

### A revoked or expired grant denies; it does not re-ask

"No matching active grant → ASK if grantable" is read as: *no grant at all* asks, *a grant
you took back* does not. Re-asking after a revocation is consent farming — drive repeated
attempts until the user taps yes to make the prompt stop. The user already made that
decision; undoing it belongs on a permissions screen.
`DENY(grant_revoked)` and `DENY(grant_expired)` both say so in words, in both languages,
and point at settings. (`test_revoked_grant_denies`, `test_expired_grant_denies`.)

### Overlapping grants resolve to the most restrictive

When more than one active grant covers a call, `SqliteGrantStore.find` picks by confirm
strictness first, then by how narrowly the pattern matches, then by recency. Two
permissions must never combine into more access than either gave alone: a wildcard
`once_per_session` grant plus a narrow `every_use` grant means the narrow resource asks
every time. (`test_most_restrictive_overlapping_grant_wins`.)

### Rate limits exist for the model, not the user

The failure mode is an agent in a loop, not a malicious user. CRITICAL capabilities get one
attempt per turn: doing an irreversible, externally-visible thing twice in one turn is
always a bug. Denials do not consume budget, so being refused twenty times does not exhaust
the allowance for a legitimate call.

### Wildcard scope on HIGH and CRITICAL deserves a warning

`ars_auth.policy.requires_narrow_scope` marks HIGH and CRITICAL capabilities as ones where a
`("*",)` grant should be called out in the UI. Advisory only — the guard honours a wildcard
grant the user genuinely made — but `files.read` over `*` is the whole disk, including
`~/.ssh`, and the person clicking it should be told so in words.

## What a grant is scoped against

`resource_patterns` are `fnmatch` globs matched case-sensitively against the tool call's
`resource`. The conventions in use:

| Capability | `resource` looks like |
|---|---|
| `email.read`, `email.search` | a mailbox query: `from:bank.ro`, `in:inbox`, `subject:invoice` |
| `email.send` | `to:maria@example.com` |
| `files.read`, `files.write` | an absolute path: `/Users/alex/Documents/tax/*` |
| `web.fetch` | a URL |
| `github.*` | `owner/repo` |
| `calendar.*` | `cal:primary` |
| `memory.*` | a record kind or key prefix: `pref:*` |
| `shell.exec` | the command line |

A call with `resource=None` means "everything", and only a grant containing the literal
`"*"` pattern covers it (`CapabilityGrant.covers`,
`test_resource_none_does_not_match_a_narrow_grant`). The guard matches the `resource` the
caller declares; the skills-runtime is responsible for checking that what the skill
actually does matches what it declared.

## Data classification (auth service)

| Data | Location | Encryption at rest | Retention | Deletion path |
|---|---|---|---|---|
| Capability grants | `{data_dir}/grants.db`, 0600 | no (filesystem only) | forever, by design — the consent history is the point | `revoke()` appends a revocation; rows are never removed |
| Token metadata (`TokenRef`) | `{data_dir}/vault.db`, 0600 | no — contains no secret | until revoked | `TokenVault.revoke` marks it, keeps the history |
| OAuth token material | macOS Keychain, or `{data_dir}/vault.keys/*.fernet` 0600 | Keychain, or Fernet with the key in the Keychain | until revoked or expired | `TokenVault.revoke` deletes the Keychain item and unlinks the ciphertext — tested |
| Vault master key (fallback only) | Keychain, or `{data_dir}/vault.keys/master.key` 0600 | n/a | until rotated | delete the file; all ciphertexts become undecryptable |
| Audit log | `{data_dir}/audit.jsonl`, 0600 | no | **undefined — see below** | **none — see below** |
| Session consents, rate counters | memory only | n/a | process lifetime | process exit, or `end_session()` |

**Open finding: the audit log has no retention or deletion path.** It grows without bound
and nothing removes it. Under this project's own standard ("we'll figure out deletion
later" is a finding), that is a finding against this service.

`security/policies/retention.md` exists but is scoped to `MemoryRecord` in
`services/memory`; it says nothing about the audit log, and the two cannot simply share a
cap. Memory retention is keyed on `Sensitivity` and is about forgetting facts about the
user. The audit log is the opposite kind of record: it is the user's evidence of what
their assistant did, and deleting it early destroys the ability to answer "what did it do
with my email last Tuesday" — the question the log exists for. A short window protects
privacy; a long window protects accountability, and that trade-off is the user's to make.

Proposed, not implemented: a configurable window defaulting to 400 days (long enough to
cover an annual review), daily rotation, and rotated files deleted rather than compressed
and kept. It needs a decision from the product owner before it is code. Until then, the
audit log is the one asset in this table without a documented lifecycle.

## Third parties that receive user data

The auth service itself contacts no third party. It mediates access to those that do:

| Provider | Receives | Justification | Redaction rule |
|---|---|---|---|
| Google (Gmail, Calendar) | OAuth token; the user's own queries and message content | the user asked A.R.S to use their mailbox and calendar | tokens never enter logs or model context; audit records the query, not the content |
| GitHub | OAuth token; repo reads and writes | the user asked A.R.S to work with their repositories | as above |

Each is reachable only through a grant, only with a token registered for that capability,
and only via `TokenVault.use` under an ALLOW. A skill fetching from either tags its output
`TrustLevel.EXTERNAL`, which taints the turn.

Cloud LLM backends are **not** in this table because the auth service never routes context
to them. That boundary (`SENSITIVE` records refused, redaction applied) belongs to
`services/compute` and needs its own entry when that service lands.
