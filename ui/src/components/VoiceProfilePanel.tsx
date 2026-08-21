import { useEffect, useState } from "react";
import { api, type VoiceProfileInfo } from "../api";

const VOICES = [
  ["zh-CN-XiaoxiaoNeural", "晓晓 · warm female (zh)"],
  ["zh-CN-YunxiNeural", "云希 · calm male (zh)"],
  ["en-US-GuyNeural", "Guy · deep male (en)"],
  ["en-US-AriaNeural", "Aria · bright female (en)"],
];

function parsePercent(s: string) {
  const n = parseInt(s.replace("+", ""), 10);
  return Number.isNaN(n) ? 0 : n;
}

export default function VoiceProfilePanel() {
  const [profiles, setProfiles] = useState<Record<string, VoiceProfileInfo>>({});
  const [active, setActive] = useState("aria");
  const [draft, setDraft] = useState<VoiceProfileInfo | null>(null);
  const [testing, setTesting] = useState(false);

  const load = () => api<Record<string, VoiceProfileInfo>>("/api/voice/profiles").then(setProfiles);
  useEffect(() => {
    load().catch(() => {});
  }, []);
  useEffect(() => {
    if (profiles[active] && !draft) setDraft(profiles[active]);
  }, [profiles, active, draft]);

  const save = async () => {
    if (!draft) return;
    await api("/api/voice/profiles", { method: "POST", body: JSON.stringify(draft) });
    await load();
  };

  const test = async () => {
    if (!draft) return;
    setTesting(true);
    try {
      await api("/api/speak", {
        method: "POST",
        body: JSON.stringify({
          text: `这是语音档案 ${draft.name} 的试听效果。Rate ${draft.rate}, pitch ${draft.pitch}.`,
          profile: draft.name,
        }),
      });
    } finally {
      setTesting(false);
    }
  };

  if (!draft) return null;
  const rate = parsePercent(draft.rate);
  const pitch = parsePercent(draft.pitch);
  const volume = parsePercent(draft.volume);

  return (
    <div className="panel">
      <h2>Voice Studio</h2>
      <div className="profile-grid">
        {Object.values(profiles).map((p) => (
          <div
            key={p.name}
            className={`profile-card ${p.name === active ? "active" : ""}`}
            onClick={() => {
              setActive(p.name);
              setDraft(p);
            }}
          >
            <div className="n">{p.name}</div>
            <div className="v">{p.voice}</div>
          </div>
        ))}
      </div>

      <div className="sliders">
        <label>
          <span>rate 语速</span>
          <span>{rate > 0 ? `+${rate}` : rate}%</span>
        </label>
        <input
          type="range" min={-50} max={50} value={rate}
          onChange={(e) => setDraft({ ...draft, rate: `${e.target.value >= 0 ? "+" : ""}${e.target.value}%` })}
        />
        <label>
          <span>pitch 音调</span>
          <span>{pitch > 0 ? `+${pitch}` : pitch}Hz</span>
        </label>
        <input
          type="range" min={-20} max={20} value={pitch}
          onChange={(e) => setDraft({ ...draft, pitch: `${e.target.value >= 0 ? "+" : ""}${e.target.value}Hz` })}
        />
        <label>
          <span>volume 音量</span>
          <span>{volume > 0 ? `+${volume}` : volume}%</span>
        </label>
        <input
          type="range" min={-50} max={50} value={volume}
          onChange={(e) => setDraft({ ...draft, volume: `${e.target.value >= 0 ? "+" : ""}${e.target.value}%` })}
        />
        <label>
          <span>voice 音色</span>
        </label>
        <select
          value={draft.voice}
          onChange={(e) => setDraft({ ...draft, voice: e.target.value })}
          style={{ background: "#0a1224", color: "var(--text)", border: "1px solid var(--line)", borderRadius: 8, padding: "6px 8px" }}
        >
          {VOICES.map(([id, label]) => (
            <option key={id} value={id}>{label}</option>
          ))}
        </select>
      </div>

      <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
        <button onClick={save} style={{ flex: 1, padding: "8px 0", borderRadius: 8, border: "1px solid var(--cyan)", color: "var(--cyan)", background: "transparent", cursor: "pointer" }}>
          Save profile
        </button>
        <button
          onClick={test}
          disabled={testing}
          style={{ flex: 1, padding: "8px 0", borderRadius: 8, border: "none", color: "#04121f", fontWeight: 700, cursor: "pointer", background: "linear-gradient(90deg, #22d3ee, #818cf8)" }}
        >
          {testing ? "speaking…" : "▶ Test voice"}
        </button>
      </div>
    </div>
  );
}
