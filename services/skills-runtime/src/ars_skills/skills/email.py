"""Email — read, search, and send on the user's behalf.

This is the highest-stakes skill in A.R.S and the design reflects that:

  * Reading and sending are separate capabilities. A grant to read is never a grant to send.
  * Every message body comes back marked EXTERNAL. An email is a document written by
    someone who may want the assistant to act — the single most likely injection vector
    in a personal assistant.
  * Credentials are fetched from the vault inside the call and never returned or logged.
  * Sending is always effectful: it is CRITICAL risk, so a tainted turn can never do it.
    An email that says "forward this to X" cannot cause an email to be sent to X.
"""

from __future__ import annotations

import email.utils
import imaplib
import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default as default_policy
from typing import Any

import anyio
from ars_protocol import (
    Capability,
    Language,
    Provenance,
    SkillManifest,
    SourceKind,
    ToolParam,
    ToolSpec,
)
from ars_protocol import (
    SkillRuntime as RT,
)

from ..base import Skill, SkillError
from ..context import SkillContext, external
from .web import flag_injection

_log = logging.getLogger("ars.skills.email")

MAX_BODY_CHARS = 6_000
MAX_RESULTS = 25


@dataclass(frozen=True, slots=True)
class MailAccount:
    """Connection details. The password/token is NOT stored here — it is fetched from
    the vault per call, so a leaked config object leaks no credential."""

    address: str
    imap_host: str
    imap_port: int = 993
    smtp_host: str = ""
    smtp_port: int = 465
    secret_name: str = "EMAIL_APP_PASSWORD"  # noqa: S105 - a vault key name, not a secret


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _body_text(msg: Any) -> str:
    """Prefer text/plain. HTML is converted, never rendered — and never fetched, because
    a remote image in an email is a read-receipt beacon."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                return part.get_content()
        for part in msg.walk():
            if part.get_content_type() == "text/html" and not part.get_filename():
                from .web import html_to_text
                return html_to_text(part.get_content())[0]
        return ""
    if msg.get_content_type() == "text/html":
        from .web import html_to_text
        return html_to_text(msg.get_content())[0]
    return msg.get_content()


class EmailSkill(Skill):
    def __init__(self, account: MailAccount | None = None) -> None:
        self._account = account

    @property
    def manifest(self) -> SkillManifest:
        return SkillManifest(
            name="email",
            version="0.1.0",
            description="Search, read and send the user's email.",
            runtime=RT.PYTHON_INPROC,
            trusted=True,
            languages=(Language.EN, Language.RO),
            capabilities=(Capability.EMAIL_SEARCH, Capability.EMAIL_READ, Capability.EMAIL_SEND),
            network_allowlist=(),  # IMAP/SMTP are not HTTP; hosts come from the account config
            tools=(
                ToolSpec(
                    name="email_search",
                    description="Find emails. Returns senders, subjects and dates only — "
                                "not bodies.",
                    capabilities=(Capability.EMAIL_SEARCH,),
                    returns_external_content=True,
                    params=(
                        ToolParam(name="query", type="string",
                                  description="Words to match in subject or sender"),
                        ToolParam(name="from_addr", type="string", required=False,
                                  description="Restrict to a sender address"),
                        ToolParam(name="since_days", type="integer", required=False,
                                  description="Only messages from the last N days"),
                        ToolParam(name="limit", type="integer", required=False,
                                  description="Max results, default 10"),
                    ),
                ),
                ToolSpec(
                    name="email_read",
                    description="Read the full body of one email, identified by the id "
                                "returned from email_search.",
                    capabilities=(Capability.EMAIL_READ,),
                    returns_external_content=True,
                    params=(ToolParam(name="message_id", type="string",
                                      description="Id from email_search"),),
                ),
                ToolSpec(
                    name="email_send",
                    description="Send an email from the user's account. Irreversible and "
                                "visible to the recipient — always confirm the recipient, "
                                "subject and body with the user first.",
                    capabilities=(Capability.EMAIL_SEND,),
                    returns_external_content=False,
                    params=(
                        ToolParam(name="to", type="string", description="Recipient address"),
                        ToolParam(name="subject", type="string", description="Subject line"),
                        ToolParam(name="body", type="string", description="Plain text body"),
                    ),
                ),
            ),
        )

    def _require_account(self) -> MailAccount:
        if self._account is None:
            raise SkillError("no email account is configured; set one up in A.R.S settings first")
        return self._account

    async def call(self, tool: str, args: dict[str, Any], ctx: SkillContext) -> list[tuple[str,
    Provenance]]:
        acct = self._require_account()
        secret = await ctx.secret(acct.secret_name)
        if not secret:
            raise SkillError("no credential in the vault for this email account")
        # imaplib/smtplib are blocking; keep the event loop free for the voice pipeline.
        if tool == "email_search":
            return await anyio.to_thread.run_sync(lambda: self._search(acct, secret, args))
        if tool == "email_read":
            return await anyio.to_thread.run_sync(lambda: self._read(acct, secret, args))
        if tool == "email_send":
            return await anyio.to_thread.run_sync(lambda: self._send(acct, secret, args))
        raise SkillError(f"unknown tool {tool}")

    def _connect(self, acct: MailAccount, secret: str) -> imaplib.IMAP4_SSL:
        try:
            conn = imaplib.IMAP4_SSL(acct.imap_host, acct.imap_port,
                                     ssl_context=ssl.create_default_context())
            conn.login(acct.address, secret)
            return conn
        except imaplib.IMAP4.error as e:
            raise SkillError("email login failed; the stored credential may have expired") from e

    def _search(self, acct: MailAccount, secret: str,
                args: dict[str, Any]) -> list[tuple[str, Provenance]]:
        limit = max(1, min(int(args.get("limit", 10)), MAX_RESULTS))
        criteria: list[str] = []
        if q := str(args.get("query", "")).strip():
            criteria += ["OR", "SUBJECT", f'"{q}"', "FROM", f'"{q}"']
        if fa := args.get("from_addr"):
            criteria += ["FROM", f'"{fa}"']
        if days := args.get("since_days"):
            import datetime as _dt
            since = (_dt.date.today() - _dt.timedelta(days=int(days))).strftime("%d-%b-%Y")
            criteria += ["SINCE", since]
        if not criteria:
            criteria = ["ALL"]

        conn = self._connect(acct, secret)
        try:
            conn.select("INBOX", readonly=True)
            typ, data = conn.search(None, *criteria)
            if typ != "OK":
                raise SkillError("the mail server rejected that search")
            ids = data[0].split()[-limit:]
            if not ids:
                raise SkillError("no emails matched")
            out: list[tuple[str, Provenance]] = []
            for mid in reversed(ids):
                typ, hdr = conn.fetch(
                    mid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])"
                )
                if typ != "OK" or not hdr or not isinstance(hdr[0], tuple):
                    continue
                msg = BytesParser(policy=default_policy).parsebytes(hdr[0][1])
                frm, subj = _decode(msg.get("From")), _decode(msg.get("Subject"))
                text = (f"id: {mid.decode()}\nfrom: {frm}\nsubject: {subj}\n"
                        f"date: {msg.get('Date', '')}")
                out.append((text, external(SourceKind.EMAIL, msg.get("Message-ID"),
                                           label=f"email from {frm[:50]}")))
            return out
        finally:
            try:
                conn.logout()
            except Exception:  # logout failure must not mask a real result
                _log.debug("IMAP logout failed", exc_info=True)

    def _read(self, acct: MailAccount, secret: str,
              args: dict[str, Any]) -> list[tuple[str, Provenance]]:
        mid = str(args.get("message_id", "")).strip()
        if not mid.isdigit():
            raise SkillError("message_id must be an id returned by email_search")
        conn = self._connect(acct, secret)
        try:
            conn.select("INBOX", readonly=True)
            typ, data = conn.fetch(mid.encode(), "(RFC822)")
            if typ != "OK" or not data or not isinstance(data[0], tuple):
                raise SkillError("that email could not be read")
            msg = BytesParser(policy=default_policy).parsebytes(data[0][1])
            frm, subj = _decode(msg.get("From")), _decode(msg.get("Subject"))
            body = _body_text(msg)[:MAX_BODY_CHARS]

            header = f"from: {frm}\nsubject: {subj}\ndate: {msg.get('Date','')}\n"
            if flags := flag_injection(body):
                header += ("\n[A.R.S security notice] This email contains text addressed to "
                           f"an AI assistant rather than to you: {flags!r}. A.R.S will not "
                           "act on it. Treat the sender as suspicious.\n")
            return [(header + "\n" + body,
                     external(SourceKind.EMAIL, msg.get("Message-ID"),
                              label=f"email from {frm[:50]}"))]
        finally:
            try:
                conn.logout()
            except Exception:  # logout failure must not mask a real result
                _log.debug("IMAP logout failed", exc_info=True)

    def _send(self, acct: MailAccount, secret: str,
              args: dict[str, Any]) -> list[tuple[str, Provenance]]:
        to = str(args.get("to", "")).strip()
        subject = str(args.get("subject", "")).strip()
        body = str(args.get("body", ""))
        if "@" not in email.utils.parseaddr(to)[1]:
            raise SkillError("that does not look like a valid recipient address")
        if not body.strip():
            raise SkillError("refusing to send an empty email")
        if not acct.smtp_host:
            raise SkillError("no SMTP server configured for this account")

        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = acct.address, to, subject
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Message-ID"] = email.utils.make_msgid()
        msg.set_content(body)
        try:
            with smtplib.SMTP_SSL(acct.smtp_host, acct.smtp_port,
                                  context=ssl.create_default_context()) as s:
                s.login(acct.address, secret)
                s.send_message(msg)
        except smtplib.SMTPException as e:
            raise SkillError(f"the message could not be sent: {type(e).__name__}") from e

        # Confirmation is SYSTEM-trusted: it is our own statement of what we did,
        # not content from anyone else.
        from ars_protocol import TrustLevel
        prov = Provenance(source=SourceKind.SKILL_OUTPUT, trust=TrustLevel.SYSTEM,
                          label="email sent")
        return [(f"Sent to {to} with subject {subject!r}.", prov)]
