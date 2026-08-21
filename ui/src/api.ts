import { useEffect, useMemo, useState } from "react";

export interface BusEvent {
  id: string;
  ts: number;
  type: string;
  data: Record<string, unknown>;
}

export interface AgentInfo {
  name: string;
  description: string;
  capabilities: string[];
  status: "idle" | "busy" | "error" | "offline";
}

export interface VoiceProfileInfo {
  name: string;
  voice: string;
  rate: string;
  pitch: string;
  volume: string;
}

export function useEventStream(maxEvents = 120) {
  const [events, setEvents] = useState<BusEvent[]>([]);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let closed = false;
    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws`);
      ws.onopen = () => setConnected(true);
      ws.onclose = () => {
        setConnected(false);
        if (!closed) setTimeout(connect, 1500);
      };
      ws.onmessage = (msg) => {
        try {
          const ev: BusEvent = JSON.parse(msg.data);
          setEvents((prev) => [ev, ...prev].slice(0, maxEvents));
        } catch {
          /* ignore malformed */
        }
      };
    };
    connect();
    return () => {
      closed = true;
      ws?.close();
    };
  }, [maxEvents]);

  return { events, connected };
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!resp.ok) throw new Error(`${path}: ${resp.status}`);
  return resp.json() as Promise<T>;
}

export function useAgents(events: BusEvent[]) {
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  useEffect(() => {
    api<AgentInfo[]>("/api/agents").then(setAgents).catch(() => {});
    const t = setInterval(
      () => api<AgentInfo[]>("/api/agents").then(setAgents).catch(() => {}),
      5000
    );
    return () => clearInterval(t);
  }, []);
  return useMemo(() => {
    // overlay live status from the event stream
    const live = new Map(agents.map((a) => [a.name, { ...a }]));
    for (const ev of events) {
      if (ev.type === "agent.status") {
        const rec = live.get(ev.data.agent as string);
        if (rec) rec.status = ev.data.status as AgentInfo["status"];
      }
    }
    return [...live.values()];
  }, [agents, events.slice(0, 20)]);
}
