"""Guard policy tables — the small number of judgement calls the code encodes.

Everything here is derived from ``ars_protocol.capability`` where it can be, and stated
explicitly where it cannot. Nothing in this file duplicates a fact the protocol already
owns: risk, "touches private data" and "is effectful" are read from the enum, never
re-listed.
"""

from __future__ import annotations

from typing import Final

from ars_protocol import Capability, ConfirmPolicy, Risk

NOT_INTERACTIVELY_GRANTABLE: Final[frozenset[Capability]] = frozenset({
    Capability.SHELL_EXEC,
    Capability.NETWORK_EGRESS,
})
"""Capabilities that may never be requested with a mid-task consent prompt.

The reasoning is about who can create a grant. A voice consent prompt can be answered by
anyone the microphone can hear, and it arrives in the middle of a task when the user is
thinking about the task, not about permissions. That is a fine way to approve "send this
email to Maria". It is a terrible way to approve arbitrary command execution.

``shell.exec`` and ``network.egress`` are the two capabilities that turn a scoped
assistant into a general-purpose remote shell and a general-purpose exfiltration channel.
Both must be granted deliberately, in the UI or in config, with the user looking at a
permissions screen rather than at a half-finished task. If there is no grant, the guard
denies with ``NO_GRANT`` and does not offer to ask.
"""


def is_interactively_grantable(capability: Capability) -> bool:
    return capability not in NOT_INTERACTIVELY_GRANTABLE


def silent_use_permitted(capability: Capability) -> bool:
    """Whether ``ConfirmPolicy.NEVER`` is honoured for this capability.

    ``capability.py`` says NEVER is "only ever appropriate for LOW risk". This turns that
    comment into an enforced rule: a grant stored with ``confirm=never`` on a MEDIUM or
    higher capability is silently upgraded to ASK rather than obeyed. A bad grant - from
    a config file, a migration, a UI bug, or someone talking the user through "just set
    it to never" - cannot buy silent access to anything that matters.
    """
    return capability.risk is Risk.LOW


def default_confirm_policy(capability: Capability) -> ConfirmPolicy:
    """What the UI should propose when the user creates a grant for this capability.

    Not enforcement - enforcement is the stored grant. This is the default that shows up
    pre-selected, and defaults are what most grants end up being.
    """
    if capability.risk is Risk.CRITICAL:
        return ConfirmPolicy.EVERY_USE
    if capability.is_effectful or capability.risk is Risk.HIGH:
        return ConfirmPolicy.EVERY_USE
    if capability.risk is Risk.MEDIUM:
        return ConfirmPolicy.ONCE_PER_SESSION
    return ConfirmPolicy.NEVER


def requires_narrow_scope(capability: Capability) -> bool:
    """Capabilities where a ``("*",)`` grant deserves a warning in the UI.

    Advisory - the guard honours a wildcard grant the user genuinely made. But
    ``files.read`` over ``*`` is the whole disk, and the person clicking it should be
    told so in words.
    """
    return capability.risk in (Risk.HIGH, Risk.CRITICAL)


def sensitive(capability: Capability) -> bool:
    """The predicate the guard's disabled-check and taint-check both key off.

    "Touches something the user would not publish, or changes the world."

    Deliberately does NOT include the outbound-only capabilities: see ``exfiltrates``.
    They are not sensitive in themselves, but they are not harmless in a tainted turn
    either, so they get their own predicate rather than being widened into this one.
    """
    return capability.touches_private_data or capability.is_effectful


def exfiltrates(capability: Capability) -> bool:
    """Sends text of the model's choosing outside the machine.

    Kept separate from ``sensitive`` because the response differs: a tainted turn may
    never silently touch private data, but it may still search the web *if the user is
    shown the literal query first*. Refusing outright would make "read this page, then
    look up what it mentions" impossible, which is a thing users legitimately want.
    """
    return capability.exfiltrates_outward
