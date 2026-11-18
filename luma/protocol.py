import colorsys
import hashlib
import os
import struct
import time

from Crypto.Cipher import AES

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
        self.dp_writes = 0  # отправлено DP-команд
        self.dp_acks = 0  # подтверждений от лампы
        self.last_dp_write = 0.0
        self.awaiting_ack = False
        self.fast_active = False  # крупные BLE-пакеты (по MTU) включены
        self._fast_writes0 = 0
        self._fast_acks0 = 0

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
        self.dp_writes = 0  # отправлено DP-команд
        self.dp_acks = 0  # подтверждений от лампы
        self.last_dp_write = 0.0
        self.awaiting_ack = False
        self.fast_active = False  # крупные BLE-пакеты (по MTU) включены
        self._fast_writes0 = 0
        self._fast_acks0 = 0
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

    def note_dp_ack(self):
        self.dp_acks += 1
        self.awaiting_ack = False

    def enable_fast_chunks(self, chunk: int):
        """После Pair шлём кадры крупными BLE-пакетами (по MTU): 1 запись вместо 4–5."""
        if chunk > 20:
            self.chunk_size = chunk
            self.fast_active = True
            self._fast_writes0 = self.dp_writes
            self._fast_acks0 = self.dp_acks

    def check_fast_fallback(self) -> bool:
        """Если лампа не подтверждает крупные пакеты — вернуть безопасный размер 20 Б."""
        if (
            self.fast_active
            and self.dp_writes - self._fast_writes0 >= 6
            and self.dp_acks == self._fast_acks0
        ):
            self.fast_active = False
            self.chunk_size = 20
            return True
        return False

    def should_wait_for_ack(self) -> bool:
        """Не копим очередь в BLE: пока нет подтверждения прошлого цвета (до 120 мс) — ждём."""
        return (
            self.dp_acks > 0
            and self.awaiting_ack
            and (time.monotonic() - self.last_dp_write) < 0.12
        )

    def _wrap_dp_command(self, dp_packet: bytes) -> list[bytes]:
        self.dp_writes += 1
        self.last_dp_write = time.monotonic()
        self.awaiting_ack = True
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
        self,
        dp_id: int,
        r: int,
        g: int,
        b: int,
        brightness_scale: float = 1.0,
        include_bright_dp: bool = False,
    ) -> list[bytes]:
        h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        # Усиление насыщенности для сочного Ambilight на светодиодах
        if s > 0.01:
            s = min(1.0, s * 2.2 + 0.12)
        h_int = int(round(h * 360)) % 360
        s_int = int(round(s * 1000))
        scaled_v = v * max(0.05, min(1.0, brightness_scale))
        v_int = int(round(scaled_v * 1000))
        hsv_ascii = f"{h_int:04x}{s_int:04x}{v_int:04x}".encode("ascii")

        # Tuya Dreamlight (RGBIC) real-time paint color format (DP 61):
        dp61_raw = struct.pack(">BBBBBHHH", 0, 1, 0, 20, 0, h_int, s_int, v_int)

        items = []
        if include_bright_dp:
            bright_val = int(round(1000 * max(0.05, min(1.0, brightness_scale))))
            items.append((47, 0x02, struct.pack(">I", bright_val)))
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
