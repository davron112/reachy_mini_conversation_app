"""Shared dataclasses for the music subsystem."""

from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class TrackInfo:
    """Lightweight description of a playable track returned by a MusicBackend."""

    track_id: str
    title: str
    artist: str
    duration_s: float | None
    stream_url: str | None
    source: str

    def display_name(self) -> str:
        """Return a human-friendly label."""
        if self.artist and self.title:
            return f"{self.artist} — {self.title}"
        return self.title or self.track_id
