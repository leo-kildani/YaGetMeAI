import audioop
from dataclasses import dataclass, field
from typing import Optional


def mulaw_to_pcm16le(mulaw_bytes: bytes) -> bytes:
    """Convert mu-law (8kHz, 8-bit) bytes to PCM16 little-endian (8kHz)."""
    return audioop.ulaw2lin(mulaw_bytes, 2)


def pcm8k_to_pcm16k(pcm16le_8k: bytes) -> bytes:
    """Resample PCM16 little-endian audio from 8kHz to 16kHz."""
    converted, _ = audioop.ratecv(pcm16le_8k, 2, 1, 8000, 16000, None)
    return converted


def mulaw_to_pcm16k(mulaw_bytes: bytes) -> bytes:
    """Convert mu-law 8kHz audio to PCM16 little-endian 16kHz audio."""
    pcm_8k = mulaw_to_pcm16le(mulaw_bytes)
    return pcm8k_to_pcm16k(pcm_8k)


@dataclass
class SimpleVAD:
    """
    Simple RMS-based VAD for telephony audio.

    Feed PCM16LE 8kHz chunks. When silence is sustained after speech, returns
    the buffered utterance (PCM16LE 8kHz). Otherwise returns None.
    """

    speech_threshold: int = 500
    start_speech_frames: int = 2
    end_silence_frames: int = 10
    min_utterance_ms: int = 350
    chunk_ms: int = 20
    in_speech: bool = False
    speech_frames: int = 0
    silence_frames: int = 0
    _buffer: bytearray = field(default_factory=bytearray)

    def reset(self) -> None:
        self.in_speech = False
        self.speech_frames = 0
        self.silence_frames = 0
        self._buffer.clear()

    def _utterance_is_long_enough(self) -> bool:
        min_frames = max(1, self.min_utterance_ms // self.chunk_ms)
        return self.speech_frames >= min_frames

    def feed_pcm16_8k(self, chunk: bytes) -> Optional[bytes]:
        if not chunk:
            return None

        rms = audioop.rms(chunk, 2)
        is_speech = rms >= self.speech_threshold

        if not self.in_speech:
            if is_speech:
                self.speech_frames += 1
                self._buffer.extend(chunk)
                if self.speech_frames >= self.start_speech_frames:
                    self.in_speech = True
                    self.silence_frames = 0
            else:
                self.speech_frames = 0
                self._buffer.clear()
            return None

        self._buffer.extend(chunk)
        if is_speech:
            self.speech_frames += 1
            self.silence_frames = 0
            return None

        self.silence_frames += 1
        if self.silence_frames < self.end_silence_frames:
            return None

        utterance = bytes(self._buffer)
        should_emit = self._utterance_is_long_enough()
        self.reset()
        if should_emit:
            return utterance
        return None
