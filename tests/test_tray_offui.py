import sys
import threading
import time

import pytest

# pystray needs a display on Linux; the tray only ships on Windows.
pytestmark = pytest.mark.skipif(sys.platform != "win32",
                                reason="tray is Windows-only")


class _SlowBridge:
    def __init__(self):
        self.done = threading.Event()

    def toggle(self):
        time.sleep(1.0)
        self.done.set()


def test_tray_click_never_blocks_the_message_loop():
    from suprbar.tray import TrayApp
    bridge = _SlowBridge()
    tray = TrayApp(bridge)
    t0 = time.monotonic()
    tray._on_default(None, None)
    assert time.monotonic() - t0 < 0.2
    assert bridge.done.wait(3)
