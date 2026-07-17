# FIFA Store Shopify Monitor

A production-grade, async monitor for [store.fifa.com](https://store.fifa.com)
(a Shopify store). It watches for **newly published products** — especially
"ring" collectibles — and **restocks**, then fires a rich **Discord** alert
within ~1–3 seconds of the product going live.

## How it works

Three endpoints are polled **concurrently**, each on its own loop, so a new
product is caught wherever Shopify exposes it first:

| Endpoint | Why |
|----------|-----|
| `/collections/collectibles/products.json?limit=250` | The expected collection |
| `/products.json?limit=250&order=created_at:desc` | Catches it in *any* collection |
| `/sitemap.xml` → product child sitemaps | Sitemaps sometimes update first |

> **Sitemap note:** the monitor points at the sitemap **index** (`/sitemap.xml`)
> and follows its canonical product child sitemap(s). A fixed
> `sitemap_products_1.xml?from=1&to=…` URL only ever returns the homepage,
> because Shopify shifts the `from`/`to` bounds as products change — and that
> shift is itself the signal that a new product appeared.

Detection rules:

- **Keyword hit** (default keyword `ring`): a product whose title, handle,
  `product_type`, or tags matches a keyword **and** whose ID is new. Sends
  `@everyone`.
- **New product**: *any* new product ID (the ring's title might not literally
  say "ring"). Labelled `NEW PRODUCT`, no mention.
- **Restock**: a known keyword product that flips from *all variants
  unavailable* → *any variant available*. Sends `@everyone`.

Everything is de-duplicated via `state.json` — the same alert type is never sent
twice for the same product, and a restart never re-alerts on the baseline set.

## Requirements

- Python 3.11+
- See `requirements.txt` (`httpx[http2]`, `python-dotenv`)

## Setup

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env              # then edit: set DISCORD_WEBHOOK_URL
cp proxies.txt.example proxies.txt # optional: add proxies (see below)
```

### `.env`

```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/XXXX/YYYY
# optional, for the 30-min heartbeat; falls back to the main webhook if unset
DISCORD_HEARTBEAT_WEBHOOK_URL=
```

### `config.json`

All non-secret settings live here. Key fields:

- `keywords` — case-insensitive substrings to match (default `["ring"]`).
- `default_interval_ms` / per-endpoint `interval_ms` — poll cadence
  (default 1500 ms) with **±30%** random jitter (`jitter_pct`).
- `request_timeout_s` — hard 4-second cap per request.
- `mention` — what to `@` on keyword/restock hits. Use `@everyone`, a role
  (`<@&ROLE_ID>`), or a user (`<@USER_ID>`).
- `heartbeat_enabled` / `heartbeat_interval_s` — status pings (default 30 min).
- Proxy health: `proxy_fail_threshold` (default 3), `proxy_bench_seconds`
  (default 300). Backoff: `backoff_base_s` (2), `backoff_cap_s` (60).

### `proxies.txt` (optional)

One proxy per line. Both formats are accepted and normalised:

```
user:pass@ip:port
ip:port:user:pass
ip:port            # authless
```

Proxies are used **round-robin, one per request** — this rotates the source IP
but does **not** increase the request rate (the rate stays at the configured
interval). A proxy that returns `429/403/430` or times out **3 times in a row**
is benched for 5 minutes. If every proxy is benched, the monitor falls back to a
**direct** connection and logs a loud warning. With no `proxies.txt`, it always
runs direct.

## Running

```bash
# Verify Discord formatting with a fake alert, then exit:
python -m fifa_monitor --test

# Run the monitor:
python -m fifa_monitor --config config.json
```

On first start it builds a **baseline** (records all current products, sends
nothing). From then on only genuinely new/restocked products alert. Press
`Ctrl+C` for a graceful shutdown that saves state.

Logs stream to the console and to `monitor.log` (rotating, 5 × 5 MB).

## Deploy 24/7

### systemd (VPS)

```bash
sudo mkdir -p /opt/fifa-monitor && sudo chown $USER /opt/fifa-monitor
# copy the repo (fifa_monitor/, config.json, .env, proxies.txt) into /opt/fifa-monitor
cd /opt/fifa-monitor && python -m venv venv && venv/bin/pip install -r requirements.txt

sudo cp fifa-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fifa-monitor
journalctl -u fifa-monitor -f
```

Edit `User`, `WorkingDirectory`, and paths in `fifa-monitor.service` to match
your box. It uses `SIGINT` for a clean, state-saving stop.

### Docker

```bash
docker build -t fifa-monitor .
docker run -d --name fifa-monitor --restart unless-stopped \
  --env-file .env \
  -v "$PWD/state.json:/app/state.json" \
  -v "$PWD/proxies.txt:/app/proxies.txt" \
  -v "$PWD/monitor.log:/app/monitor.log" \
  fifa-monitor
```

## Files

```
fifa_monitor/        # the package
  __main__.py        # CLI entry (--test, --config)
  config.py          # config.json + env loading
  monitor.py         # concurrent loops + detection
  http_client.py     # per-proxy client pool, UA rotation, conditional GETs
  proxies.py         # proxy parsing, rotation, health benching
  discord.py         # rich embeds, fire-and-forget, heartbeat
  models.py          # product parsing (JSON + sitemap)
  state.py           # atomic state.json persistence
  logging_setup.py
config.json          # your settings
requirements.txt
.env.example         # -> .env (secrets)
proxies.txt.example  # -> proxies.txt (optional)
Dockerfile
fifa-monitor.service # systemd unit
```

## Notes on being a good citizen

- Sends realistic desktop Chrome/Firefox User-Agents and normal headers.
- Uses conditional requests (`If-None-Match` / `If-Modified-Since`); a `304 Not
  Modified` is a successful, cheap poll.
- Exponential backoff (2s → 60s) per endpoint on `429/430/5xx`, while the other
  endpoints keep polling.
- This is for personal monitoring of a public storefront. Keep the interval
  reasonable and don't point it at endpoints you aren't allowed to poll.
```
