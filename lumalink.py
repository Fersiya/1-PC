"""LumaLink — точка входа.

Код разбит на модули пакета luma/:
  config.py    настройки, порты, путь к config.json
  protocol.py  шифрование и кадры Tuya BLE (v2/v3/v4)
  capture.py   захват цвета экрана (Windows GDI, запасной mss)
  server.py    WebSocket-сервер для телефона и цикл ambilight
  gui.py       окно на Tkinter
  winutil.py   настройки Windows (High-DPI, таймер 1 мс)
"""
import tkinter as tk

from luma.winutil import enable_windows_tweaks

enable_windows_tweaks()  # до создания окна: High-DPI и таймер 1 мс

from luma.gui import LumaLinkApp  # noqa: E402


def main():
    root = tk.Tk()
    LumaLinkApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
