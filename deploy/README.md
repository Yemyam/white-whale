# Deploying White Whale on a Raspberry Pi (systemd)

These units run the live pipeline as background services. Paths assume an install
at `/opt/whitewhale` running as a dedicated `whitewhale` user; adjust to taste.

## Units

| Unit | Type | What it does |
|---|---|---|
| `whitewhale-ingest.service` | long-running | RTDS WebSocket → SQLite (the firehose). Auto-restarts. |
| `whitewhale-enrich.service` | long-running | Gamma market enrichment loop; hot-reloads `limit`/`max_age` from config. |
| `whitewhale-health.service` | long-running | Serves `GET /health` (200 healthy / 503 stale). |
| `whitewhale-refresh-stats.service` | one-shot | Recomputes `wallet_stats`. Triggered by the timer. |
| `whitewhale-refresh-stats.timer` | timer | Fires the refresh daily (`Persistent=true` catches up missed runs). |

Alert emission (`whitewhale alert`) isn't a unit here yet — wire it the same way
once the downstream bot's drop directory is decided.

## Install

```bash
# 1. Code + venv at /opt/whitewhale (uv recommended)
sudo useradd --system --home /opt/whitewhale whitewhale
sudo install -d -o whitewhale -g whitewhale /opt/whitewhale
#    ... copy the repo there, then:  uv sync   (creates /opt/whitewhale/.venv)

# 2. Config: copy default.yaml -> config/local.yaml and edit (db.path, health.host, ...)
sudo -u whitewhale cp config/default.yaml config/local.yaml

# 3. One-time DB init
sudo -u whitewhale /opt/whitewhale/.venv/bin/whitewhale \
    --config /opt/whitewhale/config/local.yaml init-db

# 4. Install + enable the units
sudo cp deploy/whitewhale-*.service deploy/whitewhale-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now whitewhale-ingest whitewhale-enrich whitewhale-health
sudo systemctl enable --now whitewhale-refresh-stats.timer
```

## Operate

```bash
systemctl status whitewhale-ingest
journalctl -u whitewhale-ingest -f          # live logs
systemctl list-timers whitewhale-*          # when the next refresh fires
curl -s localhost:8787/health | jq .        # health snapshot (200 / 503)
systemctl start whitewhale-refresh-stats    # force a refresh now
```

## Notes

- **Heavy backfill is laptop-only.** `backfill-wallets` and `backtest` are run off
  the Pi; `scp` the resulting `whitewhale.db` over (see `docs/plan.md`).
- **Config hot-reload** covers the periodic loops (enrich, refresh-stats). The
  ingest service reads config once at start — `systemctl restart whitewhale-ingest`
  to pick up changes there.
- **SD-card wear**: WAL + `synchronous=NORMAL` already limit fsyncs; consider a
  USB SSD for `db.path` on a long-lived deployment (see the risks table in the plan).
