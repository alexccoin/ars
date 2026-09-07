"""Clinical reference, from AresMed.

Alex asked for specific medical answers and pointed at his own system,
`aresmed-guardian-chain`: a curated corpus of articles and protocols with authors, named
institutions, evidence levels, review dates, contraindications and urgency ratings. That
distinction is the entire reason this skill exists. A 14-billion-parameter model asked
about a drug interaction will produce a fluent paragraph from its weights, and neither it
nor the reader can tell which parts are remembered and which are invented. This returns
what a named clinician wrote, when it was last reviewed, and what it says not to do.

So the skill only ever RETURNS the corpus. It never summarises, never merges two
protocols, never fills a gap. If the corpus has nothing, that is the answer — "your
reference does not cover this" is useful, and a confident paragraph in its place is not.

Everything here is stamped EXTERNAL, like the web skill, and for the same reason: it is
text A.R.S did not write. An article body is a perfectly good place to hide an
instruction, and a medical corpus is a place a reader is unusually inclined to trust.

Configuration is deliberately absent from the repository. The endpoint comes from
ARS_MEDICAL_URL and the key from the token vault under `aresmed`, so a checkout carries
no route to anyone's patient data — see `SkillContext.secret`.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ars_protocol import (
    SUPPORTED_LANGUAGES,
    Capability,
    Provenance,
    SkillManifest,
    SourceKind,
    ToolParam,
    ToolSpec,
)
from ars_protocol import SkillRuntime as RT

from ..base import Skill, SkillError
from ..context import SkillContext, external

MAX_ROWS = 5
MAX_CHARS = 6_000
"""Per article. A protocol runs long, and the part that matters — the steps, the
contraindications — is not always in the first paragraph."""

ARTICLE_FIELDS = (
    "title,summary,content,medical_specialty,evidence_level,references,"
    "author_name,author_credentials,institution,last_reviewed"
)
PROTOCOL_FIELDS = (
    "title,description,protocol_type,urgency_level,medical_specialty,steps,"
    "contraindications,complications,medications,evidence_level,author_name,"
    "institution,last_updated,is_emergency_protocol"
)


class MedicalSkill(Skill):
    """Read-only access to a clinical reference. Never writes, never sees a patient."""

    @property
    def manifest(self) -> SkillManifest:
        host = _host(_base_url(required=False))
        return SkillManifest(
            name="medical",
            version="0.1.0",
            description="Look something up in the user's own clinical reference.",
            runtime=RT.PYTHON_INPROC,
            trusted=True,
            languages=SUPPORTED_LANGUAGES,
            capabilities=(Capability.MEDICAL_READ,),
            network_allowlist=(host,) if host else (),
            tools=(
                ToolSpec(
                    name="medical_reference",
                    description=(
                        "Search the user's clinical reference for articles on a condition, "
                        "drug or procedure. Returns what the reference says, with its "
                        "author, institution, evidence level and review date. It does not "
                        "diagnose and it is not a substitute for a clinician — report what "
                        "it says and cite it; never present it as your own conclusion, and "
                        "never fill a gap in it from your own knowledge."
                    ),
                    capabilities=(Capability.MEDICAL_READ,),
                    returns_external_content=True,
                    params=(
                        ToolParam(name="query", type="string",
                                  description="Condition, drug, symptom or procedure"),
                    ),
                ),
                ToolSpec(
                    name="medical_protocol",
                    description=(
                        "Find a clinical protocol by name or situation. Returns its steps, "
                        "contraindications, complications and urgency level verbatim. Read "
                        "the contraindications out; they are the part that matters most and "
                        "the part a summary drops."
                    ),
                    capabilities=(Capability.MEDICAL_READ,),
                    returns_external_content=True,
                    params=(
                        ToolParam(name="query", type="string",
                                  description="Protocol name or the situation it covers"),
                    ),
                ),
            ),
        )

    async def call(self, tool: str, args: dict[str, Any],
                   ctx: SkillContext) -> list[tuple[str, Provenance]]:
        query = str(args.get("query", "")).strip()
        if not query:
            raise SkillError(f"{tool} needs something to look up")
        if tool == "medical_reference":
            return await self._lookup(query, ctx, table="medical_knowledge_articles",
                                      fields=ARTICLE_FIELDS, render=_render_article)
        if tool == "medical_protocol":
            return await self._lookup(query, ctx, table="medical_protocols",
                                      fields=PROTOCOL_FIELDS, render=_render_protocol)
        raise SkillError(f"unknown tool {tool}")

    async def _lookup(self, query: str, ctx: SkillContext, *, table: str, fields: str,
                      render) -> list[tuple[str, Provenance]]:
        base = _base_url(required=True)
        key = await ctx.secret("aresmed") or os.environ.get("ARS_MEDICAL_KEY", "").strip()
        if not key:
            raise SkillError(
                "no credential for the medical reference — put it in the vault as "
                "'aresmed', or set ARS_MEDICAL_KEY"
            )
        ctx.check_cancelled()
        # Full-text-ish search over the title and body. Postgrest's `or` with `ilike`
        # rather than a single field, because "amiodarone" is as likely to be in the body
        # as in the title.
        pattern = f"%{query}%"
        resp = await ctx.http.get(
            f"{base}/rest/v1/{table}",
            params={
                "select": fields,
                "or": f"(title.ilike.{pattern},{'summary' if 'summary' in fields else 'description'}.ilike.{pattern})",
                "limit": str(MAX_ROWS),
            },
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
        )
        if resp.status_code != 200:
            raise SkillError(f"the medical reference returned {resp.status_code}")
        try:
            rows = resp.json()
        except ValueError as exc:
            raise SkillError("the medical reference returned something that is not JSON") from exc
        if not rows:
            # Not an error. "Your reference does not cover this" is a real answer, and the
            # alternative — the model filling the gap — is the thing this skill exists to
            # prevent.
            return [(
                f"The clinical reference has no entry matching {query!r}.",
                external(SourceKind.SKILL_OUTPUT, f"aresmed:{table}", label="medical reference"),
            )]
        return [
            (render(row)[:MAX_CHARS],
             external(SourceKind.SKILL_OUTPUT, f"aresmed:{table}/{row.get('title', '')}",
                      label=_citation(row)))
            for row in rows
        ]


def _base_url(*, required: bool) -> str:
    url = (os.environ.get("ARS_MEDICAL_URL") or "").strip().rstrip("/")
    if not url and required:
        raise SkillError("ARS_MEDICAL_URL is not set — A.R.S has no clinical reference configured")
    return url


def _host(url: str) -> str | None:
    if not url:
        return None
    from urllib.parse import urlparse

    return urlparse(url).hostname


def _citation(row: dict[str, Any]) -> str:
    """Who says so, and when they last checked. Shown next to the answer."""
    bits = [str(row.get("title") or "untitled")]
    author = row.get("author_name")
    if author:
        credentials = row.get("author_credentials")
        bits.append(f"{author}{', ' + credentials if credentials else ''}")
    if row.get("institution"):
        bits.append(str(row["institution"]))
    reviewed = row.get("last_reviewed") or row.get("last_updated")
    if reviewed:
        bits.append(f"reviewed {reviewed}")
    return " — ".join(bits)[:120]


def _render_article(row: dict[str, Any]) -> str:
    parts = [f"# {row.get('title', '')}", _provenance_line(row)]
    if row.get("summary"):
        parts.append(f"Summary: {row['summary']}")
    parts.append(str(row.get("content") or ""))
    if row.get("references"):
        parts.append("References: " + "; ".join(map(str, row["references"])))
    return "\n\n".join(p for p in parts if p)


def _render_protocol(row: dict[str, Any]) -> str:
    parts = [
        f"# {row.get('title', '')}",
        _provenance_line(row),
        f"Urgency: {row.get('urgency_level', 'unstated')}"
        + ("  [EMERGENCY PROTOCOL]" if row.get("is_emergency_protocol") else ""),
        str(row.get("description") or ""),
    ]
    # Contraindications first among the lists. A protocol read out without them is worse
    # than no protocol, and anything further down a long block is likelier to be dropped.
    for label, field in (("Contraindications", "contraindications"),
                         ("Complications", "complications"),
                         ("Steps", "steps"),
                         ("Medications", "medications")):
        value = row.get(field)
        if not value:
            continue
        if isinstance(value, list | dict):
            value = json.dumps(value, ensure_ascii=False, indent=1)
        parts.append(f"{label}: {value}")
    return "\n\n".join(p for p in parts if p)


def _provenance_line(row: dict[str, Any]) -> str:
    bits = [b for b in (
        row.get("medical_specialty"),
        f"evidence level {row['evidence_level']}" if row.get("evidence_level") else None,
        _citation(row),
    ) if b]
    return " · ".join(map(str, bits))
