import asyncio
import colorsys
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import sys
import time
import traceback

from aiohttp import web, WSMsgType
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
import mss

# Стандартные UUID сервисов и характеристик Tuya BLE
TUYA_SERVICE_PRIMARY = "00001910-0000-1000-8000-00805f9b34fb"
TUYA_SERVICE_ALT = "0000a201-0000-1000-8000-00805f9b34fb"
TUYA_CHAR_NOTIFY = "00002b10-0000-1000-8000-00805f9b34fb"
TUYA_CHAR_WRITE = "00002b11-0000-1000-8000-00805f9b34fb"

DEFAULT_CONFIG = {
    "device_id": "",
    "local_key": "",
    "device_uuid": "",
    "host": "0.0.0.0",
    "port": 8000,
    "protocol_version": 3,
    "dp_switch": 20,
    "dp_colour": 24,
    "fps": 12,
    "brightness_scale": 1.0,
}


def get_app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


CONFIG_PATH = get_app_dir() / "config.json"


def load_or_create_config() -> dict:
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_PATH.exists():
        try:
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg.update(loaded)
        except Exception as e:
            print(f"[!] Не удалось прочитать {CONFIG_PATH.name}: {e}")

    need_setup = (
        not cfg.get("device_id")
        or len(cfg.get("local_key", "")) != 16
        or len(cfg.get("device_uuid", "")) != 16
    )

    if need_setup:
        print("=" * 52)
        print("  ПЕРВАЯ НАСТРОЙКА TUYA BLE AMBILIGHT BRIDGE")
        print("=" * 52)
        print("Введите параметры вашей лампы из Tuya IoT Platform.")
        print(f"Они сохранятся в файл: {CONFIG_PATH}\n")

        while True:
            dev_id = input("1. Device ID (например bfa4c7cif96mu7vy): ").strip()
            if dev_id:
                cfg["device_id"] = dev_id
                break

        while True:
            l_key = input("2. Local Key (ровно 16 символов): ").strip()
            if len(l_key) == 16:
                cfg["local_key"] = l_key
                break
            print(f"   Ошибка: длина {len(l_key)}, нужно ровно 16 символов.")

        while True:
            d_uuid = input("3. Device UUID (ровно 16 символов): ").strip()
            if len(d_uuid) == 16:
                cfg["device_uuid"] = d_uuid
                break
            print(f"   Ошибка: длина {len(d_uuid)}, нужно ровно 16 символов.")

        CONFIG_PATH.write_text(
            json.dumps(cfg, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"\n[OK] Настройки сохранены в {CONFIG_PATH.name}\n")

    return cfg


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
            if not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass
    return sorted(ips)


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


class TuyaBleSession:
    def __init__(self, cfg: dict):
        self.device_id = cfg["device_id"].encode("utf-8")[:16]
        self.local_key = cfg["local_key"].encode("utf-8")
        self.uuid = cfg["device_uuid"].encode("utf-8")[:16]
        self.protocol_version = int(cfg.get("protocol_version", 3))
        self.brightness_scale = float(cfg.get("brightness_scale", 1.0))

        self.login_key = hashlib.md5(self.local_key[:6]).digest()
        self.session_key: bytes | None = None
        self.sn = 1
        self.paired = False

        self._rx_buf = bytearray()
        self._rx_expected_len = 0

    def reset(self):
        self.session_key = None
        self.sn = 1
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

        if use_session and self.session_key:
            sec_flag = 0x04
            key = self.session_key
        else:
            sec_flag = 0x01
            key = self.login_key

        iv = os.urandom(16)
        cipher = AES.new(key, AES.MODE_CBC, iv)
        encrypted = bytes([sec_flag]) + iv + cipher.encrypt(pad(raw_with_crc, 16))

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
        pack_num, offset = unpack_varint(chunk, 0)
        if pack_num == 0:
            self._rx_buf.clear()
            self._rx_expected_len, offset = unpack_varint(chunk, offset)
            _proto_ver = chunk[offset]
            offset += 1

        self._rx_buf.extend(chunk[offset:])
        if self._rx_expected_len == 0 or len(self._rx_buf) < self._rx_expected_len:
            return None

        encrypted = bytes(self._rx_buf[: self._rx_expected_len])
        self._rx_buf.clear()
        self._rx_expected_len = 0

        sec_flag = encrypted[0]
        iv = encrypted[1:17]
        ciphertext = encrypted[17:]
        key = self.session_key if (sec_flag == 0x04 and self.session_key) else self.login_key

        cipher = AES.new(key, AES.MODE_CBC, iv)
        decrypted = unpad(cipher.decrypt(ciphertext), 16)

        sn, ack_sn, code, data_len = struct.unpack(">IIHH", decrypted[:12])
        payload = decrypted[12 : 12 + data_len]
        return sn, code, payload

    def make_device_info_req(self) -> list[bytes]:
        return self._build_encrypted_frame(code=0x0000, data=b"", use_session=False)

    def handle_device_info_resp(self, payload: bytes) -> list[bytes]:
        srand = payload[6:12]
        self.session_key = hashlib.md5(self.local_key[:6] + srand).digest()
        pair_payload = self.uuid + self.local_key[:6] + self.device_id.ljust(16, b"\x00")
        return self._build_encrypted_frame(code=0x0001, data=pair_payload, use_session=True)

    def make_dp_bool(self, dp_id: int, value: bool) -> list[bytes]:
        dp_packet = struct.pack(">BBH", dp_id, 0x01, 1) + (b"\x01" if value else b"\x00")
        return self._build_encrypted_frame(code=0x0002, data=dp_packet, use_session=True)

    def make_dp_color_hsv(self, dp_id: int, r: int, g: int, b: int) -> list[bytes]:
        h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        h_int = int(round(h * 360)) % 360
        s_int = int(round(s * 1000))
        v_int = int(round(v * 1000 * self.brightness_scale))
        hsv_ascii = f"{h_int:04x}{s_int:04x}{v_int:04x}".encode("ascii")
        dp_packet = struct.pack(">BBH", dp_id, 0x03, len(hsv_ascii)) + hsv_ascii
        return self._build_encrypted_frame(code=0x0002, data=dp_packet, use_session=True)


def capture_average_rgb() -> tuple[int, int, int]:
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        img = sct.grab(monitor)
        raw = img.rgb
        step = 16 * 3
        r_sum = sum(raw[0::step])
        g_sum = sum(raw[1::step])
        b_sum = sum(raw[2::step])
        count = max(1, len(raw[0::step]))
        return r_sum // count, g_sum // count, b_sum // count


async def send_chunks(ws: web.WebSocketResponse, chunks: list[bytes]):
    for chunk in chunks:
        await ws.send_bytes(chunk)


async def ambilight_loop(ws: web.WebSocketResponse, session: TuyaBleSession, cfg: dict):
    last_rgb = (-1, -1, -1)
    fps = max(1, int(cfg.get("fps", 12)))
    dp_colour = int(cfg.get("dp_colour", 24))
    interval = 1.0 / fps

    while not ws.closed and session.paired:
        t0 = time.monotonic()
        r, g, b = await asyncio.to_thread(capture_average_rgb)

        if abs(r - last_rgb[0]) + abs(g - last_rgb[1]) + abs(b - last_rgb[2]) > 8:
            last_rgb = (r, g, b)
            await ws.send_str(json.dumps({"rgb": [r, g, b]}))
            chunks = session.make_dp_color_hsv(dp_colour, r, g, b)
            await send_chunks(ws, chunks)

        elapsed = time.monotonic() - t0
        await asyncio.sleep(max(0.01, interval - elapsed))


def make_ws_handler(cfg: dict):
    dp_switch = int(cfg.get("dp_switch", 20))

    async def ws_handler(request: web.Request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        session = TuyaBleSession(cfg)
        stream_task: asyncio.Task | None = None
        print("[WS] Смартфон подключился!")

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data.get("type") == "ble_connected":
                        session.reset()
                        print("[BLE] Запуск рукопожатия Device Info...")
                        await send_chunks(ws, session.make_device_info_req())

                    elif data.get("type") == "ble_disconnected":
                        session.reset()
                        if stream_task:
                            stream_task.cancel()

                elif msg.type == WSMsgType.BINARY:
                    parsed = session.feed_ble_chunk(msg.data)
                    if not parsed:
                        continue

                    sn, code, payload = parsed
                    print(f"[BLE RX] sn={sn}, code=0x{code:04x}, len={len(payload)}")

                    if code == 0x0000:
                        await ws.send_str(json.dumps({"log": "Получен srand, отправляем Pair..."}))
                        await send_chunks(ws, session.handle_device_info_resp(payload))

                    elif code == 0x0001:
                        if payload and payload[0] == 0x00:
                            session.paired = True
                            print("[BLE] Успешная авторизация Tuya BLE! Ambilight запущен.")
                            await ws.send_str(json.dumps({
                                "status": "Ambilight активен!",
                                "log": "Авторизация пройдена. Включаем свет..."
                            }))
                            await send_chunks(ws, session.make_dp_bool(dp_switch, True))
                            await asyncio.sleep(0.15)
                            if stream_task:
                                stream_task.cancel()
                            stream_task = asyncio.create_task(ambilight_loop(ws, session, cfg))
                        else:
                            err_msg = f"Ошибка Pair (код {payload.hex()})"
                            print("[BLE]", err_msg)
                            await ws.send_str(json.dumps({"log": err_msg}))

        finally:
            if stream_task:
                stream_task.cancel()
            print("[WS] Смартфон отключился")

        return ws

    return ws_handler


def main():
    cfg = load_or_create_config()
    host = cfg.get("host", "0.0.0.0")
    port = int(cfg.get("port", 8000))

    app = web.Application()
    app.router.add_get("/ws", make_ws_handler(cfg))

    ips = get_local_ips()
    print("=" * 52)
    print("  TUYA BLE AMBILIGHT СЕРВЕР ЗАПУЩЕН")
    print("=" * 52)
    if ips:
        for ip in ips:
            print(f"  -> Введите в приложении на телефоне IP: {ip}")
    else:
        print("  -> Узнайте IP ПК через ipconfig и введите в приложении")
    print(f"  Порт: {port} | Конфиг: {CONFIG_PATH.name}")
    print("  Для остановки закройте окно или нажмите Ctrl+C.")
    print("=" * 52 + "\n")

    web.run_app(app, host=host, port=port, print=None)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nСервер остановлен.")
    except Exception:
        traceback.print_exc()
        input("\nПроизошла ошибка. Нажмите Enter для выхода...")
