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

/** A4: stable per-agent identity (color + avatar glyph), shared with HUD. */
export const AGENT_IDENTITY: Record<string, { color: string; avatar: string }> = {
  echo: { color: "#22d3ee", avatar: "◉" },
  openai: { color: "#34d399", avatar: "✦" },
  "claude-code": { color: "#fb923c", avatar: "⌘" },
  codex: { color: "#a78bfa", avatar: "▲" },
  opencode: { color: "#60a5fa", avatar: "◈" },
};

const UNKNOWN_PALETTE = [
  "#22d3ee",
  "#34d399",
  "#fb923c",
  "#a78bfa",
  "#60a5fa",
  "#f472b6",
  "#fbbf24",
  "#2dd4bf",
];

function hashName(name: string): number {
  let h = 0;
  for (let i = 0; i < name.length; i++) {
    h = (h * 31 + name.charCodeAt(i)) >>> 0;
  }
  return h;
}

/** Resolve identity for any agent name; unknown agents get a hashed palette color. */
export function agentIdentity(raw: unknown): { color: string; avatar: string } {
  const name = String(raw ?? "").trim();
  const known = AGENT_IDENTITY[name];
  if (known) return known;
  if (!name) return { color: "#8290a8", avatar: "◇" };
  return { color: UNKNOWN_PALETTE[hashName(name) % UNKNOWN_PALETTE.length], avatar: "◇" };
}

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
      <div className="feed" role="log" aria-live="polite">
        {items.length === 0 && (
          <>
            <div className="empty-state">
              <span className="empty-glyph" aria-hidden="true" />
              <span className="empty-title">Awaiting signals</span>
              <span className="empty-hint">
                <em>dispatch a task</em> <span aria-hidden="true">✨</span>
              </span>
            </div>
            <div className="empty-skeletons" aria-hidden="true">
              {[76, 92, 58].map((w, i) => (
                <div className="feed-item sk-item" key={`sk-feed-${i}`}>
                  <span className="skeleton sk-t" />
                  <span className="skeleton sk-line" style={{ width: `${w}%` }} />
                </div>
              ))}
            </div>
          </>
        )}
        {items.map((ev) => {
          const s = EVENT_STYLE[ev.type];
          const d = ev.data as Record<string, string | number>;
          const agentName =
            d.agent !== undefined && d.agent !== null && String(d.agent) !== ""
              ? String(d.agent)
              : null;
          const ident = agentIdentity(agentName);
          const label =
            ev.type === "task.progress"
              ? `${d.agent}: ${d.current_step ?? ""} (${Math.round(Number(d.progress ?? 0) * 100)}%)`
              : ev.type === "dvoice.saying"
              ? `D-VOICE: ${String(d.text ?? "").slice(0, 90)}`
              : `${d.agent ?? "system"}: ${d.instruction ?? d.message ?? d.text ?? d.status ?? ""}`;
          return (
            <div className="feed-item" key={ev.id}>
              <span className="t">{fmt(ev.ts)}</span>
              <span style={{ color: s.color, marginRight: 6 }} aria-hidden="true">
                {s.icon}
              </span>
              {agentName && (
                <span
                  className="agent-avatar"
                  style={{ color: ident.color }}
                  title={agentName}
                  aria-hidden="true"
                >
                  {ident.avatar}
                </span>
              )}
              {String(label).slice(0, 110)}
            </div>
          );
        })}
      </div>
    </div>
  );
}
