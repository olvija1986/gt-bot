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

WEBHOOK_URL = os.environ.get("WEBHOOK_URL")  # Например https://your-app.up.railway.app/webhook
TG_TOKEN = os.environ.get("TG_TOKEN")        # Для authorization в Gatto

HEADERS = {
    "accept": "application/json, text/plain, */*",
    "authorization": f"Bearer {TG_TOKEN}",
    "content-type": "application/json",
    "user-agent": "Mozilla/5.0",
    "referer": "https://gatto.pw/",
}

# Сессии
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


# ================= API =================
def get_all_stats():
    return safe_request("https://api.nl.gatto.pw/pet.getAllStats")

def feed_cat():
    log("Кормление котов…")
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
    safe_request("https://api.nl.gatto.pw/pet.play", {"all": True})
    pets = get_user_self()
    for pet in pets:
        single_request("https://api.nl.gatto.pw/ads.watch",
                       {"id": pet["_id"], "alias": "pet.play"})
    log("Игры завершены ✓")

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
    r = safe_request("https://api.nl.gatto.pw/pet.getPrize", {"all": True})
    if not r:
        log("Призы не получены (ошибка)")
        send_telegram("Призы не получены (ошибка).")
        return
    try:
        data = r.json()
        msg = format_prizes(data)
        send_telegram(f"🎁 Призы:\n{msg}")
    except Exception as e:
        log(f"Ошибка при разборе JSON призов: {e}")
        send_telegram("Ошибка при получении призов.")
    log("Призы получены ✓")

def get_pets_not_level_10():
    pets = get_user_self()
    return [{"id": p["_id"], "level": p.get("level", 0)} for p in pets if p.get("level", 0) < 10]

def get_first_essence():
    r = safe_request("https://api.nl.gatto.pw/warehouseGoods.getByLimit",
                     {"type": "essences", "limit": 8, "offset": 0})
    if not r:
        return None
    try:
        arr = r.json()
        return arr[0] if arr else None
    except:
        return None

def use_essence(pet_id, essence_id):
    r = safe_request("https://api.nl.gatto.pw/essence.activate",
                     {"petId": pet_id, "essenceId": essence_id})
    if not r:
        return None
    try:
        return r.json()
    except:
        return None

def apply_essences_to_pets():
    pets = get_pets_not_level_10()
    send_telegram(f"✨ Начинаю применение эссенций. Питомцев ниже 10 уровня: {len(pets)}")
    log("Применение эссенций…")
    if not pets:
        send_telegram("Нет питомцев ниже 10 уровня.")
        return
    applied = 0
    improved_pets = 0
    for pet in pets:
        pet_id = pet["id"]
        start_level = pet["level"]
        while True:
            ess = get_first_essence()
            if not ess:
                send_telegram(f"Эссенции закончились. Всего применено: {applied}")
                return
            res = use_essence(pet_id, ess["_id"])
            if not res:
                break
            applied += 1
            new_level = res.get("level", start_level)
            if new_level >= 10:
                improved_pets += 1
                break
            start_level = new_level
    send_telegram(f"✨ Прокачка завершена.\nПрименено эссенций: {applied}\nПитомцев улучшено: {improved_pets}")
    log("Эссенции применены ✓")


# ================= Telegram Listener =================
def telegram_listener():
    log("Telegram listener started")
    last_update = 0
    while True:
        try:
            r = tg.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"offset": last_update + 1, "timeout": 2},
                timeout=TG_TIMEOUT
            )
            if r.status_code != 200:
                time.sleep(1)
                continue
            data = r.json()
            for upd in data.get("result", []):
                last_update = upd["update_id"]
                msg = upd.get("message", {})
                if not msg:
                    continue
                chat_id = msg.get("chat", {}).get("id")
                text = (msg.get("text") or "").strip().lower()
                if chat_id != CHAT_ID:
                    continue
                if text == "/essence":
                    send_telegram("Начинаю применение эссенций…")
                    Thread(target=apply_essences_to_pets).start()
        except Exception as e:
            log(f"Listener exception: {e}")
        time.sleep(0.2)


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
    return "ok"


# ================= Start everything =================
log("Бот запускается…")
# стартовый цикл
Thread(target=start_initial_cycle, daemon=True).start()
# Listener
Thread(target=telegram_listener, daemon=True).start()
# Scheduler
Thread(target=scheduler_thread, daemon=True).start()

# Flask
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
