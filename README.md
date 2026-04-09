# Crypto Bot — Final Full Build (Code + Docs)
WIP WIP WIP WIP WIP
This repo contains a single Python file (`bot_full.py`) with:
- Multi-chain scanner (Solana, ETH/WETH, Base, BSC, Polygon) — **shadow-mode by default**
- Objective × Strategy engine (they work *together*)
- Moonshot suggest/enter, adaptive no-pump, auto-scale with grace window
- Old-coin pump detector (alert or tiny auto-join)
- Trusted coins module (e.g., DOGE bag growth)
- Reserves, per-token/chain caps, drawdown brake
- Alerts, daily digest, RPC health, stealth/jitter, multi-tenant
- Flask `/status` + Telegram commands
- AWS systemd unit + (optional) CloudWatch heartbeat

> ⚠️ Live trading is OFF by default. Run in **shadow mode** first.

## 1) Quickstart (Local)

```bash
cp .env.example .env   # fill TELEGRAM_* later; leave wallet keys blank for shadow tests
python3 -m venv .venv && source .venv/bin/activate
pip install -U pip && pip install -r requirements.txt
python bot_full.py
