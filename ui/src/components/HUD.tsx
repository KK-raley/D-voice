import type { AgentInfo, BusEvent, ConfirmRequest, ReceiptView, StandbyInfo } from "../api";
import { agentIdentity } from "./AgentFeed";

interface TaskView {
  id: string;
  agent: string;
  instruction: string;
  progress: number;
  currentStep: string;
  status: string;
}

/** Epoch seconds -> HH:MM:SS (matches AgentFeed's timestamp style). */
function fmtTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], { hour12: false });
}

/** P0-5: mutually-exclusive three-state banner (mic off / standby / awake). */
function StandbyBanner({ standby }: { standby: StandbyInfo | null }) {
  let color = "var(--dim)";
  let label = "Microphone off";
  let sub: string | null = null;
  if (!standby) {
    label = "connecting…";
  } else if (!standby.microphone_running) {
    // gray: mic not running
  } else if (standby.state === "active" && standby.user) {
    color = "var(--ok)";
    label = `Awake · verified user: ${standby.user}`;
  } else {
    color = "var(--cyan)";
    label = "Standby · local only";
    sub = "listening for enrolled wake phrase, no LLM calls";
  }
  return (
    <div
      role="status"
      aria-live="polite"
      style={{
        display: "flex",
        alignItems: "baseline",
        gap: 10,
        flexWrap: "wrap",
        border: `1px solid ${color}`,
        borderRadius: 10,
        padding: "9px 12px",
        marginBottom: 12,
        background: `color-mix(in srgb, ${color} 8%, transparent)`,
        color,
        fontWeight: 600,
        fontSize: 14,
      }}
    >
      <span>{label}</span>
      {sub && <span style={{ color: "var(--dim)", fontWeight: 400, fontSize: 12 }}>{sub}</span>}
    </div>
  );
}

/** P0-2: high-risk confirmation card (yellow border, approve/deny). */
function ConfirmCard({
  c,
  onResolve,
}: {
  c: ConfirmRequest;
  onResolve: (id: string, approved: boolean) => void;
}) {
  return (
    <div
      style={{
        border: "1px solid var(--warn)",
        background: "color-mix(in srgb, var(--warn) 8%, transparent)",
        borderRadius: 10,
        padding: "10px 12px",
        marginBottom: 12,
        fontSize: 13,
      }}
    >
      <div style={{ display: "flex", alignItems: "baseline", gap: 8, flexWrap: "wrap", marginBottom: 6 }}>
        <span
          style={{
            fontSize: 11,
            letterSpacing: "0.22em",
            textTransform: "uppercase",
            color: "var(--warn)",
            fontWeight: 700,
          }}
        >
          Confirmation required
        </span>
        <span style={{ color: "var(--dim)", fontSize: 11 }}>
          {c.source} · expires {fmtTime(c.expires_at)}
        </span>
      </div>
      <div style={{ marginBottom: 6 }}>{c.transcript}</div>
      {c.actions.map((a, i) => (
        <div key={i} style={{ fontSize: 12, color: "var(--dim)", marginBottom: 2 }}>
          <span style={{ color: "var(--warn)" }}>{a.agent}</span>: {a.instruction}
        </div>
      ))}
      <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
        <button
          className="chip"
          style={{ color: "var(--ok)", borderColor: "var(--ok)", fontWeight: 600 }}
          onClick={() => onResolve(c.id, true)}
        >
          Approve
        </button>
        <button
          className="chip"
          style={{ color: "var(--err)", borderColor: "var(--err)", fontWeight: 600 }}
          onClick={() => onResolve(c.id, false)}
        >
          Deny
        </button>
      </div>
    </div>
  );
}

/** P0-6: latest command receipt card. */
function ReceiptCard({
  receipt,
  onCancelTasks,
}: {
  receipt: ReceiptView;
  onCancelTasks: (taskIds: string[]) => void;
}) {
  return (
    <div
      style={{
        border: "1px solid var(--line)",
        borderRadius: 10,
        padding: "10px 12px",
        marginBottom: 12,
        fontSize: 13,
        background: "rgba(148, 163, 184, 0.05)",
      }}
    >
      <div style={{ display: "flex", alignItems: "baseline", gap: 8, flexWrap: "wrap", marginBottom: 6 }}>
        <span
          style={{
            fontSize: 11,
            letterSpacing: "0.22em",
            textTransform: "uppercase",
            color: "var(--cyan)",
            fontWeight: 600,
          }}
        >
          Receipt
        </span>
        <span style={{ color: "var(--dim)", fontSize: 11 }}>{fmtTime(receipt.ts)}</span>
        {receipt.cancelled && <span className="status-badge status-idle">cancelled</span>}
      </div>
      <div style={{ marginBottom: 8 }}>{receipt.transcript}</div>
      <div style={{ display: "flex", alignItems: "center", flexWrap: "wrap", gap: 8, fontSize: 12 }}>
        <span
          className={`status-badge ${receipt.voiceprint === "accepted" ? "status-idle" : "status-offline"}`}
        >
          {receipt.voiceprint === "accepted" ? "voiceprint ✓" : "voiceprint —"}
        </span>
        <span style={{ color: receipt.local_llm ? "var(--ok)" : "var(--dim)" }}>
          {receipt.local_llm ? "local Qwen" : "remote/cloud"}
        </span>
        <span style={{ color: "var(--dim)" }}>
          {receipt.kind}
          {receipt.agents.length > 0 && ` · ${receipt.agents.join(", ")}`}
        </span>
        {receipt.cancellable && receipt.task_ids.length > 0 && !receipt.cancelled && (
          <button className="chip" onClick={() => onCancelTasks(receipt.task_ids)}>
            Cancel
          </button>
        )}
      </div>
      {receipt.reply_preview && (
        <div style={{ marginTop: 8, color: "var(--dim)", fontSize: 12 }}>{receipt.reply_preview}</div>
      )}
    </div>
  );
}

export default function HUD({
  agents,
  events,
  connected,
  standby,
  receipt,
  confirmations,
  onResolveConfirm,
  onCancelTasks,
}: {
  agents: AgentInfo[];
  events: BusEvent[];
  connected: boolean;
  standby: StandbyInfo | null;
  receipt: ReceiptView | null;
  confirmations: ConfirmRequest[];
  onResolveConfirm: (id: string, approved: boolean) => void;
  onCancelTasks: (taskIds: string[]) => void;
}) {
  // Derive live task bars from progress events
  const tasks = new Map<string, TaskView>();
  for (const ev of [...events].reverse()) {
    const d = ev.data as Record<string, string | number>;
    const id = String(d.id ?? "");
    if (!id) continue;
    if (ev.type === "task.started" && !tasks.has(id)) {
      tasks.set(id, {
        id,
        agent: String(d.agent),
        instruction: String(d.instruction),
        progress: 0,
        currentStep: "starting…",
        status: "running",
      });
    } else if (ev.type === "task.progress" && !tasks.has(id)) {
      tasks.set(id, {
        id,
        agent: String(d.agent),
        instruction: String(d.instruction),
        progress: Number(d.progress ?? 0),
        currentStep: String(d.currentStep ?? ""),
        status: "running",
      });
    } else if (ev.type === "task.progress" && tasks.has(id)) {
      tasks.get(id)!.progress = Math.max(tasks.get(id)!.progress, Number(d.progress ?? 0));
      tasks.get(id)!.currentStep = String(d.currentStep ?? tasks.get(id)!.currentStep);
    } else if ((ev.type === "task.completed" || ev.type === "task.failed") && tasks.has(id)) {
      tasks.get(id)!.status = ev.type === "task.completed" ? "completed" : "failed";
      tasks.get(id)!.progress = 1;
    }
  }
  const live = [...tasks.values()].filter((t) => t.status === "running").slice(0, 3);
  const busy = agents.some((a) => a.status === "busy");
  // agents fetch has not returned yet -> show skeleton placeholders
  const loading = agents.length === 0;

  return (
    <div className="panel">
      <h2>System HUD</h2>

      {/* P0-5: three-state standby banner (mic off / standby / awake) */}
      <StandbyBanner standby={standby} />

      {/* P0-2: pending high-risk confirmations */}
      {confirmations.map((c) => (
        <ConfirmCard key={c.id} c={c} onResolve={onResolveConfirm} />
      ))}

      {/* P0-6: latest command receipt */}
      {receipt && <ReceiptCard receipt={receipt} onCancelTasks={onCancelTasks} />}

      {/* Waveform */}
      <div className="wave-wrap" aria-hidden="true">
        {Array.from({ length: 36 }).map((_, i) => (
          <span
            key={i}
            style={{
              animationDelay: `${(i % 12) * 0.08}s`,
              animationPlayState: busy ? "running" : "paused",
              opacity: busy ? undefined : 0.35,
            }}
          />
        ))}
      </div>

      {/* Agents (skeleton cards while the agents fetch is in flight) */}
      {loading &&
        [0, 1, 2].map((i) => (
          <div className="agent-row skeleton-row" key={`sk-agent-${i}`} aria-hidden="true">
            <span className="skeleton sk-avatar" />
            <span className="skeleton sk-name" />
            <span className="skeleton sk-badge" />
            <span className="skeleton sk-desc" />
          </div>
        ))}
      {agents.map((a) => {
        const ident = agentIdentity(a.name);
        return (
          <div className="agent-row" key={a.name}>
            <span className={`dot ${a.status === "idle" ? "on" : a.status === "offline" ? "off" : "on"}`} />
            <span
              className="agent-avatar"
              style={{ color: ident.color }}
              title={a.name}
              aria-hidden="true"
            >
              {ident.avatar}
            </span>
            <span className="name">{a.name}</span>
            <span className={`status-badge status-${a.status}`}>{a.status}</span>
            <span className="desc">{a.description}</span>
          </div>
        );
      })}

      {/* Live task bars (skeleton bars until agent data is ready) */}
      {loading &&
        [0, 1].map((i) => (
          <div className="taskbar" key={`sk-task-${i}`} aria-hidden="true">
            <div className="meta">
              <span className="skeleton sk-meta-sm" />
              <span className="skeleton sk-meta-md" />
            </div>
            <div className="bar">
              <div className="skeleton sk-bar-fill" />
            </div>
          </div>
        ))}
      {live.map((t) => {
        const ident = agentIdentity(t.agent);
        return (
          <div className="taskbar" key={t.id}>
            <div className="meta">
              <span className="meta-agent">
                <span
                  className="agent-avatar"
                  style={{ color: ident.color }}
                  title={t.agent}
                  aria-hidden="true"
                >
                  {ident.avatar}
                </span>
                {t.agent}
              </span>
              <span>{t.currentStep.slice(0, 46)}</span>
            </div>
            <div className="bar">
              <div className="fill" style={{ width: `${t.progress * 100}%` }} />
            </div>
          </div>
        );
      })}

      <div style={{ marginTop: 10, fontSize: 12, color: "var(--dim)" }}>
        bus: {connected ? "streaming" : "reconnecting…"} · agents: {agents.length} · in-flight: {live.length}
      </div>
    </div>
  );
}
