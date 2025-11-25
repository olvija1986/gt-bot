#!/usr/bin/env python3
# coding: utf-8

import os
import requests
import schedule
import time
from datetime import datetime
from threading import Thread, Lock
from queue import Queue, Empty
from flask import Flask, request

# ================= Конфигурация =================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = None
try:
    CHAT_ID = int(os.environ.get("CHAT_ID")) if os.environ.get("CHAT_ID") else None
except Exception:
    CHAT_ID = None

TG_TIMEOUT = 3
GATTO_TIMEOUT = 20
MAX_RETRIES = 3
RETRY_DELAY = 3
APPLY_TIME = os.environ.get("APPLY_TIME", "03:00")

WEBHOOK_URL = os.environ.get("WEBHOOK_URL")  # https://app.up.railway.app/webhook
TG_TOKEN = os.environ.get("TG_TOKEN")        # токен для Gatto авторизации

HEADERS = {
    "accept": "application/json, text/plain, */*",
    "authorization": f"Bearer {TG_TOKEN}" if TG_TOKEN else "",
    "content-type": "application/json",
    "user-agent": "Mozilla/5.0",
    "referer": "https://gatto.pw/",
}

# Сессии
tg = requests.Session()
gatto = requests.Session()

# ================= Очередь задач и синхронизация =================
task_queue = Queue()
gatto_lock = Lock()  # дополнительная защита при обращении к сессии requests

# ================= Утилиты =================
def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def log(msg):
    print(f"[{now()}] {msg}", flush=True)

def send_telegram(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        log("Telegram not configured: can't send message.")
        return
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
    """
    Последовательный, логирующий и ретрающий POST-запрос к Gatto API.
    Возвращает объект response при успешном статусе 200, иначе None.
    Все обращения к сети защищены gatto_lock для дополнительной безопасности.
    """
    with gatto_lock:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                r = gatto.post(url, headers=HEADERS, json=payload or {}, timeout=GATTO_TIMEOUT)
            except Exception as e:
                log(f"Request exception to {url} (attempt {attempt}/{MAX_RETRIES}): {e}")
                r = None

            if r is None:
                # запрос не удался (исключение)
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)
                continue

            # Логируем statuse и начало тела
            snippet = (r.text[:500] + '...') if len(r.text) > 500 else r.text
            log(f"Response {r.status_code} from {url} (attempt {attempt}/{MAX_RETRIES}) -> {snippet}")

            # Если 200 — возвращаем
            if r.status_code == 200:
                return r

            # Для остальных кодов — подождать и ретрай
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
        # все попытки использованы
        log(f"All {MAX_RETRIES} attempts failed for {url}")
        return None

def single_request(url, payload=None):
    """
    Неблокирующий вариант POST: мы всё равно выполняем его последовательно (через очередь),
    но иногда хочется вызвать без обработки ответа.
    Тем не менее используем safe_request внутри, чтобы видеть лог.
    """
    safe_request(url, payload)

# ================= API =================
def get_all_stats():
    r = safe_request("https://api.nl.gatto.pw/pet.getAllStats")
    if not r:
        return None
    try:
        return r.json()
    except Exception as e:
        log(f"JSON parse error in get_all_stats: {e}")
        return None

def feed_cat():
    log("Кормление котов…")
    r = safe_request("https://api.nl.gatto.pw/pet.feed", {"all": True})
    if r:
        log("Кормление завершено ✓")
    else:
        log("Кормление: ошибка при запросе")

def get_user_self():
    r = safe_request("https://api.nl.gatto.pw/user.getSelf")
    if not r:
        log("get_user_self: нет ответа")
        return []
    try:
        data = r.json()
    except Exception as e:
        log(f"get_user_self: ошибка парсинга JSON: {e}")
        return []
    pets = []
    for region in data.get("user", {}).get("regions", []):
        pet = region.get("pet")
        if pet and "_id" in pet:
            pets.append(pet)
    return pets

def play_game():
    log("Игры с питомцами…")
    r = safe_request("https://api.nl.gatto.pw/pet.play", {"all": True})
    if not r:
        log("play_game: ошибка при pet.play")
    pets = get_user_self()
    for pet in pets:
        # ads.watch может не возвращать важный ответ, но мы всё равно вызовем через safe_request
        single_request("https://api.nl.gatto.pw/ads.watch", {"id": pet["_id"], "alias": "pet.play"})
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
        log("Призы не получены (ошибка).")
        send_telegram("Призы не получены (ошибка).")
        return
    try:
        data = r.json()
    except Exception as e:
        log(f"Ошибка при разборе JSON призов: {e}")
        send_telegram("Ошибка при получении призов (неправильный JSON).")
        return
    msg = format_prizes(data)
    send_telegram(f"🎁 Призы:\n{msg}")
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
        # Некоторые ответы могут содержать объект с полем 'data' или список напрямую
        if isinstance(arr, dict) and "data" in arr and isinstance(arr["data"], list):
            arr = arr["data"]
        return arr[0] if arr else None
    except Exception as e:
        log(f"get_first_essence: ошибка парсинга JSON: {e}")
        return None

def use_essence(pet_id, essence_id):
    r = safe_request("https://api.nl.gatto.pw/essence.activate",
                     {"petId": pet_id, "essenceId": essence_id})
    if not r:
        return None
    try:
        return r.json()
    except Exception as e:
        log(f"use_essence: ошибка парсинга JSON: {e}")
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
                log("Эссенции закончились.")
                return
            res = use_essence(pet_id, ess.get("_id") or ess.get("id"))
            if not res:
                log("use_essence вернуло None — прекращаем попытки на этом питомце.")
                break
            applied += 1
            new_level = res.get("level", start_level)
            if new_level >= 10:
                improved_pets += 1
                break
            start_level = new_level
    send_telegram(f"✨ Прокачка завершена.\nПрименено эссенций: {applied}\nПитомцев улучшено: {improved_pets}")
    log("Эссенции применены ✓")

# ================= Worker (выполняет задачи последовательно) =================
def worker():
    log("Worker запущен — готов обрабатывать задачи.")
    while True:
        try:
            task = task_queue.get(timeout=1)
        except Empty:
            continue
        try:
            # task — callable без аргументов
            try:
                task()
            except Exception as e:
                log(f"Ошибка выполнения задачи {task}: {e}")
        finally:
            task_queue.task_done()

# ================= Scheduler — кладёт задачи в очередь =================
def scheduler_thread():
    # планировщик кладёт функции в очередь; сами функции выполняются worker'ом
    schedule.every(2).minutes.do(lambda: task_queue.put(feed_cat))
    schedule.every(29).minutes.do(lambda: task_queue.put(get_prize))
    schedule.every(60).minutes.do(lambda: task_queue.put(play_game))
    schedule.every().day.at(APPLY_TIME).do(lambda: task_queue.put(apply_essences_to_pets))

    log("Планировщик запущен")
    while True:
        schedule.run_pending()
        time.sleep(1)

# ================= Initial Cycle =================
def start_initial_cycle():
    log("Стартовый цикл: ставлю задачи в очередь")
    task_queue.put(feed_cat)
    task_queue.put(get_prize)
    task_queue.put(play_game)
    log("Стартовый цикл завершён — задачи поставлены в очередь")

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
    if CHAT_ID is not None and chat_id != CHAT_ID:
        return "ok"
    if text == "/essence":
        task_queue.put(apply_essences_to_pets)
        send_telegram("Начинаю применение эссенций (задача поставлена в очередь) ⚡")
    return "ok"

# ================= Start =================
if __name__ == "__main__":
    log("Бот запускается…")

    # Установка вебхука (не критично для очереди, но полезно)
    if TELEGRAM_TOKEN and WEBHOOK_URL:
        try:
            wh = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook?url={WEBHOOK_URL}",
                timeout=TG_TIMEOUT
            )
            log(f"Webhook set: {wh.text}")
        except Exception as e:
            log(f"Ошибка установки webhook: {e}")
    else:
        log("Webhook или TELEGRAM_TOKEN не настроены — пропускаю установку webhook.")

    # Запуск worker'а
    t_worker = Thread(target=worker, daemon=True)
    t_worker.start()

    # Стартовый цикл помещает задачи в очередь
    start_initial_cycle()

    # Планировщик в отдельном потоке (он только кладет задачи)
    t_sched = Thread(target=scheduler_thread, daemon=True)
    t_sched.start()

    # Flask сервер
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
