from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)


@dataclass
class _HubSession:
    board_url: str
    subscribers: set[asyncio.Queue[bytes]] = field(default_factory=set)
    task: asyncio.Task | None = None
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)


class BoardAudioHub:
    """Single-upstream fanout per board_url for relay + transcription."""

    def __init__(self) -> None:
        self._sessions: dict[str, _HubSession] = {}
        self._lock = asyncio.Lock()

    async def _ensure_session(self, board_url: str) -> _HubSession:
        async with self._lock:
            sess = self._sessions.get(board_url)
            if sess is None:
                sess = _HubSession(board_url=board_url)
                self._sessions[board_url] = sess
            if sess.task is None or sess.task.done():
                sess.stop_event = asyncio.Event()
                sess.task = asyncio.create_task(self._run_session(sess))
            return sess

    async def _run_session(self, sess: _HubSession) -> None:
        timeout = httpx.Timeout(connect=20.0, read=None, write=20.0, pool=None)
        limits = httpx.Limits(max_keepalive_connections=0, max_connections=4)
        headers = {"Connection": "close", "Accept": "*/*", "User-Agent": "korvo-server/audio-hub"}
        try:
            async with httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=True) as client:
                async with client.stream("GET", sess.board_url, headers=headers) as resp:
                    if resp.status_code != 200:
                        log.warning("audio hub upstream HTTP %s for %s", resp.status_code, sess.board_url)
                        return
                    async for chunk in resp.aiter_bytes(16384):
                        if sess.stop_event.is_set():
                            return
                        if not chunk:
                            continue
                        stale: list[asyncio.Queue[bytes]] = []
                        for q in list(sess.subscribers):
                            try:
                                if q.full():
                                    _ = q.get_nowait()
                                q.put_nowait(chunk)
                            except Exception:
                                stale.append(q)
                        for q in stale:
                            sess.subscribers.discard(q)
                        if not sess.subscribers:
                            # No listeners left: stop this upstream quickly.
                            return
        except Exception as e:  # noqa: BLE001
            log.warning("audio hub session ended for %s: %s", sess.board_url, e)
        finally:
            async with self._lock:
                cur = self._sessions.get(sess.board_url)
                if cur is sess:
                    self._sessions.pop(sess.board_url, None)

    async def subscribe(self, board_url: str) -> AsyncIterator[bytes]:
        sess = await self._ensure_session(board_url)
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=24)
        sess.subscribers.add(q)
        try:
            while True:
                chunk = await q.get()
                yield chunk
        finally:
            sess.subscribers.discard(q)
            if not sess.subscribers:
                sess.stop_event.set()

    async def close(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for sess in sessions:
            sess.stop_event.set()
            if sess.task:
                sess.task.cancel()
                with contextlib.suppress(Exception):
                    await sess.task


audio_hub = BoardAudioHub()
