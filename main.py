import os
import requests
import schedule
import time
from datetime import datetime
from threading import Thread
from flask import Flask, request

# ================= Конфигурация =================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = int(os.environ.get("CHAT_ID"))
TG_TIMEOUT = 3
GATTO_TIMEOUT = 20
MAX_RETRIES = 3
RETRY_DELAY = 3

WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
TG_TOKEN = os.environ.get("TG_TOKEN")

HEADERS = {
    "accept": "application/json, text/plain, */*",
    "authorization": f"Bearer {TG_TOKEN}",
    "content-type": "application/json",
    "referer": "https://gatto.pw/",
    "user-agent": "Mozilla/5.0"
}

tg = requests.Session()
gatto = requests.Session()


# ================= Утилиты =================
def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    print(f"[{now()}] {msg}")


def tg_send_long(text):
    limit = 3900
    parts = [text[i:i+limit] for i in range(0, len(text), limit)]
    for part in parts:
        try:
            tg.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": part},
                timeout=TG_TIMEOUT
            )
        except Exception as e:
            log(f"Telegram send error: {e}")


def tg_send_keyboard(text, keyboard):
    try:
        tg.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": text,
                "reply_markup": {"inline_keyboard": keyboard}
            },
            timeout=TG_TIMEOUT
        )
    except Exception as e:
        log(f"Telegram keyboard send error: {e}")


def tg_answer_callback(callback_query_id, text=""):
    try:
        tg.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text},
            timeout=TG_TIMEOUT
        )
    except Exception as e:
        log(f"answerCallbackQuery error: {e}")


def send_telegram(text):
    tg_send_long(text)


# ================= AutoPlayer состояние =================
_auto_player = None


def get_auto_player():
    return _auto_player


def set_auto_player(player):
    global _auto_player
    _auto_player = player


# ================= Callback от AutoPlayer → Telegram =================
def make_game_callback(mode: str, count: int):
    def on_update(event: str, data: dict):
        if event == "started":
            send_telegram(
                f"🎮 Авто-игра запущена!\n"
                f"Режим: {'🏁 Забег' if mode == 'race' else '🌊 Заплыв'}\n"
                f"Игр запланировано: {count}"
            )
        elif event == "game_done":
            place_emoji = {1: "🥇", 2: "🥈", 3: "🥉"}.get(data["place"], "🏅")
            send_telegram(
                f"{place_emoji} Игра {data['played']}/{data['total']}\n"
                f"Место: {data['place']} | Монет: {data['money']} | Опыт: {data['exp']}"
            )
        elif event == "finished":
            send_telegram(
                f"✅ Серия завершена!\n"
                f"-------------------------------------\n"
                f"🎮 Сыграно: {data['played']}\n"
                f"🥇 Побед: {data['wins']}\n"
                f"💰 Монет: {data['total_money']}\n"
                f"⭐ Опыта: {data['total_exp']}"
            )
            set_auto_player(None)
        elif event == "error":
            send_telegram(f"❌ Ошибка авто-игры: {data.get('msg', '?')}")
            set_auto_player(None)

    return on_update


# ================= Запуск / остановка авто-игры =================
def start_auto_play(mode: str, count: int):
    from game_player import AutoPlayer

    existing = get_auto_player()
    if existing and existing.is_running():
        send_telegram("⚠️ Авто-игра уже запущена! Сначала останови её — /stopgame")
        return

    player = AutoPlayer(
        mode=mode,
        count=count,
        on_update=make_game_callback(mode, count),
    )
    set_auto_player(player)

    if not player.start():
        send_telegram("❌ Не удалось запустить авто-игру")


def stop_auto_play():
    player = get_auto_player()
    if player and player.is_running():
        player.stop()
        send_telegram("🛑 Остановка запрошена. Текущая игра доиграется до конца.")
    else:
        send_telegram("ℹ️ Авто-игра не запущена.")


# ================= Запросы к Gatto =================
def safe_request(url, payload=None):
    for _ in range(MAX_RETRIES):
        try:
            r = gatto.post(url, headers=HEADERS, json=payload or {}, timeout=GATTO_TIMEOUT)
            if r.status_code == 200:
                return r
        except:
            pass
        time.sleep(RETRY_DELAY)
    return None


def single_request(url, payload=None):
    try:
        gatto.post(url, headers=HEADERS, json=payload or {}, timeout=GATTO_TIMEOUT)
    except:
        pass


def get_all_stats():
    return safe_request("https://api.nl.gatto.pw/pet.getAllStats")


def get_all_stats_before_action():
    get_all_stats()
    time.sleep(2)


# ================= API =================
def feed_cat():
    log("Кормление котов…")
    get_all_stats_before_action()
    safe_request("https://api.nl.gatto.pw/pet.feed", {"all": True})
    log("Кормление завершено ✓")


def get_user_self():
    r = safe_request("https://api.nl.gatto.pw/user.getSelf")
    if not r:
        return []
    try:
        data = r.json()
        pets = []
        for region in data.get("user", {}).get("regions", []):
            pet = region.get("pet")
            if pet and "_id" in pet:
                pets.append(pet)
        return pets
    except:
        return []


def play_game():
    log("Игры с питомцами…")
    get_all_stats_before_action()
    safe_request("https://api.nl.gatto.pw/pet.play", {"all": True})
    pets = get_user_self()
    for pet in pets:
        single_request(
            "https://api.nl.gatto.pw/ads.watch",
            {"id": pet["_id"], "alias": "pet.play"}
        )
    log("Игры завершены ✓")


# ================= Боксы =================
def open_boxes():
    from collections import defaultdict

    send_telegram("📦 Ищу боксы…")

    lootboxes_stats = {
        "soft": 0, "ton": 0, "gton": 0, "eventCurrency": 0, "experience": 0,
        "resultSkins": [], "resultEggs": [], "resultEssence": [],
        "resultLootBox": [], "resultPremium": [], "resultPromotionPromocodes": [],
        "resultExtraItem": [], "resultMutagen": [], "resultFoods": []
    }

    r = safe_request(
        "https://api.nl.gatto.pw/warehouseGoods.getByLimit",
        {"type": "lootBoxes", "limit": 50, "offset": 0}
    )
    if not r:
        send_telegram("❌ Ошибка: не удалось получить список боксов.")
        return

    try:
        boxes = r.json()
    except:
        send_telegram("❌ Ошибка разбора JSON списка боксов.")
        return

    if not boxes:
        send_telegram("📦 На складе нет боксов.")
        return

    for box in boxes:
        box_id = box.get("_id")
        if not box_id:
            continue
        resp = safe_request("https://api.nl.gatto.pw/lootBox.open", {"id": box_id})
        if not resp:
            continue
        try:
            data = resp.json()
        except:
            continue
        for cur in ["soft", "ton", "gton", "eventCurrency", "experience"]:
            lootboxes_stats[cur] += data.get(cur, 0)
        for arr in ["resultSkins", "resultEggs", "resultEssence", "resultLootBox",
                    "resultPremium", "resultPromotionPromocodes",
                    "resultExtraItem", "resultMutagen", "resultFoods"]:
            lootboxes_stats[arr].extend(data.get(arr, []))
        time.sleep(0.4)

    def get_item_key(item):
        t = item.get("itemType")
        if t == "egg": return f"{item.get('allowedRegion')}_{item.get('rarity')}"
        if t == "skin": return item.get("itemName")
        if t in ["food", "extraItem", "lootBox", "premiumItem", "promotionPromocode"]:
            return item.get("name")
        if t == "mutagen": return item.get("probability")
        if t == "essence": return item.get("type")
        return "unknown"

    def format_category(items, title, icon):
        if not items:
            return ""
        counts = defaultdict(int)
        for item in items:
            counts[get_item_key(item)] += item.get("count", 1)
        lines = [f"{icon} {title}"]
        for k, c in counts.items():
            lines.append(f"• {k}: {c}")
        return "\n".join(lines)

    categories = [c for c in [
        format_category(lootboxes_stats["resultSkins"], "Скины", "🎨"),
        format_category(lootboxes_stats["resultEggs"], "Яйца", "🥚"),
        format_category(lootboxes_stats["resultEssence"], "Эссенции", "✨"),
        format_category(lootboxes_stats["resultMutagen"], "Мутаген", "🧪"),
        format_category(lootboxes_stats["resultFoods"], "Еда", "🍖"),
        format_category(lootboxes_stats["resultExtraItem"], "Доп. предметы", "📦"),
        format_category(lootboxes_stats["resultLootBox"], "Лутбоксы", "🎁"),
        format_category(lootboxes_stats["resultPremium"], "Премиум", "💎"),
        format_category(lootboxes_stats["resultPromotionPromocodes"], "Промокоды", "🎟"),
    ] if c.strip()]

    text_parts = [
        "📦 Финальная статистика", "-------------------------------------",
        f"🎁 Открыто боксов: {len(boxes)}", "-------------------------------------",
        f"💰 soft: {lootboxes_stats['soft']}",
        f"💰 ton: {lootboxes_stats['ton']}",
        f"💰 gton: {lootboxes_stats['gton']}",
        f"💰 eventCurrency: {lootboxes_stats['eventCurrency']}",
        f"💰 experience: {lootboxes_stats['experience']}",
        "-------------------------------------",
    ] + categories

    send_telegram("\n".join(text_parts))


# ================= Daily Prize =================
def get_daily_prize():
    log("Получение ежедневного подарка…")
    get_all_stats_before_action()
    r = safe_request("https://api.nl.gatto.pw/user.getDailyPrize", {})
    if not r:
        send_telegram("❌ Не удалось получить ежедневный подарок.")
        return
    try:
        data = r.json()
        prize_type = data.get("type", "unknown")
        value = data.get("value", 0)
        rarity = data.get("rarity", "")
        name = data.get("name") or data.get("itemName") or data.get("allowedRegion") or prize_type
        msg = f"🎁 Ежедневный подарок: {name}"
        if rarity:
            msg += f" ({rarity})"
        if value:
            msg += f" x{value}"
        send_telegram(msg)
        log("Ежедневный подарок получен ✓")
    except Exception as e:
        send_telegram(f"❌ Ошибка при разборе подарка: {e}")


# ================= Prizes =================
def format_prizes(data):
    lines = []
    for f in ["soft", "ton", "gton", "eventCurrency", "experience"]:
        if data.get(f):
            lines.append(f"{f}: {data[f]}")
    for s in data.get("resultSkins", []):
        lines.append(f"Skin: {s.get('name')} ({s.get('rarity')})")
    for e in data.get("resultEggs", []):
        lines.append(f"Egg: {e.get('allowedRegion')} ({e.get('rarity')})")
    for ess in data.get("resultEssence", []):
        lines.append(f"Essence: {ess.get('type')}")
    return "\n".join(lines) if lines else "Нет призов"


def get_prize():
    log("Получение призов…")
    get_all_stats_before_action()
    r = safe_request("https://api.nl.gatto.pw/pet.getPrize", {"all": True})
    if not r:
        send_telegram("Призы не получены (ошибка).")
        return
    try:
        msg = format_prizes(r.json())
        send_telegram(f"🎁 Призы:\n{msg}")
    except:
        send_telegram("Ошибка при разборе призов.")
    log("Призы получены ✓")


# ================= Essences =================
def get_pets_not_level_10():
    pets = get_user_self()
    return [{"id": p["_id"], "level": p.get("level", 0)} for p in pets if p.get("level", 0) < 10]


def get_first_essence():
    r = safe_request(
        "https://api.nl.gatto.pw/warehouseGoods.getByLimit",
        {"type": "essences", "limit": 8, "offset": 0}
    )
    if not r:
        return None
    try:
        arr = r.json()
        return arr[0] if arr else None
    except:
        return None


def use_essence(pet_id, essence_id):
    r = safe_request(
        "https://api.nl.gatto.pw/essence.activate",
        {"petId": pet_id, "essenceId": essence_id}
    )
    if not r:
        return None
    try:
        return r.json()
    except:
        return None


def apply_essences_to_pets():
    pets = get_pets_not_level_10()
    send_telegram(f"✨ Питомцев ниже 10 уровня: {len(pets)}")
    if not pets:
        send_telegram("Нет питомцев ниже 10 уровня.")
        return

    applied = 0
    improved_pets = 0

    for pet in pets:
        pet_id = pet["id"]
        current_level = pet["level"]
        while True:
            ess = get_first_essence()
            if not ess:
                send_telegram(f"Эссенции закончились. Применено: {applied}")
                return
            res = use_essence(pet_id, ess["_id"])
            if not res:
                break
            applied += 1
            new_level = res.get("level", current_level)
            if new_level >= 10:
                improved_pets += 1
                break
            current_level = new_level

    send_telegram(
        f"✨ Прокачка завершена.\n"
        f"Применено эссенций: {applied}\n"
        f"Питомцев улучшено: {improved_pets}"
    )


# ================= Scheduler =================
def scheduler_thread():
    schedule.every(2).minutes.do(lambda: Thread(target=feed_cat).start())
    schedule.every(29).minutes.do(lambda: Thread(target=get_prize).start())
    schedule.every(60).minutes.do(lambda: Thread(target=play_game).start())
    schedule.every().day.at("02:00").do(lambda: Thread(target=get_daily_prize).start())
    log("Планировщик запущен")
    while True:
        schedule.run_pending()
        time.sleep(1)


def set_bot_commands():
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setMyCommands"
    commands = [
        {"command": "start",      "description": "Главное меню"},
        {"command": "race",       "description": "Забеги /race [кол-во]"},
        {"command": "swim",       "description": "Заплывы /swim [кол-во]"},
        {"command": "stopgame",   "description": "Остановить авто-игру"},
        {"command": "gamestatus", "description": "Статус авто-игры"},
        {"command": "box",        "description": "Открыть боксы"},
        {"command": "essence",    "description": "Применить эссенции"},
    ]
    try:
        resp = requests.post(url, json={"commands": commands})
        log(f"Команды меню обновлены: {resp.json()}")
    except Exception as e:
        log(f"Ошибка обновления команд меню: {e}")


# ================= Initial Cycle =================
def start_initial_cycle():
    log("Стартовый цикл…")
    get_all_stats_before_action()
    feed_cat()
    get_prize()
    play_game()
    get_daily_prize()
    log("Стартовый цикл завершён ✓")


# ================= Меню игры =================
def handle_game_command(mode: str, text: str):
    parts = text.strip().split()
    if len(parts) >= 2:
        try:
            count = int(parts[1])
            if 1 <= count <= 100:
                Thread(target=start_auto_play, args=(mode, count), daemon=True).start()
                return
            else:
                send_telegram("⚠️ Укажи число от 1 до 100.")
                return
        except ValueError:
            pass

    mode_label = "🏁 Забегов" if mode == "race" else "🌊 Заплывов"
    keyboard = [
        [
            {"text": "1",  "callback_data": f"play:{mode}:1"},
            {"text": "5",  "callback_data": f"play:{mode}:5"},
            {"text": "10", "callback_data": f"play:{mode}:10"},
        ],
        [
            {"text": "20",  "callback_data": f"play:{mode}:20"},
            {"text": "50",  "callback_data": f"play:{mode}:50"},
            {"text": "100", "callback_data": f"play:{mode}:100"},
        ],
        [{"text": "❌ Отмена", "callback_data": "play:cancel:0"}],
    ]
    tg_send_keyboard(f"Сколько {mode_label} сыграть?\nИли: /{mode} [число]", keyboard)


# ================= Flask =================
app = Flask(__name__)


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    if not data:
        return "ok"

    # Нажатие кнопки
    if "callback_query" in data:
        cq     = data["callback_query"]
        cq_id  = cq.get("id")
        cq_data = cq.get("data", "")
        chat_id = cq.get("message", {}).get("chat", {}).get("id")

        tg_answer_callback(cq_id)

        if chat_id != CHAT_ID:
            return "ok"

        parts = cq_data.split(":")
        if parts[0] == "play":
            mode = parts[1] if len(parts) > 1 else ""
            if mode == "cancel":
                send_telegram("Отменено.")
            elif mode in ("race", "swim") and len(parts) == 3:
                try:
                    count = int(parts[2])
                    Thread(target=start_auto_play, args=(mode, count), daemon=True).start()
                except ValueError:
                    send_telegram("❌ Ошибка.")
        return "ok"

    # Текстовые команды
    if "message" not in data:
        return "ok"

    msg     = data["message"]
    chat_id = msg.get("chat", {}).get("id")
    text    = msg.get("text", "")

    if chat_id != CHAT_ID:
        return "ok"

    if text == "/start":
        keyboard = [
            [
                {"text": "🏁 Забег (5)",  "callback_data": "play:race:5"},
                {"text": "🌊 Заплыв (5)", "callback_data": "play:swim:5"},
            ],
            [
                {"text": "📦 Боксы",     "callback_data": "play:boxes:0"},
                {"text": "✨ Эссенции",  "callback_data": "play:essence:0"},
            ],
        ]
        tg_send_keyboard(
            "🐾 Gatto Bot\n\n"
            "/race [N] — забеги\n"
            "/swim [N] — заплывы\n"
            "/stopgame — остановить\n"
            "/gamestatus — статус",
            keyboard
        )

    elif text.startswith("/race"):
        handle_game_command("race", text)

    elif text.startswith("/swim"):
        handle_game_command("swim", text)

    elif text == "/stopgame":
        Thread(target=stop_auto_play, daemon=True).start()

    elif text == "/gamestatus":
        player = get_auto_player()
        if player and player.is_running():
            mode_label = "🏁 Забег" if player.mode == "race" else "🌊 Заплыв"
            send_telegram(
                f"▶️ Авто-игра активна\n"
                f"Режим: {mode_label}\n"
                f"Сыграно: {player.played}/{player.target_count}\n"
                f"Побед: {player.wins}\n"
                f"Монет: {player.total_money}\n"
                f"Опыта: {player.total_exp}"
            )
        else:
            send_telegram("⏹ Авто-игра не запущена.")

    elif text == "/essence":
        Thread(target=apply_essences_to_pets, daemon=True).start()
        send_telegram("Начинаю ⚡")

    elif text.startswith("/box"):
        Thread(target=open_boxes, daemon=True).start()
        send_telegram("📦 Открываю боксы…")

    return "ok"


# ================= Start =================
log("Бот запускается…")

try:
    wh = requests.get(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook?url={WEBHOOK_URL}"
    )
    log(f"Webhook set: {wh.text}")
except Exception as e:
    log(f"Ошибка установки webhook: {e}")

set_bot_commands()

Thread(target=start_initial_cycle, daemon=True).start()
Thread(target=scheduler_thread, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
