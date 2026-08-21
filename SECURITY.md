# Security Policy

## Supported versions

| Version | Supported |
| ------- | ----------|
| 0.1.x   | Yes - security fixes land in patch releases |

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
  hardening roadmap: liveness checks in v0.2.
- Dependency CVEs in the runtime install set.
- The HUD backend (FastAPI) when bound to non-loopback interfaces.

Out of scope: attacks requiring physical access to an unlocked machine.
