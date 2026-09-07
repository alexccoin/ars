"""Exceptions raised by the packet decoders in `ars_medical.devices`.

Two different failure shapes, kept as two different exception types on purpose, because
a caller (a `DeviceBackend.read` loop) needs to react to them differently:

  * `PacketDecodeError` — the bytes do not parse as the characteristic they claim to be
    (too short, trailing octets after every declared field is consumed). This is either
    a different device on the same UUIDs or a transport-layer corruption; the session
    should count it and move on to the next indication, never guess at a partial value.

  * `MeasurementUnsuccessful` — the bytes parse *perfectly* and say, explicitly, "this
    measurement did not happen" (the Weight Scale `0xFFFF` sentinel, WSS §3.2.1.2 — see
    `weight.py`). This is not a decode error and not a plausibility question; it is the
    device's own statement that there is no number to store. Modelled separately from
    `PacketDecodeError` so a caller can tell "garbage arrived" from "the device tried and
    told us it failed" without inspecting a message string.
"""

from __future__ import annotations


class PacketDecodeError(ValueError):
    """A characteristic payload did not match its expected layout."""


class MeasurementUnsuccessful(Exception):
    """The device explicitly reported that no measurement was taken.

    Raised instead of returning a value — the alternative, returning `None` or a
    sentinel float, is exactly the shape of bug this type exists to make impossible: a
    caller that forgets to check would otherwise pass a fabricated number straight to
    `VitalReading`.
    """
