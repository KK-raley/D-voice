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

/** One entry of the Edge-TTS voice catalog (GET /api/voices). */
export interface EdgeVoice {
  ShortName: string;
  Gender: string;
  Locale: string;
}

/** Preset payload without the name key (GET /api/voice/presets values). */
export interface VoicePresetInfo {
  voice: string;
  rate: string;
  pitch: string;
  volume: string;
}

/** Response of POST /api/voice/presets. */
export interface ApplyPresetResult {
  ok: boolean;
  profile: VoiceProfileInfo;
  default_profile: string;
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
          if (!ev.id || !ev.type) return; // ignore malformed frames
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
  const headers = { "Content-Type": "application/json", ...(init?.headers ?? {}) };
  const resp = await fetch(path, { ...init, headers });
  if (!resp.ok) {
    let detail = `${resp.status}`;
    try {
      detail += `: ${JSON.stringify(await resp.json()).slice(0, 200)}`;
    } catch {
      /* no body */
    }
    throw new Error(`${path} ${detail}`);
  }
  return resp.json() as Promise<T>;
}

/** Edge-TTS voice catalog; optional locale (prefix, e.g. "zh") and gender filters. */
export function fetchVoices(locale?: string, gender?: string): Promise<EdgeVoice[]> {
  const params = new URLSearchParams();
  if (locale) params.set("locale", locale);
  if (gender) params.set("gender", gender);
  const qs = params.toString();
  return api<EdgeVoice[]>(`/api/voices${qs ? `?${qs}` : ""}`);
}

/** Scenario presets keyed by name (focus / evening / presentation). */
export function fetchVoicePresets(): Promise<Record<string, VoicePresetInfo>> {
  return api<Record<string, VoicePresetInfo>>("/api/voice/presets");
}

/** Apply a preset: upserts it as a profile and makes it the default. */
export function applyVoicePreset(name: string): Promise<ApplyPresetResult> {
  return api<ApplyPresetResult>("/api/voice/presets", {
    method: "POST",
    body: JSON.stringify({ name }),
  });
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
    const live = new Map(agents.map((a) => [a.name, { ...a }]));
    // events[0] is newest: replay old -> new so the newest status wins.
    const window = events.slice(0, 20).reverse();
    for (const ev of window) {
      if (ev.type === "agent.status") {
        const rec = live.get(ev.data.agent as string);
        if (rec) rec.status = ev.data.status as AgentInfo["status"];
      }
    }
    return [...live.values()];
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agents, events[0], events.length]);
}
