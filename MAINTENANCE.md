# Maintenance Plan · D-VOICE (Vocalis)

> Living document — updated every iteration. Last reviewed: **2026-08-22** (v0.2 cycle, week 1)
>
> This file is the project's heartbeat. Every merge to `main` should either
> close an item here or add one. See [ROADMAP.md](ROADMAP.md) for feature
> vision; this file tracks *how we keep improving what already shipped*.

---

## 1. Maintenance principles

1. **Never go mute** — any degradation (no Ollama, no mic, no network TTS) must
   fall back visibly, never silently. Every fallback logs + emits `monitor.alert`.
2. **Offline-first tests** — `pytest -q` must pass with no network, no mic,
   no GPU. New features land with offline tests in the same PR.
3. **Biometrics are sacred** — voiceprints stay in `~/.vocalis/` (0600/0700),
   never committed, never logged, never uploaded.
4. **Small, reviewable iterations** — one theme per PR; prefer 3 similar lines
   over a premature abstraction.
5. **Docs age like code** — rename/refactor PRs update README, CHANGELOG and
   the relevant doc in the same commit (the JARVIS→D-VOICE rename set the bar).

---

## 2. Iteration tracks

We maintain the project along six standing tracks. Each track has an owner
area, a current status, and a queued improvement list. Check items off by
opening an issue referencing the track (e.g. `ui-polish`).

### Track A · UI aesthetics (`ui-polish`) — HUD / visual identity

Status: **v0.1 baseline shipped** — dark HUD, live waveform, agent feed,
Voice Studio, chat console.

| # | Improvement | Priority | ETA |
|---|---|---|---|
| A1 | Loading skeletons + empty-state illustrations (feed/agents/tasks panels) | med | v0.2 |
| A2 | Reduce motion / accessibility pass: focus rings, `prefers-reduced-motion`, ARIA labels on live regions | **high** | v0.2 |
| A3 | Light theme + theme persistence (localStorage) | low | v0.3 |
| A4 | Agent avatars & color identity per connector (echo/openai/claude-code) | med | v0.2 |
| A5 | Mobile/responsive HUD (grid → single column under 720px) | med | v0.3 |
| A6 | Animated task timeline view (reuse the stable task-id timeline data) | low | v0.3 |

### Track B · Voice output characteristics (`voice-features`) — TTS selectable features

Status: **v0.1 baseline shipped** — 3 built-in `VoiceProfile`s, live rate/pitch/volume
tuning in Voice Studio + CLI.

| # | Improvement | Priority | ETA |
|---|---|---|---|
| B1 | **Voice picker**: enumerate Edge-TTS voices by locale/gender in the HUD (dropdown, not hand-typed) | **high** | v0.2 |
| B2 | **Profile presets**: "focus / evening / presentation" one-click presets mapping to profile bundles | **high** | v0.2 |
| B3 | SSML breaks & emphasis control for narration cadence | med | v0.3 |
| B4 | Per-agent voices (claude-code answers in Orion, monitor alerts in Aria) | med | v0.2 |
| B5 | Emotional tone parameter once XTTS-v2 backend lands (roadmap v0.3) | low | v0.3 |
| B6 | Local offline TTS fallback (Piper) so `never go mute` holds without internet | **high** | v0.2 |

### Track C · Exposed hooks & extensibility (`hooks`) — the integration surface

Status: **v0.1 has the bus + connector base**; hooks are implicit. Goal: make
them a **documented, tested contract**.

| # | Improvement | Priority | ETA |
|---|---|---|---|
| C1 | **Document the event-hook surface**: every `EventType` with payload schema + `bus.on()` recipes → `docs/hooks.md` | **high** | v0.2 |
| C2 | Python entry-point plugins: `vocalis.agents` group so third parties register connectors without forking | **high** | v0.2 |
| C3 | Webhook hook: forward filtered events (e.g. `task.completed`) to a user URL | med | v0.3 |
| C4 | Pre/post TTS hooks (text normalization, e.g. unit expansion "2m41s" → "两分四十一秒") | med | v0.2 |
| C5 | Gate-hook: custom rejection policy callback (e.g. challenge unknown speaker with a question) | low | v0.3 |
| C6 | Version the event schema (`event.schema_version`) before third parties depend on it | med | v0.3 |

### Track D · Agent connectivity (`agents`) — ports & connectors

Status: **echo + openai (any OpenAI-compatible endpoint) + claude-code shipped.**

| # | Improvement | Priority | ETA |
|---|---|---|---|
| D1 | **Stdio/MCP connector** (Model Context Protocol) — the port most agent runtimes expose | **high** | v0.3 |
| D2 | Connector health page in HUD: last error, latency, auth state per agent | med | v0.2 |
| D3 | Retries + circuit breaker in `AgentConnector.run()` | med | v0.2 |
| D4 | Task cancellation (propagate `asyncio.CancelledError` to subprocesses) | **high** | v0.2 |

### Track E · Security & privacy (`security`)

| # | Improvement | Priority | ETA |
|---|---|---|---|
| E1 | Rate-limit `/api/command` + `/api/speak` (abuse & cost guard) | **high** | v0.2 |
| E2 | FAR/FRR evaluation harness for the gate threshold (record metrics, not vibes) | med | v0.3 |
| E3 | Encrypted voiceprint vault (age/libsecret) | med | v1.0 |
| E4 | Dependency audit automation (`pip-audit` in CI, weekly) | med | v0.2 |

### Track F · Performance & reliability (`perf`)

| # | Improvement | Priority | ETA |
|---|---|---|---|
| F1 | ASR model warm-start at server boot (not first request) | med | v0.2 |
| F2 | TTS cache eviction policy (LRU by bytes, configurable) | low | v0.3 |
| F3 | EventBus history persistence across restarts (recent events replay on HUD connect) | med | v0.2 |
| F4 | Structured performance counters (p95 dispatch→first-progress latency) | low | v0.3 |

---

## 3. Current cycle backlog · v0.2 (four weeks, started 2026-08-22)

The standing tracks above are the long game; this section is the committed
near-term plan. Update it every Monday: strike done items, re-scope slipped
ones. A week is done when its items are merged with tests + docs.

| Week | Theme | Items (from tracks) | Deliverable |
|---|---|---|---|
| **W1** | Voice you can see | B1 voice picker (locale/gender dropdown in HUD) · B2 one-click presets (focus/evening/presentation) | HUD screenshot → SHOWCASE refresh |
| **W2** | The hook contract | C1 `docs/hooks.md` (every EventType + payload schema + `bus.on()` recipes) · C2 entry-point plugins (`vocalis.agents` group) with a sample plugin repo | Third parties can integrate without forking |
| **W3** | Agent resilience | D2 connector health panel (last error/latency/auth) · D4 task cancellation propagating to subprocesses · E1 rate-limit `/api/command` + `/api/speak` | Failed agents degrade visibly, never hang |
| **W4** | Ship v0.2 | B6 Piper offline TTS fallback (stretch) · A2 accessibility pass (focus rings, reduced-motion) · F3 event history replay on HUD connect · record SHOWCASE media · version bump + tag + CHANGELOG | Release v0.2 with demo video on README top |

Slipped items roll to the next cycle's W1 — they are re-planned, not dropped.

---

## 4. Cadence & rituals

| Rhythm | Activity |
|---|---|
| **Every PR** | offline tests + ruff clean; CHANGELOG `[Unreleased]` entry; affected docs updated |
| **Weekly** | triage new issues (48h first response target); `pip-audit`; flaky-test sweep |
| **Bi-weekly** | pick top item from each **high-priority** track cell; close or re-scope |
| **Per release** | bump version, tag, move CHANGELOG `[Unreleased]` → versioned, refresh this file's "Last reviewed" stamp |
| **Quarterly** | re-read this file; archive done items; hold a "deprecation review" (old config keys, old event names) |

Definition of done for any item: code + offline test + docs + CHANGELOG entry.
No exceptions — that is what makes maintenance *continuous* instead of
*episodic*.

---

## 5. Handoff notes for future maintainers

- The event names are a public contract now (`dvoice.saying`, `task.progress`,
  ...). Renaming one is a breaking change: announce in CHANGELOG and keep a
  compatibility alias for one minor version.
- `python` on this dev machine may resolve to the WindowsApps alias; use the
  full interpreter path if subprocess spawning misbehaves.
- TOML cannot serialize `None` — `VocalisConfig.to_dict()` strips nulls;
  preserve that when adding config fields (see `tests/test_config.py::test_roundtrip`).
- UI build products are mounted by FastAPI at `/`; rebuild `ui/` before
  cutting release Docker images.
