import { useRef, useState } from "react";
import { api } from "../api";

interface Msg {
  who: "user" | "jarvis";
  text: string;
}

const SUGGESTIONS = [
  "现在状态如何？",
  "让 echo 模拟一个任务流程",
  "@echo 分析这个仓库并给出摘要",
];

export default function ChatPanel() {
  const [msgs, setMsgs] = useState<Msg[]>([
    { who: "jarvis", text: "JARVIS online. 所有子系统就绪，等待您的指令。" },
  ]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);

  const send = async (text: string) => {
    text = text.trim();
    if (!text || busy) return;
    setMsgs((m) => [...m, { who: "user", text }]);
    setInput("");
    setBusy(true);
    try {
      const resp = await api<{ kind: string; reply?: string; spoken?: string }>("/api/command", {
        method: "POST",
        body: JSON.stringify({ text, speak: true }),
      });
      const reply =
        resp.reply ||
        resp.spoken ||
        (resp.kind === "task" ? "任务已派发，进度见 HUD 与 Feed。" : "已处理。");
      setMsgs((m) => [...m, { who: "jarvis", text: reply }]);
    } catch (e) {
      setMsgs((m) => [...m, { who: "jarvis", text: `指令通道错误：${String(e)}` }]);
    } finally {
      setBusy(false);
      requestAnimationFrame(() => listRef.current?.scrollTo(0, 1e6));
    }
  };

  return (
    <div className="panel">
      <h2>JARVIS Console</h2>
      <div className="chat">
        <div className="msgs" ref={listRef}>
          {msgs.map((m, i) => (
            <div className={`msg ${m.who}`} key={i}>
              <div className="who">{m.who === "user" ? "YOU" : "JARVIS"}</div>
              {m.text}
            </div>
          ))}
        </div>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          {SUGGESTIONS.map((s) => (
            <button
              key={s}
              onClick={() => send(s)}
              style={{
                fontSize: 12,
                padding: "4px 10px",
                borderRadius: 999,
                border: "1px solid var(--line)",
                background: "transparent",
                color: "var(--dim)",
                cursor: "pointer",
              }}
            >
              {s}
            </button>
          ))}
        </div>
        <div className="composer">
          <input
            value={input}
            placeholder={busy ? "JARVIS 处理中…" : "说点什么，或下达指令（回车发送）"}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && send(input)}
            disabled={busy}
          />
          <button onClick={() => send(input)} disabled={busy || !input.trim()}>
            SEND
          </button>
        </div>
      </div>
    </div>
  );
}
