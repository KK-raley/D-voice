import { useEffect, useMemo, useState, type CSSProperties } from "react";
import {
  api,
  applyVoicePreset,
  fetchVoices,
  type EdgeVoice,
  type VoiceProfileInfo,
} from "../api";

/** Offline fallback when /api/voices is unreachable (mirrors the backend list). */
const FALLBACK_VOICES: EdgeVoice[] = [
  { ShortName: "zh-CN-XiaoxiaoNeural", Gender: "Female", Locale: "zh-CN" },
  { ShortName: "zh-CN-YunxiNeural", Gender: "Male", Locale: "zh-CN" },
  { ShortName: "en-US-GuyNeural", Gender: "Male", Locale: "en-US" },
  { ShortName: "en-US-AriaNeural", Gender: "Female", Locale: "en-US" },
];

const PRESETS = [
  ["focus", "🎯 Focus 深度工作"],
  ["evening", "🌙 Evening 放松"],
  ["presentation", "🎤 Presentation 演示"],
] as const;

const selectStyle: CSSProperties = {
  background: "#0a1224",
  color: "var(--text)",
  border: "1px solid var(--line)",
  borderRadius: 8,
  padding: "6px 8px",
};

function parsePercent(s: string) {
  const n = parseInt(s.replace("+", ""), 10);
  return Number.isNaN(n) ? 0 : n;
}

export default function VoiceProfilePanel() {
  const [profiles, setProfiles] = useState<Record<string, VoiceProfileInfo>>({});
  const [active, setActive] = useState("aria");
  const [draft, setDraft] = useState<VoiceProfileInfo | null>(null);
  const [testing, setTesting] = useState(false);
  const [applying, setApplying] = useState<string | null>(null);

  // Voice catalog fetched from the backend; silently falls back to the
  // built-in four entries on failure.
  const [allVoices, setAllVoices] = useState<EdgeVoice[]>(FALLBACK_VOICES);
  const [localeFilter, setLocaleFilter] = useState("All");
  const [genderFilter, setGenderFilter] = useState("All");

  const load = () => api<Record<string, VoiceProfileInfo>>("/api/voice/profiles").then(setProfiles);
  useEffect(() => {
    load().catch(() => {});
    fetchVoices().then(setAllVoices).catch(() => {});
  }, []);
  useEffect(() => {
    if (profiles[active] && !draft) setDraft(profiles[active]);
  }, [profiles, active, draft]);

  const save = async () => {
    if (!draft) return;
    await api("/api/voice/profiles", { method: "POST", body: JSON.stringify(draft) });
    await load();
  };

  const applyPreset = async (name: string) => {
    setApplying(name);
    try {
      const res = await applyVoicePreset(name);
      await load();
      setActive(res.default_profile);
      setDraft(res.profile);
    } finally {
      setApplying(null);
    }
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

  // Locale options derived from the catalog (language prefix, deduped).
  const locales = useMemo(() => {
    const langs = new Set(
      allVoices.map((v) => v.Locale.split("-")[0]).filter(Boolean)
    );
    return ["All", ...Array.from(langs).sort()];
  }, [allVoices]);

  const filteredVoices = useMemo(
    () =>
      allVoices.filter(
        (v) =>
          (localeFilter === "All" ||
            v.Locale.toLowerCase().startsWith(localeFilter.toLowerCase())) &&
          (genderFilter === "All" || v.Gender === genderFilter)
      ),
    [allVoices, localeFilter, genderFilter]
  );

  if (!draft) return null;
  const rate = parsePercent(draft.rate);
  const pitch = parsePercent(draft.pitch);
  const volume = parsePercent(draft.volume);

  return (
    <div className="panel">
      <h2>Voice Studio</h2>

      <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
        {PRESETS.map(([name, label]) => (
          <button
            key={name}
            onClick={() => applyPreset(name)}
            disabled={applying !== null}
            style={{
              flex: 1,
              padding: "8px 0",
              borderRadius: 8,
              cursor: "pointer",
              border: `1px solid ${name === active ? "#22d3ee" : "var(--line)"}`,
              color: name === active ? "#22d3ee" : "var(--text)",
              background: name === active ? "rgba(34,211,238,0.08)" : "transparent",
            }}
          >
            {label}
          </button>
        ))}
      </div>

      <div className="profile-grid">
        {Object.values(profiles).map((p) => (
          <div
            key={p.name}
            className={`profile-card ${p.name === active ? "active" : ""}`}
            role="button"
            tabIndex={0}
            aria-pressed={p.name === active}
            onClick={() => {
              setActive(p.name);
              setDraft(p);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                setActive(p.name);
                setDraft(p);
              }
            }}
          >
            <div className="n">{p.name}</div>
            <div className="v">{p.voice}</div>
          </div>
        ))}
      </div>

      <div className="sliders">
        <label htmlFor="vp-rate">
          <span>rate 语速</span>
          <span>{rate > 0 ? `+${rate}` : rate}%</span>
        </label>
        <input
          id="vp-rate"
          type="range" min={-50} max={50} value={rate}
          aria-label="语速 rate"
          onChange={(e) => setDraft({ ...draft, rate: `${Number(e.target.value) >= 0 ? "+" : ""}${e.target.value}%` })}
        />
        <label htmlFor="vp-pitch">
          <span>pitch 音调</span>
          <span>{pitch > 0 ? `+${pitch}` : pitch}Hz</span>
        </label>
        <input
          id="vp-pitch"
          type="range" min={-20} max={20} value={pitch}
          aria-label="音调 pitch"
          onChange={(e) => setDraft({ ...draft, pitch: `${Number(e.target.value) >= 0 ? "+" : ""}${e.target.value}Hz` })}
        />
        <label htmlFor="vp-volume">
          <span>volume 音量</span>
          <span>{volume > 0 ? `+${volume}` : volume}%</span>
        </label>
        <input
          id="vp-volume"
          type="range" min={-50} max={50} value={volume}
          aria-label="音量 volume"
          onChange={(e) => setDraft({ ...draft, volume: `${Number(e.target.value) >= 0 ? "+" : ""}${e.target.value}%` })}
        />
        <label htmlFor="vp-voice">
          <span>voice 音色</span>
        </label>
        <div style={{ display: "flex", gap: 8 }}>
          <select
            value={localeFilter}
            onChange={(e) => setLocaleFilter(e.target.value)}
            style={{ ...selectStyle, flex: 1 }}
            aria-label="locale filter"
          >
            {locales.map((l) => (
              <option key={l} value={l}>
                {l === "All" ? "All locales" : l}
              </option>
            ))}
          </select>
          <select
            value={genderFilter}
            onChange={(e) => setGenderFilter(e.target.value)}
            style={{ ...selectStyle, flex: 1 }}
            aria-label="gender filter"
          >
            {["All", "Female", "Male"].map((g) => (
              <option key={g} value={g}>
                {g === "All" ? "All genders" : g}
              </option>
            ))}
          </select>
        </div>
        <select
          id="vp-voice"
          value={draft.voice}
          onChange={(e) => setDraft({ ...draft, voice: e.target.value })}
          style={{ ...selectStyle, width: "100%", marginTop: 4 }}
        >
          {!filteredVoices.some((v) => v.ShortName === draft.voice) && (
            <option value={draft.voice}>{draft.voice} (custom)</option>
          )}
          {filteredVoices.map((v) => (
            <option key={v.ShortName} value={v.ShortName}>
              {v.ShortName} ({v.Gender})
            </option>
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
