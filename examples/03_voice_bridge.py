"""Step 3 - Speak agent output with a custom voice profile.

Shows the unified TTS layer: pick a profile, tune it, and have any text
(agent reply, notification, report) spoken aloud.

    python examples/03_voice_bridge.py
"""

import asyncio

from vocalis.voice.tts import TTSService, VoiceProfile


async def main() -> None:
    tts = TTSService()
    print("Available profiles:", list(tts.profiles()))

    await tts.speak(
        "任务已完成：三个文件的测试全部通过，耗时 42 秒。",
        profile_name="aria",
    )

    # Tune the profile live: slower, warmer, louder.
    profile = tts.get_profile("aria").apply_delta(rate=-15, pitch=+3, volume=+15)
    profile.name = "aria-evening"
    tts.upsert_profile(profile)
    await tts.speak("已切换到舒缓模式，晚安。", profile_name="aria-evening")

    # List what changed
    print("Tuned profile:", profile.to_dict())


if __name__ == "__main__":
    asyncio.run(main())
