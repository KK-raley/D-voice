import { useEffect, useState, type CSSProperties } from "react";
import {
  fetchBrain,
  fetchVisionState,
  setScreenWatch,
  updateBrain,
  type BrainInfo,
  type VisionState,
} from "../api";

/** One-click brain presets: [label, backend, model, base_url, api_key_env]. */
const PRESETS: Array<{
  label: string;
  backend: BrainInfo["backend"];
  model: string;
  base_url: string | null;
  api_key_env: string;
}> = [
  {
    label: "Qwen3 4B · 本机部署",
    backend: "local-qwen",
    model: "qwen3-4b-q4_k_m.gguf",
    base_url: "http://127.0.0.1:8080/v1",
    api_key_env: "DVOICE_API_KEY",
  },
  {
    label: "Qwen 0.5B 本地",
    backend: "ollama",
    model: "qwen2.5:0.5b-instruct",
    base_url: null,
    api_key_env: "DVOICE_API_KEY",
  },
  {
    label: "Qwen 1.5B 本地",
    backend: "ollama",
    model: "qwen2.5:1.5b-instruct",
    base_url: null,
    api_key_env: "DVOICE_API_KEY",
  },
  {
    label: "Qwen 3B 本地",
    backend: "ollama",
    model: "qwen2.5:3b-instruct",
    base_url: null,
    api_key_env: "DVOICE_API_KEY",
  },
];

const inputStyle: CSSProperties = {
  background: "#0a1224",
  color: "var(--text)",
  border: "1px solid var(--line)",
  borderRadius: 8,
  padding: "6px 8px",
  width: "100%",
  boxSizing: "border-box",
};

const labelStyle: CSSProperties = {
  fontSize: 12,
  color: "var(--dim)",
  margin: "8px 0 2px",
  display: "block",
};

export default function BrainPanel() {
  const [info, setInfo] = useState<BrainInfo | null>(null);
  const [backend, setBackend] = useState<BrainInfo["backend"]>("local-qwen");
  const [model, setModel] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKeyEnv, setApiKeyEnv] = useState("DVOICE_API_KEY");
  const [apiKey, setApiKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [watch, setWatch] = useState<VisionState | null>(null);

  useEffect(() => {
    fetchVisionState().then(setWatch).catch(() => {});
  }, []);

  const toggleWatch = async () => {
    if (!watch) return;
    try {
      const res = await setScreenWatch(!watch.running, 30);
      setWatch(res);
    } catch {
      /* 静默：监管开关失败不影响大脑配置 */
    }
  };

  useEffect(() => {
    fetchBrain()
      .then((b) => {
        setInfo(b);
        setBackend(b.backend);
        setModel(b.model);
        setBaseUrl(b.base_url ?? "");
        setApiKeyEnv(b.api_key_env);
      })
      .catch(() => setMsg("无法读取大脑配置（后端未启动？）"));
  }, []);

  const applyPreset = (p: (typeof PRESETS)[number]) => {
    setBackend(p.backend);
    setModel(p.model);
    setBaseUrl(p.base_url ?? "");
    setApiKeyEnv(p.api_key_env);
  };

  const save = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const res = await updateBrain({
        backend,
        model,
        base_url: backend !== "ollama" ? baseUrl : null,
        local_only: true,
        api_key_env: backend === "openai-compatible" ? apiKeyEnv : "DVOICE_API_KEY",
        api_key: backend === "openai-compatible" && apiKey.trim() ? apiKey.trim() : undefined,
        enabled: true,
      });
      setInfo(res);
      setApiKey(""); // 不回显密钥
      setMsg(
        res.available
          ? "✅ 已切换，大脑在线"
          : "⚠️ 已保存，本地模型尚未就绪。请启动本地 Qwen；当前仅能使用规则回复。"
      );
    } catch (e) {
      setMsg(`❌ 保存失败：${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  };

  const probe = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const b = await fetchBrain();
      setInfo(b);
      setMsg(b.available ? "🟢 本地服务连接正常（不代表已进行推理）" : "🔴 未连接：请先启动本地模型服务");
    } catch (e) {
      setMsg(`❌ 探测失败：${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <h2>Brain 大脑选择</h2>

      {/* Current status */}
      {info && (
        <div style={{ fontSize: 12, color: "var(--dim)", marginBottom: 10 }}>
          当前：{info.backend === "local-qwen" ? "🖥 本地 Qwen" : info.backend === "ollama" ? "🖥 Ollama" : "兼容服务"} ·{" "}
          <code>{info.model}</code>{" "}
          <span style={{ color: info.available ? "#4ade80" : "#f87171" }}>
            {info.available ? "● online" : "● offline"}
          </span>
        </div>
      )}

      {/* Backend toggle */}
      <div style={{ display: "flex", gap: 8, marginBottom: 10 }}>
        {(
          [
            ["local-qwen", "🖥 本机 Qwen"],
            ["ollama", "🖥 本地模型 (Ollama)"],
          ] as const
        ).map(([value, label]) => (
          <button
            key={value}
            onClick={() => setBackend(value)}
            style={{
              flex: 1,
              padding: "8px 0",
              borderRadius: 8,
              cursor: "pointer",
              border: `1px solid ${backend === value ? "#22d3ee" : "var(--line)"}`,
              color: backend === value ? "#22d3ee" : "var(--text)",
              background: backend === value ? "rgba(34,211,238,0.08)" : "transparent",
            }}
          >
            {label}
          </button>
        ))}
      </div>

      {/* Quick presets */}
      <div style={{ display: "flex", gap: 6, marginBottom: 4, flexWrap: "wrap" }}>
        {PRESETS.map((p) => (
          <button
            key={p.label}
            onClick={() => applyPreset(p)}
            style={{
              padding: "5px 10px",
              borderRadius: 999,
              cursor: "pointer",
              fontSize: 12,
              border: "1px solid var(--line)",
              color: "var(--text)",
              background: "transparent",
            }}
          >
            {p.label}
          </button>
        ))}
      </div>

      {/* Fields */}
      <label style={labelStyle} htmlFor="brain-model">
        model 模型名
      </label>
      <input
        id="brain-model"
        value={model}
        onChange={(e) => setModel(e.target.value)}
        placeholder={backend === "ollama" ? "qwen2.5:1.5b-instruct" : "qwen3-4b-q4_k_m.gguf"}
        style={inputStyle}
      />

      {backend !== "ollama" && (
        <>
          <label style={labelStyle} htmlFor="brain-base-url">
            本机服务地址（只允许回环地址）
          </label>
          <input
            id="brain-base-url"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder="http://127.0.0.1:8080/v1"
            style={inputStyle}
          />
          {backend === "openai-compatible" && <><label style={labelStyle} htmlFor="brain-key-env">
            api_key_env 环境变量名（这里填名字，不是密钥本身）
          </label>
          <input
            id="brain-key-env"
            value={apiKeyEnv}
            onChange={(e) => setApiKeyEnv(e.target.value)}
            placeholder="DEEPSEEK_API_KEY"
            style={inputStyle}
          />
          <label style={labelStyle} htmlFor="brain-key">
            API 密钥（sk-...，保存到本地受保护文件，不进配置、不回显）
          </label>
          <input
            id="brain-key"
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder="sk-...（留空 = 沿用已保存的密钥）"
            style={inputStyle}
            autoComplete="off"
          />
          </>}
        </>
      )}
      <p style={{ fontSize: 12, color: "var(--dim)" }}>
        本地部署：{info?.deployment_dir || "D:\\qwen-deployment"}。不需要云端密钥；模型未启动时不会自动切换云服务。
      </p>

      {/* Actions */}
      <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
        <button
          onClick={save}
          disabled={busy}
          style={{
            flex: 1,
            padding: "8px 0",
            borderRadius: 8,
            border: "1px solid var(--cyan)",
            color: "var(--cyan)",
            background: "transparent",
            cursor: "pointer",
          }}
        >
          应用并保存
        </button>
        <button
          onClick={probe}
          disabled={busy}
          style={{
            flex: 1,
            padding: "8px 0",
            borderRadius: 8,
            border: "none",
            color: "#04121f",
            fontWeight: 700,
            cursor: "pointer",
            background: "linear-gradient(90deg, #22d3ee, #818cf8)",
          }}
        >
          测试连接
        </button>
      </div>

      {msg && (
        <div style={{ fontSize: 12, marginTop: 8, color: "var(--dim)" }}>{msg}</div>
      )}

      {/* 独立屏幕监管（不依赖 agent 上报的第二监管来源） */}
      <div
        style={{
          marginTop: 14,
          paddingTop: 10,
          borderTop: "1px solid var(--line)",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 8,
        }}
      >
        <div style={{ fontSize: 12, color: "var(--dim)" }}>
          👁 屏幕监管（每30秒本地OCR观测，独立于agent上报）
          {watch?.running && watch.observations.length > 0 && (
            <div>最近观测：{watch.observations[watch.observations.length - 1].title || "（无标题窗口）"}</div>
          )}
        </div>
        <button
          onClick={toggleWatch}
          style={{
            padding: "5px 12px",
            borderRadius: 999,
            cursor: "pointer",
            fontSize: 12,
            border: `1px solid ${watch?.running ? "#22d3ee" : "var(--line)"}`,
            color: watch?.running ? "#22d3ee" : "var(--text)",
            background: watch?.running ? "rgba(34,211,238,0.08)" : "transparent",
            whiteSpace: "nowrap",
          }}
        >
          {watch?.running ? "监管中 · 点击停止" : "已关闭 · 点击开启"}
        </button>
      </div>
    </div>
  );
}
