import os
from flask import Flask, request
import requests
import threading
import time
from datetime import datetime
import schedule

# ============= CONFIG =============
# ============= CONFIG =============
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = int(os.environ.get("CHAT_ID"))
TG_TOKEN = os.environ.get("TG_TOKEN")

WEBHOOK_URL = "https://web-production-dd39a.up.railway.app/webhook"

HEADERS = {
    "accept": "application/json, text/plain, */*",
    "authorization": f"Bearer {TG_TOKEN}",
    "content-type": "application/json",
}


# ============= UTILS =============
def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def send_telegram(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text},
            timeout=10
        )
    except:
        pass


# ============= API HELPERS =============
def safe_request(url, payload=None):
    for attempt in range(3):
        try:
            r = requests.post(url, headers=HEADERS, json=payload or {}, timeout=10)
            if r.status_code == 200:
                return r
        except:
            pass
        time.sleep(2)
    return None


# ======= YOUR BOT ACTIONS (feed, prize, game, etc.) =======
def feed_cat():
    log("Feeding cats...")
    safe_request("https://api.nl.gatto.pw/pet.feed", {"all": True})
    log("Feed done ✓")


def play_game():
    log("Play game...")
    safe_request("https://api.nl.gatto.pw/pet.play", {"all": True})
    log("Game done ✓")


def get_prize():
    log("Get prize...")
    r = safe_request("https://api.nl.gatto.pw/pet.getPrize", {"all": True})
    if not r:
        send_telegram("Ошибка получения призов")
        return
    send_telegram("🎁 Призы получены")
    log("Prize done ✓")


def apply_essences_to_pets():
    send_telegram("✨ Запускаю применение эссенций…")
    log("Applying essences…")
    # твоя реализация
    send_telegram("✨ Эссенции применены ✓")


# ============= FLASK APP (WEBHOOK) =============
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
        threading.Thread(target=apply_essences_to_pets).start()
        send_telegram("Начинаю ⚡")

    return "ok"


# ============= SCHEDULER =============
def scheduler_thread():
    schedule.every(2).minutes.do(feed_cat)
    schedule.every(30).minutes.do(get_prize)
    schedule.every(60).minutes.do(play_game)

    while True:
        schedule.run_pending()
        time.sleep(1)


# ============= STARTUP =============
def set_webhook():
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"
    requests.get(url, params={"url": WEBHOOK_URL})


if __name__ == "__main__":
    log("Starting bot...")

    set_webhook()
    threading.Thread(target=scheduler_thread, daemon=True).start()

    app.run(host="0.0.0.0", port=8080)
