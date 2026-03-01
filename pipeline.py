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
    ASR_BASE_TO_LOCALES: dict[str, list[str]] = {
        "en": ["en-US", "en-GB"],
        "es": ["es-US", "es-ES"],
        "de": ["de-DE"],
        "fr": ["fr-FR", "fr-CA"],
        "it": ["it-IT"],
        "ar": ["ar-AR"],
        "ja": ["ja-JP"],
        "ko": ["ko-KR"],
        "pt": ["pt-BR", "pt-PT"],
        "ru": ["ru-RU"],
        "hi": ["hi-IN"],
        "nl": ["nl-NL"],
        "da": ["da-DK"],
        "nn": ["nn-NO"],
        "nb": ["nb-NO"],
        "cs": ["cs-CZ"],
        "pl": ["pl-PL"],
        "sv": ["sv-SE"],
        "th": ["th-TH"],
        "tr": ["tr-TR"],
        "he": ["he-IL"],
    }
    ASR_LOCALE_ALIASES: dict[str, str] = {
        "en-us": "en-US",
        "en-gb": "en-GB",
        "es-us": "es-US",
        "es-es": "es-ES",
        "de-de": "de-DE",
        "fr-fr": "fr-FR",
        "fr-ca": "fr-CA",
        "it-it": "it-IT",
        # Nvidia model description uses ar-AR.
        "ar-ar": "ar-AR",
        # Common alternative hint that some callers/configs use.
        "ar-sa": "ar-AR",
        "ja-jp": "ja-JP",
        "ko-kr": "ko-KR",
        "pt-br": "pt-BR",
        "pt-pt": "pt-PT",
        "ru-ru": "ru-RU",
        "hi-in": "hi-IN",
        "nl-nl": "nl-NL",
        "da-dk": "da-DK",
        "nn-no": "nn-NO",
        "nb-no": "nb-NO",
        "cs-cz": "cs-CZ",
        "pl-pl": "pl-PL",
        "sv-se": "sv-SE",
        "th-th": "th-TH",
        "tr-tr": "tr-TR",
        "he-il": "he-IL",
    }

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
        # Some hosted multilingual endpoints reject certain language hints
        # (for example "ar" in offline mode). Retry with progressively safer hints.
        retry_hints = self._asr_retry_hints(language_hint)
        response = None
        last_exc: Optional[Exception] = None
        for hint in retry_hints:
            try:
                config = riva.client.RecognitionConfig(
                    encoding=riva.client.AudioEncoding.LINEAR_PCM,
                    sample_rate_hertz=16000,
                    language_code=hint,
                    max_alternatives=1,
                    enable_automatic_punctuation=True,
                    verbatim_transcripts=False,
                )
                response = asr_service.offline_recognize(pcm16le_16k, config)
                break
            except Exception as exc:
                msg = str(exc)
                last_exc = exc
                if "Unavailable model requested" in msg or "StatusCode.INVALID_ARGUMENT" in msg:
                    continue
                raise

        if response is None:
            raise RuntimeError(
                f"ASR request failed for all language hints {retry_hints}: {last_exc}"
            )

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

    def _asr_retry_hints(self, hint: str) -> list[str]:
        raw = (hint or "").strip().replace("_", "-")
        raw_lower = raw.lower()
        base = raw_lower.split("-", 1)[0] if raw_lower else ""

        canonical_locale = self.ASR_LOCALE_ALIASES.get(raw_lower, "")
        base_locales = self.ASR_BASE_TO_LOCALES.get(base, [])

        candidates = [raw, canonical_locale, *base_locales, "multi", "en-US"]
        deduped: list[str] = []
        for value in candidates:
            v = (value or "").strip()
            if v and v not in deduped:
                deduped.append(v)
        return deduped

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

    def detect_language_from_text(self, text: str) -> str:
        if not text.strip():
            return ""
        translator = Translator()
        detected = translator.detect(text)
        if inspect.isawaitable(detected):
            detected = asyncio.run(detected)
        lang = getattr(detected, "lang", "")
        return self._normalize_lang_code(lang, fallback="")

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
        audio_ulaw, _, _, _, _ = self.process_utterance_with_detection(
            pcm16le_8k=pcm16le_8k,
            src_lang=src_lang,
            dest_lang=dest_lang,
            use_auto=False,
            current_src_lang=src_lang,
        )
        return audio_ulaw

    def process_utterance_with_detection(
        self,
        *,
        pcm16le_8k: bytes,
        src_lang: str,
        dest_lang: str,
        use_auto: bool,
        current_src_lang: str,
    ) -> tuple[bytes, str, bool, str, str]:
        asr_result = self.transcribe(pcm16le_8k, language_hint=src_lang or "en-US")
        if not asr_result.text:
            resolved = self._normalize_lang_code(current_src_lang, fallback="") or self._normalize_lang_code(
                src_lang, fallback="en"
            )
            return b"", resolved, False, "", ""

        detected_lang = self._normalize_lang_code(asr_result.detected_language or "", fallback="")
        if use_auto and not detected_lang:
            detected_lang = self.detect_language_from_text(asr_result.text)

        language_detected = bool(detected_lang)

        effective_src_lang = (
            detected_lang
            if use_auto and detected_lang
            else self._normalize_lang_code(current_src_lang, fallback="")
            or self._normalize_lang_code(src_lang, fallback="auto")
        )

        translated_text = self.translate(
            asr_result.text,
            src_lang=effective_src_lang,
            dest_lang=dest_lang,
        )
        if not translated_text:
            resolved = detected_lang or self._normalize_lang_code(effective_src_lang, fallback="en")
            return b"", resolved, language_detected, asr_result.text, ""

        audio_ulaw = self.synthesize(translated_text, language=dest_lang)
        resolved = detected_lang or self._normalize_lang_code(effective_src_lang, fallback="en")
        return audio_ulaw, resolved, language_detected, asr_result.text, translated_text
