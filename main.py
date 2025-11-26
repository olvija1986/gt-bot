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

WEBHOOK_URL = os.environ.get("WEBHOOK_URL")  # https://app.up.railway.app/webhook
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
    """Отправляет длинное сообщение, разбивая на блоки <4096 символов."""
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


def send_telegram(text):
    tg_send_long(text)


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
        single_request(
            "https://api.nl.gatto.pw/ads.watch",
            {"id": pet["_id"], "alias": "pet.play"}
        )
    log("Игры завершены ✓")


# ================= Форматирование дропа =================

VALID_CATEGORIES = [
    "soft", "ton", "gton", "eventCurrency", "experience",
    "resultSkins", "resultEggs", "resultEssence",
    "resultLootBox", "resultPremium", "resultPromotionPromocodes",
    "resultExtraItem", "resultMutagen", "resultFoods"
]

CATEGORY_EMOJI = {
    "resultEggs": "🥚 Яйца",
    "resultFoods": "🍖 Еда",
    "resultMutagen": "🧪 Мутаген",
    "resultSkins": "🎨 Скины",
    "resultEssence": "✨ Эссенции",
    "resultExtraItem": "📦 Доп. предметы",
    "resultPremium": "💎 Премиум",
    "resultPromotionPromocodes": "🎟 Промокоды",
    "resultLootBox": "🎁 Боксы (дроп)"
}

def format_loot_stats(stats, total_boxes):
    """Возвращает красивый текст Telegram с группировкой."""
    out = []
    out.append("📦 Финальная статистика\n-------------------------------------")
    out.append(f"🎁 Открытые боксы: {total_boxes}")
    out.append("-------------------------------------")

    # Валюта
    for key in ["soft", "ton", "gton", "eventCurrency", "experience"]:
        if key in stats and isinstance(stats[key], int):
            out.append(f"💰 {key}: {stats[key]}")
    out.append("-------------------------------------")

    # Предметы
    for cat, items in stats.items():
        if isinstance(items, dict) and items:
            title = CATEGORY_EMOJI.get(cat, f"📂 {cat}")
            out.append(f"{title}:")
            for name, count in items.items():
                out.append(f"  - {name} — {count}")
            out.append("-------------------------------------")

    return "\n".join(out)


# ================= /box командный процесс =================
# ================= Команда /box =================
def open_boxes():
    """Открывает все боксы на складе и отправляет финальную статистику."""
    send_telegram("📦 Начинаю открывать боксы…")
    lootboxes_stats = {
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

    # 1. Получаем боксы со склада
    r = safe_request(
        "https://api.nl.gatto.pw/warehouseGoods.getByLimit",
        {"type": "lootBoxes", "limit": 8, "offset": 0}
    )
    if not r:
        send_telegram("Ошибка: не удалось получить список боксов.")
        return

    try:
        boxes = r.json()
    except:
        send_telegram("Ошибка разбора JSON списка боксов.")
        return

    if not boxes:
        send_telegram("📦 На складе нет боксов.")
        return

    # 2. Открываем каждый бокс
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

        # 3. Суммируем валюту
        for cur in ["soft", "ton", "gton", "eventCurrency", "experience"]:
            lootboxes_stats[cur] += data.get(cur, 0)

        # 4. Добавляем массивы предметов
        for arr_field in [
            "resultSkins", "resultEggs", "resultEssence",
            "resultLootBox", "resultPremium", "resultPromotionPromocodes",
            "resultExtraItem", "resultMutagen", "resultFoods"
        ]:
            lootboxes_stats[arr_field].extend(data.get(arr_field, []))
        time.sleep(0.5)  # пауза между открытиями

    # 5. Форматируем финальный текст
    text_lines = ["📦 Финальная статистика", "-------------------------------------"]
    text_lines.append(f"🎁 Открыто боксов: {len(boxes)}")
    text_lines.append("-------------------------------------")

    # Валюта
    for cur in ["soft", "ton", "gton", "eventCurrency", "experience"]:
        text_lines.append(f"💰 {cur}: {lootboxes_stats[cur]}")
    text_lines.append("-------------------------------------")

    # Предметы по категориям
    def format_items(items, title_emoji, title_name):
        if not items:
            return ""
        lines = [f"{title_emoji} {title_name}:"]
        for item in items:
            name = item.get("name") or item.get("description") or "Unknown"
            lines.append(f"  - {name}")
        lines.append("-------------------------------------")
        return "\n".join(lines)

    text_lines.append(format_items(lootboxes_stats["resultSkins"], "🎨", "Скины"))
    text_lines.append(format_items(lootboxes_stats["resultEggs"], "🥚", "Яйца"))
    text_lines.append(format_items(lootboxes_stats["resultEssence"], "✨", "Эссенция"))
    text_lines.append(format_items(lootboxes_stats["resultLootBox"], "🎁", "Боксы (дроп)"))
    text_lines.append(format_items(lootboxes_stats["resultPremium"], "💎", "Премиум"))
    text_lines.append(format_items(lootboxes_stats["resultPromotionPromocodes"], "🎟", "Промокоды"))
    text_lines.append(format_items(lootboxes_stats["resultExtraItem"], "📦", "Доп. предметы"))
    text_lines.append(format_items(lootboxes_stats["resultMutagen"], "🧪", "Мутаген"))
    text_lines.append(format_items(lootboxes_stats["resultFoods"], "🍖", "Еда"))

    final_text = "\n".join([line for line in text_lines if line])

    # 6. Разбиваем на части ≤ 4000 символов
    max_len = 4000
    for i in range(0, len(final_text), max_len):
        send_telegram(final_text[i:i+max_len])

# ================= getPrize и Essences =================

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


# ===== Эссенции (без изменений) =====

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
    send_telegram(f"✨ Начинаю применение эссенций. Питомцев ниже 10 уровня: {len(pets)}")

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
                send_telegram(f"Эссенции закончились. Всего применено: {applied}")
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

    # ========== НОВАЯ КОМАНДА /box ==========
    if text.startswith("/box"):
        parts = text.split()
        Thread(target=open_boxes).start()
        send_telegram(f"📦 Открываю боксы…")

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
