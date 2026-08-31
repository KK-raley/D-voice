"""D-VOICE 的视觉能力：看屏幕 + 独立监管。"""

from vocalis.vision.screen import ScreenObservation, observe_screen
from vocalis.vision.watcher import ScreenWatcher

__all__ = ["ScreenObservation", "ScreenWatcher", "observe_screen"]
