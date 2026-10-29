"""Audio player wrappers: drive mpv or python-vlc from async code."""

from __future__ import annotations
import os
import json
import uuid
import shutil
import asyncio
import logging
import tempfile
from typing import Protocol, runtime_checkable
from pathlib import Path


logger = logging.getLogger(__name__)

MPV_BINARY = "mpv"
VLC_PROBE_IMPORT = "vlc"
DEFAULT_VOLUME = 65
MIN_VOLUME = 0
MAX_VOLUME = 100
MPV_STARTUP_TIMEOUT_S = 5.0
MPV_COMMAND_TIMEOUT_S = 1.5

ENV_MPV_AUDIO_DEVICE = "REACHY_MPV_AUDIO_DEVICE"
ENV_MPV_AUDIO_DRIVER = "REACHY_MPV_AUDIO_DRIVER"


class AudioPlayerError(RuntimeError):
    """Raised when audio playback fails."""


@runtime_checkable
class AudioPlayer(Protocol):
    """Minimal async interface every backend player must implement."""

    name: str

    async def play(self, url: str) -> None:
        """Replace current playback with `url` (network URL or local path)."""
        ...

    async def pause(self) -> None:
        """Pause without dropping the current track."""
        ...

    async def resume(self) -> None:
        """Resume paused playback."""
        ...

    async def stop(self) -> None:
        """Stop playback and clear current track."""
        ...

    async def set_volume(self, level: int) -> None:
        """Set volume 0..100."""
        ...

    async def get_volume(self) -> int:
        """Return current volume 0..100."""
        ...

    async def is_playing(self) -> bool:
        """Return True if a track is currently playing (not paused, not stopped)."""
        ...

    async def close(self) -> None:
        """Release the player process / resources."""
        ...


class MpvPlayer:
    """Drive a long-lived mpv process via its JSON IPC socket."""

    name = "mpv"

    def __init__(self, socket_path: Path | None = None) -> None:
        """Initialise with an explicit IPC socket path or a temp one."""
        self._socket_path = socket_path or (Path(tempfile.gettempdir()) / f"reachy_mpv_{uuid.uuid4().hex}.sock")
        self._process: asyncio.subprocess.Process | None = None
        self._volume = DEFAULT_VOLUME
        self._command_lock = asyncio.Lock()
        self._audio_driver = (os.getenv(ENV_MPV_AUDIO_DRIVER) or "").strip() or None
        self._audio_device = (os.getenv(ENV_MPV_AUDIO_DEVICE) or "").strip() or None

    def _spawn_args(self) -> list[str]:
        args = [
            MPV_BINARY,
            "--no-video",
            "--idle=yes",
            "--no-terminal",
            "--really-quiet",
            f"--input-ipc-server={self._socket_path}",
            f"--volume={self._volume}",
        ]
        if self._audio_driver:
            args.append(f"--ao={self._audio_driver}")
        if self._audio_device:
            args.append(f"--audio-device={self._audio_device}")
        return args

    async def _spawn(self) -> None:
        if self._process and self._process.returncode is None:
            return
        if not shutil.which(MPV_BINARY):
            raise AudioPlayerError(f"{MPV_BINARY} not found on PATH")
        if self._socket_path.exists():
            try:
                self._socket_path.unlink()
            except OSError:
                pass
        self._process = await asyncio.create_subprocess_exec(
            *self._spawn_args(),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        deadline = asyncio.get_event_loop().time() + MPV_STARTUP_TIMEOUT_S
        while asyncio.get_event_loop().time() < deadline:
            if self._socket_path.exists():
                return
            await asyncio.sleep(0.05)
        await self.close()
        raise AudioPlayerError("mpv IPC socket did not appear in time")

    async def _send(self, *command: object) -> dict[str, object]:
        await self._spawn()
        payload = json.dumps({"command": list(command)}).encode() + b"\n"
        async with self._command_lock:
            try:
                reader, writer = await asyncio.open_unix_connection(str(self._socket_path))
            except OSError as exc:
                raise AudioPlayerError(f"cannot reach mpv socket: {exc}") from exc
            try:
                writer.write(payload)
                await writer.drain()
                try:
                    raw = await asyncio.wait_for(reader.readline(), timeout=MPV_COMMAND_TIMEOUT_S)
                except asyncio.TimeoutError as exc:
                    raise AudioPlayerError("mpv IPC command timed out") from exc
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:  # noqa: BLE001
                    pass
        try:
            reply = json.loads(raw.decode().strip() or "{}")
        except json.JSONDecodeError as exc:
            raise AudioPlayerError(f"mpv replied with invalid JSON: {raw!r}") from exc
        if not isinstance(reply, dict):
            raise AudioPlayerError(f"mpv replied with a non-object: {raw!r}")
        return reply

    async def play(self, url: str) -> None:
        """Replace current playback target with `url`."""
        await self._send("loadfile", url, "replace")
        await self._send("set_property", "pause", False)

    async def pause(self) -> None:
        """Pause the current track."""
        await self._send("set_property", "pause", True)

    async def resume(self) -> None:
        """Resume the current track."""
        await self._send("set_property", "pause", False)

    async def stop(self) -> None:
        """Clear the playlist (mpv stays idle)."""
        await self._send("stop")

    async def set_volume(self, level: int) -> None:
        """Set volume; clamped to MIN..MAX."""
        self._volume = max(MIN_VOLUME, min(MAX_VOLUME, level))
        await self._send("set_property", "volume", self._volume)

    async def get_volume(self) -> int:
        """Read mpv's reported volume; falls back to the cached value."""
        try:
            response = await self._send("get_property", "volume")
            data = response.get("data")
            if isinstance(data, (int, float)):
                self._volume = int(data)
        except AudioPlayerError:
            pass
        return self._volume

    async def is_playing(self) -> bool:
        """Return True when mpv has a non-idle, non-paused track."""
        try:
            paused = await self._send("get_property", "pause")
            idle = await self._send("get_property", "idle-active")
        except AudioPlayerError:
            return False
        return not paused.get("data", True) and not idle.get("data", True)

    async def close(self) -> None:
        """Terminate the mpv process and remove the socket file."""
        proc = self._process
        if proc and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                proc.kill()
        self._process = None
        try:
            if self._socket_path.exists():
                self._socket_path.unlink()
        except OSError:
            pass


class VlcPlayer:
    """Fallback player backed by the python-vlc bindings."""

    name = "vlc"

    def __init__(self) -> None:
        """Lazy-import python-vlc and instantiate the singleton player."""
        try:
            import vlc
        except ImportError as exc:
            raise AudioPlayerError("python-vlc not installed. Install with: pip install python-vlc") from exc
        self._vlc = vlc
        self._instance = vlc.Instance("--quiet")
        if self._instance is None:
            raise AudioPlayerError("Failed to create VLC instance")
        self._player = self._instance.media_player_new()
        self._player.audio_set_volume(DEFAULT_VOLUME)
        self._volume = DEFAULT_VOLUME

    async def play(self, url: str) -> None:
        """Load and start playback of `url`."""
        media = self._instance.media_new(url)
        self._player.set_media(media)
        await asyncio.to_thread(self._player.play)

    async def pause(self) -> None:
        """Pause playback (VLC's set_pause is non-blocking)."""
        await asyncio.to_thread(self._player.set_pause, 1)

    async def resume(self) -> None:
        """Resume playback."""
        await asyncio.to_thread(self._player.set_pause, 0)

    async def stop(self) -> None:
        """Stop playback."""
        await asyncio.to_thread(self._player.stop)

    async def set_volume(self, level: int) -> None:
        """Set volume; clamped to MIN..MAX."""
        self._volume = max(MIN_VOLUME, min(MAX_VOLUME, level))
        self._player.audio_set_volume(self._volume)

    async def get_volume(self) -> int:
        """Return the cached volume (VLC's reported value is unreliable on some platforms)."""
        return self._volume

    async def is_playing(self) -> bool:
        """Return True when VLC reports the playing state."""
        return bool(self._player.is_playing())

    async def close(self) -> None:
        """Release the VLC player and instance."""
        try:
            self._player.stop()
            self._player.release()
            self._instance.release()
        except Exception:  # noqa: BLE001
            pass


def _vlc_importable() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec(VLC_PROBE_IMPORT) is not None
    except Exception:  # noqa: BLE001
        return False


def select_audio_player(preference: str | None = None) -> AudioPlayer:
    """Return the first available audio player according to preference.

    Preference values:
      - "mpv"  -> require mpv binary
      - "vlc"  -> require python-vlc
      - None   -> auto (mpv then vlc)
    """
    requested = (preference or os.getenv("REACHY_MUSIC_PLAYER") or "auto").strip().lower()
    if requested == "mpv":
        return MpvPlayer()
    if requested == "vlc":
        return VlcPlayer()
    if shutil.which(MPV_BINARY):
        logger.info("Selected audio player: mpv")
        return MpvPlayer()
    if _vlc_importable():
        logger.info("Selected audio player: python-vlc")
        return VlcPlayer()
    raise AudioPlayerError("No audio player available. Install mpv or `pip install python-vlc`.")
