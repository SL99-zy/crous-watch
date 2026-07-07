#!/usr/bin/env python3
"""
crous-watch — Monitor CROUS student housing (trouverunlogement.lescrous.fr)
and send a Telegram message when NEW accommodations appear.

Why this design:
  * The CROUS search is PUBLIC and server-side rendered, so a plain HTTP GET
    returns the listings. No Selenium, no Chrome, no CROUS login needed.
  * State is kept per search in a JSON file, so you are only alerted about
    listings you have not seen before (no spam on every poll).
  * Runs its own loop with jitter, or a single pass with --once (for cron).

Usage:
  python crous_watch.py            # loop forever, polling every POLL_INTERVAL s
  python crous_watch.py --once     # one pass then exit (use with cron/Task Scheduler)
  python crous_watch.py --test     # send a Telegram test message and exit
  python crous_watch.py --reset    # forget seen listings (next run re-seeds)

Config comes from environment variables or a .env file. See .env.example.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs, urlunparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------- #
# Minimal .env loader (so we don't hard-depend on python-dotenv)
# --------------------------------------------------------------------------- #
def load_dotenv(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # existing real env vars win over the .env file
        os.environ.setdefault(key, value)


load_dotenv()

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# One or more search URLs, separated by "|". Copy them from the website after
# you draw your zone / set your filters on trouverunlogement.lescrous.fr.
SEARCH_URLS = [
    u.strip()
    for u in os.environ.get("SEARCH_URLS", "").split("|")
    if u.strip()
]

# Easier alternative to SEARCH_URLS: just list city names, comma-separated
# (e.g. "Lyon, Toulouse, Paris"). Each city is geocoded to a map bounding box
# at startup and turned into a search URL automatically. Combined with any
# SEARCH_URLS above.
CITIES = [c.strip() for c in os.environ.get("CITIES", "").split(",") if c.strip()]
# The campaign/tool number in /tools/<N>/search — changes every academic year.
# Needed to build URLs from CITIES. Find it in any URL copied from the site.
TOOL_ID = os.environ.get("TOOL_ID", "42").strip()

# Populated at startup: list of (label, url) zones to watch.
WATCHES: list[tuple[str | None, str]] = []

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "300"))      # seconds between cycles
JITTER = int(os.environ.get("JITTER", "60"))                     # random extra 0..JITTER s
PAGE_DELAY = float(os.environ.get("PAGE_DELAY", "1.5"))          # polite delay between pages
MAX_PAGES = int(os.environ.get("MAX_PAGES", "20"))               # safety cap on pagination
STATE_FILE = os.environ.get("STATE_FILE", "seen.json")
GEOCODE_CACHE_FILE = os.environ.get("GEOCODE_CACHE_FILE", "geocode_cache.json")
# On the very first run for a search, seed silently instead of alerting on the
# entire current inventory. Set to "true" if you WANT the first batch too.
NOTIFY_ON_FIRST_RUN = os.environ.get("NOTIFY_ON_FIRST_RUN", "false").lower() == "true"
# Also send a ping when an offer that was online disappears from a city.
NOTIFY_REMOVED = os.environ.get("NOTIFY_REMOVED", "true").lower() == "true"

BASE = "https://trouverunlogement.lescrous.fr"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger("crous-watch")


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def telegram_send(text: str) -> bool:
    """Send a Markdown message. Returns True on success."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — cannot notify.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        if r.status_code != 200:
            log.error("Telegram API error %s: %s", r.status_code, r.text[:300])
            return False
        return True
    except requests.RequestException as e:
        log.error("Telegram request failed: %s", e)
        return False


# --------------------------------------------------------------------------- #
# Scraping
# --------------------------------------------------------------------------- #
def _load_geocode_cache() -> dict:
    p = Path(GEOCODE_CACHE_FILE)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def _geocode_bounds(city: str) -> str | None:
    """Return the CROUS 'bounds' string for a city, using a disk cache first so a
    Nominatim outage at restart doesn't drop cities. Only the raw bounds are
    cached (the URL is rebuilt with the current TOOL_ID)."""
    key = city.strip().lower()
    cache = _load_geocode_cache()

    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": f"{city}, France", "format": "json",
                    "limit": 1, "countrycodes": "fr"},
            headers={"User-Agent": "crous-watch/1.0 (housing notifier)"},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        if not data:
            log.warning("City not found by geocoder: %s", city)
            return cache.get(key)  # fall back to cache if we ever had it
        # Nominatim boundingbox = [south_lat, north_lat, west_lon, east_lon]
        south, north, west, east = map(float, data[0]["boundingbox"])
        bounds = f"{west}_{north}_{east}_{south}"
        if cache.get(key) != bounds:
            cache[key] = bounds
            _atomic_write(GEOCODE_CACHE_FILE, json.dumps(cache, ensure_ascii=False, indent=2))
        return bounds
    except (requests.RequestException, ValueError, KeyError, IndexError) as e:
        if key in cache:
            log.warning("Geocoding '%s' failed (%s) — using cached bounds", city, e)
            return cache[key]
        log.warning("Geocoding failed for '%s' and no cache: %s", city, e)
        return None


def geocode_city(city: str) -> str | None:
    """Turn a city name into a CROUS search URL (cached bounds + current TOOL_ID)."""
    bounds = _geocode_bounds(city)
    if not bounds:
        return None
    url = f"{BASE}/tools/{TOOL_ID}/search?bounds={bounds}"
    log.info("City '%s' -> %s", city, url)
    return url


def build_city_urls(cities: list[str]) -> list[tuple[str, str]]:
    """Return [(city_label, search_url), ...] for each geocodable city."""
    pairs = []
    for i, city in enumerate(cities):
        url = geocode_city(city)
        if url:
            pairs.append((city.strip().title(), url))
        if i < len(cities) - 1:
            time.sleep(1.1)  # Nominatim asks for <=1 request/second
    return pairs


def label_from_url(url: str) -> str | None:
    """Use the search URL's `locationName` query param as the zone label,
    e.g. '...&locationName=Reims+%2851100%29' -> 'Reims'."""
    name = parse_qs(urlparse(url).query).get("locationName", [None])[0]
    if not name:
        return None
    return name.split(" (")[0].strip() or None  # drop the postal suffix


def with_page(url: str, page: int) -> str:
    """Return url with its 'page' query parameter set to `page`."""
    parts = urlparse(url)
    q = parse_qs(parts.query)
    q["page"] = [str(page)]
    new_query = urlencode({k: v[-1] for k, v in q.items()})
    return urlunparse(parts._replace(query=new_query))


def _is_card(tag) -> bool:
    """True for the real listing container: an element whose class list
    contains the exact token 'fr-card' (not 'fr-card__title' etc.)."""
    return "fr-card" in (tag.get("class") or [])


def parse_price(card) -> str:
    badge = card.find("p", class_="fr-badge")
    if badge:
        return badge.get_text(strip=True)
    return "prix n/a"


def parse_cards(html: str) -> dict[str, dict]:
    """
    Return {accommodation_id: {title, price, url}} for every listing on the page.

    Robust strategy: find every link to /accommodations/<id>. That anchor pattern
    is far more stable than the auto-generated Svelte/DSFR CSS class hashes.
    """
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, dict] = {}

    for a in soup.select("a[href*='/accommodations/']"):
        href = a.get("href", "")
        m = re.search(r"/accommodations/(\d+)", href)
        if not m:
            continue
        acc_id = m.group(1)
        if acc_id in found:
            continue

        # Prefer a real title; fall back to the anchor text.
        title = a.get_text(" ", strip=True)
        details: list[str] = []
        if card := a.find_parent(_is_card):
            title_el = card.find(class_="fr-card__title")
            if title_el:
                title = title_el.get_text(" ", strip=True)
            price = parse_price(card)
            desc_el = card.find("p", class_="fr-card__desc")
            desc = desc_el.get_text(" ", strip=True) if desc_el else ""
            # Room facts: size, type, beds, amenities (li.fr-card__detail)
            details = [
                d.get_text(" ", strip=True)
                for d in card.find_all(class_="fr-card__detail")
                if d.get_text(strip=True)
            ]
        else:
            price, desc = "prix n/a", ""

        city, postal = parse_city(desc)
        full_url = href if href.startswith("http") else BASE + href
        found[acc_id] = {
            "title": title or f"Logement {acc_id}",
            "price": price,
            "desc": desc,
            "details": details,
            "city": city,
            "postal": postal,
            "url": full_url,
        }
    return found


def parse_city(address: str) -> tuple[str, str]:
    """Extract (city, postal_code) from a French address string.
    e.g. '22 avenue Jean Nicoli, BP 55, 20250 CORTE' -> ('Corte', '20250')."""
    if not address:
        return "", ""
    matches = list(re.finditer(r"\b(\d{5})\b\s+(.+)$", address))
    if not matches:
        return "", ""
    postal = matches[-1].group(1)
    city = matches[-1].group(2).strip(" ,.-").title()
    return city, postal


def total_count(html: str) -> int | None:
    """Best-effort read of the 'N logements trouvés' headline."""
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    m = re.search(r"(\d+)\s+logement", text)
    if m:
        return int(m.group(1))
    if "Aucun logement" in text or "aucun logement" in text:
        return 0
    return None


def build_session() -> requests.Session:
    """A requests Session that automatically retries transient failures
    (connection errors and 429/5xx) a few times with exponential backoff,
    so a momentary glitch doesn't cost us a whole cycle's check for a city."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    })
    retry = Retry(
        total=3,                       # up to 3 retries
        backoff_factor=0.5,            # waits ~0.5s, 1s, 2s between tries
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def fetch_search(session: requests.Session, url: str) -> dict[str, dict] | None:
    """Fetch all pages of one search URL.

    Returns {id: listing} on success (possibly empty for a genuine 0 results),
    or None if the fetch FAILED (network error, non-200, or an unexpected page
    such as a maintenance screen). Returning None lets the caller skip the city
    this cycle instead of mistaking an outage for 'every offer was removed'.
    """
    all_listings: dict[str, dict] = {}
    prev_ids: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        page_url = with_page(url, page)
        try:
            r = session.get(page_url, timeout=25)
        except requests.RequestException as e:
            log.warning("Request failed (%s): %s", page_url, e)
            return None  # transient error -> skip this city this cycle

        if r.status_code != 200:
            log.warning("HTTP %s for %s", r.status_code, page_url)
            return None

        # The site sends no charset header, so requests would default to
        # Latin-1 and mangle accented characters. The pages are UTF-8.
        r.encoding = "utf-8"

        listings = parse_cards(r.text)
        ids = set(listings)

        if page == 1:
            cnt = total_count(r.text)
            log.info("  page 1: site reports %s total, %d cards parsed",
                     cnt if cnt is not None else "?", len(ids))
            # A 200 with no cards AND no recognisable results headline is not a
            # real "0 results" — it's a maintenance/soft-error page. Skip it.
            if not ids and cnt is None:
                log.warning("  unexpected page (no results headline) — treating as failure")
                return None

        if not ids:
            break  # genuine end of results (cnt was 0 / 'Aucun logement')
        if ids == prev_ids:
            break  # site ignored ?page= — same page returned, stop

        all_listings.update(listings)
        prev_ids = ids
        time.sleep(PAGE_DELAY)

    return all_listings


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    p = Path(STATE_FILE)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("State file corrupt — starting fresh.")
    return {}


def _atomic_write(path: str, text: str) -> None:
    """Write to a temp file then replace, so a crash mid-write can't corrupt it."""
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)  # atomic on the same filesystem


def save_state(state: dict) -> None:
    _atomic_write(STATE_FILE, json.dumps(state, ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------- #
# Core cycle
# --------------------------------------------------------------------------- #
TELEGRAM_MAX = 4096  # hard limit of a single Telegram message


def _md(text: str) -> str:
    """Neutralise Markdown-breaking characters in link text."""
    return text.replace("*", "").replace("_", "").replace("[", "(").replace("]", ")")


def _zone_of(label: str, items: list[dict]) -> str:
    if label:
        return label
    cities = sorted({a.get("city") for a in items if a.get("city")})
    return ", ".join(cities) if cities else "France"


def _assemble(header: list[str], blocks: list[list[str]], footer: str) -> str:
    """Join header + as many per-offer blocks as fit under the Telegram limit,
    with a '… et N autre(s)' note when some are dropped, then the footer."""
    budget = TELEGRAM_MAX - len("\n".join(header)) - len(footer) - 40
    out, used, shown = list(header), 0, 0
    for b in blocks:
        btext = "\n".join(b) + "\n"
        if used + len(btext) > budget:
            break
        out.extend(b)
        out.append("")
        used += len(btext)
        shown += 1
    dropped = len(blocks) - shown
    if dropped > 0:
        out.append(f"… et {dropped} autre{'s' if dropped > 1 else ''} offre(s).")
    out.append(footer)
    return "\n".join(out)


def format_alert(label: str, url: str, new_listings: dict[str, dict]) -> str:
    accs = list(new_listings.values())
    n = len(accs)
    zone = _zone_of(label, accs)
    plural = "s" if n > 1 else ""
    header = [f"🏠 *{n} nouvelle{plural} offre{plural} CROUS*", f"📍 _{zone}_", ""]

    blocks = []
    for i, acc in enumerate(accs, 1):
        city = acc.get("city") or zone
        postal = f" ({acc['postal']})" if acc.get("postal") else ""
        block = [
            f"*{i}. {_md(acc['title'])}*",
            f"💶 {acc['price']}   ·   🏙 {city}{postal}",
        ]
        if acc.get("details"):
            block.append("🛏 " + " · ".join(acc["details"][:4]))
        block.append(f"[➡️ Voir l'offre]({acc['url']})")
        blocks.append(block)

    return _assemble(header, blocks, f"🔎 [Voir toute la recherche]({url})")


def format_removed(label: str, url: str, removed: dict[str, dict]) -> str:
    items = list(removed.items())
    n = len(items)
    zone = _zone_of(label, [i[1] for i in items])
    plural = "s" if n > 1 else ""
    header = [f"❌ *{n} offre{plural} retirée{plural} CROUS*", f"📍 _{zone}_", ""]

    blocks = []
    for idx, (acc_id, acc) in enumerate(items, 1):
        city = acc.get("city") or zone
        postal = f" ({acc['postal']})" if acc.get("postal") else ""
        price = acc.get("price")
        blocks.append([
            f"*{idx}. {_md(acc.get('title') or f'Offre {acc_id}')}*",
            f"💶 {price}   ·   🏙 {city}{postal}" if price else f"🏙 {city}{postal}",
        ])

    return _assemble(header, blocks, f"🔎 [Voir la recherche]({url})")


def _slim(acc: dict) -> dict:
    """Keep only the fields needed to describe an offer later (e.g. once removed)."""
    return {k: acc.get(k) for k in ("title", "price", "city", "postal", "url")}


def run_once(session: requests.Session, state: dict) -> None:
    for label, url in WATCHES:
        log.info("Checking%s: %s", f" [{label}]" if label else "", url)
        listings = fetch_search(session, url)
        if listings is None:
            # Fetch failed (network/HTTP/maintenance). Do NOT touch state or
            # notify — otherwise an outage looks like 'all offers removed'.
            log.info("  fetch failed — skipping this cycle (state unchanged)")
            continue
        current_ids = set(listings)

        # Previous inventory. New format is {id: {details}}; tolerate the old
        # {url: [ids]} list format from earlier versions.
        prev = state.get(url)
        first_time = prev is None
        if isinstance(prev, list):
            prev_map = {i: {} for i in prev}
        elif isinstance(prev, dict):
            prev_map = prev
        else:
            prev_map = {}
        seen = set(prev_map)

        new_ids = current_ids - seen
        removed_ids = seen - current_ids
        new_listings = {i: listings[i] for i in new_ids}
        removed_listings = {i: prev_map[i] for i in removed_ids}

        # Start from the current inventory, then adjust so that offers whose
        # notification FAILED are not acknowledged (they retry next cycle).
        next_state = {i: _slim(listings[i]) for i in current_ids}

        seeding = first_time and not NOTIFY_ON_FIRST_RUN
        if seeding:
            log.info("  first run: seeding %d listings silently", len(current_ids))
        else:
            if new_listings:
                log.info("  %d NEW listing(s) -> notifying", len(new_listings))
                if telegram_send(format_alert(label, url, new_listings)):
                    log.info("  new-offer notification sent.")
                else:
                    log.warning("  new-offer notification FAILED — will retry")
                    for i in new_ids:  # forget them so they re-trigger next cycle
                        next_state.pop(i, None)
            if removed_listings and NOTIFY_REMOVED:
                log.info("  %d REMOVED listing(s) -> notifying", len(removed_listings))
                if telegram_send(format_removed(label, url, removed_listings)):
                    log.info("  removed-offer notification sent.")
                else:
                    log.warning("  removed-offer notification FAILED — will retry")
                    for i in removed_ids:  # keep them so removal re-triggers
                        next_state[i] = prev_map[i]
            if not new_listings and not removed_listings:
                log.info("  no changes (%d currently online)", len(current_ids))

        state[url] = next_state
        save_state(state)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="CROUS housing watcher -> Telegram")
    ap.add_argument("--once", action="store_true", help="run a single pass then exit")
    ap.add_argument("--test", action="store_true", help="send a Telegram test message")
    ap.add_argument("--reset", action="store_true", help="clear the seen-state file")
    args = ap.parse_args()

    if args.reset:
        Path(STATE_FILE).unlink(missing_ok=True)
        log.info("State cleared.")
        return 0

    if args.test:
        ok = telegram_send("✅ crous-watch test message. Telegram is wired up correctly.")
        return 0 if ok else 1

    # Build the watch list: (label, url) pairs. Cities carry their name as label;
    # explicit SEARCH_URLS have no label (city is then read from each listing).
    global WATCHES
    WATCHES = [(label_from_url(u), u) for u in SEARCH_URLS]
    if CITIES:
        log.info("Resolving %d city name(s) to search zones…", len(CITIES))
        WATCHES = WATCHES + build_city_urls(CITIES)

    if not WATCHES:
        log.error("Nothing to watch. Set CITIES or SEARCH_URLS in .env (see .env.example).")
        return 1
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("Telegram not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        return 1

    session = build_session()
    state = load_state()

    if args.once:
        run_once(session, state)
        return 0

    log.info("Starting loop: %d zone(s), every ~%ds (+0..%ds jitter). Ctrl+C to stop.",
             len(WATCHES), POLL_INTERVAL, JITTER)
    while True:
        try:
            run_once(session, state)
        except Exception as e:  # keep the loop alive on unexpected errors
            log.exception("Cycle error: %s", e)
        sleep_for = POLL_INTERVAL + random.randint(0, JITTER)
        log.info("Sleeping %ds…", sleep_for)
        try:
            time.sleep(sleep_for)
        except KeyboardInterrupt:
            log.info("Stopped by user.")
            return 0


if __name__ == "__main__":
    sys.exit(main())
