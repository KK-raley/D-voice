import AgentFeed from "./components/AgentFeed";
import ChatPanel from "./components/ChatPanel";
import HUD from "./components/HUD";
import VoiceProfilePanel from "./components/VoiceProfilePanel";
import { useAgents, useEventStream } from "./api";

export default function App() {
  const { events, connected } = useEventStream();
  const agents = useAgents(events);

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

      <div style={{ display: "grid", gap: 14 }}>
        <HUD agents={agents} events={events} connected={connected} />
        <VoiceProfilePanel />
      </div>

      <div style={{ display: "grid", gap: 14, gridTemplateRows: "auto 1fr" }}>
        <ChatPanel />
        <AgentFeed events={events} />
      </div>
    </div>
  );
}
