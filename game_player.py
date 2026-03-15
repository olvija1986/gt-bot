"""
game_player.py — Авто-игрок Gatto (Race / Swim)

Реальный протокол (из DevTools):

=== ШАГ 1: Комната ожидания ===
  WS: wss://waitroom.nl.gatto.pw/socket.io/?EIO=4&transport=websocket
  → 40{"token":"Bearer ..."}
  → 42["x-info","Amsterdam"]
  → 420["waitroom:connect", {"petId":"...", "gameType":"race"}]
  ← 430[{"success":true}]
  ← 42["waitroom.list.update", [...]]   (обновления списка)
  ← 42["waitroom.game.new", {"game":{id, lobbyUrl, ...}, "pets":[...]}]
  Сразу после → закрываем waitroom WS

=== ШАГ 2: Игра ===
  WS: {game.lobbyUrl}/socket.io/?EIO=4&transport=websocket
  → 40{"token":"Bearer ..."}
  → 42["x-info","Amsterdam"]
  → 420["game.connect", {"gameId":"...", "screenResolution":{"w":1182,"h":468}}]
  ← 42["engine.user.connected", ...]
  ← 42["engine.sync", {..., extra:{mapInfo:{barriers:[...]}}}]
  ← 42["engine.game.started", ...]
  → 42["engine.jump", {...}]            (когда нужно)
  ← 42["engine.game.ended", {usersPrizes:[...]}]
"""

import os
import time
import json
import logging
import threading
import requests
import websocket   # pip install websocket-client

logger = logging.getLogger("game_player")
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)

# ── конфиг ──────────────────────────────────────────────
TG_TOKEN       = os.environ.get("TG_TOKEN", "")
API_BASE       = "https://api.nl.gatto.pw"
WAITROOM_WS    = "wss://waitroom.nl.gatto.pw/socket.io/?EIO=4&transport=websocket"
SCREEN         = {"w": 1182, "h": 468}
WAITROOM_TIMEOUT = 60    # сек ждём матч
GAME_TIMEOUT     = 120   # сек максимум на игру
# ────────────────────────────────────────────────────────

HEADERS_HTTP = {
    "accept": "application/json, text/plain, */*",
    "authorization": f"Bearer {TG_TOKEN}",
    "content-type": "application/json",
    "referer": "https://gatto.pw/",
    "user-agent": "Mozilla/5.0",
}


# ══════════════════════════════════════════════════════════
#  Физика прыжка (порт physics.ts)
# ══════════════════════════════════════════════════════════
def validate_token() -> bool:
    """Проверяет что TG_TOKEN рабочий через обычный HTTP запрос."""
    try:
        r = requests.post(
            f"{API_BASE}/user.getSelf",
            headers=HEADERS_HTTP,
            json={},
            timeout=10
        )
        logger.info(f"[token check] status={r.status_code} response={r.text[:100]}")
        return r.status_code == 200
    except Exception as e:
        logger.error(f"[token check] error: {e}")
        return False


def ticks_to_reach_height(jump_power: float, gravity: float, target_height: float) -> int:
    """Порт physics.ts::ticksToReachHeight — для прыжка (race)."""
    y, speed_y = 0.0, jump_power
    for tick in range(1, 200):
        speed_y -= 0.6
        if speed_y < 0:
            speed_y = 0.0
        y += speed_y
        if y - gravity > 0:
            y -= gravity
        else:
            return tick
        if y >= target_height:
            return tick
    return 200


def ticks_to_reach_depth(dive_power: float, target_depth: float) -> int:
    """Порт physics.ts::ticksToReachDepth — для нырка (swim)."""
    depth, speed_y = 0.0, dive_power
    for tick in range(1, 200):
        speed_y -= 0.3
        if speed_y < 0:
            speed_y = 0.0
        depth += speed_y
        if depth >= target_depth:
            return tick
    return 200


# ══════════════════════════════════════════════════════════
#  Socket.IO helpers
# ══════════════════════════════════════════════════════════
def sio_pack(event: str, data) -> str:
    """42["event", data]"""
    return "42" + json.dumps([event, data], ensure_ascii=False, separators=(',', ':'))


def sio_parse(message: str):
    """
    Парсит Socket.IO фрейм.
    Возвращает (event, data) или (None, None).
    """
    if not message.startswith("42"):
        return None, None
    # убираем возможный ack-суффикс: "420[..." → "42[..."
    body = message[2:]
    if body and body[0].isdigit():
        # это ack-пакет типа "420[..." — убираем цифры после "42"
        i = 0
        while i < len(body) and body[i].isdigit():
            i += 1
        body = body[i:]
    try:
        payload = json.loads(body)
        if isinstance(payload, list) and payload:
            return payload[0], payload[1] if len(payload) > 1 else {}
    except Exception:
        pass
    return None, None


class SioClient:
    """
    Минимальная обёртка над websocket-client с поддержкой Engine.IO.

    Правильный порядок handshake (из реального трафика):
      <- 0{sid,pingInterval,...}   Engine.IO open
      -> 40{token:...}             Socket.IO namespace connect с авторизацией
      -> 42["x-info","Amsterdam"]  доп. инфо
      <- 40{sid}                   Socket.IO namespace ack
      -> 420["command", ...]       теперь можно слать команды
    """

    def __init__(self, url: str, token: str):
        self.url    = url
        self.token  = token[7:] if token.lower().startswith("bearer ") else token
        self._ws    = None
        self._ack   = 0
        self._done  = threading.Event()
        self._handlers = {}
        self._eio_open_received = False
        self._sio_ack_received  = False

    def on(self, event: str, fn):
        self._handlers[event] = fn

    def emit(self, event: str, data):
        self._ws_send(sio_pack(event, data))

    def emit_ack(self, event: str, data):
        n = 420 + self._ack
        self._ack += 1
        self._ws_send(str(n) + json.dumps([event, data], ensure_ascii=False, separators=(',', ':')))

    def emit_with_null(self, event: str, data):
        """42["event", data, null] — формат из реального клиентского трафика."""
        raw = "42" + json.dumps([event, data, None], ensure_ascii=False, separators=(',', ':'))
        self._ws_send(raw)

    def _ws_send(self, raw: str):
        try:
            if self._ws:
                self._ws.send(raw)
                import logging as _l
                _l.getLogger("game_player").debug(f"[SioClient] -> {raw[:120]}")
        except Exception as e:
            import logging as _l
            _l.getLogger("game_player").warning(f"[SioClient] send error: {e}")

    def _on_open(self, ws):
        # TCP соединение установлено — ждём 0{sid} от сервера, ничего не шлём
        import logging as _l
        _l.getLogger("game_player").info(f"[SioClient] TCP open: {self.url}")

    def _on_message(self, ws, msg: str):
        import logging as _l
        log = _l.getLogger("game_player")
        log.info(f"[SioClient] <- {msg[:200]}")

        if msg == "2":
            self._ws_send("3")
            return

        # Engine.IO open: 0{sid, pingInterval, ...}
        if msg.startswith("0") and not self._eio_open_received:
            self._eio_open_received = True
            log.info(f"[SioClient] EIO open received. Token starts with: {self.token[:60]!r}")
            auth = f'40{{"token":"Bearer {self.token}"}}' 
            log.info(f"[SioClient] Sending auth: {auth[:100]}")
            self._ws_send(auth)
            # x-info шлём отдельно, после небольшой паузы
            import time as _t
            _t.sleep(0.1)
            self.emit("x-info", "Amsterdam")
            return

        # Socket.IO namespace ack: 40 или 40{sid}
        if msg.startswith("40"):
            if not self._sio_ack_received:
                self._sio_ack_received = True
                log.info("[SioClient] SIO namespace ack — ready, calling _open handler")
                if self._handlers.get("_open"):
                    self._handlers["_open"]()
            return

        # Обычные события: 42["event", data]
        event, data = sio_parse(msg)
        if event:
            log.info(f"[SioClient] event: {event}")
            if event in self._handlers:
                self._handlers[event](data)

    def _on_error(self, ws, err):
        import logging as _l
        _l.getLogger("game_player").error(f"[SioClient] WS error: {err!r}")

    def _on_close(self, ws, code, msg):
        import logging as _l
        _l.getLogger("game_player").warning(f"[SioClient] WS closed: code={code!r} msg={msg!r}")
        self._done.set()

    def connect(self):
        self._ws = websocket.WebSocketApp(
            self.url,
            on_open    = self._on_open,
            on_message = self._on_message,
            on_error   = self._on_error,
            on_close   = self._on_close,
        )
        t = threading.Thread(
            target=lambda: self._ws.run_forever(ping_interval=0),
            daemon=True,
        )
        t.start()
        return t

    def disconnect(self):
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    def wait(self, timeout=None) -> bool:
        return self._done.wait(timeout=timeout)


# ══════════════════════════════════════════════════════════
#  ШАГ 1: Waitroom — ждём матч
# ══════════════════════════════════════════════════════════
class WaitroomSession:
    """
    Подключается к waitroom, отправляет petId + gameType,
    ждёт waitroom.game.new и возвращает gameId + lobbyUrl.
    """

    def __init__(self, pet_id: str, game_type: str = "race"):
        self.pet_id    = pet_id
        self.game_type = game_type
        self._result   = None
        self._done     = threading.Event()

    def wait_for_game(self, timeout: int = WAITROOM_TIMEOUT):
        """
        Возвращает dict {"game_id": str, "lobby_url": str} или None при таймауте.
        """
        client = SioClient(WAITROOM_WS, TG_TOKEN)

        def on_open():
            payload = {"petId": self.pet_id, "gameType": self.game_type}
            logger.info(f"[waitroom] → waitroom:connect {payload}")
            client.emit_ack("waitroom:connect", payload)

        def on_game_new(data: dict):
            game = data.get("game", {})
            game_id  = game.get("id") or game.get("_id")
            lobby_url = game.get("lobbyUrl", "")
            logger.info(f"[waitroom] game.new: id={game_id} lobbyUrl={lobby_url}")
            if not lobby_url.endswith("/socket.io/?EIO=4&transport=websocket"):
                if not lobby_url.endswith("/"):
                    lobby_url += "/"
                lobby_url += "socket.io/?EIO=4&transport=websocket"
            logger.info(f"[waitroom] full WS URL: {lobby_url}")
            self._result = {"game_id": str(game_id), "lobby_url": lobby_url}
            self._done.set()

        def on_list_update(data):
            count = len(data) if isinstance(data, list) else "?"
            logger.info(f"[waitroom] list.update: {count} игроков: {[u.get('userId') for u in data] if isinstance(data, list) else data}")

        client.on("_open",                  on_open)
        client.on("waitroom.game.new",      on_game_new)
        client.on("waitroom.list.update",   on_list_update)

        ws_thread = client.connect()
        self._done.wait(timeout=timeout)
        client.disconnect()
        ws_thread.join(timeout=5)

        return self._result


# ══════════════════════════════════════════════════════════
#  ШАГ 2: GameSession — играем
# ══════════════════════════════════════════════════════════
# Таблица скоростей: (agility, px/ms) — измерено из реального трафика
_SPEED_TABLE = [
    (10,  0.165),
    (53,  0.261),
    (77,  0.415),
    (100, 0.540),  # экстраполяция
]

def _agility_to_speed_per_ms(agility: int) -> float:
    """Интерполирует скорость px/ms по agility из таблицы реальных измерений."""
    xs = [x for x, _ in _SPEED_TABLE]
    ys = [y for _, y in _SPEED_TABLE]
    if agility <= xs[0]:
        return ys[0]
    if agility >= xs[-1]:
        return ys[-1]
    for i in range(len(xs) - 1):
        if xs[i] <= agility <= xs[i+1]:
            t = (agility - xs[i]) / (xs[i+1] - xs[i])
            return ys[i] + t * (ys[i+1] - ys[i])
    return ys[-1]


def _simulate_landing(start_x: float, speed_x: float, jump_power: float, gravity: float = 1.5) -> float:
    """Симулирует физику прыжка и возвращает x где пет приземлится."""
    y, sy = 0.0, jump_power
    x = float(start_x)
    for _ in range(500):
        sy -= 0.6
        if sy < 0:
            sy = 0.0
        y += sy
        if y - gravity > 0:
            y -= gravity
        else:
            y = max(0.0, y - gravity)
            if y <= 0:
                return x
        x += speed_x
    return x


class GameSession:
    def __init__(self, game_id: str, lobby_url: str, mode: str, user_id: int, on_finish=None, pet_id=None):
        self.game_id   = game_id
        self.lobby_url = lobby_url
        self.mode      = mode
        self.user_id   = user_id
        self.on_finish = on_finish

        # Стейт пета
        self.pet_id        = pet_id  # передаётся из AutoPlayer, уточняется из engine.user.connected
        self.pet_row       = None
        self.pet_x         = 118.0   # стартовая позиция
        self.pet_y         = 0.0
        self.pet_status    = "running"
        # currentSpeed — обновляется из engine.sync и engine.jump событий
        self.current_speed_x  = 0.0   # px/tick (не px/ms!)
        self.current_speed_y  = 0.0
        # Физические параметры пета — берём из engine.user.connected
        self.jump_power    = 20.0    # дефолт, перезапишется из pet.info
        self.dive_power    = 10.0    # для swim
        self.gravity       = 1.5
        self.width_pet     = 40
        self.width_barrier = 30      # raceBarrier / poolBarrier
        # Клик координата из реального трафика
        self.click_x       = 673.9921875
        self.click_y       = 346.53125

        self.barriers          = []
        self._all_barriers_raw = []
        self.last_update   = 0
        self.started       = False
        self.game_started_at = None
        # lag_px: насколько наша оценка позиции отстаёт от серверной (px).
        # Калибруется после каждого подтверждённого прыжка.
        # Начальное значение 60px — из реальных логов (было ~68px при agi=10).
        # После первого прыжка обновится до реального значения.
        self.lag_px             = 0.0
        self._lag_samples       = []
        self.physics_start_at   = 0.0   # serverTime из engine.game.started
        self.server_time_offset = 0.0   # local → server time offset
        self._jump_timers       = []    # список Timer объектов
        self._last_jumped_barrier = 0.0  # x барьера для которого уже запланирован/выполнен прыжок
        self._last_sent_jumped_at = 0.0   # jumpedAt который мы отправили последним
        self._prev_confirmed_x   = 0.0   # x предыдущего подтверждённого прыжка
        self._prev_confirmed_at  = 0.0   # jumpedAt предыдущего подтверждённого прыжка
        self._prev_target_x      = 0.0   # target_x предыдущего запланированного прыжка
        self.result             = None
        self._done         = threading.Event()

    def _running_speed_from_jump(self, confirmed_x: float, jumped_at: int) -> float:
        """
        Вычисляет скорость бега по дельте target_x между соседними прыжками.

        confirmed_x — позиция в середине дуги, не подходит для скорости.
        target_x предыдущего и текущего прыжков — оба на земле → точная дельта.
        """
        use_at = self._last_sent_jumped_at if self._last_sent_jumped_at > 0 else jumped_at

        # Дельта target_x / delta_time между соседними прыжками
        if self._prev_target_x > 0 and self._prev_confirmed_at > 0:
            # _prev_target_x — где пет был при предыдущем прыжке (на земле)
            # сейчас пет тоже на земле в районе текущего target_x
            # Но мы не знаем текущий target_x здесь, используем confirmed_x как приближение
            # Лучше: используем delta_jumpedAt и prev_target_x как anchor
            dt = (use_at - self._prev_confirmed_at) / 10.0
            if dt > 5:  # минимум 5 тиков между прыжками
                # За dt тиков пет прошёл от prev_target_x до ~confirmed_x
                # Но confirmed_x неточен (дуга). Используем avg speed от prev_target_x
                # как лучшую оценку: скорость = (confirmed_x - prev_target_x + jump_dist) / dt
                # Упрощение: average speed over full interval
                dx = confirmed_x - self._prev_target_x
                if dx > 0:
                    speed = dx / dt
                    logger.info(f"run_speed delta: prev_x={self._prev_target_x:.0f} "
                               f"curr_x={confirmed_x:.0f} dt={dt:.0f}t → {speed:.4f}")
                    return speed

        # Первый прыжок — от старта
        if not self.physics_start_at or self.physics_start_at <= 0:
            return self.current_speed_x
        elapsed = (use_at - self.physics_start_at) / 10.0
        if elapsed <= 0:
            return self.current_speed_x
        return (confirmed_x - 118.0) / elapsed

    def _ai_loop(self):
        """
        Polling loop по образцу raceBot.ts (исходники сервера).
        Каждые 80ms проверяет дистанцию до барьера и прыгает если нужно.
        intelligence=1.0 → triggerDist = idealDist (идеальная реакция).
        """
        while not self._done.is_set():
            if self.started and self.barriers and self.current_speed_x > 0:
                if self.pet_status not in ("jumping", "diving"):
                    self._tick_bot()
            time.sleep(0.08)

    def _tick_bot(self):
        """Порт raceBot.tickBot — вызывается каждые 80ms."""
        # Обновляем оценку позиции (тик = 10ms)
        if self.game_started_at:
            elapsed_ms = time.time() * 1000 - self.game_started_at
            self.pet_x = 118.0 + self.current_speed_x * (elapsed_ms / 10.0)

        pet_front = self.pet_x + self.width_pet

        # findNextBarrier: первый барьер чья правая грань ещё впереди пета
        barrier = next(
            (b for b in self.barriers if b["x"] + self.width_barrier > pet_front),
            None
        )
        if not barrier:
            return

        dist = barrier["x"] - pet_front
        if dist <= 0:
            return

        # calcIdealJumpDistance: порт из raceBot.ts
        # idealDist = speed * (ticksToReachHeight + 2)
        barrier_high = barrier.get("high", 50)
        if self.mode == "race":
            ticks = ticks_to_reach_height(self.jump_power, self.gravity, barrier_high)
        else:
            ticks = ticks_to_reach_depth(self.dive_power, barrier_high)
        ideal_dist = self.current_speed_x * (ticks + 2)

        # Запас на polling lag (80ms) + сетевую задержку (~150ms) + погрешность позиции
        # При speed=2.0: 230ms * 2.0/10ms_per_tick = 46px движения до реального прыжка
        poll_lag_px = self.current_speed_x * 23  # 230ms / 10ms

        if dist <= ideal_dist + poll_lag_px:
            now_srv = int(time.time() * 1000 + self.server_time_offset)
            payload = {
                "clickPosition": {"x": self.click_x, "y": self.click_y},
                "jumpedAt": now_srv,
            }
            event = "engine.jump" if self.mode == "race" else "engine.dive"
            self._client.emit_with_null(event, payload)
            self.pet_status = "jumping"
            self._last_jumped_barrier = barrier["x"]
            self._last_sent_jumped_at = now_srv
            logger.info(
                f"[{self.game_id}] ⏱ JUMP barrier={barrier['x']} "
                f"dist={dist:.1f} ideal={ideal_dist:.1f} spx={self.current_speed_x:.3f}"
            )

    def _make_action_payload(self) -> dict:
        return {
            "clickPosition": {"x": self.click_x, "y": self.click_y},
            "jumpedAt":      int(time.time() * 1000),
        }

    def run(self, timeout: int = GAME_TIMEOUT):
        client = SioClient(self.lobby_url, TG_TOKEN)
        self._client = client

        def on_open():
            logger.info(f"[{self.game_id}] WS игры открыт, подключаемся… petId={self.pet_id}")
            payload = {
                "gameId":           self.game_id,
                "screenResolution": SCREEN,
            }
            if self.pet_id:
                payload["petId"] = self.pet_id
            client.emit_ack("game.connect", payload)

        def on_user_connected(data: dict):
            user = data.get("user", {})
            if user.get("_id") != self.user_id:
                return
            pet_wrap = data.get("pet", {})
            info = pet_wrap.get("info", {})
            self.pet_id  = str(info.get("_id", ""))
            self.pet_row = pet_wrap.get("row")

            # jumpPower и gravity из pet.info (если сервер их отдаёт)
            if "jumpPower" in info:
                self.jump_power = float(info["jumpPower"])
            if "divePower" in info:
                self.dive_power = float(info["divePower"])
            if "power" in info:
                self.gravity = float(info["power"].get("gravity", self.gravity))

            # Квадратичная формула скорости из реальных измерений:
            # (10→1.648, 53→2.610, 77→4.150 px/tick)
            # speed = 0.000624*agi^2 - 0.016927*agi + 1.754893
            agility = info.get("chars", {}).get("agility", 53)
            self.current_speed_x = (
                0.000624 * agility**2
                - 0.016927 * agility
                + 1.754893
            )
            self.current_speed_x = max(0.5, self.current_speed_x)

            logger.info(
                f"[{self.game_id}] Наш пет: {info.get('name')} "
                f"row={self.pet_row} agility={agility} "
                f"speed={self.current_speed_x:.3f}px/tick "
                f"(тик=10ms → {self.current_speed_x/10:.4f}px/ms) "
                f"jp={self.jump_power:.1f} g={self.gravity:.2f}"
            )
            # Применяем барьеры если они уже пришли до нашего connected
            if self._all_barriers_raw and self.pet_row and not self.barriers:
                self.barriers = sorted(
                    [b for b in self._all_barriers_raw if b.get("row") == self.pet_row],
                    key=lambda b: b["x"]
                )
                logger.info(f"[{self.game_id}] Барьеры применены после connected: "
                            f"{len(self.barriers)} на row={self.pet_row}")

        _sync_count = [0]

        def on_sync(data: dict):
            _sync_count[0] += 1
            # Дампим первые 2 синка полностью для диагностики
            if _sync_count[0] <= 2:
                users_preview = []
                for u in data.get("users", []):
                    users_preview.append({
                        "uid": u.get("user", {}).get("_id"),
                        "keys": list(u.keys()),
                        "coords": u.get("coordinates"),
                        "pet_keys": list(u.get("pet", {}).keys()) if u.get("pet") else [],
                    })
                logger.info(f"[{self.game_id}] SYNC#{_sync_count[0]} users={users_preview}")

            found = False
            for u in data.get("users", []):
                uid = u.get("user", {}).get("_id")
                if uid != self.user_id:
                    continue
                found = True
                coords = u.get("coordinates", {})
                if coords.get("x") is not None:
                    self.pet_x = coords["x"]
                if coords.get("y") is not None:
                    self.pet_y = coords["y"]

                pet_wrap = u.get("pet", {})
                if pet_wrap.get("row"):
                    self.pet_row = pet_wrap["row"]

                # currentSpeed из синка — это px/tick
                speed = u.get("speed") or pet_wrap.get("speed") or {}
                if speed.get("x") is not None:
                    self.current_speed_x = speed["x"]
                if speed.get("y") is not None:
                    self.current_speed_y = speed["y"]

                # jumpPower, gravity из pet.info в синке
                info = pet_wrap.get("info", {})
                if info.get("jumpPower"):
                    self.jump_power = float(info["jumpPower"])
                if info.get("power", {}).get("gravity"):
                    self.gravity = float(info["power"]["gravity"])

                status = u.get("status") or pet_wrap.get("status")
                if status:
                    self.pet_status = status
                break

            if not found and self.started:
                logger.warning(f"[{self.game_id}] Наш пет не найден в синке! users count={len(data.get('users',[]))}")

            self.last_update = data.get("lastUpdatedAt", self.last_update)
            


            # Барьеры и gameStartedAt из extra — приходят в первом синке
            extra = data.get("extra", {})
            map_info = extra.get("mapInfo", {})
            barriers_raw = map_info.get("barriers")

            # server_time_offset — вычисляем из каждого синка для точности
            server_time_sync = data.get("serverTime", 0)
            if server_time_sync:
                local_now_ms = time.time() * 1000
                self.server_time_offset = server_time_sync - local_now_ms

            if barriers_raw is not None:
                self._all_barriers_raw = barriers_raw
                if self.pet_row is not None and not self.barriers:
                    self.barriers = sorted(
                        [b for b in barriers_raw if b.get("row") == self.pet_row],
                        key=lambda b: b["x"]
                    )
                    logger.info(
                        f"[{self.game_id}] Карта: {len(self.barriers)} барьеров "
                        f"на row={self.pet_row}, первый x={self.barriers[0]['x'] if self.barriers else '—'}"
                    )

        def on_started(data: dict):
            self.last_update = data.get("lastUpdate", self.last_update)
            self.started     = True
            server_time      = data.get("serverTime", 0)
            local_now_ms     = time.time() * 1000
            self.server_time_offset = server_time - local_now_ms
            self.game_started_at    = local_now_ms

            # physics_start_at = serverTime из engine.game.started
            # Проверено из DevTools: jumpedAt - physics_start = elapsed тиков * 10ms
            self.physics_start_at = server_time

            logger.info(
                f"[{self.game_id}] 🏁 Игра! physics_start={server_time} "
                f"speed={self.current_speed_x:.4f}px/tick"
            )
            # AI loop запускается в run() — он сам найдёт барьеры

        def on_ended(data: dict):
            for p in data.get("usersPrizes", []):
                if p.get("userId") == self.user_id:
                    self.result = p
                    prize = p.get("prize", {})
                    extra = p.get("extraAwards", [])
                    logger.info(
                        f"[{self.game_id}] Место: {p.get('winningPlace','?')} | "
                        f"монет: {prize.get('moneyAmount',0)} | "
                        f"опыт: {prize.get('experience',0)} | "
                        f"extra: {len(extra)}"
                    )
            # Отменяем незапущенные таймеры
            for t in self._jump_timers:
                t.cancel()
            self._jump_timers.clear()
            self._done.set()
            if self.on_finish:
                self.on_finish(self.result)

        def on_dive(data: dict):
            """engine.dive от других игроков — обновляем наш статус если это мы."""
            if data.get("userId") == self.user_id:
                self.pet_status = "diving"
                coords = data.get("coordinates", {})
                self.pet_x = coords.get("x", self.pet_x)
                self.pet_y = coords.get("y", self.pet_y)
                speed = data.get("speed", {})
                if speed.get("x") is not None:
                    self.current_speed_x = speed["x"]
                if speed.get("y") is not None:
                    self.current_speed_y = speed["y"]
                self.last_update = data.get("lastUpdate", self.last_update)

        def on_emerge(data: dict):
            """engine.emerge — пет вынырнул."""
            if data.get("userId") == self.user_id:
                self.pet_status = "running"
                coords = data.get("coordinates", {})
                self.pet_x = coords.get("x", self.pet_x)
                self.pet_y = coords.get("y", self.pet_y)
                speed = data.get("speed", {})
                if speed.get("x") is not None:
                    self.current_speed_x = speed["x"]
                if speed.get("y") is not None:
                    self.current_speed_y = speed["y"]
                self.last_update = data.get("lastUpdate", self.last_update)

        def on_jump(data: dict):
            """engine.jump — сервер подтвердил прыжок, обновляем состояние."""
            if data.get("userId") != self.user_id:
                return

            self.pet_status = "jumping"
            coords = data.get("coordinates", {})
            real_x = coords.get("x")
            speed_data = data.get("speed", {}) or {}
            arc_spx = speed_data.get("x", 0)

            if real_x is not None:
                self.pet_x = real_x
                # Обновляем скорость из ответа сервера
                if arc_spx > 0.5:
                    self.current_speed_x = arc_spx
                # Калибруем game_started_at по реальной позиции
                if self.current_speed_x > 0:
                    ticks = (real_x - 118.0) / self.current_speed_x
                    self.game_started_at = time.time() * 1000 - ticks * 10
                logger.info(
                    f"[{self.game_id}] JUMP confirmed: x={real_x:.0f} spx={self.current_speed_x:.4f}"
                )

            self.last_update = data.get("lastUpdate", self.last_update)

            # Автосброс статуса — ai_loop сам обнаружит когда можно прыгать снова
            def reset_after_jump():
                time.sleep(1.2)
                if self.pet_status == "jumping":
                    self.pet_status = "running"
            threading.Thread(target=reset_after_jump, daemon=True).start()

        client.on("_open",                  on_open)
        client.on("engine.user.connected",  on_user_connected)
        client.on("engine.sync",            on_sync)
        client.on("engine.game.started",    on_started)
        client.on("engine.game.ended",      on_ended)
        client.on("engine.dive",            on_dive)
        client.on("engine.emerge",          on_emerge)
        client.on("engine.jump",            on_jump)

        # Запускаем polling loop (порт raceBot.ts)
        threading.Thread(target=self._ai_loop, daemon=True).start()

        ws_thread = client.connect()

        if not self._done.wait(timeout=timeout):
            logger.warning(f"[{self.game_id}] Таймаут {timeout}с")

        client.disconnect()
        ws_thread.join(timeout=5)


# ══════════════════════════════════════════════════════════
#  REST helpers
# ══════════════════════════════════════════════════════════
def _post(url: str, payload: dict):
    try:
        r = requests.post(url, headers=HEADERS_HTTP, json=payload, timeout=20)
        if r.status_code == 200:
            return r.json()
        logger.warning(f"POST {url} → {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logger.error(f"POST {url}: {e}")
    return None


def get_my_user_id() -> int | None:
    data = _post(f"{API_BASE}/user.getSelf", {})
    if data:
        uid = data.get("user", {}).get("_id")
        if uid:
            return int(uid)
    return None


def get_all_pets() -> list:
    """
    Возвращает лучшего пета для каждой эволюции.
    Сортировка: по эволюции (убыв.), внутри эволюции — лучший по нужной стате.
    """
    data = _post(f"{API_BASE}/user.getSelf", {})
    if not data:
        return []
    raw_pets = []
    try:
        regions = data.get("user", {}).get("regions", [])
        logger.info(f"get_all_pets: регионов={len(regions)}")
        for i, region in enumerate(regions):
            pet = region.get("pet")
            if pet and pet.get("_id"):
                chars = pet.get("chars", {})
                logger.info(
                    f"  [{i}] {pet.get('name')} | id={pet.get('_id')} | "
                    f"evo={pet.get('evolution')} lv={pet.get('level')} | "
                    f"agi={chars.get('agility',0)} swim={chars.get('swim',0)} "
                    f"str={chars.get('strength',0)}"
                )
                base = pet.get("basePet", {})
                chars = pet.get("chars", {})
                raw_pets.append({
                    "id":        str(pet["_id"]),
                    "name":      pet.get("name", "?"),
                    "region":    base.get("allowedRegion", "?"),
                    "kind":      base.get("kind", "?"),
                    "rarity":    base.get("rarity", "?"),
                    "level":     pet.get("level", 0),
                    "evolution": pet.get("evolution", 0),
                    "agility":   chars.get("agility", 0),
                    "swim":      chars.get("swim", 0),
                    "fly":       chars.get("fly", 0),
                    "stamina":   chars.get("stamina", 0),
                    "strength":  chars.get("strength", 0),
                })
    except Exception as e:
        logger.error(f"get_all_pets error: {e}")
    return raw_pets


def get_best_pets_by_evolution(mode: str) -> list:
    """
    Возвращает список лучших петов — по одному на каждую эволюцию.
    Внутри эволюции выбирает лучшего по статe режима.
    Сортировка итогового списка: эволюция по убыванию.
    """
    pets = get_all_pets()
    if not pets:
        return []

    stat_key = "swim" if mode == "swim" else "agility"

    # Группируем по эволюции
    by_evo = {}
    for p in pets:
        evo = p["evolution"]
        if evo not in by_evo:
            by_evo[evo] = []
        by_evo[evo].append(p)

    # Лучший в каждой эволюции
    result = []
    for evo in sorted(by_evo.keys(), reverse=True):
        group = by_evo[evo]
        best = max(group, key=lambda p: p[stat_key])
        result.append(best)

    return result


def get_best_pet_for_mode(mode: str) -> str | None:
    """Выбирает лучшего пета для режима (наивысшая эволюция + лучшая стата)."""
    pets = get_best_pets_by_evolution(mode)
    if not pets:
        return None
    best = pets[0]  # первый = наивысшая эволюция
    stat_key = "swim" if mode == "swim" else "agility"
    logger.info(f"Авто-выбор для {mode}: {best['name']} evo{best['evolution']} "
                f"lv{best['level']} {stat_key}={best[stat_key]}")
    return best["id"]


# ══════════════════════════════════════════════════════════
#  AutoPlayer — серия игр
# ══════════════════════════════════════════════════════════
class AutoPlayer:
    def __init__(self, mode: str = "race", count: int = 5, on_update=None, pet_id: str = None):
        self.mode         = mode
        self.target_count = count
        self.on_update    = on_update

        self._stop   = threading.Event()
        self._thread = None

        self.played      = 0
        self.wins        = 0
        self.total_money = 0
        self.total_exp   = 0
        self.total_extra = []
        self.user_id     = None
        self.pet_id      = pet_id  # можно передать явно, иначе выберется лучший

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self):
        logger.info(f"[AutoPlayer] TG_TOKEN prefix: {TG_TOKEN[:60]!r}")
        if not validate_token():
            self._notify("error", {"msg": "❌ TG_TOKEN не работает — проверь переменную окружения."})
            return

        self.user_id = get_my_user_id()
        self.pet_id  = self.pet_id or get_best_pet_for_mode(self.mode)

        if not self.user_id or not self.pet_id:
            self._notify("error", {"msg": "❌ Не удалось получить user_id / pet_id. Проверь TG_TOKEN."})
            return

        logger.info(f"AutoPlayer: user_id={self.user_id} pet_id={self.pet_id}")
        self._notify("started", {"mode": self.mode, "count": self.target_count})

        while self.played < self.target_count and not self._stop.is_set():
            # ─── Шаг 1: ждём матч ───────────────────────────
            logger.info(f"AutoPlayer: ищем {self.mode} ({self.played+1}/{self.target_count})…")
            waitroom = WaitroomSession(self.pet_id, self.mode)
            game_info = waitroom.wait_for_game(timeout=WAITROOM_TIMEOUT)

            if not game_info:
                self._notify("error", {"msg": "❌ Матч не найден за 60 с. Пауза 10 с."})
                time.sleep(10)
                continue

            if self._stop.is_set():
                break

            game_id   = game_info["game_id"]
            lobby_url = game_info["lobby_url"]

            # ─── Шаг 2: играем ──────────────────────────────
            result_box = {}

            def on_finish(result, _box=result_box):
                _box["r"] = result

            session = GameSession(
                game_id   = game_id,
                lobby_url = lobby_url,
                mode      = self.mode,
                user_id   = self.user_id,
                on_finish = on_finish,
                pet_id    = self.pet_id,
            )
            session.run(timeout=GAME_TIMEOUT)

            self.played += 1
            result = result_box.get("r")

            if result:
                place  = result.get("winningPlace", "?")
                prize  = result.get("prize", {})
                money  = prize.get("moneyAmount", 0)
                exp    = prize.get("experience", 0)
                extra  = result.get("extraAwards", [])
                self.total_money += money
                self.total_exp   += exp
                self.total_extra.extend(extra)
                if place == 1:
                    self.wins += 1
            else:
                place, money, exp, extra = "?", 0, 0, []

            self._notify("game_done", {
                "played":       self.played,
                "total":        self.target_count,
                "place":        place,
                "money":        money,
                "exp":          exp,
                "extra_awards": extra,
            })

            if self.played < self.target_count and not self._stop.is_set():
                time.sleep(3)

        self._notify("finished", {
            "played":      self.played,
            "wins":        self.wins,
            "total_money": self.total_money,
            "total_exp":   self.total_exp,
            "total_extra": self.total_extra,
        })

    def _notify(self, event: str, data: dict):
        logger.info(f"AutoPlayer [{event}] {data}")
        if self.on_update:
            try:
                self.on_update(event, data)
            except Exception as e:
                logger.error(f"on_update error: {e}")
