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
APPLY_TIME = "03:00"

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

def send_telegram(text):
    try:
        tg.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text},
            timeout=TG_TIMEOUT
        )
    except Exception as e:
        log(f"Telegram send error: {e}")

# ================= Запросы =================
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

# ================= getAllStats Wrapper =================
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
        single_request("https://api.nl.gatto.pw/ads.watch", {"id": pet["_id"], "alias": "pet.play"})
    log("Игры завершены ✓")

# ================= Формат наград =================
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
        data = r.json()
        send_telegram(f"🎁 Призы:\n{format_prizes(data)}")
    except:
        send_telegram("Ошибка при получении призов.")

# ================= Эссенции =================
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
    return r.json() if r else None

def apply_essences_to_pets():
    pets = get_pets_not_level_10()
    send_telegram(f"✨ Начинаю применение эссенций. Питомцев ниже 10 уровня: {len(pets)}")
    if not pets:
        send_telegram("Нет питомцев ниже 10 уровня.")
        return

    applied = 0
    improved = 0

    for pet in pets:
        pet_id = pet["id"]
        level = pet["level"]

        while True:
            ess = get_first_essence()
            if not ess:
                send_telegram(f"Эссенции закончились. Всего применено: {applied}")
                return

            res = use_essence(pet_id, ess["_id"])
            if not res:
                break

            applied += 1
            new_level = res.get("level", level)

            if new_level >= 10:
                improved += 1
                break

            level = new_level

    send_telegram(
        f"✨ Прокачка завершена.\n"
        f"Применено эссенций: {applied}\n"
        f"Питомцев улучшено: {improved}"
    )

# ================= Боксы =================
def open_all_boxes():
    log("Открываю все боксы…")

    stats = {
        "soft": 0,
        "ton": 0,
        "gton": 0,
        "eventCurrency": 0,
        "experience": 0,
        "resultSkins": [],
        "resultEggs": [],
        "resultEssence": [],
        "resultLootBox": [],
        "resultPremium": [],
        "resultPromotionPromocodes": [],
        "resultExtraItem": [],
        "resultMutagen": [],
        "resultFoods": []
    }

    offset = 0
    limit = 8
    opened_count = 0
    categories = stats.keys()

    while True:
        resp = safe_request(
            "https://api.nl.gatto.pw/warehouseGoods.getByLimit",
            {"type": "lootBoxes", "limit": limit, "offset": offset}
        )
        if not resp:
            break

        boxes = resp.json()
        if not boxes:
            break

        for box in boxes:
            opened_count += 1
            drop = safe_request(
                "https://api.nl.gatto.pw/lootBox.open",
                {"id": box.get("_id")}
            )
            if not drop:
                continue

            drop = drop.json()

            for cat in categories:
                val = drop.get(cat)
                if isinstance(val, int):
                    stats[cat] += val
                elif isinstance(val, list):
                    stats[cat].extend(val)

        offset += limit

    send_telegram(f"📦 Открыто боксов: {opened_count}\n{stats}")
    log("Боксы завершены ✓")

# ================= Scheduler =================
def scheduler_thread():
    schedule.every(2).minutes.do(lambda: Thread(target=feed_cat).start())
    schedule.every(29).minutes.do(lambda: Thread(target=get_prize).start())
    schedule.every(60).minutes.do(lambda: Thread(target=play_game).start())
    schedule.every().day.at(APPLY_TIME).do(lambda: Thread(target=apply_essences_to_pets).start())

    log("Планировщик запущен")
    while True:
        schedule.run_pending()
        time.sleep(1)

# ================= Initial Cycle =================
def start_initial_cycle():
    log("Стартовый цикл…")
    get_all_stats_before_action()
    feed_cat()
    get_prize()
    play_game()
    log("Стартовый цикл завершён ✓")

# ================= Flask =================
app = Flask(__name__)

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    if not data or "message" not in data:
        return "ok"

    msg = data["message"]
    chat_id = msg.get("chat", {}).get("id")
    text = msg.get("text", "")

    if chat_id != CHAT_ID:
        return "ok"

    if text == "/essence":
        Thread(target=apply_essences_to_pets).start()
        send_telegram("Начинаю ⚡")

    elif text == "/box":
        send_telegram("📦 Начинаю открывать боксы…")
        Thread(target=open_all_boxes).start()

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

Thread(target=start_initial_cycle, daemon=True).start()
Thread(target=scheduler_thread, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
