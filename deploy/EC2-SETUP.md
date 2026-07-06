# Running crous-watch 24/7 on an EC2 free-tier instance

This gives true, gapless ~1-minute polling. You do NOT need the GitHub Actions
workflow when running on EC2 (they'd double-notify) — pick one. On EC2 the state
file `seen.json` lives on the instance disk, so dedup persists across restarts.

## 1. Launch the instance

- **AMI:** Amazon Linux 2023 (or Ubuntu — adjust the user name below).
- **Type:** `t3.micro` or `t2.micro` (free tier, 750 h/month for 12 months).
- **Key pair:** create/download one so you can SSH in.
- **Security group:**
  - Inbound: SSH (port 22) from *your IP only*.
  - Outbound: leave default (all) — the bot only makes outbound HTTPS calls.
  - No web/inbound ports needed.

SSH in:
```bash
ssh -i your-key.pem ec2-user@<PUBLIC_IP>       # Ubuntu: ubuntu@<PUBLIC_IP>
```

## 2. Install and fetch the code

Amazon Linux 2023:
```bash
sudo dnf install -y python3 python3-pip git
git clone https://github.com/SL99-zy/crous-watch.git
cd crous-watch
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```
(Ubuntu: `sudo apt update && sudo apt install -y python3-venv python3-pip git`)

## 3. Configure secrets

```bash
cp .env.example .env
nano .env        # fill in the values below, then Ctrl+O, Enter, Ctrl+X
chmod 600 .env   # lock it down so only you can read the token
```

Set in `.env`:
```ini
TELEGRAM_BOT_TOKEN=your_token_from_BotFather
TELEGRAM_CHAT_ID=your_numeric_id
CITIES=Reims, Rennes, Troyes, Nice, Toulon, Lille, Corte
TOOL_ID=42
POLL_INTERVAL=60          # true 1-minute polling
JITTER=10
NOTIFY_ON_FIRST_RUN=true  # first run also sends what's online now
```
(On EC2 you can use `CITIES=` — it geocodes once at startup, not every cycle.)

Test it once by hand:
```bash
.venv/bin/python crous_watch.py --test    # sends a Telegram test message
```

## 4. Run it forever with systemd

```bash
sudo cp deploy/crous-watch.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now crous-watch     # start now + on every boot
```

Manage / observe:
```bash
systemctl status crous-watch                # is it running?
journalctl -u crous-watch -f                # live logs (Ctrl+C to stop watching)
sudo systemctl restart crous-watch          # after editing .env
sudo systemctl stop crous-watch             # stop it
```

That's it — it now runs 24/7, restarts on crash, and survives reboots.

## Notes

- **Pick ONE runner:** if you use EC2, disable the GitHub Actions schedule
  (delete `.github/workflows/crous-watch.yml` or comment out the `schedule:`),
  or you'll get duplicate alerts from two independent watchers.
- **After free tier (12 months)** a t3.micro is ~US$7–8/month. A tiny VPS
  (Hetzner/OVH/Scaleway ~€3–4/month) or a Raspberry Pi at home is cheaper.
- **Politeness:** `POLL_INTERVAL=60` = ~10k requests/day to CROUS. If you ever
  see HTTP 403/blocks in the logs, raise it to 120–300. Every 2–3 minutes is
  plenty for catching new rooms.
