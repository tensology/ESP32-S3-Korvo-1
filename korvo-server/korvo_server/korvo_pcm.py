"""Stateful parser: Korvo infinite WAV stream → raw mono s16le PCM bytes."""

from __future__ import annotations

WAV_HEADER_BYTES = 44


class WavStreamToPcm16:
    """Strip the first 44-byte RIFF header, then pass through PCM16 mono body."""

    __slots__ = ("_hdr_remain",)

    def __init__(self) -> None:
        self._hdr_remain = WAV_HEADER_BYTES

    def feed(self, chunk: bytes) -> bytes:
        if not chunk:
            return b""
        if self._hdr_remain <= 0:
            return chunk
        if len(chunk) <= self._hdr_remain:
            self._hdr_remain -= len(chunk)
            return b""
        drop = self._hdr_remain
        self._hdr_remain = 0
        return chunk[drop:]
