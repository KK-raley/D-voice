import AgentFeed from "./components/AgentFeed";
import BrainPanel from "./components/BrainPanel";
import ChatPanel from "./components/ChatPanel";
import HUD from "./components/HUD";
import VoiceProfilePanel from "./components/VoiceProfilePanel";
import {
  cancelTask,
  getStandby,
  resolveConfirmation,
  useAgents,
  useEventStream,
  type CommandReceipt,
  type ConfirmRequest,
  type ReceiptView,
  type StandbyInfo,
} from "./api";
import { useEffect, useState } from "react";

export default function App() {
  const { events, connected } = useEventStream();
  const agents = useAgents(events);
  const [token, setToken] = useState(() => sessionStorage.getItem("vocalis-token") || "");

  // P0-5: poll standby state for the HUD three-state banner.
  const [standby, setStandby] = useState<StandbyInfo | null>(null);
  useEffect(() => {
    const poll = () => getStandby().then(setStandby).catch(() => {});
    poll();
    const t = setInterval(poll, 5000);
    return () => clearInterval(t);
  }, []);

  // P0-6: keep only the latest command receipt.
  const [receipt, setReceipt] = useState<ReceiptView | null>(null);
  const cancelReceiptTasks = (taskIds: string[]) => {
    void Promise.allSettled(taskIds.map((id) => cancelTask(id))).then((results) => {
      if (results.some((r) => r.status === "fulfilled")) {
        setReceipt((prev) => (prev ? { ...prev, cancelled: true } : prev));
      }
    });
  };

  // P0-2: pending high-risk confirmation cards.
  const [confirmations, setConfirmations] = useState<ConfirmRequest[]>([]);
  const resolveConfirm = (id: string, approved: boolean) => {
    setConfirmations((prev) => prev.filter((c) => c.id !== id)); // remove the card immediately
    void resolveConfirmation(id, approved).catch(() => {});
  };

  // Process each new bus event exactly once (events[0] is always the newest frame).
  const [lastEventId, setLastEventId] = useState("");
  useEffect(() => {
    const ev = events[0];
    if (!ev || ev.id === lastEventId) return;
    setLastEventId(ev.id);
    if (ev.type === "command.receipt") {
      setReceipt({ ...(ev.data as unknown as CommandReceipt), ts: ev.ts, cancelled: false });
    } else if (ev.type === "confirm.requested") {
      const req = ev.data as unknown as ConfirmRequest;
      if (req?.id) {
        setConfirmations((prev) => (prev.some((c) => c.id === req.id) ? prev : [...prev, req]));
      }
    } else if (ev.type === "confirm.resolved") {
      const rid = String((ev.data as Record<string, unknown>).id ?? "");
      setConfirmations((prev) => prev.filter((c) => c.id !== rid));
    }
  }, [events, lastEventId]);

  // Drop expired confirmation cards every second (backend timestamps are epoch seconds).
  useEffect(() => {
    if (confirmations.length === 0) return;
    const t = setInterval(() => {
      const now = Date.now() / 1000;
      setConfirmations((prev) => {
        const next = prev.filter((c) => c.expires_at > now);
        return next.length === prev.length ? prev : next;
      });
    }, 1000);
    return () => clearInterval(t);
  }, [confirmations.length]);

  return (
    <div className="hud">
      <div className="brand">
        <img className="logo" src="/favicon.svg" alt="Vocalis" />
        <h1>VOCALIS</h1>
        <div className="conn">
          <span className={`dot ${connected ? "on" : "off"}`} />
          {connected ? "LINK ESTABLISHED" : "RECONNECTING"}
        </div>
      </div>

      {/* 主界面：语音对话区（唯一主动暴露的界面） */}
      <ChatPanel events={events} />

      {/* 次级面板：默认折叠，展开后可见 HUD / 大脑选择 / 语音设置 / 任务流 */}
      <details style={{ marginTop: 14 }}>
        <summary
          style={{
            cursor: "pointer",
            color: "var(--dim)",
            fontSize: 13,
            padding: "6px 2px",
            userSelect: "none",
          }}
        >
          ⚙ 系统面板（状态 / 大脑选择 / 语音设置 / 任务流）
        </summary>
        <div style={{ display: "grid", gap: 14, marginTop: 12 }}>
          <div className="panel">
            <label htmlFor="access-token">访问令牌（仅后端设置 VOCALIS_TOKEN 时需要）</label>
            <input id="access-token" type="password" autoComplete="off" value={token}
              onChange={(event) => setToken(event.target.value)} />
            <button onClick={() => { sessionStorage.setItem("vocalis-token", token); location.reload(); }}>
              保存到当前会话并重连
            </button>
          </div>
          <div style={{ display: "grid", gap: 14 }}>
            <HUD
              agents={agents}
              events={events}
              connected={connected}
              standby={standby}
              receipt={receipt}
              confirmations={confirmations}
              onResolveConfirm={resolveConfirm}
              onCancelTasks={cancelReceiptTasks}
            />
            <BrainPanel />
            <VoiceProfilePanel />
          </div>
          <AgentFeed events={events} />
        </div>
      </details>
    </div>
  );
}
