import ctypes
import sys


def enable_windows_tweaks() -> None:
    """Чёткие шрифты на High-DPI экранах Windows и системный таймер 1 мс."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass
