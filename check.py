import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

STATE_FILE = ".state.json"

HOME_URL = "https://www.koninklijke-serres-royales.be/fr/"
TICKETS_URL = "https://www.koninklijke-serres-royales.be/fr/tickets"

URLS = [HOME_URL, TICKETS_URL]

# Phrase actuelle indiquant que la vente n'est pas encore annoncée
NEEDLE_PHRASE = "La date exacte de la mise en vente des tickets sera communiquée ultérieurement"

HEARTBEAT_EVERY_SECONDS = 2 * 60 * 60  # 2h


def now_ts() -> int:
    return int(time.time())


def iso_now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
            return state, False
    except FileNotFoundError:
        return {"pages": {}, "last_heartbeat_ts": 0}, True


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def normalize_text(html: str) -> str:
    # Retire scripts/styles + balises, compacte les espaces
    text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def excerpt_around(text: str, needle: str, window: int = 160) -> str:
    """Petit extrait utile (debug) autour d'un needle, sinon début du texte."""
    idx = text.lower().find(needle.lower())
    if idx == -1:
        return text[: min(len(text), 300)]
    start = max(0, idx - window)
    end = min(len(text), idx + len(needle) + window)
    return text[start:end]


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
    headers = {"User-Agent": "royal-greenhouses-ticket-watch/2.0 (+contact: you)"}
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


def tickets_open_signal(tickets_text: str) -> bool:
    """
    Signal strict.
    On considère 'possiblement ouvert' si:
    - la phrase NEEDLE_PHRASE a disparu
    - ET la page contient des marqueurs "achat/panier/paiement/créneau"
    """
    t = tickets_text.lower()

    if NEEDLE_PHRASE.lower() in t:
        return False

    strong_markers = [
        "billetterie",
        "ajouter au panier",
        "panier",
        "checkout",
        "paiement",
        "acheter",
        "achat",
        "ticket",         # attention: présent souvent, mais combiné aux autres marqueurs
        "créneau",
        "horaire",
        "sélectionnez",
        "choisissez",
    ]

    # On demande au moins 2 marqueurs pour éviter un faux positif sur un mot isolé.
    hits = sum(1 for m in strong_markers if m in t)
    return hits >= 2


def main():
    state, is_first_run = load_state()
    pages = state.setdefault("pages", {})

    if is_first_run:
        tg_notify(
            "✅ Watcher Serres royales démarré.\n"
            f"Heure: {iso_now_utc()}\n"
            "Checks: toutes les 5 minutes. Heartbeat: toutes les 2h."
        )
        state["last_heartbeat_ts"] = now_ts()

    changed_urls = []
    tickets_changed = False
    tickets_text_latest = None

    for url in URLS:
        prev = pages.get(url, {})
        prev_headers = prev.get("headers", {})
        prev_hash = prev.get("last_hash", "")

        result = fetch(url, prev_headers=prev_headers)

        # Pas de changement côté serveur
        if result["status"] == 304:
            continue

        # Normalise + hash
        text = result["text"] or ""
        new_hash = sha256(text)

        # Met à jour l'état (headers + hash + petit extrait utile)
        pages[url] = {
            "headers": result["headers"],
            "last_hash": new_hash,
            "excerpt": excerpt_around(text, NEEDLE_PHRASE),
        }

        if new_hash != prev_hash:
            changed_urls.append(url)
            if url == TICKETS_URL:
                tickets_changed = True
                tickets_text_latest = text

    # Heartbeat après le check (preuve qu'il tourne vraiment)
    maybe_send_heartbeat(state)

    save_state(state)

    # IMPORTANT: pas d'alerte "vente ouverte" au tout premier run
    if is_first_run:
        return

    # Alerte uniquement si /tickets a changé ET si signal strict
    if tickets_changed and tickets_text_latest and tickets_open_signal(tickets_text_latest):
        tg_notify(
            "🎟️ Serres royales : la page /tickets a changé et la vente semble ouverte.\n"
            f"{TICKETS_URL}"
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
