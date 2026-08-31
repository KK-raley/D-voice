# Security Policy

## Supported versions

| Version | Supported |
| ------- | ----------|
| 0.1.x   | Yes - security fixes land in patch releases |
| 0.2.0rc1 | Candidate under evaluation; not production-certified |

## Voice authorization boundary (0.2.0rc1)

The safe microphone path verifies an enrolled speaker, verifies the aligned
wake phrase audio and pins subsequent turns to that speaker. These are
probabilistic checks, not liveness or anti-replay protection. Recordings and
cloned voices remain a known risk; voice alone must never authorize payments,
deletion or other high-impact actions. Real FAR/FRR and replay evaluation are
release gates, not claimed completed features.

Standby captures bounded local audio and may run local ASR; it does not dispatch
to the brain or expose ambient transcripts. It is not a hardware mute. Turn off
the microphone to stop all capture. Explicit text/vision/MCP calls are separate
trusted operator interfaces, not governed by the microphone wake gate. Keep
screen monitoring off when no background observations are wanted.

Bind HTTP to loopback. `VOCALIS_TOKEN` protects mutations and sensitive histories;
the HUD can supply it via its system panel. This is not a hardened multi-tenant
network service. Local Qwen blocks external endpoints/keys/proxies by default;
Edge-TTS and deliberately configured external agents have separate network use.
The Windows launcher instead selects local SAPI and workspace `.vocalis` state.

## Biometric data handling

Vocalis voiceprints (`~/.vocalis/profiles/*.voiceprofile.json`) are **biometric
personal data**. By design:

- they never leave the local machine;
- they are excluded from git via `.gitignore` (`*.voiceprofile.json`, `*.npy`, `*.wav`);
- no telemetry or crash dumps include them.

## Reporting a vulnerability

Please do **not** open a public issue for security problems.

1. Email: **security@vocalis.dev** (or use GitHub's private vulnerability reporting).
2. Include reproduction steps and affected versions.
3. You will receive an acknowledgement within **72 hours**.
4. Fixes ship as patch releases with CVE attribution (if desired).

## Scope

- Speaker-verification bypass attempts (replay, synthesis) - please report; documented
  hardening roadmap: liveness checks remain future work, not part of 0.2.0rc1.
- Dependency CVEs in the runtime install set.
- The HUD backend (FastAPI) when bound to non-loopback interfaces.

Out of scope: attacks requiring physical access to an unlocked machine.
