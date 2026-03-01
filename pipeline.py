import asyncio
import inspect
import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs
from googletrans import LANGUAGES
from googletrans import Translator

from audio_utils import pcm8k_to_pcm16k

load_dotenv()


@dataclass
class ASRResult:
    text: str
    detected_language: Optional[str] = None


class TranslationPipeline:
    def __init__(self) -> None:
        self.nvidia_api_key = os.getenv("NVIDIA_API_KEY", "")
        self.nvidia_server = os.getenv("NVIDIA_ASR_SERVER", "grpc.nvcf.nvidia.com:443")
        self.nvidia_function_id = (
            os.getenv("NVIDIA_ASR_FUNCTION_ID")
            or os.getenv("NVIDIA_ASR_MODEL_FUNCTION_ID", "")
        )
        elevenlabs_key = os.getenv("ELEVENLABS_API_KEY") or os.getenv("ELEVENLABS_API", "")
        self.elevenlabs = ElevenLabs(api_key=elevenlabs_key)
        self.voice_map = {
            "ar": os.getenv("ELEVENLABS_ARABIC_VOICE_ID", ""),
            "en": os.getenv("ELEVENLABS_ENGLISH_VOICE_ID", ""),
            "es": os.getenv("ELEVENLABS_SPANISH_VOICE_ID", ""),
            "vi": os.getenv("ELEVENLABS_VIETNAMESE_VOICE_ID", ""),
        }

    def transcribe(self, pcm16le_8k: bytes, language_hint: str = "en-US") -> ASRResult:
        """
        Offline transcription via Nvidia Riva Python client.
        Input should be PCM16LE 8kHz; it is resampled to 16kHz before ASR.
        """
        pcm16le_16k = pcm8k_to_pcm16k(pcm16le_8k)
        if not pcm16le_16k:
            return ASRResult(text="")

        try:
            import riva.client  # type: ignore
        except Exception as exc:  # pragma: no cover - dependency/runtime guard
            raise RuntimeError(
                "nvidia-riva-client is not available. Install requirements first."
            ) from exc

        metadata = []
        if self.nvidia_function_id:
            metadata.append(("function-id", self.nvidia_function_id))
        if self.nvidia_api_key:
            metadata.append(("authorization", f"Bearer {self.nvidia_api_key}"))

        auth = riva.client.Auth(
            uri=self.nvidia_server,
            use_ssl=True,
            metadata_args=metadata,
        )
        asr_service = riva.client.ASRService(auth)
        config = riva.client.RecognitionConfig(
            encoding=riva.client.AudioEncoding.LINEAR_PCM,
            sample_rate_hertz=16000,
            language_code=language_hint,
            max_alternatives=1,
            enable_automatic_punctuation=True,
            verbatim_transcripts=False,
        )
        response = asr_service.offline_recognize(pcm16le_16k, config)

        transcripts: list[str] = []
        detected_lang: Optional[str] = None
        for result in getattr(response, "results", []):
            alternatives = getattr(result, "alternatives", [])
            if not alternatives:
                continue
            transcripts.append(alternatives[0].transcript.strip())
            if not detected_lang:
                detected_lang = getattr(result, "language_code", None)

        text = " ".join([t for t in transcripts if t]).strip()
        return ASRResult(text=text, detected_language=detected_lang)

    def translate(self, text: str, src_lang: str, dest_lang: str) -> str:
        if not text.strip():
            return ""
        src = self._normalize_lang_code(src_lang, fallback="auto")
        dest = self._normalize_lang_code(dest_lang, fallback="en")
        translator = Translator()
        translated = translator.translate(text, src=src, dest=dest)
        # googletrans versions differ: some return a sync object, newer ones return
        # a coroutine. Normalize both to a resolved translation result.
        if inspect.isawaitable(translated):
            translated = asyncio.run(translated)
        return translated.text.strip()

    def _normalize_lang_code(self, lang: str, fallback: str) -> str:
        """
        Normalize locale-style language tags for googletrans.
        Examples: ar-SA -> ar, en_US -> en.
        """
        if not lang:
            return fallback

        code = lang.strip().lower().replace("_", "-")
        if code == "auto":
            return "auto"
        if code in LANGUAGES:
            return code

        base = code.split("-", 1)[0]
        if base in LANGUAGES:
            return base
        return fallback

    def synthesize(self, text: str, language: str) -> bytes:
        if not text.strip():
            return b""
        lang_code = self._normalize_lang_code(language, fallback="en")
        voice_id = self.voice_map.get(lang_code) or self.voice_map.get("en")
        if not voice_id:
            raise RuntimeError(f"No ElevenLabs voice configured for language '{language}'.")

        audio_iter = self.elevenlabs.text_to_speech.convert(
            text=text,
            voice_id=voice_id,
            model_id="eleven_multilingual_v2",
            output_format="ulaw_8000",
        )
        if isinstance(audio_iter, (bytes, bytearray)):
            return bytes(audio_iter)
        return b"".join(chunk for chunk in audio_iter if chunk)

    def process_utterance(self, pcm16le_8k: bytes, src_lang: str, dest_lang: str) -> bytes:
        asr_result = self.transcribe(pcm16le_8k, language_hint=src_lang or "en-US")
        if not asr_result.text:
            return b""

        translated_text = self.translate(
            asr_result.text,
            src_lang=asr_result.detected_language or src_lang,
            dest_lang=dest_lang,
        )
        if not translated_text:
            return b""

        return self.synthesize(translated_text, language=dest_lang)
