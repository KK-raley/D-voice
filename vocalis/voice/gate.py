"""VoiceGate: your assistant only obeys *your* voice.

Enrollment stores per-user centroid speaker embeddings (d-vectors).
At runtime every captured utterance is embedded and matched against the
enrolled centroids with cosine similarity; below-threshold utterances are
rejected as "unknown speaker" before they ever reach an LLM or agent.

Voiceprints are personal biometric data. They are stored only under
``~/.vocalis/profiles/`` and never leave the machine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from vocalis.config import VocalisConfig, profiles_dir
from vocalis.voice.speaker import cosine_similarity, embed_utterance

PROFILE_EXT = ".voiceprofile.json"


@dataclass
class GateDecision:
    accepted: bool
    user: str | None
    similarity: float
    threshold: float
    runner_up: tuple[str | None, float] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "user": self.user,
            "similarity": round(self.similarity, 4),
            "threshold": self.threshold,
            "runner_up": (
                [self.runner_up[0], round(self.runner_up[1], 4)]
                if self.runner_up
                else None
            ),
        }


class VoiceGate:
    def __init__(self, config: VocalisConfig | None = None) -> None:
        self.config = config or VocalisConfig.load()
        self.profiles: dict[str, np.ndarray] = {}
        self.load_profiles()

    # ------------------------------------------------------------------
    # Profile storage
    # ------------------------------------------------------------------
    def load_profiles(self) -> None:
        self.profiles.clear()
        for path in sorted(profiles_dir().glob(f"*{PROFILE_EXT}")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.profiles[payload["user"]] = np.asarray(
                    payload["embedding"], dtype=np.float32
                )
            except Exception as e:  # corrupted profile: skip, never crash the gate
                print(f"[voicegate] skipping corrupt profile {path.name}: {e}")

    def profile_count(self) -> int:
        return len(self.profiles)

    # ------------------------------------------------------------------
    # Enrollment
    # ------------------------------------------------------------------
    def enroll(self, user: str, utterances: list[np.ndarray]) -> dict[str, Any]:
        """Create (or refresh) a voice profile from N calibration utterances."""
        if len(utterances) < self.config.voice_gate.min_enroll_utterances:
            raise ValueError(
                f"need >= {self.config.voice_gate.min_enroll_utterances} utterances, "
                f"got {len(utterances)}"
            )
        embeddings = np.stack([embed_utterance(u) for u in utterances])
        # Check intra-class consistency: all samples must come from one voice.
        centroid = embeddings.mean(axis=0)
        centroid /= np.linalg.norm(centroid) + 1e-9
        spread = float(np.mean([cosine_similarity(e, centroid) for e in embeddings]))
        if spread < 0.75:
            raise ValueError(
                f"enrollment samples are inconsistent (mean sim {spread:.2f} < 0.75); "
                "please re-record in a quiet environment"
            )

        self.profiles[user] = centroid.astype(np.float32)
        path: Path = profiles_dir() / f"{user}{PROFILE_EXT}"
        path.write_text(
            json.dumps(
                {"user": user, "embedding": centroid.tolist(), "spread": spread},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return {"user": user, "utterances": len(utterances), "consistency": round(spread, 4)}

    def delete(self, user: str) -> bool:
        path = profiles_dir() / f"{user}{PROFILE_EXT}"
        removed = self.profiles.pop(user, None) is not None
        if path.exists():
            path.unlink()
            return True
        return removed

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------
    def verify(self, audio: np.ndarray) -> GateDecision:
        """Embed one utterance and match it against all enrolled users."""
        if not self.profiles:
            raise RuntimeError(
                "no enrolled voices - run `vocalis enroll` first or see examples/01"
            )
        embedding = embed_utterance(audio)
        scores = {
            user: cosine_similarity(embedding, centroid)
            for user, centroid in self.profiles.items()
        }
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        best_user, best_score = ranked[0]
        threshold = self.config.voice_gate.threshold
        accepted = best_score >= threshold
        runner_up = ranked[1] if len(ranked) > 1 else None
        return GateDecision(
            accepted=accepted,
            user=best_user if accepted else None,
            similarity=best_score,
            threshold=threshold,
            runner_up=runner_up,
        )

    def verify_embedding(self, embedding: np.ndarray) -> GateDecision:
        """Verify against a pre-computed embedding (used by tests/ASR pipeline)."""
        if not self.profiles:
            raise RuntimeError("no enrolled voices")
        scores = {
            u: cosine_similarity(embedding, c) for u, c in self.profiles.items()
        }
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        best_user, best_score = ranked[0]
        threshold = self.config.voice_gate.threshold
        accepted = best_score >= threshold
        runner_up = ranked[1] if len(ranked) > 1 else None
        return GateDecision(
            accepted=accepted,
            user=best_user if accepted else None,
            similarity=best_score,
            threshold=threshold,
            runner_up=runner_up,
        )
