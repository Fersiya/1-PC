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

from typing import Any
from aiohttp import web, WSMsgType
from Crypto.Cipher import AES
import mss

try:
    from PIL import Image, ImageDraw, ImageTk
    HAS_PIL = True
except Exception:
    HAS_PIL = False

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
    "dp_switch": 1,
    "dp_mode": 2,
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
# ПРОТОКОЛ И ШИФРОВАНИЕ КАНАЛА СВЯЗИ С ЛАМПОЙ (TUYA BLE V4 / V3 / V2)
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


SEC_AUTH = 0x01  # ключ auth_key из Device Info (только для разбора входящих)
SEC_LOGIN = 0x04  # login_key = md5(local_key[:6]) — только запрос Device Info
SEC_SESSION = 0x05  # session_key = md5(local_key[:6] + srand) — всё остальное

CODE_DEVICE_INFO = 0x0000
CODE_PAIR = 0x0001
CODE_DPS = 0x0002
CODE_DPS_V4 = 0x0027
CODE_RECV_DP = 0x8001
CODE_RECV_TIME_DP = 0x8003
CODE_RECV_SIGN_DP = 0x8004
CODE_RECV_SIGN_TIME_DP = 0x8005
CODE_RECV_DP_V4 = 0x8006
CODE_RECV_TIME_DP_V4 = 0x8007
CODE_RECV_TIME1_REQ = 0x8011
CODE_RECV_TIME2_REQ = 0x8012

TUYA_COMPANY_ID = 0x07D0


def parse_adv_protocol(adv_hex: str) -> int | None:
    """Версия протокола Tuya BLE из рекламного пакета.

    FD50 (протокол 4): старший полубайт первого байта service data 0xFD50.
    Classic (протокол 2/3): байт 1 manufacturer data (company id 0x07D0).
    """
    try:
        raw = bytes.fromhex(adv_hex)
    except ValueError:
        return None
    pos = 0
    while pos < len(raw):
        ln = raw[pos]
        if ln == 0 or pos + 1 + ln > len(raw):
            break
        ad_type = raw[pos + 1]
        body = raw[pos + 2 : pos + 1 + ln]
        if ad_type == 0x16 and len(body) >= 3 and int.from_bytes(body[0:2], "little") == 0xFD50:
            if body[2] & 0x01:  # запись идентификации
                ver = body[2] >> 4
                return ver if ver in (2, 3, 4) else None
        elif (
            ad_type == 0xFF
            and len(body) >= 4
            and int.from_bytes(body[0:2], "little") == TUYA_COMPANY_ID
        ):
            return body[3] if body[3] in (2, 3, 4) else None
        pos += 1 + ln
    return None


class LightBridgeSession:
    """Сессия Tuya BLE: Device Info (login_key, флаг 4) -> Pair и DP (session_key, флаг 5)."""

    def __init__(self, cfg: dict):
        self.device_id = cfg["device_id"].strip().encode("utf-8")[:22]
        self.local_key = cfg["local_key"].strip().encode("utf-8")[:6]
        self.uuid = cfg["device_uuid"].strip().encode("utf-8")[:16]
        self.protocol_version = int(cfg.get("protocol_version", 3))
        self.chunk_size = 20

        self.login_key = hashlib.md5(self.local_key).digest()
        self.session_key: bytes | None = None
        self.auth_key: bytes | None = None
        self.sn = 1
        self.dp_sn = 1
        self.paired = False
        self.handshake_replied = False
        self.is_bound = False

        self._rx_buf = bytearray()
        self._rx_expected_len = 0

    def reset(self, protocol_version: int | None = None, chunk_size: int = 20):
        if protocol_version is not None:
            self.protocol_version = protocol_version
        self.chunk_size = max(20, min(240, int(chunk_size)))
        self.session_key = None
        self.auth_key = None
        self.sn = 1
        self.dp_sn = 1
        self.paired = False
        self.handshake_replied = False
        self._rx_buf.clear()
        self._rx_expected_len = 0

    def _build_frame(
        self,
        code: int,
        data: bytes,
        ack_sn: int = 0,
        proto_ver: int | None = None,
        chunk_size: int | None = None,
    ) -> list[bytes]:
        if code == CODE_DEVICE_INFO:
            key, sec_flag = self.login_key, SEC_LOGIN
        else:
            if not self.session_key:
                raise ValueError("Нет session-ключа: сначала нужен ответ Device Info")
            key, sec_flag = self.session_key, SEC_SESSION

        sn = self.sn
        self.sn += 1

        ver = proto_ver if proto_ver is not None else self.protocol_version
        max_pkt = chunk_size if chunk_size is not None else self.chunk_size

        raw = struct.pack(">IIHH", sn, ack_sn, code, len(data)) + data
        raw += struct.pack(">H", crc16_modbus(raw))
        raw += b"\x00" * ((16 - len(raw) % 16) % 16)

        iv = os.urandom(16)
        payload_wire = bytes([sec_flag]) + iv + AES.new(key, AES.MODE_CBC, iv).encrypt(raw)

        chunks = []
        pack_num = 0
        pos = 0
        total_len = len(payload_wire)

        while pos < total_len:
            pkt = bytearray()
            pkt.extend(pack_varint(pack_num))
            if pack_num == 0:
                pkt.extend(pack_varint(total_len))
                pkt.append(ver << 4)
            space = max_pkt - len(pkt)
            pkt.extend(payload_wire[pos : pos + space])
            pos += space
            pack_num += 1
            chunks.append(bytes(pkt))

        return chunks

    def feed_ble_chunk(self, chunk: bytes) -> tuple[int, int, bytes] | None:
        """Собирает пакет из BLE-чанков. Возвращает (sn, code, payload) или None, если пакет не полон."""
        if not chunk:
            return None
        pack_num, offset = unpack_varint(chunk, 0)
        if pack_num == 0:
            self._rx_buf.clear()
            self._rx_expected_len, offset = unpack_varint(chunk, offset)
            if offset >= len(chunk):
                return None
            offset += 1  # байт версии протокола
        elif self._rx_expected_len == 0:
            self._rx_buf.clear()
            return None

        self._rx_buf.extend(chunk[offset:])
        if self._rx_expected_len == 0 or len(self._rx_buf) < self._rx_expected_len:
            return None

        wire = bytes(self._rx_buf[: self._rx_expected_len])
        self._rx_buf.clear()
        self._rx_expected_len = 0

        sec_flag = wire[0]
        if len(wire) < 33:
            raise ValueError(f"Пакет BLE слишком короткий ({len(wire)} байт)")

        if sec_flag == SEC_LOGIN:
            key = self.login_key
        elif sec_flag == SEC_SESSION:
            key = self.session_key
        elif sec_flag == SEC_AUTH:
            key = self.auth_key
        else:
            raise ValueError(f"Неизвестный флаг шифрования 0x{sec_flag:02x}")
        if key is None:
            raise ValueError(f"Нет ключа для флага шифрования 0x{sec_flag:02x}")

        iv = wire[1:17]
        ciphertext = wire[17:]
        ciphertext = ciphertext[: (len(ciphertext) // 16) * 16]
        decrypted = AES.new(key, AES.MODE_CBC, iv).decrypt(ciphertext)

        sn, _ack_sn, code, data_len = struct.unpack(">IIHH", decrypted[:12])
        end = 12 + data_len
        if end + 2 > len(decrypted):
            raise ValueError(
                f"Ошибка расшифровки (code=0x{code:04x}, len={data_len}) — проверьте Local Key"
            )
        (crc_rx,) = struct.unpack(">H", decrypted[end : end + 2])
        if crc_rx != crc16_modbus(decrypted[:end]):
            raise ValueError(
                f"CRC пакета не совпал (code=0x{code:04x}) — проверьте Local Key"
            )

        return sn, code, decrypted[12:end]

    def make_probe_req(self, proto_ver: int, chunk_size: int) -> list[bytes]:
        """Запрос Device Info (login_key, флаг 4) с заданной версией протокола в заголовке."""
        self.protocol_version = proto_ver
        self.chunk_size = chunk_size
        self.sn = 1
        # Протокол 4: ровно 2 байта (размер данных в пакете), иначе лампа сама рвёт связь.
        info_payload = struct.pack(">H", chunk_size) if proto_ver >= 4 else b""
        return self._build_frame(
            code=CODE_DEVICE_INFO,
            data=info_payload,
            proto_ver=proto_ver,
            chunk_size=chunk_size,
        )

    def handle_device_info_resp(self, payload: bytes) -> list[bytes]:
        if len(payload) < 12:
            raise ValueError(f"Короткий ответ Device Info ({len(payload)} байт)")
        if payload[2] in (2, 3, 4):
            self.protocol_version = payload[2]
        self.is_bound = payload[5] != 0
        srand = payload[6:12]
        self.session_key = hashlib.md5(self.local_key + srand).digest()
        if len(payload) >= 46:
            self.auth_key = payload[14:46]
        # uuid(16) + local_key(6) + device_id, весь пакет добивается нулями до 44 байт
        pair_payload = (self.uuid + self.local_key + self.device_id).ljust(44, b"\x00")
        return self._build_frame(code=CODE_PAIR, data=pair_payload)

    def make_response(self, code: int, ack_sn: int, payload: bytes = b"") -> list[bytes] | None:
        """Ответ на входящую команду лампы (запрос времени, отчёты DP)."""
        if code == CODE_RECV_TIME1_REQ:
            tz = -int(time.timezone / 36)
            data = str(int(time.time() * 1000)).encode() + struct.pack(">h", tz)
        elif code == CODE_RECV_TIME2_REQ:
            t = time.localtime()
            tz = -int(time.timezone / 36)
            data = struct.pack(
                ">BBBBBBBh",
                t.tm_year % 100, t.tm_mon, t.tm_mday,
                t.tm_hour, t.tm_min, t.tm_sec, t.tm_wday, tz,
            )
        elif code in (CODE_RECV_DP, CODE_RECV_TIME_DP):
            data = b""
        elif code in (CODE_RECV_DP_V4, CODE_RECV_TIME_DP_V4) and len(payload) >= 7:
            if payload[5] & 0x80:  # устройство не ждёт подтверждения
                return None
            data = payload[:7] + b"\x00"
        elif code in (CODE_RECV_SIGN_DP, CODE_RECV_SIGN_TIME_DP) and len(payload) >= 3:
            data = payload[:2] + bytes([payload[2], 0])
        else:
            return None
        return self._build_frame(code=code, data=data, ack_sn=ack_sn)

    def _dp_item(self, dp_id: int, dp_type: int, value: bytes) -> bytes:
        # v3: длина значения — 1 байт (ha_tuya_ble); v4: 2 байта (SDK Tuya, ha_tuya_ble v4).
        if self.protocol_version >= 4:
            return struct.pack(">BBH", dp_id, dp_type, len(value)) + value
        return struct.pack(">BBB", dp_id, dp_type, len(value)) + value

    def _wrap_dp_command(self, dp_packet: bytes) -> list[bytes]:
        if self.protocol_version >= 4:
            sn = self.dp_sn
            self.dp_sn += 1
            v4_data = struct.pack(">BI", 0x00, sn) + dp_packet
            return self._build_frame(code=CODE_DPS_V4, data=v4_data)
        return self._build_frame(code=CODE_DPS, data=dp_packet)

    def make_dp_bool(self, dp_id: int, value: bool) -> list[bytes]:
        return self._wrap_dp_command(self._dp_item(dp_id, 0x01, b"\x01" if value else b"\x00"))

    def make_dp_enum(self, dp_id: int, value: int) -> list[bytes]:
        return self._wrap_dp_command(self._dp_item(dp_id, 0x04, bytes([value & 0xFF])))

    def make_dp_value(self, dp_id: int, value: int) -> list[bytes]:
        val_bytes = struct.pack(">I", int(value))
        return self._wrap_dp_command(self._dp_item(dp_id, 0x02, val_bytes))

    def make_combined_dps(self, dp_items: list[tuple[int, int, bytes]]) -> list[bytes]:
        """Упаковывает несколько DP (TLV) в единый кадр (атомарное применение)."""
        combined = bytearray()
        for dp_id, dp_type, val in dp_items:
            combined.extend(self._dp_item(dp_id, dp_type, val))
        return self._wrap_dp_command(bytes(combined))

    def make_color_bundle(
        self, dp_switch: int, dp_mode: int, dp_colour: int, r: int, g: int, b: int, brightness_scale: float = 1.0, mode_val: int = 1
    ) -> list[bytes]:
        """Формирует единый multi-TLV пакет: питание ВКЛ + режим Colour + яркость 1000 + цвет HSV + DP 61 (Dreamlight paint_colour_data)."""
        h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        if s > 0.01:
            s = min(1.0, s * 2.2 + 0.12)
        h_int = int(round(h * 360)) % 360
        s_int = int(round(s * 1000))
        v_int = int(round(v * 1000 * max(0.05, min(1.0, brightness_scale))))
        hsv_ascii = f"{h_int:04x}{s_int:04x}{v_int:04x}".encode("ascii")

        # Tuya Dreamlight (RGBIC) real-time paint color format (DP 61):
        # 00 (версия 0) + 01 (режим цвета) + 00 (эффект) + 14 (число сегментов = 20) + 00 (действие: вся лента) + HHHH + SSSS + VVVV
        dp61_raw = struct.pack(">BBBBBHHH", 0, 1, 0, 20, 0, h_int, s_int, v_int)

        items = [
            (dp_switch, 0x01, b"\x01"),
        ]
        if dp_mode > 0:
            items.append((dp_mode, 0x04, bytes([mode_val & 0xFF])))
        items.append((47, 0x02, struct.pack(">I", int(round(1000 * max(0.05, min(1.0, brightness_scale)))))))
        if dp_colour != 61:
            items.append((dp_colour, 0x03, hsv_ascii))
        items.append((61, 0x00, dp61_raw))

        return self.make_combined_dps(items)

    def make_dp_color_hsv(
        self, dp_id: int, r: int, g: int, b: int, brightness_scale: float
    ) -> list[bytes]:
        h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        # Усиление насыщенности для сочного Ambilight на светодиодах
        if s > 0.01:
            s = min(1.0, s * 2.2 + 0.12)
        h_int = int(round(h * 360)) % 360
        s_int = int(round(s * 1000))
        v_int = int(round(v * 1000 * max(0.05, min(1.0, brightness_scale))))
        hsv_ascii = f"{h_int:04x}{s_int:04x}{v_int:04x}".encode("ascii")

        # Tuya Dreamlight (RGBIC) real-time paint color format (DP 61):
        dp61_raw = struct.pack(">BBBBBHHH", 0, 1, 0, 20, 0, h_int, s_int, v_int)

        items = []
        if dp_id != 61:
            items.append((dp_id, 0x03, hsv_ascii))
        items.append((61, 0x00, dp61_raw))
        return self.make_combined_dps(items)

    @staticmethod
    def parse_dp_payload(code: int, payload: bytes) -> dict:
        """Парсинг стандартного TLV контейнера Tuya DP."""
        dps = {}
        if code in (CODE_RECV_DP_V4, CODE_RECV_TIME_DP_V4):
            offset = 7
        elif code in (CODE_RECV_SIGN_DP, CODE_RECV_SIGN_TIME_DP):
            offset = 4
        elif code in (CODE_DPS_V4,):
            offset = 5
        elif code in (CODE_DPS,):
            offset = 0
        else:
            offset = 0

        while offset < len(payload):
            if code in (CODE_RECV_DP_V4, CODE_RECV_TIME_DP_V4, CODE_DPS_V4):
                if offset + 4 > len(payload):
                    break
                dp_id, dp_type, val_len = struct.unpack(">BBH", payload[offset : offset + 4])
                offset += 4
            else:
                if offset + 3 > len(payload):
                    break
                dp_id, dp_type, val_len = struct.unpack(">BBB", payload[offset : offset + 3])
                offset += 3

            if offset + val_len > len(payload):
                break
            val_data = payload[offset : offset + val_len]
            offset += val_len

            if dp_type == 0x01:  # Bool
                dps[dp_id] = bool(val_data[0]) if val_data else False
            elif dp_type == 0x02:  # Value (4 байта)
                dps[dp_id] = int.from_bytes(val_data, "big")
            elif dp_type == 0x04:  # Enum (1 байт)
                dps[dp_id] = val_data[0] if val_data else 0
            elif dp_type == 0x03:  # String
                try:
                    dps[dp_id] = val_data.decode("utf-8")
                except Exception:
                    dps[dp_id] = val_data.hex()
            else:  # Raw / Bitmap
                dps[dp_id] = val_data.hex()

        return dps


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
# ФОНОВЫЙ СЕРВЕР (WEBSOCKET + UDP DISCOVERY + АВТОПОДБОР ПРОТОКОЛА)
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
        self._active_ws: web.WebSocketResponse | None = None
        self._active_session: LightBridgeSession | None = None
        self.pause_ambilight_until: float = 0.0

    def pause_ambilight(self, seconds: float = 60.0):
        self.pause_ambilight_until = time.monotonic() + seconds
        self._emit("log", text=f"Синхронизация экрана приостановлена на {int(seconds)} сек.")

    def resume_ambilight(self):
        self.pause_ambilight_until = 0.0
        self._emit("log", text="Синхронизация экрана возобновлена")

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

    def send_manual_dp(self, dp_id: int, dp_type: str, value):
        if not self._loop or not self.running:
            self._emit("log", text="Сервер не запущен — сначала запустите сервер!")
            return
        asyncio.run_coroutine_threadsafe(self._async_send_manual_dp(dp_id, dp_type, value), self._loop)

    async def _async_send_manual_dp(self, dp_id: int, dp_type: str, value):
        if not self._active_ws or not self._active_session or self._active_ws.closed:
            self._emit("log", text="Нельзя отправить команду: смартфон или лампа не подключены!")
            return
        session = self._active_session
        ws = self._active_ws
        # При ручном тесте приостанавливаем Ambilight на 30 секунд, чтобы экран не перезаписывал тест
        self.pause_ambilight_until = time.monotonic() + 30.0
        try:
            if dp_type == "bool":
                chunks = session.make_dp_bool(dp_id, bool(value))
                state_str = "ВКЛ" if value else "ВЫКЛ"
                self._emit("log", text=f"Команда: DP {dp_id} (питание) = {state_str} [экран на паузе 30с]")
                await self._send_chunks(ws, chunks)
            elif dp_type == "enum":
                chunks = session.make_dp_enum(dp_id, int(value))
                mode_names = {0: "White", 1: "Colour", 2: "Scene", 3: "Music"}
                self._emit("log", text=f"Команда: DP {dp_id} (режим) = {value} ({mode_names.get(int(value), '?')}) [экран на паузе 30с]")
                await self._send_chunks(ws, chunks)
            elif dp_type == "value":
                chunks = session.make_dp_value(dp_id, int(value))
                self._emit("log", text=f"Команда: DP {dp_id} (значение) = {value} [экран на паузе 30с]")
                await self._send_chunks(ws, chunks)
            elif dp_type == "color":
                r, g, b = value
                dp_switch = int(self.get_cfg_snapshot().get("dp_switch", 1))
                dp_mode = int(self.get_cfg_snapshot().get("dp_mode", 2))
                chunks = session.make_color_bundle(dp_switch, dp_mode, dp_id, r, g, b, 1.0)
                self._emit("log", text=f"Команда: Единый пакет Multi-DP (Питание ВКЛ, Режим 1, Ярк 1000, Цвет RGB({r},{g},{b}), DP61 Dreamlight) [экран на паузе 30с]")
                await self._send_chunks(ws, chunks)
            elif dp_type == "mode":
                dp_switch = int(self.get_cfg_snapshot().get("dp_switch", 1))
                mode_val = int(value)
                mode_names = {0: "White", 1: "Colour", 2: "Scene", 3: "Music"}
                items = [
                    (dp_switch, 0x01, b"\x01"),
                    (dp_id, 0x04, bytes([mode_val & 0xFF])),
                    (47, 0x02, struct.pack(">I", 1000)),
                ]
                chunks = session.make_combined_dps(items)
                self._emit("log", text=f"Команда: Смена режима Multi-DP (Питание ВКЛ, Режим {mode_val} [{mode_names.get(mode_val, '?')}], Ярк 1000) [экран на паузе 30с]")
                await self._send_chunks(ws, chunks)
            elif dp_type == "custom":
                raw_type = int(value.get("type", 0))
                raw_bytes = value.get("bytes", b"")
                chunks = session._wrap_dp_command(session._dp_item(dp_id, raw_type, raw_bytes))
                self._emit("log", text=f"Команда: Ручной DP {dp_id} (тип {raw_type}, {len(raw_bytes)} Б) = {raw_bytes.hex()} [экран на паузе 30с]")
                await self._send_chunks(ws, chunks)
        except Exception as e:
            self._emit("log", text=f"Ошибка отправки команды: {e}")

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

    async def _handshake_probe_loop(
        self,
        ws: web.WebSocketResponse,
        session: LightBridgeSession,
        mtu: int,
        adv_ver: int | None = None,
    ):
        full_chunk = max(20, min(240, mtu - 3))
        pref_ver = adv_ver or int(self.get_cfg_snapshot().get("protocol_version", 3))

        # Запрос Device Info всегда идёт с login_key (флаг 0x04); перебираем только
        # версию протокола в заголовке и размер чанка.
        versions: list[int] = [pref_ver]
        if not adv_ver:  # версия не известна из рекламы — пробуем остальные
            versions += [v for v in (3, 4, 2) if v != pref_ver]
        probes = [(v, 20, f"v{v}, чанк 20 Б") for v in versions]
        if full_chunk > 20:
            probes.append((pref_ver, full_chunk, f"v{pref_ver}, чанк {full_chunk} Б"))

        for p_ver, c_size, label in probes:
            if ws.closed or session.handshake_replied or self._stop_event.is_set():
                return
            chunks = session.make_probe_req(p_ver, c_size)
            self._emit("log", text=f"Запрос Device Info [{label}] ({len(chunks)} пак.)")
            try:
                await self._send_chunks(ws, chunks)
            except Exception:
                return
            await asyncio.sleep(1.5)

        if not (ws.closed or session.handshake_replied):
            hint = (
                "Лампа не отвечает на рукопожатие. Закройте Smart Life/Tuya на телефоне "
                "(у лампы одно BLE-соединение) и проверьте, что это Tuya-лампа."
            )
            self._emit("log", text=hint)
            try:
                await ws.send_str(json.dumps({"status": hint}))
            except Exception:
                pass

    async def _ambilight_loop(self, ws: web.WebSocketResponse, session: LightBridgeSession):
        smooth_r, smooth_g, smooth_b = 0.0, 0.0, 0.0
        initialized = False
        last_sent = (-100, -100, -100)
        first_frame = True

        while not ws.closed and session.paired and not self._stop_event.is_set():
            t0 = time.monotonic()
            if time.monotonic() < self.pause_ambilight_until:
                first_frame = True
                await asyncio.sleep(0.15)
                continue
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

            if first_frame or abs(r - last_sent[0]) + abs(g - last_sent[1]) + abs(b - last_sent[2]) >= 5:
                last_sent = (r, g, b)
                try:
                    await ws.send_str(json.dumps({"rgb": [r, g, b]}))
                    if first_frame:
                        first_frame = False
                        dp_switch = int(snap.get("dp_switch", 1))
                        dp_mode = int(snap.get("dp_mode", 2))
                        chunks = session.make_color_bundle(dp_switch, dp_mode, dp_colour, r, g, b, brightness)
                    else:
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
        dp_mode = int(snap.get("dp_mode", 21))
        adv_ver: int | None = None
        dp_ack_logged = False
        stream_task: asyncio.Task | None = None
        probe_task: asyncio.Task | None = None

        peer = request.remote or "Смартфон"
        self._active_ws = ws
        self._active_session = session
        self._emit("client_state", connected=True, text=f"Подключён телефон ({peer})")
        self._emit("log", text=f"Смартфон подключился ({peer})")

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data.get("type") == "ble_connected":
                        mtu = int(data.get("mtu", 23))
                        session.reset(protocol_version=adv_ver)
                        if probe_task:
                            probe_task.cancel()
                        probe_task = asyncio.create_task(
                            self._handshake_probe_loop(ws, session, mtu, adv_ver)
                        )

                    elif data.get("type") == "ble_gatt_dump":
                        adv_hex = data.get("adv", "")
                        reads = data.get("reads", {})
                        gatt_list = data.get("gatt", [])
                        self._emit("log", text=f"GATT дамп ({data.get('mac')}): сервисов={len(gatt_list)}")
                        for s in gatt_list:
                            c_desc = ", ".join(
                                f"{c['uuid'][4:8]}(p={c['props']})" for c in s.get("chars", [])
                            )
                            self._emit("log", text=f"  Сервис {s['uuid'][4:8]}: {c_desc}")
                        for k, v in reads.items():
                            self._emit("log", text=f"  Read {k[4:8]}: {v}")
                        if adv_hex:
                            self._emit("log", text=f"  Adv: {adv_hex[:64]}")
                            adv_ver = parse_adv_protocol(adv_hex)
                            if adv_ver:
                                self._emit("log", text=f"  Версия протокола Tuya BLE из рекламы: v{adv_ver}")

                    elif data.get("type") == "ble_tx_err":
                        self._emit(
                            "log",
                            text=f"Ошибка записи BLE: status={data.get('status')}, len={data.get('len')}",
                        )

                    elif data.get("type") == "ble_disconnected":
                        session.reset()
                        if probe_task:
                            probe_task.cancel()
                        if stream_task:
                            stream_task.cancel()
                        self._emit("client_state", connected=True, text="Телефон на связи • Лампа отключена")
                        self._emit("log", text="Лампа отключилась от телефона")

                elif msg.type == WSMsgType.BINARY:
                    session.handshake_replied = True
                    if probe_task:
                        probe_task.cancel()
                    self._emit("log", text=f"BLE RX ({len(msg.data)} байт): {msg.data.hex()}")
                    try:
                        parsed = session.feed_ble_chunk(msg.data)
                    except ValueError as e:
                        err_msg = f"Не удалось разобрать пакет лампы: {e}"
                        self._emit("log", text=err_msg)
                        await ws.send_str(json.dumps({"status": err_msg}))
                        continue
                    if not parsed:
                        continue

                    rx_sn, code, payload = parsed

                    if code == CODE_DEVICE_INFO:
                        self._emit(
                            "log",
                            text=f"Ответ лампы получен (протокол v{payload[2] if len(payload) > 2 else '?'}), отправка Pair...",
                        )
                        await ws.send_str(json.dumps({"log": "Обмен ключами..."}))
                        await self._send_chunks(ws, session.handle_device_info_resp(payload))

                    elif code == CODE_PAIR:
                        # 0 — успех, 2 — лампа уже сопряжена (тоже успех)
                        if payload and payload[0] in (0x00, 0x02):
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
                            init_bundle = session.make_color_bundle(
                                dp_switch, dp_mode, int(snap.get("dp_colour", 24)), 255, 255, 255, 1.0
                            )
                            await self._send_chunks(ws, init_bundle)
                            await asyncio.sleep(0.1)
                            if stream_task:
                                stream_task.cancel()
                            stream_task = asyncio.create_task(self._ambilight_loop(ws, session))
                        else:
                            err_msg = (
                                f"Лампа отклонила Pair (код {payload.hex()}). "
                                "Проверьте Device ID, Local Key и UUID."
                            )
                            self._emit("log", text=err_msg)
                            await ws.send_str(json.dumps({"status": err_msg, "log": err_msg}))

                    elif code in (CODE_DPS, CODE_DPS_V4):
                        if payload and payload[-1] != 0x00:
                            self._emit("log", text=f"Лампа отклонила DP-команду (ответ {payload.hex()})")
                        elif not dp_ack_logged:
                            dp_ack_logged = True
                            self._emit("log", text="Лампа приняла DP-команду")

                    else:
                        dps = session.parse_dp_payload(code, payload)
                        if dps:
                            mode_names = {0: "White", 1: "Colour", 2: "Scene", 3: "Music"}
                            dp_desc = []
                            for dpid, val in dps.items():
                                if dpid == dp_mode or (dpid in (2, 21) and isinstance(val, int)):
                                    val_str = f"{val} ({mode_names.get(val, 'Unknown')})"
                                else:
                                    val_str = str(val)
                                dp_desc.append(f"DP{dpid}={val_str}")
                            self._emit("log", text=f"Отчёт лампы: {', '.join(dp_desc)}")

                            # Автоопределение DP для Tuya BLE ламп:
                            if 1 in dps and isinstance(dps[1], bool) and dp_switch == 20:
                                dp_switch = 1
                                self._emit("log", text="Автоопределение: DP питания скорректирован на 1")
                            if 2 in dps and isinstance(dps[2], int) and dp_mode == 21:
                                dp_mode = 2
                                self._emit("log", text="Автоопределение: DP режима скорректирован на 2")

# Авто-переопределение выключено, чтобы уважать ручное управление питанием и режимами

                        reply = session.make_response(code, rx_sn, payload)
                        if reply:
                            await self._send_chunks(ws, reply)
                        elif not dps:
                            self._emit("log", text=f"Пакет лампы 0x{code:04x}: {payload.hex()}")

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
            self._active_ws = None
            self._active_session = None
            if probe_task:
                probe_task.cancel()
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
        self.root.geometry("560x800")
        self.root.minsize(520, 720)
        self.root.configure(bg=self.BG_MAIN)

        self._apply_window_icon()

        self.cfg = load_config()
        self.ui_queue: queue.Queue = queue.Queue()
        self.controller = BridgeServerController(self.ui_queue, self.cfg)
        self.ips = get_local_ips()
        self.primary_ip = self.ips[0] if self.ips else "192.168.1.X"

        self._setup_clipboard_shortcuts()
        self._build_ui()
        self._poll_queue()

        if self._is_config_valid(silent=True):
            self.controller.start()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

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
