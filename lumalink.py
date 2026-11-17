import asyncio
import colorsys
import ctypes
import hashlib
import json
import os
from pathlib import Path
import queue
import socket
import struct
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from aiohttp import web, WSMsgType
from Crypto.Cipher import AES
import mss

# Включаем четкое масштабирование шрифтов на Windows High-DPI экранах
if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

APP_NAME = "LumaLink"
APP_SUBTITLE = "Синхронизация подсветки экрана"
WS_PORT = 8000
DISCOVERY_PORT = 8001

DEFAULT_CONFIG = {
    "device_id": "",
    "local_key": "",
    "device_uuid": "",
    "port": WS_PORT,
    "protocol_version": 4,
    "dp_switch": 20,
    "dp_colour": 24,
    "fps": 14,
    "brightness": 100,
    "smoothing": 45,
    "capture_mode": "fullscreen",
}


def get_config_path() -> Path:
    exe_dir = (
        Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent
    )
    local_cfg = exe_dir / "config.json"
    if local_cfg.exists():
        return local_cfg

    appdata = os.environ.get("APPDATA")
    base_dir = Path(appdata) / APP_NAME if appdata else Path.home() / f".{APP_NAME.lower()}"
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir / "config.json"


CONFIG_PATH = get_config_path()


def load_config() -> dict:
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg.update(data)
        except Exception:
            pass
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def get_local_ips() -> list[str]:
    ips = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                ips.add(ip)
    except Exception:
        pass
    return sorted(ips)


# ==============================================================================
# ПРОТОКОЛ И ШИФРОВАНИЕ КАНАЛА СВЯЗИ С ЛАМПОЙ (TUYA BLE V4 / V3)
# ==============================================================================
def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def pack_varint(val: int) -> bytes:
    out = bytearray()
    while True:
        b = val & 0x7F
        val >>= 7
        if val:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def unpack_varint(buf: bytes, offset: int = 0) -> tuple[int, int]:
    val = 0
    shift = 0
    while offset < len(buf):
        b = buf[offset]
        offset += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return val, offset


class LightBridgeSession:
    def __init__(self, cfg: dict):
        self.device_id = cfg["device_id"].strip().encode("utf-8")[:16]
        self.local_key = cfg["local_key"].strip().encode("utf-8")
        self.uuid = cfg["device_uuid"].strip().encode("utf-8")[:16]
        self.protocol_version = int(cfg.get("protocol_version", 4))

        self.login_key = hashlib.md5(self.local_key[:6]).digest()
        self.session_key: bytes | None = None
        self.sn = 1
        self.dp_sn = 1
        self.paired = False

        self._rx_buf = bytearray()
        self._rx_expected_len = 0

    def reset(self, protocol_version: int | None = None):
        if protocol_version is not None:
            self.protocol_version = protocol_version
        self.session_key = None
        self.sn = 1
        self.dp_sn = 1
        self.paired = False
        self._rx_buf.clear()
        self._rx_expected_len = 0

    def _build_encrypted_frame(
        self, code: int, data: bytes, ack_sn: int = 0, use_session: bool = True
    ) -> list[bytes]:
        sn = self.sn
        self.sn += 1

        header = struct.pack(">IIHH", sn, ack_sn, code, len(data))
        raw = header + data
        crc = crc16_modbus(raw)
        raw_with_crc = raw + struct.pack(">H", crc)

        pad_len = (16 - (len(raw_with_crc) % 16)) % 16
        padded = raw_with_crc + (b"\x00" * pad_len)

        if use_session and self.session_key:
            sec_flag = 0x04
            key = self.session_key
        else:
            sec_flag = 0x01
            key = self.login_key

        iv = os.urandom(16)
        cipher = AES.new(key, AES.MODE_CBC, iv)
        encrypted = bytes([sec_flag]) + iv + cipher.encrypt(padded)

        chunks = []
        pack_num = 0
        pos = 0
        total_len = len(encrypted)

        while pos < total_len:
            pkt = bytearray()
            pkt.extend(pack_varint(pack_num))
            if pack_num == 0:
                pkt.extend(pack_varint(total_len))
                pkt.append(self.protocol_version << 4)
            space = 20 - len(pkt)
            pkt.extend(encrypted[pos : pos + space])
            pos += space
            pack_num += 1
            chunks.append(bytes(pkt))

        return chunks

    def feed_ble_chunk(self, chunk: bytes) -> tuple[int, int, bytes] | None:
        if not chunk:
            return None
        pack_num, offset = unpack_varint(chunk, 0)
        if pack_num == 0:
            self._rx_buf.clear()
            self._rx_expected_len, offset = unpack_varint(chunk, offset)
            if offset >= len(chunk):
                return None
            _proto_ver = chunk[offset]
            offset += 1

        self._rx_buf.extend(chunk[offset:])
        if self._rx_expected_len == 0 or len(self._rx_buf) < self._rx_expected_len:
            return None

        encrypted = bytes(self._rx_buf[: self._rx_expected_len])
        self._rx_buf.clear()
        self._rx_expected_len = 0

        if len(encrypted) < 33:
            raise ValueError(f"Пакет BLE слишком короткий ({len(encrypted)} байт)")

        sec_flag = encrypted[0]
        iv = encrypted[1:17]
        ciphertext = encrypted[17:]
        if len(ciphertext) % 16 != 0:
            ciphertext = ciphertext[: (len(ciphertext) // 16) * 16]

        key = self.session_key if (sec_flag == 0x04 and self.session_key) else self.login_key

        cipher = AES.new(key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(ciphertext)

        sn, ack_sn, code, data_len = struct.unpack(">IIHH", decrypted[:12])
        if 12 + data_len > len(decrypted):
            raise ValueError(f"Ошибка расшифровки пакета (code=0x{code:04x}, len={data_len})")

        payload = decrypted[12 : 12 + data_len]
        return sn, code, payload

    def make_device_info_req(self) -> list[bytes]:
        return self._build_encrypted_frame(code=0x0000, data=b"", use_session=False)

    def handle_device_info_resp(self, payload: bytes) -> list[bytes]:
        if len(payload) < 12:
            raise ValueError(f"Короткий ответ Device Info ({len(payload)} байт)")
        if payload[2] in (2, 3, 4):
            self.protocol_version = payload[2]
        srand = payload[6:12]
        self.session_key = hashlib.md5(self.local_key[:6] + srand).digest()
        pair_payload = self.uuid + self.local_key[:6] + self.device_id.ljust(16, b"\x00")
        return self._build_encrypted_frame(code=0x0001, data=pair_payload, use_session=True)

    def _wrap_dp_command(self, dp_packet: bytes) -> list[bytes]:
        if self.protocol_version >= 4:
            sn = self.dp_sn
            self.dp_sn += 1
            v4_data = struct.pack(">IB", sn, 0x00) + dp_packet
            return self._build_encrypted_frame(code=0x0027, data=v4_data, use_session=True)
        return self._build_encrypted_frame(code=0x0002, data=dp_packet, use_session=True)

    def make_dp_bool(self, dp_id: int, value: bool) -> list[bytes]:
        dp_packet = struct.pack(">BBH", dp_id, 0x01, 1) + (b"\x01" if value else b"\x00")
        return self._wrap_dp_command(dp_packet)

    def make_dp_color_hsv(
        self, dp_id: int, r: int, g: int, b: int, brightness_scale: float
    ) -> list[bytes]:
        h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        h_int = int(round(h * 360)) % 360
        s_int = int(round(s * 1000))
        v_int = int(round(v * 1000 * max(0.05, min(1.0, brightness_scale))))
        hsv_ascii = f"{h_int:04x}{s_int:04x}{v_int:04x}".encode("ascii")
        dp_packet = struct.pack(">BBH", dp_id, 0x03, len(hsv_ascii)) + hsv_ascii
        return self._wrap_dp_command(dp_packet)


# ==============================================================================
# ЗАХВАТ ЭКРАНА
# ==============================================================================
def capture_screen_rgb(mode: str = "fullscreen") -> tuple[int, int, int]:
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        img = sct.grab(monitor)
        raw = img.rgb
        w, h = img.width, img.height

        if mode == "edges":
            step = 12
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

        step = 16 * 3
        r_sum = sum(raw[0::step])
        g_sum = sum(raw[1::step])
        b_sum = sum(raw[2::step])
        count = max(1, len(raw[0::step]))
        return r_sum // count, g_sum // count, b_sum // count


# ==============================================================================
# ФОНОВЫЙ СЕРВЕР (WEBSOCKET + UDP DISCOVERY)
# ==============================================================================
class BridgeServerController:
    def __init__(self, ui_queue: queue.Queue, cfg: dict):
        self.ui_queue = ui_queue
        self.cfg = cfg.copy()
        self.lock = threading.Lock()
        self.running = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None

    def update_live_settings(self, new_cfg: dict):
        with self.lock:
            self.cfg.update(new_cfg)

    def get_cfg_snapshot(self) -> dict:
        with self.lock:
            return self.cfg.copy()

    def start(self):
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._run_thread, daemon=True)
        self._thread.start()

    def stop(self):
        if not self.running:
            return
        self.running = False
        if self._loop and self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)

    def _emit(self, event_type: str, **kwargs):
        self.ui_queue.put({"type": event_type, **kwargs})

    def _run_thread(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop_event = asyncio.Event()
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as e:
            self._emit("log", text=f"Ошибка сервера: {e}")
            self._emit("server_state", active=False)
        finally:
            self.running = False
            self._loop.close()

    async def _udp_discovery_task(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setblocking(False)
        try:
            sock.bind(("0.0.0.0", DISCOVERY_PORT))
        except Exception:
            sock.close()
            return

        hostname = socket.gethostname()
        loop = asyncio.get_running_loop()

        try:
            while not self._stop_event.is_set():
                try:
                    data, addr = await asyncio.wait_for(
                        loop.sock_recvfrom(sock, 512), timeout=1.0
                    )
                    if data.strip() == b"LUMALINK_DISCOVER":
                        port = int(self.get_cfg_snapshot().get("port", WS_PORT))
                        reply = json.dumps(
                            {"service": "lumalink", "name": hostname, "port": port}
                        ).encode("utf-8")
                        await loop.sock_sendto(sock, reply, addr)
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    await asyncio.sleep(0.2)
        finally:
            sock.close()

    async def _send_chunks(self, ws: web.WebSocketResponse, chunks: list[bytes]):
        for chunk in chunks:
            await ws.send_bytes(chunk)

    async def _ambilight_loop(self, ws: web.WebSocketResponse, session: LightBridgeSession):
        smooth_r, smooth_g, smooth_b = 0.0, 0.0, 0.0
        initialized = False
        last_sent = (-100, -100, -100)

        while not ws.closed and session.paired and not self._stop_event.is_set():
            t0 = time.monotonic()
            snap = self.get_cfg_snapshot()
            fps = max(1, min(30, int(snap.get("fps", 14))))
            brightness = float(snap.get("brightness", 100)) / 100.0
            smoothing = max(0.0, min(0.90, float(snap.get("smoothing", 45)) / 100.0))
            mode = snap.get("capture_mode", "fullscreen")
            dp_colour = int(snap.get("dp_colour", 24))

            raw_r, raw_g, raw_b = await asyncio.to_thread(capture_screen_rgb, mode)

            if not initialized:
                smooth_r, smooth_g, smooth_b = float(raw_r), float(raw_g), float(raw_b)
                initialized = True
            else:
                alpha = smoothing
                smooth_r = smooth_r * alpha + raw_r * (1.0 - alpha)
                smooth_g = smooth_g * alpha + raw_g * (1.0 - alpha)
                smooth_b = smooth_b * alpha + raw_b * (1.0 - alpha)

            r, g, b = int(round(smooth_r)), int(round(smooth_g)), int(round(smooth_b))
            self._emit("color", rgb=(r, g, b))

            if abs(r - last_sent[0]) + abs(g - last_sent[1]) + abs(b - last_sent[2]) >= 5:
                last_sent = (r, g, b)
                try:
                    await ws.send_str(json.dumps({"rgb": [r, g, b]}))
                    chunks = session.make_dp_color_hsv(dp_colour, r, g, b, brightness)
                    await self._send_chunks(ws, chunks)
                except Exception:
                    break

            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(0.01, (1.0 / fps) - elapsed))

    async def _ws_handler(self, request: web.Request):
        ws = web.WebSocketResponse(heartbeat=20.0)
        await ws.prepare(request)

        snap = self.get_cfg_snapshot()
        session = LightBridgeSession(snap)
        dp_switch = int(snap.get("dp_switch", 20))
        stream_task: asyncio.Task | None = None

        peer = request.remote or "Смартфон"
        self._emit("client_state", connected=True, text=f"Подключён телефон ({peer})")
        self._emit("log", text=f"Смартфон подключился ({peer})")

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data.get("type") == "ble_connected":
                        cur_proto = int(self.get_cfg_snapshot().get("protocol_version", 4))
                        session.reset(protocol_version=cur_proto)
                        self._emit("log", text=f"Канал BLE готов -> отправка запроса (протокол v{cur_proto})...")
                        await self._send_chunks(ws, session.make_device_info_req())

                    elif data.get("type") == "ble_disconnected":
                        session.reset()
                        if stream_task:
                            stream_task.cancel()
                        self._emit("client_state", connected=True, text="Телефон на связи • Лампа отключена")
                        self._emit("log", text="Лампа отключилась от телефона")

                elif msg.type == WSMsgType.BINARY:
                    self._emit("log", text=f"BLE RX ({len(msg.data)} байт): {msg.data.hex()}")
                    parsed = session.feed_ble_chunk(msg.data)
                    if not parsed:
                        continue

                    _sn, code, payload = parsed

                    if code == 0x0000:
                        ver = payload[2] if len(payload) > 2 else "?"
                        self._emit("log", text=f"Ответ чипа получен (v{ver}), отправка Pair...")
                        await ws.send_str(json.dumps({"log": "Обмен ключами..."}))
                        await self._send_chunks(ws, session.handle_device_info_resp(payload))

                    elif code == 0x0001:
                        if payload and payload[0] == 0x00:
                            session.paired = True
                            self._emit("client_state", connected=True, text="Синхронизация активна!")
                            self._emit("log", text="Авторизация успешна! Трансляция цвета запущена.")
                            await ws.send_str(
                                json.dumps(
                                    {
                                        "status": "Синхронизация активна!",
                                        "log": "Лампа подключена. Передача цвета...",
                                    }
                                )
                            )
                            await self._send_chunks(ws, session.make_dp_bool(dp_switch, True))
                            await asyncio.sleep(0.15)
                            if stream_task:
                                stream_task.cancel()
                            stream_task = asyncio.create_task(self._ambilight_loop(ws, session))
                        else:
                            err_msg = f"Ошибка ключа лампы (код {payload.hex()})"
                            self._emit("log", text=err_msg)
                            await ws.send_str(json.dumps({"status": err_msg, "log": err_msg}))

                elif msg.type == WSMsgType.ERROR:
                    self._emit("log", text=f"Ошибка WebSocket: {ws.exception()}")

        except Exception as e:
            err_text = f"Сбой сессии: {e}"
            self._emit("log", text=err_text)
            try:
                await ws.send_str(json.dumps({"status": err_text}))
            except Exception:
                pass
        finally:
            if stream_task:
                stream_task.cancel()
            self._emit("client_state", connected=False, text="Ожидание подключения телефона...")
            self._emit("log", text="Смартфон отключился")

        return ws

    async def _serve(self):
        port = int(self.get_cfg_snapshot().get("port", WS_PORT))
        app = web.Application()
        app.router.add_get("/ws", self._ws_handler)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()

        udp_task = asyncio.create_task(self._udp_discovery_task())
        self._emit("server_state", active=True)
        self._emit("log", text=f"Сервер LumaLink запущен (порт {port})")

        try:
            await self._stop_event.wait()
        finally:
            udp_task.cancel()
            await runner.cleanup()
            self._emit("server_state", active=False)
            self._emit("log", text="Сервер остановлен")


# ==============================================================================
# ГРАФИЧЕСКИЙ ИНТЕРФЕЙС WINDOWS (TKINTER)
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
        self.root.geometry("560x790")
        self.root.minsize(520, 720)
        self.root.configure(bg=self.BG_MAIN)

        self.cfg = load_config()
        self.ui_queue: queue.Queue = queue.Queue()
        self.controller = BridgeServerController(self.ui_queue, self.cfg)
        self.ips = get_local_ips()
        self.primary_ip = self.ips[0] if self.ips else "192.168.1.X"

        self._build_ui()
        self._poll_queue()

        if self._is_config_valid(silent=True):
            self.controller.start()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

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

        tk.Label(
            ctrl_card,
            text="Настройки подсветки в реальном времени",
            font=("Segoe UI", 10, "bold"),
            fg=self.TEXT_PRIMARY,
            bg=self.BG_CARD,
        ).pack(anchor="w", pady=(0, 10))

        self.var_brightness = tk.IntVar(value=int(self.cfg.get("brightness", 100)))
        self.var_smoothing = tk.IntVar(value=int(self.cfg.get("smoothing", 45)))
        self.var_fps = tk.IntVar(value=int(self.cfg.get("fps", 14)))
        self.var_mode = tk.StringVar(value=self.cfg.get("capture_mode", "fullscreen"))

        self._add_slider(ctrl_card, "Яркость лампы", self.var_brightness, 10, 100, "%")
        self._add_slider(ctrl_card, "Плавность переходов", self.var_smoothing, 0, 85, "%")
        self._add_slider(ctrl_card, "Частота обновления", self.var_fps, 5, 25, " FPS")

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
                command=self._push_live_settings,
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
        self.var_dp_switch = tk.StringVar(value=str(self.cfg.get("dp_switch", 20)))
        self.var_dp_colour = tk.StringVar(value=str(self.cfg.get("dp_colour", 24)))
        self.var_proto = tk.IntVar(value=int(self.cfg.get("protocol_version", 4)))

        self._add_field(keys_card, "Device ID:", self.var_dev_id)
        self._add_field(keys_card, "Local Key (16 симв.):", self.var_local_key)
        self._add_field(keys_card, "Device UUID (16 симв.):", self.var_uuid)

        dp_row = tk.Frame(keys_card, bg=self.BG_CARD)
        dp_row.pack(fill="x", pady=(4, 8))
        tk.Label(dp_row, text="DP Питание:", font=("Segoe UI", 9), fg=self.TEXT_MUTED, bg=self.BG_CARD).pack(side="left")
        tk.Entry(
            dp_row, textvariable=self.var_dp_switch, width=4, font=("Consolas", 10),
            fg=self.TEXT_PRIMARY, bg=self.BG_INPUT, insertbackground=self.TEXT_PRIMARY, bd=0
        ).pack(side="left", padx=(4, 10), ipady=3)

        tk.Label(dp_row, text="DP Цвет:", font=("Segoe UI", 9), fg=self.TEXT_MUTED, bg=self.BG_CARD).pack(side="left")
        tk.Entry(
            dp_row, textvariable=self.var_dp_colour, width=4, font=("Consolas", 10),
            fg=self.TEXT_PRIMARY, bg=self.BG_INPUT, insertbackground=self.TEXT_PRIMARY, bd=0
        ).pack(side="left", padx=(4, 12), ipady=3)

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
            ).pack(side="left", padx=(4, 0))

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

    def _add_slider(self, parent, title: str, var: tk.IntVar, frm: int, to: int, suffix: str):
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
            self._push_live_settings()

        scale = ttk.Scale(
            row, from_=frm, to=to, orient="horizontal", style="Dark.Horizontal.TScale", command=on_slide
        )
        scale.set(var.get())
        scale.pack(side="left", fill="x", expand=True, padx=8)

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

    def _collect_cfg(self) -> dict:
        self.cfg.update(
            {
                "device_id": self.var_dev_id.get().strip(),
                "local_key": self.var_local_key.get().strip(),
                "device_uuid": self.var_uuid.get().strip(),
                "dp_switch": int(self.var_dp_switch.get() or 20),
                "dp_colour": int(self.var_dp_colour.get() or 24),
                "protocol_version": int(self.var_proto.get() or 4),
                "brightness": self.var_brightness.get(),
                "smoothing": self.var_smoothing.get(),
                "fps": self.var_fps.get(),
                "capture_mode": self.var_mode.get(),
            }
        )
        return self.cfg

    def _push_live_settings(self):
        try:
            cfg = self._collect_cfg()
            self.controller.update_live_settings(cfg)
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
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _poll_queue(self):
        while not self.ui_queue.empty():
            ev = self.ui_queue.get_nowait()
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
                r, g, b = ev["rgb"]
                hex_col = f"#{r:02x}{g:02x}{b:02x}"
                self.color_canvas.itemconfig(self.orb_outer, fill=hex_col, outline=hex_col)
                self.lbl_rgb_info.config(text=f"Цвет экрана: RGB({r}, {g}, {b})  •  {hex_col.upper()}")

        self.root.after(50, self._poll_queue)

    def _on_close(self):
        self.controller.stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    LumaLinkApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
