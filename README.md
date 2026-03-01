# TransMate

Real-time voice translation over phone calls. Callers speak in one language; the system transcribes (ASR), translates, and synthesizes (TTS) so the recipient hears the message in their language, and vice versa. Includes a recipient dashboard for live transcripts and call history.

---

## Authors

- **Dylan Matthews**
- **Daniel Nguyen**
- **Ryan Johnson**
- **Ivan Riviera**
- **Leonardo Kildani**

---

## Prerequisites

- **Python 3.10+** (3.14 users: see note in [Installation](#installation) about `nvidia-riva-client`)
- **Node.js 18+** and npm (for the dashboard)
- **Twilio** account (phone numbers, webhooks)
- **ElevenLabs** API key and voice IDs
- **NVIDIA** API key and ASR function ID (NVIDIA Riva / Parakeet ASR)
- **OpenAI** API key (optional; for call summaries)

---

## Installation

### 1. Clone the repository

```bash
git clone <repository-url>
cd transmate
```

### 2. Backend (Python)

Create a virtual environment and install dependencies:

```bash
python -m venv venv
source venv/bin/activate   # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

**Note:** On Python 3.14, if `nvidia-riva-client` fails to install, try:

```bash
pip install --no-deps nvidia-riva-client
pip install -r requirements.txt
```

### 3. Environment variables (backend)

Copy or create a `.env` file in the **project root** with at least:

```env
# Twilio
TWILIO_ACCOUNT_SID=your_account_sid
TWILIO_AUTH_TOKEN=your_auth_token
TWILIO_PHONE_NUMBER=+1xxxxxxxxxx
RECIPIENT_PHONE_NUMBER=+1xxxxxxxxxx

# Languages (caller speaks this; recipient hears translation in RECIPIENT_LANGUAGE)
CALLER_LANGUAGE=es
RECIPIENT_LANGUAGE=en
USE_AUTO=true

# NVIDIA (ASR)
NVIDIA_API_KEY=your_nvidia_api_key
NVIDIA_ASR_FUNCTION_ID=your_function_id
# or: NVIDIA_ASR_MODEL_FUNCTION_ID=your_function_id

# ElevenLabs (TTS)
ELEVENLABS_API_KEY=your_elevenlabs_api_key
ELEVENLABS_ENGLISH_VOICE_ID=...
ELEVENLABS_SPANISH_VOICE_ID=...
ELEVENLABS_ARABIC_VOICE_ID=...
ELEVENLABS_VIETNAMESE_VOICE_ID=...

# Optional: public URL for Twilio webhooks (e.g. ngrok)
PUBLIC_URL=localhost:8000

# Optional: call summaries (dashboard)
OPENAI_API_KEY=your_openai_api_key
```

Create the data directory for call history (optional):

```bash
mkdir -p data
```

### 4. Dashboard (React + Vite)

```bash
cd dashboard
npm install
```

Create `dashboard/.env` (see `dashboard/.env.example`):

```env
VITE_API_URL=http://localhost:8000
# Optional if API and WS host differ:
# VITE_WS_URL=ws://localhost:8000
```

---

## Running the program

You need **two processes**: the FastAPI backend and (optionally) the dashboard.

### 1. Start the backend

From the **project root** (where `main.py` is), with your virtual environment activated:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

The API and WebSocket endpoints will be available at `http://localhost:8000`.

### 2. Expose the backend to the internet (for Twilio)

Twilio must reach your server for incoming calls. Use a tunnel, e.g. ngrok:

```bash
ngrok http 8000
```

Set `PUBLIC_URL` in `.env` to your ngrok URL (e.g. `https://xxxx.ngrok-free.app`), and point your Twilio phone number’s voice webhook to:

- `https://<your-public-url>/incoming-call` (or the route you use for the TwiML webhook)

### 3. Start the dashboard (optional)

From the `dashboard` directory:

```bash
npm run dev
```

Open **http://localhost:5173** to view the recipient dashboard (live transcripts, call history, summaries).

---

## Project structure

| Path | Description |
|------|-------------|
| `main.py` | FastAPI app: Twilio webhooks, WebSocket media streams, call orchestration, dashboard WS |
| `pipeline.py` | Translation pipeline: NVIDIA ASR → Googletrans → ElevenLabs TTS (ulaw_8000) |
| `audio_utils.py` | VAD, mulaw ↔ PCM conversion |
| `data/calls.json` | Call history and summaries (optional) |
| `dashboard/` | React + TypeScript + Vite recipient dashboard |
| `.env` | API keys and config (root; do not commit) |
| `requirements.txt` | Python dependencies |

---

## License

See repository for license information.
