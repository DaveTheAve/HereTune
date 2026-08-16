from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import AsyncIterator


@dataclass(slots=True)
class LiveMessage:
    event: str
    data: dict[str, object]

    def sse(self) -> str:
        return f"event: {self.event}\ndata: {json.dumps(self.data, ensure_ascii=False, separators=(',', ':'))}\n\n"


class LiveHub:
    """Tiny in-process SSE fan-out bus.

    The scanner/analyzer run partly in daemon threads, so ``publish`` is safe to
    call from either an asyncio task or a worker thread. Slow browser tabs get a
    bounded queue; when full we drop the oldest event and keep the newest state.
    """

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.subscribers: set[asyncio.Queue[LiveMessage]] = set()

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def publish(self, event: str, **data: object) -> None:
        loop = self.loop
        if loop is None or loop.is_closed():
            return

        def deliver() -> None:
            message = LiveMessage(event, dict(data))
            for queue in tuple(self.subscribers):
                try:
                    if queue.full():
                        queue.get_nowait()
                    queue.put_nowait(message)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

        try:
            loop.call_soon_threadsafe(deliver)
        except RuntimeError:
            pass

    async def stream(self) -> AsyncIterator[str]:
        queue: asyncio.Queue[LiveMessage] = asyncio.Queue(maxsize=64)
        self.subscribers.add(queue)
        try:
            yield "retry: 2000\nevent: hello\ndata: {\"ok\":true}\n\n"
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield message.sse()
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            self.subscribers.discard(queue)
