# crous-watch

Monitor CROUS student housing on **trouverunlogement.lescrous.fr** and get a
**Telegram** message the moment a *new* listing appears in your chosen zone(s).

No Selenium, no Chrome, no CROUS login required — the search pages are public and
server-side rendered, so a plain HTTP request is enough. It remembers what it has
already shown you, so you only get pinged about genuinely new listings.

## How it works

```
loop every ~5 min:
  for each SEARCH_URL:
    GET the search page(s)  ->  parse listing ids/titles/prices from the HTML
    compare ids against seen.json
    any id not seen before  ->  send Telegram message
    save the current inventory to seen.json
```

- **Dedup:** state is kept per search URL in `seen.json`. First run seeds silently
  (set `NOTIFY_ON_FIRST_RUN=true` if you want the current inventory too).
- **Pagination:** follows `?page=N` until results repeat or run out.
- **Politeness:** realistic User-Agent, delay between pages, jittered poll interval.

## Setup

1. **Install Python 3.9+**, then install deps:
   ```
   pip install -r requirements.txt
   ```
   (or just run `run.bat` on Windows — it creates a venv for you.)

2. **Create a Telegram bot:** message [@BotFather](https://t.me/BotFather) →
   `/newbot` → copy the token.

3. **Get your chat id:** message [@userinfobot](https://t.me/userinfobot), or send
   your new bot a message and open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `chat.id`.

4. **Copy config:** `copy .env.example .env` (Windows) / `cp .env.example .env`,
   then fill in the token, chat id, and your `SEARCH_URLS`.

### Choosing which cities to watch

You have two ways. Pick whichever is easier.

**Option A — by city name (easiest).** Just list cities in `.env`:

```
CITIES=Lyon, Toulouse, Rennes
TOOL_ID=42
```

Each city is geocoded to a map bounding box automatically (via OpenStreetMap's
free Nominatim service) and turned into a search URL at startup. `TOOL_ID` is the
campaign number from `/tools/<N>/` — **it changes every academic year**, so open
<https://trouverunlogement.lescrous.fr>, start any search, and copy the number
from the URL.

**Option B — by exact URL (more control).** Draw your zone and set filters (room
type, max price, etc.) on the site, then copy the address-bar URL:

```
SEARCH_URLS=https://trouverunlogement.lescrous.fr/tools/42/search?bounds=<west>_<north>_<east>_<south>
```

The `bounds` values are the corners of the map rectangle
(`west_lon _ north_lat _ east_lon _ south_lat`). Watch several zones by separating
URLs with `|`. Options A and B can be combined — all zones are watched together.

> Tip: city names cover the whole city. If you want *just one campus* or a price
> filter, use Option B — draw a tight box / set filters on the site and copy that URL.

## Run

```
python crous_watch.py            # loop forever (default)
python crous_watch.py --once     # single pass, then exit (for cron/Task Scheduler)
python crous_watch.py --test     # send a Telegram test message and quit
python crous_watch.py --reset    # forget seen listings
```

### Run it 24/7

- **Windows Task Scheduler:** create a task that runs
  `crous_watch.py --once` every 5–10 minutes (keeps `seen.json` between runs).
- **Linux cron:** `*/5 * * * * cd /path/crous-watch && python crous_watch.py --once`
- **Always-on process:** just run without `--once` on a VPS / Raspberry Pi
  (optionally under `systemd`, `pm2`, `nohup`, or `tmux`).

## Notes / limitations

- If CROUS changes their page markup, card parsing may need a tweak — the code
  keys on the stable `/accommodations/<id>` link pattern to minimise this.
- Don't hammer the site. `POLL_INTERVAL=300` (5 min) is a reasonable floor.
- This only *watches and alerts*. Applying for a listing is still done by you,
  logged into your account — by design.
