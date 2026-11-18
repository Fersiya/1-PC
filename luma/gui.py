import ctypes
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from .config import APP_NAME, APP_SUBTITLE, get_local_ips, load_config, save_config
from .capture import cleanup_fast_capturer
from .server import BridgeServerController
from .winutil import is_autostart_enabled, set_autostart
from .power import WindowsPowerWatcher

try:
    import pystray
    HAS_PYSTRAY = True
except Exception:
    HAS_PYSTRAY = False


try:
    from PIL import Image, ImageDraw, ImageTk
    HAS_PIL = True
except Exception:
    HAS_PIL = False


def build_window_icon_image(size: int = 64):
    if not HAS_PIL:
        return None
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = 2
    draw.rounded_rectangle(
        [pad, pad, size - pad, size - pad],
        radius=size // 4,
        fill=(14, 18, 28, 255),
        outline=(42, 54, 82, 255),
        width=2,
    )
    cx, cy = size / 2.0, size / 2.0
    ring_r = size * 0.29
    ring_box = [cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r]
    steps = 60
    for s in range(steps):
        t = s / steps
        a0 = -135 + t * 360
        a1 = a0 + (360 / steps) + 2
        if t < 0.5:
            k = t * 2
            col = (int(124 * k), int(229 + (92 - 229) * k), 255, 255)
        else:
            k = (t - 0.5) * 2
            col = (int(124 + 112 * k), int(92 - 20 * k), int(255 - 102 * k), 255)
        draw.arc(ring_box, start=a0, end=a1, fill=col, width=max(3, size // 14))
    d = size * 0.13
    draw.polygon([(cx, cy - d * 1.2), (cx + d, cy), (cx, cy + d * 1.2), (cx - d, cy)], fill=(248, 250, 252, 255))
    return img


# ==============================================================================
# ГРАФИЧЕСКИЙ ИНТЕРФЕЙС WINDOWS (TKINTER + ПОДДЕРЖКА CTRL+C/V В RU РАСКЛАДКЕ)
# ==============================================================================
class LumaLinkApp:
    BG_MAIN = "#0B0E14"
    BG_CARD = "#141923"
    BG_INPUT = "#1C2331"
    BORDER = "#263042"
    ACCENT = "#7C5CFF"
    ACCENT_HOVER = "#6A48F5"
    CYAN = "#00E5FF"
    GREEN = "#10B981"
    RED = "#EF4444"
    TEXT_PRIMARY = "#F8FAFC"
    TEXT_MUTED = "#94A3B8"

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"{APP_NAME} — {APP_SUBTITLE}")
        self.root.geometry("560x830")
        self.root.minsize(520, 740)
        self.root.configure(bg=self.BG_MAIN)

        self._apply_window_icon()

        self.cfg = load_config()
        self.ui_queue: queue.Queue = queue.Queue()
        self.controller = BridgeServerController(self.ui_queue, self.cfg)
        self.ips = get_local_ips()
        self.primary_ip = self.ips[0] if self.ips else "192.168.1.X"

        self.var_tray = tk.BooleanVar(value=bool(self.cfg.get("minimize_to_tray", True)))
        self.var_autostart = tk.BooleanVar(value=bool(is_autostart_enabled()))
        self.var_auto_off = tk.BooleanVar(value=bool(self.cfg.get("auto_off_on_sleep", True)))
        self.tray_icon = None
        self._sliders: dict[str, tuple[ttk.Scale, tk.Label, str]] = {}
        self._preset_buttons: dict[str, tk.Button] = {}
        self._active_preset: str | None = None
        self.power_watcher = WindowsPowerWatcher(self.controller.on_power_state_changed)
        self.power_watcher.start()

        self._setup_clipboard_shortcuts()
        self._build_ui()
        self._poll_queue()

        if HAS_PYSTRAY:
            self._init_tray()

        if self._is_config_valid(silent=True):
            self.controller.start()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _init_tray(self):
        try:
            icon_img = build_window_icon_image(64)
            if icon_img is None:
                return

            def on_open(icon, item):
                self.root.after(0, self.restore_from_tray)

            def on_toggle_server(icon, item):
                self.root.after(0, self._toggle_server)

            def on_preset(k):
                self.root.after(0, lambda: self.apply_preset(k))

            def on_exit(icon, item):
                self.root.after(0, self.force_exit)

            menu = pystray.Menu(
                pystray.MenuItem("Открыть LumaLink", on_open, default=True),
                pystray.MenuItem("Сервер Вкл/Выкл", on_toggle_server),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("🎮 Пресет: Игры", lambda icon, item: on_preset("games")),
                pystray.MenuItem("🎬 Пресет: Кино", lambda icon, item: on_preset("cinema")),
                pystray.MenuItem("⚡ Пресет: Динамика", lambda icon, item: on_preset("dynamic")),
                pystray.MenuItem("📖 Пресет: Чтение", lambda icon, item: on_preset("reading")),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Выход", on_exit),
            )
            self.tray_icon = pystray.Icon(APP_NAME, icon_img, f"{APP_NAME} — {APP_SUBTITLE}", menu)
            threading.Thread(target=self.tray_icon.run, daemon=True).start()
        except Exception:
            self.tray_icon = None

    def restore_from_tray(self):
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()

    def minimize_to_tray(self):
        self.root.withdraw()

    def force_exit(self):
        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
            self.tray_icon = None
        self._on_close(force=True)

    def _apply_window_icon(self):
        try:
            pil_img = build_window_icon_image(64)
            if pil_img is not None:
                self._tk_icon = ImageTk.PhotoImage(pil_img)
                self.root.iconphoto(True, self._tk_icon)
        except Exception:
            pass

    def _setup_clipboard_shortcuts(self):
        # Фикс Ctrl+C / Ctrl+V / Ctrl+X / Ctrl+A для русской раскладки клавиатуры на Windows
        def on_ctrl_key(event):
            widget = self.root.focus_get()
            if not isinstance(widget, (tk.Entry, tk.Text)):
                return None

            kc = event.keycode
            if kc == 86:  # V -> Вставить
                if isinstance(widget, tk.Entry):
                    try:
                        clip = self.root.clipboard_get()
                        if widget.selection_present():
                            widget.delete("sel.first", "sel.last")
                        widget.insert("insert", clip)
                    except Exception:
                        pass
                    return "break"
            elif kc == 67:  # C -> Копировать
                try:
                    if isinstance(widget, tk.Entry) and widget.selection_present():
                        text = widget.selection_get()
                    elif isinstance(widget, tk.Text) and widget.tag_ranges("sel"):
                        text = widget.get("sel.first", "sel.last")
                    else:
                        return "break"
                    self.root.clipboard_clear()
                    self.root.clipboard_append(text)
                except Exception:
                    pass
                return "break"
            elif kc == 88:  # X -> Вырезать
                if isinstance(widget, tk.Entry) and widget.selection_present():
                    try:
                        text = widget.selection_get()
                        self.root.clipboard_clear()
                        self.root.clipboard_append(text)
                        widget.delete("sel.first", "sel.last")
                    except Exception:
                        pass
                    return "break"
            elif kc == 65:  # A -> Выделить всё
                if isinstance(widget, tk.Entry):
                    widget.select_range(0, "end")
                    widget.icursor("end")
                elif isinstance(widget, tk.Text):
                    widget.tag_add("sel", "1.0", "end")
                return "break"
            return None

        self.root.bind_all("<Control-KeyPress>", on_ctrl_key)

    def _attach_entry_context_menu(self, entry: tk.Entry):
        menu = tk.Menu(
            entry,
            tearoff=0,
            bg=self.BG_INPUT,
            fg=self.TEXT_PRIMARY,
            activebackground=self.ACCENT,
            activeforeground=self.TEXT_PRIMARY,
            bd=0,
        )

        def do_cut():
            if entry.selection_present():
                self.root.clipboard_clear()
                self.root.clipboard_append(entry.selection_get())
                entry.delete("sel.first", "sel.last")

        def do_copy():
            if entry.selection_present():
                self.root.clipboard_clear()
                self.root.clipboard_append(entry.selection_get())

        def do_paste():
            try:
                clip = self.root.clipboard_get()
                if entry.selection_present():
                    entry.delete("sel.first", "sel.last")
                entry.insert("insert", clip)
            except Exception:
                pass

        def do_select_all():
            entry.focus_set()
            entry.select_range(0, "end")
            entry.icursor("end")

        menu.add_command(label="Вырезать", command=do_cut)
        menu.add_command(label="Копировать", command=do_copy)
        menu.add_command(label="Вставить", command=do_paste)
        menu.add_separator()
        menu.add_command(label="Выделить всё", command=do_select_all)

        def show_menu(event):
            entry.focus_set()
            menu.tk_popup(event.x_root, event.y_root)

        entry.bind("<Button-3>", show_menu)

    def _attach_log_context_menu(self, text_widget: tk.Text):
        menu = tk.Menu(
            text_widget,
            tearoff=0,
            bg=self.BG_INPUT,
            fg=self.TEXT_PRIMARY,
            activebackground=self.ACCENT,
            activeforeground=self.TEXT_PRIMARY,
            bd=0,
        )

        def copy_sel():
            try:
                if text_widget.tag_ranges("sel"):
                    self.root.clipboard_clear()
                    self.root.clipboard_append(text_widget.get("sel.first", "sel.last"))
            except Exception:
                pass

        def copy_all():
            try:
                content = text_widget.get("1.0", "end").strip()
                self.root.clipboard_clear()
                self.root.clipboard_append(content)
            except Exception:
                pass

        menu.add_command(label="Копировать выделенное", command=copy_sel)
        menu.add_command(label="Копировать весь журнал", command=copy_all)

        def show_menu(event):
            text_widget.focus_set()
            menu.tk_popup(event.x_root, event.y_root)

        text_widget.bind("<Button-3>", show_menu)

    def _is_config_valid(self, silent: bool = False) -> bool:
        dev_id = self.var_dev_id.get().strip()
        l_key = self.var_local_key.get().strip()
        d_uuid = self.var_uuid.get().strip()

        if not dev_id or len(l_key) != 16 or len(d_uuid) != 16:
            if not silent:
                messagebox.showwarning(
                    "Проверьте ключи устройства",
                    "Заполните параметры лампы в блоке «Параметры лампы»:\n\n"
                    "• Device ID — не пустой\n"
                    "• Local Key — ровно 16 символов\n"
                    "• Device UUID — ровно 16 символов",
                )
            return False
        return True

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "Dark.Horizontal.TScale",
            background=self.BG_CARD,
            troughcolor=self.BG_INPUT,
            sliderlength=18,
        )

        container = tk.Frame(self.root, bg=self.BG_MAIN, padx=20, pady=16)
        container.pack(fill="both", expand=True)

        header = tk.Frame(container, bg=self.BG_MAIN)
        header.pack(fill="x", pady=(0, 12))

        title_col = tk.Frame(header, bg=self.BG_MAIN)
        title_col.pack(side="left")

        tk.Label(
            title_col,
            text=APP_NAME,
            font=("Segoe UI", 20, "bold"),
            fg=self.TEXT_PRIMARY,
            bg=self.BG_MAIN,
        ).pack(anchor="w")

        tk.Label(
            title_col,
            text=APP_SUBTITLE,
            font=("Segoe UI", 10),
            fg=self.TEXT_MUTED,
            bg=self.BG_MAIN,
        ).pack(anchor="w")

        self.server_badge = tk.Label(
            header,
            text="● Остановлен",
            font=("Segoe UI", 10, "bold"),
            fg=self.TEXT_MUTED,
            bg=self.BG_CARD,
            padx=12,
            pady=6,
        )
        self.server_badge.pack(side="right", anchor="center")

        hero_card = tk.Frame(
            container, bg=self.BG_CARD, highlightbackground=self.BORDER, highlightthickness=1, padx=16, pady=14
        )
        hero_card.pack(fill="x", pady=(0, 12))

        self.color_canvas = tk.Canvas(
            hero_card, width=78, height=78, bg=self.BG_CARD, highlightthickness=0
        )
        self.color_canvas.pack(side="left", padx=(0, 16))
        self.orb_outer = self.color_canvas.create_oval(
            4, 4, 74, 74, fill="#1E293B", outline="#334155", width=3
        )

        info_col = tk.Frame(hero_card, bg=self.BG_CARD)
        info_col.pack(side="left", fill="both", expand=True)

        self.lbl_client_status = tk.Label(
            info_col,
            text="Ожидание подключения телефона...",
            font=("Segoe UI", 11, "bold"),
            fg=self.TEXT_PRIMARY,
            bg=self.BG_CARD,
            anchor="w",
        )
        self.lbl_client_status.pack(fill="x")

        self.lbl_rgb_info = tk.Label(
            info_col,
            text="Цвет экрана: ожидание запуска",
            font=("Segoe UI", 9),
            fg=self.TEXT_MUTED,
            bg=self.BG_CARD,
            anchor="w",
        )
        self.lbl_rgb_info.pack(fill="x", pady=(2, 8))

        ip_row = tk.Frame(info_col, bg=self.BG_CARD)
        ip_row.pack(fill="x")

        tk.Label(
            ip_row,
            text=f"IP для телефона:  {self.primary_ip}",
            font=("Consolas", 10, "bold"),
            fg=self.CYAN,
            bg=self.BG_INPUT,
            padx=10,
            pady=4,
        ).pack(side="left")

        btn_copy = tk.Button(
            ip_row,
            text="Копировать",
            font=("Segoe UI", 9),
            fg=self.TEXT_PRIMARY,
            bg=self.BG_INPUT,
            activebackground=self.BORDER,
            activeforeground=self.TEXT_PRIMARY,
            bd=0,
            padx=10,
            pady=4,
            cursor="hand2",
            command=self._copy_ip,
        )
        btn_copy.pack(side="left", padx=(6, 0))

        ctrl_card = tk.Frame(
            container, bg=self.BG_CARD, highlightbackground=self.BORDER, highlightthickness=1, padx=16, pady=14
        )
        ctrl_card.pack(fill="x", pady=(0, 12))

        ctrl_hdr = tk.Frame(ctrl_card, bg=self.BG_CARD)
        ctrl_hdr.pack(fill="x", pady=(0, 8))

        tk.Label(
            ctrl_hdr,
            text="Настройки подсветки в реальном времени",
            font=("Segoe UI", 10, "bold"),
            fg=self.TEXT_PRIMARY,
            bg=self.BG_CARD,
        ).pack(side="left")

        presets_row = tk.Frame(ctrl_card, bg=self.BG_CARD)
        presets_row.pack(fill="x", pady=(0, 8))

        tk.Label(
            presets_row,
            text="Пресеты:",
            font=("Segoe UI", 9, "bold"),
            fg=self.TEXT_MUTED,
            bg=self.BG_CARD,
        ).pack(side="left", padx=(0, 8))

        preset_defs = [
            ("games", "🎮 Игры"),
            ("cinema", "🎬 Кино"),
            ("dynamic", "⚡ Динамика"),
            ("reading", "📖 Чтение"),
        ]
        for p_key, p_lbl in preset_defs:
            btn = tk.Button(
                presets_row,
                text=p_lbl,
                font=("Segoe UI", 8, "bold"),
                fg=self.TEXT_PRIMARY,
                bg=self.BG_INPUT,
                activebackground=self.BORDER,
                activeforeground=self.TEXT_PRIMARY,
                bd=0,
                padx=8,
                pady=3,
                cursor="hand2",
                command=lambda k=p_key: self.apply_preset(k),
            )
            btn.pack(side="left", padx=2)
            self._preset_buttons[p_key] = btn

        self.var_brightness = tk.IntVar(value=int(self.cfg.get("brightness", 100)))
        self.var_smoothing = tk.IntVar(value=int(self.cfg.get("smoothing", 45)))
        self.var_fps = tk.IntVar(value=int(self.cfg.get("fps", 14)))
        self.var_mode = tk.StringVar(value=self.cfg.get("capture_mode", "fullscreen"))
        self.var_segments = tk.IntVar(value=int(self.cfg.get("segments", 0)))

        self._add_slider(ctrl_card, "Яркость лампы", self.var_brightness, 10, 100, "%", name="brightness")
        self._add_slider(ctrl_card, "Плавность переходов", self.var_smoothing, 0, 85, "%", name="smoothing")
        self._add_slider(ctrl_card, "Частота обновления", self.var_fps, 5, 30, " FPS", name="fps")
        self._add_slider(ctrl_card, "Сегменты лампы (0 = один цвет, эксперимент)", self.var_segments, 0, 20, "", name="segments")

        mode_row = tk.Frame(ctrl_card, bg=self.BG_CARD)
        mode_row.pack(fill="x", pady=(6, 0))

        tk.Label(
            mode_row,
            text="Зона захвата:",
            font=("Segoe UI", 9),
            fg=self.TEXT_MUTED,
            bg=self.BG_CARD,
        ).pack(side="left")

        for val, label in [("fullscreen", "Весь экран"), ("edges", "Края экрана (Ambilight)")]:
            rb = tk.Radiobutton(
                mode_row,
                text=label,
                variable=self.var_mode,
                value=val,
                font=("Segoe UI", 9),
                fg=self.TEXT_PRIMARY,
                bg=self.BG_CARD,
                selectcolor=self.BG_INPUT,
                activebackground=self.BG_CARD,
                activeforeground=self.TEXT_PRIMARY,
                command=lambda: (self._set_active_preset_button(None), self._push_live_settings()),
            )
            rb.pack(side="left", padx=(12, 0))

        keys_card = tk.Frame(
            container, bg=self.BG_CARD, highlightbackground=self.BORDER, highlightthickness=1, padx=16, pady=14
        )
        keys_card.pack(fill="x", pady=(0, 12))

        keys_hdr = tk.Frame(keys_card, bg=self.BG_CARD)
        keys_hdr.pack(fill="x", pady=(0, 8))

        tk.Label(
            keys_hdr,
            text="Параметры лампы (сохраняются автоматически)",
            font=("Segoe UI", 10, "bold"),
            fg=self.TEXT_PRIMARY,
            bg=self.BG_CARD,
        ).pack(side="left")

        self.var_dev_id = tk.StringVar(value=self.cfg.get("device_id", ""))
        self.var_local_key = tk.StringVar(value=self.cfg.get("local_key", ""))
        self.var_uuid = tk.StringVar(value=self.cfg.get("device_uuid", ""))
        self.var_dp_switch = tk.StringVar(value=str(self.cfg.get("dp_switch", 1)))
        self.var_dp_mode = tk.StringVar(value=str(self.cfg.get("dp_mode", 2)))
        self.var_dp_colour = tk.StringVar(value=str(self.cfg.get("dp_colour", 24)))
        self.var_proto = tk.IntVar(value=int(self.cfg.get("protocol_version", 4)))

        self._add_field(keys_card, "Device ID:", self.var_dev_id)
        self._add_field(keys_card, "Local Key (16 симв.):", self.var_local_key)
        self._add_field(keys_card, "Device UUID (16 симв.):", self.var_uuid)

        dp_row = tk.Frame(keys_card, bg=self.BG_CARD)
        dp_row.pack(fill="x", pady=(4, 8))
        tk.Label(dp_row, text="DP Вкл:", font=("Segoe UI", 9), fg=self.TEXT_MUTED, bg=self.BG_CARD).pack(side="left")
        ent_sw = tk.Entry(
            dp_row, textvariable=self.var_dp_switch, width=3, font=("Consolas", 10),
            fg=self.TEXT_PRIMARY, bg=self.BG_INPUT, insertbackground=self.TEXT_PRIMARY, bd=0
        )
        ent_sw.pack(side="left", padx=(3, 6), ipady=3)
        self._attach_entry_context_menu(ent_sw)

        tk.Label(dp_row, text="DP Режим:", font=("Segoe UI", 9), fg=self.TEXT_MUTED, bg=self.BG_CARD).pack(side="left")
        ent_mode = tk.Entry(
            dp_row, textvariable=self.var_dp_mode, width=3, font=("Consolas", 10),
            fg=self.TEXT_PRIMARY, bg=self.BG_INPUT, insertbackground=self.TEXT_PRIMARY, bd=0
        )
        ent_mode.pack(side="left", padx=(3, 6), ipady=3)
        self._attach_entry_context_menu(ent_mode)

        tk.Label(dp_row, text="DP Цвет:", font=("Segoe UI", 9), fg=self.TEXT_MUTED, bg=self.BG_CARD).pack(side="left")
        ent_col = tk.Entry(
            dp_row, textvariable=self.var_dp_colour, width=3, font=("Consolas", 10),
            fg=self.TEXT_PRIMARY, bg=self.BG_INPUT, insertbackground=self.TEXT_PRIMARY, bd=0
        )
        ent_col.pack(side="left", padx=(3, 8), ipady=3)
        self._attach_entry_context_menu(ent_col)

        tk.Label(dp_row, text="Протокол:", font=("Segoe UI", 9), fg=self.TEXT_MUTED, bg=self.BG_CARD).pack(side="left")
        for p_val, p_lbl in [(4, "v4 (FD50)"), (3, "v3")]:
            tk.Radiobutton(
                dp_row,
                text=p_lbl,
                variable=self.var_proto,
                value=p_val,
                font=("Segoe UI", 9),
                fg=self.TEXT_PRIMARY,
                bg=self.BG_CARD,
                selectcolor=self.BG_INPUT,
                activebackground=self.BG_CARD,
                activeforeground=self.TEXT_PRIMARY,
                command=self._push_live_settings,
            ).pack(side="left", padx=(2, 0))

        btn_row = tk.Frame(keys_card, bg=self.BG_CARD)
        btn_row.pack(fill="x", pady=(4, 0))

        self.btn_toggle_server = tk.Button(
            btn_row,
            text="Запустить сервер",
            font=("Segoe UI", 10, "bold"),
            fg=self.TEXT_PRIMARY,
            bg=self.ACCENT,
            activebackground=self.ACCENT_HOVER,
            activeforeground=self.TEXT_PRIMARY,
            bd=0,
            padx=16,
            pady=8,
            cursor="hand2",
            command=self._toggle_server,
        )
        self.btn_toggle_server.pack(side="left")

        btn_save = tk.Button(
            btn_row,
            text="Сохранить параметры",
            font=("Segoe UI", 9),
            fg=self.TEXT_PRIMARY,
            bg=self.BG_INPUT,
            activebackground=self.BORDER,
            activeforeground=self.TEXT_PRIMARY,
            bd=0,
            padx=14,
            pady=8,
            cursor="hand2",
            command=self._save_only,
        )
        btn_save.pack(side="left", padx=(10, 0))

        opts_row = tk.Frame(keys_card, bg=self.BG_CARD)
        opts_row.pack(fill="x", pady=(8, 0))

        cb_tray = tk.Checkbutton(
            opts_row,
            text="В трей",
            variable=self.var_tray,
            font=("Segoe UI", 9),
            fg=self.TEXT_PRIMARY,
            bg=self.BG_CARD,
            selectcolor=self.BG_INPUT,
            activebackground=self.BG_CARD,
            activeforeground=self.TEXT_PRIMARY,
            command=self._push_live_settings,
        )
        cb_tray.pack(side="left")

        def on_toggle_auto():
            val = self.var_autostart.get()
            set_autostart(val)
            self._append_log("Автозапуск с Windows включён" if val else "Автозапуск с Windows отключён")
            self._push_live_settings()

        cb_auto = tk.Checkbutton(
            opts_row,
            text="Автозапуск",
            variable=self.var_autostart,
            font=("Segoe UI", 9),
            fg=self.TEXT_PRIMARY,
            bg=self.BG_CARD,
            selectcolor=self.BG_INPUT,
            activebackground=self.BG_CARD,
            activeforeground=self.TEXT_PRIMARY,
            command=on_toggle_auto,
        )
        cb_auto.pack(side="left", padx=(10, 0))

        cb_sleep = tk.Checkbutton(
            opts_row,
            text="Гасить при сне / Win+L",
            variable=self.var_auto_off,
            font=("Segoe UI", 9),
            fg=self.TEXT_PRIMARY,
            bg=self.BG_CARD,
            selectcolor=self.BG_INPUT,
            activebackground=self.BG_CARD,
            activeforeground=self.TEXT_PRIMARY,
            command=self._push_live_settings,
        )
        cb_sleep.pack(side="left", padx=(10, 0))

        # Карточка экспресс-теста лампы (для быстрой диагностики работы DPs)
        test_card = tk.Frame(container, bg=self.BG_CARD, highlightbackground=self.BORDER, highlightthickness=1, padx=10, pady=8)
        test_card.pack(fill="x", pady=(0, 10))

        row_top = tk.Frame(test_card, bg=self.BG_CARD)
        row_top.pack(fill="x", pady=(0, 6))

        tk.Label(row_top, text="Питание:", font=("Segoe UI", 8, "bold"), fg=self.TEXT_PRIMARY, bg=self.BG_CARD).pack(side="left", padx=(0, 4))
        tk.Button(
            row_top, text="Выкл", font=("Segoe UI", 8, "bold"), bg=self.RED, fg=self.TEXT_PRIMARY,
            bd=0, padx=6, pady=2, cursor="hand2", activebackground="#DC2626", activeforeground=self.TEXT_PRIMARY,
            command=lambda: self.controller.send_manual_dp(int(self.var_dp_switch.get() or 1), "bool", False)
        ).pack(side="left", padx=2)

        tk.Button(
            row_top, text="Вкл", font=("Segoe UI", 8, "bold"), bg=self.GREEN, fg="#0B0E14",
            bd=0, padx=6, pady=2, cursor="hand2", activebackground="#059669", activeforeground="#0B0E14",
            command=lambda: self.controller.send_manual_dp(int(self.var_dp_switch.get() or 1), "bool", True)
        ).pack(side="left", padx=(2, 8))

        tk.Label(row_top, text="Режим (DP2):", font=("Segoe UI", 8), fg=self.TEXT_MUTED, bg=self.BG_CARD).pack(side="left", padx=(0, 2))
        for m_val, m_title in [(0, "0 Белый"), (1, "1 Цвет"), (2, "2 Сцена"), (3, "3 Музыка")]:
            tk.Button(
                row_top, text=m_title, font=("Consolas", 8, "bold"), bg=self.BG_INPUT, fg=self.CYAN if m_val == 1 else self.TEXT_PRIMARY,
                bd=0, padx=4, pady=2, cursor="hand2", activebackground=self.BORDER, activeforeground=self.TEXT_PRIMARY,
                command=lambda v=m_val: self.controller.send_manual_dp(int(self.var_dp_mode.get() or 2), "mode", v)
            ).pack(side="left", padx=1)

        tk.Button(
            row_top, text="Ярк 100%", font=("Segoe UI", 8), bg=self.BG_INPUT, fg=self.TEXT_PRIMARY,
            bd=0, padx=5, pady=2, cursor="hand2", activebackground=self.BORDER, activeforeground=self.TEXT_PRIMARY,
            command=lambda: self.controller.send_manual_dp(47, "value", 1000)
        ).pack(side="right", padx=(4, 0))

        row_bot = tk.Frame(test_card, bg=self.BG_CARD)
        row_bot.pack(fill="x")

        tk.Label(row_bot, text="Цвет (DP24+61):", font=("Segoe UI", 8, "bold"), fg=self.TEXT_PRIMARY, bg=self.BG_CARD).pack(side="left", padx=(0, 4))
        colors_to_test = [
            ("Красный", (255, 0, 0), "#EF4444", "#FFFFFF"),
            ("Зелёный", (0, 255, 0), "#10B981", "#0B0E14"),
            ("Синий", (0, 100, 255), "#3B82F6", "#FFFFFF"),
            ("Белый", (255, 255, 255), "#E2E8F0", "#0B0E14")
        ]
        for c_lbl, c_rgb, c_bg, c_fg in colors_to_test:
            tk.Button(
                row_bot, text=c_lbl, font=("Segoe UI", 8), bg=c_bg, fg=c_fg,
                bd=0, padx=6, pady=2, cursor="hand2",
                command=lambda rgb=c_rgb: self.controller.send_manual_dp(int(self.var_dp_colour.get() or 24), "color", rgb)
            ).pack(side="left", padx=2)

        tk.Button(
            row_bot, text="▶ Экран", font=("Segoe UI", 8, "bold"), bg=self.ACCENT, fg=self.TEXT_PRIMARY,
            bd=0, padx=6, pady=2, cursor="hand2", activebackground=self.ACCENT_HOVER, activeforeground=self.TEXT_PRIMARY,
            command=lambda: self.controller.resume_ambilight()
        ).pack(side="right", padx=(2, 0))

        tk.Button(
            row_bot, text="⏸ Пауза", font=("Segoe UI", 8), bg=self.BG_INPUT, fg=self.TEXT_PRIMARY,
            bd=0, padx=6, pady=2, cursor="hand2", activebackground=self.BORDER, activeforeground=self.TEXT_PRIMARY,
            command=lambda: self.controller.pause_ambilight(120.0)
        ).pack(side="right", padx=(4, 2))

        # Строка ручной отправки любого произвольного DP (для тестирования Dreamlight DP 51, DP 25 и т.д.)
        row_custom = tk.Frame(test_card, bg=self.BG_CARD)
        row_custom.pack(fill="x", pady=(6, 0))

        tk.Label(row_custom, text="Тест DP:", font=("Segoe UI", 8, "bold"), fg=self.TEXT_PRIMARY, bg=self.BG_CARD).pack(side="left", padx=(0, 2))
        self.var_cust_id = tk.StringVar(value="61")
        entry_cid = tk.Entry(
            row_custom, textvariable=self.var_cust_id, font=("Consolas", 8), width=4,
            bg=self.BG_INPUT, fg=self.TEXT_PRIMARY, insertbackground=self.TEXT_PRIMARY, bd=0
        )
        entry_cid.pack(side="left", padx=(0, 4))
        self._attach_entry_context_menu(entry_cid)

        tk.Label(row_custom, text="Тип:", font=("Segoe UI", 8), fg=self.TEXT_MUTED, bg=self.BG_CARD).pack(side="left", padx=(0, 2))
        self.var_cust_type = tk.StringVar(value="raw_hex")
        type_cb = ttk.Combobox(
            row_custom, textvariable=self.var_cust_type, values=["raw_hex", "string", "value", "enum", "bool"],
            state="readonly", width=8, font=("Consolas", 8)
        )
        type_cb.pack(side="left", padx=(0, 4))

        tk.Label(row_custom, text="Данные:", font=("Segoe UI", 8), fg=self.TEXT_MUTED, bg=self.BG_CARD).pack(side="left", padx=(0, 2))
        self.var_cust_val = tk.StringVar(value="0001001400000003e803e8")
        entry_cval = tk.Entry(
            row_custom, textvariable=self.var_cust_val, font=("Consolas", 8),
            bg=self.BG_INPUT, fg=self.TEXT_PRIMARY, insertbackground=self.TEXT_PRIMARY, bd=0
        )
        entry_cval.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self._attach_entry_context_menu(entry_cval)

        def do_send_custom():
            try:
                cid = int(self.var_cust_id.get().strip())
                ctype = self.var_cust_type.get().strip()
                val_raw = self.var_cust_val.get().strip()
                if ctype == "raw_hex":
                    b = bytes.fromhex(val_raw)
                    self.controller.send_manual_dp(cid, "custom", {"type": 0x00, "bytes": b})
                elif ctype == "string":
                    b = val_raw.encode("utf-8")
                    self.controller.send_manual_dp(cid, "custom", {"type": 0x03, "bytes": b})
                elif ctype == "value":
                    self.controller.send_manual_dp(cid, "value", int(val_raw))
                elif ctype == "enum":
                    self.controller.send_manual_dp(cid, "enum", int(val_raw))
                elif ctype == "bool":
                    self.controller.send_manual_dp(cid, "bool", val_raw.lower() in ("1", "true", "yes"))
            except Exception as e:
                messagebox.showerror("Ошибка", f"Неверные данные DP: {e}")

        tk.Button(
            row_custom, text="Отправить DP", font=("Segoe UI", 8, "bold"), bg=self.ACCENT, fg=self.TEXT_PRIMARY,
            bd=0, padx=6, pady=1, cursor="hand2", activebackground=self.ACCENT_HOVER, activeforeground=self.TEXT_PRIMARY,
            command=do_send_custom
        ).pack(side="right")

        log_card = tk.Frame(
            container, bg=self.BG_CARD, highlightbackground=self.BORDER, highlightthickness=1, padx=12, pady=10
        )
        log_card.pack(fill="both", expand=True)

        self.log_text = tk.Text(
            log_card,
            height=6,
            font=("Consolas", 9),
            fg=self.TEXT_MUTED,
            bg=self.BG_CARD,
            bd=0,
            wrap="word",
            state="disabled",
        )
        self.log_text.pack(fill="both", expand=True)
        self._attach_log_context_menu(self.log_text)

    def _add_slider(self, parent, title: str, var: tk.IntVar, frm: int, to: int, suffix: str, name: str = ""):
        row = tk.Frame(parent, bg=self.BG_CARD)
        row.pack(fill="x", pady=3)

        lbl_val = tk.Label(
            row,
            text=f"{var.get()}{suffix}",
            font=("Consolas", 9, "bold"),
            fg=self.CYAN,
            bg=self.BG_CARD,
            width=7,
            anchor="e",
        )
        lbl_val.pack(side="right")

        tk.Label(
            row, text=title, font=("Segoe UI", 9), fg=self.TEXT_MUTED, bg=self.BG_CARD, width=22, anchor="w"
        ).pack(side="left")

        def on_slide(val):
            v = int(round(float(val)))
            var.set(v)
            lbl_val.config(text=f"{v}{suffix}")
            # Пользователь вручную подвинул ползунок — сбрасываем плашку активного пресета
            self._set_active_preset_button(None)
            # Если подсветка была приостановлена ручным тестом — возобновляем
            if self.controller.pause_ambilight_until > 0:
                self.controller.resume_ambilight()
            # Мгновенно передаём настройки в работающий цикл Ambilight без фризов диска
            self._push_live_settings(save_disk=False)

        def on_release(event):
            # Сохраняем в файл только при завершении перетаскивания ползунка
            self._push_live_settings(save_disk=True)

        scale = ttk.Scale(
            row, from_=frm, to=to, orient="horizontal", style="Dark.Horizontal.TScale", command=on_slide
        )
        scale.set(var.get())
        scale.bind("<ButtonRelease-1>", on_release)
        scale.pack(side="left", fill="x", expand=True, padx=8)

        if name:
            self._sliders[name] = (scale, lbl_val, suffix)

    def _update_slider_ui(self, name: str, val: int):
        if name in self._sliders:
            scale, lbl_val, suffix = self._sliders[name]
            scale.set(val)
            lbl_val.config(text=f"{val}{suffix}")

    def _set_active_preset_button(self, active_key: str | None):
        self._active_preset = active_key
        for key, btn in self._preset_buttons.items():
            if key == active_key:
                btn.config(bg=self.ACCENT, fg=self.TEXT_PRIMARY)
            else:
                btn.config(bg=self.BG_INPUT, fg=self.TEXT_PRIMARY)

    def apply_preset(self, preset_key: str):
        self._set_active_preset_button(preset_key)
        if preset_key == "games":
            self.var_fps.set(30)
            self._update_slider_ui("fps", 30)
            self.var_smoothing.set(10)
            self._update_slider_ui("smoothing", 10)
            self.var_mode.set("fullscreen")
            self.var_brightness.set(100)
            self._update_slider_ui("brightness", 100)
            self.controller.resume_ambilight()
            self._push_live_settings(save_disk=True)
            self._append_log("Применён пресет: 🎮 Игры (30 FPS, отклик 10%, весь экран, яркость 100%)")
        elif preset_key == "cinema":
            self.var_fps.set(20)
            self._update_slider_ui("fps", 20)
            self.var_smoothing.set(65)
            self._update_slider_ui("smoothing", 65)
            self.var_mode.set("edges")
            self.var_brightness.set(90)
            self._update_slider_ui("brightness", 90)
            self.controller.resume_ambilight()
            self._push_live_settings(save_disk=True)
            self._append_log("Применён пресет: 🎬 Кино (20 FPS, плавность 65%, края Ambilight, яркость 90%)")
        elif preset_key == "dynamic":
            self.var_fps.set(25)
            self._update_slider_ui("fps", 25)
            self.var_smoothing.set(30)
            self._update_slider_ui("smoothing", 30)
            self.var_mode.set("fullscreen")
            self.var_brightness.set(100)
            self._update_slider_ui("brightness", 100)
            self.controller.resume_ambilight()
            self._push_live_settings(save_disk=True)
            self._append_log("Применён пресет: ⚡ Динамика (25 FPS, плавность 30%, весь экран, яркость 100%)")
        elif preset_key == "reading":
            self.var_brightness.set(60)
            self._update_slider_ui("brightness", 60)
            # Приостанавливаем захват экрана на длительное время (24 часа)
            self.controller.pause_ambilight(86400.0)
            # Отправляем тёплый мягкий свет (2700K: RGB 255, 195, 130)
            warm_rgb = (255, 195, 130)
            self.controller.send_manual_dp(int(self.var_dp_colour.get() or 24), "color", warm_rgb)
            self.controller.send_manual_dp(47, "value", 600)
            hex_col = f"#{warm_rgb[0]:02x}{warm_rgb[1]:02x}{warm_rgb[2]:02x}"
            self.color_canvas.itemconfig(self.orb_outer, fill=hex_col, outline=hex_col)
            self.lbl_rgb_info.config(text="Режим: Чтение (тёплый свет 2700K)  •  Яркость: 60%")
            self._push_live_settings(save_disk=True)
            self._append_log("Применён пресет: 📖 Чтение (тёплый свет 2700K, яркость 60%, пауза экрана)")

    def _add_field(self, parent, label: str, var: tk.StringVar):
        row = tk.Frame(parent, bg=self.BG_CARD)
        row.pack(fill="x", pady=3)
        tk.Label(
            row, text=label, font=("Segoe UI", 9), fg=self.TEXT_MUTED, bg=self.BG_CARD, width=21, anchor="w"
        ).pack(side="left")
        entry = tk.Entry(
            row,
            textvariable=var,
            font=("Consolas", 10),
            fg=self.TEXT_PRIMARY,
            bg=self.BG_INPUT,
            insertbackground=self.TEXT_PRIMARY,
            bd=0,
        )
        entry.pack(side="left", fill="x", expand=True, ipady=4, padx=(4, 0))
        self._attach_entry_context_menu(entry)

    def _collect_cfg(self) -> dict:
        self.cfg.update(
            {
                "device_id": self.var_dev_id.get().strip(),
                "local_key": self.var_local_key.get().strip(),
                "device_uuid": self.var_uuid.get().strip(),
                "dp_switch": int(self.var_dp_switch.get() or 1),
                "dp_mode": int(self.var_dp_mode.get() or 2),
                "dp_colour": int(self.var_dp_colour.get() or 24),
                "protocol_version": int(self.var_proto.get() or 4),
                "brightness": self.var_brightness.get(),
                "smoothing": self.var_smoothing.get(),
                "fps": self.var_fps.get(),
                "capture_mode": self.var_mode.get(),
                "segments": self.var_segments.get(),
                "minimize_to_tray": self.var_tray.get(),
                "autostart": self.var_autostart.get(),
                "auto_off_on_sleep": self.var_auto_off.get(),
            }
        )
        return self.cfg

    def _push_live_settings(self, save_disk: bool = True):
        try:
            cfg = self._collect_cfg()
            self.controller.update_live_settings(cfg)
            if save_disk:
                save_config(cfg)
        except Exception:
            pass

    def _save_only(self):
        if not self._is_config_valid(silent=False):
            return
        cfg = self._collect_cfg()
        save_config(cfg)
        self.controller.update_live_settings(cfg)
        self._append_log("Настройки сохранены.")

    def _toggle_server(self):
        if self.controller.running:
            self.controller.stop()
        else:
            if not self._is_config_valid(silent=False):
                return
            cfg = self._collect_cfg()
            save_config(cfg)
            self.controller.update_live_settings(cfg)
            self.controller.start()

    def _copy_ip(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.primary_ip)
        self._append_log(f"IP {self.primary_ip} скопирован в буфер обмена")

    def _append_log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self.log_text.config(state="normal")
        self.log_text.insert("end", f"[{ts}] {msg}\n")
        # держим в окне последние 400 строк: безлимитный Text с каждой строкой тормозит интерфейс
        if int(self.log_text.index("end-1c").split(".")[0]) > 400:
            self.log_text.delete("1.0", "end-400l")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _poll_queue(self):
        for _ in range(60):  # не более 60 событий за тик, чтобы окно не зависало
            try:
                ev = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            etype = ev.get("type")
            if etype == "log":
                self._append_log(ev["text"])
            elif etype == "server_state":
                if ev["active"]:
                    self.server_badge.config(text="● Сервер активен", fg=self.GREEN)
                    self.btn_toggle_server.config(text="Остановить сервер", bg=self.RED, activebackground="#DC2626")
                else:
                    self.server_badge.config(text="● Остановлен", fg=self.TEXT_MUTED)
                    self.btn_toggle_server.config(text="Запустить сервер", bg=self.ACCENT, activebackground=self.ACCENT_HOVER)
            elif etype == "client_state":
                color = self.GREEN if ev["connected"] else self.TEXT_PRIMARY
                self.lbl_client_status.config(text=ev["text"], fg=color)
            elif etype == "color":
                if self.root.state() != "iconic":
                    r, g, b = ev["rgb"]
                    raw_r, raw_g, raw_b = ev.get("raw_rgb", (r, g, b))
                    br = ev.get("brightness", 100)
                    hex_col = f"#{r:02x}{g:02x}{b:02x}"
                    self.color_canvas.itemconfig(self.orb_outer, fill=hex_col, outline=hex_col)
                    self.lbl_rgb_info.config(
                        text=f"Экран: RGB({raw_r}, {raw_g}, {raw_b})  •  Яркость: {br}%"
                    )

        self.root.after(50, self._poll_queue)

    def _on_close(self, force: bool = False):
        if not force and self.var_tray.get() and HAS_PYSTRAY and self.tray_icon:
            self.minimize_to_tray()
            return
        self._push_live_settings(save_disk=True)
        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
            self.tray_icon = None
        if hasattr(self, "power_watcher") and self.power_watcher:
            try:
                self.power_watcher.stop()
            except Exception:
                pass
        self.controller.stop()
        cleanup_fast_capturer()
        if sys.platform == "win32":
            try:
                ctypes.windll.winmm.timeEndPeriod(1)
            except Exception:
                pass
        self.root.destroy()
