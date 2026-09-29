import threading
import time

from suprbar.tray import TrayApp


class _SlowBridge:
    def __init__(self):
        self.done = threading.Event()

    def toggle(self):
        time.sleep(1.0)
        self.done.set()


def test_tray_click_never_blocks_the_message_loop():
    bridge = _SlowBridge()
    tray = TrayApp(bridge)
    t0 = time.monotonic()
    tray._on_default(None, None)
    assert time.monotonic() - t0 < 0.2
    assert bridge.done.wait(3)
