"""Step 1 - Enroll your voice.

Records 3 utterances and creates a biometric voice profile under
``~/.vocalis/profiles/``. After this, VoiceGate will only accept you.

Run:
    python examples/01_enroll_voice.py --user alice
"""

import argparse

from vocalis.voice.audio import record
from vocalis.voice.gate import VoiceGate


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", required=True, help="your profile name")
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()

    gate = VoiceGate()
    utterances = []
    for i in range(args.rounds):
        input(f"Take {i + 1}/{args.rounds}: press Enter and speak for ~3s...")
        utterances.append(record(seconds=3.5))
        print("  captured")

    result = gate.enroll(args.user, utterances)
    print(f"Enrolled {result['user']} (consistency {result['consistency']})")


if __name__ == "__main__":
    main()
