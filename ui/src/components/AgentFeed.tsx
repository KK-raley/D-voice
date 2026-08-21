import { useMemo } from "react";
import type { BusEvent } from "../api";

const EVENT_STYLE: Record<string, { icon: string; color: string }> = {
  "voice.accepted": { icon: "◉", color: "#34d399" },
  "voice.rejected": { icon: "⊘", color: "#f87171" },
  "task.queued": { icon: "…", color: "#64748b" },
  "task.started": { icon: "▶", color: "#22d3ee" },
  "task.progress": { icon: " ◆ ", color: "#818cf8" },
  "task.completed": { icon: "✔", color: "#34d399" },
  "task.failed": { icon: "✖", color: "#f87171" },
  "dvoice.saying": { icon: "◈", color: "#818cf8" },
  "monitor.alert": { icon: "⚠", color: "#fbbf24" },
  "tts.speaking": { icon: "♫", color: "#22d3ee" },
};

function fmt(ts: number) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour12: false });
}

export default function AgentFeed({ events }: { events: BusEvent[] }) {
  const items = useMemo(
    () =>
      events
        .filter((e) => EVENT_STYLE[e.type])
        .slice(0, 60),
    [events]
  );

  return (
    <div className="panel" style={{ gridRow: "span 2" }}>
      <h2>Live Operations Feed</h2>
      <div className="feed">
        {items.length === 0 && (
          <div className="feed-item" style={{ color: "var(--dim)" }}>
            Awaiting events… dispatch a task or ask D-VOICE something.
          </div>
        )}
        {items.map((ev) => {
          const s = EVENT_STYLE[ev.type];
          const d = ev.data as Record<string, string | number>;
          const label =
            ev.type === "task.progress"
              ? `${d.agent}: ${d.current_step ?? ""} (${Math.round(Number(d.progress ?? 0) * 100)}%)`
              : ev.type === "dvoice.saying"
              ? `D-VOICE: ${String(d.text ?? "").slice(0, 90)}`
              : `${d.agent ?? "system"}: ${d.instruction ?? d.message ?? d.text ?? d.status ?? ""}`;
          return (
            <div className="feed-item" key={ev.id}>
              <span className="t">{fmt(ev.ts)}</span>
              <span style={{ color: s.color, marginRight: 6 }}>{s.icon}</span>
              {String(label).slice(0, 110)}
            </div>
          );
        })}
      </div>
    </div>
  );
}
