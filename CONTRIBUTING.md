# Contributing to Vocalis

Thanks for considering a contribution - Vocalis is built in the open, for everyone who has ever
wanted a **D-VOICE of their own**. This guide will get you merged fast.

## Getting started

```bash
git clone https://github.com/KK-raley/D-voice.git
cd vocalis

# Python backend (editable + all extras)
pip install -e ".[all,dev]"
pre-commit install

# HUD frontend
cd ui && npm install && npm run dev
```

### Recommended local model

```bash
ollama pull qwen2.5:3b-instruct   # D-VOICE brain
```

## Development workflow

1. **Fork & branch**: `feat/<short-name>` or `fix/<short-name>` from `develop`.
2. **Code** following the layout below.
3. **Test**: `pytest -q` must pass; add tests for new behavior.
4. **Lint**: `ruff check vocalis tests examples` and `mypy vocalis` (warnings only).
5. **Docs**: if you touch user-facing behavior, update `docs/` and `CHANGELOG.md` (Unreleased section).
6. **PR**: fill the template; keep PRs small and single-purpose.

## Project layout

```
vocalis/            Python package
  voice/            VoiceGate, ASR, TTS, audio IO
  agents/           Connector base + echo/claude-code/openai
  dvoice/           Brain (local LLM), monitor, commander
  notify/           Completion notifications
  server/           FastAPI + event bus
ui/                 React HUD (Vite + TS)
examples/           Numbered walkthrough scripts
tests/              pytest suite (offline-friendly)
```

## Adding an agent connector

1. Subclass `vocalis.agents.base.AgentConnector`.
2. Implement `stream_run()` yielding progress floats / step strings.
3. Register it in `build_default_registry()` (or your app code).
4. Add a test in `tests/test_agents.py` style.

## Privacy rules (hard requirements)

- **Never** commit voiceprints (`*.voiceprofile.json`), recordings, or API keys.
- Anything biometric stays under `~/.vocalis/` at runtime.
- New dependencies must be justified - Vocalis aims to stay lean.

## Releasing (maintainers)

1. Bump `version` in `pyproject.toml` + `vocalis/__init__.py`.
2. Move CHANGELOG "Unreleased" -> new version heading.
3. Tag `vX.Y.Z`; CI builds and the GitHub Release notes are generated.

## Questions?

Open a [discussion](https://github.com/KK-raley/D-voice/discussions) - we reply within 72 hours.
