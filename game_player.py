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
import bisect
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


def _env_flag(name: str, default: bool = False) -> bool:
    """Читает bool-флаг из env: 1/true/yes/on = True."""
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}

# ── конфиг ──────────────────────────────────────────────
TG_TOKEN       = os.environ.get("TG_TOKEN", "")
API_BASE       = "https://api.nl.gatto.pw"
WAITROOM_WS    = "wss://waitroom.nl.gatto.pw/socket.io/?EIO=4&transport=websocket"
SCREEN         = {"w": 1182, "h": 468}
WAITROOM_TIMEOUT = 60    # сек ждём матч
GAME_TIMEOUT     = 120   # сек максимум на игру
SOCKET_FULL_LOG  = _env_flag("SOCKET_FULL_LOG", True)
AI_POLL_INTERVAL_MS = 80
# Дополнительное упреждение до идеальной точки прыжка.
# Нужен запас, чтобы не утыкаться в барьер при сетевом джиттере.
JUMP_LEAD_TICKS = float(os.environ.get("JUMP_LEAD_TICKS", "14"))
# Минимальный trigger для первого прыжка (до калибровки скорости).
# Фиксирован в пикселях — работает для любого пета.
FIRST_JUMP_MIN_TRIGGER_PX = float(os.environ.get("FIRST_JUMP_MIN_TRIGGER_PX", "120"))
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


def estimate_jump_power(y: float, speed_y: float, gravity: float = 6.0) -> float:
    """Вычисляет jump_power из подтверждённых y и speed_y в середине дуги.

    Формула: y*0.6 = p*(speed_y - (gravity+0.3) + 0.5*p), где p = jp - speed_y.
    """
    if y <= 0 or speed_y <= 0:
        return speed_y if speed_y > 0 else 20.0
    a = 0.5
    b = speed_y - (gravity + 0.3)
    c = -0.6 * y
    disc = b * b - 4 * a * c
    if disc < 0:
        return 20.0
    p = (-b + disc ** 0.5) / (2 * a)
    return speed_y + max(0, p)


def calibrate_gravity_jp(confirm_y: float, confirm_sy: float,
                         prev_confirm_y: float = -1.0,
                         prev_confirm_sy: float = -1.0) -> tuple:
    """Вычисляет gravity и jump_power из подтверждённого прыжка.

    ПРОБЛЕМА: одна точка (y, sy) имеет БЕСКОНЕЧНО МНОГО решений (N, jp, g)
    с err=0. Нужна дополнительная информация для однозначного выбора.

    Стратегия:
    1. Если есть данные двух прыжков (prev_confirm_y/sy) — решаем систему
       из двух уравнений → единственное решение.
    2. Если один прыжок — выбираем N который даёт jp ≈ 20 (типичное значение),
       что соответствует gravity ≈ 2-8 для большинства петов.
    """
    candidates = []

    for N in range(3, 50):
        jp = confirm_sy + 0.6 * N
        if jp < 10 or jp > 40:
            continue
        g = confirm_sy + 0.3 * N - 0.3 - confirm_y / N
        if g < 0.5 or g > 20.0:  # g < 0.5 нереалистично для race петов
            continue
        # Верифицируем симуляцией
        y, sy = 0.0, jp
        for tick in range(1, N + 1):
            sy -= 0.6
            if sy < 0:
                sy = 0.0
            y += sy
            if y - g > 0:
                y -= g
            else:
                y = 0.0
                break
        err = abs(y - confirm_y) + abs(sy - confirm_sy) * 10
        if err < 1.0:
            # Проверяем длину дуги: нереалистично длинные дуги = неправильная гравитация.
            # Типичная дуга для race: 30-200 тиков (0.3-2.0 секунды).
            # Дуга > 400 тиков (> 4 секунд) — однозначно ошибка калибровки.
            full_arc = remaining_arc_ticks(0, jp, g)
            if full_arc > 400:
                logger.debug(
                    f"calibrate_gravity_jp: REJECTED N={N} jp={jp:.2f} g={g:.3f} "
                    f"full_arc={full_arc}t (too long)"
                )
                continue
            candidates.append((N, jp, g, err, full_arc))

    if not candidates:
        return 6.0, estimate_jump_power(confirm_y, confirm_sy, 6.0)

    # Если есть данные предыдущего прыжка — используем как второе уравнение
    if prev_confirm_y > 0 and prev_confirm_sy > 0:
        valid_2pt = []
        for N, jp, g, err1, _ in candidates:
            # Для того же jp, найдём N2 для prev_confirm
            N2 = round((jp - prev_confirm_sy) / 0.6)
            if N2 < 1 or N2 > 50:
                continue
            # Симулируем prev_confirm
            y2, sy2 = 0.0, jp
            for tick in range(1, N2 + 1):
                sy2 -= 0.6
                if sy2 < 0:
                    sy2 = 0.0
                y2 += sy2
                if y2 - g > 0:
                    y2 -= g
                else:
                    y2 = 0.0
                    break
            err2 = abs(y2 - prev_confirm_y) + abs(sy2 - prev_confirm_sy) * 10
            total = err1 + err2
            if total < 2.0:
                valid_2pt.append((g, jp, total))
        if valid_2pt:
            valid_2pt.sort(key=lambda c: (round(c[2], 1), abs(c[1] - 24.5)))
            best_g, best_jp, best_err = valid_2pt[0]
            logger.info(f"calibrate_gravity_jp: 2-point solution: g={best_g:.3f} "
                       f"jp={best_jp:.2f} total_err={best_err:.4f}")
            return best_g, best_jp

    # Один прыжок: выбираем кандидата с реалистичной дугой.
    # Из реальных данных evo7 lv10: jp обычно 23-26, g обычно 3-8.
    # Сортировка: 1) предпочитаем arc 30-200 тиков, 2) jp ближе к 24.5 (типичное для evo7).
    # Для weaker петов jp ≈ 20-22 — тоже попадёт, т.к. arc всё равно реалистичный.
    candidates.sort(key=lambda c: (
        0 if 30 <= c[4] <= 200 else 1,  # c[4] = full_arc
        abs(c[1] - 24.5),                # jp ближе к типичному
    ))
    best_N, best_jp, best_g, best_err, best_arc = candidates[0]
    logger.info(f"calibrate_gravity_jp: best candidate N={best_N} jp={best_jp:.2f} "
               f"g={best_g:.3f} err={best_err:.4f} arc={best_arc}t")
    return best_g, best_jp


def remaining_arc_ticks(y: float, speed_y: float, gravity: float = 6.0) -> int:
    """Сколько тиков до приземления из текущей точки дуги (y, speed_y)."""
    for tick in range(1, 5000):
        speed_y -= 0.6
        if speed_y < 0:
            speed_y = 0.0
        y += speed_y
        if y - gravity > 0:
            y -= gravity
        else:
            return tick
    return 5000


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
                log = _l.getLogger("game_player")
                log.debug(f"[SioClient] -> {raw[:120]}")
                if SOCKET_FULL_LOG:
                    log.info(f"[SioClient][full] -> {raw}")
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
        if SOCKET_FULL_LOG:
            log.info(f"[SioClient][full] <- {msg}")

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


def _simulate_landing(start_x: float, speed_x: float, jump_power: float, gravity: float = 6.0) -> float:
    """Симулирует физику прыжка и возвращает x где пет приземлится."""
    y, sy = 0.0, jump_power
    x = float(start_x)
    for _ in range(5000):
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


def _simulate_arc_profile(speed_x: float, jump_power: float, gravity: float = 6.0):
    """
    Симулирует полный прыжок и возвращает (dxs, ys) — параллельные массивы
    горизонтального смещения и высоты на каждом тике.
    Используется для точного расчёта пролёта над барьерами.
    """
    dxs, ys = [], []
    y, sy = 0.0, jump_power
    dx = 0.0
    for _ in range(5000):
        sy -= 0.6
        if sy < 0:
            sy = 0.0
        y += sy
        if y - gravity > 0:
            y -= gravity
        else:
            y = max(0.0, y - gravity)
            if y <= 0:
                return dxs, ys
        dx += speed_x
        dxs.append(dx)
        ys.append(y)
    return dxs, ys


def _height_at_dx(dxs: list, ys: list, target_dx: float) -> float:
    """Интерполирует высоту на горизонтальном расстоянии target_dx (бинарный поиск)."""
    if not dxs or target_dx <= 0:
        return 0.0
    if target_dx >= dxs[-1]:
        return 0.0  # за пределами дуги — уже на земле
    i = bisect.bisect_left(dxs, target_dx)
    if i == 0:
        return ys[0] * (target_dx / dxs[0]) if dxs[0] > 0 else ys[0]
    x0, y0 = dxs[i - 1], ys[i - 1]
    x1, y1 = dxs[i], ys[i]
    t = (target_dx - x0) / (x1 - x0) if x1 != x0 else 0
    return y0 + t * (y1 - y0)


def _min_height_over_zone(dxs: list, ys: list, start_dx: float, zone_width: float) -> float:
    """Минимальная высота пета над зоной барьера [start_dx, start_dx + zone_width]."""
    h1 = _height_at_dx(dxs, ys, start_dx)
    h2 = _height_at_dx(dxs, ys, start_dx + zone_width * 0.5)
    h3 = _height_at_dx(dxs, ys, start_dx + zone_width)
    return min(h1, h2, h3)


def _calc_landing(real_x: float, speed_x: float, confirm_y: float, confirm_sy: float,
                  gravity: float = 6.0) -> tuple:
    """
    Вычисляет позицию и время приземления из mid-arc состояния.
    Возвращает (landing_x, landing_ticks).
    """
    y, sy = confirm_y, confirm_sy
    dx = 0.0
    for tick in range(1, 5000):
        sy -= 0.6
        if sy < 0:
            sy = 0.0
        y += sy
        if y - gravity > 0:
            y -= gravity
        else:
            return real_x + dx, tick
        dx += speed_x
    return real_x + dx, 5000


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
        self.gravity       = 6.0     # консервативный дефолт; калибруется после 1-го прыжка
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
        self._prev_last_jumped_barrier = 0.0  # предыдущее значение для отката при rejection
        self._rejected_in_flight = False       # после rejection: следующее engine.jump — stale update
        self._jump_started_at = 0.0            # time.time() когда последний _do_jump был вызван
        self._last_sent_jumped_at = 0.0   # jumpedAt который мы отправили последним
        self._jump_latency_ms     = 150.0 # EWMA задержки send→server подтверждения
        self._confirmed_jumps     = 0     # кол-во подтверждённых прыжков (для калибровки)
        self._prev_confirmed_x   = 0.0   # x предыдущего подтверждённого прыжка
        self._prev_confirmed_at  = 0.0   # jumpedAt предыдущего подтверждённого прыжка
        self._last_confirm_y     = -1.0  # y последнего подтверждённого прыжка
        self._last_confirm_sy    = -1.0  # speed_y последнего подтверждённого прыжка
        self._first_confirm_y    = -1.0  # y первого подтверждённого прыжка (для 2-point calibration)
        self._first_confirm_sy   = -1.0  # sy первого подтверждённого прыжка
        self._prev_target_x      = 0.0   # target_x предыдущего запланированного прыжка
        self._anchor_x           = 118.0 # последняя подтверждённая x
        self._anchor_server_time = 0.0   # serverTime для anchor_x
        self._arc_spx            = 0.0   # speed_x во время дуги (выше ground speed)
        self._landing_x          = 0.0   # предсказанная позиция приземления
        self._landing_server_time = 0.0  # предсказанный serverTime приземления

        # ── Модель ускорения скорости (из speed.ts) ──
        # speed(t) = min(initial_speed + accel * t_sec, max_speed)
        # Калибруется из подтверждённых прыжков.
        self._speed_initial      = 0.0   # начальная скорость px/tick (из pre-game sync или connected)
        self._speed_max          = 0.0   # максимальная скорость px/tick (оценка)
        self._speed_accel        = 0.0   # ускорение px/tick/sec
        self._speed_samples      = []    # [(server_time, speed_x)] для калибровки ускорения
        self._speed_model_ready  = False # модель откалибрована

        self.result             = None
        self._done         = threading.Event()

    def _now_server_ms(self) -> float:
        """Оценивает текущее serverTime по локальным часам + offset."""
        return time.time() * 1000 + self.server_time_offset

    def _speed_at_server_time(self, server_time_ms: float) -> float:
        """
        Вычисляет ground speed (px/tick) на момент server_time_ms.

        Модель из speed.ts: speed(t) = min(initial + accel * t_sec, max_speed)
        Если модель не откалибрована — возвращает current_speed_x.
        """
        if not self._speed_model_ready or not self.physics_start_at:
            return self.current_speed_x
        t_sec = max(0.0, (server_time_ms - self.physics_start_at) / 1000.0)
        spx = self._speed_initial + self._speed_accel * t_sec
        if self._speed_max > 0:
            spx = min(spx, self._speed_max)
        return max(0.1, spx)

    def _calibrate_speed_model(self, server_time: float, speed_x: float):
        """
        Добавляет точку (server_time, speed_x) и калибрует модель ускорения.

        После 2+ точек вычисляем линейную регрессию speed(t) = initial + accel * t_sec.
        """
        if not self.physics_start_at or speed_x <= 0.1:
            return
        self._speed_samples.append((server_time, speed_x))

        # Нужно минимум 2 точки для вычисления ускорения
        if len(self._speed_samples) < 2:
            # Одна точка: сохраняем как initial, ускорение 0
            self._speed_initial = speed_x
            return

        # Линейная регрессия: speed = a + b * t_sec
        n = len(self._speed_samples)
        sum_t = sum_s = sum_ts = sum_tt = 0.0
        for st, sx in self._speed_samples:
            t = (st - self.physics_start_at) / 1000.0
            sum_t += t
            sum_s += sx
            sum_ts += t * sx
            sum_tt += t * t

        denom = n * sum_tt - sum_t * sum_t
        if abs(denom) < 1e-9:
            return

        b = (n * sum_ts - sum_t * sum_s) / denom  # ускорение
        a = (sum_s - b * sum_t) / n                # начальная скорость

        if a > 0.1 and b >= 0:
            self._speed_initial = a
            self._speed_accel = b
            # Оценка max_speed: если ускорение > 0, max ≈ последнее наблюдение * 1.5
            # (или будет уточнено если скорость перестанет расти)
            if b > 0 and not self._speed_max:
                last_spx = self._speed_samples[-1][1]
                self._speed_max = last_spx * 2.0  # грубая верхняя граница
            self._speed_model_ready = True
            logger.info(
                f"[{self.game_id}] Speed model: initial={a:.4f} accel={b:.6f}/sec "
                f"max={self._speed_max:.4f} samples={n}"
            )

    def _integrate_distance(self, from_server_time: float, to_server_time: float) -> float:
        """
        Интегрирует пройденное расстояние с учётом ускорения скорости.
        speed(t) = min(initial + accel * t_sec, max_speed)
        Расстояние = интеграл speed от t0 до t1 (в тиках).
        """
        if not self._speed_model_ready or not self.physics_start_at:
            # Фоллбэк: постоянная скорость
            dt_ticks = (to_server_time - from_server_time) / 10.0
            return self.current_speed_x * max(0.0, dt_ticks)

        # t0, t1 — секунды от начала физики
        t0_sec = max(0.0, (from_server_time - self.physics_start_at) / 1000.0)
        t1_sec = max(t0_sec, (to_server_time - self.physics_start_at) / 1000.0)

        a = self._speed_accel   # px/tick / sec
        s0 = self._speed_initial  # px/tick при t=0
        s_max = self._speed_max if self._speed_max > 0 else 1e9

        # Время (сек от старта) когда скорость достигает max
        if a > 1e-9:
            t_cap_sec = (s_max - s0) / a
        else:
            t_cap_sec = 1e9  # нет ускорения

        # Интегрируем speed(t)*100 ticks/sec от t0_sec до t1_sec
        # speed в px/tick, 100 тиков/сек → distance_per_sec = speed * 100
        # Но нам нужно в тиках: dt_ticks = (t1 - t0) * 100, distance = sum(speed * 1 tick)
        # Или: distance = integral(speed(t), dt) * 100  (переводим сек в тики)

        dist = 0.0
        # Фаза 1: ускорение (t0 до min(t1, t_cap))
        phase1_end = min(t1_sec, t_cap_sec)
        if phase1_end > t0_sec:
            dt = phase1_end - t0_sec
            # speed(t) = s0 + a*t (px/tick)
            # integral = (s0 + a*t0)*dt + a*dt^2/2  (в px/tick * sec)
            # Переводим в px: * 100 тиков/сек
            avg_speed = (s0 + a * t0_sec) + a * dt / 2.0
            dist += avg_speed * dt * 100.0  # px

        # Фаза 2: максимальная скорость (t_cap до t1)
        if t1_sec > t_cap_sec and t_cap_sec > t0_sec:
            dt = t1_sec - t_cap_sec
            dist += s_max * dt * 100.0
        elif t1_sec > t_cap_sec and t_cap_sec <= t0_sec:
            # Уже на максимальной скорости весь интервал
            dt = t1_sec - t0_sec
            dist += s_max * dt * 100.0

        return dist

    def _estimate_pet_x(self) -> float:
        """
        Предсказывает x по последней опорной точке (x, serverTime).

        Во время прыжка пет движется с arc_spx (выше ground speed).
        После приземления — с ground speed (с учётом ускорения).
        """
        now_srv = self._now_server_ms()
        # Текущая скорость для forward-prediction (пол-RTT вперёд)
        spx_now = self._speed_at_server_time(now_srv)
        half_rtt_ticks = self._jump_latency_ms / 20.0
        forward_px = spx_now * half_rtt_ticks

        if self._anchor_server_time > 0 and spx_now > 0:
            # Во время прыжка: arc_spx до приземления, ground speed после
            if (self.pet_status == "jumping"
                    and self._arc_spx > 0
                    and self._landing_server_time > self._anchor_server_time):
                if now_srv < self._landing_server_time:
                    dt_ticks = max(0.0, (now_srv - self._anchor_server_time) / 10.0)
                    return self._anchor_x + self._arc_spx * dt_ticks + self._arc_spx * half_rtt_ticks
                else:
                    arc_ticks = (self._landing_server_time - self._anchor_server_time) / 10.0
                    arc_dist = self._arc_spx * arc_ticks
                    ground_dist = self._integrate_distance(self._landing_server_time, now_srv)
                    return self._anchor_x + arc_dist + ground_dist + forward_px

            # На земле: интегрируем расстояние с ускорением
            ground_dist = self._integrate_distance(self._anchor_server_time, now_srv)
            return self._anchor_x + ground_dist + forward_px

        if self.game_started_at and spx_now > 0:
            if self.physics_start_at:
                ground_dist = self._integrate_distance(self.physics_start_at, now_srv)
                # До калибровки: скорость ускоряется, но модель не готова.
                # _integrate_distance использует постоянную current_speed_x →
                # позиция ЗАНИЖЕНА → dist к барьеру завышена → прыжок поздно.
                # Применяем тот же буст что и в _tick_bot.
                if self._confirmed_jumps == 0 and not self._speed_model_ready:
                    elapsed_sec = max(0.0, (now_srv - self.physics_start_at) / 1000.0)
                    if elapsed_sec > 0.3:
                        # Средний буст за период [0, elapsed_sec]:
                        # avg_boost = 1 + 0.15 * elapsed_sec / 2
                        avg_boost = 1.0 + min(0.25, elapsed_sec * 0.075)
                        ground_dist *= avg_boost
                return 118.0 + ground_dist + forward_px
            elapsed_ms = time.time() * 1000 - self.game_started_at
            return 118.0 + spx_now * (elapsed_ms / 10.0) + forward_px

        return self.pet_x

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
            if self.started and self.barriers:
                # Обновляем current_speed_x из модели ускорения
                if self._speed_model_ready:
                    self.current_speed_x = self._speed_at_server_time(self._now_server_ms())

                if self.current_speed_x <= 0:
                    time.sleep(AI_POLL_INTERVAL_MS / 1000.0)
                    continue

                # Safety timeout: если pet_status == "jumping" > 5с, принудительно сбросить
                if (self.pet_status in ("jumping", "diving")
                        and self._jump_started_at > 0
                        and time.time() - self._jump_started_at > 25.0):
                    logger.warning(
                        f"[{self.game_id}] SAFETY TIMEOUT: pet_status={self.pet_status} "
                        f"for {time.time() - self._jump_started_at:.1f}s, forcing running"
                    )
                    self.pet_status = "running"
                    self._rejected_in_flight = False
                    self._last_confirm_y = -1.0
                    self._last_confirm_sy = -1.0
                    self._arc_spx = 0.0

                if self.pet_status not in ("jumping", "diving"):
                    self._tick_bot()
            time.sleep(AI_POLL_INTERVAL_MS / 1000.0)

    def _do_jump(self, last_barrier_x: float, dist: float, extra_info: str = ""):
        """Отправляет прыжок/нырок на сервер."""
        now_srv = int(time.time() * 1000 + self.server_time_offset)
        payload = {
            "clickPosition": {"x": self.click_x, "y": self.click_y},
            "jumpedAt": now_srv,
        }
        event = "engine.jump" if self.mode == "race" else "engine.dive"
        self._client.emit_with_null(event, payload)
        self.pet_status = "jumping"
        self._jump_started_at = time.time()
        self._rejected_in_flight = False  # новый прыжок — сбрасываем флаг
        self._prev_last_jumped_barrier = self._last_jumped_barrier  # сохраняем для отката
        self._last_jumped_barrier = last_barrier_x
        self._last_sent_jumped_at = now_srv
        logger.info(
            f"[{self.game_id}] ⏱ JUMP barrier={last_barrier_x} "
            f"dist={dist:.1f} spx={self.current_speed_x:.3f} "
            f"jp={self.jump_power:.1f} {extra_info}"
        )

    def _tick_bot(self):
        """
        Универсальный polling loop — работает для любой скорости пета.

        Вместо эвристических формул используем ПОЛНУЮ СИМУЛЯЦИЮ дуги прыжка:
        1. Для каждого возможного расстояния до барьера симулируем дугу
           и проверяем, хватает ли высоты для пролёта.
        2. Находим «безопасную зону» — диапазон расстояний, в которых прыжок
           гарантированно перелетает барьер.
        3. Для сильных петов: проверяем ВСЕ барьеры в пределах дуги
           и помечаем перелетённые, чтобы не прыгать для них повторно.
        """
        self.pet_x = self._estimate_pet_x()
        pet_front = self.pet_x + self.width_pet

        # Ищем следующий барьер впереди
        next_idx = None
        for i, b in enumerate(self.barriers):
            if b["x"] + self.width_barrier > pet_front:
                next_idx = i
                break
        if next_idx is None:
            return

        barrier = self.barriers[next_idx]
        dist = barrier["x"] - pet_front
        if dist <= 0:
            return
        if barrier["x"] <= self._last_jumped_barrier:
            return

        spx = self.current_speed_x
        barrier_high = barrier.get("high", 50)

        # ── Коррекция скорости для первого прыжка ──
        # До калибровки current_speed_x = начальная скорость из формулы (agi→px/tick).
        # Но speed УСКОРЯЕТСЯ со временем (speed.ts: initial → max за speedUpInSec).
        # К моменту первого барьера (2-3 сек) скорость уже значительно выше начальной.
        # Если speed model готов (speed config от сервера) — скорость уже корректна.
        # Если НЕТ — оцениваем по прошедшему времени: +15%/сек, макс +50%.
        if self._confirmed_jumps == 0 and not self._speed_model_ready and self.physics_start_at > 0:
            elapsed_sec = max(0.0, (self._now_server_ms() - self.physics_start_at) / 1000.0)
            if elapsed_sec > 0.3:
                # Типичное ускорение из реальных данных:
                # initial=1.64 → actual=2.33 за ~3сек ≈ +14%/сек.
                boost = 1.0 + min(0.5, elapsed_sec * 0.15)
                spx = spx * boost
                logger.info(
                    f"[{self.game_id}] First jump speed boost: "
                    f"base={self.current_speed_x:.3f} elapsed={elapsed_sec:.1f}s "
                    f"boost={boost:.2f} → spx={spx:.3f}"
                )

        # ── Симуляция дуги ──
        dxs, ys = _simulate_arc_profile(spx, self.jump_power, self.gravity)
        if not dxs:
            return
        arc_len = dxs[-1]

        # Барьер ещё далеко — не в пределах дуги
        if dist > arc_len:
            return

        # ── Найти безопасную зону прыжка ──
        # safe zone: [min_safe_d, max_safe_d] — диапазон значений dist,
        # при которых дуга пролетает над barrier_high.
        # min_safe_d = прыгнуть максимально поздно (ближе к барьеру)
        # max_safe_d = прыгнуть максимально рано (дальше от барьера)
        step = max(0.5, spx * 0.5)
        min_safe_d = None
        max_safe_d = None
        best_safe_d = None
        best_margin = -999.0
        d = step
        while d < arc_len:
            h = _min_height_over_zone(dxs, ys, d, self.width_barrier)
            margin = h - barrier_high
            if margin > 0:
                if min_safe_d is None:
                    min_safe_d = d
                max_safe_d = d
                if margin > best_margin:
                    best_margin = margin
                    best_safe_d = d
            d += step

        if min_safe_d is None:
            # Пет НЕ МОЖЕТ перепрыгнуть этот барьер ни при каком тайминге.
            # Для первого прыжка: пробуем с ещё более высокой скоростью (1.5x текущей).
            # Реальная скорость может быть выше оценки из-за неточной формулы.
            if self._confirmed_jumps == 0:
                spx_retry = spx * 1.3
                dxs2, ys2 = _simulate_arc_profile(spx_retry, self.jump_power, self.gravity)
                if dxs2:
                    arc_len2 = dxs2[-1]
                    d = max(0.5, spx_retry * 0.5)
                    while d < arc_len2:
                        h = _min_height_over_zone(dxs2, ys2, d, self.width_barrier)
                        margin = h - barrier_high
                        if margin > 0:
                            if min_safe_d is None:
                                min_safe_d = d
                            max_safe_d = d
                            if margin > best_margin:
                                best_margin = margin
                                best_safe_d = d
                        d += max(0.5, spx_retry * 0.5)
                    if min_safe_d is not None:
                        # Нашли зону с повышенной скоростью — обновляем arc
                        dxs, ys = dxs2, ys2
                        arc_len = arc_len2
                        spx = spx_retry
                        logger.info(
                            f"[{self.game_id}] First jump: safe zone found with "
                            f"boosted spx={spx_retry:.3f} (1.3x retry)"
                        )
            if min_safe_d is None:
                # Аварийный прыжок — максимальная высота (best effort).
                if dist <= spx * 10:
                    self._do_jump(barrier["x"], dist, "EMERGENCY no-safe-zone")
                return

        # ── Учёт сетевого лага ──
        lag_ticks = (
            AI_POLL_INTERVAL_MS / 10.0
            + self._jump_latency_ms / 20.0
            + JUMP_LEAD_TICKS
        )
        lag_px = spx * lag_ticks

        # Хотим, чтобы dist - lag_px попало в [min_safe_d, max_safe_d].
        # Триггер: dist <= max_safe_d + lag_px (вошли в окно)
        trigger = best_safe_d + lag_px  # целимся в оптимальную точку
        trigger = max(min_safe_d + lag_px, min(trigger, max_safe_d + lag_px))

        # Первый прыжок (скорость ещё не откалибрована): доп. запас.
        if self._confirmed_jumps == 0:
            trigger = max(trigger, FIRST_JUMP_MIN_TRIGGER_PX)
            trigger = min(trigger, max_safe_d + lag_px)  # не выходим за пределы дуги

        if dist > trigger:
            return  # ещё рано

        # ── Мульти-барьерная проверка (сильные петы) ──
        # Проверяем, перелетит ли дуга ещё барьеры впереди.
        last_cleared_x = barrier["x"]
        for i in range(next_idx + 1, len(self.barriers)):
            b2 = self.barriers[i]
            d2 = b2["x"] - pet_front
            if d2 >= arc_len:
                break
            h2 = b2.get("high", 50)
            h_over = _min_height_over_zone(dxs, ys, d2, self.width_barrier)
            if h_over > h2:
                last_cleared_x = b2["x"]
            else:
                # Дуга НЕ перелетит этот барьер.
                # Проверяем, приземлимся ли мы ДО него.
                if arc_len >= d2 - self.width_pet:
                    # Приземлимся НА барьер — нужно прыгнуть позже,
                    # чтобы укоротить полёт и сесть между барьерами.
                    # Пересчитываем trigger с учётом посадки перед b2.
                    max_arc_before_b2 = d2 - self.width_pet - self.width_barrier
                    if max_arc_before_b2 < min_safe_d:
                        # Невозможно перелететь первый и сесть до второго
                        logger.warning(
                            f"[{self.game_id}] Барьеры слишком близко: "
                            f"{barrier['x']} и {b2['x']}, arc={arc_len:.0f}"
                        )
                    # Продолжаем — лучше перепрыгнуть первый, чем стоять
                break

        self._do_jump(last_cleared_x, dist,
                      f"zone=[{min_safe_d:.0f},{max_safe_d:.0f}] "
                      f"best={best_safe_d:.0f} margin={best_margin:.1f} "
                      f"trigger={trigger:.0f} lag={lag_px:.0f} arc={arc_len:.0f} "
                      f"spx_used={spx:.3f} cleared_to={last_cleared_x}")

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
            # Квадратичная формула скорости из реальных измерений:
            # (10→1.648, 53→2.610, 77→4.150 px/tick)
            # Это начальная оценка; после первого прыжка скорость
            # калибруется автоматически из данных сервера.
            agility = info.get("chars", {}).get("agility", 53)
            self.current_speed_x = (
                0.000624 * agility**2
                - 0.016927 * agility
                + 1.754893
            )
            self.current_speed_x = max(0.5, self.current_speed_x)

            # Сохраняем speed config из pet_wrap (если сервер отдаёт)
            speed_cfg = pet_wrap.get("speed", {})
            if speed_cfg:
                logger.info(f"[{self.game_id}] pet speed config: {speed_cfg}")
            # Сохраняем начальную скорость как первый семпл модели
            self._speed_initial = self.current_speed_x

            # Вычисляем параметры из speed.ts формул если есть chars
            chars = info.get("chars", {})
            strength = chars.get("strength", 0)
            speed_initial_cfg = speed_cfg.get("initial", 0)
            speed_max_cfg = speed_cfg.get("max", 0)
            speed_inc_cfg = speed_cfg.get("increasePerSec", 0)
            if speed_initial_cfg > 0 and speed_max_cfg > 0 and speed_inc_cfg > 0:
                # Формула из speed.ts для Race:
                # initialSpeed = speed.initial + speed.initial * (strength / 100)
                # maxSpeed = speed.max + speed.max * ((str/100 + agi/100) * 0.45)
                # accelPerSec = speed.increasePerSec + speed.increasePerSec * ((str/100 + agi/100) * 0.45)
                stat_mult = (strength / 100.0 + agility / 100.0) * 0.45
                self._speed_initial = speed_initial_cfg * (1 + strength / 100.0)
                self._speed_max = speed_max_cfg * (1 + stat_mult)
                self._speed_accel = speed_inc_cfg * (1 + stat_mult)
                self._speed_model_ready = True
                self.current_speed_x = self._speed_initial
                logger.info(
                    f"[{self.game_id}] Speed from config: "
                    f"initial={self._speed_initial:.4f} "
                    f"max={self._speed_max:.4f} "
                    f"accel={self._speed_accel:.6f}/sec "
                    f"str={strength} agi={agility}"
                )

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
                    self._anchor_x = self.pet_x
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
                    if self.pet_status in ("jumping", "diving") and status == "running":
                        logger.info(
                            f"[{self.game_id}] LANDING from sync: "
                            f"server status=running, y={self.pet_y:.1f}"
                        )
                        self._rejected_in_flight = False
                        self._last_confirm_y = -1.0
                        self._last_confirm_sy = -1.0
                        self._arc_spx = 0.0
                    self.pet_status = status

                # Дополнительная проверка: если пет в воздухе но y ≤ 1 → приземлился
                if (self.pet_status in ("jumping", "diving")
                        and coords.get("y") is not None
                        and coords["y"] <= 1.0
                        and self.started):
                    logger.info(
                        f"[{self.game_id}] LANDING from sync coords: y={coords['y']:.2f}"
                    )
                    self.pet_status = "running"
                    self._rejected_in_flight = False
                    self._last_confirm_y = -1.0
                    self._last_confirm_sy = -1.0
                    self._arc_spx = 0.0

                # Если приземлились — обновляем anchor для точной позиции
                if self.pet_status == "running" and coords.get("x") is not None:
                    server_time_sync = data.get("serverTime", 0)
                    if server_time_sync > 0:
                        self._anchor_x = coords["x"]
                        self._anchor_server_time = server_time_sync

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
                if found:
                    self._anchor_server_time = server_time_sync

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
            self._anchor_server_time = server_time
            self._anchor_x = self.pet_x

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
            # Калибруем jump_power из прыжков оппонентов (y=0 → sy = jump_power)
            if data.get("userId") != self.user_id:
                opp_coords = data.get("coordinates", {})
                opp_speed = data.get("speed", {}) or {}
                opp_y = opp_coords.get("y", -1)
                opp_sy = opp_speed.get("y", 0)
                if opp_y == 0 and opp_sy > 10 and self._confirmed_jumps == 0:
                    # У оппонента y=0 — прыжок только начался, sy = jump_power
                    self.jump_power = opp_sy
                    logger.info(
                        f"[{self.game_id}] JP из оппонента: "
                        f"userId={data.get('userId')} sy={opp_sy:.2f}"
                    )
                return

            coords = data.get("coordinates", {})
            real_x = coords.get("x")
            speed_data = data.get("speed", {}) or {}
            arc_spx = speed_data.get("x", 0)
            confirm_y = coords.get("y", 0)
            confirm_sy = speed_data.get("y", 0)

            # После rejection сервер может прислать ещё один engine.jump
            # с текущей позицией пета в дуге — это НЕ новый прыжок.
            # Проверяем ДО rejection detection, т.к. stale update может иметь
            # те же y/sy и ложно триггерить повторный rejection.
            if self._rejected_in_flight:
                logger.info(
                    f"[{self.game_id}] Stale position update after rejection: "
                    f"x={real_x} y={confirm_y:.1f} sy={confirm_sy:.1f}"
                )
                self._last_confirm_y = confirm_y
                self._last_confirm_sy = confirm_sy
                if real_x is not None:
                    self.pet_x = real_x
                    self._anchor_x = real_x
                    st = data.get("serverTime")
                    if st:
                        self._anchor_server_time = st
                self._rejected_in_flight = False

                # Проверяем: пет уже приземлился?
                if confirm_y <= 1.0:
                    logger.info(
                        f"[{self.game_id}] LANDING from stale update: y={confirm_y:.2f}"
                    )
                    self.pet_status = "running"
                    self._last_confirm_y = -1.0
                    self._last_confirm_sy = -1.0
                    self._arc_spx = 0.0
                return

            # Детекция отклонённого прыжка: сервер вернул те же y/sy что и раньше
            # → пет всё ещё в воздухе, прыжок был проигнорирован.
            if (self._last_confirm_y >= 0
                    and abs(confirm_y - self._last_confirm_y) < 0.1
                    and abs(confirm_sy - self._last_confirm_sy) < 0.1):
                logger.warning(
                    f"[{self.game_id}] JUMP REJECTED: пет в воздухе! "
                    f"y={confirm_y:.1f} sy={confirm_sy:.1f} (те же что и прошлый) "
                    f"reverting _last_jumped_barrier {self._last_jumped_barrier:.0f} → {self._prev_last_jumped_barrier:.0f}"
                )
                # Откат состояния: прыжок не состоялся — возвращаем предыдущие значения
                self._last_jumped_barrier = self._prev_last_jumped_barrier
                self.pet_status = "jumping"  # пет ВСЁ ЕЩЁ в воздухе от предыдущего прыжка
                self._rejected_in_flight = True  # следующее engine.jump — stale update, игнорировать
                # Обновляем anchor из rejection data для точности позиции
                if real_x is not None:
                    self.pet_x = real_x
                    self._anchor_x = real_x
                    rej_st = data.get("serverTime")
                    if rej_st:
                        self._anchor_server_time = rej_st
                return

            self.pet_status = "jumping"
            self._last_confirm_y = confirm_y
            self._last_confirm_sy = confirm_sy

            # Оценка фактической задержки между jumpedAt и serverTime.
            jumped_at = self._last_sent_jumped_at
            server_time = data.get("serverTime")
            if jumped_at and server_time:
                sample = max(0.0, min(600.0, float(server_time) - float(jumped_at)))
                self._jump_latency_ms = self._jump_latency_ms * 0.7 + sample * 0.3

            if real_x is not None:
                self.pet_x = real_x
                jump_server_time = data.get("serverTime") or self._now_server_ms()

                # Измеряем скорость от anchor до confirmed.
                measured_spx = 0.0
                if self._anchor_server_time > 0 and self._anchor_x >= 0:
                    dt_ticks = (jump_server_time - self._anchor_server_time) / 10.0
                    if dt_ticks > 100:  # минимум 1с
                        measured_spx = (real_x - self._anchor_x) / dt_ticks

                # arc_spx — мгновенная скорость из engine.jump (speed.x).
                # measured_spx — средняя скорость (anchor→confirmed), зависит от
                # точности landing prediction.
                #
                # Для БЫСТРЫХ петов (длинные дуги → landing prediction неточный):
                #   measured_spx может быть x2 от реальной → ненадёжна.
                # Для МЕДЛЕННЫХ петов (короткие дуги → landing prediction точный):
                #   measured_spx отражает реальное УСКОРЕНИЕ, arc_spx занижена.
                #
                # Стратегия: если measured_spx в пределах 30% от arc_spx И выше —
                # это надёжное ускорение, используем measured_spx.
                # Иначе (слишком большая разница) — used arc_spx.
                best_spx = arc_spx if arc_spx > 0.5 else 0.0
                if arc_spx > 0.5 and measured_spx > arc_spx:
                    ratio = measured_spx / arc_spx
                    if ratio < 1.3:
                        # measured_spx надёжна (близка к arc_spx) и выше → ускорение
                        best_spx = measured_spx
                elif measured_spx > 0.5 and best_spx < 0.1:
                    best_spx = measured_spx
                if best_spx > 0.5:
                    self.current_speed_x = best_spx

                self._confirmed_jumps += 1
                self._anchor_x = real_x
                self._anchor_server_time = jump_server_time

                # Калибруем gravity И jump_power
                # 1-й прыжок: одна точка (y, sy) — неоднозначная, используем эвристику jp≈20
                # 2-й прыжок: ДВЕ точки → единственное решение (перекалибровка)
                confirm_y_jp = confirm_y
                confirm_sy_jp = confirm_sy
                if confirm_y_jp > 10 and confirm_sy_jp > 1 and self._confirmed_jumps <= 2:
                    cal_gravity, cal_jp = calibrate_gravity_jp(
                        confirm_y_jp, confirm_sy_jp,
                        self._first_confirm_y, self._first_confirm_sy
                    )
                    if 15 < cal_jp < 40:
                        old_g = self.gravity
                        self.jump_power = cal_jp
                        self.gravity = cal_gravity
                        logger.info(
                            f"[{self.game_id}] Калибровка JP+G (jump#{self._confirmed_jumps}): "
                            f"y={confirm_y_jp:.1f} sy={confirm_sy_jp:.1f} → "
                            f"jp={cal_jp:.2f} gravity={cal_gravity:.3f} "
                            f"(prev g={old_g:.3f})"
                        )
                    # Сохраняем данные первого прыжка для перекалибровки
                    if self._confirmed_jumps == 1 and self._first_confirm_y < 0:
                        self._first_confirm_y = confirm_y_jp
                        self._first_confirm_sy = confirm_sy_jp

                # Калибруем модель ускорения скорости из лучшей оценки скорости.
                # best_spx учитывает ускорение (measured_spx когда надёжна).
                if best_spx > 0.5 and jump_server_time > 0:
                    self._calibrate_speed_model(jump_server_time, best_spx)

                # Обновляем current_speed_x из модели ускорения (для AI loop)
                if self._speed_model_ready:
                    self.current_speed_x = self._speed_at_server_time(self._now_server_ms())

                # Калибруем game_started_at по реальной позиции
                if self.current_speed_x > 0:
                    ticks = (real_x - 118.0) / self.current_speed_x
                    self.game_started_at = time.time() * 1000 - ticks * 10
                logger.info(
                    f"[{self.game_id}] JUMP confirmed: x={real_x:.0f} "
                    f"measured_spx={measured_spx:.4f} arc_spx={arc_spx:.4f} "
                    f"best_spx={best_spx:.4f} using={self.current_speed_x:.4f} "
                    f"lag_ms≈{self._jump_latency_ms:.0f}"
                )

            self.last_update = data.get("lastUpdate", self.last_update)

            # ── Вычисляем позицию приземления и ставим таймер ──
            arc_remain = remaining_arc_ticks(confirm_y, confirm_sy, self.gravity)

            # Для landing_x используем arc_spx (скорость во время дуги),
            # а не ground speed — пет летит быстрее чем бежит.
            use_arc_spx = arc_spx if arc_spx > 0.5 else self.current_speed_x
            self._arc_spx = use_arc_spx

            landing_x, landing_ticks = _calc_landing(
                real_x if real_x is not None else self.pet_x,
                use_arc_spx, confirm_y, confirm_sy, self.gravity
            )
            jump_server_time_val = data.get("serverTime") or self._now_server_ms()
            self._landing_x = landing_x
            self._landing_server_time = jump_server_time_val + landing_ticks * 10.0

            # Таймер reset: arc_remain * 10ms + 50% запас + сетевая задержка.
            latency_buf = max(0.3, self._jump_latency_ms / 500.0)
            reset_delay = max(1.0, arc_remain * 0.015 + latency_buf)
            reset_delay = min(reset_delay, 25.0)  # не более 25с (для low-gravity петов дуга до 15с+)

            def reset_after_jump(delay, lx, lst, created_at_jump):
                time.sleep(delay)
                self._rejected_in_flight = False
                # Если после создания этого таймера был подтверждён НОВЫЙ прыжок,
                # этот таймер устарел — его anchor_x неактуален.
                if self._confirmed_jumps > created_at_jump:
                    logger.info(
                        f"[{self.game_id}] Arc timer SKIPPED (stale): "
                        f"created for jump#{created_at_jump}, "
                        f"current jumps={self._confirmed_jumps}"
                    )
                    return
                if self.pet_status in ("jumping", "diving"):
                    self.pet_status = "running"
                self._last_confirm_y = -1.0
                self._last_confirm_sy = -1.0
                self._anchor_x = lx
                self._anchor_server_time = lst
                self._arc_spx = 0.0
                logger.info(
                    f"[{self.game_id}] Arc timer: pet_status → running, "
                    f"anchor_x={lx:.0f} (jump#{created_at_jump})"
                )

            threading.Thread(
                target=reset_after_jump,
                args=(reset_delay, landing_x, self._landing_server_time,
                      self._confirmed_jumps),
                daemon=True
            ).start()
            logger.info(
                f"[{self.game_id}] Arc: remain={arc_remain}t "
                f"({arc_remain * 0.01:.1f}s) reset={reset_delay:.1f}s "
                f"y={confirm_y:.0f} sy={confirm_sy:.1f} "
                f"arc_spx={use_arc_spx:.3f} landing_x≈{landing_x:.0f}"
            )

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
