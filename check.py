import json
import os
import re
import sys
import requests

STATE_FILE = ".state.json"

URLS = [
    "https://www.koninklijke-serres-royales.be/fr/",
    "https://www.koninklijke-serres-royales.be/fr/tickets",
]

NEEDLE_PHRASE = "La date exacte de la mise en vente des tickets sera communiquée ultérieurement"

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"pages": {}}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def normalize_text(html: str) -> str:
    # simple "strip tags" sans dépendances externes
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
    headers = {"User-Agent": "royal-greenhouses-ticket-watch/1.0 (+contact: you)"}
    if prev_headers.get("etag"):
        headers["If-None-Match"] = prev_headers["etag"]
    if prev_headers.get("last_modified"):
        headers["If-Modified-Since"] = prev_headers["last_modified"]

    r = requests.get(url, headers=headers, timeout=25)
    # 304 => pas de changement, on ne retélécharge pas plus
    if r.status_code == 304:
        return {"changed": False, "status": 304, "headers": prev_headers, "text": None}

    r.raise_for_status()

    new_headers = {
        "etag": r.headers.get("ETag"),
        "last_modified": r.headers.get("Last-Modified"),
    }
    text = normalize_text(r.text)
    return {"changed": True, "status": r.status_code, "headers": new_headers, "text": text}

def main():
    state = load_state()
    pages = state.setdefault("pages", {})

    any_signal = False
    changes = []

    for url in URLS:
        prev = pages.get(url, {})
        result = fetch(url, prev_headers=prev.get("headers", {}))

        if result["status"] == 304:
            continue

        # sauvegarde headers même si texte inchangé (au cas où)
        pages[url] = {"headers": result["headers"], "last_text": prev.get("last_text")}

        if result["text"] is None:
            continue

        # Détecte un vrai changement de contenu
        if result["text"] != prev.get("last_text"):
            pages[url]["last_text"] = result["text"]
            changes.append(url)

            phrase_gone = (NEEDLE_PHRASE not in result["text"])
            looks_open = any(k in result["text"].lower() for k in ["réserver", "reservation", "book", "available", "disponible"])
            if phrase_gone or looks_open:
                any_signal = True

    save_state(state)

    # Notif seulement si on a un changement + un signal "ouverture possible"
    if changes and any_signal:
        tg_notify(
            "🎟️ Serres royales : changement détecté (vente possiblement ouverte) !\n"
            + "\n".join(changes)
        )

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # En cas d’erreur, on log juste (pas de notif spam)
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
