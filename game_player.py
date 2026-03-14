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
class GameSession:
    def __init__(self, game_id: str, lobby_url: str, mode: str, user_id: int, on_finish=None):
        self.game_id   = game_id
        self.lobby_url = lobby_url
        self.mode      = mode
        self.user_id   = user_id
        self.on_finish = on_finish

        # Стейт пета
        self.pet_id        = None
        self.pet_row       = None
        self.pet_x         = 0.0
        self.pet_y         = 0.0
        self.pet_status    = "running"
        self.speed_x       = 6.5
        self.speed_y       = 0.0
        self.jump_power    = 12.0
        self.gravity       = 1.5
        self.width_pet     = 40
        self.width_barrier = 30

        self.barriers    = []
        self.last_update = 0
        self.started     = False
        self.result      = None
        self._done       = threading.Event()

    def _ai_loop(self):
        while not self._done.is_set():
            if self.started and self.barriers and self.mode == "race":
                if self._should_jump():
                    self._client.emit("engine.jump", self._make_jump_payload())
                    logger.debug(f"[{self.game_id}] JUMP x={self.pet_x:.0f}")
                    time.sleep(0.35)
            time.sleep(0.08)

    def _should_jump(self) -> bool:
        if self.pet_status == "jumping" or not self.barriers:
            return False
        pet_front = self.pet_x + self.width_pet
        next_b = next(
            (b for b in self.barriers if b["x"] + self.width_barrier > pet_front),
            None
        )
        if not next_b:
            return False
        dist = next_b["x"] - pet_front
        if dist <= 0:
            return False
        ticks = ticks_to_reach_height(self.jump_power, self.gravity, 50)
        ideal_dist = self.speed_x * (ticks + 2)
        return dist <= ideal_dist * 0.85

    def _make_jump_payload(self) -> dict:
        return {
            "lastUpdate":    self.last_update,
            "coordinates":   {"x": self.pet_x, "y": self.pet_y},
            "speed":         {"x": self.speed_x, "y": self.speed_y},
            "petLastUpdate": self.last_update,
            "userId":        self.user_id,
            "petId":         self.pet_id or "",
            "serverTime":    int(time.time() * 1000),
        }

    def run(self, timeout: int = GAME_TIMEOUT):
        client = SioClient(self.lobby_url, TG_TOKEN)
        self._client = client

        def on_open():
            logger.info(f"[{self.game_id}] WS игры открыт, подключаемся…")
            client.emit_ack("game.connect", {
                "gameId":           self.game_id,
                "screenResolution": SCREEN,
            })

        def on_user_connected(data: dict):
            user = data.get("user", {})
            if user.get("_id") != self.user_id:
                return
            pet_wrap = data.get("pet", {})
            info = pet_wrap.get("info", {})
            self.pet_id  = str(info.get("_id", ""))
            self.pet_row = pet_wrap.get("row")
            logger.info(
                f"[{self.game_id}] Наш пет: {info.get('name')} "
                f"row={self.pet_row} id={self.pet_id}"
            )

        def on_sync(data: dict):
            # Координаты нашего пета
            for u in data.get("users", []):
                if u.get("user", {}).get("_id") != self.user_id:
                    continue
                coords = u.get("coordinates", {})
                self.pet_x = coords.get("x", self.pet_x)
                self.pet_y = coords.get("y", self.pet_y)

                pet_wrap = u.get("pet", {})
                if pet_wrap.get("row"):
                    self.pet_row = pet_wrap["row"]

                speed = u.get("speed", {})
                if speed:
                    self.speed_x = speed.get("x", self.speed_x)
                    self.speed_y = speed.get("y", self.speed_y)
                if "status" in u:
                    self.pet_status = u["status"]
                break

            self.last_update = data.get("lastUpdatedAt", self.last_update)

            # Барьеры — только в первом синке
            barriers_raw = (
                data.get("extra", {})
                    .get("mapInfo", {})
                    .get("barriers")
            )
            if barriers_raw is not None and self.pet_row is not None and not self.barriers:
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
            self.started = True
            logger.info(f"[{self.game_id}] 🏁 Игра началась!")

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
            self._done.set()
            if self.on_finish:
                self.on_finish(self.result)

        client.on("_open",                  on_open)
        client.on("engine.user.connected",  on_user_connected)
        client.on("engine.sync",            on_sync)
        client.on("engine.game.started",    on_started)
        client.on("engine.game.ended",      on_ended)

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


def get_my_pet_id() -> str | None:
    """Возвращает _id первого пета пользователя."""
    data = _post(f"{API_BASE}/user.getSelf", {})
    if not data:
        return None
    try:
        regions = data.get("user", {}).get("regions", [])
        for region in regions:
            pet = region.get("pet")
            if pet and pet.get("_id"):
                return str(pet["_id"])
    except Exception:
        pass
    return None


# ══════════════════════════════════════════════════════════
#  AutoPlayer — серия игр
# ══════════════════════════════════════════════════════════
class AutoPlayer:
    def __init__(self, mode: str = "race", count: int = 5, on_update=None):
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
        self.pet_id      = None

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
        self.pet_id  = get_my_pet_id()

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
