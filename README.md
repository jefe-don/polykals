# rapper_market_watch

Monitors **Kalshi** and **Polymarket** for new prediction markets mentioning:
Kanye West (also "Kanye", "Ye", "Yeezy"), Travis Scott, Young Thug ("Thugger"),
Future, Lil Wayne ("Weezy"), Lil Baby, and Playboi Carti.

Both platforms are queried through their public read-only APIs — no API keys
needed. Seen market/event IDs are persisted to `seen_markets.json` so only
genuinely **new** markets trigger alerts. The very first run records everything
currently live as a baseline (printed, but no push alerts).

Matching notes: most names are case-insensitive whole-word matches, but
**"Future"** and **"Ye"** are case-sensitive whole-word matches on purpose —
otherwise every market containing "future" or "ye(s)" would fire. A bare
"Future" hit additionally requires music-related wording (album, tour,
Grammy, "by Future", …) in the same text, so title-cased prose like
"Building the Future of Finance" doesn't trigger.

**Exclusions:** markets mentioning *Billboard* or *Hot 100* are ignored
entirely — Kalshi creates a fresh weekly chart event with one market per
song, which would spam alerts every chart week. Add your own comma-separated
exclusion terms via the `EXCLUDE_KEYWORDS` env var (e.g.
`set EXCLUDE_KEYWORDS=spotify,album equivalent units`).

## Requirements

- Python 3.10+
- `pip install requests`

## Usage

```bash
# Single check-and-exit (cron mode). First run writes the baseline.
python3 rapper_market_watch.py

# Run forever, checking every CHECK_INTERVAL_SECS (default 3600)
python3 rapper_market_watch.py --loop

# Send a test alert through stdout + any configured push channels, then exit.
# Use this to verify your NTFY_TOPIC / DISCORD_WEBHOOK_URL work.
python3 rapper_market_watch.py --test-alert
```

### Environment variables (all optional)

| Variable | Purpose |
|---|---|
| `NTFY_TOPIC` | ntfy.sh topic for push alerts (subscribe in the ntfy app) |
| `DISCORD_WEBHOOK_URL` | Discord webhook URL for alerts |
| `CHECK_INTERVAL_SECS` | Seconds between checks in `--loop` mode (default 3600) |
| `STATE_FILE` | Path of the seen-IDs JSON (default: `seen_markets.json` next to the script) |

Alerts always print to stdout; ntfy/Discord fire only when their variables are set.
Warnings (one platform down, bad payloads) go to stderr; the other platform is
still checked.

## Scheduling

### Option A — cron (hourly)

```bash
crontab -e
```

```cron
# Hourly rapper market check. Redirect stdout to a log; cron mails stderr by default.
0 * * * * NTFY_TOPIC=my-rapper-alerts DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..." /usr/bin/python3 /opt/rapper-watch/rapper_market_watch.py >> /var/log/rapper_market_watch.log 2>&1
```

Run the script once by hand first so the baseline is written interactively
rather than inside cron.

### Option B — systemd service (Linux, `--loop` mode)

`/etc/systemd/system/rapper-market-watch.service`:

```ini
[Unit]
Description=Kalshi/Polymarket rapper market watcher
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/rapper-watch/rapper_market_watch.py --loop
WorkingDirectory=/opt/rapper-watch
Environment=CHECK_INTERVAL_SECS=3600
Environment=NTFY_TOPIC=my-rapper-alerts
# Environment=DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
Restart=on-failure
RestartSec=60
# Run as an unprivileged user that can write seen_markets.json in WorkingDirectory
User=rapperwatch

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now rapper-market-watch
journalctl -u rapper-market-watch -f     # tail the logs
```

(Alternatively use a systemd *timer* with the default single-shot mode —
functionally equivalent to the cron entry.)

### Option C — launchd (macOS, hourly single-shot)

`~/Library/LaunchAgents/com.user.rapper-market-watch.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.rapper-market-watch</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/Users/you/rapper-watch/rapper_market_watch.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/you/rapper-watch</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>NTFY_TOPIC</key>
        <string>my-rapper-alerts</string>
    </dict>
    <key>StartInterval</key>
    <integer>3600</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/rapper_market_watch.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/rapper_market_watch.err</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.user.rapper-market-watch.plist
launchctl start com.user.rapper-market-watch   # kick off the first (baseline) run now
tail -f /tmp/rapper_market_watch.log
```

To stop: `launchctl unload ~/Library/LaunchAgents/com.user.rapper-market-watch.plist`

### Option D — Windows

**Quick way (foreground):** set your env vars and run loop mode in a Command
Prompt:

```bat
set DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR_ID/YOUR_TOKEN
python3 rapper_market_watch.py --loop
```

> ⚠️ Windows consoles pause a running program while text is selected
> (QuickEdit mode) — if you click inside the window, the watcher freezes,
> including its hourly checks, until you press Enter/Esc. Disable it via
> title bar right-click → Properties → uncheck **QuickEdit Mode**, or use
> Task Scheduler below.

**Proper way (Task Scheduler, hourly):** create `watch.bat` next to the
script:

```bat
@echo off
cd /d %~dp0
set DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR_ID/YOUR_TOKEN
python3 rapper_market_watch.py >> watch.log 2>&1
```

Then Task Scheduler → Create Basic Task → trigger **Daily**, then edit the
trigger and enable *"Repeat task every 1 hour for a duration of 1 day"* →
action: start `watch.bat`. In the task's properties, enable *"Run whether
user is logged on or not"* so it survives logoffs and reboots.
