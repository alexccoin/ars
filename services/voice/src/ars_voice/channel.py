"""A closable async frame channel.

The engine interfaces all take `AsyncIterator[AudioFrame]`, but the pipeline owns exactly
one microphone stream and has to route it to several engines whose lifetimes differ (the
wakeword lives forever, an ASR stream lives for one utterance). This is the adapter.

Bounded by default. An unbounded channel in front of a slow ASR turns a CPU spike into
silently growing latency, which is indistinguishable from "the model got worse".
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from ars_protocol import AudioFrame


class FrameChannel:
    def __init__(self, maxsize: int = 512) -> None:
        self._queue: asyncio.Queue[AudioFrame | None] = asyncio.Queue(maxsize=maxsize)
        self._closed = False
        self.dropped = 0

    @property
    def closed(self) -> bool:
        return self._closed

    def push_nowait(self, frame: AudioFrame) -> None:
        if self._closed:
            return
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            self.dropped += 1

    async def push(self, frame: AudioFrame) -> None:
        if self._closed:
            return
        await self._queue.put(frame)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:  # pragma: no cover - drain then close
            self._queue._queue.append(None)  # type: ignore[attr-defined]

    async def __aiter__(self) -> AsyncIterator[AudioFrame]:
        while True:
            frame = await self._queue.get()
            if frame is None:
                return
            yield frame

    def frames(self) -> AsyncIterator[AudioFrame]:
        return self.__aiter__()
