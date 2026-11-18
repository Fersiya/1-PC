import ctypes
from ctypes import wintypes
import sys
import time

import mss

# ==============================================================================
# СВЕРХБЫСТРЫЙ ЗАХВАТ ЭКРАНА (WINDOWS GDI: 0.07 МС, 0% CPU, БЕЗ ДЕРГАНИЯ КУРСОРА)
# ==============================================================================
if sys.platform == "win32":
    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [
            ("bmiHeader", BITMAPINFOHEADER),
            ("bmiColors", wintypes.DWORD * 3),
        ]


def zones_from_bgra(raw: bytes, tw: int, th: int, n: int) -> list[tuple[int, int, int]]:
    """Средний цвет n вертикальных полос буфера BGRA (tw x th)."""
    out = []
    for i in range(n):
        x0 = i * tw // n
        x1 = max(x0 + 1, (i + 1) * tw // n)
        r = g = b = cnt = 0
        for y in range(th):
            row = y * tw * 4
            seg = raw[row + x0 * 4: row + x1 * 4]
            b += sum(seg[0::4]); g += sum(seg[1::4]); r += sum(seg[2::4])
            cnt += x1 - x0
        cnt = max(1, cnt)
        out.append((r // cnt, g // cnt, b // cnt))
    return out


class FastWindowsCapturer:
    """Сверхбыстрый захват экрана через Windows GDI CreateDIBSection (0.03 мс, 0% CPU).

    Ключевые оптимизации для мониторов с высокой частотой (144Hz, 180Hz, 240Hz):
    1. Экранный контекст (GetDC(0)) захватывается ИСКЛЮЧИТЕЛЬНО на мгновение
       вызова StretchBlt (~0.03 мс) и СРАЗУ ЖЕ освобождается через ReleaseDC(0).
       Это исключает переход Windows DWM в режим совместимости GDI — аппаратный
       DirectFlip и MPO остаются активными, а окна двигаются на 180 Гц идеально плавно.
    2. Флаг SRCCOPY (0x00CC0020) БЕЗ флага CAPTUREBLT — курсор мыши не затрагивается
       и не мерцает.
    3. CreateDIBSection даёт прямой указатель в память — без вызовов GetDIBits.
    """

    def __init__(self, tw: int = 64, th: int = 36):
        self.tw = tw
        self.th = th
        self.user32 = ctypes.windll.user32
        self.gdi32 = ctypes.windll.gdi32

        # Независимый DC в системной памяти (не блокирует дисплей)
        self.hdc_mem = self.gdi32.CreateCompatibleDC(0)

        self.bmi = BITMAPINFO()
        self.bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        self.bmi.bmiHeader.biWidth = tw
        self.bmi.bmiHeader.biHeight = -th  # top-down DIB
        self.bmi.bmiHeader.biPlanes = 1
        self.bmi.bmiHeader.biBitCount = 32
        self.bmi.bmiHeader.biCompression = 0

        self.ppv_bits = ctypes.c_void_p()
        self.hbm = self.gdi32.CreateDIBSection(
            self.hdc_mem, ctypes.byref(self.bmi), 0, ctypes.byref(self.ppv_bits), None, 0
        )
        self.old_bm = self.gdi32.SelectObject(self.hdc_mem, self.hbm)
        self.gdi32.SetStretchBltMode(self.hdc_mem, 3)  # COLORONCOLOR (без оверхеда)
        self.buf_size = tw * th * 4

    def capture(self, mode: str = "fullscreen") -> tuple[int, int, int]:
        w = self.user32.GetSystemMetrics(0)
        h = self.user32.GetSystemMetrics(1)
        if w <= 0 or h <= 0 or not self.ppv_bits.value:
            return 0, 0, 0

        # Получаем экранный DC, делаем мгновенный блит и СРАЗУ освобождаем DC!
        hdc_screen = self.user32.GetDC(0)
        if not hdc_screen:
            return 0, 0, 0
        try:
            self.gdi32.StretchBlt(
                self.hdc_mem, 0, 0, self.tw, self.th, hdc_screen, 0, 0, w, h, 0x00CC0020
            )
        finally:
            self.user32.ReleaseDC(0, hdc_screen)

        raw = ctypes.string_at(self.ppv_bits.value, self.buf_size)

        if mode == "edges":
            # Края экрана (Ambilight: верхняя кромка и боковые стороны)
            b_sum = g_sum = r_sum = 0
            # Верх: строки 0..5 (6 строк * 64 столбца = 384 точки)
            top_bytes = raw[: 6 * 64 * 4]
            b_sum += sum(top_bytes[0::4])
            g_sum += sum(top_bytes[1::4])
            r_sum += sum(top_bytes[2::4])
            count = 6 * 64

            # Бока: строки 6..35, слева 7 столбцов (0..6), справа 7 столбцов (57..63)
            for y in range(6, 36):
                row_off = y * 64 * 4
                l_bytes = raw[row_off : row_off + 7 * 4]
                b_sum += sum(l_bytes[0::4])
                g_sum += sum(l_bytes[1::4])
                r_sum += sum(l_bytes[2::4])

                r_bytes = raw[row_off + 57 * 4 : row_off + 64 * 4]
                b_sum += sum(r_bytes[0::4])
                g_sum += sum(r_bytes[1::4])
                r_sum += sum(r_bytes[2::4])
                count += 14

            count = max(1, count)
            return r_sum // count, g_sum // count, b_sum // count

        # Полный экран: усреднение всех 64x36 точек
        b_sum = sum(raw[0::4])
        g_sum = sum(raw[1::4])
        r_sum = sum(raw[2::4])
        cnt = self.tw * self.th
        return r_sum // cnt, g_sum // cnt, b_sum // cnt

    def capture_zones(self, n: int) -> list[tuple[int, int, int]]:
        """Делит экран на n вертикальных полос (слева направо), средний цвет каждой."""
        w = self.user32.GetSystemMetrics(0)
        h = self.user32.GetSystemMetrics(1)
        if w <= 0 or h <= 0 or not self.ppv_bits.value:
            return [(0, 0, 0)] * n
        hdc_screen = self.user32.GetDC(0)
        if not hdc_screen:
            return [(0, 0, 0)] * n
        try:
            self.gdi32.StretchBlt(
                self.hdc_mem, 0, 0, self.tw, self.th, hdc_screen, 0, 0, w, h, 0x00CC0020
            )
        finally:
            self.user32.ReleaseDC(0, hdc_screen)
        raw = ctypes.string_at(self.ppv_bits.value, self.buf_size)
        return zones_from_bgra(raw, self.tw, self.th, n)

    def close(self):
        try:
            if self.hdc_mem and self.old_bm:
                self.gdi32.SelectObject(self.hdc_mem, self.old_bm)
            if self.hbm:
                self.gdi32.DeleteObject(self.hbm)
            if self.hdc_mem:
                self.gdi32.DeleteDC(self.hdc_mem)
        except Exception:
            pass


def capture_screen_rgb_fallback(mode: str = "fullscreen") -> tuple[int, int, int]:
    """Резервный захват экрана через mss (если GDI недоступен)."""
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        img = sct.grab(monitor)
        raw = img.rgb
        w, h = img.width, img.height

        if mode == "edges":
            step = 16
            r_sum = g_sum = b_sum = count = 0
            top_h = max(1, h // 6)
            side_w = max(1, w // 7)

            for y in range(0, top_h, step):
                row_off = y * w * 3
                for x in range(0, w, step):
                    idx = row_off + x * 3
                    r_sum += raw[idx]
                    g_sum += raw[idx + 1]
                    b_sum += raw[idx + 2]
                    count += 1

            for y in range(top_h, h, step):
                row_off = y * w * 3
                for x in range(0, side_w, step):
                    idx = row_off + x * 3
                    r_sum += raw[idx]
                    g_sum += raw[idx + 1]
                    b_sum += raw[idx + 2]
                    count += 1
                for x in range(w - side_w, w, step):
                    idx = row_off + x * 3
                    r_sum += raw[idx]
                    g_sum += raw[idx + 1]
                    b_sum += raw[idx + 2]
                    count += 1

            count = max(1, count)
            return r_sum // count, g_sum // count, b_sum // count

        step = 32 * 3
        r_sum = sum(raw[0::step])
        g_sum = sum(raw[1::step])
        b_sum = sum(raw[2::step])
        count = max(1, len(raw[0::step]))
        return r_sum // count, g_sum // count, b_sum // count


_global_capturer: FastWindowsCapturer | None = None


def get_fast_capturer() -> FastWindowsCapturer | None:
    global _global_capturer
    if sys.platform == "win32" and _global_capturer is None:
        try:
            _global_capturer = FastWindowsCapturer()
        except Exception:
            _global_capturer = None
    return _global_capturer


def cleanup_fast_capturer() -> None:
    global _global_capturer
    if _global_capturer:
        try:
            _global_capturer.close()
        except Exception:
            pass
        _global_capturer = None


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


def is_window_moving() -> bool:
    """True, пока пользователь двигает или растягивает окно (GUI_INMOVESIZE)."""
    if sys.platform != "win32":
        return False
    try:
        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(info)
        if ctypes.windll.user32.GetGUIThreadInfo(0, ctypes.byref(info)):
            return bool(info.flags & 0x00000002)
    except Exception:
        pass
    return False


# GDI-чтение рабочего стола заставляет DWM делать синхронный снимок — на 144–180 Гц это
# даёт рывки окон. Поэтому: не снимаем во время перетаскивания окон и держим долю времени
# захвата не выше ~8% (интервал >= 12 длительностей предыдущего снимка).
_cap_state = {
    "rgb": (0, 0, 0), "t": 0.0, "ema": 0.0,
    "n": 0, "sum": 0.0, "max": 0.0, "skip_drag": 0, "skip_busy": 0,
}
_BUSY_FACTOR = 12.0


def _capture_screen_rgb_raw(mode: str) -> tuple[int, int, int]:
    cap = get_fast_capturer()
    if cap:
        try:
            return cap.capture(mode)
        except Exception:
            pass
    return capture_screen_rgb_fallback(mode)


def capture_screen_rgb(mode: str = "fullscreen") -> tuple[int, int, int]:
    st = _cap_state
    now = time.perf_counter()
    if is_window_moving():
        st["skip_drag"] += 1
        return st["rgb"]
    if st["ema"] > 0 and (now - st["t"]) < st["ema"] * _BUSY_FACTOR:
        st["skip_busy"] += 1
        return st["rgb"]
    rgb = _capture_screen_rgb_raw(mode)
    dt = time.perf_counter() - now
    st["rgb"] = rgb
    st["t"] = now
    st["ema"] = dt if st["ema"] == 0 else st["ema"] * 0.8 + dt * 0.2
    st["n"] += 1
    st["sum"] += dt
    st["max"] = max(st["max"], dt)
    return rgb


_zone_state = {"zones": None, "t": 0.0}


def capture_screen_zones(n: int, mode: str = "fullscreen") -> list[tuple[int, int, int]]:
    """Цвета n полос экрана; при перетаскивании окон/нагрузке отдаёт прошлые значения."""
    st = _zone_state
    now = time.perf_counter()
    if st["zones"] is not None and len(st["zones"]) == n:
        if is_window_moving() or (_cap_state["ema"] > 0 and (now - st["t"]) < _cap_state["ema"] * _BUSY_FACTOR):
            return st["zones"]
    cap = get_fast_capturer()
    zones = None
    if cap:
        try:
            zones = cap.capture_zones(n)
        except Exception:
            zones = None
    if zones is None:
        zones = [capture_screen_rgb_fallback(mode)] * n
    dt = time.perf_counter() - now
    _cap_state["ema"] = dt if _cap_state["ema"] == 0 else _cap_state["ema"] * 0.8 + dt * 0.2
    st["zones"], st["t"] = zones, now
    return zones


def pop_capture_stats() -> dict:
    """Статистика захвата с прошлого вызова (для диагностики в логе)."""
    st = _cap_state
    out = {
        "n": st["n"],
        "avg_ms": (st["sum"] / st["n"] * 1000.0) if st["n"] else 0.0,
        "max_ms": st["max"] * 1000.0,
        "skip_drag": st["skip_drag"],
        "skip_busy": st["skip_busy"],
    }
    st["n"] = 0; st["sum"] = 0.0; st["max"] = 0.0; st["skip_drag"] = 0; st["skip_busy"] = 0
    return out
