import { useEffect, useMemo, useRef, useState } from "react";

type TranscriptSegment = {
  role: "caller" | "recipient";
  original: string;
  translated: string;
  ts?: string;
};

type CallRecord = {
  call_id: string;
  session_id: string;
  started_at?: string | null;
  ended_at?: string | null;
  duration_seconds?: number | null;
  caller_lang?: string;
  recipient_lang?: string;
  segments: TranscriptSegment[];
  summary: string;
};

type DashboardIncomingCallEvent = {
  event: "incoming_call";
  session_id: string;
  caller_lang?: string;
  recipient_lang?: string;
  started_at?: string;
};

type DashboardTranscriptEvent = {
  event: "transcript";
  session_id: string;
  role: "caller" | "recipient";
  original: string;
  translated: string;
  ts?: string;
};

type DashboardCallEndedEvent = CallRecord & {
  event: "call_ended";
};

type DashboardEvent =
  | DashboardIncomingCallEvent
  | DashboardTranscriptEvent
  | DashboardCallEndedEvent;

type ActiveCall = {
  sessionId: string;
  startedAt: string;
  callerLang: string;
  recipientLang: string;
};

const apiBase = (
  import.meta.env.VITE_API_URL?.toString().replace(/\/$/, "") ||
  "http://localhost:8000"
);

const wsBase = (
  import.meta.env.VITE_WS_URL?.toString().replace(/\/$/, "") ||
  apiBase.replace(/^http/i, "ws")
);

function formatDateTime(value?: string | null): string {
  if (!value) return "Unknown time";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Unknown time";
  return date.toLocaleString();
}

function formatDuration(seconds?: number | null): string {
  if (seconds == null || Number.isNaN(seconds)) return "--";
  const total = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(total / 60);
  const remainder = total % 60;
  if (minutes === 0) return `${remainder}s`;
  return `${minutes}m ${remainder}s`;
}

function App() {
  const [previousCalls, setPreviousCalls] = useState<CallRecord[]>([]);
  const [selectedCallId, setSelectedCallId] = useState<string | null>(null);
  const [activeCall, setActiveCall] = useState<ActiveCall | null>(null);
  const [liveSegments, setLiveSegments] = useState<TranscriptSegment[]>([]);
  const [isSocketConnected, setIsSocketConnected] = useState(false);
  const [socketError, setSocketError] = useState<string | null>(null);
  const [nowTick, setNowTick] = useState(Date.now());
  const activeSessionIdRef = useRef<string | null>(null);

  const selectedCall = useMemo(
    () => previousCalls.find((call) => call.call_id === selectedCallId) ?? null,
    [previousCalls, selectedCallId],
  );

  const liveCallerSegments = useMemo(
    () => liveSegments.filter((segment) => segment.role === "caller"),
    [liveSegments],
  );

  useEffect(() => {
    const interval = window.setInterval(() => {
      setNowTick(Date.now());
    }, 1000);
    return () => window.clearInterval(interval);
  }, []);

  useEffect(() => {
    async function loadCalls() {
      try {
        const response = await fetch(`${apiBase}/api/calls`);
        if (!response.ok) {
          throw new Error(`Failed to fetch calls (${response.status})`);
        }
        const payload = await response.json();
        const calls = Array.isArray(payload?.calls)
          ? payload.calls
          : Array.isArray(payload)
            ? payload
            : [];
        setPreviousCalls(calls);
      } catch (error) {
        console.error(error);
      }
    }
    void loadCalls();
  }, []);

  useEffect(() => {
    activeSessionIdRef.current = activeCall?.sessionId ?? null;
  }, [activeCall]);

  useEffect(() => {
    let socket: WebSocket | null = null;
    let reconnectTimer: number | null = null;

    const connect = () => {
      socket = new WebSocket(`${wsBase}/dashboard/ws`);

      socket.onopen = () => {
        setIsSocketConnected(true);
        setSocketError(null);
      };

      socket.onmessage = (messageEvent) => {
        try {
          const event = JSON.parse(messageEvent.data) as DashboardEvent;
          if (event.event === "incoming_call") {
            setActiveCall({
              sessionId: event.session_id,
              startedAt: event.started_at || new Date().toISOString(),
              callerLang: event.caller_lang || "unknown",
              recipientLang: event.recipient_lang || "unknown",
            });
            setLiveSegments([]);
            return;
          }

          if (event.event === "transcript") {
            if (activeSessionIdRef.current && event.session_id === activeSessionIdRef.current) {
              setLiveSegments((existing) => [
                ...existing,
                {
                  role: event.role,
                  original: event.original,
                  translated: event.translated,
                  ts: event.ts,
                },
              ]);
            }
            return;
          }

          if (event.event === "call_ended") {
            setActiveCall(null);
            setLiveSegments([]);
            setPreviousCalls((existing) => {
              const rest = existing.filter((call) => call.call_id !== event.call_id);
              return [event, ...rest];
            });
          }
        } catch (error) {
          console.error("Invalid dashboard event", error);
        }
      };

      socket.onerror = () => {
        setSocketError("Live dashboard connection interrupted.");
      };

      socket.onclose = () => {
        setIsSocketConnected(false);
        reconnectTimer = window.setTimeout(connect, 1500);
      };
    };

    connect();

    return () => {
      if (reconnectTimer) {
        window.clearTimeout(reconnectTimer);
      }
      socket?.close();
    };
  }, []);

  const liveDuration = useMemo(() => {
    if (!activeCall) return "--";
    const start = new Date(activeCall.startedAt).getTime();
    if (Number.isNaN(start)) return "--";
    return formatDuration((nowTick - start) / 1000);
  }, [activeCall, nowTick]);

  return (
    <main className="dashboard-page">
      <div className="dashboard-logo-bg" aria-hidden="true">
        <div
          className="dashboard-logo-plate"
          style={{ backgroundImage: 'url("/images/yagetme-logo.png")' }}
        />
      </div>
      <div className="ambient-grid" aria-hidden="true" />
      <section className="split-layout">
        <aside className="status-panel">
          <div className="status-head">
            <span className={`status-dot ${isSocketConnected ? "connected" : "disconnected"}`} />
            <p className="status-label">
              {isSocketConnected ? "Live Channel Connected" : "Reconnecting Live Channel"}
            </p>
          </div>

          {activeCall ? (
            <div className="status-card active">
              <h2>On Call</h2>
              <p className="session-id">Session {activeCall.sessionId.slice(0, 10)}</p>
              <dl>
                <div>
                  <dt>Elapsed</dt>
                  <dd>{liveDuration}</dd>
                </div>
                <div>
                  <dt>Caller Language</dt>
                  <dd>{activeCall.callerLang}</dd>
                </div>
                <div>
                  <dt>Recipient Language</dt>
                  <dd>{activeCall.recipientLang}</dd>
                </div>
              </dl>
            </div>
          ) : (
            <div className="status-card waiting">
              <h2>Waiting for Call</h2>
              <p>The dashboard is listening for inbound sessions.</p>
              <p className="subtle">You can review previous calls while waiting.</p>
            </div>
          )}

          {socketError && <p className="socket-error">{socketError}</p>}
        </aside>

        <section className="content-panel">
          {activeCall ? (
            <article className="panel-card transcript-card">
              <header className="panel-header">
                <h3>Translated Transcription (Caller)</h3>
                <span>{liveCallerSegments.length} segments</span>
              </header>
              <div className="transcript-list">
                {liveCallerSegments.length === 0 ? (
                  <p className="empty-state">Listening for translated caller speech...</p>
                ) : (
                  liveCallerSegments.map((segment, index) => (
                    <div className="transcript-item" key={`${segment.ts || index}-${index}`}>
                      <p className="transcript-original">{segment.original || "(no source text)"}</p>
                      <p className="transcript-translated">{segment.translated}</p>
                    </div>
                  ))
                )}
              </div>
            </article>
          ) : (
            <article className="panel-card history-card">
              <header className="panel-header">
                <h3>Previous Calls</h3>
                <span>{previousCalls.length}</span>
              </header>

              <div className="history-layout">
                <div className="history-list">
                  {previousCalls.length === 0 ? (
                    <p className="empty-state">No call history yet.</p>
                  ) : (
                    previousCalls.map((call) => (
                      <button
                        key={call.call_id}
                        className={`history-item ${selectedCallId === call.call_id ? "selected" : ""}`}
                        onClick={() => setSelectedCallId(call.call_id)}
                      >
                        <div className="history-top">
                          <strong>Session {call.session_id.slice(0, 8)}</strong>
                          <span>{formatDuration(call.duration_seconds)}</span>
                        </div>
                        <div className="history-meta">{formatDateTime(call.ended_at)}</div>
                        <p>{call.summary || "No summary available."}</p>
                      </button>
                    ))
                  )}
                </div>

                <div className="history-detail">
                  {selectedCall ? (
                    <>
                      <h4>Call Detail</h4>
                      <p className="detail-time">{formatDateTime(selectedCall.ended_at)}</p>
                      <p className="detail-duration">
                        Duration: {formatDuration(selectedCall.duration_seconds)}
                      </p>
                      <p className="detail-summary">{selectedCall.summary || "No summary available."}</p>
                      <div className="detail-transcript">
                        {(selectedCall.segments || []).map((segment, index) => (
                          <div className="transcript-item mini" key={`${segment.ts || index}-${index}`}>
                            <p className="mini-role">{segment.role}</p>
                            <p className="transcript-original">{segment.original}</p>
                            <p className="transcript-translated">{segment.translated}</p>
                          </div>
                        ))}
                      </div>
                    </>
                  ) : (
                    <p className="empty-state">Select a previous call to view summary and transcript.</p>
                  )}
                </div>
              </div>
            </article>
          )}
        </section>
      </section>
    </main>
  );
}

export default App;
