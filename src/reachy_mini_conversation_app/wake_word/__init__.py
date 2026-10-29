"""On-device wake-word detection (openWakeWord).

Listens to mic frames in parallel with the Gemini stream, fires a callback
when the configured wake phrase ('Qarabay') is detected so that the app can
pause/duck music, tilt the ears and switch into active-listening mode even
while music is playing through the same speaker.
"""

from reachy_mini_conversation_app.wake_word.detector import WakeWordDetector
from reachy_mini_conversation_app.wake_word.coordinator import WakeWordCoordinator


__all__ = ["WakeWordDetector", "WakeWordCoordinator"]
