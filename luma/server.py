import asyncio
import json
import queue
import socket
import struct
import threading
import time

from aiohttp import web, WSMsgType

from .config import DISCOVERY_PORT, WS_PORT
from .protocol import (
    CODE_DEVICE_INFO,
    CODE_DPS,
    CODE_DPS_V4,
    CODE_PAIR,
    LightBridgeSession,
    parse_adv_protocol,
)
from .capture import capture_screen_rgb, capture_screen_zones, pop_capture_stats
from .smoothing import smooth_alpha

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
        last_smooth_t = time.monotonic()
        next_stats = time.monotonic() + 10.0
        last_sent_rgb = (-100, -100, -100)
        last_sent_brightness = -1.0
        last_sent_mode = ""
        first_frame = True
        last_bright_dp_time = 0.0
        last_ui_emit_time = 0.0
        last_ui_disp_rgb = (-1, -1, -1)
        zone_smooth: list[list[float]] = []
        last_zone_sent: list[tuple[int, int, int]] = []
        last_seg_time = 0.0

        while not ws.closed and session.paired and not self._stop_event.is_set():
            t0 = time.monotonic()
            if time.monotonic() < self.pause_ambilight_until:
                first_frame = True
                await asyncio.sleep(0.15)
                continue
            if session.check_fast_fallback():
                self._emit("log", text="Лампа не подтверждает крупные пакеты — возвращаю размер 20 Б")
                first_frame = True
            snap = self.get_cfg_snapshot()
            fps = max(1, min(30, int(snap.get("fps", 14))))
            brightness = float(snap.get("brightness", 100)) / 100.0
            smoothing = max(0.0, min(0.90, float(snap.get("smoothing", 45)) / 100.0))
            mode = snap.get("capture_mode", "fullscreen")
            dp_colour = int(snap.get("dp_colour", 24))

            try:
                raw_r, raw_g, raw_b = capture_screen_rgb(mode)
            except Exception:
                raw_r, raw_g, raw_b = 0, 0, 0

            if time.monotonic() >= next_stats:
                next_stats = time.monotonic() + 10.0
                cs = pop_capture_stats()
                self._emit(
                    "log",
                    text=(
                        f"Захват экрана за 10 с: {cs['n']} снимков, среднее {cs['avg_ms']:.2f} мс, "
                        f"макс {cs['max_ms']:.2f} мс; пропущено: {cs['skip_drag']} при перетаскивании окон, "
                        f"{cs['skip_busy']} из-за нагрузки"
                    ),
                )

            if not initialized:
                smooth_r, smooth_g, smooth_b = float(raw_r), float(raw_g), float(raw_b)
                initialized = True
            else:
                now_s = time.monotonic()
                diff = abs(raw_r - smooth_r) + abs(raw_g - smooth_g) + abs(raw_b - smooth_b)
                alpha = smooth_alpha(smoothing, now_s - last_smooth_t, diff)
                smooth_r = smooth_r * alpha + raw_r * (1.0 - alpha)
                smooth_g = smooth_g * alpha + raw_g * (1.0 - alpha)
                smooth_b = smooth_b * alpha + raw_b * (1.0 - alpha)

            last_smooth_t = time.monotonic()
            r, g, b = int(round(smooth_r)), int(round(smooth_g)), int(round(smooth_b))

            # Визуальный индикатор в интерфейсе с учётом выставленной яркости (частота UI до 12 FPS)
            disp_r = int(round(r * max(0.12, brightness)))
            disp_g = int(round(g * max(0.12, brightness)))
            disp_b = int(round(b * max(0.12, brightness)))
            now = time.monotonic()
            if (now - last_ui_emit_time >= 0.08) or ((disp_r, disp_g, disp_b) != last_ui_disp_rgb):
                last_ui_emit_time = now
                last_ui_disp_rgb = (disp_r, disp_g, disp_b)
                self._emit(
                    "color",
                    rgb=(disp_r, disp_g, disp_b),
                    raw_rgb=(r, g, b),
                    brightness=int(round(brightness * 100)),
                )

            color_diff = (
                abs(r - last_sent_rgb[0])
                + abs(g - last_sent_rgb[1])
                + abs(b - last_sent_rgb[2])
            )
            brightness_changed = abs(brightness - last_sent_brightness) >= 0.005
            mode_changed = mode != last_sent_mode
            min_diff = 1 if smoothing > 0.1 else 2

            should_send = (
                first_frame
                or brightness_changed
                or mode_changed
                or (color_diff >= min_diff)
            )
            segments = max(0, min(20, int(snap.get("segments", 0))))
            if segments >= 2 and not first_frame:
                should_send = False
                try:
                    zones = capture_screen_zones(segments, mode)
                except Exception:
                    zones = [(r, g, b)] * segments
                if len(zone_smooth) != segments:
                    zone_smooth = [[float(c) for c in z] for z in zones]
                    last_zone_sent = []
                else:
                    dtz = time.monotonic() - t0 + 1.0 / fps
                    for zs, z in zip(zone_smooth, zones):
                        dz = abs(z[0] - zs[0]) + abs(z[1] - zs[1]) + abs(z[2] - zs[2])
                        a = smooth_alpha(smoothing, dtz, dz)
                        for k in range(3):
                            zs[k] = zs[k] * a + z[k] * (1.0 - a)
                cur = [tuple(int(round(c)) for c in zs) for zs in zone_smooth]
                if len(last_zone_sent) != segments:
                    last_zone_sent = [(-100, -100, -100)] * segments
                # Лампе шлём только изменившиеся сегменты и не чаще ~4 раз/с: иначе она перегружается и рвёт связь
                changed_items = [
                    (i, c)
                    for i, (c, o) in enumerate(zip(cur, last_zone_sent))
                    if abs(c[0] - o[0]) + abs(c[1] - o[1]) + abs(c[2] - o[2]) >= 6
                ]
                if brightness_changed:
                    changed_items = list(enumerate(cur))
                now_m = time.monotonic()
                if changed_items and now_m - last_seg_time >= 0.25 and not session.should_wait_for_ack():
                    last_seg_time = now_m
                    for i, c in changed_items:
                        last_zone_sent[i] = c
                    last_sent_brightness = brightness
                    try:
                        await self._send_chunks(ws, session.make_segments(changed_items, brightness))
                    except Exception:
                        break
            elif should_send and not first_frame and session.should_wait_for_ack():
                should_send = False  # лампа ещё не приняла прошлый цвет — отправим свежий в следующем цикле

            if should_send:
                last_sent_rgb = (r, g, b)
                last_sent_brightness = brightness
                last_sent_mode = mode

                try:
                    await ws.send_str(
                        json.dumps(
                            {"rgb": [r, g, b], "brightness": int(round(brightness * 100))}
                        )
                    )
                    if first_frame:
                        first_frame = False
                        dp_switch = int(snap.get("dp_switch", 1))
                        dp_mode = int(snap.get("dp_mode", 2))
                        chunks = session.make_color_bundle(
                            dp_switch, dp_mode, dp_colour, r, g, b, brightness
                        )
                    else:
                        chunks = session.make_dp_color_hsv(
                            dp_colour,
                            r,
                            g,
                            b,
                            brightness,
                            include_bright_dp=True,
                        )
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
        mtu = 23
        reports_logged = 0
        dp_colour_cfg = int(snap.get("dp_colour", 24))
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
                        if session.note_tx_err():
                            self._emit("log", text="Ошибка отправки крупного пакета BLE — переключаюсь на размер 20 Б")
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
                    if not session.paired:  # после Pair поток идёт десятки раз в секунду — не засоряем UI
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
                            session.enable_fast_chunks(max(20, min(240, mtu - 3)))
                            if session.fast_active:
                                self._emit("log", text=f"Крупные BLE-пакеты: {session.chunk_size} Б (MTU {mtu})")
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
                        session.note_dp_ack()
                        if payload and payload[-1] != 0x00:
                            self._emit("log", text=f"Лампа отклонила DP-команду (ответ {payload.hex()})")
                        elif not dp_ack_logged:
                            dp_ack_logged = True
                            self._emit("log", text="Лампа приняла DP-команду")

                    else:
                        session.note_dp_ack()
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
                            only_colour = set(dps) <= {dp_colour_cfg, 61}
                            if reports_logged < 25 or not only_colour:
                                reports_logged += 1
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
