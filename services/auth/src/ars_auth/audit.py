"""The audit log — append-only JSONL, fsync'd, written before the action.

This file is how the user answers "what did it do with my email last Tuesday". That
makes it a security control, not telemetry, and it has three properties that follow from
that:

**Written before, not after.** :meth:`AuditLog.begin` is called by the guard as part of
``evaluate``, before any side effect exists. A skill that hangs, crashes the process, or
succeeds and then loses power still leaves a record that it was *about* to touch the
user's mailbox. A log written after the fact only records the actions that went well,
which is exactly the wrong half.

**Never rewritten.** The outcome arrives later, so it is a second line referencing the
first, not an edit of it. :meth:`read` folds the two. Rewriting in place would mean a bug
(or an attacker with write access) could quietly change history; with append-only, the
worst they can do is append, and the original line is still there.

**Never a second copy of the user's data.** Identifiers, capability, resource, verdict,
reason. No email bodies, no file contents, no tool payloads, no model text. If the audit
log leaks, it reveals that A.R.S read mail from ``from:bank.ro`` at 14:02 - not what the
mail said. Free-text fields (``resource``, ``outcome``) are length-capped, stripped of
control characters, and passed through a redactor that removes anything shaped like a
credential, because a mailbox query is user-influenced text and the log is not the place
to find out we were wrong about that.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import unicodedata
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Final

from ars_protocol import AuditRecord, Capability, DenyReason, Risk, Verdict

SCHEMA_VERSION: Final = 1

KIND_DECISION: Final = "decision"
KIND_OUTCOME: Final = "outcome"

MAX_RESOURCE_CHARS: Final = 512
"""Resources are identifiers - a URL, a mailbox query, a file path. Anything longer is
being used to smuggle content into the log."""

MAX_OUTCOME_CHARS: Final = 160
"""Outcome is "ok" or an error class. Not a stack trace, and definitely not a response
body."""

_CONTROL_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cs", "Cn"})

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # query/kv params whose name says "secret"
    re.compile(r"(?i)\b(access_token|refresh_token|id_token|api[_-]?key|apikey|token|"
               r"secret|password|passwd|pwd|authorization|auth|session|sig|signature)"
               r"\s*[=:]\s*[^\s&;]+"),
    # bearer tokens
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}"),
    # provider-shaped credentials
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"\bsk-[A-Za-z0-9._\-]{16,}"),
    re.compile(r"\bya29\.[A-Za-z0-9._\-]{10,}"),
    # a long opaque blob is not an identifier anyone needs in an audit log
    re.compile(r"\b[A-Za-z0-9+/_\-]{48,}={0,2}\b"),
)

Redactor = Callable[[str], str]


def default_redactor(text: str) -> str:
    """Remove anything credential-shaped from a free-text field.

    Deliberately aggressive. Losing the tail of a URL in the audit log is a nuisance;
    writing an OAuth token into a plaintext file that exists to be read by the user's
    lawyer is a breach.
    """
    out = text
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(lambda m: _redact_match(m.group(0)), out)
    return out


def _redact_match(matched: str) -> str:
    if "=" in matched or ":" in matched:
        head, sep, _ = matched.partition("=" if "=" in matched else ":")
        return f"{head}{sep}[redacted]"
    return "[redacted]"


def _clean(text: str | None, *, max_chars: int, redactor: Redactor) -> str | None:
    """Strip control characters, redact secrets, collapse whitespace, truncate.

    Control characters matter here specifically: this is a line-delimited format, and a
    newline inside ``resource`` would let a caller forge audit lines. ``json.dumps``
    escapes them anyway - this is the second of the two belts.
    """
    if text is None:
        return None
    normalised = unicodedata.normalize("NFKC", text)
    stripped = "".join(ch for ch in normalised
                       if unicodedata.category(ch) not in _CONTROL_CATEGORIES)
    collapsed = " ".join(stripped.split())
    redacted = redactor(collapsed)
    if len(redacted) > max_chars:
        redacted = redacted[: max_chars - 1] + "…"
    return redacted


class AuditLog:
    """Append-only JSONL audit log.

    Usage from the guard::

        record = await audit.begin(AuditRecord(...))     # before the action
        ...                                              # action runs
        await audit.complete(record.id, outcome="ok")    # after

    Every write is followed by ``flush`` + ``os.fsync``. That costs on the order of a
    millisecond on APFS, and it is spent deliberately: a decision record that is still in
    the page cache when the machine loses power did not happen, as far as the user's
    ability to audit their own assistant is concerned.
    """

    def __init__(self, path: Path | str, *, redactor: Redactor = default_redactor) -> None:
        self.path = Path(path)
        self._redactor = redactor
        self._lock = asyncio.Lock()
        self._ensure_file()

    # ------------------------------------------------------------------ writing

    def _ensure_file(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        if not self.path.exists():
            # 0600 from the moment it exists; never a window where it is world-readable.
            fd = os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            os.close(fd)
        else:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def _append_sync(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

    async def _append(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            await asyncio.to_thread(self._append_sync, payload)

    async def begin(self, record: AuditRecord) -> AuditRecord:
        """Write the decision line. Call this *before* the action executes."""
        payload: dict[str, Any] = {
            "v": SCHEMA_VERSION,
            "kind": KIND_DECISION,
            "id": record.id,
            "at_ms": record.at_ms,
            "session_id": record.session_id,
            "turn_id": record.turn_id,
            "capability": str(record.capability),
            "resource": _clean(record.resource, max_chars=MAX_RESOURCE_CHARS,
                               redactor=self._redactor),
            "verdict": str(record.verdict),
            "reason": str(record.reason) if record.reason is not None else None,
            "risk": str(record.risk),
            "tainted": record.tainted,
            "grant_id": record.grant_id,
            "user_confirmed": record.user_confirmed,
            "outcome": None,
        }
        await self._append(payload)
        return record

    async def complete(self, record_id: str, *, outcome: str,
                       user_confirmed: bool | None = None, at_ms: int | None = None) -> None:
        """Append the outcome of a previously-begun decision.

        This is an append, not an update: the decision line stays exactly as written.
        """
        from ars_protocol import now_ms

        payload: dict[str, Any] = {
            "v": SCHEMA_VERSION,
            "kind": KIND_OUTCOME,
            "id": record_id,
            "at_ms": at_ms if at_ms is not None else now_ms(),
            "outcome": _clean(outcome, max_chars=MAX_OUTCOME_CHARS, redactor=self._redactor),
            "user_confirmed": user_confirmed,
        }
        await self._append(payload)

    # ------------------------------------------------------------------ reading

    def _read_lines(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rows.append(json.loads(raw))
                except json.JSONDecodeError:
                    # A torn final line from a hard kill. Skip it; never abort the read,
                    # because the user asking "what did it do" must still get an answer.
                    continue
        return rows

    async def read(
        self,
        *,
        since_ms: int | None = None,
        until_ms: int | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        capability: Capability | Iterable[Capability] | None = None,
        verdict: Verdict | None = None,
        resource_glob: str | None = None,
        tainted_only: bool = False,
        limit: int | None = None,
    ) -> tuple[AuditRecord, ...]:
        """Read the log back, decision lines folded with their outcome lines.

        This is the "what did it do with my email last Tuesday" query::

            await audit.read(since_ms=tuesday_00, until_ms=wednesday_00,
                             capability=[Capability.EMAIL_READ, Capability.EMAIL_SEND])

        Ordered oldest first. ``limit`` keeps the most recent N.
        """
        rows = await asyncio.to_thread(self._read_lines)

        caps: frozenset[str] | None = None
        if capability is not None:
            caps = (frozenset({str(capability)}) if isinstance(capability, Capability)
                    else frozenset(str(c) for c in capability))

        decisions: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for row in rows:
            kind = row.get("kind")
            rid = row.get("id")
            if not isinstance(rid, str):
                continue
            if kind == KIND_DECISION:
                if rid not in decisions:
                    order.append(rid)
                decisions[rid] = row
            elif kind == KIND_OUTCOME and rid in decisions:
                base = decisions[rid]
                base["outcome"] = row.get("outcome")
                if row.get("user_confirmed") is not None:
                    base["user_confirmed"] = row.get("user_confirmed")

        out: list[AuditRecord] = []
        for rid in order:
            row = decisions[rid]
            at = int(row.get("at_ms", 0))
            if since_ms is not None and at < since_ms:
                continue
            if until_ms is not None and at >= until_ms:
                continue
            if session_id is not None and row.get("session_id") != session_id:
                continue
            if turn_id is not None and row.get("turn_id") != turn_id:
                continue
            if caps is not None and row.get("capability") not in caps:
                continue
            if verdict is not None and row.get("verdict") != str(verdict):
                continue
            if tainted_only and not row.get("tainted"):
                continue
            if resource_glob is not None:
                res = row.get("resource")
                if not isinstance(res, str) or not fnmatch.fnmatchcase(res, resource_glob):
                    continue
            try:
                out.append(AuditRecord(
                    id=row["id"],
                    at_ms=at,
                    session_id=row["session_id"],
                    turn_id=row["turn_id"],
                    capability=Capability(row["capability"]),
                    resource=row.get("resource"),
                    verdict=Verdict(row["verdict"]),
                    reason=DenyReason(row["reason"]) if row.get("reason") else None,
                    risk=Risk(row["risk"]),
                    tainted=bool(row.get("tainted", False)),
                    grant_id=row.get("grant_id"),
                    user_confirmed=row.get("user_confirmed"),
                    outcome=row.get("outcome"),
                ))
            except (KeyError, ValueError):
                # A line from a future schema version, or a corrupted one. Skip, do not
                # crash the audit reader.
                continue

        if limit is not None and len(out) > limit:
            out = out[-limit:]
        return tuple(out)
