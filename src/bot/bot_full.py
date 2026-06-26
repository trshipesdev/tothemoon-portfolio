#!/usr/bin/env python3
# crypto bot — shadow mode by default; see PENDING.md for live-trading TODOs

import os, io, json, time, random, asyncio, threading, traceback
from collections import deque
from datetime import datetime, timezone, timedelta
import requests
from typing import Dict, List, Optional, Any

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from flask import Flask, jsonify, request as flask_request, abort, send_from_directory
from functools import wraps
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SHADOW_MODE = os.getenv("SHADOW_MODE", "true").strip().lower() in ("1", "true", "on", "yes")


def _git_sha_short() -> str:
    try:
        import subprocess
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:
        return os.getenv("GIT_SHA", "local")


BUILD = {"sha": _git_sha_short()}


def set_shadow_mode(val: bool):
    global SHADOW_MODE
    SHADOW_MODE = bool(val)
    STATE["shadow_mode"] = SHADOW_MODE
    save_state()


CONFIG: Dict[str, Any] = {
    "tenant": {
        "name":        "default",
        "jitter_seed": 42,
        "allowlist":   [],
    },

    "rpc": {
        "sol":  [u for u in [os.getenv("SOL_RPC_URL"),  "https://api.mainnet-beta.solana.com"]       if u],
        "eth":  [u for u in [os.getenv("ETH_RPC_URL"),  "https://rpc.ankr.com/eth"]                  if u],
        "base": [u for u in [os.getenv("BASE_RPC_URL"), "https://mainnet.base.org"]                   if u],
        "bsc":  [u for u in [os.getenv("BSC_RPC_URL"),  "https://bsc-dataseed1.binance.org"]          if u],
        "poly": [u for u in [os.getenv("POLY_RPC_URL"), "https://polygon-rpc.com"]                    if u],
    },
    "keys": {
        "solana_secret_base58_env": "WALLET_PK_SOL",
        "evm_hex_key_env":          "WALLET_PK_EVM",
    },

    "chains": ["sol", "eth", "base", "bsc", "poly"],
    "scan": {
        "new_max_age_min":      120,  # pairs ≤ this age treated as NEW
        "hype_window_min":      240,
        "dexscreener_poll_sec":  20,  # how often to scan for NEW candidates
        "position_poll_sec":      3,  # how often to check OPEN positions (fast — catch quick pumps/dips)
    },
    "presale_min_score": 10,  # min presale_score to enter/suggest a new token (0=disabled, 10=social buzz only)

    # Degen-name hype nudge — provocative/vulgar token names reliably pull extra degen
    # volume in memecoin culture (e.g. PENISPUMP +534%). Small, tunable hype boost; the
    # safety gate (rugcheck) + momentum exits still apply since these are very volatile.
    "degen_terms": {
        "enabled": True,
        "bonus":   12,   # hype points added (pre-100 cap) when a name matches
        "terms":   ["cum", "sex", "penis", "dick", "cock", "tit", "boob", "milf",
                    "daddy", "thicc", "coom", "goon", "bussy", "rizz", "69", "420"],
    },

    "modes": {
        "safe":    {"tp": [0.15],      "sl": 0.07, "slip_bps": 60,  "liq_min": 50000, "max_age_min": 360, "max_entries_per_token_day": 2, "size_mult": 0.7},
        "default": {"tp": [0.28],      "sl": 0.12, "slip_bps": 100, "liq_min": 30000, "max_age_min": 180, "max_entries_per_token_day": 3, "size_mult": 1.0},
        "hype":    {"tp": [0.45, 0.9], "sl": 0.18, "slip_bps": 150, "liq_min": 20000, "max_age_min": 120, "max_entries_per_token_day": 4, "size_mult": 1.3},
        "degen":   {"tp": [0.80, 1.5], "sl": 0.28, "slip_bps": 220, "liq_min": 10000, "max_age_min": 120, "max_entries_per_token_day": 6, "size_mult": 1.6,
                    "no_pump": {"hurdle": 0.03, "min_sec": 240, "max_sec": 900},
                    # Scalp-ladder + moon-bag: sell [fraction] of the original position at each
                    # [gain]. Fractions sum to 0.70 → the remaining 0.30 is the MOON BAG, which
                    # has no TP cap and rides a wide trailing stop to catch the rare +500% runners.
                    "tp_ladder": [[0.30, 0.20], [0.80, 0.25], [1.50, 0.25]],
                    "moonbag_trail_pct": 0.45},   # wide leash on the moon bag (vs 0.15 normal)
    },
    "mode": "default",

    "objective": {
        "kind":          "off",  # off | target
        "target_usd":    0,
        "horizon_weeks": 0,
        "r_bounds": {
            "size_mult_max":  1.35,
            "extra_open_max": 2,
            "hype_shift_pp":  15,
            "degen_min_sec":  180,
        },
    },

    # Tuned from live data: the bot was finding 100+ good tokens/day but the $148
    # daily budget (20%) ran out after ~9 trades, forcing the rest to "suggested"
    # (found-but-not-bought). With a positive edge (75% win, wins > losses), smaller
    # tickets + a bigger daily budget capture more of that edge. Drawdown brake still
    # protects the downside. Tunable — re-evaluate each analysis.
    "base_size_usd":        30.0,   # was 50 — smaller bets spread across more tokens
    "reserve_pct":          0.25,   # keep 25% of vault untouched
    "per_token_cap_pct":    0.12,   # max 12% of deployable per token  ← real risk cap
    # Stop churning the same hyper-volatile token: cap fresh entries per symbol per day.
    # Raised 2→4: a cap of 2 strangled the bot once it cycled the few liquid tokens
    # (everything else is below the liq floor), leaving nothing to trade. 4 keeps
    # anti-churn protection while letting it stay active. Per-mode override supported.
    "max_entries_per_token_day": 4,
    "per_chain_cap_pct":    {"sol": 0.40, "eth": 0.35, "base": 0.25, "bsc": 0.30, "poly": 0.25},  # ← real risk cap
    # Far-off sanity backstop only (5x deployable/day). The REAL, robust risk controls
    # are the reserve + per-chain + per-token caps + drawdown brake, which compute from
    # LIVE open positions and so can't drift or self-lock. The old tight daily counter
    # could climb to the cap and then freeze (nothing open to sell → can't unwind it →
    # locked out until midnight), starving the bot. Keep this high so it rarely binds.
    "daily_deploy_cap_pct": 5.0,
    "drawdown_brake":       {"lookback": 30, "dd": 0.25, "size_mult": 0.60, "reserve_pct": 0.40},
    # Risk guardrails
    "max_open_positions":   12,     # cap concurrent positions (memecoins dump together)
    "blacklist":            [],     # symbols/addresses to never buy (manual, dashboard-editable)

    "vaults": {"hot_native_pct": 0.75, "hot_usdc_pct": 0.25},

    "moonshot": {
        "mode":             "suggest",  # enter | suggest
        "size_mult":        1.5,
        # Lowered 25k→15k based on rejection analysis: both moonshots we missed
        # (+106%, +274%) sat at $17–22k liq, just under the old floor. Pools below
        # ~$10k almost all dumped AND are near-impossible to exit, so 15k is the
        # data-backed sweet spot. Momentum exits cap the downside on the misses.
        "liq_min":          15000,
        "liq_max":          250000,
        "hype_min":         80,         # 0–100
        "buy_ratio_min":    0.45,       # reject if <45% of recent (h1) trades are buys (being dumped)
        "price_impact_max": 0.02,
        "min_ticket_usd":   15.0,
        "adaptive_timer":   {"low_liq_sec": 300, "high_liq_sec": 1200},
        "tp":               [0.35, 0.60, 1.20],
        "sl":               0.22,
        "retries":          1,
        "rug_liq_drop":     0.40,   # instant exit if liq drops > 40% in one tick
        # Momentum exit params
        "trailing_stop_pct": 0.15,  # exit if price falls >15% from peak
        "velocity_exit_pct": 0.08,  # exit if m5 price change < -8% (single tick crash)
        "vol_dry_pct":       0.20,  # alert if h1 vol < 20% of entry vol
        "liq_drain_ticks":   3,     # exit if liq declining for 3 consecutive ticks
        "liq_drain_pct":     0.05,  # each tick must drop >5% to count as drain
        # Re-entry after exit (tightened: don't chase tokens that hard-dumped)
        "reentry": {
            "enabled":        True,
            "cooldown_min":   15,   # wait at least 15 min after exit (was 5 — stop fast re-buys)
            "stable_ticks":   4,    # price must hold tight for 4 consecutive ticks (~80s)
            "stable_range_pct": 0.03,  # within 3% high-low over those ticks (was 4%)
            "liq_floor_pct":  0.70, # liq must be >= 70% of entry liq (was 60%)
            "vol_min_h1":     5000.0,  # h1 volume must still be alive (was 3000)
            # Only re-enter tokens that COOLED OFF (trailing stop), never ones that
            # hard-dumped (rug / velocity crash / liq drain / stop-loss). This is the
            # fix for the "re-bought a loser 24s later" behavior.
            "skip_hard_exits": True,
            "max_per_day":    1,    # at most 1 re-entry attempt per token per day
        },
    },

    # Stall exit — bank a position that ran up then stopped climbing, instead of
    # letting it round-trip to breakeven (the #1 reason the account gave back gains).
    # Skips the moon bag, which is meant to ride. Tunable from the dashboard.
    "stall_exit": {
        "enabled":   True,
        "min_gain":  0.20,   # only kicks in once up ≥ 20%
        "stall_sec": 600,    # no new high for 10 min
        "give_back": 0.06,   # and price has slipped ≥ 6% off the peak
    },

    "oldcoin": {
        "auto_join":      False,
        "tiny_entry_usd": 20.0,
        "volume_x":       10.0,  # fires if h1 ≥ 10× the 24h hourly average
        "mentions_x":     2.0,   # reserved for social feed
        "watchlist": {
            # symbol -> DexScreener token address
            # "DOGE": "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
            # "SHIB": "0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE",
        },
    },

    "autoscale": {
        "enabled":      True,
        "grace_sec":    90,
        "add_frac":     0.25,   # add 25% of current position
        "pi_max":       0.02,
        "cooldown_min": 30,
        "max_adds":     2,
        "add_sl":       0.10,
        "fast_drop":    {"m3": 0.08, "m10": 0.15, "tighten_to": 0.06},
    },

    "telegram": {
        "token_env":        "TELEGRAM_TOKEN",
        "allowlist_env":    "TELEGRAM_ALLOWLIST",
        "quiet_hours_utc":  [3, 7],
        "daily_digest_utc": "14:00",
    },

    "stealth":  {"split_parts": [2, 4], "slip_bps_jitter": 30, "candidate_shuffle": True, "burst_per_30s": 4},
    "presale":  {"min_score": 70},  # legacy — use presale_min_score at top level
    "skim_pct": 0.10,  # fraction of realized profit moved to income_usd when skim is ON

    # Network gas/transaction fees, applied to BOTH legs of every paper trade so
    # shadow PnL reflects real on-chain costs. Approx USD per swap, per chain.
    # Solana is ~free; Ethereum mainnet is brutal on small trades.
    "gas_sim":     True,
    "gas_dynamic": True,   # fetch live ETH gas (eth_gasPrice × ETH price) every ~5 min
    # Per-swap USD. Solana base fee is ~$0.001, but landing memecoin snipes needs
    # priority fees (+ occasional token-account creation), so a realistic all-in cost
    # is ~$0.03/swap. Base/Polygon are cheap; BSC mid; ETH fetched live.
    "gas_usd":     {"sol": 0.03, "base": 0.03, "poly": 0.02, "bsc": 0.15, "eth": 6.0},

    # Scam / rug safety gate — checked on tokens the bot is about to buy.
    # Reddit (free) + X/Twitter (needs X_BEARER_TOKEN) scan for scam chatter;
    # on-chain checks holder concentration (Solana) and honeypots (EVM).
    "safety": {
        "reddit_enabled":     True,
        "x_enabled":          True,    # only active if X_BEARER_TOKEN is set
        "onchain_enabled":    True,
        "scam_chatter_max":   2,       # reject if total scam-warning mentions >= this
        "sol_holder_max_pct": 0.25,    # reject if a non-LP wallet holds > this share of supply
        "evm_sell_tax_max":   20.0,    # reject if honeypot.is reports sell tax above this %
        "rugcheck_score_max": 70,      # reject if rugcheck.xyz normalized risk score >= this (Solana)
    },

    # AI auto-pilot — Haiku reads market health + recent performance and recommends
    # (or auto-applies) a risk mode. Needs ANTHROPIC_API_KEY. Capital caps still
    # bound absolute trade size, so the AI only steers the risk PROFILE, never sizing.
    "ai": {
        "enabled":        False,   # master switch (also requires ANTHROPIC_API_KEY)
        "auto_apply":     False,   # True = AI switches modes itself; False = advisory only
        "model":          "claude-haiku-4-5",
        "interval_min":   12,      # how often to consult the AI (was 30 — too slow for
                                   # fast memecoin regimes; also event-triggered on drawdown)
        "min_confidence": 0.6,     # only auto-apply at/above this confidence
    },
}

random.seed(CONFIG["tenant"]["jitter_seed"])

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
STATE: Dict[str, Any] = {
    "ts":               None,
    "vault_usd":        1000.0,
    "vault_start":      1000.0,
    "deployable_usd":   750.0,
    "income_usd":       0.0,
    "positions":        {},
    "pnl_hist":         [],
    "open_today_usd":   0.0,
    "last_daily_reset": None,
    "signals":          {"btc_d": None, "heat": None},
    "rpc":              {"health": {}},
    "liq_prev":         {},
    "trade_log":        [],
    "telegram":         {"owner_chat_id": None},
    "boost":            {"mult": 1.0, "expires": None},
    "spray_until":      None,
    "skim":             {"enabled": False},
    "brake_alerted":    False,
    "reentry_watch":    {},  # sym → {exit_ts, entry_liq, entry_vol_h1, address, chain, price_samples}
    "open_burst":       [],  # timestamps of recent position opens for burst rate-limiting
    "gas_paid_usd":     0.0,  # cumulative simulated gas/fees paid across all paper trades
    "scout_log":        [],  # recent candidate evaluations: why it entered/suggested/rejected
    "gas_live":         {},  # live per-chain gas estimates: {chain: usd, "ts": epoch}
    "ai":               {"last_run": None, "last": None, "history": []},  # AI advisor state
    "entries_today":    {},  # symbol → count of fresh entries today (anti-churn), reset daily
}

_state_path_env = os.getenv("STATE_PATH", "")
SAVEFILE = _state_path_env if _state_path_env else f"state_{CONFIG['tenant']['name']}.json"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ── Forward recorder ───────────────────────────────────────────────────────────
# Append a compact, timestamped log of what the bot SAW (entered candidates) and the
# per-tick price/liquidity of positions it HELD, so the backtester can later replay
# the REAL entry moments and REAL liquidity path (rug/liq-drain modelling). Best-effort
# and throttled; never allowed to break the engine. Toggle with RECORDER env (default on).
RECORDER = {
    "enabled":   os.getenv("RECORDER", "true").lower() != "false",
    "dir":       os.path.join(os.path.dirname(SAVEFILE) or ".", "recordings"),
    "tick_sec":  30,    # min seconds between recorded ticks per position
    "keep_days": 14,    # prune recordings older than this
}
_REC_TS: Dict[str, float] = {}   # symbol → last recorded tick time (module-level, not in STATE)
_REC_PRUNED_DAY = {"d": ""}


def _record(ev: Dict[str, Any]) -> None:
    """Append one event to today's recording file. Best-effort; swallows all errors."""
    if not RECORDER["enabled"]:
        return
    try:
        os.makedirs(RECORDER["dir"], exist_ok=True)
        day = now_utc().date().isoformat()
        if _REC_PRUNED_DAY["d"] != day:        # prune once per day
            _REC_PRUNED_DAY["d"] = day
            _prune_recordings()
        ev.setdefault("ts", now_utc().timestamp())
        with open(os.path.join(RECORDER["dir"], f"{day}.jsonl"), "a") as f:
            f.write(json.dumps(ev, separators=(",", ":")) + "\n")
    except Exception:
        pass


def _prune_recordings() -> None:
    import glob
    try:
        cutoff = now_utc().timestamp() - RECORDER["keep_days"] * 86400
        for p in glob.glob(os.path.join(RECORDER["dir"], "*.jsonl")):
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
    except Exception:
        pass


def _record_tick(symbol: str, p: Dict[str, Any], px: Dict[str, float]) -> None:
    """Record a position's price/liq/vol, throttled to RECORDER['tick_sec']."""
    if not RECORDER["enabled"]:
        return
    last = _REC_TS.get(symbol, 0.0)
    if time.time() - last < RECORDER["tick_sec"]:
        return
    _REC_TS[symbol] = time.time()
    _record({"ev": "tick", "symbol": symbol, "chain": p.get("chain", "sol"),
             "address": p.get("address", ""), "price": px.get("price", 0.0),
             "liq": px.get("liq", 0.0), "vol_h1": px.get("vol_h1", 0.0)})


# DexScreener chain slugs for building clickable token links
DEX_CHAIN_SLUG = {"sol": "solana", "eth": "ethereum", "base": "base", "bsc": "bsc", "poly": "polygon"}


def _dex_link(chain: str, address: str) -> str:
    """Build a DexScreener chart URL for a token (empty string if no address)."""
    if not address:
        return ""
    return f"https://dexscreener.com/{DEX_CHAIN_SLUG.get(chain, chain)}/{address}"


def _exit_plain(reason: str) -> str:
    """Translate a technical exit reason into plain English for alerts."""
    if reason.startswith("RUG"):
        return "liquidity suddenly crashed (looks like a rug pull) — the bot bailed instantly"
    if reason.startswith("VELOCITY"):
        return "the price dropped sharply in minutes — the bot cut it fast to dodge a crash"
    if reason.startswith("TRAIL STOP"):
        return "it fell too far from its peak — the bot locked in the gains it had"
    if reason.startswith("LIQ DRAIN"):
        return "liquidity kept draining (people pulling their money out) — the bot exited before it got worse"
    if reason.startswith("fixed_sl"):
        return "it hit your max-loss line — the bot sold to cap the damage"
    return reason


def save_state():
    try:
        with open(SAVEFILE, "w") as f:
            json.dump(STATE, f, indent=2, default=str)
    except Exception as e:
        log(f"WARN save_state failed: {e}")


def load_state():
    try:
        with open(SAVEFILE, "r") as f:
            loaded = json.load(f)
        STATE.update(loaded)
        # Ensure keys added after the state file was created are initialized
        STATE.setdefault("reentry_watch", {})
        STATE.setdefault("open_burst",    [])
        STATE.setdefault("brake_alerted", False)
        STATE.setdefault("gas_paid_usd",  0.0)
        STATE.setdefault("scout_log",     [])
        STATE.setdefault("entries_today", {})
        # Set vault_start once from the loaded vault if it was never recorded
        STATE.setdefault("vault_start", STATE.get("vault_usd", 1000.0))
        # Drop any fully-closed position shells (units<=0) left from before the purge
        # fix — otherwise they keep counting against the max-open-positions cap.
        STATE["positions"] = {s: p for s, p in STATE.get("positions", {}).items()
                              if p.get("units", 0) > 0}
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"WARN load_state failed: {e}")

# ---------------------------------------------------------------------------
# Data feeds
# ---------------------------------------------------------------------------
DEXSCREENER_BASE     = "https://api.dexscreener.com"
DEXSCREENER_TOKEN    = DEXSCREENER_BASE + "/latest/dex/tokens/"
DEXSCREENER_BOOSTS   = DEXSCREENER_BASE + "/token-boosts/latest/v1"
DEXSCREENER_PROFILES = DEXSCREENER_BASE + "/token-profiles/latest/v1"
COINGECKO_BASE       = "https://api.coingecko.com/api/v3"
COINGECKO_GLOBAL     = COINGECKO_BASE + "/global"
BIRDEYE_BASE         = "https://public-api.birdeye.so"
BIRDEYE_KEY_ENV      = "BIRDEYE_API_KEY"
LUNARCRUSH_BASE      = "https://lunarcrush.com/api4/public"
LUNARCRUSH_KEY_ENV   = "LUNARCRUSH_API_KEY"

CHAIN_IDS: Dict[str, str] = {
    "sol":  "solana",
    "eth":  "ethereum",
    "base": "base",
    "bsc":  "bsc",
    "poly": "polygon",
}


def _get(url: str, timeout: int = 8, retries: int = 2) -> Optional[Any]:
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 503) and attempt < retries:
                time.sleep(1.5 ** attempt)
                continue
            return None
        except requests.Timeout:
            if attempt < retries:
                time.sleep(1.0)
                continue
            log(f"WARN _get timeout: {url[:80]}")
        except Exception as e:
            log(f"WARN _get error ({url[:80]}): {e}")
            break
    return None


def fetch_dexscreener_token(addr: str) -> Optional[Dict[str, Any]]:
    return _get(DEXSCREENER_TOKEN + addr)


def fetch_btc_dominance() -> Optional[float]:
    data = _get(COINGECKO_GLOBAL)
    if data:
        return data.get("data", {}).get("market_cap_percentage", {}).get("btc")
    return None


def fetch_coingecko_trending() -> List[Dict[str, Any]]:
    """Trending coins from CoinGecko as an additional scanner source."""
    data = _get(COINGECKO_BASE + "/search/trending")
    if not data:
        return []
    out: List[Dict[str, Any]] = []
    for item in (data.get("coins") or []):
        coin = item.get("item", {})
        sym  = coin.get("symbol", "").upper()
        if not sym:
            continue
        # Resolve chain from platforms dict if available
        platforms = coin.get("platforms") or {}
        chain     = "eth"  # default
        address   = ""
        for k, addr in platforms.items():
            if addr:
                chain_map = {"ethereum": "eth", "binance-smart-chain": "bsc",
                             "base": "base", "polygon-pos": "poly", "solana": "sol"}
                chain   = chain_map.get(k, "eth")
                address = addr
                break
        out.append({
            "symbol": sym, "chain": chain, "address": address,
            "market_cap_rank": coin.get("market_cap_rank"),
            "source": "coingecko_trending",
        })
    return out


def fetch_birdeye_price(token_addr: str) -> Optional[Dict[str, Any]]:
    """Fetch Solana token price from Birdeye. Returns {price, liquidity, volume24h} or None."""
    key = os.getenv(BIRDEYE_KEY_ENV, "")
    if not key:
        return None
    try:
        resp = requests.get(
            f"{BIRDEYE_BASE}/defi/price",
            params={"address": token_addr},
            headers={"X-API-KEY": key, "x-chain": "solana"},
            timeout=8,
        )
        if resp.status_code == 200:
            return resp.json().get("data")
    except Exception:
        pass
    return None


def fetch_birdeye_sol_candidates() -> List[Dict[str, Any]]:
    """Fetch trending Solana tokens from Birdeye token list."""
    key = os.getenv(BIRDEYE_KEY_ENV, "")
    if not key:
        return []
    try:
        resp = requests.get(
            f"{BIRDEYE_BASE}/defi/tokenlist",
            params={"sort_by": "v24hChangePercent", "sort_type": "desc",
                    "offset": 0, "limit": 20, "min_liquidity": 10000},
            headers={"X-API-KEY": key, "x-chain": "solana"},
            timeout=8,
        )
        if resp.status_code != 200:
            return []
        out: List[Dict[str, Any]] = []
        for t in (resp.json().get("data", {}).get("tokens") or []):
            sym   = (t.get("symbol") or "").upper()
            price = float(t.get("price") or 0)
            liq   = float(t.get("liquidity") or 0)
            vol24 = float(t.get("v24h") or 0)
            addr  = t.get("address", "")
            age   = float(t.get("lastTradeUnixTime") or 0)
            age_min = (time.time() - age) / 60 if age else 9999
            if sym and price > 0 and liq > 0:
                hype = min(100, int(vol24 / max(liq, 1) * 20) + _degen_hype_bonus(sym))
                out.append({
                    "symbol": sym, "chain": "sol", "price": price,
                    "liq": liq, "age_min": age_min, "hype": hype,
                    "positive": float(t.get("v24hChangePercent") or 0) > 0,
                    "address": addr, "source": "birdeye",
                })
        return out
    except Exception:
        return []


_cg_trending_cache: Dict[str, Any] = {"ts": 0.0, "symbols": {}}  # sym → rank (1=hottest)


def _coingecko_trending_rank(symbol: str) -> Optional[int]:
    """Return CoinGecko trending rank (1–10) for symbol, or None if not trending. Cached 10 min."""
    cache = _cg_trending_cache
    if time.time() - cache["ts"] > 600:
        data = _get(COINGECKO_BASE + "/search/trending")
        if data:
            cache["symbols"] = {
                item.get("item", {}).get("symbol", "").upper(): idx + 1
                for idx, item in enumerate(data.get("coins") or [])
            }
            cache["ts"] = time.time()
    return cache["symbols"].get(symbol.upper())


def fetch_social_volume(symbol: str) -> float:
    """Return social volume ratio (≥1 normal, >2 spike).
    Uses LunarCrush if key is set, otherwise falls back to CoinGecko trending rank."""
    key = os.getenv(LUNARCRUSH_KEY_ENV, "")
    if key:
        try:
            resp = requests.get(
                f"{LUNARCRUSH_BASE}/coins/{symbol.lower()}/v1",
                headers={"Authorization": f"Bearer {key}"},
                timeout=8,
            )
            if resp.status_code == 200:
                d   = resp.json().get("data", {})
                now = float(d.get("social_volume_24h") or 0)
                avg = float(d.get("social_volume_7d_average") or 0)
                if avg > 0:
                    return now / avg
        except Exception:
            pass
        return 1.0
    # No LunarCrush key — use CoinGecko trending rank as proxy
    # Rank 1 = hottest (#1 trending) → return 3.0; rank 10 → 1.5; not trending → 1.0
    rank = _coingecko_trending_rank(symbol)
    if rank is None:
        return 1.0
    return max(1.5, 3.0 - (rank - 1) * 0.15)


def compute_heat(btc_d: Optional[float]) -> Optional[str]:
    if btc_d is None:  return None
    if btc_d < 40:     return "alt_season"
    if btc_d < 50:     return "neutral"
    if btc_d < 60:     return "btc_season"
    return "btc_max"


def _degen_hype_bonus(symbol: str) -> int:
    """Small hype boost for provocative/vulgar names that reliably pull degen volume."""
    dt = CONFIG.get("degen_terms", {})
    if not dt.get("enabled"):
        return 0
    low = (symbol or "").lower()
    return int(dt.get("bonus", 12)) if any(t in low for t in dt.get("terms", [])) else 0


def _pair_to_candidate(pair: Dict[str, Any], our_chain: str) -> Optional[Dict[str, Any]]:
    try:
        symbol = (pair.get("baseToken") or {}).get("symbol", "").upper()
        if not symbol:
            return None
        created_at = pair.get("pairCreatedAt")
        age_min    = ((time.time() * 1000 - created_at) / 60000) if created_at else 9999
        price_usd  = float(pair.get("priceUsd") or 0)
        liq        = float((pair.get("liquidity") or {}).get("usd") or 0)
        vol        = pair.get("volume") or {}
        vol_h24    = float(vol.get("h24") or 0)
        vol_h1     = float(vol.get("h1") or 0)
        if price_usd <= 0 or liq <= 0:
            return None
        # Hype from RECENT momentum (h1 volume velocity) rather than a 24h average —
        # a token exploding right now should outscore one with steady all-day volume.
        vol_signal = vol_h1 if vol_h1 > 0 else (vol_h24 / 24.0)
        hype       = min(100, int(vol_signal / max(liq, 1) * 40) + _degen_hype_bonus(symbol))
        # Buyer/seller pressure (h1) — a token that's mostly sells is being dumped.
        tx_h1      = (pair.get("txns") or {}).get("h1") or {}
        buys       = float(tx_h1.get("buys") or 0)
        sells      = float(tx_h1.get("sells") or 0)
        buy_ratio  = (buys / (buys + sells)) if (buys + sells) > 0 else None
        price_chg  = float((pair.get("priceChange") or {}).get("h24") or 0)
        return {"symbol": symbol, "chain": our_chain, "price": price_usd,
                "liq": liq, "age_min": age_min, "hype": hype, "positive": price_chg > 0,
                "vol_h1": vol_h1, "buy_ratio": buy_ratio,
                "address": (pair.get("baseToken") or {}).get("address", "")}
    except Exception:
        return None


def fetch_new_candidates() -> List[Dict[str, Any]]:
    active_ids = {CHAIN_IDS[c] for c in CONFIG["chains"] if c in CHAIN_IDS}
    seen: set = set()
    out: List[Dict[str, Any]] = []

    def _ingest(token_address: str, dex_chain_id: str):
        our_chain = next((k for k, v in CHAIN_IDS.items() if v == dex_chain_id), None)
        if not our_chain:
            return
        data = fetch_dexscreener_token(token_address)
        if not data:
            return
        for pair in (data.get("pairs") or []):
            c = _pair_to_candidate(pair, our_chain)
            if c:
                key = (c["symbol"], c["chain"])
                if key not in seen:
                    seen.add(key)
                    out.append(c)

    for item in (_get(DEXSCREENER_BOOSTS) or []):
        cid = item.get("chainId", "")
        if cid in active_ids:
            _ingest(item.get("tokenAddress", ""), cid)

    for item in (_get(DEXSCREENER_PROFILES) or []):
        cid = item.get("chainId", "")
        if cid in active_ids:
            _ingest(item.get("tokenAddress", ""), cid)

    # Birdeye — trending Solana tokens (only if API key set)
    for c in fetch_birdeye_sol_candidates():
        key = (c["symbol"], c["chain"])
        if key not in seen:
            seen.add(key)
            out.append(c)

    # CoinGecko — trending tokens across chains
    for c in fetch_coingecko_trending():
        if c.get("address"):
            our_chain = c["chain"]
            if our_chain in CONFIG["chains"]:
                _ingest(c["address"], CHAIN_IDS.get(our_chain, our_chain))

    return out

def _px_dict(price: float, liq: float = 100000.0, vol_h1: float = 0.0, change_m5: float = 0.0) -> Dict:
    return {"price": price, "liq": liq, "vol_h1": vol_h1, "change_m5": change_m5}


def _parse_dex_pair(pair: Dict) -> Optional[Dict]:
    price     = float(pair.get("priceUsd") or 0)
    liq       = float((pair.get("liquidity") or {}).get("usd") or 100000.0)
    vol_h1    = float((pair.get("volume")      or {}).get("h1") or 0)
    change_m5 = float((pair.get("priceChange") or {}).get("m5") or 0)
    return _px_dict(price, liq, vol_h1, change_m5) if price > 0 else None


def fetch_positions_prices() -> Dict[str, Dict]:
    """Batch-fetch price, liq, vol_h1, change_m5 for all open positions."""
    open_pos = {s: p for s, p in STATE["positions"].items() if p.get("units", 0) > 0}
    if not open_pos:
        return {}
    result:   Dict[str, Dict] = {}
    addr_map: Dict[str, str]  = {p["address"].lower(): s
                                  for s, p in open_pos.items() if p.get("address")}
    if addr_map:
        data    = _get(DEXSCREENER_TOKEN + ",".join(addr_map))
        pairs   = (data or {}).get("pairs") or []
        by_addr: Dict[str, Dict] = {}
        for pair in pairs:
            addr = ((pair.get("baseToken") or {}).get("address") or "").lower()
            if addr and addr not in by_addr:
                by_addr[addr] = pair
        for addr, sym in addr_map.items():
            px = _parse_dex_pair(by_addr[addr]) if addr in by_addr else None
            if px:
                result[sym] = px
    for sym, p in open_pos.items():
        if sym not in result:
            addr = p.get("address", "")
            if p.get("chain") == "sol" and addr:
                bd = fetch_birdeye_price(addr)
                if bd and float(bd.get("value") or 0) > 0:
                    result[sym] = _px_dict(float(bd["value"]), float(bd.get("liquidity") or 100000.0))
                    continue
            result[sym] = _px_dict(p.get("avg", 1.0) * 1.02)
    return result

# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
class Score:
    def __init__(self, hype: int, liq: float, age_min: float, positive: bool,
                 buy_ratio: Optional[float] = None):
        self.hype      = hype
        self.liq       = liq
        self.age_min   = age_min
        self.positive  = positive
        self.buy_ratio = buy_ratio   # h1 buys/(buys+sells); None if unknown


def moonshot_reject_reason(sc: Score) -> Optional[str]:
    """Return a human-readable reason the candidate fails the filters, or None if it passes."""
    ms = CONFIG["moonshot"]
    spray = STATE.get("spray_until")
    spraying = spray and now_utc().date().isoformat() <= spray
    # Use the ACTIVE MODE's liquidity floor so modes actually change aggressiveness
    # (degen $10k → more candidates, safe $50k → fewer/safer). Falls back to the
    # moonshot floor if a mode doesn't set one.
    mode_liq = CONFIG["modes"].get(CONFIG["mode"], {}).get("liq_min", ms["liq_min"])
    liq_min  = mode_liq * (0.7 if spraying else 1.0)
    hype_min = max(50, ms["hype_min"] - (20 if spraying else 0))
    if sc.liq < liq_min:
        return f"liquidity ${sc.liq:,.0f} below min ${liq_min:,.0f}"
    if sc.liq > ms["liq_max"]:
        return f"liquidity ${sc.liq:,.0f} above max ${ms['liq_max']:,.0f}"
    max_age = CONFIG["modes"].get(CONFIG["mode"], {}).get(
        "max_age_min", CONFIG["scan"]["new_max_age_min"])
    if sc.age_min > max_age:
        return f"age {sc.age_min:.0f}m over {max_age:.0f}m limit"
    if not sc.positive:
        return "price trend is negative (24h)"
    if sc.hype < hype_min:
        return f"hype {sc.hype} below min {hype_min}"
    # Buyer/seller pressure — skip tokens being actively dumped (more sells than buys).
    br_min = ms.get("buy_ratio_min", 0.45)
    if sc.buy_ratio is not None and sc.buy_ratio < br_min:
        return f"selling pressure — only {sc.buy_ratio*100:.0f}% of trades are buys"
    return None


def passes_moonshot_filters(sc: Score) -> bool:
    return moonshot_reject_reason(sc) is None


def _scout(symbol: str, chain: str, decision: str, reason: str,
           sc: Optional[Score] = None, address: str = ""):
    """Record why the scanner did (or didn't) act on a candidate, for the dashboard Scout log."""
    log_list: List[Dict[str, Any]] = STATE.setdefault("scout_log", [])
    entry: Dict[str, Any] = {
        "ts": now_utc().isoformat(), "symbol": symbol, "chain": chain,
        "decision": decision, "reason": reason, "address": address,
    }
    if sc is not None:
        entry.update({"hype": sc.hype, "liq": round(sc.liq, 0), "age_min": round(sc.age_min, 1)})
    log_list.append(entry)
    if len(log_list) > 300:                # keep the most recent 300 evaluations
        del log_list[: len(log_list) - 300]

# ---------------------------------------------------------------------------
# Objectives × strategy
# ---------------------------------------------------------------------------
OBJ_STATE: Dict[str, Any] = {"started": None, "target_curve": []}


def start_objective(target_usd: float, weeks: int):
    if target_usd <= 0 or weeks <= 0:
        raise ValueError(f"target_usd and weeks must be positive (got {target_usd}, {weeks})")
    CONFIG["objective"].update({"kind": "target", "target_usd": float(target_usd), "horizon_weeks": int(weeks)})
    OBJ_STATE["started"]      = now_utc()
    OBJ_STATE["target_curve"] = [target_usd * (i + 1) / weeks for i in range(weeks)]


def objective_nudge() -> Dict[str, Any]:
    _zero = {"size_mult": 1.0, "extra_open": 0, "hype_pp": 0, "degen_min_sec": None}
    if CONFIG["objective"]["kind"] != "target":
        return _zero
    weeks = CONFIG["objective"]["horizon_weeks"]
    if weeks <= 0 or not OBJ_STATE["started"]:
        return _zero

    elapsed       = (now_utc() - OBJ_STATE["started"]).days / 7.0
    idx           = min(max(int(elapsed), 0), weeks - 1)
    target_so_far = OBJ_STATE["target_curve"][idx]
    actual        = sum(p.get("realized", 0.0) for p in STATE["positions"].values()) + STATE.get("income_usd", 0.0)
    delta         = actual - target_so_far

    if delta < -target_so_far / 8:
        r, extra_open, hype_pp = 1.2, 1, 8
        degen_min_sec = max(CONFIG["objective"]["r_bounds"]["degen_min_sec"], 180)
    elif delta > target_so_far / 8:
        r, extra_open, hype_pp, degen_min_sec = 0.9, 0, -6, None
    else:
        r, extra_open, hype_pp, degen_min_sec = 1.0, 0, 0, None

    rb = CONFIG["objective"]["r_bounds"]
    return {
        "size_mult":    min(r, rb["size_mult_max"]),
        "extra_open":   min(extra_open, rb["extra_open_max"]),
        "hype_pp":      max(-rb["hype_shift_pp"], min(hype_pp, rb["hype_shift_pp"])),
        "degen_min_sec": degen_min_sec,
    }

# ---------------------------------------------------------------------------
# Capital & reserves
# ---------------------------------------------------------------------------
def drawdown_brake_active() -> bool:
    hist = STATE["pnl_hist"][-CONFIG["drawdown_brake"]["lookback"]:]
    if not hist:
        return False
    loss = sum(x for x in hist if x < 0)
    gain = sum(x for x in hist if x > 0)
    net  = gain + loss
    return net < 0 and abs(net) >= CONFIG["drawdown_brake"]["dd"] * max(1.0, abs(gain))


def deployable_now() -> float:
    # Hold back MORE capital while the drawdown brake is on (bad streak) — protect the
    # bankroll when the edge has gone cold, lean in again when it recovers.
    reserve = CONFIG["reserve_pct"]
    if drawdown_brake_active():
        reserve = max(reserve, CONFIG.get("drawdown_brake", {}).get("reserve_pct", 0.40))
    return max(0.0, STATE["vault_usd"] * (1 - reserve))


def per_chain_room(chain: str) -> float:
    cap  = CONFIG["per_chain_cap_pct"].get(chain, 0.25) * deployable_now()
    used = sum(p["usd"] for p in STATE["positions"].values() if p["chain"] == chain)
    return max(0.0, cap - used)


def per_token_cap_room() -> float:
    return CONFIG["per_token_cap_pct"] * deployable_now()


def _conviction_mult(hype: Optional[int], buy_ratio: Optional[float]) -> float:
    """Scale ticket size by setup quality: stronger hype + buying pressure → bigger bet.
    Bounded [0.8, 1.5] so it nudges, never blows past the risk caps."""
    m = 1.0
    if hype is not None:
        m += max(0.0, (hype - 80) / 20.0) * 0.4   # hype 80→1.0, 100→1.4
    if buy_ratio is not None:
        if   buy_ratio >= 0.80: m += 0.15
        elif buy_ratio >= 0.65: m += 0.07
    return max(0.8, min(1.5, m))


def size_ticket_usd(chain: str, hype: Optional[int] = None,
                    buy_ratio: Optional[float] = None) -> float:
    base = CONFIG["base_size_usd"] * CONFIG["modes"][CONFIG["mode"]]["size_mult"]
    base *= objective_nudge()["size_mult"]
    base *= _conviction_mult(hype, buy_ratio)   # bet more on the strongest setups
    if drawdown_brake_active():
        base *= CONFIG["drawdown_brake"]["size_mult"]
    boost = STATE.get("boost", {})
    if boost.get("mult", 1.0) != 1.0:
        if boost.get("expires") and now_utc().isoformat() < boost["expires"]:
            base *= boost["mult"]
        else:
            STATE["boost"] = {"mult": 1.0, "expires": None}
    base    = min(base, per_chain_room(chain), per_token_cap_room())
    day_cap = CONFIG["daily_deploy_cap_pct"] * deployable_now()
    return max(0.0, min(base, day_cap - STATE["open_today_usd"]))

# ---------------------------------------------------------------------------
# Execution adapters
# ---------------------------------------------------------------------------
def est_price_impact(order_usd: float, liq_usd: float) -> float:
    if liq_usd <= 0:
        return 1.0
    return min(0.05, order_usd / (liq_usd * 10.0))


def _stealth_ok() -> bool:
    """Return False if we've opened too many positions in the last 30s (burst guard)."""
    limit = CONFIG["stealth"]["burst_per_30s"]
    cutoff = time.time() - 30
    burst: List[float] = STATE.setdefault("open_burst", [])
    burst[:] = [t for t in burst if t > cutoff]
    return len(burst) < limit


def _jitter_slip(base_slip: float) -> float:
    """Add small random bps jitter to slippage to avoid MEV pattern detection."""
    jitter_bps = CONFIG["stealth"]["slip_bps_jitter"]
    return base_slip + random.uniform(0, jitter_bps / 10000)


def _gas_usd(chain: str) -> float:
    """Estimated network gas/fee in USD for one swap on `chain` (0 if gas sim off).
    Prefers a live estimate (see refresh_gas_estimates) and falls back to static config."""
    if not CONFIG.get("gas_sim", True):
        return 0.0
    live = STATE.get("gas_live", {}).get(chain)
    if live is not None:
        return float(live)
    return float(CONFIG.get("gas_usd", {}).get(chain, 0.05))


def _eth_gas_usd_live() -> Optional[float]:
    """Live Ethereum swap cost in USD = gasPrice(wei) × ~150k gas × ETH price."""
    rpcs = CONFIG["rpc"].get("eth") or []
    if not rpcs:
        return None
    try:
        r = requests.post(rpcs[0], json={"jsonrpc": "2.0", "id": 1,
                                         "method": "eth_gasPrice", "params": []}, timeout=6)
        gas_price_wei = int(r.json()["result"], 16)
    except Exception:
        return None
    px = _get("https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd")
    eth_usd = float((px or {}).get("ethereum", {}).get("usd") or 0)
    if eth_usd <= 0:
        return None
    SWAP_GAS_UNITS = 150000
    return gas_price_wei * SWAP_GAS_UNITS / 1e18 * eth_usd


def _sol_gas_usd_live() -> Optional[float]:
    """Live Solana swap cost in USD = (base + priority fee × CU) × SOL price.
    Priority fees spike during launch congestion — exactly when the bot snipes a
    fresh token — so a flat estimate understates the cost of the trades you most want."""
    rpcs = CONFIG["rpc"].get("sol") or []
    if not rpcs:
        return None
    try:
        r = requests.post(rpcs[0], json={"jsonrpc": "2.0", "id": 1,
                                         "method": "getRecentPrioritizationFees", "params": [[]]}, timeout=6)
        fees = sorted(f.get("prioritizationFee", 0) for f in (r.json().get("result") or []))
        if not fees:
            return None
        median_microlamports_per_cu = fees[len(fees) // 2]
    except Exception:
        return None
    px = _get("https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd")
    sol_usd = float((px or {}).get("solana", {}).get("usd") or 0)
    if sol_usd <= 0:
        return None
    SWAP_CU, BASE_LAMPORTS = 200_000, 5_000
    priority_lamports = median_microlamports_per_cu * SWAP_CU / 1e6
    return (BASE_LAMPORTS + priority_lamports) / 1e9 * sol_usd


def refresh_gas_estimates():
    """Periodically refresh live gas (ETH + SOL — the ones with live feeds). Cached ~5 min."""
    if not (CONFIG.get("gas_sim", True) and CONFIG.get("gas_dynamic", True)):
        return
    cache = STATE.setdefault("gas_live", {})
    if time.time() - cache.get("ts", 0) < 300:
        return
    updated = False
    eth = _eth_gas_usd_live()
    if eth and eth > 0:
        cache["eth"] = round(eth, 4); updated = True
        log(f"Live ETH gas: ${eth:.2f}/swap")
    sol = _sol_gas_usd_live()
    if sol and sol > 0:
        cache["sol"] = round(sol, 4); updated = True
        log(f"Live SOL gas: ${sol:.3f}/swap")
    if updated:
        cache["ts"] = time.time()


def shadow_buy(symbol: str, chain: str, usd: float, price: float, liq_usd: float, address: str = "") -> Dict[str, Any]:
    if symbol not in STATE["positions"] and not _stealth_ok():
        log(f"BURST GUARD: skipping new open for {symbol} (too many opens in 30s)")
        send_alert(
            f"⚡ Skipped buying {symbol} — the bot opened several trades very fast and is pausing briefly "
            f"so it doesn't behave like an obvious bot. Nothing's wrong; no money moved.")
        return {"price": price, "units": 0.0}
    impact       = est_price_impact(usd, liq_usd)
    slip         = _jitter_slip(0.5 * impact)
    filled_price = price * (1 + slip)
    units        = usd / filled_price
    is_new_pos   = symbol not in STATE["positions"]
    pos          = STATE["positions"].setdefault(symbol, {
        "chain": chain, "units": 0.0, "usd": 0.0,
        "avg": 0.0, "realized": 0.0, "time": now_utc().isoformat(),
        "address": address, "add_count": 0, "last_add_ts": 0.0,
        "peak_price": filled_price, "entry_vol_h1": 0.0,
        "entry_liq": liq_usd, "liq_ticks": [], "fees_usd": 0.0, "deployed_usd": 0.0,
        # Lock the risk profile to the mode at ENTRY. Otherwise an AI mode-switch
        # mid-trade would retroactively change this position's stop-loss/TP.
        "entry_mode": CONFIG["mode"],
    })
    if address and not pos.get("address"):
        pos["address"] = address
    if filled_price > pos.get("peak_price", 0):
        pos["peak_price"] = filled_price
    if is_new_pos:
        STATE.setdefault("open_burst", []).append(time.time())
    gas = _gas_usd(chain)   # network fee to enter — a sunk cost on top of the buy
    new_total_units = pos["units"] + units
    pos["avg"]      = (pos["avg"] * pos["units"] + filled_price * units) / max(1e-9, new_total_units)
    pos["usd"]      += usd
    pos["units"]     = new_total_units
    pos["fees_usd"]  = pos.get("fees_usd", 0.0) + gas
    pos["deployed_usd"] = pos.get("deployed_usd", 0.0) + usd   # total ever put in (for TP-ladder fractions)
    STATE["vault_usd"]      -= (usd + gas)
    STATE["open_today_usd"] += usd
    STATE["gas_paid_usd"]    = STATE.get("gas_paid_usd", 0.0) + gas
    # Capital-at-risk tracking — the REAL money on the table, not future money.
    # peak_deployed = the most $ ever simultaneously in open positions (≈ the smallest
    # bankroll that could have run this). peak_open = most positions held at once.
    live = [q for q in STATE["positions"].values() if q.get("units", 0) > 0]
    cur_deployed = sum(q.get("usd", 0.0) for q in live)
    STATE["cur_deployed_usd"]  = cur_deployed
    STATE["peak_deployed_usd"] = max(STATE.get("peak_deployed_usd", 0.0), cur_deployed)
    STATE["peak_open_count"]   = max(STATE.get("peak_open_count", 0), len(live))
    if "first_bet_usd" not in STATE:
        STATE["first_bet_usd"] = usd   # the "starting roller" — size of the very first bet
    STATE.setdefault("trade_log", []).append({
        "ts": now_utc().isoformat(), "symbol": symbol, "chain": chain,
        "side": "buy", "usd": usd, "price": filled_price, "units": units, "gas": gas,
        "address": pos.get("address", ""), "pnl": None,
        "mode": pos.get("entry_mode", CONFIG.get("mode")),
    })
    return {"price": filled_price, "units": units}


def shadow_sell(symbol: str, usd: float, price: float, liq_usd: float) -> Dict[str, Any]:
    pos = STATE["positions"].get(symbol)
    if not pos or pos["usd"] <= 0:
        return {"sold": 0.0, "pnl": 0.0}
    portion  = min(1.0, usd / max(1e-9, pos["usd"]))
    units    = pos["units"] * portion
    gas      = _gas_usd(pos.get("chain", "sol"))           # exit network fee
    fee_share = pos.get("fees_usd", 0.0) * portion          # proportional entry gas
    # Exit price impact — SELLING crashes the fill in thin pools. Uncapped at the 5%
    # entry ceiling because dumping a big bag into a shallow pool craters far more
    # than a sizing nudge (a sell worth the whole pool ≈ −25%). This is what kept the
    # moon-bag math honest: you can't dump a +500% bag at the top in a $15k pool.
    order_val   = units * price
    exit_impact = min(0.30, order_val / max(liq_usd, 1.0) / 4.0)
    proceeds = units * price * (1 - 0.002) * (1 - exit_impact) - gas   # fee + impact out of proceeds
    cost     = units * pos["avg"]
    pnl      = proceeds - cost - fee_share                  # entry gas folded into PnL
    pos["units"]       -= units
    pos["usd"]         -= cost
    pos["fees_usd"]     = max(0.0, pos.get("fees_usd", 0.0) - fee_share)
    if pos["units"] <= 0:
        pos["avg"]      = 0.0
        pos["tp_index"] = 0   # reset for potential re-entry into same symbol
        pos["fees_usd"] = 0.0
        pos["deployed_usd"] = 0.0
    pos["realized"]    += pnl
    # Per-mode performance — attribute realized P&L to the mode the position was
    # ENTERED under, so you can see which mode actually makes money.
    mp  = STATE.setdefault("mode_perf", {})
    rec = mp.setdefault(pos.get("entry_mode", "?"), {"pnl": 0.0, "sells": 0, "wins": 0})
    rec["pnl"]  += pnl
    rec["sells"] += 1
    if pnl > 0:
        rec["wins"] += 1
    STATE["gas_paid_usd"] = STATE.get("gas_paid_usd", 0.0) + gas
    # Return the freed capital to today's deploy budget so the daily cap tracks NET
    # new exposure, not gross turnover — lets the bot cycle into the next moonshot
    # after closing a winner instead of being budget-locked for the day.
    STATE["open_today_usd"] = max(0.0, STATE.get("open_today_usd", 0.0) - cost)
    if STATE.get("skim", {}).get("enabled") and pnl > 0:
        skim = pnl * CONFIG.get("skim_pct", 0.10)
        STATE["income_usd"] = STATE.get("income_usd", 0.0) + skim
        proceeds -= skim
    STATE["vault_usd"] += proceeds
    STATE["pnl_hist"].append(pnl)
    STATE.setdefault("trade_log", []).append({
        "ts": now_utc().isoformat(), "symbol": symbol, "chain": pos.get("chain", "?"),
        "side": "sell", "usd": proceeds, "price": price, "units": units, "gas": gas,
        "address": pos.get("address", ""), "pnl": pnl,
        "mode": pos.get("entry_mode", CONFIG.get("mode")),
    })
    # Purge a fully-closed position so its empty shell doesn't linger in the
    # positions dict and count against the max-open-positions cap (which had jammed
    # the bot at 12 phantom "positions", blocking every new entry).
    if pos.get("units", 0) <= 0:
        STATE["positions"].pop(symbol, None)
    return {"sold": proceeds, "pnl": pnl}


# ---------------------------------------------------------------------------
# Live execution — Solana (Jupiter)
# ---------------------------------------------------------------------------
USDC_MINT_SOL  = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS  = 6
JUPITER_QUOTE  = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP   = "https://quote-api.jup.ag/v6/swap"


def _sol_keypair():
    try:
        import base58
        from solders.keypair import Keypair  # type: ignore
    except ImportError:
        raise RuntimeError("pip install solders base58")
    raw = os.getenv(CONFIG["keys"]["solana_secret_base58_env"], "")
    if not raw:
        raise ValueError("WALLET_PK_SOL not set")
    return Keypair.from_bytes(base58.b58decode(raw))


def _sol_rpc() -> str:
    """Return first responsive Solana RPC URL from the configured list."""
    urls = CONFIG["rpc"]["sol"]
    for url in urls:
        try:
            r = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "getHealth"}, timeout=3)
            if r.status_code == 200:
                return url
        except Exception:
            continue
    return urls[0]  # best effort


def _evm_w3(chain: str):
    from web3 import Web3  # type: ignore
    urls = CONFIG["rpc"].get(chain, [])
    for url in urls:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 30}))
            if w3.is_connected():
                return w3
        except Exception:
            continue
    return Web3(Web3.HTTPProvider(urls[0], request_kwargs={"timeout": 30}))


def _sol_token_decimals(mint: str) -> int:
    """Query Solana RPC for SPL token mint decimals. Defaults to 9."""
    try:
        resp = requests.post(_sol_rpc(), json={
            "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
            "params": [mint, {"encoding": "jsonParsed"}],
        }, timeout=5)
        dec = (resp.json()
               .get("result", {}).get("value", {})
               .get("data", {}).get("parsed", {})
               .get("info", {}).get("decimals", 9))
        return int(dec)
    except Exception:
        return 9


def _jupiter_get_quote(input_mint: str, output_mint: str, amount_units: int, slip_bps: int) -> Optional[Dict]:
    resp = requests.get(JUPITER_QUOTE, params={
        "inputMint": input_mint, "outputMint": output_mint,
        "amount": amount_units, "slippageBps": slip_bps,
    }, timeout=12)
    return resp.json() if resp.status_code == 200 else None


def _jupiter_get_swap_tx(quote: Dict, user_pubkey: str) -> Optional[str]:
    resp = requests.post(JUPITER_SWAP, json={
        "quoteResponse": quote, "userPublicKey": user_pubkey,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": "auto",
    }, timeout=15)
    return resp.json().get("swapTransaction") if resp.status_code == 200 else None


def _sol_submit_tx(tx_b64: str, kp) -> str:
    import base64
    from solders.transaction import VersionedTransaction  # type: ignore
    raw     = base64.b64decode(tx_b64)
    tx      = VersionedTransaction.from_bytes(raw)
    signed  = VersionedTransaction(tx.message, [kp])
    encoded = base64.b64encode(bytes(signed)).decode()
    rpc     = _sol_rpc()
    resp    = requests.post(rpc, json={
        "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
        "params": [encoded, {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}],
    }, timeout=30)
    result = resp.json()
    if "error" in result:
        raise RuntimeError(result["error"])
    return result["result"]


def live_buy_sol(usd: float, price: float, token_mint: str) -> Dict[str, Any]:
    try:
        kp         = _sol_keypair()
        slip_bps   = CONFIG["modes"][CONFIG["mode"]]["slip_bps"]
        usdc_units = int(usd * 10 ** USDC_DECIMALS)
        quote      = _jupiter_get_quote(USDC_MINT_SOL, token_mint, usdc_units, slip_bps)
        if not quote or not quote.get("outAmount"):
            return {"error": "JUPITER_NO_ROUTE"}
        tx_b64     = _jupiter_get_swap_tx(quote, str(kp.pubkey()))
        if not tx_b64:
            return {"error": "JUPITER_SWAP_FAILED"}
        sig        = _sol_submit_tx(tx_b64, kp)
        out_units  = int(quote["outAmount"])
        tok_dec    = _sol_token_decimals(token_mint)
        out_amount = out_units / 10 ** tok_dec
        filled_px  = usd / out_amount if out_amount else price
        return {"price": filled_px, "units": out_amount, "sig": sig}
    except Exception as e:
        return {"error": str(e)}


def live_sell_sol(usd: float, price: float, token_mint: str, pos_units: float, token_decimals: int = 9) -> Dict[str, Any]:
    try:
        kp         = _sol_keypair()
        slip_bps   = CONFIG["modes"][CONFIG["mode"]]["slip_bps"]
        sell_units = int(pos_units * (usd / (pos_units * price)) * 10 ** token_decimals)
        quote      = _jupiter_get_quote(token_mint, USDC_MINT_SOL, sell_units, slip_bps)
        if not quote or not quote.get("outAmount"):
            return {"error": "JUPITER_NO_ROUTE"}
        tx_b64     = _jupiter_get_swap_tx(quote, str(kp.pubkey()))
        if not tx_b64:
            return {"error": "JUPITER_SWAP_FAILED"}
        sig        = _sol_submit_tx(tx_b64, kp)
        proceeds   = int(quote["outAmount"]) / 10 ** USDC_DECIMALS
        return {"sold": proceeds, "sig": sig}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Live execution — EVM (Uniswap v3 / PancakeSwap v3)
# ---------------------------------------------------------------------------
UNISWAP_V3_ROUTER: Dict[str, str] = {
    "eth":  "0xE592427A0AEce92De3Edee1F18E0157C05861564",
    "base": "0x2626664c2603336E57B271c5C0b26F421741e481",
    "poly": "0xE592427A0AEce92De3Edee1F18E0157C05861564",
    "bsc":  "0x1b81D678ffb9C0263b24A97847620C99d213eB14",  # PancakeSwap v3
}

USDC_ADDR: Dict[str, str] = {
    "eth":  "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    "base": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "poly": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
    "bsc":  "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
}

_SWAP_ROUTER_ABI = [{
    "inputs": [{"components": [
        {"name": "tokenIn",           "type": "address"},
        {"name": "tokenOut",          "type": "address"},
        {"name": "fee",               "type": "uint24"},
        {"name": "recipient",         "type": "address"},
        {"name": "deadline",          "type": "uint256"},
        {"name": "amountIn",          "type": "uint256"},
        {"name": "amountOutMinimum",  "type": "uint256"},
        {"name": "sqrtPriceLimitX96", "type": "uint160"},
    ], "name": "params", "type": "tuple"}],
    "name": "exactInputSingle",
    "outputs": [{"name": "amountOut", "type": "uint256"}],
    "stateMutability": "payable",
    "type": "function",
}]

_ERC20_ABI = [
    {"inputs": [{"name": "_owner", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "_spender", "type": "address"}, {"name": "_value", "type": "uint256"}],
     "name": "approve", "outputs": [{"name": "", "type": "bool"}],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "_owner", "type": "address"}, {"name": "_spender", "type": "address"}],
     "name": "allowance", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "decimals",
     "outputs": [{"name": "", "type": "uint8"}], "stateMutability": "view", "type": "function"},
]


def _evm_account(w3):
    from web3 import Web3  # type: ignore
    pk = os.getenv(CONFIG["keys"]["evm_hex_key_env"], "")
    if not pk:
        raise ValueError("WALLET_PK_EVM not set")
    return w3.eth.account.from_key(pk), pk


def _evm_ensure_approval(w3, usdc_contract, owner: str, spender: str, amount: int, pk: str):
    if usdc_contract.functions.allowance(owner, spender).call() >= amount:
        return
    nonce = w3.eth.get_transaction_count(owner)
    tx    = usdc_contract.functions.approve(spender, 2**256 - 1).build_transaction({
        "from": owner, "nonce": nonce, "gasPrice": w3.eth.gas_price, "gas": 80000,
    })
    signed = w3.eth.account.sign_transaction(tx, pk)
    w3.eth.wait_for_transaction_receipt(w3.eth.send_raw_transaction(signed.raw_transaction), timeout=60)


ONEINCH_BASE     = "https://api.1inch.dev/swap/v6.0"
ONEINCH_KEY_ENV  = "ONEINCH_API_KEY"
ONEINCH_CHAIN_ID = {"eth": 1, "base": 8453, "bsc": 56, "poly": 137}
EVM_FEE_TIER: Dict[str, int] = {"eth": 3000, "base": 3000, "bsc": 2500, "poly": 3000}


def _evm_swap_raw(w3, acct, pk: str, tx_data: Dict) -> Any:
    from web3 import Web3  # type: ignore
    nonce   = w3.eth.get_transaction_count(acct.address)
    tx      = {
        "from":     acct.address,
        "to":       Web3.to_checksum_address(tx_data["to"]),
        "data":     tx_data["data"],
        "value":    int(tx_data.get("value", 0)),
        "gas":      int(tx_data.get("gas", 350000)),
        "gasPrice": w3.eth.gas_price,
        "nonce":    nonce,
    }
    signed  = w3.eth.account.sign_transaction(tx, pk)
    receipt = w3.eth.wait_for_transaction_receipt(
        w3.eth.send_raw_transaction(signed.raw_transaction), timeout=120)
    if not receipt or receipt.status != 1:
        raise RuntimeError("EVM_TX_REVERTED")
    return receipt


def _1inch_swap_tx(chain: str, src: str, dst: str, amount: int, sender: str, slip_bps: int) -> Optional[Dict]:
    key = os.getenv(ONEINCH_KEY_ENV, "")
    if not key or chain not in ONEINCH_CHAIN_ID:
        return None
    try:
        resp = requests.get(
            f"{ONEINCH_BASE}/{ONEINCH_CHAIN_ID[chain]}/swap",
            params={
                "src": src, "dst": dst, "amount": amount,
                "from": sender, "slippage": slip_bps / 100,
                "disableEstimate": "true",
            },
            headers={"Authorization": f"Bearer {key}"},
            timeout=12,
        )
        return resp.json() if resp.status_code == 200 else None
    except Exception:
        return None


def live_buy_evm(chain: str, usd: float, price: float, token_addr: str) -> Dict[str, Any]:
    try:
        from web3 import Web3  # type: ignore
        if chain not in USDC_ADDR:
            return {"error": f"NO_CONFIG_FOR_{chain.upper()}"}
        w3         = _evm_w3(chain)
        acct, pk   = _evm_account(w3)
        usdc       = Web3.to_checksum_address(USDC_ADDR[chain])
        token      = Web3.to_checksum_address(token_addr)
        slip_bps   = CONFIG["modes"][CONFIG["mode"]]["slip_bps"]
        usdc_c     = w3.eth.contract(address=usdc,  abi=_ERC20_ABI)
        token_c    = w3.eth.contract(address=token, abi=_ERC20_ABI)
        u_decimals = usdc_c.functions.decimals().call()
        t_decimals = token_c.functions.decimals().call()
        amount_in  = int(usd * 10 ** u_decimals)

        bal_before = token_c.functions.balanceOf(acct.address).call()

        # Try 1inch aggregator first for better routing
        data_1inch = _1inch_swap_tx(chain, usdc, token_addr, amount_in, acct.address, slip_bps)
        if data_1inch and data_1inch.get("tx"):
            spender = Web3.to_checksum_address(data_1inch["tx"]["to"])
            _evm_ensure_approval(w3, usdc_c, acct.address, spender, amount_in, pk)
            receipt = _evm_swap_raw(w3, acct, pk, data_1inch["tx"])
        elif chain in UNISWAP_V3_ROUTER:
            router   = Web3.to_checksum_address(UNISWAP_V3_ROUTER[chain])
            _evm_ensure_approval(w3, usdc_c, acct.address, router, amount_in, pk)
            out_min  = int((usd / price) * 10 ** t_decimals * (1 - slip_bps / 10000))
            deadline = int(time.time()) + 120
            nonce    = w3.eth.get_transaction_count(acct.address)
            router_c = w3.eth.contract(address=router, abi=_SWAP_ROUTER_ABI)
            fee      = EVM_FEE_TIER.get(chain, 3000)
            swap_tx  = router_c.functions.exactInputSingle((
                usdc, token, fee, acct.address, deadline, amount_in, out_min, 0,
            )).build_transaction({"from": acct.address, "nonce": nonce,
                                  "gasPrice": w3.eth.gas_price, "gas": 300000})
            signed   = w3.eth.account.sign_transaction(swap_tx, pk)
            receipt  = w3.eth.wait_for_transaction_receipt(
                w3.eth.send_raw_transaction(signed.raw_transaction), timeout=120)
            if not receipt or receipt.status != 1:
                return {"error": "EVM_TX_REVERTED"}
        else:
            return {"error": f"NO_ROUTER_FOR_{chain.upper()}"}

        bal_after    = token_c.functions.balanceOf(acct.address).call()
        actual_units = (bal_after - bal_before) / 10 ** t_decimals
        filled_px    = usd / actual_units if actual_units > 0 else price
        return {"price": filled_px, "units": actual_units, "tx_hash": receipt.transactionHash.hex()}
    except Exception as e:
        return {"error": str(e)}


def live_sell_evm(chain: str, usd: float, price: float, token_addr: str, pos_units: float) -> Dict[str, Any]:
    try:
        from web3 import Web3  # type: ignore
        if chain not in USDC_ADDR:
            return {"error": f"NO_CONFIG_FOR_{chain.upper()}"}
        w3         = _evm_w3(chain)
        acct, pk   = _evm_account(w3)
        usdc       = Web3.to_checksum_address(USDC_ADDR[chain])
        token      = Web3.to_checksum_address(token_addr)
        slip_bps   = CONFIG["modes"][CONFIG["mode"]]["slip_bps"]
        token_c    = w3.eth.contract(address=token, abi=_ERC20_ABI)
        usdc_c     = w3.eth.contract(address=usdc,  abi=_ERC20_ABI)
        t_decimals = token_c.functions.decimals().call()
        u_decimals = usdc_c.functions.decimals().call()
        frac       = min(1.0, usd / max(1e-9, pos_units * price))
        sell_units = int(pos_units * frac * 10 ** t_decimals)

        usdc_before = usdc_c.functions.balanceOf(acct.address).call()

        data_1inch = _1inch_swap_tx(chain, token_addr, usdc, sell_units, acct.address, slip_bps)
        if data_1inch and data_1inch.get("tx"):
            spender = Web3.to_checksum_address(data_1inch["tx"]["to"])
            _evm_ensure_approval(w3, token_c, acct.address, spender, sell_units, pk)
            receipt = _evm_swap_raw(w3, acct, pk, data_1inch["tx"])
        elif chain in UNISWAP_V3_ROUTER:
            router   = Web3.to_checksum_address(UNISWAP_V3_ROUTER[chain])
            _evm_ensure_approval(w3, token_c, acct.address, router, sell_units, pk)
            out_min  = int(usd * (1 - slip_bps / 10000) * 10 ** u_decimals)
            deadline = int(time.time()) + 120
            nonce    = w3.eth.get_transaction_count(acct.address)
            router_c = w3.eth.contract(address=router, abi=_SWAP_ROUTER_ABI)
            fee      = EVM_FEE_TIER.get(chain, 3000)
            swap_tx  = router_c.functions.exactInputSingle((
                token, usdc, fee, acct.address, deadline, sell_units, out_min, 0,
            )).build_transaction({"from": acct.address, "nonce": nonce,
                                  "gasPrice": w3.eth.gas_price, "gas": 300000})
            signed   = w3.eth.account.sign_transaction(swap_tx, pk)
            receipt  = w3.eth.wait_for_transaction_receipt(
                w3.eth.send_raw_transaction(signed.raw_transaction), timeout=120)
            if not receipt or receipt.status != 1:
                return {"error": "EVM_TX_REVERTED"}
        else:
            return {"error": f"NO_ROUTER_FOR_{chain.upper()}"}

        usdc_after      = usdc_c.functions.balanceOf(acct.address).call()
        actual_proceeds = max(0.0, (usdc_after - usdc_before) / 10 ** u_decimals)
        return {"sold": actual_proceeds, "tx_hash": receipt.transactionHash.hex()}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Live execution — router
# ---------------------------------------------------------------------------
def live_buy(symbol: str, chain: str, usd: float, price: float, liq_usd: float, address: str = "") -> Dict[str, Any]:
    if not address:
        return {"error": "NO_TOKEN_ADDRESS"}
    if chain == "sol":
        return live_buy_sol(usd, price, address)
    return live_buy_evm(chain, usd, price, address)


def live_sell(symbol: str, chain: str, usd: float, price: float, liq_usd: float, address: str = "") -> Dict[str, Any]:
    if not address:
        return {"error": "NO_TOKEN_ADDRESS"}
    pos = STATE["positions"].get(symbol, {})
    if chain == "sol":
        return live_sell_sol(usd, price, address, pos.get("units", 0.0))
    return live_sell_evm(chain, usd, price, address, pos.get("units", 0.0))


def exec_buy(symbol: str, chain: str, usd: float, price: float, liq_usd: float, address: str = "") -> Dict[str, Any]:
    return shadow_buy(symbol, chain, usd, price, liq_usd, address) if SHADOW_MODE else live_buy(symbol, chain, usd, price, liq_usd, address)


def exec_sell(symbol: str, usd: float, price: float, liq_usd: float) -> Dict[str, Any]:
    if SHADOW_MODE:
        return shadow_sell(symbol, usd, price, liq_usd)
    pos     = STATE["positions"].get(symbol, {})
    chain   = pos.get("chain", "sol")
    address = pos.get("address", "")
    return live_sell(symbol, chain, usd, price, liq_usd, address)


# ---------------------------------------------------------------------------
# Wallet balance fetch
# ---------------------------------------------------------------------------
def fetch_sol_balance() -> float:
    """Return USDC balance on Solana in USD."""
    try:
        kp     = _sol_keypair()
        pubkey = str(kp.pubkey())
        rpc    = _sol_rpc()
        resp   = requests.post(rpc, json={
            "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
            "params": [pubkey, {"mint": USDC_MINT_SOL}, {"encoding": "jsonParsed"}],
        }, timeout=10)
        accounts = resp.json().get("result", {}).get("value", [])
        return sum(
            float(acc.get("account", {}).get("data", {}).get("parsed", {})
                  .get("info", {}).get("tokenAmount", {}).get("uiAmount") or 0)
            for acc in accounts
        )
    except Exception as e:
        log(f"fetch_sol_balance: {e}")
        return 0.0


def fetch_evm_balance(chain: str) -> float:
    """Return USDC balance on an EVM chain in USD."""
    try:
        from web3 import Web3  # type: ignore
        if chain not in USDC_ADDR:
            return 0.0
        w3   = _evm_w3(chain)
        acct, _ = _evm_account(w3)
        usdc    = Web3.to_checksum_address(USDC_ADDR[chain])
        c       = w3.eth.contract(address=usdc, abi=_ERC20_ABI)
        dec     = c.functions.decimals().call()
        raw     = c.functions.balanceOf(acct.address).call()
        return raw / 10 ** dec
    except Exception as e:
        log(f"fetch_evm_balance {chain}: {e}")
        return 0.0


def refresh_vault_balance():
    """In live mode, sum on-chain USDC across all configured chains and update vault_usd."""
    if SHADOW_MODE:
        return
    total = fetch_sol_balance() if "sol" in CONFIG["chains"] else 0.0
    for chain in CONFIG["chains"]:
        if chain != "sol":
            total += fetch_evm_balance(chain)
    if total > 0:
        STATE["vault_usd"] = total
        log(f"Live vault: ${total:.2f}")

# ---------------------------------------------------------------------------
# Position manager
# ---------------------------------------------------------------------------
def adaptive_no_pump_window(liq_usd: float) -> int:
    ms = CONFIG["moonshot"]["adaptive_timer"]
    return ms["low_liq_sec"] if liq_usd < 50000 else ms["high_liq_sec"]


def should_exit_no_pump(entry_ts: float, now_ts: float, entry_price: float, cur_price: float, liq_usd: float) -> bool:
    if now_ts - entry_ts < adaptive_no_pump_window(liq_usd):
        return False
    # Use current mode's no_pump config; fall back to degen for modes that don't define it
    mode_cfg = CONFIG["modes"].get(CONFIG["mode"], {})
    hurdle   = mode_cfg.get("no_pump", CONFIG["modes"]["degen"]["no_pump"])["hurdle"]
    return (cur_price - entry_price) / entry_price < hurdle

# ---------------------------------------------------------------------------
# Auto-scale
# ---------------------------------------------------------------------------
def autoscale_maybe(symbol: str, chain: str, price: float, liq_usd: float, velocity_ok: bool):
    A = CONFIG["autoscale"]
    if not A["enabled"] or not velocity_ok:
        return
    pos = STATE["positions"].get(symbol, {})
    if pos.get("add_count", 0) >= A["max_adds"]:
        return
    if time.time() - pos.get("last_add_ts", 0.0) < A["cooldown_min"] * 60:
        return
    add_usd = A["add_frac"] * pos.get("usd", 0.0)
    if add_usd <= 0:
        return
    if est_price_impact(add_usd, liq_usd) > A["pi_max"]:
        add_usd *= 0.5
        if est_price_impact(add_usd, liq_usd) > A["pi_max"]:
            return
    add_usd = min(add_usd, per_chain_room(chain), per_token_cap_room())
    if add_usd < CONFIG["moonshot"]["min_ticket_usd"]:
        return
    addr = pos.get("address", "")
    shadow_buy(symbol, chain, add_usd, price, liq_usd, addr)
    pos["add_count"]   = pos.get("add_count", 0) + 1
    pos["last_add_ts"] = time.time()
    log(f"AUTOSCALE {symbol} add #{pos['add_count']} ${add_usd:.2f}")
    send_alert(
        f"📈 ADDED to {symbol} — it's winning, so the bot put in ${add_usd:.0f} more (paper) to ride the "
        f"momentum. Add #{pos['add_count']}.")

# ---------------------------------------------------------------------------
# Old-coin pump detector
# ---------------------------------------------------------------------------
def detect_oldcoin_pump(symbol: str, volume_x: float, mentions_x: float) -> bool:
    """True if h1 volume ≥ volume_x × trailing avg AND (if LunarCrush key set) social volume ≥ mentions_x."""
    addr = CONFIG["oldcoin"].get("watchlist", {}).get(symbol.upper())
    if not addr:
        return False
    data = fetch_dexscreener_token(addr)
    if not data:
        return False
    vol_spike = False
    for pair in (data.get("pairs") or []):
        try:
            vol   = pair.get("volume") or {}
            h1    = float(vol.get("h1")  or 0)
            h24   = float(vol.get("h24") or 0)
            avg_h = h24 / 24
            if avg_h > 0 and h1 >= avg_h * volume_x:
                vol_spike = True
                break
        except Exception:
            continue
    if not vol_spike:
        return False
    # Social gate: check LunarCrush (or CoinGecko trending fallback) when mentions_x configured
    if mentions_x > 1.0:
        return fetch_social_volume(symbol) >= mentions_x
    return True

def check_rpc_health():
    health: Dict[str, Any] = {}
    for chain, urls in CONFIG["rpc"].items():
        url = (urls[0] if isinstance(urls, list) and urls else None)
        if not url:
            health[chain] = {"status": "no_url", "ms": None}
            continue
        try:
            t0 = time.time()
            if chain == "sol":
                resp = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "getHealth"}, timeout=5)
                ok   = resp.status_code == 200
            else:
                resp = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}, timeout=5)
                ok   = resp.status_code == 200 and "result" in resp.json()
            health[chain] = {"status": "ok" if ok else "err", "ms": int((time.time() - t0) * 1000)}
        except Exception:
            health[chain] = {"status": "err", "ms": None}
    STATE["rpc"]["health"] = health

# ---------------------------------------------------------------------------
# Trusted coins
# ---------------------------------------------------------------------------
TRUSTED = {
    "DOGE": {"core_units": 30000, "exit_band": [0.50, 0.70], "retrace": 0.70, "floor": 0.12}
}


def manage_trusted_coins():
    for sym, spec in TRUSTED.items():
        addr = CONFIG["oldcoin"].get("watchlist", {}).get(sym.upper())
        if not addr:
            continue
        data = fetch_dexscreener_token(addr)
        if not data or not data.get("pairs"):
            continue
        p0    = data["pairs"][0]
        price = float(p0.get("priceUsd") or 0)
        liq   = float((p0.get("liquidity") or {}).get("usd") or 100000.0)
        chain = next((k for k, v in CHAIN_IDS.items() if v == p0.get("chainId", "")), "sol")
        if not price:
            continue
        pos   = STATE["positions"].get(sym, {})
        units = pos.get("units", 0.0)

        floor = spec.get("floor", 0)
        if floor and price <= floor and units < spec.get("core_units", 0):
            usd = size_ticket_usd(chain)
            if usd >= CONFIG["moonshot"]["min_ticket_usd"]:
                shadow_buy(sym, chain, usd, price, liq, addr)
                log(f"TRUSTED dip-buy {sym} @{price:.5f} (floor {floor})")
                send_alert(
                    f"🐕 BOUGHT THE DIP on {sym} @ {price:.5f} — it dropped to your set floor price, so the "
                    f"bot grabbed more of this trusted long-term hold while it's cheap.")

        band = spec.get("exit_band", [None, None])
        core = spec.get("core_units", 0)
        if band[0] and band[1] and band[0] <= price <= band[1] and units > core:
            trim_units = units - core
            trim_usd   = trim_units * price
            if trim_usd >= CONFIG["moonshot"]["min_ticket_usd"]:
                shadow_sell(sym, trim_usd, price, liq)
                log(f"TRUSTED trim {sym}: {trim_units:.0f} units @{price:.5f}")
                send_alert(
                    f"🐕 TRIMMED {sym} @ {price:.5f} — it rose into your take-profit zone, so the bot sold "
                    f"some to bank gains while keeping your core long-term bag.")

# ---------------------------------------------------------------------------
# Re-entry watch
# ---------------------------------------------------------------------------
# Exit reasons that mean the token FAILED (dumped) — don't chase these back in.
# Only cooled-off winners (TRAIL STOP) qualify for re-entry.
_HARD_EXIT_PREFIXES = ("RUG", "VELOCITY", "LIQ DRAIN", "fixed_sl")


def _add_reentry_watch(sym: str, pos: Dict, cur_liq: float, cur_vol_h1: float, reason: str):
    R = CONFIG["moonshot"]["reentry"]
    if not R["enabled"] or not pos.get("address"):
        return
    # Skip re-entry for hard-dump exits (rug, velocity crash, liq drain, stop-loss).
    # This is the fix for re-buying a token that just failed us.
    if R.get("skip_hard_exits", True) and reason.startswith(_HARD_EXIT_PREFIXES):
        log(f"REENTRY SKIP {sym}: hard exit ({reason}) — not chasing it back")
        return
    STATE.setdefault("reentry_watch", {})[sym] = {
        "exit_ts":      time.time(),
        "exit_reason":  reason,
        "entry_liq":    pos.get("entry_liq", cur_liq),
        "entry_vol_h1": pos.get("entry_vol_h1", cur_vol_h1),
        "address":      pos.get("address", ""),
        "chain":        pos.get("chain", "sol"),
        "price_samples": [],
    }
    log(f"REENTRY WATCH added {sym} (reason: {reason})")


def check_reentry_watch():
    watch = STATE.get("reentry_watch", {})
    if not watch:
        return
    R      = CONFIG["moonshot"]["reentry"]
    now_ts = time.time()

    # Batch fetch
    addr_map = {d["address"].lower(): sym for sym, d in watch.items() if d.get("address")}
    if not addr_map:
        return
    data    = _get(DEXSCREENER_TOKEN + ",".join(addr_map))
    pairs   = (data or {}).get("pairs") or []
    by_addr: Dict[str, Dict] = {}
    for pair in pairs:
        addr = ((pair.get("baseToken") or {}).get("address") or "").lower()
        if addr and addr not in by_addr:
            by_addr[addr] = pair

    to_remove: List[str] = []
    for sym, w in list(watch.items()):
        addr = w.get("address", "").lower()
        pair = by_addr.get(addr)
        if not pair:
            continue

        if now_ts - w.get("exit_ts", now_ts) < R["cooldown_min"] * 60:
            continue

        price     = float(pair.get("priceUsd") or 0)
        liq       = float((pair.get("liquidity") or {}).get("usd") or 0)
        vol_h1    = float((pair.get("volume")      or {}).get("h1") or 0)
        change_m5 = float((pair.get("priceChange") or {}).get("m5") or 0)

        if price <= 0:
            to_remove.append(sym)
            continue

        # Still crashing? reset stability clock
        if change_m5 < -5.0:
            w["price_samples"] = []
            continue

        # Liq too low — token dying, drop from watch
        entry_liq = w.get("entry_liq", liq)
        if entry_liq > 0 and liq < entry_liq * R["liq_floor_pct"]:
            log(f"REENTRY DROP {sym}: liq {liq:.0f} < floor {entry_liq * R['liq_floor_pct']:.0f}")
            to_remove.append(sym)
            continue

        # Volume dead
        if vol_h1 < R["vol_min_h1"]:
            continue

        # Accumulate stability samples
        samples: List[float] = w.setdefault("price_samples", [])
        samples.append(price)
        if len(samples) > R["stable_ticks"]:
            samples[:] = samples[-R["stable_ticks"]:]

        if len(samples) < R["stable_ticks"]:
            continue

        # Stability gate: price range within stable_range_pct
        lo, hi = min(samples), max(samples)
        if (hi - lo) / max(lo, 1e-12) > R["stable_range_pct"]:
            continue

        # All green — re-enter
        chain  = w.get("chain", "sol")
        ticket = size_ticket_usd(chain)
        if ticket < CONFIG["moonshot"]["min_ticket_usd"]:
            to_remove.append(sym)
            continue
        exec_buy(sym, chain, ticket, price, liq, w.get("address", ""))
        log(f"REENTRY {sym} @{price:.6f} ${ticket:.2f} (stable {R['stable_ticks']} ticks, liq ${liq:.0f})")
        send_alert(f"🔄 RE-ENTRY {sym} @{price:.6f} — price stable, liq holding | ${ticket:.2f}")
        save_state()
        to_remove.append(sym)

    for sym in to_remove:
        watch.pop(sym, None)


# ---------------------------------------------------------------------------
# Presale assistant
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Scam / rug safety gate — Reddit + X chatter + on-chain checks
# ---------------------------------------------------------------------------
X_BEARER_ENV = "X_BEARER_TOKEN"
HONEYPOT_CHAIN_ID = {"eth": 1, "base": 8453, "bsc": 56, "poly": 137}
SCAM_WORDS = (
    "scam", "rug", "rugged", "rugpull", "rug pull", "honeypot", "honey pot",
    "ponzi", "avoid", "do not buy", "don't buy", "dont buy", "scammer",
    "phishing", "stay away", "exit scam", "stolen", "fake team",
)
REDDIT_ID_ENV     = "REDDIT_CLIENT_ID"
REDDIT_SECRET_ENV = "REDDIT_CLIENT_SECRET"
_reddit_cache: Dict[str, Any] = {}
_x_cache:      Dict[str, Any] = {}
_reddit_token: Dict[str, Any] = {"token": None, "exp": 0.0}


def _reddit_oauth_token() -> Optional[str]:
    """Userless Reddit OAuth token (free app credentials). Cached until expiry.
    Reddit blocks the public .json API from datacenter IPs, so OAuth is required on a server."""
    cid = os.getenv(REDDIT_ID_ENV, "")
    sec = os.getenv(REDDIT_SECRET_ENV, "")
    if not cid:
        return None
    if _reddit_token["token"] and time.time() < _reddit_token["exp"]:
        return _reddit_token["token"]
    try:
        r = requests.post("https://www.reddit.com/api/v1/access_token",
                          auth=(cid, sec),
                          data={"grant_type": "client_credentials"},
                          headers={"User-Agent": "tothemoon-bot/1.0"}, timeout=8)
        if r.status_code == 200:
            j = r.json()
            _reddit_token["token"] = j.get("access_token")
            _reddit_token["exp"]   = time.time() + float(j.get("expires_in", 3600)) - 60
            return _reddit_token["token"]
    except Exception:
        pass
    return None


def _count_scam(texts: List[str]) -> int:
    n = 0
    for t in texts:
        low = (t or "").lower()
        if any(w in low for w in SCAM_WORDS):
            n += 1
    return n


def fetch_reddit_sentiment(symbol: str) -> Dict[str, int]:
    """Recent Reddit posts mentioning the token + how many sound like scam warnings. Cached 10 min."""
    key = symbol.upper()
    c = _reddit_cache.get(key)
    if c and time.time() - c["ts"] < 600:
        return c["data"]
    out = {"mentions": 0, "scam_hits": 0}
    tok = _reddit_oauth_token()
    try:
        ua = {"User-Agent": "tothemoon-bot/1.0"}
        q  = requests.utils.quote(symbol)
        if tok:   # OAuth path — works from datacenter IPs
            url = f"https://oauth.reddit.com/search?q={q}&sort=new&limit=25&t=week"
            r = requests.get(url, headers={**ua, "Authorization": f"bearer {tok}"}, timeout=8)
        else:     # public path — works locally, 403s from cloud servers (returns 0 gracefully)
            url = f"https://www.reddit.com/search.json?q={q}&sort=new&limit=25&t=week"
            r = requests.get(url, headers=ua, timeout=8)
        if r.status_code == 200:
            posts = r.json().get("data", {}).get("children", [])
            out["mentions"] = len(posts)
            out["scam_hits"] = _count_scam(
                [f"{p.get('data', {}).get('title', '')} {p.get('data', {}).get('selftext', '')}" for p in posts])
    except Exception:
        pass
    _reddit_cache[key] = {"ts": time.time(), "data": out}
    return out


def fetch_x_buzz(symbol: str) -> Dict[str, Any]:
    """Recent X/Twitter mentions + scam mentions. Needs X_BEARER_TOKEN (paid API); no-op otherwise."""
    tok = os.getenv(X_BEARER_ENV, "")
    if not tok:
        return {"mentions": 0, "scam_hits": 0, "enabled": False}
    key = symbol.upper()
    c = _x_cache.get(key)
    if c and time.time() - c["ts"] < 600:
        return c["data"]
    out = {"mentions": 0, "scam_hits": 0, "enabled": True}
    try:
        q = requests.utils.quote(f"{symbol} -is:retweet lang:en")
        url = f"https://api.twitter.com/2/tweets/search/recent?query={q}&max_results=50&tweet.fields=text"
        r = requests.get(url, headers={"Authorization": f"Bearer {tok}"}, timeout=8)
        if r.status_code == 200:
            tweets = r.json().get("data", [])
            out["mentions"]  = len(tweets)
            out["scam_hits"] = _count_scam([t.get("text", "") for t in tweets])
    except Exception:
        pass
    _x_cache[key] = {"ts": time.time(), "data": out}
    return out


def fetch_rugcheck(address: str) -> Dict[str, Any]:
    """rugcheck.xyz Solana rug/scam report. Flags danger-level risks or a high risk score."""
    out: Dict[str, Any] = {"flagged": False, "reason": "", "score": None}
    try:
        r = requests.get(
            f"https://api.rugcheck.xyz/v1/tokens/{address}/report/summary",
            headers={"User-Agent": "Mozilla/5.0 (tothemoon-bot)"}, timeout=8)
        if r.status_code != 200:
            return out
        data = r.json() or {}
        out["score"] = data.get("score_normalised")
        dangers = [x.get("name") for x in (data.get("risks") or []) if x.get("level") == "danger"]
        if dangers:
            out["flagged"] = True
            out["reason"]  = "rugcheck flags " + ", ".join(d for d in dangers[:2] if d)
        elif out["score"] is not None and out["score"] >= CONFIG["safety"]["rugcheck_score_max"]:
            out["flagged"] = True
            out["reason"]  = f"rugcheck risk score {out['score']}/100 (high)"
    except Exception:
        pass
    return out


def fetch_onchain_safety(address: str, chain: str) -> Dict[str, Any]:
    """On-chain rug checks: Solana rugcheck.xyz + holder concentration, EVM honeypot/sell-tax."""
    out: Dict[str, Any] = {"flagged": False, "reason": ""}
    if not address:
        return out
    if chain == "sol":
        # rugcheck.xyz is the primary Solana scam/rug signal (LP lock, mint authority,
        # holder distribution, known scams). Fall back to raw holder concentration if it's down.
        rc = fetch_rugcheck(address)
        if rc["flagged"]:
            return {"flagged": True, "reason": rc["reason"]}
        if rc["score"] is not None:
            return out   # rugcheck ran and cleared it — trust it over the rough RPC heuristic
        try:
            rpc = _sol_rpc()
            la = requests.post(rpc, json={"jsonrpc": "2.0", "id": 1,
                "method": "getTokenLargestAccounts", "params": [address]}, timeout=8).json()
            sup = requests.post(rpc, json={"jsonrpc": "2.0", "id": 1,
                "method": "getTokenSupply", "params": [address]}, timeout=8).json()
            total = float(sup["result"]["value"]["uiAmount"] or 0)
            vals  = sorted((float(a.get("uiAmount") or 0) for a in la["result"]["value"]), reverse=True)
            # Assume the single largest holder is the liquidity pool; flag the next-largest
            # real wallet if it controls a dangerous share of supply.
            non_lp = vals[1:]
            if total > 0 and non_lp:
                share = non_lp[0] / total
                if share > CONFIG["safety"]["sol_holder_max_pct"]:
                    out["flagged"] = True
                    out["reason"]  = f"one wallet holds {share*100:.0f}% of supply (whale can dump on you)"
        except Exception:
            pass
    elif chain in HONEYPOT_CHAIN_ID:
        try:
            cid = HONEYPOT_CHAIN_ID[chain]
            r = _get(f"https://api.honeypot.is/v2/IsHoneypot?address={address}&chainID={cid}")
            if r:
                if r.get("honeypotResult", {}).get("isHoneypot"):
                    out["flagged"] = True
                    out["reason"]  = "flagged as a honeypot — you could buy but not sell"
                else:
                    tax = float((r.get("simulationResult", {}) or {}).get("sellTax", 0) or 0)
                    if tax > CONFIG["safety"]["evm_sell_tax_max"]:
                        out["flagged"] = True
                        out["reason"]  = f"sell tax {tax:.0f}% — scammy tokenomics"
        except Exception:
            pass
    return out


def safety_gate(symbol: str, address: str, chain: str) -> "tuple[bool, str]":
    """Combined scam/rug check for a token the bot is about to buy. Returns (ok, reason_if_blocked)."""
    S = CONFIG["safety"]
    scam_hits = 0
    parts: List[str] = []
    if S.get("reddit_enabled"):
        rd = fetch_reddit_sentiment(symbol)
        scam_hits += rd["scam_hits"]
        if rd["scam_hits"]:
            parts.append(f"Reddit: {rd['scam_hits']} scam-warning post(s)")
    if S.get("x_enabled"):
        xb = fetch_x_buzz(symbol)
        if xb.get("enabled"):
            scam_hits += xb["scam_hits"]
            if xb["scam_hits"]:
                parts.append(f"X: {xb['scam_hits']} scam mention(s)")
    if scam_hits >= S.get("scam_chatter_max", 2):
        return False, "scam chatter online — " + "; ".join(parts)
    if S.get("onchain_enabled"):
        oc = fetch_onchain_safety(address, chain)
        if oc["flagged"]:
            return False, "on-chain risk — " + oc["reason"]
    return True, ""


def presale_score(meta: Dict[str, Any]) -> int:
    score  = 20 if meta.get("audit") else 0
    score += 20 if meta.get("kyc")   else 0
    score += min(30, int(meta.get("lock_days", 0) / 30))
    score += min(30, int(meta.get("mentions",  0) / 5000) * 10)
    return score

# ---------------------------------------------------------------------------
# AI auto-pilot (Claude Haiku) — recommends/auto-applies a risk mode
# ---------------------------------------------------------------------------
ANTHROPIC_KEY_ENV = "ANTHROPIC_API_KEY"

AI_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "recommended_mode": {"type": "string", "enum": ["safe", "default", "hype", "degen"]},
        "confidence":       {"type": "number"},
        "aggressive":       {"type": "boolean"},
        "reasoning":        {"type": "string"},
    },
    "required": ["recommended_mode", "confidence", "reasoning"],
    "additionalProperties": False,
}

AI_SYSTEM = (
    "You are the risk-mode controller for a multi-chain crypto memecoin scalping bot (paper-trading). "
    "You pick ONE risk profile for the next ~30 minutes.\n\n"
    "Modes, least to most aggressive (TP = take-profit targets, SL = stop-loss):\n"
    "- safe:    TP +15%, SL -7%, needs $50k+ liquidity. CAPS winners early.\n"
    "- default: TP +28%, SL -12%, $30k+ liquidity.\n"
    "- hype:    TP +45%/+90%, SL -18%, $20k+ liquidity. Lets winners run further.\n"
    "- degen:   TP +80%/+150%, SL -28%, $10k+ liquidity. Lets the wildest moonshots run; more rugs.\n\n"
    "CRITICAL — how this strategy makes money: it is ASYMMETRIC. Most trades lose a little, a FEW win big. "
    "A 40-55% win rate is NORMAL and HEALTHY here — do NOT turn conservative just because win rate looks 'low'. "
    "What matters is EXPECTANCY (per-trade expected value) and whether big winners are being captured. "
    "If avg_win is much larger than avg_loss and expectancy is positive, the strategy is working — stay in "
    "default/hype/degen to LET WINNERS RUN.\n\n"
    "Beware: 'safe' mode's tight +15% take-profit CAPS the big winners that this strategy depends on, which "
    "destroys the edge. Only choose 'safe' when expectancy is genuinely NEGATIVE or the drawdown brake is active. "
    "Otherwise prefer default (balanced) or hype/degen (when momentum is strong and fresh launches are passing). "
    "Lean MORE aggressive when expectancy is positive and alt momentum is strong; pull back toward 'default' "
    "(not 'safe') when the market is BTC-dominated or candidates are thin. Per-trade size is capped elsewhere — "
    "you ONLY choose the risk profile; you cannot oversize.\n\n"
    "Return strict JSON: recommended_mode, confidence (0-1), aggressive (bool), one-sentence reasoning."
)


def _scout_reason_summary(n: int = 40) -> Dict[str, int]:
    """Tally recent scout decisions so the AI can see what the scanner is finding."""
    out: Dict[str, int] = {"entered": 0, "suggested": 0, "rejected": 0}
    for e in STATE.get("scout_log", [])[-n:]:
        out[e.get("decision", "rejected")] = out.get(e.get("decision", "rejected"), 0) + 1
    return out


def _ai_market_context() -> Dict[str, Any]:
    hist     = STATE.get("pnl_hist", [])[-30:]
    wins_l   = [x for x in hist if x > 0]
    losses_l = [x for x in hist if x <= 0]
    wr       = (len(wins_l) / len(hist)) if hist else 0.0
    avg_win  = (sum(wins_l) / len(wins_l)) if wins_l else 0.0
    avg_loss = (sum(losses_l) / len(losses_l)) if losses_l else 0.0
    expectancy = wr * avg_win + (1 - wr) * avg_loss   # expected $ per trade
    return {
        "current_mode":     CONFIG["mode"],
        "btc_dominance":    STATE["signals"].get("btc_d"),
        "market_heat":      STATE["signals"].get("heat"),
        "vault_usd":        round(STATE.get("vault_usd", 0), 2),
        "open_positions":   sum(1 for p in STATE.get("positions", {}).values() if p.get("units", 0) > 0),
        "recent_trades":    len(hist),
        "recent_win_rate":  round(wr, 2),
        "avg_win":          round(avg_win, 2),
        "avg_loss":         round(avg_loss, 2),
        "expectancy_per_trade": round(expectancy, 2),   # positive = profitable even at low win rate
        "biggest_recent_win":   round(max(hist), 2) if hist else 0,
        "recent_pnl":       round(sum(hist), 2),
        "scout_last_40":    _scout_reason_summary(40),
        "drawdown_brake":   drawdown_brake_active(),
    }


def ai_advise(force: bool = False) -> Optional[Dict[str, Any]]:
    """Consult Claude Haiku for a risk-mode recommendation. Returns the decision dict or None.
    Gated on CONFIG['ai']['enabled'] + ANTHROPIC_API_KEY; auto-applies the mode if configured."""
    A = CONFIG["ai"]
    if not force and not A.get("enabled"):
        return None
    if not os.getenv(ANTHROPIC_KEY_ENV):
        if force:
            log("AI: ANTHROPIC_API_KEY not set")
        return None
    try:
        import anthropic  # lazy — bot runs fine without the dep installed
    except ImportError:
        log("AI: `anthropic` package not installed (pip install anthropic)")
        return None

    ctx = _ai_market_context()
    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=A.get("model", "claude-haiku-4-5"),
            max_tokens=400,
            system=AI_SYSTEM,
            messages=[{"role": "user", "content": json.dumps(ctx)}],
            output_config={"format": {"type": "json_schema", "schema": AI_DECISION_SCHEMA}},
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        decision = json.loads(text)
    except Exception as e:
        log(f"AI advise failed: {e}")
        return None

    decision["confidence"] = max(0.0, min(1.0, float(decision.get("confidence", 0))))
    decision["ts"] = now_utc().isoformat()
    decision["from_mode"] = CONFIG["mode"]

    aistate = STATE.setdefault("ai", {"last_run": None, "last": None, "history": []})
    aistate["last_run"] = decision["ts"]
    aistate["last"]     = decision
    aistate.setdefault("history", []).append(decision)
    if len(aistate["history"]) > 100:
        del aistate["history"][:len(aistate["history"]) - 100]

    rec = decision["recommended_mode"]
    applied = False
    if (A.get("auto_apply") and rec in CONFIG["modes"]
            and rec != CONFIG["mode"] and decision["confidence"] >= A.get("min_confidence", 0.6)):
        old = CONFIG["mode"]
        CONFIG["mode"] = rec
        applied = True
        log(f"AI auto-switched mode {old} → {rec} (conf {decision['confidence']:.2f})")
        send_alert(
            f"🤖 AI switched the bot from '{old}' to '{rec}' mode ({decision['confidence']:.0%} sure). "
            f"Mode = how aggressive it trades (safe → degen). Why: {decision['reasoning']}", critical=True)
    else:
        send_alert(
            f"🤖 AI tip: consider '{rec}' mode ({decision['confidence']:.0%} sure). "
            f"Mode = how aggressive the bot trades (safe → degen). Why: {decision['reasoning']}")
    decision["applied"] = applied
    save_state()
    return decision

# ---------------------------------------------------------------------------
# Flask
# ---------------------------------------------------------------------------
app = Flask(__name__)

try:
    from flask_cors import CORS  # type: ignore
    CORS(app)
except ImportError:
    pass

_here         = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_DIR = os.getenv("DASHBOARD_DIR", os.path.join(_here, "..", "dashboard"))
ALERT_LOG: deque = deque(maxlen=200)


def _dash_auth(fn):
    @wraps(fn)
    def w(*args, **kwargs):
        tok = os.getenv("DASHBOARD_TOKEN", "")
        if tok and flask_request.headers.get("Authorization") != f"Bearer {tok}":
            abort(401)
        return fn(*args, **kwargs)
    return w


@app.route("/status")
def http_status():
    return jsonify({
        "vault_usd":   STATE["vault_usd"],
        "deployable":  deployable_now(),
        "positions":   len([p for p in STATE["positions"].values() if p.get("units", 0) > 0]),
        "mode":        CONFIG["mode"],
        "shadow_mode": SHADOW_MODE,
        "objective":   CONFIG["objective"],
        "signals":     STATE["signals"],
        "rpc":         STATE["rpc"],
        "build":       BUILD.get("sha", "dev"),
    })


@app.route("/app", defaults={"path": ""})
@app.route("/app/<path:path>")
def dashboard_ui(path):
    return send_from_directory(DASHBOARD_DIR, "index.html")


@app.route("/api/state")
@_dash_auth
def api_state():
    mode_cfg = CONFIG["modes"].get(CONFIG["mode"], {})
    return jsonify({
        **{k: v for k, v in STATE.items() if k not in ("trade_log", "liq_prev", "scout_log")},
        "deployable_usd":  deployable_now(),
        "mode":            CONFIG["mode"],
        "modes":           list(CONFIG["modes"].keys()),
        "mode_tp":         mode_cfg.get("tp", []),
        "mode_sl":         mode_cfg.get("sl", 0.0),
        "shadow_mode":     SHADOW_MODE,
        "moonshot_mode":   CONFIG["moonshot"]["mode"],
        "auto_old":        CONFIG["oldcoin"]["auto_join"],
        "objective_kind":  CONFIG["objective"]["kind"],
        "objective":       CONFIG["objective"],
        "watchlist":       CONFIG["oldcoin"].get("watchlist", {}),
        "skim_enabled":    STATE.get("skim", {}).get("enabled", False),
        "build":           BUILD.get("sha", "local"),
        "brake_active":    drawdown_brake_active(),
        # Tunable config surfaced so the dashboard can edit it
        "config": {
            "presale_min_score": CONFIG.get("presale_min_score", 10),
            "gas_sim":           CONFIG.get("gas_sim", True),
            "gas_dynamic":       CONFIG.get("gas_dynamic", True),
            "gas_usd":           CONFIG.get("gas_usd", {}),
            "skim_pct":          CONFIG.get("skim_pct", 0.10),
            "reentry":           CONFIG["moonshot"]["reentry"],
            "oldcoin": {
                "volume_x":   CONFIG["oldcoin"]["volume_x"],
                "mentions_x": CONFIG["oldcoin"]["mentions_x"],
            },
            "doge":   TRUSTED.get("DOGE", {}),
            "ai":     CONFIG["ai"],
            "safety": CONFIG["safety"],
            "degen_terms": CONFIG.get("degen_terms", {}),
            "stall_exit":  CONFIG.get("stall_exit", {}),
            "buy_ratio_min": CONFIG["moonshot"].get("buy_ratio_min", 0.45),
            "max_open_positions": CONFIG.get("max_open_positions", 12),
            "base_size_usd": CONFIG.get("base_size_usd", 30.0),
            "blacklist":   CONFIG.get("blacklist", []),
        },
        "vault_start":     STATE.get("vault_start", 1000.0),
        "ai_key_set":      bool(os.getenv(ANTHROPIC_KEY_ENV)),
        "x_key_set":       bool(os.getenv(X_BEARER_ENV)),
        "reddit_key_set":  bool(os.getenv(REDDIT_ID_ENV)),
    })


@app.route("/api/positions")
@_dash_auth
def api_positions():
    prices = fetch_positions_prices()
    result: Dict[str, Any] = {}
    for sym, p in STATE["positions"].items():
        if p.get("units", 0) <= 0:
            continue
        px        = prices.get(sym, _px_dict(p.get("avg", 0) * 1.02))
        cur_price = px["price"]
        cur_liq   = px["liq"]
        cost      = p.get("usd", 0.0)
        value     = p.get("units", 0.0) * cur_price
        pnl_usd   = value - cost
        pnl_pct   = (pnl_usd / cost * 100) if cost else 0.0
        peak      = p.get("peak_price", p.get("avg", cur_price))
        trail_pct = ((cur_price / peak) - 1) * 100 if peak else 0.0
        result[sym] = {**p, "cur_price": cur_price, "cur_liq": cur_liq,
                       "vol_h1": px["vol_h1"], "change_m5": px["change_m5"],
                       "value": value, "pnl_usd": pnl_usd, "pnl_pct": pnl_pct,
                       "trail_pct": trail_pct}
    return jsonify(result)


@app.route("/api/history")
@_dash_auth
def api_history():
    trades  = STATE.get("trade_log", [])
    sells   = [t for t in trades if t.get("side") == "sell" and t.get("pnl") is not None]
    wins    = [t for t in sells if t["pnl"] > 0]
    losses  = [t for t in sells if t["pnl"] <= 0]
    running = 0.0
    enriched: List[Dict] = []
    for t in trades:
        if t.get("side") == "sell" and t.get("pnl") is not None:
            running += t["pnl"]
        enriched.append({**t, "running_pnl": running})
    return jsonify({
        "trades":    enriched,
        "total_pnl": running,
        "win_rate":  len(wins) / max(1, len(sells)),
        "avg_win":   sum(t["pnl"] for t in wins)   / max(1, len(wins)),
        "avg_loss":  sum(t["pnl"] for t in losses) / max(1, len(losses)),
    })


@app.route("/api/alerts")
@_dash_auth
def api_alerts():
    return jsonify(list(ALERT_LOG))


@app.route("/api/rpc")
@_dash_auth
def api_rpc():
    return jsonify(STATE.get("rpc", {}).get("health", {}))


@app.route("/api/scoutlog")
@_dash_auth
def api_scoutlog():
    """Recent candidate evaluations (most recent first) — what it looked at and why it decided."""
    log_list = STATE.get("scout_log", [])
    counts = {"entered": 0, "suggested": 0, "rejected": 0}
    for e in log_list:
        counts[e.get("decision", "rejected")] = counts.get(e.get("decision", "rejected"), 0) + 1
    return jsonify({"scout": list(reversed(log_list)), "counts": counts})


@app.route("/api/mode", methods=["POST"])
@_dash_auth
def api_set_mode():
    m = (flask_request.get_json() or {}).get("mode", "")
    if m not in CONFIG["modes"]:
        return jsonify({"error": "invalid mode"}), 400
    CONFIG["mode"] = m
    return jsonify({"ok": True, "mode": m})


@app.route("/api/shadow", methods=["POST"])
@_dash_auth
def api_set_shadow():
    set_shadow_mode(bool((flask_request.get_json() or {}).get("enabled", True)))
    return jsonify({"ok": True, "shadow_mode": SHADOW_MODE})


@app.route("/api/moonshot", methods=["POST"])
@_dash_auth
def api_set_moonshot():
    m = (flask_request.get_json() or {}).get("mode", "")
    if m not in ("enter", "suggest"):
        return jsonify({"error": "invalid mode"}), 400
    CONFIG["moonshot"]["mode"] = m
    return jsonify({"ok": True})


@app.route("/api/auto_old", methods=["POST"])
@_dash_auth
def api_set_auto_old():
    CONFIG["oldcoin"]["auto_join"] = bool((flask_request.get_json() or {}).get("enabled", False))
    return jsonify({"ok": True})


@app.route("/api/boost", methods=["POST"])
@_dash_auth
def api_set_boost():
    data  = flask_request.get_json() or {}
    mult  = float(data.get("mult", 1.0))
    hours = float(data.get("hours", 1.0))
    if mult <= 0:
        return jsonify({"error": "mult must be > 0"}), 400
    expires = (now_utc() + timedelta(hours=hours)).isoformat()
    STATE["boost"] = {"mult": mult, "expires": expires}
    save_state()
    return jsonify({"ok": True, "expires": expires})


@app.route("/api/spray", methods=["POST"])
@_dash_auth
def api_set_spray():
    data  = flask_request.get_json() or {}
    until = data.get("until")
    if until:
        datetime.fromisoformat(until)
    STATE["spray_until"] = until
    save_state()
    return jsonify({"ok": True, "spray_until": until})


@app.route("/api/objective", methods=["POST"])
@_dash_auth
def api_set_objective():
    data  = flask_request.get_json() or {}
    kind  = data.get("kind", "off")
    if kind == "off":
        CONFIG["objective"]["kind"] = "off"
        return jsonify({"ok": True})
    target = float(data.get("target_usd", 0))
    weeks  = int(data.get("weeks", 0))
    if target <= 0 or weeks <= 0:
        return jsonify({"error": "target_usd and weeks required"}), 400
    start_objective(target, weeks)
    return jsonify({"ok": True})


@app.route("/api/buy", methods=["POST"])
@_dash_auth
def api_buy():
    data    = flask_request.get_json() or {}
    symbol  = data.get("symbol", "").upper().strip()
    usd     = float(data.get("usd", 0))
    chain   = data.get("chain", "sol").lower()
    address = data.get("address", "")
    price   = float(data.get("price") or 0)
    liq     = float(data.get("liq")   or 0)
    if not symbol or usd <= 0:
        return jsonify({"error": "symbol and usd required"}), 400
    # Auto-fetch price and liq from DexScreener if not supplied
    if (price <= 0 or liq <= 0) and address:
        dex = fetch_dexscreener_token(address)
        if dex and dex.get("pairs"):
            p0    = dex["pairs"][0]
            price = price or float(p0.get("priceUsd") or 0)
            liq   = liq   or float((p0.get("liquidity") or {}).get("usd") or 100000.0)
    if price <= 0:
        return jsonify({"error": "price required (could not auto-fetch from DexScreener)"}), 400
    if liq <= 0:
        liq = 100000.0
    res = exec_buy(symbol, chain, usd, price, liq, address)
    save_state()
    return jsonify(res)


@app.route("/api/sell", methods=["POST"])
@_dash_auth
def api_sell():
    data   = flask_request.get_json() or {}
    symbol = data.get("symbol", "").upper().strip()
    amount = str(data.get("amount", "100%"))
    pos    = STATE["positions"].get(symbol)
    if not symbol or not pos:
        return jsonify({"error": "position not found"}), 404
    # Use live price/liq if not explicitly passed
    px    = fetch_positions_prices().get(symbol, _px_dict(pos.get("avg", 1.0) * 1.02, pos.get("entry_liq", 100000.0)))
    price = float(data.get("price") or 0) or px["price"]
    liq   = float(data.get("liq")   or 0) or px["liq"]
    sell_usd = _parse_sell_amount(pos, amount)
    if sell_usd <= 0:
        return jsonify({"error": "nothing to sell"}), 400
    res = exec_sell(symbol, sell_usd, price, liq)
    save_state()
    return jsonify(res)


@app.route("/api/close", methods=["POST"])
@_dash_auth
def api_close():
    data   = flask_request.get_json() or {}
    symbol = data.get("symbol", "").upper().strip()
    pos    = STATE["positions"].get(symbol)
    if not symbol or not pos or pos.get("units", 0) <= 0:
        return jsonify({"error": "position not found"}), 404
    px    = fetch_positions_prices().get(symbol, _px_dict(pos.get("avg", 1.0) * 1.02, pos.get("entry_liq", 100000.0)))
    price = px["price"]
    liq   = px["liq"]
    res   = exec_sell(symbol, pos["usd"], price, liq)
    save_state()
    return jsonify(res)


@app.route("/api/position/tp", methods=["POST"])
@_dash_auth
def api_position_tp():
    data   = flask_request.get_json() or {}
    symbol = data.get("symbol", "").upper().strip()
    tp     = data.get("tp")
    pos    = STATE["positions"].get(symbol)
    if not pos:
        return jsonify({"error": "position not found"}), 404
    if tp is None:
        pos.pop("tp_override", None)
    else:
        pos["tp_override"] = [float(x) for x in tp]
    save_state()
    return jsonify({"ok": True})


@app.route("/api/position/sl", methods=["POST"])
@_dash_auth
def api_position_sl():
    data   = flask_request.get_json() or {}
    symbol = data.get("symbol", "").upper().strip()
    sl     = data.get("sl")
    pos    = STATE["positions"].get(symbol)
    if not pos:
        return jsonify({"error": "position not found"}), 404
    if sl is None:
        pos.pop("sl_override", None)
    else:
        pos["sl_override"] = float(sl)
    save_state()
    return jsonify({"ok": True})


@app.route("/api/watchlist/add", methods=["POST"])
@_dash_auth
def api_watchlist_add():
    data    = flask_request.get_json() or {}
    symbol  = data.get("symbol", "").upper().strip()
    address = data.get("address", "").strip()
    if not symbol or not address:
        return jsonify({"error": "symbol and address required"}), 400
    CONFIG["oldcoin"]["watchlist"][symbol] = address
    return jsonify({"ok": True})


@app.route("/api/watchlist/remove", methods=["POST"])
@_dash_auth
def api_watchlist_remove():
    CONFIG["oldcoin"]["watchlist"].pop(
        (flask_request.get_json() or {}).get("symbol", "").upper().strip(), None)
    return jsonify({"ok": True})


@app.route("/api/config/oldcoin", methods=["POST"])
@_dash_auth
def api_config_oldcoin():
    data = flask_request.get_json() or {}
    for key in ("volume_x", "mentions_x", "tiny_entry_usd"):
        if key in data:
            CONFIG["oldcoin"][key] = float(data[key])
    if "auto_join" in data:
        CONFIG["oldcoin"]["auto_join"] = bool(data["auto_join"])
    return jsonify({"ok": True})


@app.route("/api/skim", methods=["POST"])
@_dash_auth
def api_skim():
    enabled = bool((flask_request.get_json() or {}).get("enabled", False))
    STATE.setdefault("skim", {})["enabled"] = enabled
    save_state()
    return jsonify({"ok": True, "skim_enabled": enabled})


@app.route("/api/doge", methods=["POST"])
@_dash_auth
def api_doge():
    """Tune the DOGE trusted-coin bag: core_units, exit_band [lo,hi], floor."""
    data = flask_request.get_json() or {}
    spec = TRUSTED.setdefault("DOGE", {})
    if "core_units" in data:
        spec["core_units"] = float(data["core_units"])
    if "exit_band" in data and isinstance(data["exit_band"], list) and len(data["exit_band"]) == 2:
        lo, hi = float(data["exit_band"][0]), float(data["exit_band"][1])
        if 0 < lo < hi:
            spec["exit_band"] = [lo, hi]
        else:
            return jsonify({"error": "exit_band must be 0 < lo < hi"}), 400
    if "floor" in data:
        spec["floor"] = float(data["floor"])
    return jsonify({"ok": True, "doge": spec})


@app.route("/api/config", methods=["POST"])
@_dash_auth
def api_config():
    """Generic tuning: presale_min_score, gas_sim, skim_pct, reentry params."""
    data = flask_request.get_json() or {}
    if "presale_min_score" in data:
        CONFIG["presale_min_score"] = int(data["presale_min_score"])
    if "gas_sim" in data:
        CONFIG["gas_sim"] = bool(data["gas_sim"])
    if "gas_dynamic" in data:
        CONFIG["gas_dynamic"] = bool(data["gas_dynamic"])
    if "skim_pct" in data:
        CONFIG["skim_pct"] = max(0.0, min(1.0, float(data["skim_pct"])))
    if isinstance(data.get("reentry"), dict):
        for k in ("enabled", "cooldown_min", "stable_ticks", "stable_range_pct",
                  "liq_floor_pct", "vol_min_h1", "skip_hard_exits", "max_per_day"):
            if k in data["reentry"]:
                CONFIG["moonshot"]["reentry"][k] = data["reentry"][k]
    if isinstance(data.get("safety"), dict):
        for k in ("reddit_enabled", "x_enabled", "onchain_enabled"):
            if k in data["safety"]:
                CONFIG["safety"][k] = bool(data["safety"][k])
        for k in ("scam_chatter_max", "sol_holder_max_pct", "evm_sell_tax_max", "rugcheck_score_max"):
            if k in data["safety"]:
                CONFIG["safety"][k] = float(data["safety"][k])
    if isinstance(data.get("degen_terms"), dict):
        if "enabled" in data["degen_terms"]:
            CONFIG["degen_terms"]["enabled"] = bool(data["degen_terms"]["enabled"])
        if "bonus" in data["degen_terms"]:
            CONFIG["degen_terms"]["bonus"] = max(0, int(data["degen_terms"]["bonus"]))
    if isinstance(data.get("stall_exit"), dict):
        if "enabled" in data["stall_exit"]:
            CONFIG["stall_exit"]["enabled"] = bool(data["stall_exit"]["enabled"])
        for k in ("min_gain", "give_back"):
            if k in data["stall_exit"]:
                CONFIG["stall_exit"][k] = max(0.0, float(data["stall_exit"][k]))
        if "stall_sec" in data["stall_exit"]:
            CONFIG["stall_exit"]["stall_sec"] = max(30, int(data["stall_exit"]["stall_sec"]))
    if "buy_ratio_min" in data:
        CONFIG["moonshot"]["buy_ratio_min"] = max(0.0, min(1.0, float(data["buy_ratio_min"])))
    if "max_open_positions" in data:
        CONFIG["max_open_positions"] = max(1, int(data["max_open_positions"]))
    if "blacklist" in data and isinstance(data["blacklist"], list):
        CONFIG["blacklist"] = [str(x).strip() for x in data["blacklist"] if str(x).strip()]
    return jsonify({"ok": True})


@app.route("/api/export", methods=["GET"])
@_dash_auth
def api_export():
    return jsonify({
        "state":                STATE,
        "config_mode":          CONFIG["mode"],
        "config_moonshot_mode": CONFIG["moonshot"]["mode"],
        "config_objective":     CONFIG["objective"],
        "shadow_mode":          SHADOW_MODE,
    })


@app.route("/api/import", methods=["POST"])
@_dash_auth
def api_import():
    """Shallow-merge a JSON patch into STATE (same as /import_state in Telegram)."""
    data = flask_request.get_json() or {}
    patch = data.get("state", data)   # accept either {state:{...}} or a bare patch
    if not isinstance(patch, dict):
        return jsonify({"error": "expected a JSON object"}), 400
    STATE.update(patch)
    save_state()
    return jsonify({"ok": True, "keys": list(patch.keys())})


@app.route("/api/ai", methods=["POST"])
@_dash_auth
def api_ai_config():
    """Configure the AI auto-pilot: enabled, auto_apply, interval_min, min_confidence."""
    data = flask_request.get_json() or {}
    for k in ("enabled", "auto_apply"):
        if k in data:
            CONFIG["ai"][k] = bool(data[k])
    if "interval_min" in data:
        CONFIG["ai"]["interval_min"] = max(1, int(data["interval_min"]))
    if "min_confidence" in data:
        CONFIG["ai"]["min_confidence"] = max(0.0, min(1.0, float(data["min_confidence"])))
    return jsonify({"ok": True, "ai": CONFIG["ai"], "key_set": bool(os.getenv(ANTHROPIC_KEY_ENV))})


@app.route("/api/ai/run", methods=["POST"])
@_dash_auth
def api_ai_run():
    """Consult the AI right now (advisory or auto-apply per config). Returns the decision."""
    decision = ai_advise(force=True)
    if decision is None:
        return jsonify({"error": "AI unavailable — check ANTHROPIC_API_KEY and that `anthropic` is installed"}), 400
    return jsonify({"ok": True, "decision": decision})


@app.route("/api/restart", methods=["POST"])
@_dash_auth
def api_restart():
    """Flush state and exit; Docker (restart=unless-stopped) brings it back."""
    save_state()
    def _reboot():
        time.sleep(0.3)
        os._exit(0)
    threading.Thread(target=_reboot, daemon=True).start()
    return jsonify({"ok": True, "restarting": True})

# ---------------------------------------------------------------------------
# Alerts + logging
# ---------------------------------------------------------------------------
TG_STATE: Dict[str, Any] = {"bot": None, "loop": None}


def log(msg: str):
    print(f"[{now_utc().isoformat()}] {msg}")


def emit_heartbeat():
    try:
        import boto3  # type: ignore
        cw = boto3.client("cloudwatch", region_name=os.getenv("AWS_REGION", "us-east-1"))
        cw.put_metric_data(Namespace="CryptoBot", MetricData=[{
            "MetricName": "Heartbeat", "Value": 1, "Unit": "Count",
            "Dimensions": [{"Name": "Tenant", "Value": CONFIG["tenant"]["name"]}],
        }])
    except Exception:
        pass


def send_alert(msg: str, critical: bool = False):
    """Send a proactive message to the owner chat. Non-critical msgs respect quiet hours."""
    ALERT_LOG.appendleft({"ts": now_utc().isoformat(), "msg": msg, "critical": critical})
    if not TG_STATE.get("bot") or not TG_STATE.get("loop"):
        return
    chat_id = STATE.get("telegram", {}).get("owner_chat_id")
    if not chat_id:
        return
    if not critical:
        hour = now_utc().hour
        q = CONFIG["telegram"]["quiet_hours_utc"]
        if q[0] <= hour < q[1]:
            return

    async def _send():
        try:
            await TG_STATE["bot"].send_message(chat_id=chat_id, text=msg)
        except Exception:
            pass

    asyncio.run_coroutine_threadsafe(_send(), TG_STATE["loop"])


def send_digest():
    pos_lines = [
        f"  {s} {p['chain']} usd~${p['usd']:.2f} avg~{p['avg']:.6f}"
        for s, p in STATE["positions"].items() if p.get("units", 0) > 0
    ]
    recent_pnl = sum(STATE["pnl_hist"][-20:])
    msg = (
        f"📊 Daily digest\n"
        f"Vault: ${STATE['vault_usd']:.2f}  deployable: ~${deployable_now():.2f}\n"
        f"Deployed today: ${STATE['open_today_usd']:.2f}\n"
        f"PnL (last 20 trades): ${recent_pnl:.2f}\n"
        f"btc.d={STATE['signals']['btc_d']}  heat={STATE['signals']['heat']}\n"
        f"mode={CONFIG['mode']}  shadow={'ON' if SHADOW_MODE else 'OFF'}\n"
        f"Positions ({len(pos_lines)}):\n" + ("\n".join(pos_lines) if pos_lines else "  none")
    )
    send_alert(msg, critical=True)


def tg_allowed(user_id: int) -> bool:
    al = CONFIG["tenant"]["allowlist"]
    return (not al) or (str(user_id) in al)


def require_auth(fn):
    @wraps(fn)
    async def w(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update and update.effective_user and not tg_allowed(update.effective_user.id):
            return
        return await fn(update, context)
    return w

# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------
async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"your id: {update.effective_user.id}")


@require_auth
async def cmd_version(update, context):
    await update.message.reply_text(
        f"build {BUILD['sha']} | shadow={'ON' if SHADOW_MODE else 'OFF'} | mode={CONFIG['mode']}"
    )


@require_auth
async def cmd_restart(update, context):
    if not (context.args and context.args[0].lower() == "now"):
        await update.message.reply_text("Confirm with: /restart now")
        return
    save_state()
    await update.message.reply_text("State flushed. Restarting…")
    def _reboot():
        time.sleep(0.5)
        os._exit(0)
    threading.Thread(target=_reboot, daemon=True).start()


@require_auth
async def cmd_shadow(update, context):
    arg = context.args[0].lower() if context.args else "status"
    if arg in ("on", "true", "1"):
        set_shadow_mode(True)
        await update.message.reply_text("Shadow mode: ON (paper trading).")
    elif arg in ("off", "false", "0"):
        set_shadow_mode(False)
        await update.message.reply_text(
            "Shadow mode: OFF — live execution active (Jupiter / Uniswap + 1inch)."
        )
    else:
        await update.message.reply_text(
            f"Shadow mode is {'ON' if SHADOW_MODE else 'OFF'}. Usage: /shadow on|off"
        )


@require_auth
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    open_pos = {s: p for s, p in STATE["positions"].items() if p.get("units", 0) > 0}
    prices   = fetch_positions_prices() if open_pos else {}
    pos_lines = []
    for s, p in open_pos.items():
        cur = prices.get(s, {}).get("price") or p["avg"]
        pnl = (p["units"] * cur - p["usd"])
        pnl_sign = "+" if pnl >= 0 else ""
        pos_lines.append(f"  {s} ({p['chain']}) cost=${p['usd']:.0f} cur={cur:.6f} pnl={pnl_sign}${pnl:.2f}")
    rewatch = len(STATE.get("reentry_watch", {}))
    msg = (
        f"Shadow: {'ON' if SHADOW_MODE else 'OFF'}  mode: {CONFIG['mode']}\n"
        f"Vault: ${STATE['vault_usd']:.2f}  deployable: ~${deployable_now():.2f}\n"
        f"Open today: ${STATE['open_today_usd']:.2f}  income: ${STATE.get('income_usd', 0):.2f}\n"
        f"btc.d={STATE['signals']['btc_d']}  heat={STATE['signals']['heat']}\n"
        f"Objective: {CONFIG['objective']['kind']}  brake: {'ON' if drawdown_brake_active() else 'off'}\n"
        + (f"Re-entry watching: {rewatch} tokens\n" if rewatch else "")
        + f"Positions ({len(open_pos)}):\n"
        + ("\n".join(pos_lines) if pos_lines else "  (none)")
    )
    await update.message.reply_text(msg)


@require_auth
async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    valid = list(CONFIG["modes"].keys())
    if not context.args or context.args[0].lower() not in valid:
        await update.message.reply_text(f"Usage: /mode <{'|'.join(valid)}>\nCurrent: {CONFIG['mode']}")
        return
    m = context.args[0].lower()
    CONFIG["mode"] = m
    await update.message.reply_text(f"Mode set to {m}")


@require_auth
async def cmd_objective(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        kind = context.args[0]
        if kind == "off":
            CONFIG["objective"]["kind"] = "off"
            await update.message.reply_text("Objective off")
            return
        tgt = float(context.args[1])
        weeks = int(context.args[2])
        start_objective(tgt, weeks)
        await update.message.reply_text(f"Objective: +${tgt} in {weeks} weeks")
    except Exception:
        await update.message.reply_text("Usage: /objective off | /objective target <usd> <weeks>")


@require_auth
async def cmd_moonshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        m = context.args[0].lower()
        assert m in ("enter", "suggest")
        CONFIG["moonshot"]["mode"] = m
        await update.message.reply_text(f"Moonshot mode: {m}")
    except Exception:
        await update.message.reply_text("Usage: /moonshot <enter|suggest>")


@require_auth
async def cmd_auto_old(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        CONFIG["oldcoin"]["auto_join"] = (context.args[0].lower() == "on")
        await update.message.reply_text(f"Auto-join old pumps: {CONFIG['oldcoin']['auto_join']}")
    except Exception:
        await update.message.reply_text("Usage: /auto_old <on|off>")


@require_auth
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    STATE.setdefault("telegram", {})["owner_chat_id"] = cid
    save_state()
    await update.message.reply_text(f"Owner chat linked (id {cid}). Proactive alerts will be sent here.")


@require_auth
async def cmd_skim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        val = context.args[0].lower()
        assert val in ("on", "off")
        STATE.setdefault("skim", {})["enabled"] = (val == "on")
        save_state()
        await update.message.reply_text(f"Profit skimming: {'ON' if val == 'on' else 'OFF'}")
    except Exception:
        cur = STATE.get("skim", {}).get("enabled", False)
        await update.message.reply_text(f"Skim is {'ON' if cur else 'OFF'}. Usage: /skim on|off")


@require_auth
async def cmd_spray_until(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        date_str = context.args[0] if context.args else ""
        if date_str.lower() in ("off", "stop", ""):
            STATE["spray_until"] = None
            save_state()
            await update.message.reply_text("Spray mode off — normal entry filters restored.")
            return
        datetime.fromisoformat(date_str)   # validate format
        STATE["spray_until"] = date_str
        save_state()
        await update.message.reply_text(f"Spray mode until {date_str}: looser liq + hype filters.")
    except Exception:
        cur = STATE.get("spray_until") or "off"
        await update.message.reply_text(f"Spray: {cur}\nUsage: /spray_until YYYY-MM-DD | off")


@require_auth
async def cmd_boost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        mult  = float(context.args[0])
        hours = float(context.args[1]) if len(context.args) > 1 else 1.0
        if mult <= 0:
            raise ValueError
        expires = (now_utc() + timedelta(hours=hours)).isoformat()
        STATE["boost"] = {"mult": mult, "expires": expires}
        save_state()
        await update.message.reply_text(f"Boost {mult}× active for {hours}h (expires {expires[:16]} UTC)")
    except Exception:
        b = STATE.get("boost", {})
        cur = f"{b.get('mult', 1.0)}× expires {(b.get('expires') or 'N/A')[:16]}" if b.get("mult", 1.0) != 1.0 else "off"
        await update.message.reply_text(f"Boost: {cur}\nUsage: /boost <mult> [hours=1]")


@require_auth
async def cmd_export_state(update: Update, context: ContextTypes.DEFAULT_TYPE):
    export = {"state": STATE, "config_mode": CONFIG["mode"], "config_moonshot_mode": CONFIG["moonshot"]["mode"],
              "config_objective": CONFIG["objective"], "shadow_mode": SHADOW_MODE}
    raw  = json.dumps(export, indent=2, default=str)
    buf  = io.BytesIO(raw.encode())
    buf.name = f"state_{CONFIG['tenant']['name']}.json"
    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=buf,
        filename=buf.name,
        caption=(f"Export — {len(STATE.get('positions', {}))} positions, "
                 f"vault ${STATE.get('vault_usd', 0):.2f}, mode={CONFIG['mode']}"),
    )


@require_auth
async def cmd_import_state(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Paste compact JSON as a single arg. To restore from file, replace the state JSON file directly.\n"
            "Usage: /import_state {\"vault_usd\": 500}"
        )
        return
    try:
        patch = json.loads(" ".join(context.args))
        if not isinstance(patch, dict):
            raise ValueError("expected a JSON object")
        STATE.update(patch)
        save_state()
        await update.message.reply_text(f"State patched with {len(patch)} keys. Use /status to verify.")
    except Exception as e:
        await update.message.reply_text(f"Parse error: {e}")


@require_auth
async def cmd_doge_core(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        units = float(context.args[0])
        assert units >= 0
        TRUSTED["DOGE"]["core_units"] = units
        await update.message.reply_text(f"DOGE core bag set to {units:,.0f} units")
    except Exception:
        cur = TRUSTED.get("DOGE", {}).get("core_units", "?")
        await update.message.reply_text(f"DOGE core is {cur}. Usage: /doge_core <units>")


@require_auth
async def cmd_doge_band(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        lo = float(context.args[0])
        hi = float(context.args[1])
        assert 0 < lo < hi
        TRUSTED["DOGE"]["exit_band"] = [lo, hi]
        await update.message.reply_text(f"DOGE exit band set to [{lo}, {hi}]")
    except Exception:
        band = TRUSTED.get("DOGE", {}).get("exit_band", [None, None])
        await update.message.reply_text(f"DOGE band is {band}. Usage: /doge_band <min> <max>")


@require_auth
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/status /mode /objective /moonshot /auto_old\n"
        "/skim /spray_until /boost /export_state /import_state\n"
        "/doge_core /doge_band\n"
        "/buy /sell /shadow /version /restart\n"
        "/start — link owner chat for alerts\n"
        "/whoami — get your Telegram user ID\n"
        "/help_long — full command reference"
    )


@require_auth
async def cmd_help_long(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Modes: safe / default / hype / degen\n\n"
        "/objective target <usd> <weeks>  — set a profit target\n"
        "/objective off                   — clear objective\n"
        "/moonshot enter|suggest          — auto-enter or alert on new launches\n"
        "/auto_old on|off                 — tiny auto-join on old-coin pumps\n"
        "/skim on|off                     — skim realized PnL to income vault\n"
        "/spray_until YYYY-MM-DD|off      — broaden entry filters until date\n"
        "/boost <mult> [hours]            — temporary size multiplier\n"
        "/export_state                    — dump current state as JSON\n"
        "/import_state {key: val}         — patch state with inline JSON\n"
        "/doge_core <units>               — set DOGE core bag target\n"
        "/doge_band <min> <max>           — set DOGE trim exit band\n"
        "/buy SYMBOL USD [chain] [price]  — manual buy\n"
        "/sell SYMBOL <USD|%|all> [price] — manual sell\n"
        "/shadow on|off                   — toggle paper trading\n"
        "/start                           — link this chat for proactive alerts\n"
        "/version                         — build info\n"
        "/restart now                     — restart process"
    )


@require_auth
async def cmd_buy(update, context):
    try:
        symbol = context.args[0].upper()
        usd    = float(context.args[1])
        chain  = (context.args[2] if len(context.args) > 2 else "sol").lower()
        price  = float(context.args[3]) if len(context.args) > 3 else 1.0
        liq    = float(context.args[4]) if len(context.args) > 4 else 100000.0
        if usd <= 0:
            raise ValueError
        res = exec_buy(symbol, chain, usd, price, liq)
        if "error" in res:
            await update.message.reply_text(f"Live trade blocked: {res['error']}")
            return
        await update.message.reply_text(
            f"{'BOUGHT' if SHADOW_MODE else 'LIVE BUY'} {symbol} ${usd:.2f} on {chain} "
            f"@~{res['price']:.6f} units={res['units']:.4f}"
        )
    except Exception:
        await update.message.reply_text("Usage: /buy SYMBOL USD [chain] [price] [liq]")


def _parse_sell_amount(pos: Dict, token: str) -> float:
    t = token.strip().lower()
    if t in ("all", "max", "100%"):
        return pos["usd"]
    if t.endswith("%"):
        pct = float(t[:-1]) / 100.0
        return max(0.0, min(pos["usd"], pos["usd"] * pct))
    return float(t)


@require_auth
async def cmd_sell(update, context):
    try:
        symbol        = context.args[0].upper()
        amt_token     = context.args[1]
        default_price = STATE["positions"].get(symbol, {}).get("avg", 0.0) * 1.02 or 1.0
        price = float(context.args[2]) if len(context.args) > 2 else default_price
        liq   = float(context.args[3]) if len(context.args) > 3 else 100000.0
        pos   = STATE["positions"].get(symbol)
        if not pos:
            await update.message.reply_text("No position.")
            return
        usd = _parse_sell_amount(pos, amt_token)
        res = exec_sell(symbol, usd, price, liq)
        if "error" in res:
            await update.message.reply_text(f"Live trade blocked: {res['error']}")
            return
        await update.message.reply_text(
            f"{'SOLD' if SHADOW_MODE else 'LIVE SELL'} {symbol} ${usd:.2f} "
            f"@~{price:.6f} realized pnl ${res['pnl']:.2f}"
        )
    except Exception:
        await update.message.reply_text("Usage: /sell SYMBOL <USD|%|all> [price] [liq]")

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
ENGINE_STOP = False


def scan_candidates():
    """Slow loop: find new tokens, apply filters/safety, enter. Plus re-entry watch + trusted coins."""
    # Daily reset at the top of the first tick after midnight
    today = now_utc().date().isoformat()
    if STATE.get("last_daily_reset") != today:
        STATE["open_today_usd"]  = 0.0
        STATE["entries_today"]   = {}
        STATE["peak_deployed_usd"] = 0.0   # reset the "most on the table at once" for the new day
        STATE["peak_open_count"]   = 0
        STATE["last_daily_reset"] = today
        save_state()
        log("Daily reset: open_today_usd → 0, entries_today cleared, capital peaks reset")

    btc_d                     = fetch_btc_dominance()
    STATE["signals"]["btc_d"] = btc_d
    STATE["signals"]["heat"]  = compute_heat(btc_d)

    candidates = fetch_new_candidates()
    if CONFIG["stealth"]["candidate_shuffle"]:
        random.shuffle(candidates)

    for c in candidates:
        symbol = c["symbol"]
        chain  = c["chain"]
        price  = c["price"]
        liq    = c["liq"]
        sc     = Score(c.get("hype", 0), liq, c["age_min"], c.get("positive", True),
                       buy_ratio=c.get("buy_ratio"))
        is_new = c["age_min"] <= CONFIG["scan"]["new_max_age_min"]

        addr = c.get("address", "")
        link = _dex_link(chain, addr)
        # Manual blacklist — never buy these (by symbol or address), dashboard-editable
        bl = {str(x).lower() for x in CONFIG.get("blacklist", [])}
        if symbol.lower() in bl or (addr and addr.lower() in bl):
            _scout(symbol, chain, "rejected", "on manual blacklist", sc, addr)
            continue
        # Correlation guardrail — don't pile into too many positions at once (memecoins
        # dump together in a market-wide flush). Only blocks NEW symbols, not adds.
        # Count only LIVE positions (units>0) — closed shells must not jam the cap.
        open_count = sum(1 for q in STATE["positions"].values() if q.get("units", 0) > 0)
        if (symbol not in STATE["positions"]
                and open_count >= CONFIG.get("max_open_positions", 12)):
            _scout(symbol, chain, "rejected",
                   f"at max open positions ({CONFIG.get('max_open_positions', 12)})", sc, addr)
            continue
        if is_new:
            reject = moonshot_reject_reason(sc)
            if reject:
                _scout(symbol, chain, "rejected", reject, sc, addr)
                continue
            # Presale safety gate — for very new tokens score for audit/social signals
            ps_meta = {
                "audit":     False,
                "kyc":       False,
                "lock_days": 0,
                "mentions":  fetch_social_volume(symbol) * 5000,
            }
            ps = presale_score(ps_meta)
            ps_threshold = CONFIG.get("presale_min_score", 10)
            if ps < ps_threshold:
                log(f"PRESALE GATE {symbol}: score {ps} < {ps_threshold}, suggest only")
                send_alert(
                    f"⚠️ SKIPPED {symbol} ({chain}) — a brand-new token with no social buzz or "
                    f"audit yet (safety score {ps}/100). Too unproven to buy; just watching.\n{link}")
                _scout(symbol, chain, "rejected", f"presale score {ps} < {ps_threshold} (no social/audit signal)", sc, addr)
                continue
            # Scam / rug safety gate — Reddit + X chatter + on-chain rug checks
            safe, why = safety_gate(symbol, addr, chain)
            if not safe:
                log(f"SAFETY GATE {symbol}: {why}")
                send_alert(
                    f"🛡️ SKIPPED {symbol} ({chain}) — {why}. The bot steered clear to protect you "
                    f"from a likely scam/rug.\n{link}")
                _scout(symbol, chain, "rejected", f"safety: {why}", sc, addr)
                continue
            # Anti-churn: don't keep re-buying the same hyper-volatile token all day.
            # Per-mode cap (degen cycles the few liquid tokens faster) → global fallback.
            entry_cap = CONFIG["modes"].get(CONFIG["mode"], {}).get(
                "max_entries_per_token_day", CONFIG.get("max_entries_per_token_day", 4))
            if STATE.setdefault("entries_today", {}).get(symbol, 0) >= entry_cap:
                _scout(symbol, chain, "rejected",
                       f"already entered {entry_cap}x today (anti-churn on volatile token)", sc, addr)
                continue
            if CONFIG["moonshot"]["mode"] == "enter":
                usd = size_ticket_usd(chain, hype=sc.hype, buy_ratio=sc.buy_ratio)
                if usd >= CONFIG["moonshot"]["min_ticket_usd"] and est_price_impact(usd, liq) <= CONFIG["moonshot"]["price_impact_max"]:
                    shadow_buy(symbol, chain, usd, price, liq, addr)
                    STATE["entries_today"][symbol] = STATE["entries_today"].get(symbol, 0) + 1
                    _record({"ev": "entry", "symbol": symbol, "chain": chain, "address": addr,
                             "price": price, "liq": liq, "hype": sc.hype,
                             "buy_ratio": sc.buy_ratio, "usd": usd})
                    log(f"ENTER {symbol} new launch ${usd:.2f} (presale score {ps})")
                    send_alert(
                        f"🚀 BOUGHT {symbol} ({chain}) — a new token that passed every safety filter "
                        f"(liquidity, hype, age). Put in ${usd:.0f} of paper money @ {price:.6f}. "
                        f"Plan: take profit as it climbs, auto-sell if it drops.\n{link}")
                    _scout(symbol, chain, "entered", f"passed filters, ps {ps}, sized ${usd:.0f}", sc, addr)
                else:
                    # "Suggested" = passed every filter but no daily budget / too thin to size.
                    # Not actionable, and there can be 100+/day — log to the Scout tab but
                    # don't spam Telegram. Raising the daily cap converts these into buys.
                    log(f"SUGGEST {symbol} new launch (caps/impact)")
                    _scout(symbol, chain, "suggested", f"passed filters but blocked by caps/budget (ps {ps})", sc, addr)
            else:
                log(f"SUGGEST {symbol} new launch (mode=suggest) [ps:{ps}]")
                send_alert(
                    f"👀 SPOTTED {symbol} ({chain}) — a new launch that passed the safety filters. "
                    f"The bot is in 'suggest' mode, so it's flagging it for you instead of auto-buying.\n{link}")
                _scout(symbol, chain, "suggested", f"passed filters, ps {ps} (moonshot mode = suggest)", sc, addr)
        else:
            if detect_oldcoin_pump(symbol, CONFIG["oldcoin"]["volume_x"], CONFIG["oldcoin"]["mentions_x"]):
                if CONFIG["oldcoin"]["auto_join"]:
                    usd = min(CONFIG["oldcoin"]["tiny_entry_usd"], size_ticket_usd(chain))
                    if usd >= CONFIG["moonshot"]["min_ticket_usd"]:
                        shadow_buy(symbol, chain, usd, price, liq, addr)
                        log(f"AUTO-JOIN tiny {symbol} ${usd:.2f}")
                        send_alert(
                            f"⚡ BOUGHT a tiny ${usd:.0f} of {symbol} ({chain}) — an older coin whose "
                            f"trading volume just spiked (a 'pump'). Small bet to ride the momentum.\n{link}")
                    else:
                        log(f"ALERT old pump {symbol} (cap too small)")
                        send_alert(
                            f"⚡ {symbol} ({chain}) is pumping (volume spiking) but your caps are full, "
                            f"so the bot can't join. Heads-up only.\n{link}")
                else:
                    log(f"ALERT old pump {symbol}")
                    send_alert(
                        f"⚡ PUMP: {symbol} ({chain}) — an older coin with a sudden volume spike. "
                        f"The bot is alerting you, not buying (auto-join is off).\n{link}")

    # Watchlist — coins not surfaced by the feed
    candidate_symbols = {c["symbol"] for c in candidates}
    for sym, addr in list(CONFIG["oldcoin"].get("watchlist", {}).items()):
        if sym in candidate_symbols:
            continue
        if not detect_oldcoin_pump(sym, CONFIG["oldcoin"]["volume_x"], CONFIG["oldcoin"]["mentions_x"]):
            continue
        if CONFIG["oldcoin"]["auto_join"] and sym not in STATE["positions"]:
            data = fetch_dexscreener_token(addr)
            w_price, w_liq, w_chain = 1.0, 100000.0, "sol"
            if data and data.get("pairs"):
                p0      = data["pairs"][0]
                w_price = float(p0.get("priceUsd") or 1.0)
                w_liq   = float((p0.get("liquidity") or {}).get("usd") or 100000.0)
                cid     = p0.get("chainId", "solana")
                w_chain = next((k for k, v in CHAIN_IDS.items() if v == cid), "sol")
            usd = min(CONFIG["oldcoin"]["tiny_entry_usd"], size_ticket_usd(w_chain))
            if usd >= CONFIG["moonshot"]["min_ticket_usd"]:
                shadow_buy(sym, w_chain, usd, w_price, w_liq, addr)
                log(f"AUTO-JOIN watchlist {sym} ${usd:.2f}")
                send_alert(
                    f"⚡ BOUGHT a tiny ${usd:.0f} of {sym} ({w_chain}) — a coin on your watchlist just "
                    f"spiked in volume. Small bet to ride it.\n{_dex_link(w_chain, addr)}")
            else:
                log(f"ALERT watchlist pump {sym} (cap too small)")
                send_alert(
                    f"⚡ {sym} (your watchlist) is pumping but your caps are full — heads-up only.\n{_dex_link(w_chain, addr)}")
        else:
            log(f"ALERT watchlist pump {sym}")
            send_alert(
                f"⚡ PUMP: {sym} (your watchlist) — a sudden volume spike. Alerting you, not buying.\n{_dex_link('sol', addr)}")

    # Re-entry watch + trusted-coin management run on the slow cadence (not time-critical)
    check_reentry_watch()
    manage_trusted_coins()


def manage_positions():
    """Fast loop: live prices, rug guard, velocity/trailing exits, TP/SL, autoscale.
    Run every few seconds so the bot can actually SEE and react to quick pumps/dips
    instead of being blind between candidate scans."""
    # Position management — live prices, rug guard, TP/SL, no-pump, autoscale
    brake_now = drawdown_brake_active()
    if brake_now and not STATE.get("brake_alerted"):
        send_alert(
            "🛑 SAFETY BRAKE ON — the bot hit a losing streak, so it's pausing new trades for the rest "
            "of the day and shrinking bet sizes. This protects your vault from a bad run.", critical=True)
        STATE["brake_alerted"] = True
    elif not brake_now:
        STATE["brake_alerted"] = False

    live_prices = fetch_positions_prices()
    MS          = CONFIG["moonshot"]
    for s, p in list(STATE["positions"].items()):
        if p.get("units", 0) <= 0:
            continue
        px        = live_prices.get(s, _px_dict(p.get("avg", 1.0) * 1.02))
        price     = px["price"]
        liq       = px["liq"]
        vol_h1    = px["vol_h1"]
        change_m5 = px["change_m5"]
        entry_ts  = datetime.fromisoformat(p.get("time", now_utc().isoformat())).timestamp()
        _record_tick(s, p, px)   # forward recorder: real per-tick price/liq for backtests

        # Track peak price (+ when it was last set, for stall detection)
        if price > p.get("peak_price", 0):
            p["peak_price"] = price
            p["peak_ts"]    = time.time()
        peak = p.get("peak_price", p["avg"])

        # Rolling liq history for drain detection
        liq_ticks: List[float] = p.setdefault("liq_ticks", [])
        liq_ticks.append(liq)
        if len(liq_ticks) > MS["liq_drain_ticks"] + 1:
            liq_ticks[:] = liq_ticks[-(MS["liq_drain_ticks"] + 1):]

        # Seed entry_vol_h1 on first tick with real data
        if vol_h1 > 0 and not p.get("entry_vol_h1"):
            p["entry_vol_h1"] = vol_h1

        # Build the take-profit ladder for the active mode. A per-position override (list of
        # thresholds) keeps the old 50%-per-rung behavior; a mode tp_ladder ([gain, fraction])
        # adds a moon bag = the unsold remainder, which rides a wide trailing stop.
        # Use the mode this position was ENTERED under (locked at buy), so an AI
        # mode-switch mid-trade can't retroactively change its stop-loss/TP.
        entry_mode = p.get("entry_mode", CONFIG["mode"])
        mode_cfg = CONFIG["modes"].get(entry_mode, CONFIG["modes"][CONFIG["mode"]])
        if p.get("tp_override"):
            ladder = [[t, 0.5] for t in p["tp_override"]]
        else:
            ladder = mode_cfg.get("tp_ladder") or [[t, 0.5] for t in mode_cfg.get("tp", MS["tp"])]
        moonbag_frac = max(0.0, 1.0 - sum(f for _, f in ladder))
        in_moonbag   = p.get("tp_index", 0) >= len(ladder) and moonbag_frac > 0
        trail_pct    = (mode_cfg.get("moonbag_trail_pct", MS["trailing_stop_pct"])
                        if in_moonbag else MS["trailing_stop_pct"])
        # Adaptive trailing stop: tighten as unrealized gain grows so we keep more of
        # the big runners (a flat 45% trail gives back 45% of a +500% move).
        gain_now = (price / max(p.get("avg", 0) or 1e-9, 1e-9)) - 1
        if   gain_now >= 8.0:                    trail_pct = min(trail_pct, 0.22)
        elif gain_now >= 3.0:                    trail_pct = min(trail_pct, 0.28)
        elif gain_now >= 1.0 and not in_moonbag: trail_pct = min(trail_pct, 0.10)
        elif gain_now >= 0.5 and not in_moonbag: trail_pct = min(trail_pct, 0.12)

        # ── EXIT PRIORITY ORDER ────────────────────────────────────────────
        exit_reason: Optional[str] = None

        # 1. Instant rug guard — liq craters in one tick
        prev_liq = STATE["liq_prev"].get(s, liq)
        if liq < prev_liq * (1 - MS["rug_liq_drop"]):
            exit_reason = f"RUG liq {prev_liq:.0f}→{liq:.0f}"
        STATE["liq_prev"][s] = liq

        # 2. Velocity exit — sharp m5 drop (rug unfolding, panic sell)
        if exit_reason is None and change_m5 < -(MS["velocity_exit_pct"] * 100):
            exit_reason = f"VELOCITY {change_m5:.1f}% in 5m"

        # 3. Trailing stop — drop from peak (locks in gains, cuts reversals). Once the
        #    ladder is done, the moon bag rides a WIDE trailing stop to catch +500% runners.
        if exit_reason is None and price <= peak * (1 - trail_pct):
            pct = ((price / peak) - 1) * 100
            tag = "MOONBAG TRAIL" if in_moonbag else "TRAIL STOP"
            exit_reason = f"{tag} {pct:.1f}% from peak {peak:.6f}"

        # 3b. Stall exit — up nicely but stopped climbing. This is the fix for the
        #     account's core pain: winners that peaked then round-tripped to breakeven.
        #     Skips the moon bag (which is meant to ride). Banks a stalled gain.
        st = CONFIG.get("stall_exit", {})
        if (exit_reason is None and st.get("enabled") and not in_moonbag
                and gain_now >= st.get("min_gain", 0.20)
                and (time.time() - p.get("peak_ts", entry_ts)) >= st.get("stall_sec", 600)
                and price <= peak * (1 - st.get("give_back", 0.06))):
            exit_reason = f"STALL +{gain_now*100:.0f}% then flat {st.get('stall_sec',600)//60}m — locking it in"

        # 4. Slow liq drain — consecutive ticks all declining
        if (exit_reason is None
                and len(liq_ticks) >= MS["liq_drain_ticks"]
                and all(liq_ticks[i] < liq_ticks[i-1] * (1 - MS["liq_drain_pct"])
                        for i in range(1, len(liq_ticks)))):
            exit_reason = f"LIQ DRAIN {liq_ticks[0]:.0f}→{liq:.0f} over {len(liq_ticks)} ticks"

        if exit_reason:
            res = exec_sell(s, p["usd"], price, liq)
            pnl = res.get("pnl", 0.0)
            log(f"EXIT {s} [{exit_reason}] pnl ${pnl:.2f}")
            send_alert(
                f"🚨 SOLD {s} — {_exit_plain(exit_reason)}. Paper result: ${pnl:+.2f}.\n"
                f"{_dex_link(p.get('chain', 'sol'), p.get('address', ''))}", critical=True)
            _add_reentry_watch(s, p, liq, vol_h1, exit_reason)
            STATE["liq_prev"].pop(s, None)
            save_state()
            continue

        # 5. Volume dry-up — soft alert only (not a standalone exit)
        entry_vol = p.get("entry_vol_h1", 0)
        if vol_h1 > 0 and entry_vol > 0 and vol_h1 < entry_vol * MS["vol_dry_pct"]:
            send_alert(
                f"⚠️ {s} is going quiet — trading volume dropped to ${vol_h1:,.0f} (was ${entry_vol:,.0f} "
                f"when you bought in). Interest is fading; the bot is watching but not selling yet.")

        # 6. TP ladder — scalp a chunk at each rung, but never sell into the moon bag
        tp_hit        = False
        tp_index      = p.get("tp_index", 0)
        sl_level      = p.get("sl_override") or mode_cfg["sl"]
        deployed      = p.get("deployed_usd", p["usd"]) or p["usd"]
        moonbag_floor = moonbag_frac * deployed
        for i, (gain, frac) in enumerate(ladder):
            if i < tp_index:
                continue  # already executed this rung
            if price >= p["avg"] * (1 + gain):
                sellable = max(0.0, p["usd"] - moonbag_floor)   # keep the moon bag intact
                sell_usd = min(frac * deployed, sellable)
                p["tp_index"] = i + 1
                if sell_usd > 0.01:
                    res = exec_sell(s, sell_usd, price, liq)
                    pnl = res.get("pnl", 0.0)
                    log(f"TP {s} rung {i+1}/{len(ladder)} +{gain*100:.0f}%: sold ${sell_usd:.2f} pnl ${pnl:.2f}")
                    last = (i + 1 >= len(ladder)) and moonbag_frac > 0
                    tail = (f" Keeping a {moonbag_frac*100:.0f}% moon bag riding for a bigger run."
                            if last else " Letting the rest ride higher.")
                    send_alert(
                        f"✅ TOOK PROFIT on {s} (+{gain*100:.0f}%) — banked ${pnl:+.2f} on {frac*100:.0f}% "
                        f"of the position.{tail} (Rung #{i+1}.)")
                    tp_hit = True
                save_state()
                break

        # 7. Fixed SL fallback (for slow bleeds that don't trip velocity/trail)
        if not tp_hit and p.get("units", 0) > 0 and price <= p["avg"] * (1 - sl_level):
            res = exec_sell(s, p["usd"], price, liq)
            pnl = res.get("pnl", 0.0)
            log(f"SL {s}: exit pnl ${pnl:.2f}")
            send_alert(
                f"🔻 STOP-LOSS on {s} — it slid down to your max-loss line, so the bot sold to cap the "
                f"damage. Paper result: ${pnl:+.2f}. Better a small loss than a big one.", critical=True)
            _add_reentry_watch(s, p, liq, vol_h1, "fixed_sl")
            save_state()

        # No-pump soft flag
        if p.get("units", 0) > 0 and should_exit_no_pump(entry_ts, time.time(), p["avg"], price, liq):
            send_alert(
                f"⏱ {s} has been flat since you bought it — it isn't taking off. Heads-up so you can "
                f"decide whether to cut it loose; the bot is still holding for now.")

        # Autoscale after grace
        if p.get("units", 0) > 0 and time.time() - entry_ts >= CONFIG["autoscale"]["grace_sec"]:
            autoscale_maybe(s, p["chain"], price, liq, velocity_ok=(price > p["avg"]))


def engine_once():
    """One full cycle: scan for new tokens, then manage open positions. Used by tests."""
    scan_candidates()
    manage_positions()


def engine_loop():
    last_rpc_check    = 0.0
    last_vault_refresh = 0.0
    last_gas_refresh  = 0.0
    last_ai_run       = 0.0
    digest_date       = None
    digest_hour       = int(CONFIG["telegram"]["daily_digest_utc"].split(":")[0])
    last_scan         = 0.0
    _crash_count  = 0
    _last_crash_alert = 0.0
    while not ENGINE_STOP:
        # FAST: manage open positions every cycle so we catch quick pumps/dips
        try:
            manage_positions()
            _crash_count = 0
        except Exception:
            traceback.print_exc()
            _crash_count += 1
            if _crash_count == 1 or (_crash_count % 10 == 0 and time.time() - _last_crash_alert > 300):
                send_alert(f"⚠️ manage_positions crashed (×{_crash_count}) — check logs", critical=True)
                _last_crash_alert = time.time()
        # SLOWER: scan for new candidates on its own cadence
        if time.time() - last_scan >= CONFIG["scan"]["dexscreener_poll_sec"]:
            try:
                scan_candidates()
            except Exception:
                traceback.print_exc()
            last_scan = time.time()
        now = now_utc()
        if now.hour == digest_hour and now.date() != digest_date:
            send_digest()
            digest_date = now.date()
        if time.time() - last_rpc_check > 300:
            try:
                check_rpc_health()
                emit_heartbeat()
            except Exception:
                pass
            last_rpc_check = time.time()
        if not SHADOW_MODE and time.time() - last_vault_refresh > 3600:
            try:
                refresh_vault_balance()
            except Exception:
                pass
            last_vault_refresh = time.time()
        if time.time() - last_gas_refresh > 300:
            try:
                refresh_gas_estimates()
            except Exception:
                pass
            last_gas_refresh = time.time()
        # Consult the AI on a timer, OR immediately when the drawdown brake first trips
        # (a bad streak is exactly when a mode change matters most — don't wait 12 min).
        brake_now    = drawdown_brake_active()
        brake_tripped = brake_now and not STATE.get("_brake_prev", False)
        STATE["_brake_prev"] = brake_now
        ai_due = time.time() - last_ai_run > CONFIG["ai"].get("interval_min", 12) * 60
        if CONFIG["ai"].get("enabled") and (ai_due or brake_tripped):
            try:
                ai_advise(force=brake_tripped)
            except Exception:
                pass
            last_ai_run = time.time()
        time.sleep(CONFIG["scan"].get("position_poll_sec", 3))

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
def start_flask():
    # Default to loopback for safety. In Docker set DASHBOARD_HOST=0.0.0.0 so the
    # host port-forward can reach Flask; docker-compose still binds the published
    # port to 127.0.0.1 on the host, so it is not exposed to the network.
    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("DASHBOARD_PORT", "8787"))
    threading.Thread(target=lambda: app.run(host=host, port=port), daemon=True).start()


def start_telegram():
    token = os.getenv(CONFIG["telegram"]["token_env"], "")
    allow = os.getenv(CONFIG["telegram"]["allowlist_env"], "")
    if allow:
        CONFIG["tenant"]["allowlist"] = [x.strip() for x in allow.split(",") if x.strip()]
    if not token:
        log("TELEGRAM_TOKEN not set — skipping Telegram")
        return

    async def _post_init(application):
        TG_STATE["bot"]  = application.bot
        TG_STATE["loop"] = asyncio.get_running_loop()

    tg = Application.builder().token(token).post_init(_post_init).build()
    tg.add_handler(CommandHandler("whoami",       whoami))
    tg.add_handler(CommandHandler("start",        cmd_start))
    tg.add_handler(CommandHandler("status",       cmd_status))
    tg.add_handler(CommandHandler("mode",         cmd_mode))
    tg.add_handler(CommandHandler("objective",    cmd_objective))
    tg.add_handler(CommandHandler("moonshot",     cmd_moonshot))
    tg.add_handler(CommandHandler("auto_old",     cmd_auto_old))
    tg.add_handler(CommandHandler("skim",         cmd_skim))
    tg.add_handler(CommandHandler("spray_until",  cmd_spray_until))
    tg.add_handler(CommandHandler("boost",        cmd_boost))
    tg.add_handler(CommandHandler("export_state", cmd_export_state))
    tg.add_handler(CommandHandler("import_state", cmd_import_state))
    tg.add_handler(CommandHandler("doge_core",    cmd_doge_core))
    tg.add_handler(CommandHandler("doge_band",    cmd_doge_band))
    tg.add_handler(CommandHandler("help",         cmd_help))
    tg.add_handler(CommandHandler("help_long",    cmd_help_long))
    tg.add_handler(CommandHandler("buy",          cmd_buy))
    tg.add_handler(CommandHandler("sell",         cmd_sell))
    tg.add_handler(CommandHandler("shadow",       cmd_shadow))
    tg.add_handler(CommandHandler("version",      cmd_version))
    tg.add_handler(CommandHandler("restart",      cmd_restart))
    log("Telegram polling started")
    tg.run_polling(allowed_updates=["message"], drop_pending_updates=False)


def main():
    load_state()
    global SHADOW_MODE
    if "shadow_mode" in STATE:
        SHADOW_MODE = bool(STATE["shadow_mode"])
    else:
        STATE["shadow_mode"] = SHADOW_MODE
        save_state()

    # Optional env overrides so runtime settings survive restarts.
    env_mode = os.getenv("BOT_MODE", "").strip().lower()
    if env_mode:
        if env_mode in CONFIG["modes"]:
            CONFIG["mode"] = env_mode
        else:
            log(f"WARN BOT_MODE='{env_mode}' invalid — keeping '{CONFIG['mode']}'")
    env_moon = os.getenv("MOONSHOT_MODE", "").strip().lower()
    if env_moon in ("enter", "suggest"):
        CONFIG["moonshot"]["mode"] = env_moon
    # AI auto-pilot on/off survives restarts via env
    if os.getenv("AI_ENABLED", "").strip().lower() in ("1", "true", "on", "yes"):
        CONFIG["ai"]["enabled"] = True
    if os.getenv("AI_AUTO_APPLY", "").strip().lower() in ("1", "true", "on", "yes"):
        CONFIG["ai"]["auto_apply"] = True
    # Scan cadence override (seconds between NEW-candidate scans).
    sps = os.getenv("SCAN_POLL_SEC", "").strip()
    if sps.isdigit() and int(sps) >= 3:
        CONFIG["scan"]["dexscreener_poll_sec"] = int(sps)
    # Position-monitoring cadence override (seconds) — lower = faster exit reaction.
    pps = os.getenv("POSITION_POLL_SEC", "").strip()
    if pps.isdigit() and int(pps) >= 1:
        CONFIG["scan"]["position_poll_sec"] = int(pps)

    # Validate CONFIG mode is defined
    if CONFIG["mode"] not in CONFIG["modes"]:
        raise RuntimeError(f"CONFIG mode '{CONFIG['mode']}' not in CONFIG['modes'] — check config")

    # In live mode, fail fast if wallet keys are missing
    if not SHADOW_MODE:
        sol_chains = [c for c in CONFIG["chains"] if c == "sol"]
        evm_chains = [c for c in CONFIG["chains"] if c in ("eth", "base", "bsc", "poly")]
        if sol_chains and not os.getenv(CONFIG["keys"]["solana_secret_base58_env"]):
            raise RuntimeError(
                f"{CONFIG['keys']['solana_secret_base58_env']} not set — required for live SOL trades. "
                "Set SHADOW_MODE=true or provide the key."
            )
        if evm_chains and not os.getenv(CONFIG["keys"]["evm_hex_key_env"]):
            raise RuntimeError(
                f"{CONFIG['keys']['evm_hex_key_env']} not set — required for live EVM trades. "
                "Set SHADOW_MODE=true or provide the key."
            )

    log(f"Starting (shadow={SHADOW_MODE} mode={CONFIG['mode']})")
    refresh_vault_balance()
    start_flask()
    threading.Thread(target=engine_loop, daemon=True).start()
    start_telegram()


if __name__ == "__main__":
    main()
