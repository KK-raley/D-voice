"""Step 2 - VoiceGate demo (verify a WAV file).

Demonstrates accepting the enrolled owner and rejecting an impostor.
Uses any WAV file(s) you provide:

    python examples/02_voice_gate_demo.py owner.wav impostor.wav
"""

import sys

from vocalis.voice.audio import load_wav
from vocalis.voice.gate import VoiceGate


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    gate = VoiceGate()
    print(f"Enrolled users: {list(gate.profiles)} (threshold={gate.config.voice_gate.threshold})")
    for path in sys.argv[1:]:
        audio, sr = load_wav(path)
        decision = gate.verify(audio)
        verdict = "ACCEPTED" if decision.accepted else "REJECTED"
        print(
            f"{path}: {verdict} user={decision.user} "
            f"sim={decision.similarity:.3f} (sr={sr})"
        )


if __name__ == "__main__":
    main()
