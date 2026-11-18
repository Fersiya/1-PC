import ctypes
from ctypes import wintypes
import sys
import threading
import time

if sys.platform == "win32":
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    wtsapi32 = ctypes.windll.wtsapi32

    LRESULT = ctypes.c_int64
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = LRESULT

    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", wintypes.BYTE * 8),
        ]

    GUID_CONSOLE_DISPLAY_STATE = GUID(
        0x6FE69556,
        0x704A,
        0x47A0,
        (wintypes.BYTE * 8)(0x8F, 0x24, 0xC2, 0x8D, 0x93, 0x6F, 0x08, 0x05),
    )

    class POWERBROADCAST_SETTING(ctypes.Structure):
        _fields_ = [
            ("PowerSetting", GUID),
            ("DataLength", wintypes.DWORD),
            ("Data", wintypes.BYTE * 1),
        ]

    user32.RegisterPowerSettingNotification.argtypes = [wintypes.HANDLE, ctypes.POINTER(GUID), wintypes.DWORD]
    user32.RegisterPowerSettingNotification.restype = wintypes.HANDLE
    user32.UnregisterPowerSettingNotification.argtypes = [wintypes.HANDLE]
    user32.UnregisterPowerSettingNotification.restype = wintypes.BOOL

    wtsapi32.WTSRegisterSessionNotification.argtypes = [wintypes.HWND, wintypes.DWORD]
    wtsapi32.WTSRegisterSessionNotification.restype = wintypes.BOOL
    wtsapi32.WTSUnRegisterSessionNotification.argtypes = [wintypes.HWND]
    wtsapi32.WTSUnRegisterSessionNotification.restype = wintypes.BOOL

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HICON),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
            ("hIconSm", wintypes.HICON),
        ]

    WM_POWERBROADCAST = 0x0218
    PBT_POWERSETTINGCHANGE = 0x8013
    PBT_APMSUSPEND = 0x0004
    PBT_APMRESUMEAUTOMATIC = 0x0012
    PBT_APMRESUMESUSPEND = 0x0007

    WM_WTSSESSION_CHANGE = 0x02B1
    WTS_SESSION_LOCK = 0x7
    WTS_SESSION_UNLOCK = 0x8
    WTS_SESSION_LOGOFF = 0x6
    WTS_SESSION_LOGON = 0x5

    WM_CLOSE = 0x0010


class WindowsPowerWatcher:
    """Отслеживает выключение экрана, блокировку Windows (Win+L) и переход в сон.

    Вызывает callback(is_active: bool) при смене состояния.
    """

    def __init__(self, callback):
        self.callback = callback
        self.thread: threading.Thread | None = None
        self.hwnd: int | None = None
        self.hnotify: int | None = None
        self.running = False
        self.is_active = True
        self._wnd_proc_ref = None

    def start(self):
        if sys.platform != "win32" or self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True, name="LumaPowerWatcher")
        self.thread.start()

    def stop(self):
        if not self.running:
            return
        self.running = False
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    def _trigger(self, active: bool, reason: str):
        if self.is_active != active:
            self.is_active = active
            try:
                self.callback(active, reason)
            except Exception:
                pass

    def _run_loop(self):
        hinst = kernel32.GetModuleHandleW(None)
        cls_name = f"LumaPowerWatcher_{int(time.time() * 1000)}"

        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_POWERBROADCAST:
                if wparam == PBT_POWERSETTINGCHANGE and lparam:
                    try:
                        pbs = ctypes.cast(lparam, ctypes.POINTER(POWERBROADCAST_SETTING)).contents
                        state = pbs.Data[0]
                        # state: 0 = Display off, 1 = Display on, 2 = Dimmed
                        if state == 0:
                            self._trigger(False, "Дисплей выключен")
                        elif state == 1:
                            self._trigger(True, "Дисплей включен")
                    except Exception:
                        pass
                elif wparam == PBT_APMSUSPEND:
                    self._trigger(False, "Компьютер уходит в сон")
                elif wparam in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
                    self._trigger(True, "Компьютер проснулся")

            elif msg == WM_WTSSESSION_CHANGE:
                if wparam in (WTS_SESSION_LOCK, WTS_SESSION_LOGOFF):
                    self._trigger(False, "Экран заблокирован (Win+L)")
                elif wparam in (WTS_SESSION_UNLOCK, WTS_SESSION_LOGON):
                    self._trigger(True, "Экран разблокирован")

            elif msg == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0

            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wnd_proc_ref = WNDPROC(wnd_proc)

        wnd_cls = WNDCLASSEXW()
        wnd_cls.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wnd_cls.lpfnWndProc = self._wnd_proc_ref
        wnd_cls.hInstance = hinst
        wnd_cls.lpszClassName = cls_name

        atom = user32.RegisterClassExW(ctypes.byref(wnd_cls))
        if not atom:
            return

        hwnd = user32.CreateWindowExW(0, cls_name, "LumaLinkPower", 0, 0, 0, 0, 0, None, None, hinst, None)
        if not hwnd:
            user32.UnregisterClassW(cls_name, hinst)
            return

        self.hwnd = hwnd
        self.hnotify = user32.RegisterPowerSettingNotification(hwnd, ctypes.byref(GUID_CONSOLE_DISPLAY_STATE), 0)
        wtsapi32.WTSRegisterSessionNotification(hwnd, 0)

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        # Очистка
        try:
            if self.hnotify:
                user32.UnregisterPowerSettingNotification(self.hnotify)
            wtsapi32.WTSUnRegisterSessionNotification(hwnd)
            user32.UnregisterClassW(cls_name, hinst)
        except Exception:
            pass
        self.hwnd = None
        self.hnotify = None
