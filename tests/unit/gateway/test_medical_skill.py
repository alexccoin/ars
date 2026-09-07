"""The clinical reference skill.

Nothing here reaches a network. What is under test is the property that makes this skill
worth having: it returns the corpus and nothing else. A model asked about a drug produces
a fluent paragraph from its weights, and neither it nor the reader can tell which parts
are remembered and which are invented — so if this skill ever summarises, merges or fills
a gap, it has become the thing it was built to replace.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from ars_protocol import Capability
from ars_skills.base import SkillError
from ars_skills.context import SkillContext
from ars_skills.skills.medical import MedicalSkill

ARTICLE = {
    "title": "Amiodarone in atrial fibrillation",
    "summary": "Rate and rhythm control considerations.",
    "content": "Amiodarone prolongs the QT interval. Monitor thyroid function.",
    "medical_specialty": "cardiology",
    "evidence_level": "A",
    "references": ["ESC 2024 guidelines"],
    "author_name": "Dr Elena Muresan",
    "author_credentials": "MD, PhD",
    "institution": "Cluj County Emergency Hospital",
    "last_reviewed": "2026-02-11",
}
PROTOCOL = {
    "title": "Adult anaphylaxis",
    "description": "Immediate management of suspected anaphylaxis.",
    "urgency_level": "critical",
    "is_emergency_protocol": True,
    "medical_specialty": "emergency",
    "steps": [{"n": 1, "do": "IM adrenaline 0.5 mg"}],
    "contraindications": ["none absolute in true anaphylaxis"],
    "complications": ["biphasic reaction"],
    "evidence_level": "A",
    "author_name": "Dr Elena Muresan",
    "institution": "Cluj County Emergency Hospital",
    "last_updated": "2026-01-30",
}


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload, self.status_code = payload, status_code

    def json(self):
        return self._payload


class _Http:
    def __init__(self, payload, status_code=200):
        self._payload, self._status = payload, status_code
        self.headers_seen: dict = {}

    async def get(self, url, params=None, headers=None):
        self.headers_seen = headers or {}
        return _Response(self._payload, self._status)


def _ctx(http, secret="the-key") -> SkillContext:
    from ars_protocol import Language

    async def _secret(_name: str):
        return secret

    return SkillContext(http=http, language=Language.EN, cancelled=asyncio.Event(),
                        secret=_secret)


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setenv("ARS_MEDICAL_URL", "https://example.invalid")


def test_the_capability_is_private_and_high_risk() -> None:
    """Not because a clinical protocol is secret — it is written to be read — but because
    the QUESTION is among the most sensitive things a person will ever type, and a
    capability that is not private is never asked about before it is used."""
    assert Capability.MEDICAL_READ.touches_private_data
    assert Capability.MEDICAL_READ.risk.value == "high"
    assert not Capability.MEDICAL_READ.is_effectful


@pytest.mark.asyncio
async def test_an_article_comes_back_with_who_wrote_it_and_when() -> None:
    http = _Http([ARTICLE])
    blocks = await MedicalSkill().call("medical_reference", {"query": "amiodarone"}, _ctx(http))

    (text, provenance), = blocks
    assert "prolongs the QT interval" in text
    assert "Dr Elena Muresan" in provenance.label
    assert "reviewed 2026-02-11" in provenance.label
    assert "evidence level A" in text
    assert provenance.trust.value == "external", "corpus text must be data, never instruction"


@pytest.mark.asyncio
async def test_a_protocol_leads_with_its_contraindications() -> None:
    """A protocol read out without them is worse than no protocol, and anything further
    down a long block is likelier to be dropped."""
    http = _Http([PROTOCOL])
    (text, _prov), = await MedicalSkill().call(
        "medical_protocol", {"query": "anaphylaxis"}, _ctx(http)
    )

    assert text.index("Contraindications") < text.index("Steps")
    assert "EMERGENCY PROTOCOL" in text
    assert "critical" in text


@pytest.mark.asyncio
async def test_nothing_found_says_so_instead_of_inventing() -> None:
    """"Your reference does not cover this" is a real answer. The alternative is the model
    filling the gap, which is the thing this skill exists to prevent."""
    (text, _prov), = await MedicalSkill().call(
        "medical_reference", {"query": "unicorn fever"}, _ctx(_Http([]))
    )

    assert "no entry" in text.lower()
    assert "unicorn fever" in text


@pytest.mark.asyncio
async def test_it_refuses_without_a_credential() -> None:
    with pytest.raises(SkillError, match="vault"):
        await MedicalSkill().call("medical_reference", {"query": "x"},
                                  _ctx(_Http([]), secret=None))


@pytest.mark.asyncio
async def test_it_refuses_when_no_reference_is_configured(monkeypatch) -> None:
    """A checkout carries no route to anyone's patient data."""
    monkeypatch.delenv("ARS_MEDICAL_URL", raising=False)
    with pytest.raises(SkillError, match="ARS_MEDICAL_URL"):
        await MedicalSkill().call("medical_reference", {"query": "x"}, _ctx(_Http([])))


@pytest.mark.asyncio
async def test_an_upstream_error_is_reported_not_swallowed() -> None:
    with pytest.raises(SkillError, match="503"):
        await MedicalSkill().call("medical_reference", {"query": "x"},
                                  _ctx(_Http([], status_code=503)))


@pytest.mark.asyncio
async def test_the_credential_is_never_returned_in_the_output() -> None:
    """`SkillContext.secret` hands over a real credential. It must reach the request and
    nothing else — not the text, not the provenance, not a log line."""
    http = _Http([ARTICLE])
    blocks = await MedicalSkill().call("medical_reference", {"query": "amiodarone"},
                                       _ctx(http, secret="super-secret-key"))

    rendered = json.dumps([(t, p.model_dump(mode="json")) for t, p in blocks])
    assert "super-secret-key" not in rendered
    assert http.headers_seen.get("apikey") == "super-secret-key"
