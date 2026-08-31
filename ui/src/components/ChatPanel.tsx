import { useEffect, useRef, useState } from "react";
import { api, apiHeaders, lookAtScreen, transcribeAudio, type BusEvent, type VoiceResult } from "../api";

interface Msg {
  who: "user" | "dvoice";
  text: string;
}

/**
 * 语音对话主界面：问答以文字展示，麦克风是唯一输入。
 * 文字输入端（输入框 / SEND / 建议 chips）按需求暂时隐藏——
 * 恢复时把 `hidden` 常量改回 false 即可。
 */
const SHOW_TEXT_INPUT = false;

/** 按句切分语音播报文本（合并过短段，减少合成请求数）。 */
function splitSentences(text: string): string[] {
  const parts = text.split(/(?<=[。！？!?；;\n])/).filter((s) => s.trim());
  const merged: string[] = [];
  for (const p of parts) {
    const last = merged[merged.length - 1];
    if (last !== undefined && (last + p).length <= 40) {
      merged[merged.length - 1] = last + p;
    } else {
      merged.push(p);
    }
  }
  return merged.length ? merged : [text];
}

export default function ChatPanel({ events = [] }: { events?: BusEvent[] }) {
  const [msgs, setMsgs] = useState<Msg[]>([
    { who: "dvoice", text: "先录入声纹，再开启持续待机。说「你好 D-VOICE」，等确认后再说指令。休眠时仅本地检测，不调用大模型。" },
  ]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [recording, setRecording] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);
  const mediaRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const startedAtRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout>>();
  const [standby, setStandby] = useState<VoiceResult | null>(null);
  const lastEventRef = useRef<string>();

  useEffect(() => {
    const refresh = () => api<VoiceResult>("/api/standby").then(setStandby).catch(() => {});
    void refresh();
    const timer = setInterval(refresh, 2000);
    return () => {
      clearInterval(timer);
      clearTimeout(timerRef.current);
      const recorder = mediaRef.current;
      if (recorder) {
        recorder.onstop = null;
        if (recorder.state !== "inactive") recorder.stop();
        recorder.stream.getTracks().forEach((track) => track.stop());
      }
    };
  }, []);

  useEffect(() => {
    const event = events.find((item) => item.type === "voice.session");
    if (!event || event.id === lastEventRef.current) return;
    lastEventRef.current = event.id;
    const result = event.data as unknown as VoiceResult;
    if (result.text) push("user", result.text);
    if (result.reply) push("dvoice", result.reply);
  }, [events]);

  const toggleStandby = async () => {
    setBusy(true);
    try {
      const result = await api<VoiceResult>("/api/standby/microphone", {
        method: "POST", body: JSON.stringify({ enabled: !standby?.microphone_running }),
      });
      setStandby(result);
      if (result.error) push("dvoice", result.error);
    } catch (error) {
      push("dvoice", `持续待机未开启：${String(error)}`);
    } finally { setBusy(false); }
  };

  const push = (who: Msg["who"], text: string) =>
    setMsgs((m) => [...m, { who, text }]);

  /** Play the reply aloud in the BROWSER (server-side winsound is unreliable
   *  for HUD use; browser playback also works when HUD runs on another PC).
   *  长回复按句子分段合成（并行请求、按序播放），第一句尽快出声；
   *  失败时明确提示，不再静默。 */
  const speakReply = async (text: string) => {
    const chunks = splitSentences(text).slice(0, 4);
    const fetchBlob = async (t: string): Promise<Blob | null> => {
      for (let attempt = 0; attempt < 2; attempt++) {
        try {
          const resp = await fetch("/api/speak", {
            method: "POST",
            headers: apiHeaders(),
            body: JSON.stringify({ text: t }),
          });
          if (resp.ok) return await resp.blob();
        } catch {
          /* retry below */
        }
        if (attempt === 0) await new Promise((r) => setTimeout(r, 600));
      }
      return null;
    };
    // 并行预取（回复被 SYSTEM_PROMPT 限制在 3 句内，≤4 段），按序播放
    const blobs = chunks.map((c) => fetchBlob(c));
    let anyOk = false;
    for (const p of blobs) {
      const blob = await p;
      if (!blob) continue;
      anyOk = true;
      try {
        const url = URL.createObjectURL(blob);
        const audio = new Audio(url);
        try {
          await new Promise<void>((resolve, reject) => {
            audio.onended = () => resolve();
            audio.onerror = () => reject(new Error("audio playback failed"));
            void audio.play().catch(reject);
          });
        } finally {
          URL.revokeObjectURL(url);
        }
      } catch {
        /* 浏览器自动播放策略拦截：文字回复仍在 */
      }
    }
    if (!anyOk) push("dvoice", "（语音合成暂时不可用，以上为文字回复。）");
  };

  const send = async (text: string) => {
    text = text.trim();
    if (!text || busy) return;
    push("user", text);
    setBusy(true);
    try {
      const resp = await api<{ kind: string; reply?: string; spoken?: string }>("/api/command", {
        method: "POST",
        body: JSON.stringify({ text, speak: false }),
      });
      const reply =
        resp.reply ||
        resp.spoken ||
        (resp.kind === "task" ? "任务已派发，进度见 HUD 与 Feed。" : "已处理。");
      push("dvoice", reply);
      void speakReply(reply);
    } catch (e) {
      push("dvoice", `指令通道错误：${String(e)}`);
    } finally {
      setBusy(false);
      requestAnimationFrame(() => listRef.current?.scrollTo(0, 1e6));
    }
  };

  const finishRecording = async () => {
    const blob = new Blob(chunksRef.current, {
      type: mediaRef.current?.mimeType || "audio/webm",
    });
    const elapsed = Date.now() - startedAtRef.current;
    if (blob.size < 2000 || elapsed < 400) {
      push("dvoice", "（太短了，没听清。点击麦克风后说一句话，再点一次结束。）");
      return;
    }
    setBusy(true);
    try {
      const result = await transcribeAudio(blob);
      setStandby((previous) => ({ ...previous, ...result }));
      if (result.text) push("user", result.text);
      const reply = result.reply || (result.state !== "active"
        ? "仍在待机：请由已录入声纹的本人说出唤醒词；未调用大模型。"
        : "未接受这段语音，请本人重新说一句完整指令。");
      push("dvoice", reply);
      if (result.reply) await speakReply(reply);
    } catch (e) {
      push("dvoice", `语音识别失败：${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
      requestAnimationFrame(() => listRef.current?.scrollTo(0, 1e6));
    }
  };

  const toggleMic = async () => {
    if (recording) {
      mediaRef.current?.stop(); // onstop handles the rest
      return;
    }
    if (busy) return;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
        ? "audio/webm;codecs=opus"
        : undefined;
      const rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
      chunksRef.current = [];
      rec.ondataavailable = (e) => {
        if (e.data.size) chunksRef.current.push(e.data);
      };
      rec.onstop = () => {
        clearTimeout(timerRef.current);
        stream.getTracks().forEach((t) => t.stop());
        setRecording(false);
        void finishRecording();
      };
      startedAtRef.current = Date.now();
      rec.start();
      mediaRef.current = rec;
      setRecording(true);
      timerRef.current = setTimeout(() => {
        if (rec.state === "recording") rec.stop();
      }, 14000);
    } catch (e) {
      push("dvoice", `无法访问麦克风：${e instanceof Error ? e.message : e}（浏览器需授权，且页面需通过 localhost 或 https 打开）`);
    }
  };

  /** D-VOICE 亲眼看一眼屏幕并回答（截屏 + 本地 OCR + 大脑）。 */
  const lookAtMyScreen = async () => {
    if (busy) return;
    push("user", "👁 看看我的屏幕，现在显示的是什么？");
    setBusy(true);
    try {
      const res = await lookAtScreen("请描述我屏幕上现在显示的内容，简要概括。");
      const reply =
        res.reply || (res.text ? `我看到了（${res.title || "未知窗口"}）：\n${res.text.slice(0, 300)}` : "我没看清屏幕内容。");
      push("dvoice", reply);
      void speakReply(reply);
    } catch (e) {
      push("dvoice", `视觉通道错误：${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
      requestAnimationFrame(() => listRef.current?.scrollTo(0, 1e6));
    }
  };

  return (
    <div className="panel">
      <h2>语音对话 Voice</h2>
      <div style={{ padding: "10px 0", fontSize: 13 }} aria-live="polite">
        {standby?.microphone_running ? "● 本机麦克风持续检测" : "○ 持续检测已关闭"}
        {" · "}{standby?.state === "active" ? "已唤醒" : "休眠 · 零大模型 token"}
        {standby?.error && <p role="alert">{standby.error}</p>}
        <div style={{ marginTop: 8 }}>
          <button className="chip" onClick={toggleStandby} disabled={busy || recording}>
            {standby?.microphone_running ? "关闭麦克风" : "开启持续待机"}
          </button>
          <span style={{ color: "var(--dim)", marginLeft: 8 }}>使用运行后端的电脑麦克风；关闭网页不会停止，需主动关闭。</span>
        </div>
      </div>
      <div className="chat">
        <div
          className="msgs"
          ref={listRef}
          role="log"
          aria-live="polite"
          aria-busy={busy || recording}
          style={{ minHeight: 260 }}
        >
          {msgs.map((m, i) => (
            <div className={`msg ${m.who}`} key={i}>
              <div className="who">{m.who === "user" ? "YOU" : "D-VOICE"}</div>
              {m.text}
            </div>
          ))}
          {recording && (
            <div className="msg user">
              <div className="who">YOU</div>
              ● 正在聆听…（再次点击麦克风结束）
            </div>
          )}
        </div>

        {/* Voice input: the only entry point */}
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            gap: 8,
            padding: "14px 0 6px",
          }}
        >
          <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
            <button
              onClick={lookAtMyScreen}
              disabled={busy || recording}
              aria-label="看看我的屏幕"
              title="D-VOICE 看一眼你的屏幕并描述"
              style={{
                width: 48,
                height: 48,
                borderRadius: "50%",
                border: "1px solid var(--line)",
                cursor: "pointer",
                fontSize: 20,
                color: "var(--text)",
                background: "transparent",
                opacity: busy || recording ? 0.5 : 1,
              }}
            >
              👁
            </button>
            <button
              onClick={toggleMic}
              disabled={busy || standby?.microphone_running}
              aria-label={recording ? "停止录音并发送" : "开始语音输入"}
              title={recording ? "停止录音并发送" : "点击说话，再点一次发送"}
              style={{
                width: 72,
                height: 72,
                borderRadius: "50%",
                border: recording ? "none" : "2px solid var(--cyan)",
                cursor: busy ? "wait" : "pointer",
                fontSize: 30,
                color: "#04121f",
                fontWeight: 700,
                background: recording
                  ? "radial-gradient(circle at 30% 30%, #ff8a8a, #ef4444)"
                  : "linear-gradient(135deg, #22d3ee, #818cf8)",
                boxShadow: recording ? "0 0 24px rgba(239,68,68,0.6)" : "0 0 14px rgba(34,211,238,0.35)",
                opacity: busy ? 0.5 : 1,
              }}
            >
              {recording ? "⏹" : "🎤"}
            </button>
            <div style={{ width: 48 }} aria-hidden="true" />
          </div>
          <div style={{ fontSize: 12, color: "var(--dim)" }}>
            {recording
              ? "正在聆听…点击结束并发送"
              : busy
                ? "D-VOICE 处理中…"
                : standby?.microphone_running ? "持续待机中：唤醒后说指令；说「休眠」回到待机" : "单次录音（最长14秒） · 点👁主动看屏幕"}
          </div>
        </div>

        {/* Text input intentionally hidden (voice-first). Flip to re-enable. */}
        {SHOW_TEXT_INPUT && (
          <>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 8 }}>
              <button className="chip" onClick={() => send("现在状态如何？")}>
                现在状态如何？
              </button>
              <button className="chip" onClick={() => send("让 echo 模拟一个任务流程")}>
                让 echo 模拟一个任务流程
              </button>
            </div>
            <div className="composer">
              <input
                value={input}
                placeholder={busy ? "D-VOICE 处理中…" : "说点什么，或下达指令（回车发送）"}
                aria-label="D-VOICE 指令输入"
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && send(input)}
                disabled={busy}
              />
              <button onClick={() => send(input)} disabled={busy || !input.trim()}>
                SEND
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
