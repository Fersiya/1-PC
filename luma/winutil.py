import ctypes
from pathlib import Path
import sys

if sys.platform == "win32":
    import winreg


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


def set_autostart(enable: bool) -> bool:
    """Включение или отключение автозапуска LumaLink при входе в Windows."""
    if sys.platform != "win32":
        return False
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            if enable:
                if getattr(sys, "frozen", False):
                    cmd = f'"{sys.executable}" --minimized'
                else:
                    lumalink_py = Path(__file__).resolve().parent.parent / "lumalink.py"
                    cmd = f'"{sys.executable}" "{lumalink_py}" --minimized'
                winreg.SetValueEx(key, "LumaLink", 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, "LumaLink")
                except FileNotFoundError:
                    pass
        return True
    except Exception:
        return False


def is_autostart_enabled() -> bool:
    """Проверка, включён ли автозапуск LumaLink в реестре Windows."""
    if sys.platform != "win32":
        return False
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, "LumaLink")
            return True
    except Exception:
        return False
