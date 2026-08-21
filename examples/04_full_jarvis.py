"""Step 4 - The full JARVIS loop.

Voice-gated input -> command parsing -> agent dispatch -> live monitoring
-> spoken narration -> completion notification. Runs headless (no mic
needed) by using a text instruction; wire `vocalis run --verify` for the
full microphone experience.

    python examples/04_full_jarvis.py
"""

import asyncio

from vocalis.agents.registry import build_default_registry
from vocalis.config import VocalisConfig
from vocalis.jarvis.assistant import JarvisBrain
from vocalis.jarvis.commander import Commander
from vocalis.jarvis.monitor import TaskMonitor
from vocalis.notify.notifier import Notifier
from vocalis.server.events import EventType
from vocalis.voice.tts import TTSService


async def main() -> None:
    config = VocalisConfig.load()
    registry = build_default_registry()
    tts = TTSService(config)
    brain = JarvisBrain(config, registry)
    notifier = Notifier(tts)

    async def narrate(text: str) -> None:
        print(f"[JARVIS] {text}")
        await tts.speak(text, play=False)  # keep the demo quiet; set play=True to hear

    monitor = TaskMonitor(
        config,
        on_narration=narrate,
        on_completion=notifier.notify_task,
    )
    await monitor.start()

    commander = Commander(registry, brain)

    # 1) Ask a question (local model or rule fallback)
    answer = await brain.chat("你现在能做什么？")
    print("[JARVIS]", answer)

    # 2) Dispatch a task and watch live narration
    result = await commander.execute("让 echo 演示一个带进度汇报的任务")
    print("[result]", result["kind"])

    # 3) Status query
    report = await commander.execute("当前状态")
    print("[status]", report["reply"])

    await monitor.stop()


if __name__ == "__main__":
    asyncio.run(main())
