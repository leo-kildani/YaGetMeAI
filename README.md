# YaGetMe AI

YaGetMe AI is a real-time, bidirectional voice translation system for live phone calls.
It is designed to remove language barriers without removing the human conversation.

## Inspiration

Several people on our team have lived the language-barrier problem at full stakes:

- Daniel's Vietnamese-speaking parents have hit dead-ends with government offices.
- Leo's Arabic-speaking family has struggled to get timely, accurate help in medical settings.
- Dylan's Malayalam-speaking parents have dealt with awkward three-way calls with rushed interprertations, leading to simply giving up when no proper translation was available.
- Ivan's Spanish-speaking parents have struggled to communicate with large retail stores.

We kept seeing the same pattern: when language breaks, human connection breaks, and phone calls become stressful, slow, and error-prone.

At the same time, customer experience research continues to show that people do not want to be pushed into fully automated systems. People still prefer live human conversations, especially for important issues.

So we asked: what if AI did not replace human support, but instead removed the language barrier so people could stay human?

## What It Does

YaGetMe AI enables two people to speak naturally in different languages during a normal phone call:

- A caller dials a standard phone number.
- Both sides speak normally in their own language.
- YaGetMe AI translates in near real time so each side hears the other in their preferred language with low-latency turn-taking.

The goal is to preserve tone, pacing, emotion, and trust while making conversations understandable.

It is applicable anywhere live calls matter, including:

- Healthcare
- Government services
- Insurance
- Legal services
- Hospitality
- Customer support
- Education
- Emergency response

## How We Built It

We built an end-to-end streaming pipeline optimized for low latency:

- **Twilio Media Streams** to bridge both call legs and maintain persistent WebSocket audio streams.
- **FastAPI** to orchestrate sessions, route audio, manage state, and broadcast live events to the dashboard.
- **NVIDIA Parakeet** (`parakeet-1.1b-rnnt-multilingual-asr`) for streaming multilingual ASR across 25 languages.
- **Googlelang** for language detection and bidirectional translation:
  - Caller -> recipient's desired language
  - Recipient -> caller's detected language
- **ElevenLabs TTS** to synthesize translated speech and stream it back to the opposite call leg.
- **React dashboard** connected over WebSocket for live transcripts and call events.
- **Post-call summarization with OpenAI** and persistence into `calls.json` for structured records.

## Challenges We Ran Into

### 1) Latency as the primary constraint

Real-time translation must feel conversational, not like voicemail. We had to reduce buffering, control chunk sizes, and optimize every stage across the pipeline.

### 2) Model and API tradeoffs

We evaluated multiple ASR, translation, and TTS options. High accuracy alone was not enough; the system also had to be consistently fast.

### 3) Architecture churn

Early designs did not reliably support long-lived bidirectional streaming. We redesigned around Twilio Media Streams for stable persistent WebSocket connections, which unlocked continuous retrieval and playback.

### 4) State synchronization

Managing two call legs, dual audio streams, live transcripts, dashboard updates, and cleanup required strict session lifecycle handling.

## Accomplishments We Are Proud Of

- Built a working real-time translator for live two-party phone calls with no app required.
- Achieved low-latency interaction that feels natural instead of robotic or delayed.
- Delivered operator visibility through live transcript streaming and automatic post-call summaries.
- Designed the platform to be industry-agnostic from day one.

## What We Learned

- People do not want AI to replace human support; they want more human outcomes.
- Real-time systems demand engineering discipline around buffering, backpressure, jitter, and cumulative latency.
- Twilio Media Streams is a powerful primitive: once bidirectional audio was reliable, the rest became a solvable streaming pipeline problem.

## What's Next

- Harden edge cases: noisy environments, crosstalk, interruptions, low bandwidth, spotty connectivity, heavy accents, and code-switching.
- Improve turn-taking: tighter barge-in handling and adaptive buffering for speed + intelligibility.
- Add optional domain vocabulary packs (medical, legal, insurance) to improve terminology accuracy.
- Expand trust/compliance capabilities: configurable retention, redaction, and audit trails for regulated environments.

## Vision

YaGetMe AI is built on a simple principle:

**AI should not replace human conversations. It should make them possible.**
