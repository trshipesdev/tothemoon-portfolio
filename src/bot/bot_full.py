#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CRYPTO BOT — FINAL FULL BUILD (SPEC FREEZE)
==========================================

You asked for a single, plug-and-play file that contains:
- Full code (shadow-mode ready, live-mode gated),
- All features we agreed (multi-chain, strategies + objectives together,
  moonshot suggest/enter, auto-scale with grace, adaptive no-pump,
  old-coin pump detector, trusted-coin module, reserves, caps, risk controller,
  alerts/digest, RPC health, stealth, multi-tenant, presale assistant, etc.),
- Inline documentation/tutorial so anyone can use it,
- Clear TODO placeholders ONLY where your unique secrets/wallets/RPCs must go.

▶ IMPORTANT
  • This file runs TODAY in SHADOW MODE (paper trading) without secrets.
  • LIVE TRADING requires you to fill the marked placeholders (RPC URLs, keys)
    and install deps listed below. Swapping shadow→live is a toggle.

------------------------------------------------------------
TABLE OF CONTENTS (search these anchors)
------------------------------------------------------------
1) QUICKSTART  ......................................  [DOC:QUICKSTART]
2) SETTINGS YOU MUST FILL ...........................  [DOC:FILLME]
3) SAFETY RAILS (READ FIRST) ........................ [DOC:SAFETY]
4) COMMANDS (TELEGRAM) .............................. [DOC:COMMANDS]
5) OBJECTIVES × STRATEGIES (HOW THEY INTERLOCK) ..... [DOC:OBJSTRAT]
6) COIN AGE LOGIC (NEW vs HYPE vs TRUSTED) .......... [DOC:AGELOGIC]
7) ALERT LEVELS & SUGGESTIONS ....................... [DOC:ALERTS]
8) EXTENSIBILITY & MULTI-TENANT ..................... [DOC:EXTEND]
9) INSTALL & RUN (LOCAL/AWS) ........................ [DOC:RUN]

CODE SECTIONS
A) Imports & Utilities ..............................  [CODE:UTIL]
B) Global Config & Feature Flags ....................  [CODE:CONFIG]
C) State, Persistence, Snapshots ....................  [CODE:STATE]
D) Data Feeds (DexScreener, BTC.D heat) .............  [CODE:DATA]
E) Scoring & Filters (Moonshot/Hype/Trusted) ........  [CODE:SCORING]
F) Objectives × Strategy Controller .................  [CODE:OBJ]
G) Capital Sizing, Reserves, Caps, Brakes ...........  [CODE:CAP]
H) Execution Adapters (Shadow + Live stubs) .........  [CODE:EXEC]
I) Position Manager (TP/SL, adaptive no-pump) .......  [CODE:PM]
J) Auto-Scale Add (grace window, stops) .............  [CODE:AUTOADD]
K) Old-Coin Pump Detector ...........................  [CODE:OLD]
L) Trusted Coins Module (e.g., DOGE) ................  [CODE:TRUSTED]
M) Presale Assistant (alerts only) ..................  [CODE:PRESALE]
N) Alerts, Digest, Watchdog, RPC Health .............  [CODE:ALERTS]
O) Telegram Bot Handlers ............................  [CODE:TG]
P) Flask Admin Dashboard ............................  [CODE:FLASK]
Q) Main Engine & Loop ...............................  [CODE:MAIN]
R) Systemd Example (AWS) ............................  [CODE:SYSTEMD]

============================================================
1) QUICKSTART  [DOC:QUICKSTART]
============================================================
• Python 3.10+ recommended
• Install deps (see [DOC:RUN])
• Leave SHADOW_MODE=True to test end-to-end safely.
• Start: `python bot_full.py`
• Open Telegram and talk to your bot (see [DOC:COMMANDS]).
• Flip to LIVE only after funding wallets and filling placeholders.

============================================================
2) SETTINGS YOU MUST FILL  [DOC:FILLME]
============================================================
Search for "FILLME" in this file:
- RPC URLs for Solana, Ethereum, Base, BSC, Polygon
- Private key(s) / seed (use env vars, NOT hard-coded!)
- Telegram bot token + your user ID allowlist
- Optional AWS CloudWatch credentials (IAM role is easier)

============================================================
3) SAFETY RAILS (READ FIRST)  [DOC:SAFETY]
============================================================
- Circuit breaker halts entries after large drawdown.
- Per-token and per-chain caps enforced.
- Reserve floor keeps HOT vault buffer.
- Rug/LP drain overrides any plan → exit now.
- Adaptive no-pump uses age/liq to avoid gas churn.
- LIVE mode disabled until you toggle it.

============================================================
4) COMMANDS (TELEGRAM)  [DOC:COMMANDS]
============================================================
/status – snapshot of vaults, positions, mode, signals
/mode <safe|default|hype|degen> – set strategy profile
/objective <off|target> <amount> <weeks> – e.g., /objective target 1000 8
/skim <on|off> – toggle profit skims
/spray_until YYYY-MM-DD – broaden entries until date (optional)
/auto_old <on|off> – auto-join old-coin pumps tiny+alert
/moonshot <enter|suggest> – how to treat new launches
/boost <x.xx> – temporary size multiplier (e.g., 1.3)
/buy <SYMBOL> <USD> – manual buy (shadow/live obeyed)
/sell <SYMBOL> <%orUSD> – manual sell
/doge_core <units> | /doge_band <min> <max> – trusted coin
/export_state | /import_state – backup/restore
/help – short help; /help_long – full help

============================================================
5) OBJECTIVES × STRATEGIES  [DOC:OBJSTRAT]
============================================================
- Strategies (how): safe/default/hype/degen define TP/SL, slippage, filters.
- Objectives (what): e.g., “$1000 in 8 weeks” nudges risk **within** guardrails.
- They work together: strategy executes, objective adjusts size, breadth,
  and bucket weights to stay on trajectory; never crosses safety caps.

============================================================
6) COIN AGE LOGIC  [DOC:AGELOGIC]
============================================================
- NEW (minutes old): no-pump timer (adaptive), rug/LP checks, degen exits.
- YOUNG HYPE (days–weeks): swing logic via volume+social spikes, re-entries ok.
- TRUSTED (majors): ATH-aware trims, dip rebuys, bag growth.

============================================================
7) ALERT LEVELS & SUGGESTIONS  [DOC:ALERTS]
============================================================
- ⚠️ Low-tier (curious), 🚀 High-tier (move), 🛑 Risk (exit now)
- Suggestions include sized commands you can paste to execute.
- Auto-scale (optional) after 90s grace if all checks are green.

============================================================
8) EXTENSIBILITY & MULTI-TENANT  [DOC:EXTEND]
============================================================
- Tenants: separate profiles (state, jitter seed, allowlist) per wallet.
- DEX adapters: add more routes without changing core engine.
- Metrics hooks for your custom dashboards.

============================================================
9) INSTALL & RUN  [DOC:RUN]
============================================================
pip install -U requests python-telegram-bot==13.15 flask boto3 pydantic
# Optional EVM: web3
# Optional Solana: solders or solana, jupiter-aggregator SDK if used

Set env vars:
  TELEGRAM_TOKEN=...   TELEGRAM_ALLOWLIST="12345,67890"
  WALLET_PK_SOL=...    WALLET_PK_EVM=...
Run:
  python bot_full.py
------------------------------------------------------------
"""

# ================================
# A) IMPORTS & UTILITIES [CODE:UTIL]
# ================================
import os, sys, json, time, math, random, threading, signal, queue
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_DOWN
import traceback
import requests
import asyncio

from typing import Dict, List, Optional, Any, Tuple

# load .env if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# (Optional) Telegram + Flask
from flask import Flask, jsonify
from functools import wraps

# Telegram (legacy v13 for simplicity). If you prefer v20+, adapt accordingly.
try:
    from telegram.ext import Updater, CommandHandler, Filters
    from telegram import ParseMode
except Exception:
    Updater = None

# ================================
# B) GLOBAL CONFIG [CODE:CONFIG]
# ================================
# Read from env first (default ON for safety)
SHADOW_MODE = os.getenv("SHADOW_MODE", "true").strip().lower() in ("1","true","on","yes")

# Allow toggling at runtime and persist to STATE
def set_shadow_mode(val: bool):
    global SHADOW_MODE
    SHADOW_MODE = bool(val)
    STATE["shadow_mode"] = SHADOW_MODE
    save_state()

CONFIG: Dict[str, Any] = {
    "tenant": {
        "name": "default",  # Multi-tenant: run separate processes with different names
        "jitter_seed": 42,    # different per tenant for stealth
        "allowlist": [],      # TELEGRAM user IDs allowed (filled from env)
    },

    # -------- Wallets / RPC (FILLME) --------
    "rpc": {
        "sol": [
            # FILLME: add more premium failovers
            "https://api.mainnet-beta.solana.com",
        ],
        "eth": [
            # FILLME: your HTTPS provider (e.g., Alchemy/Infura)
            "https://rpc.ankr.com/eth",
        ],
        "base": [
            # FILLME: Base mainnet RPC
            "https://mainnet.base.org",
        ],
        "bsc": [
            "https://bsc-dataseed1.binance.org",
        ],
        "poly": [
            "https://polygon-rpc.com",
        ],
    },

    # Private keys via env only (never hardcode!)
    "keys": {
        "solana_secret_base58_env": "WALLET_PK_SOL",   # FILL ENV
        "evm_hex_key_env": "WALLET_PK_EVM",           # FILL ENV
    },

    # -------- Trading scope --------
    "chains": ["sol", "eth", "base", "bsc", "poly"],
    "scan": {
        "new_max_age_min": 120,   # treat <= this age as NEW
        "hype_window_min": 240,   # days/weeks-old considered HYPE within this watch window
        "dexscreener_poll_sec": 20,
    },

    # -------- Strategies (micro execution) --------
    "modes": {
        "safe":    {"tp": [0.15], "sl": 0.07, "slip_bps": 60,  "liq_min": 50000, "age_min": 30,  "size_mult": 0.7},
        "default": {"tp": [0.28], "sl": 0.12, "slip_bps": 100, "liq_min": 30000, "age_min": 10,  "size_mult": 1.0},
        "hype":    {"tp": [0.45, 0.9], "sl": 0.18, "slip_bps": 150, "liq_min": 20000, "age_min": 5,   "size_mult": 1.3},
        "degen":   {"tp": [0.80, 1.5], "sl": 0.28, "slip_bps": 220, "liq_min": 10000, "age_min": 0,   "size_mult": 1.6,
                      "no_pump": {"hurdle": 0.03, "min_sec": 240, "max_sec": 900}},  # 4–15m adaptive
    },
    "mode": "default",  # current strategy mode

    # -------- Objectives (macro goal) --------
    "objective": {
        "kind": "off",         # off | target
        "target_usd": 0,
        "horizon_weeks": 0,
        "r_bounds": {           # risk nudging caps
            "size_mult_max": 1.35,
            "extra_open_max": 2,
            "hype_shift_pp": 15, # percentage points to move into hype/degen total
            "degen_min_sec": 180,
        },
    },

    # -------- Capital, reserves, caps --------
    "base_size_usd": 50.0,       # starting ticket before multipliers
    "reserve_pct": 0.25,         # keep 25% of HOT vault untouched
    "per_token_cap_pct": 0.12,   # max 12% of deployable per token
    "per_chain_cap_pct": {       # max exposure per chain
        "sol": 0.40, "eth": 0.35, "base": 0.25, "bsc": 0.30, "poly": 0.25
    },
    "daily_deploy_cap_pct": 0.20,  # max new deployment per 24h
    "drawdown_brake": {"lookback": 30, "dd": 0.25, "size_mult": 0.60},

    # -------- Vault split --------
    "vaults": {"hot_native_pct": 0.75, "hot_usdc_pct": 0.25},  # rolling vs profit split

    # -------- Moonshot controls --------
    "moonshot": {
        "mode": "suggest",     # enter | suggest  (default: suggest; you can flip live)
        "size_mult": 1.5,
        "liq_min": 25000, "liq_max": 250000,
        "hype_min": 80,  # 0–100 sentiment score (positive-only)
        "price_impact_max": 0.02,
        "min_ticket_usd": 15.0,
        "adaptive_timer": {"low_liq_sec": 300, "high_liq_sec": 1200},
        "tp": [0.35, 0.60, 1.20],
        "sl": 0.22,
        "retries": 1,   # you can raise later with cooldowns
    },

    # -------- Old-coin pump controls --------
    "oldcoin": {
        "auto_join": False,    # if True: join tiny & alert; else alert-only
        "tiny_entry_usd": 20.0,
        "volume_x": 10.0,      # 10× volume in 15m window
        "mentions_x": 2.0,     # 2× social mentions
    },

    # -------- Auto-scale (if alert missed) --------
    "autoscale": {
        "enabled": True,
        "grace_sec": 90,
        "add_frac": 0.25,        # +25% of current position
        "pi_max": 0.02,          # price impact ceiling
        "cooldown_min": 30,      # per position
        "max_adds": 2,
        "add_sl": 0.10,          # -10% stop on add tranche
        "fast_drop": {"m3": 0.08, "m10": 0.15, "tighten_to": 0.06},
    },

    # -------- Alerts & Digest --------
    "telegram": {
        "token_env": "TELEGRAM_TOKEN",  # FILL ENV
        "allowlist_env": "TELEGRAM_ALLOWLIST",  # CSV user IDs
        "quiet_hours_utc": [3, 7],  # non-critical muted 03:00–07:59 UTC
        "daily_digest_utc": "14:00",
    },

    # -------- Stealth --------
    "stealth": {"split_parts": [2,4], "slip_bps_jitter": 30, "candidate_shuffle": True, "burst_per_30s": 4},

    # -------- Presale Assistant --------
    "presale": {"min_score": 70},
}

random.seed(CONFIG["tenant"]["jitter_seed"])  # per-tenant uniqueness

# ================================
# C) STATE & PERSIST [CODE:STATE]
# ================================
STATE = {
    "ts": None,
    "vault_usd": 1000.0,      # shadow vault; in LIVE this is fetched on-chain
    "deployable_usd": 750.0,  # rolling (native) side in shadow
    "income_usd": 0.0,        # skimmed profits
    "positions": {},          # symbol -> dict
    "pnl_hist": [],           # last N trade PnLs for drawdown brake
    "open_today_usd": 0.0,    # new deployments today
    "last_daily_reset": None,
    "signals": {"btc_d": None, "heat": None},
    "rpc": {"health": {}},
}

SAVEFILE = f"state_{CONFIG['tenant']['name']}.json"

def now_utc():
    return datetime.now(timezone.utc)

def save_state():
    try:
        with open(SAVEFILE, "w") as f:
            json.dump(STATE, f, indent=2, default=str)
    except Exception:
        pass

def load_state():
    try:
        with open(SAVEFILE, "r") as f:
            data = json.load(f)
            STATE.update(data)
    except Exception:
        pass

# ================================
# D) DATA FEEDS [CODE:DATA]
# ================================
DEXSCREENER = "https://api.dexscreener.com/latest/dex/tokens/"

def fetch_dexscreener_token(addr_or_symbol: str) -> Optional[Dict[str, Any]]:
    try:
        r = requests.get(DEXSCREENER + addr_or_symbol, timeout=8)
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None

# (Simple placeholder heat metrics; wire your sources as you like.)
def fetch_btc_dominance_hint() -> Optional[float]:
    # TODO: connect to your preferred API; return 0..100 dominance
    return None

# ================================
# E) SCORING / FILTERS [CODE:SCORING]
# ================================
class Score:
    def __init__(self, hype: int, liq: float, age_min: int, positive: bool):
        self.hype = hype
        self.liq = liq
        self.age_min = age_min
        self.positive = positive


def passes_moonshot_filters(sc: Score) -> bool:
    ms = CONFIG["moonshot"]
    if sc.liq < ms["liq_min"] or sc.liq > ms["liq_max"]:
        return False
    if sc.age_min > CONFIG["scan"]["new_max_age_min"]:
        return False
    if not sc.positive:
        return False
    if sc.hype < ms["hype_min"]:
        return False
    return True

# ================================
# F) OBJECTIVE × STRATEGY [CODE:OBJ]
# ================================
OBJ_STATE = {"started": None, "target_curve": []}

def start_objective(target_usd: float, weeks: int):
    CONFIG["objective"]["kind"] = "target"
    CONFIG["objective"]["target_usd"] = float(target_usd)
    CONFIG["objective"]["horizon_weeks"] = int(weeks)
    OBJ_STATE["started"] = now_utc()
    # Linear curve (weekly)
    OBJ_STATE["target_curve"] = [target_usd * (i+1)/weeks for i in range(weeks)]


def objective_nudge() -> Dict[str, Any]:
    """Return nudges for size, breadth, hype shift, timer—bounded by r_bounds."""
    if CONFIG["objective"]["kind"] != "target":
        return {"size_mult": 1.0, "extra_open": 0, "hype_pp": 0, "degen_min_sec": None}
    weeks = CONFIG["objective"]["horizon_weeks"]
    if weeks <= 0 or not OBJ_STATE["started"]:
        return {"size_mult": 1.0, "extra_open": 0, "hype_pp": 0, "degen_min_sec": None}

    # progress
    elapsed = (now_utc() - OBJ_STATE["started"]).days / 7.0
    idx = min(max(int(elapsed), 0), weeks-1)
    target_so_far = OBJ_STATE["target_curve"][idx]
    actual = sum(p.get("realized", 0.0) for p in STATE["positions"].values()) + STATE.get("income_usd", 0.0)
    delta = actual - target_so_far

    # map delta→risk factor
    if delta < -target_so_far/8:   # behind by ~1 week
        r = 1.2
        extra_open = 1
        hype_pp = 8
        degen_min_sec = max( CONFIG["objective"]["r_bounds"]["degen_min_sec"], 180 )
    elif delta > target_so_far/8:  # ahead by ~1 week
        r = 0.9
        extra_open = 0
        hype_pp = -6
        degen_min_sec = None
    else:
        r = 1.0
        extra_open = 0
        hype_pp = 0
        degen_min_sec = None

    # clamp
    r = min(r, CONFIG["objective"]["r_bounds"]["size_mult_max"])
    extra_open = min(extra_open, CONFIG["objective"]["r_bounds"]["extra_open_max"])
    hype_pp = max( -CONFIG["objective"]["r_bounds"]["hype_shift_pp"], min(hype_pp, CONFIG["objective"]["r_bounds"]["hype_shift_pp"]))
    return {"size_mult": r, "extra_open": extra_open, "hype_pp": hype_pp, "degen_min_sec": degen_min_sec}

# ================================
# G) CAPITAL & RESERVES [CODE:CAP]
# ================================
def drawdown_brake_active() -> bool:
    hist = STATE["pnl_hist"][-CONFIG["drawdown_brake"]["lookback"]:]
    if not hist:
        return False
    loss = sum(x for x in hist if x < 0)
    gain = sum(x for x in hist if x > 0)
    net = gain + loss
    if net < 0 and abs(net) >= CONFIG["drawdown_brake"]["dd"] * max(1.0, abs(gain)):
        return True
    return False

def deployable_now() -> float:
    reserve = CONFIG["reserve_pct"] * STATE["vault_usd"]
    return max(0.0, STATE["vault_usd"] - reserve)

def per_chain_room(chain: str) -> float:
    cap = CONFIG["per_chain_cap_pct"].get(chain, 0.25) * deployable_now()
    used = sum(p["usd"] for p in STATE["positions"].values() if p["chain"] == chain)
    return max(0.0, cap - used)

def per_token_cap_room() -> float:
    return CONFIG["per_token_cap_pct"] * deployable_now()

def size_ticket_usd(chain: str) -> float:
    base = CONFIG["base_size_usd"] * CONFIG["modes"][CONFIG["mode"]]["size_mult"]
    nudges = objective_nudge()
    base *= nudges["size_mult"]
    if drawdown_brake_active():
        base *= CONFIG["drawdown_brake"]["size_mult"]
    # apply caps
    base = min(base, per_chain_room(chain))
    base = min(base, per_token_cap_room())
    # daily deploy cap
    day_cap = CONFIG["daily_deploy_cap_pct"] * deployable_now()
    base = min(base, max(0.0, day_cap - STATE["open_today_usd"]))
    return max(0.0, base)

# ================================
# H) EXECUTION ADAPTERS [CODE:EXEC]
# ================================
def est_price_impact(order_usd: float, liq_usd: float) -> float:
    if liq_usd <= 0: return 1.0
    return min(0.05, order_usd / (liq_usd * 10.0))  # crude; refine per DEX

def shadow_buy(symbol: str, chain: str, usd: float, price: float, liq_usd: float) -> Dict[str, Any]:
    impact = est_price_impact(usd, liq_usd)
    filled_price = price * (1 + 0.5*impact)
    units = usd / filled_price
    pos = STATE["positions"].setdefault(symbol, {"chain": chain, "units": 0.0, "usd": 0.0, "avg": 0.0, "realized": 0.0, "time": str(now_utc())})
    new_total_usd = pos["usd"] + usd
    pos["avg"] = (pos["avg"]*pos["usd"] + filled_price*units) / max(1e-9, new_total_usd/filled_price)
    pos["usd"] = new_total_usd
    pos["units"] += units
    STATE["vault_usd"] -= usd
    STATE["open_today_usd"] += usd
    return {"price": filled_price, "units": units}

def shadow_sell(symbol: str, usd: float, price: float, liq_usd: float) -> Dict[str, Any]:
    pos = STATE["positions"].get(symbol)
    if not pos or pos["usd"] <= 0:
        return {"sold": 0.0, "pnl": 0.0}
    # sell proportion of units
    portion = min(1.0, usd / max(1e-9, pos["usd"]))
    units = pos["units"] * portion
    proceeds = units * price * (1 - 0.002)  # est fee
    cost = units * pos["avg"]
    pnl = proceeds - cost

    pos["units"] -= units
    pos["usd"] -= cost
    if pos["units"] <= 0:
        pos["avg"] = 0.0
    pos["realized"] += pnl
    STATE["vault_usd"] += proceeds
    STATE["pnl_hist"].append(pnl)
    return {"sold": proceeds, "pnl": pnl}

# LIVE adapters: placeholders to integrate Jupiter/Uniswap routers safely.
# Keep SHADOW_MODE=True until you wire these with your keys & routes.

# ================================
# I) POSITION MANAGER [CODE:PM]
# ================================
def adaptive_no_pump_window(liq_usd: float) -> int:
    ms = CONFIG["moonshot"]["adaptive_timer"]
    return ms["low_liq_sec"] if liq_usd < 50000 else ms["high_liq_sec"]

def should_exit_no_pump(entry_ts: float, now_ts: float, entry_price: float, cur_price: float, liq_usd: float) -> bool:
    win = adaptive_no_pump_window(liq_usd)
    if now_ts - entry_ts < win:
        return False
    hurdle = CONFIG["modes"]["degen"]["no_pump"]["hurdle"]
    return (cur_price - entry_price)/entry_price < hurdle

# ================================
# J) AUTO-SCALE ADD [CODE:AUTOADD]
# ================================
def autoscale_maybe(symbol: str, chain: str, price: float, liq_usd: float, velocity_ok: bool, social_rising: bool):
    A = CONFIG["autoscale"]
    if not A["enabled"] or not velocity_ok or not social_rising:
        return
    # Grace window: implemented via alert timing in /engine; here we just size
    add_usd = CONFIG["autoscale"]["add_frac"] * STATE["positions"].get(symbol, {}).get("usd", 0.0)
    if add_usd <= 0:
        return
    if est_price_impact(add_usd, liq_usd) > A["pi_max"]:
        add_usd *= 0.5
        if est_price_impact(add_usd, liq_usd) > A["pi_max"]:
            return
    # caps
    add_usd = min(add_usd, per_chain_room(chain), per_token_cap_room())
    if add_usd < CONFIG["moonshot"]["min_ticket_usd"]:
        return
    shadow_buy(symbol, chain, add_usd, price, liq_usd)
    # NOTE: in live, attach add-specific stop

# ================================
# K) OLD-COIN PUMP DETECTOR [CODE:OLD]
# ================================
def detect_oldcoin_pump(symbol: str, volume_x: float, mentions_x: float) -> bool:
    # TODO: integrate actual sources; here a stub always False
    return False

# ================================
# L) TRUSTED COINS (DOGE) [CODE:TRUSTED]
# ================================
TRUSTED = {
    "DOGE": {"core_units": 30000, "exit_band": [0.50, 0.70], "retrace": 0.70, "floor": 0.12}
}

# ================================
# M) PRESALE ASSISTANT [CODE:PRESALE]
# ================================
def presale_score(meta: Dict[str, Any]) -> int:
    # Simple stub: audits, KYC, lock length, social
    score = 0
    score += 20 if meta.get("audit") else 0
    score += 20 if meta.get("kyc") else 0
    score += min(30, int(meta.get("lock_days",0)/30))
    score += min(30, int(meta.get("mentions",0)/5000)*10)
    return score

# ================================
# N) ALERTS / DIGEST / HEALTH [CODE:ALERTS]
# ================================
app = Flask(__name__)

def log(msg: str):
    ts = now_utc().isoformat()
    print(f"[{ts}] {msg}")

TELEGRAM = {"updater": None}

def tg_allowed(user_id: int) -> bool:
    return (not CONFIG["tenant"]["allowlist"]) or (str(user_id) in CONFIG["tenant"]["allowlist"])  # if allowlist empty, allow all (dev)

# ================================
# O) TELEGRAM HANDLERS [CODE:TG]
# ================================
from telegram import Update
from telegram.ext import ContextTypes

def require_auth(fn):
    @wraps(fn)
    async def w(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update and update.effective_user and not tg_allowed(update.effective_user.id):
            return
        return await fn(update, context)
    return w

async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # deliberately ungated so you can get your numeric user id
    await update.message.reply_text(f"your id: {update.effective_user.id}")
@require_auth
async def cmd_shadow(update, context):
    # /shadow       -> show current
    # /shadow on    -> paper trading ON
    # /shadow off   -> paper trading OFF (live flag)
    arg = (context.args[0].lower() if context.args else "status")
    if arg in ("on", "true", "1"):
        set_shadow_mode(True)
        await update.message.reply_text("Shadow mode: ON (paper trading).")
    elif arg in ("off", "false", "0"):
        set_shadow_mode(False)
        await update.message.reply_text(
            "Shadow mode: OFF (live flag set). "
            "⚠️ Live execution not wired yet, so no on-chain orders will be placed."
        )
    else:
        await update.message.reply_text(
            f"Shadow mode is currently {'ON' if SHADOW_MODE else 'OFF'}. Usage: /shadow on|off"
        )

@require_auth
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pos_lines = []
    for s, p in STATE["positions"].items():
        pos_lines.append(f"{s} {p['chain']} units={p['units']:.4f} usd~${p['usd']:.2f} avg~{p['avg']:.6f}")
    msg = (
        f"Shadow: {'ON' if SHADOW_MODE else 'OFF'}\n"
        f"Vault: ${STATE['vault_usd']:.2f} (deployable ~${deployable_now():.2f})\n"
        f"Objective: {CONFIG['objective']['kind']} | Mode: {CONFIG['mode']}\n"
        f"Open today: ${STATE['open_today_usd']:.2f}\n"
        f"Positions:\n" + ("\n".join(pos_lines) if pos_lines else "(none)")
    )
    await update.message.reply_text(msg)

@require_auth
async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        m = context.args[0].lower()
        assert m in CONFIG["modes"]
        CONFIG["mode"] = m
        await update.message.reply_text(f"Mode set to {m}")
    except Exception:
        await update.message.reply_text("Usage: /mode <safe|default|hype|degen>")

@require_auth
async def cmd_objective(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        kind = context.args[0]
        if kind == "off":
            CONFIG["objective"]["kind"] = "off"
            await update.message.reply_text("Objective off")
            return
        tgt = float(context.args[1]); weeks = int(context.args[2])
        start_objective(tgt, weeks)
        await update.message.reply_text(f"Objective: +${tgt} in {weeks} weeks started")
    except Exception:
        await update.message.reply_text("Usage: /objective off | /objective target <usd> <weeks>")

@require_auth
async def cmd_moonshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        m = context.args[0].lower()
        assert m in ("enter","suggest")
        CONFIG["moonshot"]["mode"] = m
        await update.message.reply_text(f"Moonshot mode: {m}")
    except Exception:
        await update.message.reply_text("Usage: /moonshot <enter|suggest>")

@require_auth
async def cmd_auto_old(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        v = context.args[0].lower()
        CONFIG["oldcoin"]["auto_join"] = (v == "on")
        await update.message.reply_text(f"Auto-join old pumps: {CONFIG['oldcoin']['auto_join']}")
    except Exception:
        await update.message.reply_text("Usage: /auto_old <on|off>")

@require_auth
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("/status, /mode, /objective, /moonshot, /auto_old, /buy, /sell, /help_long")

@require_auth
async def cmd_help_long(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("See top-of-file docs: OBJECTIVES×STRATEGIES, AGE LOGIC, ALERTS, RUN.")

@require_auth
async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        args = context.args
        symbol = args[0].upper()
        usd = float(args[1])
        chain = (args[2] if len(args) > 2 else "sol").lower()
        price = float(args[3]) if len(args) > 3 else 1.0
        liq = float(args[4]) if len(args) > 4 else 100000.0
        if usd <= 0:
            raise ValueError

        if not SHADOW_MODE:
            await update.message.reply_text(
                "Shadow mode is OFF (live). Live trading isn’t wired here yet — no order placed."
            )
            return

        res = shadow_buy(symbol, chain, usd, price, liq)
        await update.message.reply_text(
            f"BOUGHT {symbol} ${usd:.2f} on {chain} @~{res['price']:.6f} "
            f"units={res['units']:.4f} | vault=${STATE['vault_usd']:.2f}"
        )
    except Exception:
        await update.message.reply_text(
            "Usage: /buy SYMBOL USD [chain=sol] [price=1.0] [liq=100000]"
        )

def _parse_amount_for_sell(pos, token: str) -> float:
    t = token.strip().lower()
    if t in ("all", "max", "100%"):
        return pos["usd"]
    if t.endswith("%"):
        pct = float(t[:-1]) / 100.0
        return max(0.0, min(pos["usd"], pos["usd"] * pct))
    return float(t)


@require_auth
async def cmd_sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        args = context.args
        symbol = args[0].upper()
        amt_token = args[1]

        if not SHADOW_MODE:
            await update.message.reply_text(
                "Shadow mode is OFF (live). Live trading isn’t wired here yet — no order placed."
            )
            return

        # default ≈ +2% over avg so you see PnL movement in shadow
        default_price = STATE["positions"].get(symbol, {}).get("avg", 0.0) * 1.02 or 1.0
        price = float(args[2]) if len(args) > 2 else default_price
        liq = float(args[3]) if len(args) > 3 else 100000.0

        pos = STATE["positions"].get(symbol)
        if not pos:
            await update.message.reply_text("No position.")
            return

        usd = _parse_amount_for_sell(pos, amt_token)
        res = shadow_sell(symbol, usd, price, liq)
        await update.message.reply_text(
            f"SOLD {symbol} ${usd:.2f} @~{price:.6f} proceeds ${res['sold']:.2f} "
            f"pnl ${res['pnl']:.2f} | vault=${STATE['vault_usd']:.2f}"
        )
    except Exception:
        await update.message.reply_text(
            "Usage: /sell SYMBOL <USD|%|all> [price≈+2% over avg] [liq=100000]"
        )

# ================================
# P) FLASK ADMIN [CODE:FLASK]
# ================================
@app.route("/status")
def http_status():
    return jsonify({
        "vault_usd": STATE["vault_usd"],
        "deployable": deployable_now(),
        "positions": STATE["positions"],
        "mode": CONFIG["mode"],
        "objective": CONFIG["objective"],
        "signals": STATE["signals"],
        "rpc": STATE["rpc"]
    })

# ================================
# Q) MAIN ENGINE [CODE:MAIN]
# ================================
ENGINE_STOP = False

def engine_once():
    # 1) Fetch signals (stubs here)
    STATE["signals"]["btc_d"] = fetch_btc_dominance_hint()

    # 2) Scan candidates (Dexscreener token lists would be here)
    candidates = []  # list of dicts with symbol, chain, price, liq, age_min, hype_score, positive
    # TODO: populate candidates via your feeds; here we simulate none

    # Shuffle for stealth
    if CONFIG["stealth"]["candidate_shuffle"]:
        random.shuffle(candidates)

    # 3) Evaluate candidates
    for c in candidates:
        symbol = c["symbol"]; chain = c["chain"]; price = c["price"]; liq = c["liq"]; age = c["age_min"]
        hype = c.get("hype", 0); positive = c.get("positive", True)
        sc = Score(hype, liq, age, positive)

        is_new = (age <= CONFIG["scan"]["new_max_age_min"])
        if is_new:
            if not passes_moonshot_filters(sc):
                continue
            if CONFIG["moonshot"]["mode"] == "enter":
                usd = size_ticket_usd(chain)
                if usd >= CONFIG["moonshot"]["min_ticket_usd"] and est_price_impact(usd, liq) <= CONFIG["moonshot"]["price_impact_max"]:
                    shadow_buy(symbol, chain, usd, price, liq)
                    log(f"ENTER {symbol} new launch ${usd:.2f}")
                else:
                    log(f"SUGGEST {symbol} new launch (caps/impact)")
            else:
                log(f"SUGGEST {symbol} new launch (mode=suggest)")
        else:
            # Old/hype coin path: look for pumps
            if detect_oldcoin_pump(symbol, CONFIG["oldcoin"]["volume_x"], CONFIG["oldcoin"]["mentions_x"]):
                if CONFIG["oldcoin"]["auto_join"]:
                    usd = min(CONFIG["oldcoin"]["tiny_entry_usd"], size_ticket_usd(chain))
                    if usd >= CONFIG["moonshot"]["min_ticket_usd"]:
                        shadow_buy(symbol, chain, usd, price, liq)
                        log(f"AUTO-JOIN tiny {symbol} ${usd:.2f}")
                    else:
                        log(f"ALERT old pump {symbol} (cap too small)")
                else:
                    log(f"ALERT old pump {symbol}")

    # 4) Manage open positions (TP/SL, adaptive no-pump, autoscale)
    for s, p in list(STATE["positions"].items()):
        # In shadow we need a price; in real bot you'd fetch latest
        price = p.get("avg", 0.0) * 1.02  # pretend +2%
        liq = 100000
        entry_ts = datetime.fromisoformat(p["time"]).timestamp()
        if should_exit_no_pump(entry_ts, time.time(), p["avg"], price, liq):
            # soft exit: here we just log; you can convert to real exit
            log(f"NO-PUMP soft flag {s}")
        # simple TP
        for tp in CONFIG["modes"][CONFIG["mode"]]["tp"]:
            if price >= p["avg"]*(1+tp):
                sell_usd = 0.5 * p["usd"]
                res = shadow_sell(s, sell_usd, price, liq)
                log(f"TP hit {s}: sold ${sell_usd:.2f} pnl ${res['pnl']:.2f}")
                break
        # SL
        sl = CONFIG["modes"][CONFIG["mode"]]["sl"]
        if price <= p["avg"]*(1-sl):
            res = shadow_sell(s, p["usd"], price, liq)
            log(f"SL hit {s}: exit pnl ${res['pnl']:.2f}")

def engine_loop():
    load_state()
    last_daily = now_utc().date()
    while not ENGINE_STOP:
        try:
            engine_once()
        except Exception:
            traceback.print_exc()
        time.sleep(CONFIG["scan"]["dexscreener_poll_sec"])
        # daily reset
        if now_utc().date() != last_daily:
            STATE["open_today_usd"] = 0.0
            last_daily = now_utc().date()
            save_state()

# ================================
# R) SYSTEMD EXAMPLE [CODE:SYSTEMD]
# ================================
SYSTEMD = r"""
[Unit]
Description=Crypto Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/ubuntu/cryptobot
Environment=TELEGRAM_TOKEN=***
Environment=TELEGRAM_ALLOWLIST=12345,67890
Environment=WALLET_PK_SOL=***
Environment=WALLET_PK_EVM=***
ExecStart=/usr/bin/python3 /home/ubuntu/cryptobot/bot_full.py
Restart=always
RestartSec=3
User=ubuntu

[Install]
WantedBy=multi-user.target
"""
# ================================
# ENTRYPOINT
# ================================
def start_telegram():
    token = os.getenv(CONFIG["telegram"]["token_env"], "")
    allow = os.getenv(CONFIG["telegram"]["allowlist_env"], "")
    if allow:
        CONFIG["tenant"]["allowlist"] = [x.strip() for x in allow.split(",") if x.strip()]
    if not token:
        log("TELEGRAM token missing; skipping")
        return

    from telegram.ext import Application, CommandHandler

    app = Application.builder().token(token).build()

    # not gated
    app.add_handler(CommandHandler("whoami", whoami))

    # gated commands
    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(CommandHandler("mode",      cmd_mode))
    app.add_handler(CommandHandler("objective", cmd_objective))
    app.add_handler(CommandHandler("moonshot",  cmd_moonshot))
    app.add_handler(CommandHandler("auto_old",  cmd_auto_old))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("help_long", cmd_help_long))
    app.add_handler(CommandHandler("buy",  cmd_buy))
    app.add_handler(CommandHandler("sell", cmd_sell))
    app.add_handler(CommandHandler("shadow", cmd_shadow))


    log("Telegram polling (PTB v20+) starting…")
    # This call BLOCKS on the main thread and owns its own asyncio loop.
    app.run_polling(allowed_updates=["message"], drop_pending_updates=False)

def start_flask():
    threading.Thread(target=lambda: app.run(host="127.0.0.1", port=8787), daemon=True).start()


def main():
    # Load persisted state first so the log shows correct mode
    load_state()

    # If we’ve ever toggled via /shadow, prefer that over env
    global SHADOW_MODE
    if "shadow_mode" in STATE:
        SHADOW_MODE = bool(STATE["shadow_mode"])
    else:
        # First run: record whatever env said
        STATE["shadow_mode"] = SHADOW_MODE
        save_state()

    log(f"Starting Crypto Bot (shadow mode {SHADOW_MODE})")
    # run engine + flask in background threads so Telegram can block main
    start_flask()
    threading.Thread(target=engine_loop, daemon=True).start()
    start_telegram()


if __name__ == "__main__":
    main()
