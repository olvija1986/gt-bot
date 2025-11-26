#!/usr/bin/env python3
# coding: utf-8

"""
Gatto bot — последовательная обработка запросов через очередь.
Улучшения:
- единый worker, который выполняет задачи из очереди последовательно;
- безопасные сетевые вызовы с ретраями и логированием;
- таймаут выполнения каждой задачи (чтобы воркер не блокировался);
- защита от бесконечных циклов при применении эссенций;
- /health endpoint для проверки состояния.
"""

import os
import requests
import schedule
import time
from datetime import datetime
from threading import Thread, Lock
from queue import Queue, Empty
from flask import Flask, request, jsonify

# ================= Конфигурация =================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = None
try:
    CHAT_ID = int(os.environ.get("CHAT_ID")) if os.environ.get("CHAT_ID") else None
except Exception:
    CHAT_ID = None

TG_TIMEOUT = int(os.environ.get("TG_TIMEOUT", 3))
GATTO_TIMEOUT = int(os.environ.get("GATTO_TIMEOUT", 20))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", 3))
RETRY_DELAY = int(os.environ.get("RETRY_DELAY", 3))
APPLY_TIME = os.environ.get("APPLY_TIME", "03:00")
TASK_TIMEOUT = int(os.environ.get("TASK_TIMEOUT", 60))  # max seconds per task before marking timeout
MAX_ESSENCE_ATTEMPTS_PER_PET = int(os.environ.get("MAX_ESSENCE_ATTEMPTS_PER_PET", 50))

WEBHOOK_URL = os.environ.get("WEBHOOK_URL")  # e.g. https://app.up.railway.app/webhook
TG_TOKEN = os.environ.get("TG_TOKEN")        # токен для Gatto авторизации


HEADERS = {
    "accept": "application/json, text/plain, */*",
    "authorization": f"Bearer {TG_TOKEN}" if TG_TOKEN else "",
    "content-type": "application/json",
    "referer": "https://gatto.pw/",
    "user-agent": "Mozilla/5.0"
}

tg = requests.Session()
gatto = requests.Session()

# ================= Очередь задач и синхронизация =================
task_queue = Queue()
gatto_lock = Lock()  # дополнительная защита вокруг сетевых вызовов

# ================= Утилиты =================
def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    print(f"[{now()}] {msg}", flush=True)


def send_telegram(text):
    if not TELEGRAM_TOKEN or CHAT_ID is None:
        log("Telegram не настроен — пропускаю отправку.")
        return
    try:
        r = tg.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text},
            timeout=TG_TIMEOUT
        )
        if r.status_code != 200:
            log(f"Telegram send returned {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log(f"Telegram send error: {e}")

# ================= Сетевые вызовы к Gatto =================
def safe_request(url, payload=None):
    """
    Делает POST с ретраями. Возвращает объект requests.Response при status_code == 200,
    иначе возвращает None. Логирует содержимое ответа (усечённое).
    Все сетевые обращения защищены gatto_lock, чтобы избежать параллельных вызовов.
    """
    with gatto_lock:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                r = gatto.post(url, headers=HEADERS, json=payload or {}, timeout=GATTO_TIMEOUT)
            except Exception as e:
                log(f"Request exception to {url} (attempt {attempt}/{MAX_RETRIES}): {e}")
                r = None

            if r is None:
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)
                continue

            # Логируем ответ (HTTP-код и начало тела)
            body_snippet = (r.text[:500] + '...') if len(r.text) > 500 else r.text
            log(f"Response {r.status_code} from {url} (attempt {attempt}/{MAX_RETRIES}): {body_snippet}")

            if r.status_code == 200:
                # проверка на валидный JSON делается в вызывающем коде
                return r

            # Не 200 — пробуем ретрайить (если остались попытки)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)

        log(f"All {MAX_RETRIES} attempts failed for {url}")
        return None


def single_request(url, payload=None):
    """
    Небольшая обёртка, которая просто вызывает safe_request и игнорирует результат,
    но позволяет видеть лог.
    """
    safe_request(url, payload)

<<<<<<< HEAD

# ================= getAllStats Wrapper =================
=======
# ================= API-функции =================
>>>>>>> main
def get_all_stats():
    r = safe_request("https://api.nl.gatto.pw/pet.getAllStats")
    if not r:
        return None
    try:
        return r.json()
    except Exception as e:
        log(f"get_all_stats: JSON parse error: {e}")
        return None


def get_all_stats_before_action():
    """Вызов getAllStats + задержка (как в рабочем коде)."""
    get_all_stats()
    time.sleep(2)


# ================= API =================
def feed_cat():
    log("Кормление котов…")
<<<<<<< HEAD
    get_all_stats_before_action()  # ← обязательно
    safe_request("https://api.nl.gatto.pw/pet.feed", {"all": True})
    log("Кормление завершено ✓")
=======
    r = safe_request("https://api.nl.gatto.pw/pet.feed", {"all": True})
    if r:
        log("Кормление завершено ✓")
    else:
        log("Кормление: ошибка запроса")
        send_telegram("Кормление: ошибка запроса к Gatto.")
>>>>>>> main


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
<<<<<<< HEAD
    get_all_stats_before_action()  # ← обязательно
    safe_request("https://api.nl.gatto.pw/pet.play", {"all": True})

    pets = get_user_self()
    for pet in pets:
        single_request(
            "https://api.nl.gatto.pw/ads.watch",
            {"id": pet["_id"], "alias": "pet.play"}
        )
=======
    r = safe_request("https://api.nl.gatto.pw/pet.play", {"all": True})
    if not r:
        log("play_game: pet.play gave no response")
        send_telegram("Игры: ошибка pet.play.")
    pets = get_user_self()
    for pet in pets:
        single_request("https://api.nl.gatto.pw/ads.watch", {"id": pet["_id"], "alias": "pet.play"})
>>>>>>> main
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
    get_all_stats_before_action()  # ← обязательно
    r = safe_request("https://api.nl.gatto.pw/pet.getPrize", {"all": True})
    if not r:
        log("Призы не получены (ошибка).")
        send_telegram("Призы не получены (ошибка).")
        return

    try:
        data = r.json()
    except Exception as e:
<<<<<<< HEAD
        log(f"Ошибка parse JSON призов: {e}")
        send_telegram("Ошибка при получении призов.")

=======
        log(f"Ошибка при разборе JSON призов: {e}")
        send_telegram("Ошибка при получении призов (невалидный JSON).")
        return
    msg = format_prizes(data)
    send_telegram(f"🎁 Призы:\n{msg}")
>>>>>>> main
    log("Призы получены ✓")


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
        # иногда API возвращает {'data': [...]}
        if isinstance(arr, dict) and "data" in arr and isinstance(arr["data"], list):
            arr = arr["data"]
        return arr[0] if arr else None
    except Exception as e:
        log(f"get_first_essence: JSON parse error: {e}")
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
    except Exception as e:
        log(f"use_essence: JSON parse error: {e}")
        return None


def apply_essences_to_pets():
    """
    Применяем эссенции осторожно: для каждого питомца лимит попыток,
    чтобы не зациклиться на ошибочных ответах API.
    """
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
<<<<<<< HEAD
        current_level = pet["level"]

        while True:
=======
        start_level = pet["level"]
        attempts = 0
        while attempts < MAX_ESSENCE_ATTEMPTS_PER_PET:
            attempts += 1
>>>>>>> main
            ess = get_first_essence()
            if not ess:
                send_telegram(f"Эссенции закончились. Всего применено: {applied}")
                log("Эссенции закончились.")
                return
<<<<<<< HEAD

            res = use_essence(pet_id, ess["_id"])
=======
            essence_id = ess.get("_id") or ess.get("id")
            if not essence_id:
                log("get_first_essence вернул объект без id — пропускаю.")
                break
            res = use_essence(pet_id, essence_id)
>>>>>>> main
            if not res:
                log("use_essence вернуло None — прекращаю попытки для этого питомца.")
                break

            applied += 1
<<<<<<< HEAD
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
=======
            new_level = res.get("level", start_level)
            log(f"Pet {pet_id}: level {start_level} -> {new_level} (attempt {attempts})")
            if new_level >= 10:
                improved_pets += 1
                break
            start_level = new_level
        else:
            log(f"Превышен лимит попыток ({MAX_ESSENCE_ATTEMPTS_PER_PET}) для pet {pet_id}.")
    send_telegram(f"✨ Прокачка завершена.\nПрименено эссенций: {applied}\nПитомцев улучшено: {improved_pets}")
>>>>>>> main
    log("Эссенции применены ✓")

# ================= Worker =================
def _run_task_with_timeout(task_callable, timeout):
    """
    Запускает task_callable в отдельном потоке и ждёт join(timeout).
    Если задача ещё выполняется после timeout — помечаем таймаут и возвращаем False.
    Возвращаем True если задача завершилась (без учёта ошибок внутри неё).
    NOTE: Невозможно безопасно убить поток — надеемся на корректные timeouts в сетевых вызовах.
    """
    thr = Thread(target=task_callable, daemon=True)
    thr.start()
    thr.join(timeout)
    if thr.is_alive():
        return False
    return True

def worker():
    log("Worker запущен — готов обрабатывать задачи.")
    while True:
        try:
            task = task_queue.get(timeout=1)
        except Empty:
            continue
        try:
            log(f"Worker: взял задачу {getattr(task, '__name__', str(task))}")
            finished = _run_task_with_timeout(task, TASK_TIMEOUT)
            if not finished:
                log(f"Worker: задача {getattr(task, '__name__', str(task))} превысила таймаут {TASK_TIMEOUT}s и помечена как timed out.")
                send_telegram(f"Задача {getattr(task, '__name__', 'task')} превысила таймаут и была прервана (лог отмечен).")
        except Exception as e:
            log(f"Worker: исключение при выполнении задачи: {e}")
        finally:
            task_queue.task_done()

# ================= Scheduler (кладёт задачи в очередь) =================
def scheduler_thread():
<<<<<<< HEAD
    schedule.every(2).minutes.do(lambda: Thread(target=feed_cat).start())
    schedule.every(29).minutes.do(lambda: Thread(target=get_prize).start())
    schedule.every(60).minutes.do(lambda: Thread(target=play_game).start())
    schedule.every().day.at(APPLY_TIME).do(lambda: Thread(target=apply_essences_to_pets).start())
=======
    # планировщик кладёт функции в очередь; сами функции выполняются worker'ом последовательно
    schedule.every(2).minutes.do(lambda: task_queue.put(feed_cat))
    schedule.every(29).minutes.do(lambda: task_queue.put(get_prize))
    schedule.every(60).minutes.do(lambda: task_queue.put(play_game))
    schedule.every().day.at(APPLY_TIME).do(lambda: task_queue.put(apply_essences_to_pets))
>>>>>>> main

    log("Планировщик запущен")
    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            log(f"Scheduler exception: {e}")
        time.sleep(1)

# ================= Initial Cycle =================
def start_initial_cycle():
<<<<<<< HEAD
    log("Стартовый цикл…")
    get_all_stats_before_action()   # ← ВАЖНО
    feed_cat()
    get_prize()
    play_game()
    log("Стартовый цикл завершён ✓")

=======
    log("Стартовый цикл: ставлю задачи в очередь")
    task_queue.put(feed_cat)
    task_queue.put(get_prize)
    task_queue.put(play_game)
    log("Стартовый цикл завершён — задачи поставлены в очередь")
>>>>>>> main

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
<<<<<<< HEAD

    if chat_id != CHAT_ID:
=======
    if CHAT_ID is not None and chat_id != CHAT_ID:
>>>>>>> main
        return "ok"

    if text == "/essence":
<<<<<<< HEAD
        Thread(target=apply_essences_to_pets).start()
        send_telegram("Начинаю ⚡")

=======
        task_queue.put(apply_essences_to_pets)
        send_telegram("Начинаю применение эссенций (задача поставлена в очередь) ⚡")
>>>>>>> main
    return "ok"

@app.route("/health", methods=["GET"])
def health():
    """
    Возвращает базовую информацию:
    - size очереди
    - наличие TELEGRAM_TOKEN / TG_TOKEN
    """
    return jsonify({
        "ok": True,
        "queue_size": task_queue.qsize(),
        "telegram_configured": bool(TELEGRAM_TOKEN and CHAT_ID is not None),
        "gatto_token_present": bool(TG_TOKEN),
        "task_timeout_sec": TASK_TIMEOUT
    })

# ================= Start =================
<<<<<<< HEAD
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

=======
>>>>>>> main
if __name__ == "__main__":
    log("Бот запускается…")

    # Попытка установить webhook (если задан)
    if TELEGRAM_TOKEN and WEBHOOK_URL:
        try:
            wh = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook?url={WEBHOOK_URL}",
                timeout=TG_TIMEOUT
            )
            log(f"Webhook set: {wh.status_code} {wh.text[:200]}")
        except Exception as e:
            log(f"Ошибка установки webhook: {e}")
    else:
        log("Webhook/TELEGRAM_TOKEN/WEBHOOK_URL не настроены — пропускаю установку webhook.")

    # Запуск worker'а
    t_worker = Thread(target=worker, daemon=True)
    t_worker.start()

    # Стартовый цикл — кладём задачи в очередь (не выполняем синхронно)
    start_initial_cycle()

    # Планировщик в отдельном потоке (он только кладёт задачи)
    t_sched = Thread(target=scheduler_thread, daemon=True)
    t_sched.start()

    # Flask сервер
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
