import asyncio
import base64
import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect
from twilio.rest import Client
from twilio.twiml.voice_response import Connect, VoiceResponse

from audio_utils import SimpleVAD, mulaw_to_pcm16le
from pipeline import TranslationPipeline

load_dotenv()

app = FastAPI(title="Real-Time Voice Translation")
pipeline = TranslationPipeline()
sessions_lock = asyncio.Lock()


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

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


@dataclass
class CallLeg:
    websocket: Optional[WebSocket] = None
    stream_sid: Optional[str] = None
    vad: SimpleVAD = field(default_factory=SimpleVAD)


@dataclass
class CallSession:
    session_id: str
    caller_lang: str
    recipient_lang: str
    caller: CallLeg = field(default_factory=CallLeg)
    recipient: CallLeg = field(default_factory=CallLeg)
    outbound_call_created: bool = False


sessions: dict[str, CallSession] = {}


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
    async with sessions_lock:
        session = sessions.get(session_id)
        if not session:
            return
        if session.caller.websocket is None and session.recipient.websocket is None:
            sessions.pop(session_id, None)


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
    source_lang: str,
    target_lang: str,
    target_leg: CallLeg,
) -> None:
    try:
        audio_ulaw = await asyncio.to_thread(
            pipeline.process_utterance,
            source_pcm16le_8k,
            _asr_language_hint(source_lang),
            target_lang,
        )
        await _send_media_to_leg(target_leg, audio_ulaw)
    except Exception as exc:
        print(f"[pipeline] processing failed: {exc}")


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

    source_leg = session.caller if role == "caller" else session.recipient
    target_leg = session.recipient if role == "caller" else session.caller
    source_lang = session.caller_lang if role == "caller" else session.recipient_lang
    target_lang = session.recipient_lang if role == "caller" else session.caller_lang

    source_leg.websocket = websocket
    source_leg.vad.reset()

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
                pcm_chunk = mulaw_to_pcm16le(mulaw_chunk)
                utterance = source_leg.vad.feed_pcm16_8k(pcm_chunk)
                if utterance and target_leg.websocket and target_leg.stream_sid:
                    asyncio.create_task(
                        _process_and_forward(
                            source_pcm16le_8k=utterance,
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
        await _cleanup_session_if_needed(session_id)
