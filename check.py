import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

STATE_FILE = ".state.json"

URLS = [
    "https://www.koninklijke-serres-royales.be/fr/",
    "https://www.koninklijke-serres-royales.be/fr/tickets",
]

NEEDLE_PHRASE = "La date exacte de la mise en vente des tickets sera communiquée ultérieurement"
HEARTBEAT_EVERY_SECONDS = 2 * 60 * 60  # 2h

def now_ts() -> int:
    return int(time.time())

def iso_now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
            return state, False  # exists
    except FileNotFoundError:
        # state initial
        return {"pages": {}, "last_heartbeat_ts": 0}, True  # first run

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def normalize_text(html: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def tg_notify(msg: str):
    token = os.environ["TG_BOT_TOKEN"]
    chat_id = os.environ["TG_CHAT_ID"]
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": msg, "disable_web_page_preview": True},
        timeout=20,
    )
    r.raise_for_status()

def fetch(url: str, prev_headers: dict):
    headers = {"User-Agent": "royal-greenhouses-ticket-watch/1.1 (+contact: you)"}
    if prev_headers.get("etag"):
        headers["If-None-Match"] = prev_headers["etag"]
    if prev_headers.get("last_modified"):
        headers["If-Modified-Since"] = prev_headers["last_modified"]

    r = requests.get(url, headers=headers, timeout=25)

    if r.status_code == 304:
        return {"status": 304, "headers": prev_headers, "text": None}

    r.raise_for_status()

    new_headers = {
        "etag": r.headers.get("ETag"),
        "last_modified": r.headers.get("Last-Modified"),
    }
    text = normalize_text(r.text)
    return {"status": r.status_code, "headers": new_headers, "text": text}

def maybe_send_heartbeat(state):
    ts = now_ts()
    last = int(state.get("last_heartbeat_ts", 0) or 0)
    if (ts - last) >= HEARTBEAT_EVERY_SECONDS:
        tg_notify(f"🟢 Watcher Serres royales : toujours ON ({iso_now_utc()}).")
        state["last_heartbeat_ts"] = ts

def main():
    state, is_first_run = load_state()
    pages = state.setdefault("pages", {})

    # 1) message de démarrage au tout premier run
    if is_first_run:
        tg_notify(
            "✅ Watcher Serres royales démarré.\n"
            f"Heure: {iso_now_utc()}\n"
            "Je check toutes les 5 minutes et je t’envoie un heartbeat toutes les 2h."
        )
        # initialise heartbeat pour éviter une double notif immédiate
        state["last_heartbeat_ts"] = now_ts()

    any_signal = False
    changes = []

    for url in URLS:
        prev = pages.get(url, {})
        result = fetch(url, prev_headers=prev.get("headers", {}))

        if result["status"] == 304:
            continue

        pages[url] = {"headers": result["headers"], "last_text": prev.get("last_text")}

        if result["text"] is None:
            continue

        if result["text"] != prev.get("last_text"):
            pages[url]["last_text"] = result["text"]
            changes.append(url)

            phrase_gone = (NEEDLE_PHRASE not in result["text"])
            looks_open = any(
                k in result["text"].lower()
                for k in ["réserver", "reservation", "book", "available", "disponible"]
            )
            if phrase_gone or looks_open:
                any_signal = True

    # 2) heartbeat toutes les 2h
    # (on le fait après les checks, comme ça on sait qu’il est vraiment “vivant”)
    maybe_send_heartbeat(state)

    save_state(state)

    # notif ouverture potentielle
    if changes and any_signal:
        tg_notify(
            "🎟️ Serres royales : changement détecté (vente possiblement ouverte) !\n"
            + "\n".join(changes)
        )

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
