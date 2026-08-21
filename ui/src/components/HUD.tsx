import type { AgentInfo, BusEvent } from "../api";

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

  return (
    <div className="panel">
      <h2>System HUD</h2>

      {/* Waveform */}
      <div className="wave-wrap">
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

      {/* Agents */}
      {agents.map((a) => (
        <div className="agent-row" key={a.name}>
          <span className={`dot ${a.status === "idle" ? "on" : a.status === "offline" ? "off" : "on"}`} />
          <span className="name">{a.name}</span>
          <span className={`status-badge status-${a.status}`}>{a.status}</span>
          <span className="desc">{a.description}</span>
        </div>
      ))}

      {/* Live task bars */}
      {live.map((t) => (
        <div className="taskbar" key={t.id}>
          <div className="meta">
            <span>{t.agent}</span>
            <span>{t.currentStep.slice(0, 46)}</span>
          </div>
          <div className="bar">
            <div className="fill" style={{ width: `${t.progress * 100}%` }} />
          </div>
        </div>
      ))}

      <div style={{ marginTop: 10, fontSize: 12, color: "var(--dim)" }}>
        bus: {connected ? "streaming" : "reconnecting…"} · agents: {agents.length} · in-flight: {live.length}
      </div>
    </div>
  );
}
