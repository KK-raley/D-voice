"""VoiceGate: your assistant only obeys *your* voice.

Enrollment stores per-user centroid speaker embeddings (speaker verification
d-vectors) computed by the configured backend (default: ERes2Net-large, the
open-source SOTA; Resemblyzer available as a light fallback). At runtime
every captured utterance is embedded and matched against the enrolled
centroids with cosine similarity; below-threshold utterances are rejected as
"unknown speaker" before they ever reach an LLM or agent.

Embeddings from different backends are *not* comparable (dimension and score
scale differ), so profiles are namespaced per backend:
``{user}.{backend}.voiceprofile.json`` (legacy ``{user}.voiceprofile.json``
files are treated as resemblyzer and re-enrollment is required after
switching backends).

Voiceprints are personal biometric data. They are stored only under
``~/.vocalis/profiles/`` with owner-only permissions (0600) and never
leave the machine.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from vocalis.config import VocalisConfig, profiles_dir
from vocalis.voice.speaker import (
    BACKEND_DEFAULTS,
    cosine_similarity,
    embed_utterance,
    resolve_backend,
)

logger = logging.getLogger("vocalis.voice.gate")

PROFILE_EXT = ".voiceprofile.json"
_VALID_USER = re.compile(r"[\w.-]{1,64}")
LEGACY_BACKEND = "resemblyzer"


def _safe_username(user: str) -> str:
    """Reject path traversal / weird names before they touch the filesystem."""
    if not _VALID_USER.fullmatch(user):
        raise ValueError(
            f"invalid username {user!r}: use 1-64 chars of letters/digits/._- only"
        )
    return user


def _profile_path(user: str, backend: str) -> Path:
    if backend == LEGACY_BACKEND:
        # legacy name for the original backend keeps old installs working
        return profiles_dir() / f"{user}{PROFILE_EXT}"
    return profiles_dir() / f"{user}.{backend}{PROFILE_EXT}"


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
        self.backend = resolve_backend(self.config.voice_gate.backend)
        if self.backend != (self.config.voice_gate.backend or "eres2net-large"):
            logger.warning(
                "voice_gate.backend %r unavailable - using %r instead; "
                "re-enroll if your profiles were made with another backend",
                self.config.voice_gate.backend,
                self.backend,
            )
        defaults = BACKEND_DEFAULTS[self.backend]
        self.threshold = (
            self.config.voice_gate.threshold
            if self.config.voice_gate.threshold is not None
            else defaults["threshold"]
        )
        self.enroll_consistency = defaults["enroll_consistency"]
        self.profiles: dict[str, np.ndarray] = {}
        self.load_profiles()

    # ------------------------------------------------------------------
    # Profile storage (namespaced per backend)
    # ------------------------------------------------------------------
    def load_profiles(self) -> None:
        self.profiles.clear()
        for path in sorted(profiles_dir().glob(f"*{PROFILE_EXT}")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if payload.get("backend", LEGACY_BACKEND) != self.backend:
                    continue  # belongs to another embedding backend
                self.profiles[payload["user"]] = np.asarray(
                    payload["embedding"], dtype=np.float32
                )
            except Exception as e:  # corrupted profile: skip, never crash the gate
                logger.warning("skipping corrupt profile %s: %s", path.name, e)

    def profile_count(self) -> int:
        return len(self.profiles)

    # ------------------------------------------------------------------
    # Enrollment
    # ------------------------------------------------------------------
    def enroll(
        self, user: str, utterances: list[np.ndarray], sample_rate: int = 16000
    ) -> dict[str, Any]:
        """Create (or refresh) a voice profile from N calibration utterances."""
        _safe_username(user)
        if len(utterances) < self.config.voice_gate.min_enroll_utterances:
            raise ValueError(
                f"need >= {self.config.voice_gate.min_enroll_utterances} utterances, "
                f"got {len(utterances)}"
            )
        embeddings = np.stack(
            [embed_utterance(u, sample_rate, backend=self.backend) for u in utterances]
        )
        # Check intra-class consistency: all samples must come from one voice.
        centroid = embeddings.mean(axis=0)
        centroid /= np.linalg.norm(centroid) + 1e-9
        spread = float(np.mean([cosine_similarity(e, centroid) for e in embeddings]))
        if spread < self.enroll_consistency:
            raise ValueError(
                f"enrollment samples are inconsistent (mean sim {spread:.2f} < "
                f"{self.enroll_consistency:.2f}); please re-record in a quiet environment"
            )

        self.profiles[user] = centroid.astype(np.float32)
        path = _profile_path(user, self.backend)
        path.write_text(
            json.dumps(
                {
                    "user": user,
                    "backend": self.backend,
                    "dim": int(centroid.shape[0]),
                    "embedding": centroid.tolist(),
                    "spread": spread,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.chmod(path, 0o600)  # biometric data: owner-only
        return {"user": user, "utterances": len(utterances), "consistency": round(spread, 4)}

    def delete(self, user: str) -> bool:
        _safe_username(user)
        removed = self.profiles.pop(user, None) is not None
        found = False
        for backend in ("resemblyzer", "eres2net-large"):
            path = _profile_path(user, backend)
            if path.exists():
                path.unlink()
                found = True
        return found or removed

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------
    def verify(self, audio: np.ndarray, sample_rate: int = 16000) -> GateDecision:
        """Embed one utterance and match it against all enrolled users.

        ``sample_rate`` must match the actual capture rate (44.1k/48k input
        is resampled internally); passing a wrong rate silently corrupts
        the embedding.
        """
        if not self.profiles:
            raise RuntimeError(
                "no enrolled voices - run `vocalis enroll` first or see examples/01"
            )
        embedding = embed_utterance(audio, sample_rate, backend=self.backend)
        return self.verify_embedding(embedding)

    def verify_embedding(self, embedding: np.ndarray) -> GateDecision:
        """Verify against a pre-computed embedding (used by tests/ASR pipeline)."""
        if not self.profiles:
            raise RuntimeError("no enrolled voices")
        scores = {
            u: cosine_similarity(embedding, c) for u, c in self.profiles.items()
        }
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        best_user, best_score = ranked[0]
        accepted = best_score >= self.threshold
        runner_up = ranked[1] if len(ranked) > 1 else None
        return GateDecision(
            accepted=accepted,
            user=best_user if accepted else None,
            similarity=best_score,
            threshold=self.threshold,
            runner_up=runner_up,
        )
