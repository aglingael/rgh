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
            return json.load(f), False
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


def extract_links(html: str):
    # Extraire les href sans dépendances externes
    # (peut rater certains cas JS, mais suffisant en pratique)
    hrefs = re.findall(r'href\s*=\s*["\']([^"\']+)["\']', html, flags=re.I)
    # Nettoyage léger
    out = []
    for h in hrefs:
        h = h.strip()
        if not h or h.startswith("#") or h.lower().startswith("javascript:"):
            continue
        out.append(h)
    return out


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
    headers = {"User-Agent": "royal-greenhouses-ticket-watch/3.0 (+contact: you)"}
    if prev_headers.get("etag"):
        headers["If-None-Match"] = prev_headers["etag"]
    if prev_headers.get("last_modified"):
        headers["If-Modified-Since"] = prev_headers["last_modified"]

    r = requests.get(url, headers=headers, timeout=25, allow_redirects=True)

    # 304 => inchangé
    if r.status_code == 304:
        return {"status": 304, "final_url": url, "headers": prev_headers, "text": None, "links": None}

    # 404/410 => page absente : ce n'est PAS une erreur pour nous
    if r.status_code in (404, 410):
        new_headers = {
            "etag": r.headers.get("ETag"),
            "last_modified": r.headers.get("Last-Modified"),
        }
        return {"status": r.status_code, "final_url": r.url, "headers": new_headers, "text": "", "links": []}

    # Autres erreurs => on remonte (ça te permettra de voir dans les logs Actions)
    r.raise_for_status()

    new_headers = {
        "etag": r.headers.get("ETag"),
        "last_modified": r.headers.get("Last-Modified"),
    }
    html = r.text
    text = normalize_text(html)
    links = extract_links(html)
    return {"status": r.status_code, "final_url": r.url, "headers": new_headers, "text": text, "links": links}


def maybe_send_heartbeat(state):
    ts = now_ts()
    last = int(state.get("last_heartbeat_ts", 0) or 0)
    if (ts - last) >= HEARTBEAT_EVERY_SECONDS:
        tg_notify(f"🟢 Watcher Serres royales : toujours ON ({iso_now_utc()}).")
        state["last_heartbeat_ts"] = ts


def looks_like_ticket_link(href: str) -> bool:
    h = href.lower()
    # liens relatifs ou absolus
    # On capte "tickets", "billetterie", "reservation", et aussi les plateformes externes éventuelles.
    keywords = ["ticket", "billetterie", "reservation", "réservation", "book", "booking", "checkout", "shop"]
    return any(k in h for k in keywords)


def absolute_url(base: str, href: str) -> str:
    # mini-resolve pour les liens relatifs
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        # base ex: https://domain/fr/ => on garde scheme+host
        m = re.match(r"^(https?://[^/]+)", base)
        return (m.group(1) if m else base) + href
    # relatif simple
    return base.rstrip("/") + "/" + href.lstrip("/")


def main():
    state, is_first_run = load_state()
    pages = state.setdefault("pages", {})

    if is_first_run:
        tg_notify(
            "✅ Watcher Serres royales démarré.\n"
            f"Heure: {iso_now_utc()}\n"
            "Checks: toutes les 5 minutes. Heartbeat: toutes les 2h.\n"
            "Je gère aussi le cas où /tickets disparaît puis réapparaît."
        )
        state["last_heartbeat_ts"] = now_ts()

    changed_urls = []
    # Pour détection "tickets revient"
    tickets_prev_status = int(pages.get(TICKETS_URL, {}).get("last_status", 0) or 0)
    tickets_now_status = None
    tickets_now_final_url = None
    home_now_text = None
    home_now_links = None

    for url in URLS:
        prev = pages.get(url, {})
        prev_headers = prev.get("headers", {})
        prev_hash = prev.get("last_hash", "")
        prev_links_hash = prev.get("last_links_hash", "")
        prev_status = int(prev.get("last_status", 0) or 0)

        result = fetch(url, prev_headers=prev_headers)

        # 304 => rien à faire (mais on garde status/headers)
        if result["status"] == 304:
            continue

        text = result["text"] if result["text"] is not None else ""
        links = result["links"] if result["links"] is not None else []

        new_hash = sha256(text)
        links_joined = "\n".join(sorted(set(links)))
        new_links_hash = sha256(links_joined)

        pages[url] = {
            "headers": result["headers"],
            "last_status": result["status"],
            "final_url": result.get("final_url", url),
            "last_hash": new_hash,
            "last_links_hash": new_links_hash,
        }

        # mémos pour la logique
        if url == HOME_URL:
            home_now_text = text
            home_now_links = links
        if url == TICKETS_URL:
            tickets_now_status = result["status"]
            tickets_now_final_url = result.get("final_url", TICKETS_URL)

        # changement si status change OU contenu change OU liens changent
        if result["status"] != prev_status or new_hash != prev_hash or new_links_hash != prev_links_hash:
            changed_urls.append(url)

    maybe_send_heartbeat(state)
    save_state(state)

    # 1) pas d'alertes “métier” au tout premier run
    if is_first_run:
        return

    # 2) Notif quand /tickets réapparaît
    #    Cas: avant 404/410 (ou 0), maintenant 200
    if tickets_now_status is not None:
        was_missing = tickets_prev_status in (0, 404, 410)
        is_back = tickets_now_status == 200
        if was_missing and is_back:
            tg_notify(
                "🎫 La page billetterie semble de retour !\n"
                f"URL: {tickets_now_final_url or TICKETS_URL}"
            )

    # 3) Détection d’un nouveau lien “tickets/billetterie” sur la home (même si URL change)
    if home_now_links:
        candidate_links = []
        for h in home_now_links:
            if looks_like_ticket_link(h):
                candidate_links.append(absolute_url(HOME_URL, h))

        # dédoublonne et limite
        candidate_links = sorted(set(candidate_links))[:10]

        # On alerte seulement si la home a changé ET qu’on voit des liens candidats
        # Et qu'on n'est pas déjà dans le cas "tickets revient" (éviter double notif)
        if candidate_links and (HOME_URL in changed_urls):
            # Bonus: si la phrase "communiquée ultérieurement" a disparu de la home, c'est un gros signal
            phrase_gone_on_home = home_now_text and (NEEDLE_PHRASE.lower() not in home_now_text.lower())

            if phrase_gone_on_home:
                tg_notify(
                    "🎟️ Signal fort : la phrase 'mise en vente communiquée ultérieurement' a disparu de la page d’accueil.\n"
                    "Liens billetterie potentiels détectés :\n" + "\n".join(candidate_links)
                )
            else:
                # signal plus faible, mais utile vu que /tickets peut changer d’URL
                tg_notify(
                    "🔎 Nouveau(x) lien(s) de billetterie détecté(s) sur la page d’accueil (à vérifier) :\n"
                    + "\n".join(candidate_links)
                )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
