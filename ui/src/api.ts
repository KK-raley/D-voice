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

/** D-VOICE brain config + availability (GET /api/brain). */
export interface BrainInfo {
  backend: "local-qwen" | "ollama" | "openai-compatible";
  model: string;
  base_url: string | null;
  api_key_env: string;
  enabled: boolean;
  available: boolean;
  local_only: boolean;
  deployment_dir: string;
}

/** Current brain state; available is a live probe result. */
export function fetchBrain(): Promise<BrainInfo> {
  return api<BrainInfo>("/api/brain");
}

/** Switch brain backend/model at runtime (persisted to config.toml). */
export function updateBrain(
  patch: Partial<Omit<BrainInfo, "available">> & { api_key?: string }
): Promise<BrainInfo & { ok: boolean }> {
  return api<BrainInfo & { ok: boolean }>("/api/brain", {
    method: "POST",
    body: JSON.stringify(patch),
  });
}

/** Transcribe a recorded audio blob via server-side ASR (POST /api/listen). */
export function transcribeAudio(
  blob: Blob
): Promise<VoiceResult> {
  return api<VoiceResult>("/api/listen", {
    method: "POST",
    headers: { "Content-Type": blob.type || "audio/webm" },
    body: blob,
  });
}

export interface VoiceResult {
  kind?: string;
  state: string;
  user?: string | null;
  text?: string;
  reply?: string;
  reason?: string;
  microphone_running?: boolean;
  error?: string | null;
}

/** D-VOICE looks at the screen (screenshot + local OCR + optional brain answer). */
export function lookAtScreen(
  question?: string
): Promise<{ ok: boolean; title: string; engine: string; text: string; reply: string }> {
  return api("/api/vision/look", {
    method: "POST",
    body: JSON.stringify({ question: question ?? null }),
  });
}

/** ScreenWatcher state (independent monitoring channel). */
export interface VisionState {
  running: boolean;
  interval_s: number;
  observations: Array<{ title: string; engine: string; lines: number; text: string; ts: number }>;
}

export function fetchVisionState(): Promise<VisionState> {
  return api<VisionState>("/api/vision/state");
}

export function setScreenWatch(
  enabled: boolean,
  interval_s?: number
): Promise<{ ok: boolean } & VisionState> {
  return api("/api/vision/watch", {
    method: "POST",
    body: JSON.stringify({ enabled, interval_s: interval_s ?? null }),
  });
}

export function useEventStream(maxEvents = 120) {
  const [events, setEvents] = useState<BusEvent[]>([]);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let closed = false;
    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const token = sessionStorage.getItem("vocalis-token") || "";
      ws = new WebSocket(`${proto}://${location.host}/ws${token ? `?token=${encodeURIComponent(token)}` : ""}`);
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
  const headers = { ...apiHeaders(), ...(init?.headers ?? {}) };
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

export function apiHeaders(): Record<string, string> {
  const token = sessionStorage.getItem("vocalis-token");
  return { "Content-Type": "application/json", ...(token ? { "X-Vocalis-Token": token } : {}) };
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

/* ---------- P0 trust UX: standby banner, command receipt, confirmations ---------- */

/** Response of GET /api/standby (drives the three-state HUD banner). */
export interface StandbyInfo {
  state: "standby" | "active";
  user: string | null;
  microphone_running: boolean;
  reason?: string;
  error?: string | null;
}

export function getStandby(): Promise<StandbyInfo> {
  return api<StandbyInfo>("/api/standby");
}

/** Data payload of the command.receipt bus event (P0-6). */
export interface CommandReceipt {
  transcript: string;
  kind: string;
  user: string | null;
  voiceprint: string | null;
  local_llm: boolean;
  agents: string[];
  task_ids: string[];
  reply_preview: string;
  confirmation_id: string | null;
  cancellable: boolean;
}

/** Latest receipt as held by App: adds capture time (epoch s) + cancel state. */
export interface ReceiptView extends CommandReceipt {
  ts: number;
  cancelled: boolean;
}

/** Data payload of the confirm.requested bus event (P0-2). */
export interface ConfirmRequest {
  id: string;
  source: string;
  transcript: string;
  actions: Array<{ agent: string; instruction: string }>;
  created_at: number;
  expires_at: number;
}

/** Approve or deny a high-risk confirmation (POST /api/confirmations/{id}). */
export function resolveConfirmation(id: string, approved: boolean): Promise<unknown> {
  return api(`/api/confirmations/${encodeURIComponent(id)}`, {
    method: "POST",
    body: JSON.stringify({ approved }),
  });
}

/** Abort a running agent dispatch from the receipt card (POST /api/tasks/{id}/cancel). */
export function cancelTask(id: string): Promise<{ ok: boolean; cancelled: string }> {
  return api(`/api/tasks/${encodeURIComponent(id)}/cancel`, { method: "POST" });
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
