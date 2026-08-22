import type { AgentInfo, BusEvent } from "../api";
import { agentIdentity } from "./AgentFeed";

interface TaskView {
  id: string;
  agent: string;
  instruction: string;
  progress: number;
  currentStep: string;
  status: string;
}

export default function HUD({
  agents,
  events,
  connected,
}: {
  agents: AgentInfo[];
  events: BusEvent[];
  connected: boolean;
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
