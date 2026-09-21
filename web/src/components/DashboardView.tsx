import React, { useState, useEffect, useRef, useMemo } from "react";
import { Artifact, CouncilEvent, Run } from "../types";
import {
  getRuns,
  getRun,
  pauseRun,
  resumeRun,
  stopRun,
  resolveRun,
  getRunArtifacts,
  getArtifactDownloadUrl,
  exportRunZip,
} from "../api";

interface DashboardViewProps {
  initialRunId?: string | null;
}

interface LogEntry {
  id: string;
  seq: number;
  timestamp: string;
  level: "INFO" | "DEBUG" | "WARN" | "ERROR" | "REASONING" | "SUCCESS";
  type: string;
  stageId?: string;
  workerId?: string;
  message: string;
}

const ALL_EVENT_TYPES = [
  "message",
  "run.started",
  "run.completed",
  "run.paused",
  "run.resumed",
  "run.cancelled",
  "run.failed",
  "run.log",
  "stage.started",
  "stage.completed",
  "stage.released",
  "worker.started",
  "worker.delta",
  "worker.thought",
  "worker.completed",
  "worker.failed",
  "issue.recorded",
  "heartbeat",
];

export const DashboardView: React.FC<DashboardViewProps> = ({ initialRunId }) => {
  const [runs, setRuns] = useState<Run[]>([]);
  const [activeRunId, setActiveRunId] = useState<string | null>(initialRunId || null);
  const [activeRun, setActiveRun] = useState<Run | null>(null);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  
  // Real-time worker streaming thoughts & outputs
  const [workerThoughts, setWorkerThoughts] = useState<Record<string, string>>({});
  const [workerOutputs, setWorkerOutputs] = useState<Record<string, string>>({});
  const [activeWorkerStates, setActiveWorkerStates] = useState<Record<string, string>>({});

  const [connectionStatus, setConnectionStatus] = useState<"connected" | "connecting" | "closed">("connecting");
  const [selectedLogLevel, setSelectedLogLevel] = useState<string>("ALL");
  const [logSearch, setLogSearch] = useState<string>("");
  const [autoScroll, setAutoScroll] = useState<boolean>(true);
  const [exporting, setExporting] = useState(false);

  // Toast / notification state (no alerts!)
  const [toast, setToast] = useState<{ type: "info" | "success" | "error"; message: string } | null>(null);

  const eventSourceRef = useRef<EventSource | null>(null);
  const logTerminalRef = useRef<HTMLDivElement | null>(null);

  const showToast = (message: string, type: "info" | "success" | "error" = "info") => {
    setToast({ message, type });
    setTimeout(() => {
      setToast((current) => (current?.message === message ? null : current));
    }, 6000);
  };

  // Fetch runs list
  const fetchRunsList = async () => {
    try {
      const data = await getRuns();
      setRuns(data);
      if (!activeRunId && data.length > 0) {
        setActiveRunId(data[0].run_id);
      }
    } catch (err: any) {
      console.error("Failed to fetch runs list", err);
    }
  };

  useEffect(() => {
    fetchRunsList();
  }, []);

  // Fetch active run details & artifacts
  const fetchRunDetails = async (runId: string) => {
    try {
      const [runData, artsData] = await Promise.all([
        getRun(runId),
        getRunArtifacts(runId).catch(() => [] as Artifact[]),
      ]);
      setActiveRun(runData);
      setArtifacts(artsData);
    } catch (err: any) {
      console.error("Error fetching run details", err);
    }
  };

  // Auto-scroll log terminal
  useEffect(() => {
    if (autoScroll && logTerminalRef.current) {
      logTerminalRef.current.scrollTop = logTerminalRef.current.scrollHeight;
    }
  }, [logs, autoScroll]);

  // Periodic polling fallback when run is active
  useEffect(() => {
    if (!activeRunId) return;
    const interval = setInterval(() => {
      if (activeRun && ["RUNNING", "PAUSING", "PAUSED", "NEEDS_ATTENTION"].includes(activeRun.status)) {
        fetchRunDetails(activeRunId);
      }
    }, 4000);
    return () => clearInterval(interval);
  }, [activeRunId, activeRun]);

  // Connect SSE with ALL event listeners
  useEffect(() => {
    if (!activeRunId) return;

    fetchRunDetails(activeRunId);
    setLogs([]);
    setWorkerThoughts({});
    setWorkerOutputs({});
    setActiveWorkerStates({});
    setConnectionStatus("connecting");

    // Close any previous SSE connection
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
    }

    const sse = new EventSource(`/api/runs/${activeRunId}/events`);
    eventSourceRef.current = sse;

    const handleEvent = (e: MessageEvent) => {
      try {
        const ev: CouncilEvent = JSON.parse(e.data);
        const seq = ev.sequence;
        const type = ev.type || e.type || "unknown";
        const p = ev.payload || {};
        const timestamp = ev.timestamp || new Date().toISOString();

        // Determine log level and readable message
        let level: LogEntry["level"] = "INFO";
        let message = "";
        const workerId = ev.worker_id || p.worker_id;
        const stageId = ev.stage_id || p.stage_id;

        if (type === "run.log") {
          level = (p.level as LogEntry["level"]) || "INFO";
          message = p.message || JSON.stringify(p);
        } else if (type === "worker.thought") {
          level = "REASONING";
          message = p.thought || "";
          if (workerId && p.thought) {
            setWorkerThoughts((prev) => ({
              ...prev,
              [workerId]: (prev[workerId] || "") + p.thought,
            }));
            setActiveWorkerStates((prev) => ({ ...prev, [workerId]: "thinking" }));
          }
        } else if (type === "worker.delta") {
          level = "DEBUG";
          message = p.delta || "";
          if (workerId && p.delta) {
            setWorkerOutputs((prev) => ({
              ...prev,
              [workerId]: (prev[workerId] || "") + p.delta,
            }));
            setActiveWorkerStates((prev) => ({ ...prev, [workerId]: "generating" }));
          }
        } else if (type === "worker.started") {
          level = "INFO";
          message = `Worker started turn (stage: ${stageId || "N/A"})`;
          if (workerId) {
            setActiveWorkerStates((prev) => ({ ...prev, [workerId]: "started" }));
          }
        } else if (type === "worker.completed") {
          level = "SUCCESS";
          message = `Worker completed output successfully (${p.bytes || 0} bytes)`;
          if (workerId) {
            setActiveWorkerStates((prev) => ({ ...prev, [workerId]: "completed" }));
          }
        } else if (type === "worker.failed") {
          level = "ERROR";
          message = `Worker failed: ${p.error || "Unknown error"}`;
          if (workerId) {
            setActiveWorkerStates((prev) => ({ ...prev, [workerId]: "failed" }));
          }
        } else if (type === "stage.started") {
          level = "INFO";
          message = `=== Stage started: ${p.stage_id} (${p.kind || "independent"}) ===`;
        } else if (type === "stage.completed") {
          level = "INFO";
          message = `Stage completed: ${p.stage_id}. Evaluating release barrier...`;
        } else if (type === "stage.released") {
          level = "SUCCESS";
          message = `✓ Stage release barrier cleared: ${p.stage_id}. Outputs published to subsequent stages!`;
          fetchRunDetails(activeRunId);
        } else if (type === "run.started") {
          level = "INFO";
          message = `Run initialized and started execution`;
        } else if (type === "run.completed") {
          level = "SUCCESS";
          message = `🎉 Council Run COMPLETED! Final deliverables archived.`;
          fetchRunDetails(activeRunId);
        } else if (type === "run.failed") {
          level = "ERROR";
          message = `Run failed: ${p.error || "Fatal execution error"}`;
          fetchRunDetails(activeRunId);
        } else if (type === "heartbeat") {
          // Keepalive event; no log needed
          return;
        } else {
          message = `${type}: ${JSON.stringify(p)}`;
        }

        // Add to structured logs
        const newEntry: LogEntry = {
          id: `${seq}_${type}_${Date.now()}`,
          seq,
          timestamp,
          level,
          type,
          stageId,
          workerId,
          message,
        };

        setLogs((prev) => {
          if (prev.some((entry) => entry.seq === seq && entry.type === type && entry.message === message)) {
            return prev;
          }
          return [...prev, newEntry];
        });

        // Trigger detail refresh on milestone events
        if (["stage.released", "run.completed", "stage.started", "worker.completed", "run.paused"].includes(type)) {
          fetchRunDetails(activeRunId);
        }
      } catch (err) {
        console.warn("SSE parse error", err);
      }
    };

    // Attach listeners for ALL standard and council event types
    ALL_EVENT_TYPES.forEach((type) => {
      sse.addEventListener(type, handleEvent);
    });

    sse.onopen = () => {
      setConnectionStatus("connected");
    };

    sse.onerror = () => {
      setConnectionStatus("connecting");
    };

    return () => {
      ALL_EVENT_TYPES.forEach((type) => {
        sse.removeEventListener(type, handleEvent);
      });
      sse.close();
      setConnectionStatus("closed");
    };
  }, [activeRunId]);

  const handlePause = async () => {
    if (!activeRunId) return;
    try {
      await pauseRun(activeRunId);
      showToast("Run pause requested.", "info");
      fetchRunDetails(activeRunId);
    } catch (err: any) {
      showToast("Pause error: " + err.message, "error");
    }
  };

  const handleResume = async () => {
    if (!activeRunId) return;
    try {
      await resumeRun(activeRunId);
      showToast("Run resumed.", "success");
      fetchRunDetails(activeRunId);
    } catch (err: any) {
      showToast("Resume error: " + err.message, "error");
    }
  };

  const handleStop = async () => {
    if (!activeRunId || !confirm("Stop run and terminate active processes?")) return;
    try {
      await stopRun(activeRunId);
      showToast("Run stopped.", "info");
      fetchRunDetails(activeRunId);
    } catch (err: any) {
      showToast("Stop error: " + err.message, "error");
    }
  };

  const handleResolve = async () => {
    if (!activeRunId) return;
    const note = prompt("Enter resolution explanation for resuming this run:", "Checked and verified.");
    if (note === null) return;
    try {
      await resolveRun(activeRunId, "retry", note);
      showToast("Run attention resolved, resuming...", "success");
      fetchRunDetails(activeRunId);
    } catch (err: any) {
      showToast("Resolve error: " + err.message, "error");
    }
  };

  const handleExport = async () => {
    if (!activeRunId) return;
    setExporting(true);
    try {
      const blob = await exportRunZip(activeRunId, true);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `council_run_${activeRunId}.zip`;
      a.click();
      URL.revokeObjectURL(url);
      showToast("Export dossier downloaded!", "success");
    } catch (err: any) {
      showToast("Export failed: " + err.message, "error");
    } finally {
      setExporting(false);
    }
  };

  const getStatusBadge = (status: string) => {
    const s = (status || "unknown").toUpperCase();
    let cls = "badge-gray";
    if (s === "COMPLETED") cls = "badge-green";
    else if (s === "RUNNING") cls = "badge-blue";
    else if (s === "PAUSED") cls = "badge-yellow";
    else if (s === "NEEDS_ATTENTION") cls = "badge-red";
    else if (s === "CANCELLED" || s === "FAILED") cls = "badge-red";
    return <span className={`badge ${cls}`}>{s}</span>;
  };

  // Filter logs by level and text
  const filteredLogs = useMemo(() => {
    return logs.filter((log) => {
      if (selectedLogLevel !== "ALL" && log.level !== selectedLogLevel) {
        return false;
      }
      if (logSearch.trim()) {
        const query = logSearch.toLowerCase();
        const msg = log.message.toLowerCase();
        const wid = (log.workerId || "").toLowerCase();
        const sid = (log.stageId || "").toLowerCase();
        return msg.includes(query) || wid.includes(query) || sid.includes(query);
      }
      return true;
    });
  }, [logs, selectedLogLevel, logSearch]);

  const activeWorkerIds = Object.keys(workerThoughts).length > 0
    ? Object.keys(workerThoughts)
    : activeRun?.workers?.map((w) => w.worker_id) || [];

  return (
    <div className="view-container dashboard-layout">
      {/* Toast Notification Banner */}
      {toast && (
        <div style={{ gridColumn: "1 / -1" }}>
          <div className={`toast-banner toast-${toast.type}`}>
            <span>{toast.message}</span>
            <button className="toast-close" onClick={() => setToast(null)}>✕</button>
          </div>
        </div>
      )}

      {/* Runs History Sidebar */}
      <div className="dashboard-sidebar">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <h3>Council Runs</h3>
          <button className="btn btn-sm btn-outline" onClick={fetchRunsList} title="Refresh runs list">
            ↻
          </button>
        </div>
        <ul className="runs-nav-list">
          {runs.map((r) => (
            <li
              key={r.run_id}
              className={r.run_id === activeRunId ? "active" : ""}
              onClick={() => setActiveRunId(r.run_id)}
            >
              <div className="run-nav-title">{r.name}</div>
              <div className="run-nav-sub">
                {getStatusBadge(r.status)}
                <span className="text-muted text-xs">
                  {new Date(r.created_at).toLocaleTimeString()}
                </span>
              </div>
            </li>
          ))}
        </ul>
      </div>

      {/* Main Run Monitor */}
      <div className="dashboard-main">
        {activeRun ? (
          <>
            <div className="run-header card">
              <div className="run-header-top">
                <div>
                  <h2>{activeRun.name}</h2>
                  <p className="subtitle" style={{ marginBottom: "8px" }}>{activeRun.goal}</p>
                  <div style={{ display: "flex", gap: "10px", alignItems: "center" }}>
                    <code className="text-muted text-xs">ID: {activeRun.run_id}</code>
                    <span className="text-muted text-xs">·</span>
                    <span className={`text-xs ${connectionStatus === "connected" ? "text-green" : "text-muted"}`}>
                      ● Stream: {connectionStatus}
                    </span>
                  </div>
                </div>
                <div className="run-header-status">
                  {getStatusBadge(activeRun.status)}
                  {Boolean(activeRun.dissent_recorded) && (
                    <span className="badge badge-purple" title="Dissent preserved in deliverable">
                      ⚖️ Dissent Recorded
                    </span>
                  )}
                </div>
              </div>

              {/* Action Buttons */}
              <div className="run-actions-bar">
                {activeRun.status === "RUNNING" && (
                  <button className="btn btn-sm btn-outline" onClick={handlePause}>
                    ⏸ Pause Run
                  </button>
                )}
                {activeRun.status === "PAUSED" && (
                  <button className="btn btn-sm btn-primary" onClick={handleResume}>
                    ▶ Resume Run
                  </button>
                )}
                {activeRun.status === "NEEDS_ATTENTION" && (
                  <button className="btn btn-sm btn-primary" onClick={handleResolve}>
                    🔧 Resolve Attention
                  </button>
                )}
                {["RUNNING", "PAUSED", "NEEDS_ATTENTION"].includes(activeRun.status) && (
                  <button className="btn btn-sm btn-danger-outline" onClick={handleStop}>
                    ⏹ Stop Run
                  </button>
                )}
                <button
                  className="btn btn-sm btn-outline"
                  onClick={handleExport}
                  disabled={exporting}
                >
                  {exporting ? "Exporting..." : "📦 Export Dossier (ZIP)"}
                </button>
              </div>
            </div>

            {/* Stages Progression */}
            <div className="dashboard-stages card">
              <h3>Stages & Release Barriers</h3>
              <div className="stages-row">
                {activeRun.stages?.map((stage: any, idx: number) => {
                  const isCompleted = stage.status === "COMPLETED";
                  const isCurrent = activeRun.current_stage_id === stage.stage_id;
                  return (
                    <div
                      key={stage.stage_id}
                      className={`stage-pill-box ${
                        isCompleted ? "stage-done" : isCurrent ? "stage-active" : "stage-pending"
                      }`}
                    >
                      <div className="stage-pill-header">
                        <span className="stage-num-badge">{idx + 1}</span>
                        <strong>{stage.stage_id}</strong>
                      </div>
                      <div className="text-muted text-xs">{stage.kind}</div>
                      <div className="stage-status-row">
                        {getStatusBadge(stage.status)}
                        {stage.released_at && <span className="text-xs text-green">✓ Released</span>}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>

            {/* Live Model Reasoning & Deliberation Section */}
            <div className="card">
              <div className="reasoning-header">
                <div className="reasoning-title">
                  <span className="reasoning-pulse" />
                  <span>Live Model Deliberation & Thought Streams</span>
                </div>
                <span className="text-muted text-xs">
                  Real-time cognitive thinking & output generation from participating models
                </span>
              </div>

              {activeWorkerIds.length === 0 ? (
                <p className="text-muted text-xs">Waiting for worker dispatch...</p>
              ) : (
                <div style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
                  {activeWorkerIds.map((wid) => {
                    const workerInfo = activeRun.workers?.find((w) => w.worker_id === wid);
                    const thought = workerThoughts[wid];
                    const output = workerOutputs[wid];
                    const state = activeWorkerStates[wid] || "idle";

                    return (
                      <div key={wid} className="reasoning-box">
                        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                          <div>
                            <strong style={{ color: "#c084fc", fontSize: "14px" }}>
                              🧠 {workerInfo?.role || wid}
                            </strong>{" "}
                            <span className="text-muted text-xs">
                              (<code>{wid}</code> · model: <code>{workerInfo?.model || "active"}</code>)
                            </span>
                          </div>
                          <div>
                            <span className={`badge badge-sm ${state === "thinking" ? "badge-purple" : state === "generating" ? "badge-blue" : "badge-gray"}`}>
                              {state.toUpperCase()}
                            </span>
                          </div>
                        </div>

                        {thought ? (
                          <div>
                            <div className="text-muted text-xs" style={{ marginBottom: "4px" }}>
                              Model Reasoning / Chain-of-Thought:
                            </div>
                            <div className="reasoning-body">{thought}</div>
                          </div>
                        ) : null}

                        {output ? (
                          <div style={{ marginTop: "4px" }}>
                            <div className="text-muted text-xs" style={{ marginBottom: "4px" }}>
                              Streaming Draft Content:
                            </div>
                            <div
                              className="reasoning-body"
                              style={{ maxHeight: "160px", color: "#e2e8f0", background: "rgba(0,0,0,0.6)" }}
                            >
                              {output}
                            </div>
                          </div>
                        ) : null}

                        {!thought && !output && (
                          <p className="text-muted text-xs" style={{ margin: 0 }}>
                            Ready or awaiting turn execution in stage...
                          </p>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>

            {/* Live Deliberation & Engine Logs Terminal */}
            <div className="card terminal-card">
              <div className="terminal-header">
                <div>
                  <h3>Live Engine & Deliberation Logs</h3>
                  <span className="text-muted text-xs">
                    Comprehensive sequenced log stream with debug levels, stage barriers, and worker events
                  </span>
                </div>

                <div className="terminal-filters">
                  <input
                    type="text"
                    className="terminal-search"
                    placeholder="Search logs..."
                    value={logSearch}
                    onChange={(e) => setLogSearch(e.target.value)}
                  />
                  {["ALL", "INFO", "REASONING", "SUCCESS", "WARN", "ERROR", "DEBUG"].map((lvl) => (
                    <button
                      key={lvl}
                      className={`filter-btn ${selectedLogLevel === lvl ? "active" : ""}`}
                      onClick={() => setSelectedLogLevel(lvl)}
                    >
                      {lvl === "REASONING" ? "🧠 REASONING" : lvl}
                    </button>
                  ))}
                  <label style={{ display: "flex", alignItems: "center", gap: "6px", fontSize: "12px", cursor: "pointer", color: "var(--text-secondary)" }}>
                    <input
                      type="checkbox"
                      checked={autoScroll}
                      onChange={(e) => setAutoScroll(e.target.checked)}
                    />
                    Auto-Scroll
                  </label>
                </div>
              </div>

              <div className="events-stream-box" ref={logTerminalRef}>
                {filteredLogs.length === 0 ? (
                  <p className="text-muted text-xs">
                    {connectionStatus === "connected"
                      ? "Listening for events... Council deliberation logs will stream here in real-time."
                      : "Connecting to real-time event stream..."}
                  </p>
                ) : (
                  filteredLogs.map((log) => {
                    const badgeClass = log.level.toLowerCase();
                    return (
                      <div key={log.id} className="event-item">
                        <span className="event-seq">#{log.seq}</span>
                        <span className="event-time">
                          {new Date(log.timestamp).toLocaleTimeString()}
                        </span>
                        <span className={`event-level-badge ${badgeClass}`}>
                          {log.level}
                        </span>
                        {(log.stageId || log.workerId) && (
                          <span className="event-context">
                            [{log.stageId ? `stage:${log.stageId}` : ""}{log.workerId ? ` worker:${log.workerId}` : ""}]
                          </span>
                        )}
                        <span className="event-msg">{log.message}</span>
                      </div>
                    );
                  })
                )}
              </div>
            </div>

            {/* Workers Cards */}
            <div className="dashboard-workers card">
              <h3>Participating Workers ({activeRun.workers?.length || 0})</h3>
              <div className="workers-grid">
                {activeRun.workers?.map((w: any) => (
                  <div key={w.worker_id} className="worker-card">
                    <div className="worker-header">
                      <strong>{w.worker_id}</strong>
                      {getStatusBadge(w.status)}
                    </div>
                    <div className="text-xs text-muted">Role: {w.role}</div>
                    <div className="text-xs">
                      Account: <code>{w.account_ref}</code>
                    </div>
                    <div className="text-xs">
                      Model: <code>{w.model}</code>
                    </div>
                  </div>
                ))}
              </div>
            </div>

            {/* Released Artifacts List */}
            <div className="dashboard-artifacts card">
              <h3>Released Artifacts & Deliverables ({artifacts.length})</h3>
              {artifacts.length === 0 ? (
                <p className="text-muted text-xs">
                  No artifacts released yet. Stage release barriers will reveal outputs upon completion.
                </p>
              ) : (
                <div className="artifacts-list">
                  {artifacts.map((art) => (
                    <div key={art.artifact_id} className="artifact-item">
                      <div>
                        <strong>{art.name}</strong>
                        <div className="text-muted text-xs">
                          {art.media_type} · {(art.byte_size / 1024).toFixed(1)} KB · sha256:{" "}
                          <code>{art.content_hash?.substring(0, 12)}...</code>
                        </div>
                      </div>
                      <a
                        href={getArtifactDownloadUrl(art.artifact_id)}
                        target="_blank"
                        rel="noreferrer"
                        className="btn btn-sm btn-outline"
                        download={art.name}
                      >
                        Download
                      </a>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </>
        ) : (
          <div className="empty-state">
            <p>Select a run from the sidebar or launch a new run.</p>
          </div>
        )}
      </div>
    </div>
  );
};
