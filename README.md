# tothemoon

Multi-chain crypto scalping bot. Scans DexScreener for new launches and old-coin pump signals, manages positions with momentum exits (trailing stop, velocity exit, liq drain), auto-re-enters after clean exits, and runs over Telegram with a web dashboard. Shadow mode (paper trading) is on by default — nothing goes on-chain until you explicitly flip it.

Chains: Solana, Ethereum, Base, BSC, Polygon.

---

## Quickstart (local)

```bash
cp .env.example .env          # fill in TELEGRAM_TOKEN at minimum
python3 -m venv .venv && source .venv/bin/activate
pip install -U pip && pip install -r requirements.txt
python3 src/bot/bot_full.py
```

Message `/whoami` to your bot to get your Telegram user ID, then add it to `TELEGRAM_ALLOWLIST` in `.env`.

Dashboard at `http://localhost:8787/app`.

## Quickstart (Docker)

```bash
cp .env.example .env   # fill values
docker compose up -d
docker compose logs -f
```

Flask: `http://localhost:8787/status`  
Dashboard: `http://localhost:8787/app`

## Deploy to EC2

```bash
# Fresh Ubuntu instance, run as root or with sudo:
bash deploy/ec2-user-data.sh

# Then drop in the env file and start:
cp .env.example /home/ubuntu/cryptobot/.env
nano /home/ubuntu/cryptobot/.env
systemctl start cryptobot
journalctl -fu cryptobot
```

## Run tests

```bash
python3 src/bot/tests.py -v
# Single class:
python3 src/bot/tests.py -v TestShadowRoundTrip
# Flask API only:
python3 src/bot/tests.py -v TestFlaskAPI
```

243 tests, all covering core logic in-process without live network calls.

---

## Environment variables

| Variable | Required | Notes |
|---|---|---|
| `TELEGRAM_TOKEN` | Yes | BotFather token |
| `TELEGRAM_ALLOWLIST` | Recommended | Comma-separated Telegram user IDs that may control the bot. Empty = allow everyone |
| `SHADOW_MODE` | No | `true` (default) = paper trade. Set `false` to go live |
| `WALLET_PK_SOL` | Live Solana only | Base58 private key — never commit this |
| `WALLET_PK_EVM` | Live EVM only | Hex private key (`0x…`) — never commit this |
| `SOL_RPC_URL` | No | Custom Solana RPC (falls back to public mainnet) |
| `ETH_RPC_URL` | No | Custom Ethereum RPC |
| `BASE_RPC_URL` | No | Custom Base RPC |
| `BSC_RPC_URL` | No | Custom BSC RPC |
| `POLY_RPC_URL` | No | Custom Polygon RPC |
| `BIRDEYE_API_KEY` | No | Adds Birdeye as a Solana token source and price fallback |
| `LUNARCRUSH_API_KEY` | No | Enables LunarCrush social volume gate (falls back to CoinGecko trending rank) |
| `ONEINCH_API_KEY` | No | Routes EVM swaps through 1inch aggregator instead of direct Uniswap |
| `DASHBOARD_TOKEN` | No | Bearer token for dashboard API access. Unset = no auth (localhost only) |
| `STATE_PATH` | No | Override path to the state JSON file (default: `state_default.json`) |
| `DASHBOARD_DIR` | No | Path to the dashboard HTML directory |

---

## Telegram commands

| Command | What it does |
|---|---|
| `/start` | Link this chat to receive proactive alerts |
| `/whoami` | Get your Telegram user ID |
| `/status` | Vault, open positions with live PnL, BTC dominance, brake status |
| `/mode <safe\|default\|hype\|degen>` | Switch strategy risk profile |
| `/objective target <usd> <weeks>` | Set a profit target curve; bot adjusts aggression to stay on track |
| `/objective off` | Clear the objective |
| `/moonshot <enter\|suggest>` | `enter` = auto-buy new launches; `suggest` = alert only |
| `/auto_old <on\|off>` | Tiny auto-join on old-coin volume spikes |
| `/skim <on\|off>` | Move a fraction of realized profit to a separate income vault |
| `/spray_until YYYY-MM-DD\|off` | Relax entry filters until a date (liq floor −30%, hype min −20pp) |
| `/boost <mult> [hours]` | Temporary position-size multiplier, e.g. `/boost 1.5 6` |
| `/buy SYMBOL USD [chain] [price] [liq]` | Manual buy |
| `/sell SYMBOL <USD\|%\|all> [price] [liq]` | Manual sell |
| `/shadow <on\|off>` | Toggle paper trading at runtime |
| `/export_state` | Download current state as a JSON attachment |
| `/import_state {"key": val}` | Patch state values with inline JSON |
| `/doge_core <units>` | Set DOGE core bag target |
| `/doge_band <min> <max>` | Set DOGE trim exit price band |
| `/version` | Build SHA and current mode |
| `/restart now` | Save state and restart the process |

---

## Strategy modes

| Mode | TP | SL | Slippage | Min liq |
|---|---|---|---|---|
| `safe` | +15% | −7% | 0.6% | $50k |
| `default` | +28% | −12% | 1.0% | $30k |
| `hype` | +45%, +90% | −18% | 1.5% | $20k |
| `degen` | +80%, +150% | −28% | 2.2% | $10k |

Moonshot positions use separate TP cascades: +35% / +60% / +120%.

---

## Position lifecycle

### Entry gates (new tokens)

1. DexScreener boosts + Birdeye trending + CoinGecko trending scanned every 20 s
2. Age filter: pair must be ≤ 120 min old
3. Hype filter: h24 vol / liq ≥ hype_min threshold
4. Liquidity range: $25k–$250k (relaxed −30% during spray)
5. Price-impact check: order_usd / (liq × 10) ≤ 2%
6. Presale score gate: social mentions proxy via CoinGecko trending (or LunarCrush if key set); scores below `presale_min_score` are suggest-only
7. Burst guard: max 4 new opens per 30 s window
8. Capital caps: per-token, per-chain, daily deploy, drawdown brake

### Momentum exit (moonshot positions)

Priority order per engine tick (~20 s):

1. **Rug guard** — instant exit if liq drops > 40% in one tick
2. **Velocity exit** — exit if m5 price change < −8%
3. **Trailing stop** — exit if price falls > 15% from peak
4. **Liq drain** — exit if liq has declined > 5% for 3+ consecutive ticks
5. **Volume dry-up** — soft Telegram alert only (h1 vol < 20% of entry vol)
6. **TP cascade** — +35% / +60% / +120%, each fires exactly once (`tp_index` guard)
7. **Fixed SL** — mode SL (−12% in default)

### Re-entry watch

After any exit the token enters `reentry_watch`. The bot re-enters automatically when:
- 5+ min have elapsed since exit
- Price stable for 3 consecutive ticks (within 4% range)
- Liquidity ≥ 60% of what it was at original entry
- h1 volume still alive (> $3k)

### Autoscale

After `grace_sec` (90 s) in a position the bot may add 25% more when price velocity is positive, subject to the same capital caps and a 30-min cooldown between adds (max 2 adds total).

---

## Capital safety

| Guard | Default |
|---|---|
| Reserve | 25% of vault always kept out |
| Per-token cap | 12% of deployable per token |
| Per-chain cap | 40% sol / 35% eth / 30% bsc / 25% base & poly |
| Daily deploy cap | 20% of deployable per calendar day |
| Drawdown brake | Activates if net loss ≥ 25% × net gains over last 30 trades; reduces ticket size to 60% |
| Burst guard | Max 4 new position opens per 30 s |

---

## Dashboard

Alpine.js + Tailwind + Chart.js, no build step. Served at `/app`.

Panels:
- **Overview** — vault, deployable, income, deployed today, signal indicators, objective progress
- **Positions** — live PnL, m5 change (color-coded), trail-from-peak %, TP/SL overrides, manual close
- **Re-entry watch** — tokens waiting for a clean re-entry
- **History** — trade log with running PnL, win rate, avg win/loss

API endpoints (all under `/api/`):

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/state` | GET | Full state + config snapshot |
| `/api/positions` | GET | Open positions with live prices |
| `/api/history` | GET | Trade log, PnL stats |
| `/api/alerts` | GET | Recent alert log (last 200) |
| `/api/rpc` | GET | RPC health per chain |
| `/api/mode` | POST `{"mode": "hype"}` | Change risk mode |
| `/api/shadow` | POST `{"enabled": false}` | Toggle shadow mode |
| `/api/moonshot` | POST `{"mode": "enter"}` | Toggle moonshot auto-buy |
| `/api/auto_old` | POST `{"enabled": true}` | Toggle old-coin auto-join |
| `/api/boost` | POST `{"mult": 1.5, "hours": 4}` | Set size boost |
| `/api/spray` | POST `{"until": "2026-07-01T00:00:00+00:00"}` | Set spray-until date |
| `/api/objective` | POST `{"kind": "target", "target_usd": 500, "weeks": 4}` | Set profit target |
| `/api/buy` | POST `{"symbol": "X", "usd": 50, "price": 0.001, "chain": "sol"}` | Manual buy |
| `/api/sell` | POST `{"symbol": "X", "pct": 50}` | Partial sell |
| `/api/close` | POST `{"symbol": "X"}` | Full close at live price |
| `/api/position/tp` | POST `{"symbol": "X", "tp": [0.3, 0.6]}` | Override TP levels |
| `/api/position/sl` | POST `{"symbol": "X", "sl": 0.15}` | Override SL |
| `/api/watchlist/add` | POST `{"symbol": "DOGE", "address": "0x…"}` | Add to pump watchlist |
| `/api/watchlist/remove` | POST `{"symbol": "DOGE"}` | Remove from watchlist |
| `/api/config/oldcoin` | POST `{"volume_x": 5, "mentions_x": 2}` | Tune pump thresholds |

Set `DASHBOARD_TOKEN` in `.env` to require `Authorization: Bearer <token>` on all API calls.

---

## Shadow mode

The bot starts in shadow mode — all trades are simulated, vault is virtual, no on-chain transactions happen.

Flip it:
- Telegram: `/shadow off`
- API: `POST /api/shadow {"enabled": false}`
- Env: `SHADOW_MODE=false` in `.env`

In live mode the bot validates wallet env vars at startup and refuses to start if they're missing.

---

## Live execution

| Chain | Router |
|---|---|
| Solana | Jupiter v6 REST API; token decimals resolved from Solana RPC |
| EVM (eth/base/bsc/poly) | 1inch Aggregation Protocol v6 if `ONEINCH_API_KEY` set; otherwise direct Uniswap v3 / PancakeSwap v3 |

Balances are fetched on startup (live mode only) and refreshed every hour. Position units are computed from before/after balance diffs — no reliance on router-reported amounts.

---

## Configuration knobs

All in `CONFIG` at the top of [`src/bot/bot_full.py`](src/bot/bot_full.py).

```python
CONFIG = {
    "base_size_usd":        50.0,    # starting ticket before multipliers
    "reserve_pct":          0.25,    # vault fraction kept in reserve
    "per_token_cap_pct":    0.12,    # max fraction of deployable per token
    "daily_deploy_cap_pct": 0.20,    # max new deployment per day

    "moonshot": {
        "mode":              "suggest",  # "enter" to auto-buy
        "liq_min":           25000,
        "liq_max":           250000,
        "hype_min":          80,
        "trailing_stop_pct": 0.15,
        "velocity_exit_pct": 0.08,
        "vol_dry_pct":       0.20,
        "liq_drain_ticks":   3,
        "liq_drain_pct":     0.05,
        "reentry": {
            "enabled":        True,
            "cooldown_min":   5,
            "stable_ticks":   3,
            "stable_range_pct": 0.04,
            "liq_floor_pct":  0.60,
            "vol_min_h1":     3000.0,
        },
    },

    "presale_min_score": 10,  # 0 = disable gate; 70 = require audit/KYC
    "skim_pct":          0.10, # fraction of profit moved to income_usd

    "stealth": {
        "burst_per_30s":    4,   # max new opens per 30s
        "slip_bps_jitter":  30,  # random slippage noise (bps)
    },

    "drawdown_brake": {
        "lookback": 30,
        "dd":       0.25,        # brake if net loss ≥ 25% × net gains
        "size_mult": 0.60,       # reduce ticket to 60% when braked
    },

    "oldcoin": {
        "watchlist": {},         # {"DOGE": "token_address", ...}
        "volume_x":  10.0,       # h1 vol spike multiplier
        "mentions_x": 2.0,       # social volume multiplier gate
        "auto_join": False,
        "tiny_entry_usd": 20.0,
    },
}
```
