import asyncio
import base64
import json
import os
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from twilio.rest import Client
from twilio.twiml.voice_response import Connect, VoiceResponse

from audio_utils import SimpleVAD, mulaw_to_pcm16le
from pipeline import TranslationPipeline

try:
    from openai import OpenAI
except Exception:  # pragma: no cover - optional dependency at runtime
    OpenAI = None

load_dotenv()

app = FastAPI(title="Real-Time Voice Translation")
pipeline = TranslationPipeline()
sessions_lock = asyncio.Lock()
dashboard_clients_lock = asyncio.Lock()
dashboard_clients: set[WebSocket] = set()
calls_file_lock = asyncio.Lock()


def _normalize_public_host(raw_value: str) -> str:
    host = raw_value.strip()
    host = host.removeprefix("https://").removeprefix("http://")
    return host.rstrip("/")


PUBLIC_HOST = _normalize_public_host(os.getenv("PUBLIC_URL", "localhost:8000"))
PUBLIC_HTTP_BASE = f"https://{PUBLIC_HOST}"
PUBLIC_WS_BASE = f"wss://{PUBLIC_HOST}"

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")
RECIPIENT_PHONE_NUMBER = os.getenv("RECIPIENT_PHONE_NUMBER", "")

CALLER_LANGUAGE = os.getenv("CALLER_LANGUAGE", "ar")
RECIPIENT_LANGUAGE = os.getenv("RECIPIENT_LANGUAGE", "en")
USE_AUTO = os.getenv("USE_AUTO", "false").strip().lower() in {"1", "true", "yes", "on"}
AUTO_REPROMPT_TEXT = "I'm sorry, I didn't get that."
DASHBOARD_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "DASHBOARD_ALLOWED_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if origin.strip()
]
CALLS_FILE_PATH = Path(os.getenv("CALLS_FILE_PATH", "data/calls.json"))
OPENAI_SUMMARY_MODEL = os.getenv("OPENAI_SUMMARY_MODEL", "gpt-4.1-mini")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OpenAI and OPENAI_API_KEY else None

app.add_middleware(
    CORSMiddleware,
    allow_origins=DASHBOARD_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@dataclass
class CallLeg:
    websocket: Optional[WebSocket] = None
    stream_sid: Optional[str] = None
    vad: SimpleVAD = field(default_factory=SimpleVAD)
    active_lang: str = ""
    lang_votes: list[str] = field(default_factory=list)


@dataclass
class CallSession:
    session_id: str
    caller_lang: str
    recipient_lang: str
    caller: CallLeg = field(default_factory=CallLeg)
    recipient: CallLeg = field(default_factory=CallLeg)
    outbound_call_created: bool = False
    started_at: Optional[datetime] = None
    transcript: list[dict[str, Any]] = field(default_factory=list)


sessions: dict[str, CallSession] = {}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _datetime_to_iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def _load_calls_file_sync() -> list[dict[str, Any]]:
    if not CALLS_FILE_PATH.exists():
        return []
    try:
        content = CALLS_FILE_PATH.read_text(encoding="utf-8")
        parsed = json.loads(content)
        if isinstance(parsed, list):
            return parsed
    except Exception as exc:
        print(f"[calls] failed to parse calls file: {exc}")
    return []


def _save_calls_file_sync(calls: list[dict[str, Any]]) -> None:
    CALLS_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CALLS_FILE_PATH.write_text(json.dumps(calls, ensure_ascii=True, indent=2), encoding="utf-8")


async def _read_calls() -> list[dict[str, Any]]:
    async with calls_file_lock:
        return await asyncio.to_thread(_load_calls_file_sync)


async def _append_call_record(record: dict[str, Any]) -> None:
    async with calls_file_lock:
        calls = await asyncio.to_thread(_load_calls_file_sync)
        calls.append(record)
        await asyncio.to_thread(_save_calls_file_sync, calls)


async def _broadcast_dashboard_event(payload: dict[str, Any]) -> None:
    message = json.dumps(payload)
    async with dashboard_clients_lock:
        clients = list(dashboard_clients)

    stale_clients: list[WebSocket] = []
    for client in clients:
        try:
            await client.send_text(message)
        except Exception:
            stale_clients.append(client)

    if stale_clients:
        async with dashboard_clients_lock:
            for client in stale_clients:
                dashboard_clients.discard(client)


def _format_transcript_for_summary(segments: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for segment in segments:
        role = str(segment.get("role", "unknown")).strip() or "unknown"
        original = str(segment.get("original", "")).strip()
        translated = str(segment.get("translated", "")).strip()
        if not original and not translated:
            continue
        lines.append(f"{role.upper()} original: {original}")
        lines.append(f"{role.upper()} translated: {translated}")
    return "\n".join(lines)


def _extract_openai_text(response: Any) -> str:
    text = getattr(response, "output_text", "")
    if isinstance(text, str) and text.strip():
        return text.strip()
    return str(response).strip()


async def _summarize_call_with_openai(segments: list[dict[str, Any]]) -> str:
    if not segments:
        return ""
    if openai_client is None:
        return ""

    transcript_text = _format_transcript_for_summary(segments)
    if not transcript_text:
        return ""

    try:
        response = await asyncio.to_thread(
            openai_client.responses.create,
            model=OPENAI_SUMMARY_MODEL,
            input=[
                {
                    "role": "system",
                    "content": (
                        "You summarize translated phone conversations for recipients. "
                        "Return a concise summary with key intent and outcomes in 3-5 sentences."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Summarize this call transcript:\n\n{transcript_text}",
                },
            ],
        )
        return _extract_openai_text(response)
    except Exception as exc:
        print(f"[openai] summary generation failed: {exc}")
        return ""


async def _finalize_call_record(session: CallSession) -> dict[str, Any]:
    ended_at = _utc_now()
    summary = await _summarize_call_with_openai(session.transcript)
    duration_seconds: Optional[float] = None
    if session.started_at is not None:
        duration_seconds = max(0.0, (ended_at - session.started_at).total_seconds())

    return {
        "call_id": uuid.uuid4().hex,
        "session_id": session.session_id,
        "started_at": _datetime_to_iso(session.started_at),
        "ended_at": _datetime_to_iso(ended_at),
        "duration_seconds": duration_seconds,
        "caller_lang": session.caller_lang,
        "recipient_lang": session.recipient_lang,
        "segments": session.transcript,
        "summary": summary,
    }


def _asr_language_hint(short_lang: str) -> str:
    mapping = {
        "en": "en-US",
        "ar": "ar-SA",
        "es": "es-ES",
        "vi": "vi-VN",
    }
    return mapping.get(short_lang, short_lang or "en-US")


def _build_stream_twiml(stream_url: str) -> str:
    response = VoiceResponse()
    connect = Connect()
    connect.stream(url=stream_url)
    response.append(connect)
    return str(response)


async def _create_outbound_call(session: CallSession) -> None:
    if not RECIPIENT_PHONE_NUMBER:
        raise RuntimeError("RECIPIENT_PHONE_NUMBER is missing in .env")

    callback_url = f"{PUBLIC_HTTP_BASE}/outbound-call/{session.session_id}"
    await asyncio.to_thread(
        twilio_client.calls.create,
        to=RECIPIENT_PHONE_NUMBER,
        from_=TWILIO_PHONE_NUMBER,
        url=callback_url,
        method="POST",
    )


async def _cleanup_session_if_needed(session_id: str) -> None:
    completed_session: Optional[CallSession] = None
    async with sessions_lock:
        session = sessions.get(session_id)
        if not session:
            return
        if session.caller.websocket is None and session.recipient.websocket is None:
            completed_session = sessions.pop(session_id, None)

    if completed_session is None:
        return

    call_record = await _finalize_call_record(completed_session)
    await _append_call_record(call_record)
    await _broadcast_dashboard_event({"event": "call_ended", **call_record})


async def _send_media_to_leg(target_leg: CallLeg, ulaw_audio: bytes) -> None:
    if not target_leg.websocket or not target_leg.stream_sid or not ulaw_audio:
        return

    payload_b64 = base64.b64encode(ulaw_audio).decode("utf-8")
    media_event = {
        "event": "media",
        "streamSid": target_leg.stream_sid,
        "media": {"payload": payload_b64},
    }
    mark_event = {
        "event": "mark",
        "streamSid": target_leg.stream_sid,
        "mark": {"name": f"tts-{uuid.uuid4().hex[:8]}"},
    }

    await target_leg.websocket.send_text(json.dumps(media_event))
    await target_leg.websocket.send_text(json.dumps(mark_event))


async def _process_and_forward(
    *,
    source_pcm16le_8k: bytes,
    session: CallSession,
    session_id: str,
    source_leg: CallLeg,
    source_role: str,
    source_lang: str,
    target_lang: str,
    target_leg: CallLeg,
) -> None:
    try:
        effective_target_lang = target_leg.active_lang or target_lang
        source_hint = source_leg.active_lang or source_lang
        (
            audio_ulaw,
            detected_lang,
            language_detected,
            original_text,
            translated_text,
        ) = await asyncio.to_thread(
            pipeline.process_utterance_with_detection,
            pcm16le_8k=source_pcm16le_8k,
            src_lang=_asr_language_hint(source_hint),
            dest_lang=effective_target_lang,
            use_auto=USE_AUTO,
            current_src_lang=source_hint,
        )
        if original_text or translated_text:
            segment = {
                "role": source_role,
                "original": original_text,
                "translated": translated_text,
                "ts": _datetime_to_iso(_utc_now()),
            }
            session.transcript.append(segment)
            await _broadcast_dashboard_event({"event": "transcript", "session_id": session_id, **segment})
        if USE_AUTO:
            if language_detected and detected_lang:
                source_leg.active_lang = _stabilize_detected_language(source_leg, detected_lang)
                await _send_media_to_leg(target_leg, audio_ulaw)
                return
            # In auto mode, if the caller language is still unknown:
            # - caller utterance -> reprompt caller for more speech
            # - recipient utterance -> do not translate here (recipient passthrough is handled
            #   in the media loop when caller language is unknown)
            if source_role == "caller":
                prompt_lang = source_leg.active_lang or source_lang or "en"
                reprompt_audio = await asyncio.to_thread(
                    pipeline.synthesize,
                    AUTO_REPROMPT_TEXT,
                    prompt_lang,
                )
                await _send_media_to_leg(source_leg, reprompt_audio)
            return

        await _send_media_to_leg(target_leg, audio_ulaw)
    except Exception as exc:
        print(f"[pipeline] processing failed: {exc}")


def _stabilize_detected_language(leg: CallLeg, detected_lang: str) -> str:
    normalized = detected_lang.strip().lower()
    if not normalized:
        return leg.active_lang

    leg.lang_votes.append(normalized)
    if len(leg.lang_votes) > 5:
        leg.lang_votes.pop(0)

    top_lang, top_count = Counter(leg.lang_votes).most_common(1)[0]
    if top_count >= 3:
        return top_lang
    return leg.active_lang or top_lang


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/incoming-call")
async def incoming_call() -> Response:
    session_id = uuid.uuid4().hex
    session = CallSession(
        session_id=session_id,
        caller_lang=CALLER_LANGUAGE,
        recipient_lang=RECIPIENT_LANGUAGE,
    )

    async with sessions_lock:
        sessions[session_id] = session

    await _broadcast_dashboard_event(
        {
            "event": "incoming_call",
            "session_id": session_id,
            "caller_lang": session.caller_lang,
            "recipient_lang": session.recipient_lang,
        }
    )

    if not session.outbound_call_created:
        session.outbound_call_created = True
        try:
            await _create_outbound_call(session)
        except Exception as exc:
            print(f"[twilio] outbound call creation failed: {exc}")

    stream_url = f"{PUBLIC_WS_BASE}/media-stream/caller/{session_id}"
    twiml = _build_stream_twiml(stream_url)
    return Response(content=twiml, media_type="application/xml")


@app.post("/outbound-call/{session_id}")
async def outbound_call(session_id: str) -> Response:
    stream_url = f"{PUBLIC_WS_BASE}/media-stream/recipient/{session_id}"
    twiml = _build_stream_twiml(stream_url)
    return Response(content=twiml, media_type="application/xml")


@app.get("/api/calls")
async def get_calls() -> dict[str, Any]:
    calls = await _read_calls()
    ordered_calls = sorted(calls, key=lambda item: item.get("ended_at") or "", reverse=True)
    return {"calls": ordered_calls}


@app.websocket("/dashboard/ws")
async def dashboard_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    async with dashboard_clients_lock:
        dashboard_clients.add(websocket)

    async with sessions_lock:
        active_session = next(
            (
                session
                for session in sessions.values()
                if session.caller.websocket is not None or session.recipient.websocket is not None
            ),
            None,
        )

    if active_session:
        await websocket.send_text(
            json.dumps(
                {
                    "event": "incoming_call",
                    "session_id": active_session.session_id,
                    "caller_lang": active_session.caller_lang,
                    "recipient_lang": active_session.recipient_lang,
                    "started_at": _datetime_to_iso(active_session.started_at),
                }
            )
        )

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        async with dashboard_clients_lock:
            dashboard_clients.discard(websocket)


@app.websocket("/media-stream/{role}/{session_id}")
async def media_stream(role: str, session_id: str, websocket: WebSocket) -> None:
    if role not in {"caller", "recipient"}:
        await websocket.close(code=1008, reason="Invalid role")
        return

    await websocket.accept()
    async with sessions_lock:
        session = sessions.get(session_id)
        if not session:
            session = CallSession(
                session_id=session_id,
                caller_lang=CALLER_LANGUAGE,
                recipient_lang=RECIPIENT_LANGUAGE,
            )
            sessions[session_id] = session
        if session.started_at is None:
            session.started_at = _utc_now()

    source_leg = session.caller if role == "caller" else session.recipient
    target_leg = session.recipient if role == "caller" else session.caller
    source_lang = session.caller_lang if role == "caller" else session.recipient_lang
    target_lang = session.recipient_lang if role == "caller" else session.caller_lang

    source_leg.websocket = websocket
    source_leg.vad.reset()
    source_leg.active_lang = "" if USE_AUTO else source_lang
    source_leg.lang_votes.clear()

    try:
        while True:
            raw_msg = await websocket.receive_text()
            event = json.loads(raw_msg)
            event_type = event.get("event")

            if event_type == "start":
                source_leg.stream_sid = event.get("streamSid") or event.get("start", {}).get(
                    "streamSid"
                )
                continue

            if event_type == "media":
                payload = event.get("media", {}).get("payload")
                if not payload:
                    continue
                mulaw_chunk = base64.b64decode(payload)
                # If caller language is still unknown in auto mode, pass recipient's natural
                # audio directly to the caller rather than forcing translated TTS.
                if USE_AUTO and role == "recipient" and not session.caller.active_lang:
                    await _send_media_to_leg(session.caller, mulaw_chunk)
                    continue
                pcm_chunk = mulaw_to_pcm16le(mulaw_chunk)
                utterance = source_leg.vad.feed_pcm16_8k(pcm_chunk)
                if utterance and target_leg.websocket and target_leg.stream_sid:
                    asyncio.create_task(
                        _process_and_forward(
                            source_pcm16le_8k=utterance,
                            session=session,
                            session_id=session_id,
                            source_leg=source_leg,
                            source_role=role,
                            source_lang=source_lang,
                            target_lang=target_lang,
                            target_leg=target_leg,
                        )
                    )
                continue

            if event_type == "stop":
                break
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        print(f"[ws] {role} leg error: {exc}")
    finally:
        source_leg.websocket = None
        source_leg.stream_sid = None
        source_leg.vad.reset()
        source_leg.lang_votes.clear()
        await _cleanup_session_if_needed(session_id)
