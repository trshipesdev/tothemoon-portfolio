#!/usr/bin/env python3
# crypto bot — shadow mode by default; see PENDING.md for live-trading TODOs

import os, io, json, time, random, asyncio, threading, traceback, base64, struct, hashlib
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

    # TODO: re-enable EVM when bot is consistently profitable on SOL first.
    # EVM data: SOL 47% WR vs EVM -$138 avg loss. Gas ($6+/trade ETH) eats small positions.
    # "chains": ["sol", "eth", "base", "bsc"],
    "chains": ["sol"],
    "scan": {
        "new_max_age_min":      120,  # pairs ≤ this age treated as NEW
        "hype_window_min":      240,
        "dexscreener_poll_sec":  8,   # how often to scan for NEW candidates
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
        # Each mode owns its full personality: entry filters AND exit thresholds.
        # Any key not set here falls back to CONFIG["moonshot"] global default.
        "safe": {
            "tp": [0.15], "sl": 0.07, "slip_bps": 60, "liq_min": 50000,
            "max_age_min": 360, "max_entries_per_token_day": 2, "size_mult": 0.7,
            # Entry: very selective
            "m5_min": 2.0, "buy_ratio_min": 0.65,
            # Exit: exit fast, protect capital
            "velocity_exit_pct": 0.05, "rug_liq_drop": 0.20, "loss_cooldown_min": 30,
        },
        "default": {
            "tp": [0.28], "sl": 0.12, "slip_bps": 100, "liq_min": 30000,
            "max_age_min": 180, "max_entries_per_token_day": 3, "size_mult": 1.0,
            # Entry: balanced
            "m5_min": 0.5, "buy_ratio_min": 0.55,
            # Exit: balanced
            "velocity_exit_pct": 0.07, "rug_liq_drop": 0.30, "loss_cooldown_min": 15,
        },
        "hype": {
            "tp": [0.45, 0.9], "sl": 0.18, "slip_bps": 150, "liq_min": 20000,
            "max_age_min": 120, "max_entries_per_token_day": 4, "size_mult": 1.3,
            # Entry: requires real momentum and majority buyers
            "m5_min": 1.0, "buy_ratio_min": 0.55,
            # Exit: fast — hype tokens dump fast when they turn
            "velocity_exit_pct": 0.06, "rug_liq_drop": 0.25, "loss_cooldown_min": 10,
        },
        "degen": {
            "tp": [0.80, 1.5], "sl": 0.28, "slip_bps": 220, "liq_min": 15000,
            "max_age_min": 120, "max_entries_per_token_day": 6, "size_mult": 1.4,
            "no_pump": {"hurdle": 0.03, "min_sec": 240, "max_sec": 900},
            "tp_ladder": [[0.30, 0.20], [0.80, 0.25], [1.50, 0.25]],
            "moonbag_trail_pct": 0.45,
            # Entry: loose — degen rides volatility, enter anything moving
            "m5_min": 0.0, "buy_ratio_min": 0.45,
            # Exit: wide — degen holds through dips to catch moonshots
            "velocity_exit_pct": 0.10, "rug_liq_drop": 0.40, "loss_cooldown_min": 5,
        },
        "seed": {
            "tp": [0.50, 1.00], "sl": 0.14, "slip_bps": 120, "liq_min": 15000,
            "max_age_min": 180, "max_entries_per_token_day": 3, "size_mult": 1.0,
            # Entry: quality focused
            "m5_min": 1.0, "buy_ratio_min": 0.60,
            # Exit: fast exits to protect small vault
            "velocity_exit_pct": 0.06, "rug_liq_drop": 0.25, "loss_cooldown_min": 15,
        },
        "micro": {
            "tp": [0.35, 0.70], "sl": 0.12, "slip_bps": 100, "liq_min": 10000,
            "max_age_min": 180, "max_entries_per_token_day": 3, "size_mult": 0.083,
            "min_ticket_usd": 3.0, "dollar_stop_usd": 1.50,
            # Entry: moderate
            "m5_min": 0.5, "buy_ratio_min": 0.55,
            # Exit: very tight — tiny tickets can't absorb big losses
            "velocity_exit_pct": 0.05, "rug_liq_drop": 0.20, "loss_cooldown_min": 20,
        },
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
    "base_size_usd":        60.0,   # raised 30→60: up to ~$100 bets with size_mult on $1k vault
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
        "liq_max":          2000000,
        "hype_min":         50,         # 0–100
        "buy_ratio_min":    0.50,       # global fallback — each mode overrides this independently
        "m5_min":           0.5,        # global fallback minimum m5% for entry — modes override
        "dollar_stop_usd":  12.0,       # hard dollar stop — exit any position down more than this
        # Gap-down safety: meme coins can crash 80-100% in a single poll window.
        # Capping position size at (dollar_stop × mult) bounds worst-case gap-down loss.
        # At mult=4 a 100% rug costs at most 4× the stop ($48 on $12 stop).
        # Raise per-wallet via the safety.dollar_stop_pos_mult field for bigger bets.
        "dollar_stop_pos_mult": 4.0,    # position hard cap = dollar_stop_usd × this
        "loss_cooldown_min": 15,        # global fallback — each mode overrides independently
        "seed_pct":          0.40,      # max fraction of available cash per bet in dynamic_sizing mode
        "dynamic_sizing":   False,      # True → bet = min(base_size, avail_cash × seed_pct) for seed/small vaults
        "price_impact_max": 0.02,
        "min_ticket_usd":   15.0,
        "adaptive_timer":   {"low_liq_sec": 300, "high_liq_sec": 1200},
        "tp":               [0.35, 0.60, 1.20],
        "sl":               0.22,
        "retries":          1,
        "rug_liq_drop":     0.35,   # global fallback — each mode sets its own (degen 0.40, hype 0.25)
        # Momentum exit params — global fallbacks, each mode overrides independently
        "trailing_stop_pct": 0.15,  # exit if price falls >15% from peak
        "velocity_exit_pct": 0.08,  # global fallback (hype/seed/micro use 0.06, degen uses 0.10)
        "velocity_2m_pct":   0.10,  # global fallback internal 2-min drop threshold
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
        "min_confidence": 0.7,     # only auto-apply at/above this confidence (raised 0.6→0.7)
        # Whitelist: AI may ONLY recommend these modes. Keeps it off custom modes with
        # tight SLs (e.g. "testing 3" at 3% SL produced 0% WR on 63 trades).
        "allowed_modes":  ["safe", "default", "hype", "degen"],
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
    "take_home_usd":    0.0,
    "take_home_log":    [],
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
    "recently_exited":  {},  # symbol → {ts, price, pnl} — blocks scanner re-entry after full close
    "custom_modes":     {},  # user-saved backtest configs {name → btParams}
    "reject_cache":     {},  # symbol → {ts, reason_type, price} — skip re-eval until price spikes
    # Wallet goal: Phase 1 = grow to $1000. Phase 2 = skim 50% of profit above $100 threshold.
    "wallet_goal": {
        "goal_usd":        1000.0,   # target vault before skim phase kicks in
        "phase":           1,        # 1 = growing, 2 = skimming
        "phase2_basis":    None,     # vault value when phase 2 was entered (or last skim)
        "total_paid_out":  0.0,      # lifetime earnings extracted
        "skim_threshold":  100.0,    # profit above basis needed to trigger a payout
        "skim_pct":        0.50,     # fraction of profit to take out (50%)
        "skim_cap":        100.0,    # max single payout
        "payout_log":      [],       # [{ts, amount, basis_before, vault_after}]
    },
}

_state_path_env = os.getenv("STATE_PATH", "")
SAVEFILE        = _state_path_env if _state_path_env else f"state_{CONFIG['tenant']['name']}.json"
TRADELOG_FILE   = os.path.join(os.path.dirname(SAVEFILE) or ".", "trades.jsonl")
SCAN_LOG_FILE   = os.path.join(os.path.dirname(SAVEFILE) or ".", "scan_log.jsonl")
WALLETS_FILE    = os.path.join(os.path.dirname(SAVEFILE) or ".", "wallets.json")
_SCAN_LOG_MAX_LINES = 100_000   # rotate oldest half when exceeded


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


def _append_trade(entry: Dict) -> None:
    """Append one trade to the append-only JSONL file — survives restarts and state resets."""
    try:
        with open(TRADELOG_FILE, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        log(f"WARN _append_trade failed: {e}")


def _load_tradelog_file() -> List[Dict]:
    """Read trades.jsonl and return all records, skipping corrupt lines."""
    records: List[Dict] = []
    try:
        with open(TRADELOG_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"WARN _load_tradelog_file failed: {e}")
    return records


def _save_wallets():
    """Persist wallets to a separate file so they survive state.json corruption."""
    try:
        with open(WALLETS_FILE, "w") as f:
            json.dump(STATE.get("wallets", {}), f, indent=2, default=str)
    except Exception as e:
        log(f"WARN _save_wallets failed: {e}")


def _load_wallets():
    """Load wallets from wallets.json; takes priority over anything in state.json."""
    try:
        with open(WALLETS_FILE, "r") as f:
            wallets = json.load(f)
        STATE["wallets"] = wallets
        # Recompute cur_deployed_usd from live positions — saved value may be stale after crash
        for w in wallets.values():
            w["cur_deployed_usd"] = sum(
                p.get("usd", 0.0) for p in w.get("positions", {}).values()
                if p.get("units", 0) > 0
            )
        log(f"load_wallets: {len(wallets)} wallet(s) restored from wallets.json")
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"WARN _load_wallets failed: {e}")


def save_state():
    try:
        # Exclude trade_log — it's always rebuilt from trades.jsonl on load_state(),
        # so persisting it only bloats state.json (was growing to 300k+ lines).
        payload = {k: v for k, v in STATE.items() if k != "trade_log"}
        with open(SAVEFILE, "w") as f:
            json.dump(payload, f, indent=2, default=str)
        _save_wallets()
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
        STATE.setdefault("wallets",       {})
        STATE.setdefault("gas_paid_usd",  0.0)
        STATE.setdefault("scout_log",     [])
        STATE.setdefault("entries_today", {})
        STATE.setdefault("recently_exited", {})
        STATE.setdefault("reject_cache",    {})
        # Set vault_start once from the loaded vault if it was never recorded
        # Use explicit None check — setdefault won't override a saved null
        if STATE.get("vault_start") is None:
            STATE["vault_start"] = STATE.get("vault_usd", 1000.0)
        if not isinstance(STATE.get("wallet_goal"), dict):
            STATE["wallet_goal"] = {}
        wg = STATE["wallet_goal"]
        if wg.get("goal_usd") is None:   wg["goal_usd"]   = 1000.0
        if wg.get("phase")    is None:   wg["phase"]      = 1
        wg.setdefault("phase2_basis",   None)
        wg.setdefault("total_paid_out", 0.0)
        wg.setdefault("skim_threshold", 100.0)
        wg.setdefault("skim_pct",       0.50)
        wg.setdefault("skim_cap",       100.0)
        wg.setdefault("payout_log",     [])
        # Drop any fully-closed position shells (units<=0) left from before the purge
        # fix — otherwise they keep counting against the max-open-positions cap.
        STATE["positions"] = {s: p for s, p in STATE.get("positions", {}).items()
                              if p.get("units", 0) > 0}
        # Recompute deployed capital from actual live positions so a stale value
        # baked into state.json doesn't persist across restarts/deploys.
        STATE["cur_deployed_usd"] = sum(
            p.get("usd", 0.0) for p in STATE["positions"].values()
        )
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"WARN load_state failed: {e}")

    # Merge append-only trades.jsonl into in-memory trade_log.
    # This recovers history that was wiped from state.json by a restart or deploy.
    # Dedup by (ts, symbol, side) so a normal restart (where state.json already has
    # the trades) doesn't produce duplicates.
    file_trades = _load_tradelog_file()
    if file_trades:
        existing_keys = {
            (t.get("ts"), t.get("symbol"), t.get("side"))
            for t in STATE.get("trade_log", [])
        }
        merged = list(STATE.get("trade_log", []))
        for t in file_trades:
            k = (t.get("ts"), t.get("symbol"), t.get("side"))
            if k not in existing_keys:
                merged.append(t)
                existing_keys.add(k)
        merged.sort(key=lambda t: t.get("ts", ""))
        STATE["trade_log"] = merged
        log(f"load_state: merged trades.jsonl — {len(merged)} total trades ({len(file_trades)} on disk)")

    # Wallets live in their own file so they survive state.json corruption.
    # Load after the state merge so wallets.json always wins.
    _load_wallets()

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

# ---------------------------------------------------------------------------
# Solana WebSocket — real-time price feed via on-chain pool subscriptions
# ---------------------------------------------------------------------------
# Pump.fun bonding curve PDA derivation and data parsing.
# Complements the 20s DexScreener poll — LP drains hit the WebSocket in ~1-2s.
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
# SOL_WSS_URL env overrides the default public endpoint (set to Helius/QuickNode for prod).
SOL_WSS_URL = os.getenv("SOL_WSS_URL", "wss://api.mainnet-beta.solana.com")

WS_PRICES: Dict[str, Dict] = {}     # symbol -> {price, liq, ts}
_ws_subs:  Dict[str, Dict] = {}     # symbol -> {curve}
_ws_lock   = threading.Lock()
_ws_thread_obj: Optional[threading.Thread] = None
_SOL_USD: Dict[str, Any] = {"price": 0.0, "ts": 0.0}


def _sol_usd_cached() -> float:
    """Return cached SOL/USD, refreshing from CoinGecko if stale (>60s)."""
    if time.time() - _SOL_USD["ts"] > 60:
        try:
            r = requests.get(
                "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
                timeout=5
            ).json()
            p = float(r.get("solana", {}).get("usd") or 0)
            if p > 0:
                _SOL_USD["price"] = p
                _SOL_USD["ts"] = time.time()
        except Exception:
            pass
    return _SOL_USD["price"] or 180.0


def _pf_curve_address(mint: str) -> Optional[str]:
    """Derive the Pump.fun bonding curve PDA for a given token mint."""
    try:
        from solders.pubkey import Pubkey  # type: ignore
        curve, _ = Pubkey.find_program_address(
            [b"bonding-curve", bytes(Pubkey.from_string(mint))],
            Pubkey.from_string(PUMP_PROGRAM),
        )
        return str(curve)
    except Exception:
        return None


def _parse_pf_curve(data_b64: str) -> Optional[tuple]:
    """Decode Pump.fun bonding curve account data.
    Layout: discriminator[8] + virtual_token_reserves[8] + virtual_sol_reserves[8] + ...
    Returns (price_usd, liq_usd) or None."""
    try:
        data = base64.b64decode(data_b64)
        if len(data) < 25:
            return None
        vt = struct.unpack_from("<Q", data, 8)[0]   # virtual_token_reserves
        vs = struct.unpack_from("<Q", data, 16)[0]  # virtual_sol_reserves
        if vt == 0:
            return None
        # Price: (vs/1e9 SOL) / (vt/1e6 tokens) → SOL per token × USD per SOL
        price_usd = (vs / vt / 1000) * _sol_usd_cached()
        liq_usd   = (vs / 1e9) * _sol_usd_cached() * 2  # rough TVL (2× the SOL side)
        return price_usd, liq_usd
    except Exception:
        return None


def _ws_add(symbol: str, mint: str):
    """Register a SOL token for live WebSocket price subscription."""
    if not mint or len(mint) < 32:
        return
    curve = _pf_curve_address(mint)
    if curve:
        with _ws_lock:
            _ws_subs[symbol] = {"curve": curve}
        log(f"WS: registered {symbol} → curve {curve[:8]}…")
    else:
        log(f"WS: could not derive curve for {symbol} ({mint[:10]}…) — using DexScreener only")


def _ws_remove(symbol: str):
    """Deregister a symbol from WebSocket subscriptions on position close."""
    with _ws_lock:
        _ws_subs.pop(symbol, None)
        WS_PRICES.pop(symbol, None)


async def _ws_loop():
    import websockets  # type: ignore
    while True:
        try:
            async with websockets.connect(
                SOL_WSS_URL, ping_interval=20, ping_timeout=30, max_size=10_000_000
            ) as ws:
                log(f"WS: connected to {SOL_WSS_URL}")
                sub_to_sym: Dict[int, str] = {}   # subscription_id -> symbol
                pending: Dict[int, str]    = {}   # req_id -> symbol (awaiting sub confirm)
                req_id = 100
                last_sync = 0.0

                async def sync_subs():
                    nonlocal req_id
                    with _ws_lock:
                        cur = dict(_ws_subs)
                    subscribed_syms = set(sub_to_sym.values()) | set(pending.values())
                    for sym, info in cur.items():
                        if sym not in subscribed_syms:
                            await ws.send(json.dumps({
                                "jsonrpc": "2.0", "id": req_id,
                                "method": "accountSubscribe",
                                "params": [info["curve"],
                                           {"encoding": "base64", "commitment": "confirmed"}],
                            }))
                            pending[req_id] = sym
                            req_id += 1
                    # Unsubscribe dropped symbols
                    drop = [sid for sid, sym in sub_to_sym.items() if sym not in cur]
                    for sid in drop:
                        sym = sub_to_sym.pop(sid)
                        await ws.send(json.dumps({
                            "jsonrpc": "2.0", "id": req_id,
                            "method": "accountUnsubscribe",
                            "params": [sid],
                        }))
                        req_id += 1
                        log(f"WS: unsubscribed {sym}")

                await sync_subs()
                last_sync = time.time()

                async for raw in ws:
                    msg = json.loads(raw)

                    # Subscribe confirmation: map result (sub ID) to symbol
                    if "result" in msg and isinstance(msg["result"], int):
                        rid = msg.get("id")
                        sym = pending.pop(rid, None)
                        if sym:
                            sub_to_sym[msg["result"]] = sym
                            log(f"WS: subscribed {sym} (sub={msg['result']})")

                    # Live account notification
                    elif msg.get("method") == "accountNotification":
                        params = msg.get("params", {})
                        sym = sub_to_sym.get(params.get("subscription"))
                        if sym:
                            value = params.get("result", {}).get("value", {})
                            d = value.get("data")
                            if isinstance(d, list) and d:
                                parsed = _parse_pf_curve(d[0])
                                if parsed:
                                    price_usd, liq_usd = parsed
                                    with _ws_lock:
                                        WS_PRICES[sym] = {
                                            "price": price_usd,
                                            "liq":   liq_usd,
                                            "ts":    time.time(),
                                        }

                    # Sync subscriptions every 5s (picks up new positions, drops closed ones)
                    if time.time() - last_sync > 5:
                        await sync_subs()
                        last_sync = time.time()

        except Exception as e:
            log(f"WS: error — {e}; reconnecting in 5s")
            await asyncio.sleep(5)


def _ws_thread_fn():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_ws_loop())


def _start_ws_thread():
    global _ws_thread_obj
    if _ws_thread_obj and _ws_thread_obj.is_alive():
        return
    _ws_thread_obj = threading.Thread(target=_ws_thread_fn, name="ws-price", daemon=True)
    _ws_thread_obj.start()


# ---------------------------------------------------------------------------
# Pump.fun on-chain new-token detector  (second WS conn, logsSubscribe)
# ---------------------------------------------------------------------------
# When a new token is minted on Pump.fun the program emits a CreateEvent in
# the transaction logs.  We subscribe to those logs, parse the event, and
# fire a DexScreener probe 4 s later so scan_candidates can evaluate the
# token with an accurate on-chain creation timestamp — beating the 8 s poll.
# ---------------------------------------------------------------------------

_ONCHAIN_CREATES: Dict[str, Dict] = {}   # mint → {ts, symbol}
_PROBE_QUEUE:     List[Dict]       = []   # DexScreener pair dicts ready to evaluate
_ONCHAIN_LOCK     = threading.Lock()
_CREATE_DISC: Optional[bytes]      = None
_ws_create_thread: Optional[threading.Thread] = None


def _pf_create_disc() -> bytes:
    global _CREATE_DISC
    if _CREATE_DISC is None:
        _CREATE_DISC = hashlib.sha256(b"event:CreateEvent").digest()[:8]
    return _CREATE_DISC


def _parse_pf_create_event(data_b64: str) -> Optional[Dict]:
    """Decode a Pump.fun CreateEvent from an Anchor 'Program data:' log line.
    Layout after 8-byte discriminator: name(str) symbol(str) uri(str) mint(pk) curve(pk) user(pk)."""
    try:
        data = base64.b64decode(data_b64)
        if len(data) < 8 or data[:8] != _pf_create_disc():
            return None
        from solders.pubkey import Pubkey
        pos = 8

        def read_str() -> str:
            nonlocal pos
            n = struct.unpack_from("<I", data, pos)[0]; pos += 4
            s = data[pos:pos + n].decode("utf-8", errors="replace"); pos += n
            return s

        def read_pk() -> str:
            nonlocal pos
            pk = str(Pubkey.from_bytes(data[pos:pos + 32])); pos += 32
            return pk

        name   = read_str()
        symbol = read_str()
        _uri   = read_str()
        mint   = read_pk()
        curve  = read_pk()
        return {"name": name, "symbol": symbol, "mint": mint, "bonding_curve": curve}
    except Exception:
        return None


def _on_pumpfun_create(ev: Dict):
    """Register a newly detected Pump.fun token and kick off a DexScreener probe."""
    mint, symbol = ev["mint"], ev.get("symbol", "???")
    with _ONCHAIN_LOCK:
        if mint in _ONCHAIN_CREATES:
            return
        _ONCHAIN_CREATES[mint] = {"ts": time.time(), "symbol": symbol}
        cutoff = time.time() - 600
        for k in list(_ONCHAIN_CREATES):
            if _ONCHAIN_CREATES[k]["ts"] < cutoff:
                del _ONCHAIN_CREATES[k]
    log(f"WS-create: ${symbol} ({mint[:8]}…) minted on-chain")
    _ws_add(symbol, mint)   # subscribe price feed immediately
    threading.Thread(target=_probe_new_token, args=(mint, symbol), daemon=True).start()


def _probe_new_token(mint: str, symbol: str):
    """Poll DexScreener until the token is indexed, then queue it for scan_candidates.
    New Pump.fun mints typically appear on DexScreener within 30-120 seconds.
    We retry with backoff for up to 3 minutes so we don't silently drop them.
    """
    delays = [10, 20, 30, 40, 50, 30]   # 10+20+30+40+50+30 = 180s total
    for wait in delays:
        time.sleep(wait)
        try:
            data      = fetch_dexscreener_token(mint)
            sol_pairs = [p for p in ((data or {}).get("pairs") or []) if p.get("chainId") == "solana"]
            if not sol_pairs:
                continue
            # Only consider pairs that have actual liquidity data. A new token may be
            # indexed (pair exists) but liq=0 if no trades have settled yet.
            # _pair_to_candidate() hard-rejects liq<=0, so queuing it would be a
            # silent drop that empties the probe queue with no scout entry produced.
            liq_pairs = [p for p in sol_pairs if float((p.get("liquidity") or {}).get("usd") or 0) > 0]
            if not liq_pairs:
                continue   # retry — waiting for liquidity to settle
            pair = max(liq_pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
            with _ONCHAIN_LOCK:
                pair["_onchain_ts"] = _ONCHAIN_CREATES.get(mint, {}).get("ts", time.time())
                _PROBE_QUEUE.append(pair)
            log(f"WS-create: ${symbol} indexed by DexScreener — queued for scan")
            return
        except Exception as e:
            log(f"WS-create: probe error {symbol}: {e}")
            return
    log(f"WS-create: ${symbol} never indexed by DexScreener after 3 min — dropped")


async def _ws_create_loop():
    """Subscribe to Pump.fun program logs to detect new token creation in real time."""
    import websockets
    while True:
        try:
            async with websockets.connect(
                SOL_WSS_URL, ping_interval=20, ping_timeout=30, max_size=10_000_000
            ) as ws:
                log("WS-create: connected, subscribing to Pump.fun log events")
                await ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": 1,
                    "method": "logsSubscribe",
                    "params": [{"mentions": [PUMP_PROGRAM]}, {"commitment": "processed"}],
                }))
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("method") != "logsNotification":
                        continue
                    logs = msg.get("params", {}).get("result", {}).get("value", {}).get("logs", [])
                    if not any("Instruction: Create" in l for l in logs):
                        continue
                    for line in logs:
                        if line.startswith("Program data: "):
                            ev = _parse_pf_create_event(line[14:])
                            if ev:
                                _on_pumpfun_create(ev)
                                break
        except Exception as e:
            log(f"WS-create: error — {e}; reconnecting in 5s")
            await asyncio.sleep(5)


def _ws_create_thread_fn():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_ws_create_loop())


def _start_ws_create_thread():
    global _ws_create_thread
    if _ws_create_thread and _ws_create_thread.is_alive():
        return
    _ws_create_thread = threading.Thread(target=_ws_create_thread_fn, name="ws-create", daemon=True)
    _ws_create_thread.start()
    log("WS: Solana WebSocket price thread started")


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


_dex_token_cache: Dict[str, Any] = {}   # addr → {ts, data}
_dex_list_cache:  Dict[str, Any] = {}   # url → {ts, data}
_DEX_TOKEN_TTL = 60   # seconds to reuse per-token data (price/liq don't change in <1 min)
_DEX_LIST_TTL  = 60   # seconds to reuse boost/profile lists

def fetch_dexscreener_token(addr: str) -> Optional[Dict[str, Any]]:
    cached = _dex_token_cache.get(addr)
    if cached and time.time() - cached["ts"] < _DEX_TOKEN_TTL:
        return cached["data"]
    data = _get(DEXSCREENER_TOKEN + addr)
    _dex_token_cache[addr] = {"ts": time.time(), "data": data}
    return data


def _get_dex_list(url: str) -> Optional[Any]:
    """Cached fetch for DexScreener list endpoints (boosts, profiles) — max 1 call/min."""
    cached = _dex_list_cache.get(url)
    if cached and time.time() - cached["ts"] < _DEX_LIST_TTL:
        return cached["data"]
    data = _get(url)
    _dex_list_cache[url] = {"ts": time.time(), "data": data}
    return data


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
            # lastTradeUnixTime is NOT creation time — an old coin with a recent trade
            # would appear as age_min=0.5, bypassing m5/trend checks. Clamp to at least
            # 30 min so m5 checks always apply. Proxy m5 from 24h direction.
            age_min = max(30.0, (time.time() - age) / 60 if age else 9999)
            positive24h = float(t.get("v24hChangePercent") or 0) > 0
            if sym and price > 0 and liq > 0:
                hype = min(100, int(vol24 / max(liq, 1) * 20) + _degen_hype_bonus(sym))
                out.append({
                    "symbol": sym, "chain": "sol", "price": price,
                    "liq": liq, "age_min": age_min, "hype": hype,
                    "positive": positive24h,
                    "price_chg_m5": 1.0 if positive24h else -1.0,
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
        price_chg    = float((pair.get("priceChange") or {}).get("h24") or 0)
        price_chg_m5 = float((pair.get("priceChange") or {}).get("m5") or 0)
        price_chg_h1 = float((pair.get("priceChange") or {}).get("h1") or 0)
        return {"symbol": symbol, "chain": our_chain, "price": price_usd,
                "liq": liq, "age_min": age_min, "hype": hype, "positive": price_chg > 0,
                "price_chg_m5": price_chg_m5, "price_chg_h1": price_chg_h1,
                "vol_h1": vol_h1, "buy_ratio": buy_ratio,
                "address": (pair.get("baseToken") or {}).get("address", "")}
    except Exception:
        return None


def fetch_new_candidates() -> List[Dict[str, Any]]:
    active_ids = {CHAIN_IDS[c] for c in CONFIG["chains"] if c in CHAIN_IDS}
    seen: set = set()
    out: List[Dict[str, Any]] = []

    # Collect addresses per chain from boosts + profiles lists (both cached 60s).
    # Then batch-fetch in groups of 30 — 1-2 API calls instead of 25+.
    addrs_by_chain: Dict[str, List[str]] = {}
    for item in (_get_dex_list(DEXSCREENER_BOOSTS) or []):
        cid = item.get("chainId", "")
        if cid in active_ids:
            addrs_by_chain.setdefault(cid, []).append(item.get("tokenAddress", ""))
    for item in (_get_dex_list(DEXSCREENER_PROFILES) or []):
        cid = item.get("chainId", "")
        if cid in active_ids:
            addr = item.get("tokenAddress", "")
            if addr not in addrs_by_chain.get(cid, []):
                addrs_by_chain.setdefault(cid, []).append(addr)

    for dex_chain_id, addrs in addrs_by_chain.items():
        our_chain = next((k for k, v in CHAIN_IDS.items() if v == dex_chain_id), None)
        if not our_chain:
            continue
        # DexScreener batch endpoint supports up to 30 addresses per call.
        for i in range(0, len(addrs), 30):
            batch = addrs[i:i + 30]
            batch_key = dex_chain_id + ":" + ",".join(sorted(batch))
            cached = _dex_token_cache.get(batch_key)
            if cached and time.time() - cached["ts"] < _DEX_TOKEN_TTL:
                pairs_list = cached["data"]
            else:
                url = f"{DEXSCREENER_BASE}/tokens/v1/{dex_chain_id}/{','.join(batch)}"
                pairs_list = _get(url) or []
                _dex_token_cache[batch_key] = {"ts": time.time(), "data": pairs_list}
            for pair in (pairs_list or []):
                c = _pair_to_candidate(pair, our_chain)
                if c:
                    key = (c["symbol"], c["chain"])
                    if key not in seen:
                        seen.add(key)
                        out.append(c)

    # Birdeye — trending Solana tokens (only if API key set)
    for c in fetch_birdeye_sol_candidates():
        key = (c["symbol"], c["chain"])
        if key not in seen:
            seen.add(key)
            out.append(c)

    return out

def _px_dict(price: float, liq: float = 0.0, vol_h1: float = 0.0, change_m5: float = 0.0) -> Dict:
    return {"price": price, "liq": liq, "vol_h1": vol_h1, "change_m5": change_m5}


def _parse_dex_pair(pair: Dict) -> Optional[Dict]:
    price     = float(pair.get("priceUsd") or 0)
    liq       = float((pair.get("liquidity") or {}).get("usd") or 0)
    vol_h1    = float((pair.get("volume")      or {}).get("h1") or 0)
    change_m5 = float((pair.get("priceChange") or {}).get("m5") or 0)
    return _px_dict(price, liq, vol_h1, change_m5) if price > 0 else None


# Cache base prices from DexScreener/Birdeye — these sources only refresh every 5-30s
# anyway, so re-fetching every 0.1s poll cycle burns 600+ req/min for zero benefit.
# WS overlay (below) still runs every call so exits stay sub-second.
_pos_price_cache: Dict[str, Any] = {"ts": 0.0, "data": {}, "open_pos_key": ""}
_POS_PRICE_TTL = 2.0   # seconds between DexScreener/Birdeye refreshes


def fetch_positions_prices() -> Dict[str, Dict]:
    """Batch-fetch price, liq, vol_h1, change_m5 for all open positions.

    Includes wallet-only open positions so wallet SL/TP keeps working even
    after the main position for that symbol has already closed (and _ws_remove
    was called). Without this, wallet positions lose their price feed and the
    SL stub fallback (avg * 1.02) keeps them stuck open forever.
    """
    open_pos = {s: p for s, p in STATE["positions"].items() if p.get("units", 0) > 0}
    # Merge in any wallet positions for symbols not already in main open_pos
    for w in STATE.get("wallets", {}).values():
        for s, p in w.get("positions", {}).items():
            if p.get("units", 0) > 0 and s not in open_pos and p.get("address"):
                open_pos[s] = p
    if not open_pos:
        return {}

    now = time.time()
    # Cache key captures which positions are open so we re-fetch when positions change.
    open_key = ",".join(sorted(open_pos.keys()))
    cache_fresh = (
        now - _pos_price_cache["ts"] < _POS_PRICE_TTL
        and _pos_price_cache["open_pos_key"] == open_key
    )
    if cache_fresh:
        result = dict(_pos_price_cache["data"])
    else:
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
                        # Use entry_liq as fallback — 100000 default would poison liq_prev
                        # and trigger a false rug alarm on the next DexScreener tick.
                        bd_liq = float(bd.get("liquidity") or 0) or p.get("entry_liq", 0) or 0
                        result[sym] = _px_dict(float(bd["value"]), bd_liq)
                        continue
                result[sym] = _px_dict(p.get("avg", 1.0) * 1.02)
        _pos_price_cache["ts"] = now
        _pos_price_cache["data"] = dict(result)
        _pos_price_cache["open_pos_key"] = open_key

    # Overlay WebSocket prices where fresher than 3s — these come directly from the
    # Pump.fun bonding curve on-chain and update in ~1-2s vs DexScreener's 5-30s lag.
    with _ws_lock:
        ws_snap = dict(WS_PRICES)
    for sym, ws in ws_snap.items():
        if sym in result and now - ws.get("ts", 0) < 3:
            existing = result[sym]
            result[sym] = _px_dict(
                ws["price"],
                ws.get("liq") or existing.get("liq", 0),
                existing.get("vol_h1", 0),
                existing.get("change_m5", 0),
            )

    return result

# ---------------------------------------------------------------------------
# Per-mode effective moonshot settings
# ---------------------------------------------------------------------------
_MS_OVERRIDE_KEYS = (
    "m5_min", "buy_ratio_min", "velocity_exit_pct", "velocity_2m_pct",
    "rug_liq_drop", "loss_cooldown_min", "trailing_stop_pct",
    "liq_drain_ticks", "liq_drain_pct",
)

def _effective_ms(mode_name: Optional[str] = None) -> Dict:
    """Return CONFIG['moonshot'] merged with any per-mode overrides for mode_name.

    Each built-in mode can set its own entry filters (m5_min, buy_ratio_min) and
    exit thresholds (velocity_exit_pct, rug_liq_drop, etc.) independently.
    Settings not set in the mode fall back to the global moonshot config.
    """
    ms = CONFIG["moonshot"]
    m  = mode_name or CONFIG.get("mode", "default")
    overrides = CONFIG["modes"].get(m, {})
    if not overrides:
        return ms
    result = dict(ms)
    for key in _MS_OVERRIDE_KEYS:
        if key in overrides:
            result[key] = overrides[key]
    return result


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
class Score:
    def __init__(self, hype: int, liq: float, age_min: float, positive: bool,
                 buy_ratio: Optional[float] = None, price: float = 0.0,
                 price_chg_m5: float = 0.0, price_chg_h1: float = 0.0):
        self.hype          = hype
        self.liq           = liq
        self.age_min       = age_min
        self.positive      = positive
        self.buy_ratio     = buy_ratio   # h1 buys/(buys+sells); None if unknown
        self.price         = price
        self.price_chg_m5  = price_chg_m5  # DexScreener m5 price change %
        self.price_chg_h1  = price_chg_h1  # DexScreener h1 price change %


def moonshot_reject_reason(sc: Score, chain: str = "sol") -> Optional[str]:
    """Return a human-readable reason the candidate fails the filters, or None if it passes."""
    ms = _effective_ms(CONFIG.get("mode"))
    spray = STATE.get("spray_until")
    spraying = spray and now_utc().date().isoformat() <= spray
    # Use the ACTIVE MODE's liquidity floor so modes actually change aggressiveness
    # (degen $10k → more candidates, safe $50k → fewer/safer). Falls back to the
    # moonshot floor if a mode doesn't set one.
    mode_liq = CONFIG["modes"].get(CONFIG["mode"], {}).get("liq_min", ms["liq_min"])
    # EVM chains need higher liq floors: ETH gas is $6/trade, thin pools are instantly fatal.
    # Data: SOL 47% WR +$206 vs EVM chains -$138. Raise the bar, not the ban.
    _evm_mult = {"eth": 5.0, "base": 3.0, "bsc": 3.0}
    liq_min  = mode_liq * (0.7 if spraying else 1.0) * _evm_mult.get(chain, 1.0)
    # Max-hype override (SOL only): if the community is at 100 hype, the early signal
    # outweighs thin liquidity — lower the floor to 5k. $24 position on a 5k pool
    # is 0.5% price impact, acceptable. This is what catches the early +1000% movers.
    if chain == "sol" and sc.hype >= 100:
        liq_min = min(liq_min, 5000)
    hype_min = max(50, ms["hype_min"] - (20 if spraying else 0))
    if sc.liq < liq_min * 0.90:
        return f"liquidity ${sc.liq:,.0f} below min ${liq_min:,.0f}"
    if sc.liq > ms["liq_max"]:
        return f"liquidity ${sc.liq:,.0f} above max ${ms['liq_max']:,.0f}"
    max_age = CONFIG["modes"].get(CONFIG["mode"], {}).get(
        "max_age_min", CONFIG["scan"]["new_max_age_min"])
    if sc.age_min > max_age:
        return f"age {sc.age_min:.0f}m over {max_age:.0f}m limit"
    # Skip the 24h trend check for tokens under 5 minutes old — they have no 24h history
    # so price_chg=0, which _pair_to_candidate() treats as non-positive. This false-rejects
    # brand new mints that haven't had time to establish a directional trend.
    if not sc.positive and sc.age_min >= 5:
        return "price trend is negative (24h)"
    # Require active upward momentum in the last 5 minutes.
    # h24 is useless for new tokens (compares to launch price, not the recent peak) — a token
    # can show +300% h24 while actively crashing right now. Only enter when the recent candle
    # is positive. Exempt tokens under 5 min: DexScreener has no 5m history yet for new mints.
    m5_min = ms.get("m5_min", 0.5)
    if sc.age_min >= 5 and sc.price_chg_m5 < m5_min:
        return f"not trending up — m5 {sc.price_chg_m5:.1f}%"
    if sc.hype < hype_min:
        return f"hype {sc.hype} below min {hype_min}"
    # Buyer/seller pressure — skip tokens being actively dumped (more sells than buys).
    br_min = ms.get("buy_ratio_min", 0.45)
    if sc.buy_ratio is not None and sc.buy_ratio < br_min:
        return f"selling pressure — only {sc.buy_ratio*100:.0f}% of trades are buys"
    return None


def passes_moonshot_filters(sc: Score, chain: str = "sol") -> bool:
    return moonshot_reject_reason(sc, chain) is None


def _reject_spike_threshold(reason: str) -> float:
    """Return the price-spike multiplier required before re-evaluating a rejected token.
    Higher = harder to get back on the radar.
    The TTL tiers (see reject_cache check):
      threshold >= 3.0 → 4h  (hard scam: honeypot, freeze, mint authority)
      threshold >= 2.0 → 2h  (soft safety: LP unlocked, whale concentration — common on
                               legitimate pump.fun tokens, ~50% win rate when traded)
      else             → 30m (liq/hype/generic)
    """
    r = reason.lower()
    # Hard on-chain rug signals — needs 3x or 4h wait
    if "honeypot" in r or "freeze" in r or "mint auth" in r:
        return 3.0
    # Soft rugcheck flags (LP unlocked, whale/holder concentration) — common on legit meme
    # coins, ~50% win rate historically. Re-evaluate after 2h or a 2x move.
    if "safety" in r or "rug" in r or "flagged" in r:
        return 2.0
    if "liquidity" in r:
        return 1.5   # liq needs to grow substantially (price proxy) before re-checking
    if "selling pressure" in r or "buy_ratio" in r or "buy ratio" in r:
        return 1.4   # sentiment can shift, but needs a real move
    if "age" in r:
        return 1.0   # age gate clears with time alone — re-evaluate every scan
    return 1.3       # generic gate: need a ~30% spike


_scan_log_write_count = 0

def _scout(symbol: str, chain: str, decision: str, reason: str,
           sc: Optional[Score] = None, address: str = ""):
    """Record why the scanner did (or didn't) act on a candidate, for the dashboard Scout log."""
    global _scan_log_write_count
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

    # Persist to disk: full record including price & buy_ratio for outcome analysis
    disk_entry: Dict[str, Any] = {
        "ts":   now_utc().isoformat(),
        "sym":  symbol,
        "ch":   chain,
        "addr": address or "",
        "dec":  decision,
        "rsn":  reason[:120],
    }
    if sc is not None:
        disk_entry.update({
            "px":   sc.price,
            "liq":  round(sc.liq, 0),
            "hype": sc.hype,
            "age":  round(sc.age_min, 1),
            "br":   round(sc.buy_ratio, 3) if sc.buy_ratio is not None else None,
            "m5":   round(sc.price_chg_m5, 2),
        })
    try:
        with open(SCAN_LOG_FILE, "a") as _f:
            _f.write(json.dumps(disk_entry, separators=(",", ":")) + "\n")
        _scan_log_write_count += 1
        # Rotate file if it's grown too large (keep newest half)
        if _scan_log_write_count % 2000 == 0:
            try:
                with open(SCAN_LOG_FILE) as _rf:
                    lines = _rf.readlines()
                if len(lines) > _SCAN_LOG_MAX_LINES:
                    with open(SCAN_LOG_FILE, "w") as _wf:
                        _wf.writelines(lines[len(lines)//2:])
            except Exception:
                pass
    except Exception:
        pass

    # Cache this rejection (or size-blocked suggestion) so the scanner skips the token
    # next scan. Without this, a token that passes filters but is too thin to size
    # gets SUGGEST'd every 7 seconds and floods the 300-entry scout log.
    if decision in ("rejected", "suggested") and sc is not None and sc.price > 0:
        if decision == "suggested":
            # Suggested = passed all filters but couldn't size (caps/budget).
            # Block for 5 min unless a genuine 30% pump warrants a fresh look.
            # threshold=1.3 + short TTL: stays silent at flat price, re-evals on spikes.
            STATE.setdefault("reject_cache", {})[symbol] = {
                "ts":        time.time(),
                "reason":    reason,
                "threshold": 1.3,
                "ttl":       300,   # re-evaluate after 5 minutes
                "price":     sc.price,
                "min_liq":   0,
            }
        else:
            threshold = _reject_spike_threshold(reason)
            if threshold > 1.0:   # age-gate tokens re-evaluate every scan naturally
                entry = {
                    "ts":        time.time(),
                    "reason":    reason,
                    "threshold": threshold,
                    "price":     sc.price,
                    "min_liq":   CONFIG["modes"].get(CONFIG["mode"], {}).get("liq_min",
                                 CONFIG["moonshot"].get("liq_min", 10000)),
                }
                if threshold < 2.0:
                    # Non-safety reject: use shorter TTL for brand-new tokens.
                    # New mints (< 30 min) can pump fast — re-check every 5 min.
                    # Older tokens change more slowly — 10 min is enough.
                    entry["ttl"] = 300 if sc.age_min < 30 else 600
                STATE.setdefault("reject_cache", {})[symbol] = entry

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
    # Hard floor: if the vault has fallen to ≤25% of what it started at, stop trading
    # entirely until the user tops it up. This is a separate concern from reserve_pct
    # (which is a per-trade holdback); this is a total account stop-loss.
    floor_pct   = CONFIG.get("absolute_floor_pct", 0.25)
    vault_start = STATE.get("vault_start", STATE["vault_usd"])
    if STATE["vault_usd"] <= vault_start * floor_pct:
        return 0.0
    # Hold back MORE capital while the drawdown brake is on (bad streak) — protect the
    # bankroll when the edge has gone cold, lean in again when it recovers.
    reserve = CONFIG["reserve_pct"]
    if drawdown_brake_active():
        reserve = max(reserve, CONFIG.get("drawdown_brake", {}).get("reserve_pct", 0.40))
    return max(0.0, STATE["vault_usd"] * (1 - reserve))


def _mode_min_ticket(mode_name: Optional[str] = None) -> float:
    """Per-mode min ticket override; falls back to global moonshot config."""
    m = mode_name or CONFIG["mode"]
    return CONFIG["modes"].get(m, {}).get("min_ticket_usd") or CONFIG["moonshot"].get("min_ticket_usd", 15.0)


# Hardcoded base — never mutated. _apply_custom_mode_globals can overwrite
# CONFIG["moonshot"]["dollar_stop_usd"] (e.g. testing 3 sets it to $1), which
# would produce absurd $2 position caps on ALL modes. We always fall back here.
_BASE_DOLLAR_STOP = 12.0


def _mode_dollar_stop(mode_name: Optional[str] = None) -> float:
    """Per-mode dollar stop for exit checks and position sizing.

    Priority: explicit mode field (e.g. micro: 1.50) → base default ($12).
    Custom modes (backtest presets) skip the mutated global — their dollar_stop
    is a backtest parameter, not a real per-trade limit.
    """
    m = mode_name or CONFIG["mode"]
    explicit = CONFIG["modes"].get(m, {}).get("dollar_stop_usd")
    if explicit:
        return float(explicit)
    return _BASE_DOLLAR_STOP


def per_chain_room(chain: str) -> float:
    cap  = CONFIG["per_chain_cap_pct"].get(chain, 0.25) * deployable_now()
    used = sum(p["usd"] for p in STATE["positions"].values() if p["chain"] == chain)
    return max(0.0, cap - used)


def per_token_cap_room(symbol: str) -> float:
    cap = CONFIG["per_token_cap_pct"] * deployable_now()
    already_in = STATE.get("positions", {}).get(symbol, {}).get("usd", 0.0)
    return max(0.0, cap - already_in)


def _conviction_mult(hype: Optional[int], buy_ratio: Optional[float]) -> float:
    """Scale ticket size by setup quality: stronger hype + buying pressure → bigger bet.
    Bounded [0.8, 1.5] so it nudges, never blows past the risk caps."""
    m = 1.0
    if hype is not None:
        m += max(0.0, (hype - 80) / 20.0) * 0.4   # hype 80→1.0, 100→1.4
    if buy_ratio is not None:
        if   buy_ratio >= 0.80: m += 0.15
        elif buy_ratio >= 0.65: m += 0.07
        elif buy_ratio < 0.45:  m *= 0.75  # borderline sell pressure: 25% smaller bet
    return max(0.8, min(1.5, m))


def size_ticket_usd(chain: str, hype: Optional[int] = None,
                    buy_ratio: Optional[float] = None, symbol: str = "") -> float:
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
    if CONFIG["moonshot"].get("dynamic_sizing"):
        avail_cash = max(0.0, STATE["vault_usd"] - STATE.get("cur_deployed_usd", 0.0))
        base = min(base, avail_cash * CONFIG["moonshot"].get("seed_pct", 0.40))
    base    = min(base, per_chain_room(chain), per_token_cap_room(symbol))
    day_cap = CONFIG["daily_deploy_cap_pct"] * deployable_now()
    base    = max(0.0, min(base, day_cap - STATE["open_today_usd"]))
    # For custom modes, the user sets size_mult and dollar_stop independently.
    # dollar_stop is an EXIT threshold, not a position-size cap, so don't apply
    # the pos_mult guard — it would silently cap bets to $2 with a $1 stop.
    mode_cfg = CONFIG["modes"].get(CONFIG["mode"], {})
    if not mode_cfg.get("_custom"):
        d_stop = _mode_dollar_stop(CONFIG["mode"])
        mult   = CONFIG["moonshot"].get("dollar_stop_pos_mult", 2.0)
        base   = min(base, d_stop * mult)
    # ±5% jitter — bets should never all be the same round number on-chain
    return round(base * random.uniform(0.95, 1.05), 2)

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


def shadow_buy(symbol: str, chain: str, usd: float, price: float, liq_usd: float, address: str = "",
               entry_m5: Optional[float] = None, entry_br: Optional[float] = None,
               entry_hype: Optional[float] = None) -> Dict[str, Any]:
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
        "peak_price": filled_price, "trough_price": filled_price, "entry_vol_h1": 0.0,
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
        # Seed liq_prev with real entry liq so the first manage_positions tick
        # has a truthful baseline. Without this, BirdEye fallback (default 100k)
        # contaminates liq_prev and fires a false rug alarm on the next real tick.
        if liq_usd > 0:
            STATE.setdefault("liq_prev", {})[symbol] = liq_usd
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
    _trade_entry = {
        "ts": now_utc().isoformat(), "symbol": symbol, "chain": chain,
        "side": "buy", "usd": usd, "price": filled_price, "units": units, "gas": gas,
        "address": pos.get("address", ""), "pnl": None,
        "mode": pos.get("entry_mode", CONFIG.get("mode")),
        "entry_m5":    entry_m5,
        "entry_br":    entry_br,
        "entry_hype":  entry_hype,
        "entry_liq":   liq_usd,
    }
    tl = STATE.setdefault("trade_log", [])
    tl.append(_trade_entry)
    _append_trade(_trade_entry)
    if is_new_pos:
        _arena_enter(symbol, chain, filled_price)
        if chain == "sol" and address:
            _ws_add(symbol, address)
    return {"price": filled_price, "units": units}


def _wallet_goal_check() -> Optional[float]:
    """Phase 1→2 transition and Phase 2 skim payout. Returns payout amount or None."""
    wg   = STATE.setdefault("wallet_goal", {})
    goal = wg.get("goal_usd", 1000.0)
    v    = STATE["vault_usd"]

    # Phase 1 → 2 transition
    if wg.get("phase", 1) == 1:
        if v >= goal:
            wg["phase"]        = 2
            wg["phase2_basis"] = v
            log(f"wallet_goal: Phase 2 reached — vault ${v:.2f} hit goal ${goal:.2f}. Skim mode active.")
        return None

    # Phase 2: check if profit above basis clears the threshold
    basis     = wg.get("phase2_basis") or goal
    profit    = v - basis
    threshold = wg.get("skim_threshold", 100.0)
    if profit < threshold:
        return None

    payout = min(profit * wg.get("skim_pct", 0.50), wg.get("skim_cap", 100.0))
    STATE["vault_usd"]          -= payout
    STATE["income_usd"]          = STATE.get("income_usd", 0.0) + payout
    wg["total_paid_out"]         = wg.get("total_paid_out", 0.0) + payout
    wg["phase2_basis"]           = STATE["vault_usd"]   # reset basis to post-payout vault
    wg.setdefault("payout_log", []).append({
        "ts": now_utc().isoformat(), "amount": round(payout, 2),
        "profit_before": round(profit, 2), "vault_after": round(STATE["vault_usd"], 2),
    })
    log(f"wallet_goal: Payout ${payout:.2f} (profit was ${profit:.2f}). Vault now ${STATE['vault_usd']:.2f}. Total paid out ${wg['total_paid_out']:.2f}.")
    return payout


def shadow_sell(symbol: str, usd: float, price: float, liq_usd: float, exit_reason: str = "?") -> Dict[str, Any]:
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
    avg_entry = pos["avg"]                                  # capture before reset below
    cost     = units * avg_entry
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
    _maybe_take_home(symbol, pnl)
    _wallet_goal_check()
    STATE["pnl_hist"].append(pnl)
    _trade_entry = {
        "ts": now_utc().isoformat(), "symbol": symbol, "chain": pos.get("chain", "?"),
        "side": "sell", "usd": proceeds, "price": price, "units": units, "gas": gas,
        "address": pos.get("address", ""), "pnl": pnl,
        "mode": pos.get("entry_mode", CONFIG.get("mode")),
        "exit_reason": exit_reason,
        "entry_price": avg_entry,
        "min_price":   pos.get("trough_price"),
    }
    tl = STATE.setdefault("trade_log", [])
    tl.append(_trade_entry)
    _append_trade(_trade_entry)
    # Purge a fully-closed position so its empty shell doesn't linger in the
    # positions dict and count against the max-open-positions cap (which had jammed
    # the bot at 12 phantom "positions", blocking every new entry).
    if pos.get("units", 0) <= 0:
        STATE["positions"].pop(symbol, None)
        # Recompute deployed capital so the cash-buffer check isn't stale
        # until the next buy. shadow_buy() also recomputes, but there can be
        # minutes between the last close and the next entry.
        STATE["cur_deployed_usd"] = sum(
            q.get("usd", 0.0) for q in STATE["positions"].values() if q.get("units", 0) > 0
        )
        # Record the full close so the entry loop can enforce a cool-down.
        # Pattern blocked: re-entering a declining token within minutes of a stop exit,
        # or chasing back in at the same high price after a profitable exit.
        STATE.setdefault("recently_exited", {})[symbol] = {
            "ts":          time.time(),
            "price":       price,
            "pnl":         pnl,
            "exit_reason": exit_reason,
        }
        if pnl < 0:
            # Accumulate per-token daily losses so repeated small losses escalate cooldowns.
            t_pnl = STATE.setdefault("token_pnl_today", {})
            t_pnl[symbol] = t_pnl.get(symbol, 0.0) + pnl
        if pnl <= -30:
            send_alert(
                f"🚨 HARD STOP ${symbol} — lost ${abs(pnl):.0f} on this trade. "
                f"Blacklisting for 4 hours to prevent re-entry."
            )
            STATE.setdefault("reject_cache", {})[symbol] = {
                "ts":        time.time(),
                "reason":    f"hard-stop: ${pnl:.0f} loss — blacklisted 4h",
                "threshold": 99.0,   # effectively never clears early on price alone
                "ttl":       4 * 3600,
                "price":     price,
                "min_liq":   0,
            }
        _arena_close(symbol, price, exit_reason)
        if pos.get("chain") == "sol":
            # Only drop the WS subscription when no wallet still holds the symbol.
            # If we unsubscribe while a wallet position remains open, the wallet
            # loses its price feed and its SL/TP stub falls back to avg*1.02.
            wallet_still_open = any(
                w.get("positions", {}).get(symbol, {}).get("units", 0) > 0
                for w in STATE.get("wallets", {}).values()
            )
            if not wallet_still_open:
                _ws_remove(symbol)
    save_state()
    return {"sold": proceeds, "pnl": pnl}


# ---------------------------------------------------------------------------
# Mode Arena — parallel shadow-trading across all modes simultaneously
# ---------------------------------------------------------------------------

def _arena_mode_params(mode_name: str) -> Optional[Dict]:
    """Return {sl, dollar_stop, size_mult, start} for any built-in or custom mode."""
    custom = STATE.get("custom_modes", {}).get(mode_name)
    if custom:
        return {
            "sl":           custom.get("sl_pct", 12) / 100.0,
            "dollar_stop":  float(custom.get("dollar_stop", 5)),
            "size_mult":    float(custom.get("size_mult", 1.0)),
            "start":        100.0,
        }
    builtin = CONFIG["modes"].get(mode_name)
    if builtin:
        return {
            "sl":           builtin.get("sl", 0.12),
            "dollar_stop":  float(builtin.get("dollar_stop_usd", _BASE_DOLLAR_STOP)),
            "size_mult":    float(builtin.get("size_mult", 1.0)),
            "start":        100.0,
        }
    return None


def _arena_ensure(mode_name: str, params: Dict) -> Dict:
    """Create arena state for a mode if it doesn't exist yet."""
    arenas = STATE.setdefault("arenas", {})
    if mode_name not in arenas:
        arenas[mode_name] = {
            "vault":       params["start"],
            "start_vault": params["start"],
            "positions":   {},
            "trades":      [],
            "started":     now_utc().isoformat(),
        }
    return arenas[mode_name]


_ARENA_BUILTIN_MODES = {"safe", "default", "hype", "degen"}


def _arena_enter(symbol: str, chain: str, price: float):
    """Paper-buy a new entry across the 4 builtin modes in parallel.
    Sizing is proportional: each arena bets the same FRACTION of its vault that
    the main bot bets of its own vault — so arenas never go bankrupt and their
    PnL curves are directly comparable to the main bot's."""
    main_vault = max(STATE.get("vault_usd", 1000), 1)
    main_bet   = CONFIG["base_size_usd"]   # base before size_mult

    for mode_name in _ARENA_BUILTIN_MODES:
        params = _arena_mode_params(mode_name)
        if not params:
            continue
        arena = _arena_ensure(mode_name, params)
        if symbol in arena["positions"]:
            continue
        # Proportional bet: same vault-fraction as main bot, scaled by mode size_mult
        arena_fraction = (main_bet / main_vault) * params["size_mult"]
        bet = arena["vault"] * arena_fraction
        bet = min(bet, arena["vault"] * 0.15)   # never more than 15% of remaining vault
        if bet < 1:
            continue
        units     = bet / price if price > 0 else 0
        buy_gas   = _gas_usd(chain)
        arena["vault"] -= bet + buy_gas
        arena["positions"][symbol] = {
            "entry_price": price,
            "units":       units,
            "cost":        bet + buy_gas,
            "peak_price":  price,
            "chain":       chain,
            "entry_ts":    now_utc().isoformat(),
        }


def _arena_close(symbol: str, price: float, exit_reason: str):
    """When the main bot fully exits a symbol, push the final price into each arena's
    position so _manage_arenas can close it on the next tick using each mode's own params.
    We do NOT force-close here — that made every mode exit at the exact same moment/price,
    making their results identical and defeating the purpose of the comparison."""
    for mode_name, arena in STATE.get("arenas", {}).items():
        pos = arena.get("positions", {}).get(symbol)
        if pos:
            # Update peak so trail logic has the latest reference, then let
            # _manage_arenas close it independently on the next manage_positions tick.
            if price > pos.get("peak_price", 0):
                pos["peak_price"] = price
            # Store the main bot's exit price as a sentinel so we know the token
            # is done trading and won't be re-entered by _arena_enter.
            pos["main_bot_exited"] = True
            pos["main_exit_price"] = price


# ---------------------------------------------------------------------------
# Multi-wallet engine — one scanner, N independent vaults
# ---------------------------------------------------------------------------
# Each wallet lives in STATE["wallets"][wid] with its own vault_usd, positions,
# trade_log, and optional mode override. The main scanner runs once; after each
# successful entry it "offers" the same candidate to every active wallet so they
# can independently decide whether to enter based on their own guards and sizing.

_wallet_keypairs: Dict[str, Any] = {}  # wid -> solders.keypair.Keypair (live wallets only)


def _load_wallet_keypairs():
    """Scan env for W_<wid>_PK vars and load keypairs for any live-eligible wallets."""
    try:
        import base58                           # type: ignore
        from solders.keypair import Keypair     # type: ignore
    except ImportError:
        return
    for wid in list(STATE.get("wallets", {})):
        raw = os.getenv(f"W_{wid}_PK", "").strip()
        if raw and wid not in _wallet_keypairs:
            try:
                _wallet_keypairs[wid] = Keypair.from_bytes(base58.b58decode(raw))
                log(f"[W:{wid}] keypair loaded ({str(_wallet_keypairs[wid].pubkey())[:8]}…)")
            except Exception as e:
                log(f"[W:{wid}] keypair load failed — {e}")


def _wlt_live_buy(wid: str, w: Dict, symbol: str, usd: float, addr: str) -> Dict:
    """Submit a real Jupiter buy for a live wallet. Returns {price, units, sig} or {error}."""
    kp = _wallet_keypairs.get(wid)
    if not kp:
        return {"error": "no_keypair — set W_<wid>_PK in .env"}
    try:
        mode_name  = w.get("mode") or CONFIG["mode"]
        slip_bps   = CONFIG["modes"].get(mode_name, {}).get("slip_bps", 150)
        usdc_units = int(usd * 10 ** USDC_DECIMALS)
        quote      = _jupiter_get_quote(USDC_MINT_SOL, addr, usdc_units, slip_bps)
        if not quote or not quote.get("outAmount"):
            return {"error": "no_jupiter_route"}
        tx_b64 = _jupiter_get_swap_tx(quote, str(kp.pubkey()))
        if not tx_b64:
            return {"error": "jupiter_swap_build_failed"}
        sig       = _sol_submit_tx(tx_b64, kp)
        tok_dec   = _sol_token_decimals(addr)
        out_units = int(quote["outAmount"]) / 10 ** tok_dec
        filled_px = usd / out_units if out_units else 0
        return {"price": filled_px, "units": out_units, "sig": sig}
    except Exception as e:
        return {"error": str(e)}


def _wlt_live_sell(wid: str, w: Dict, pos: Dict, usd: float, price: float) -> Dict:
    """Submit a real Jupiter sell for a live wallet. Returns {proceeds, units_sold, sig} or {error}."""
    kp = _wallet_keypairs.get(wid)
    if not kp:
        return {"error": "no_keypair — set W_<wid>_PK in .env"}
    try:
        addr     = pos.get("address", "")
        if not addr:
            return {"error": "no_token_address"}
        mode_name  = w.get("mode") or CONFIG["mode"]
        slip_bps   = CONFIG["modes"].get(mode_name, {}).get("slip_bps", 150)
        tok_dec    = _sol_token_decimals(addr)
        pos_units  = pos.get("units", 0)
        frac       = min(1.0, usd / max(pos_units * price, 1e-9))
        sell_units = int(pos_units * frac * 10 ** tok_dec)
        if sell_units <= 0:
            return {"error": "zero_units"}
        quote = _jupiter_get_quote(addr, USDC_MINT_SOL, sell_units, slip_bps)
        if not quote or not quote.get("outAmount"):
            return {"error": "no_jupiter_route"}
        tx_b64    = _jupiter_get_swap_tx(quote, str(kp.pubkey()))
        if not tx_b64:
            return {"error": "jupiter_swap_build_failed"}
        sig       = _sol_submit_tx(tx_b64, kp)
        proceeds  = int(quote["outAmount"]) / 10 ** USDC_DECIMALS
        return {"proceeds": proceeds, "units_sold": pos_units * frac, "sig": sig}
    except Exception as e:
        return {"error": str(e)}


def _wlt_init(label: str, starting_usd: float, mode: Optional[str] = None,
               address: str = "") -> Dict:
    return {
        "label":          label,
        "address":        address,
        "starting_usd":   starting_usd,
        "vault_usd":      starting_usd,
        "mode":           mode,        # None → inherit global CONFIG["mode"]
        "active":         True,
        "live":           False,       # True → submit real Jupiter swaps using W_<wid>_PK
        "sweep_address":  "",          # cold wallet address for profit sweep (overrides global)
        "ticket_cap_usd": 0.0,         # if > 0, caps ticket size (e.g. 5.0 to run testing 3 at $5/trade)
        # Per-wallet safety overrides — None means use the global/mode default
        "safety": {
            "dollar_stop_usd":      None,  # None → _mode_dollar_stop() ($12 default)
            "dollar_stop_pos_mult": None,  # None → global (4.0); raise for bigger bets, lower for tighter risk
            "velocity_m5_pct":      None,  # None → global (0.08 = 8% drop triggers velocity exit)
            "rug_liq_drop_pct":     None,  # None → global (0.25 = 25% single-tick liq drop)
            "liq_drain_pct":        None,  # None → global (0.05 per tick, 3 ticks = drain exit)
            "reserve_pct":          None,  # None → global (0.25 = 25% of vault always held back)
            "drawdown_brake":       True,  # False → skip drawdown brake for this wallet
        },
        "created":        now_utc().isoformat(),
        "positions":      {},
        "trade_log":      [],
        "entries_today":  {},
        "recently_exited":{},
        "pnl_hist":       [],
        "liq_prev":       {},
        "open_today_usd":    0.0,
        "cur_deployed_usd":  0.0,
    }


def _wlt_deployable(w: Dict) -> float:
    floor_pct = CONFIG.get("absolute_floor_pct", 0.25)
    if w["vault_usd"] <= w.get("starting_usd", w["vault_usd"]) * floor_pct:
        return 0.0
    reserve = w.get("safety", {}).get("reserve_pct") or CONFIG["reserve_pct"]
    return max(0.0, w["vault_usd"] * (1 - reserve))


def _wlt_size(w: Dict, symbol: str, chain: str, liq: float,
              hype: Optional[int] = None, buy_ratio: Optional[float] = None) -> float:
    mode_name = w.get("mode") or CONFIG["mode"]
    mode_cfg  = CONFIG["modes"].get(mode_name, CONFIG["modes"].get(CONFIG["mode"], {}))
    base = CONFIG["base_size_usd"] * mode_cfg.get("size_mult", 1.0)
    base *= _conviction_mult(hype, buy_ratio)
    dep  = _wlt_deployable(w)
    # Per-token cap and chain cap (same fractions as main bot)
    per_token = CONFIG["per_token_cap_pct"] * dep
    per_chain = CONFIG["per_chain_cap_pct"].get(chain, 0.25) * dep
    usd = min(base, per_token, per_chain)
    # Don't deploy more than what's actually in the wallet
    usd = min(usd, max(0.0, w["vault_usd"] - w.get("cur_deployed_usd", 0.0)))
    # Per-wallet ticket cap — lets a wallet run any mode at a smaller scale
    # (e.g. "testing 3" at $5 tickets for a live go-live test)
    cap = float(w.get("ticket_cap_usd") or 0)
    if cap > 0:
        usd = min(usd, cap)
    # Gap-down safety: cap position so a 100% token crash can't exceed dollar_stop × mult.
    # Per-wallet override lets individual wallets take bigger bets if desired.
    d_stop = _mode_dollar_stop(mode_name)
    mult   = float(w.get("safety", {}).get("dollar_stop_pos_mult") or
                   CONFIG["moonshot"].get("dollar_stop_pos_mult", 4.0))
    usd = min(usd, d_stop * mult)
    return usd


def _wlt_can_enter(w: Dict, symbol: str, chain: str) -> bool:
    MS = CONFIG["moonshot"]
    mode_name = w.get("mode") or CONFIG["mode"]
    # Max open positions
    open_pos = sum(1 for p in w.get("positions", {}).values() if p.get("units", 0) > 0)
    if open_pos >= MS.get("max_open_positions", 8):
        return False
    # Daily entry count per token
    max_per_day = CONFIG.get("max_entries_per_token_day", 4)
    if w.get("entries_today", {}).get(symbol, 0) >= max_per_day:
        return False
    # Loss cooldown (same as main bot: 30 min after a losing sell)
    rex = w.get("recently_exited", {}).get(symbol)
    if rex and rex.get("pnl", 0) < 0:
        elapsed = time.time() - rex.get("ts", 0)
        cooldown = int(CONFIG["moonshot"].get("loss_cooldown_min") or 30) * 60
        if elapsed < cooldown:
            return False
    # Don't double-enter a symbol already open in this wallet
    pos = w.get("positions", {}).get(symbol)
    if pos and pos.get("units", 0) > 0:
        return False
    return True


def _wlt_buy(wid: str, w: Dict, symbol: str, chain: str,
             usd: float, price: float, liq: float, addr: str):
    if w.get("live") and chain == "sol" and addr:
        result = _wlt_live_buy(wid, w, symbol, usd, addr)
        if "error" in result:
            log(f"[W:{wid}] LIVE BUY FAILED {symbol}: {result['error']}")
            return
        filled = result["price"]
        units  = result["units"]
        log(f"[W:{wid}] LIVE BUY {symbol} sig={result.get('sig','')[:16]}…")
    else:
        slip   = _jitter_slip(0.5 * est_price_impact(usd, liq))
        filled = price * (1 + slip)
        units  = usd / filled
    mode_name    = w.get("mode") or CONFIG["mode"]
    pos = w["positions"].setdefault(symbol, {
        "chain": chain, "units": 0.0, "usd": 0.0, "avg": 0.0,
        "time":  now_utc().isoformat(), "address": addr,
        "peak_price": filled, "trough_price": filled,
        "entry_liq": liq, "liq_ticks": [],
        "deployed_usd": 0.0, "entry_mode": mode_name, "tp_index": 0,
    })
    new_total    = pos["units"] + units
    pos["avg"]   = (pos["avg"] * pos["units"] + filled * units) / max(1e-9, new_total)
    pos["units"]  = new_total
    pos["usd"]   += usd
    pos["deployed_usd"] = pos.get("deployed_usd", 0.0) + usd
    if filled > pos.get("peak_price", 0):
        pos["peak_price"] = filled

    gas = _gas_usd(chain)
    w["vault_usd"]        -= usd + gas
    w["open_today_usd"]    = w.get("open_today_usd", 0.0) + usd
    w["cur_deployed_usd"]  = w.get("cur_deployed_usd", 0.0) + usd
    w.setdefault("entries_today", {})[symbol] = w["entries_today"].get(symbol, 0) + 1

    entry = {
        "ts": now_utc().isoformat(), "symbol": symbol, "chain": chain,
        "side": "buy", "usd": usd, "price": filled, "units": units,
        "gas": gas, "address": addr, "pnl": None, "mode": mode_name,
    }
    w.setdefault("trade_log", []).append(entry)
    log(f"[W:{wid}] BUY {symbol} ${usd:.2f} @ {filled:.6f}")
    save_state()   # persist position before any exit loop runs


def _wlt_sell(wid: str, w: Dict, symbol: str, price: float,
              exit_reason: str, sell_usd: Optional[float] = None):
    pos = w.get("positions", {}).get(symbol)
    if not pos or pos.get("units", 0) <= 0:
        return
    if w.get("live") and pos.get("chain") == "sol" and pos.get("address"):
        target_usd = sell_usd or (pos["units"] * price)
        result     = _wlt_live_sell(wid, w, pos, target_usd, price)
        if "error" in result:
            log(f"[W:{wid}] LIVE SELL FAILED {symbol}: {result['error']}")
            return
        # Use actual proceeds to derive the effective exit price
        units_sold = result.get("units_sold", pos["units"])
        price      = result["proceeds"] / units_sold if units_sold else price
        log(f"[W:{wid}] LIVE SELL {symbol} sig={result.get('sig','')[:16]}…")
    deployed = pos.get("deployed_usd", pos["usd"]) or pos["usd"]
    # Full exit by default; partial exit if sell_usd provided
    if sell_usd and sell_usd < pos["usd"] * 0.99:
        frac   = sell_usd / max(pos["usd"], 1e-9)
        s_units = pos["units"] * frac
        s_cost  = deployed * frac
        pos["units"] -= s_units
        pos["usd"]   -= sell_usd
        pos["deployed_usd"] = deployed - s_cost
        proceeds = s_units * price
        pnl      = proceeds - s_cost
        gas      = _gas_usd(pos.get("chain", "sol"))
        w["vault_usd"]       += proceeds - gas
        w["cur_deployed_usd"] = max(0.0, w.get("cur_deployed_usd", 0.0) - sell_usd)
    else:
        proceeds = pos["units"] * price
        pnl      = proceeds - deployed
        gas      = _gas_usd(pos.get("chain", "sol"))
        w["vault_usd"]       += proceeds - gas
        w["cur_deployed_usd"] = max(0.0, w.get("cur_deployed_usd", 0.0) - pos.get("usd", 0))
        w["positions"].pop(symbol, None)
        w.setdefault("recently_exited", {})[symbol] = {
            "ts": time.time(), "price": price, "pnl": pnl,
        }
        # Cross-wallet cooldown: a loss in any wallet blocks ALL wallets and the main
        # scanner from re-entering the same token. Prevents the BOGE/DUVAL pattern where
        # wallet A exits at a loss but wallet B immediately re-enters the same dumper.
        if pnl < 0:
            STATE.setdefault("recently_exited", {})[symbol] = {
                "ts": time.time(), "price": price, "pnl": pnl, "source": f"wallet:{wid}",
            }
        w.setdefault("liq_prev", {}).pop(symbol, None)

    w.setdefault("pnl_hist", []).append(pnl)
    sell_rec = {
        "ts": now_utc().isoformat(), "symbol": symbol,
        "chain": pos.get("chain", "sol") if pos else "sol",
        "side": "sell", "usd": sell_usd or proceeds, "price": price,
        "units": (sell_usd / max(price, 1e-12)) if sell_usd else (proceeds / max(price, 1e-12)),
        "gas": gas, "address": pos.get("address", "") if pos else "",
        "pnl": pnl, "exit_reason": exit_reason,
        "entry_price": pos.get("avg", 0) if pos else 0,
        "mode": (pos.get("entry_mode") if pos else None) or w.get("mode") or CONFIG["mode"],
    }
    w.setdefault("trade_log", []).append(sell_rec)
    log(f"[W:{wid}] SELL {symbol} pnl ${pnl:.2f} [{exit_reason}]")
    _wallet_drawdown_check(wid, w)
    _wlt_take_home(wid, w, pnl)
    _wlt_maybe_sweep(wid, w)
    save_state()


def _wallet_drawdown_check(wid: str, w: Dict):
    """Three-tier drawdown protection on live wallets vs the real deposit (starting_usd).

    Tier 1 — Velocity ($15 lost in 30 min): alert only, no action.
    Tier 2 — Warning ($50 cumulative loss): alert only, no action.
    Tier 3 — Hard stop ($100 cumulative loss): block NEW entries immediately, but
              let existing open positions run to their own exits. Once all positions
              are closed, fully deactivate the wallet and send a final Telegram alert.

    Equity = vault_usd + open position cost basis (so unrealized losses count).
    Alerts auto-reset if equity recovers back toward starting_usd.
    """
    if not w.get("live"):
        return
    start = w.get("starting_usd", 0)
    if start <= 0:
        return

    label    = w.get("label", wid)
    open_pos = {k: v for k, v in w.get("positions", {}).items() if v.get("units", 0) > 0}

    # ── If already draining after hard stop: wait for positions to clear ──
    if w.get("paused_new_entries") and not open_pos:
        w["active"]             = False
        w["paused_new_entries"] = False
        send_alert(
            f"🛑 FULLY PAUSED — {label}\n"
            f"All positions closed. Bot deactivated. Re-enable from dashboard when ready.",
            critical=True)
        log(f"[W:{wid}] FULLY PAUSED — all positions closed, wallet deactivated")
        return

    # Total equity: cash in vault + cost basis of every open position
    equity   = w.get("vault_usd", 0) + sum(
        p.get("usd", 0) for p in open_pos.values()
    )
    drawdown = start - equity   # positive = cumulative loss from deposit
    alerted  = w.setdefault("dd_alerted", {})

    # ── Tier 3: Hard stop at $100 loss ───────────────────────────────────
    if drawdown >= 100 and not alerted.get("stop"):
        alerted["stop"]           = True
        w["paused_new_entries"]   = True   # block new entries immediately
        n_open = len(open_pos)
        if n_open == 0:
            # No open positions — deactivate now
            w["active"]             = False
            w["paused_new_entries"] = False
            send_alert(
                f"🛑 HARD STOP — {label}\n"
                f"Down ${drawdown:.0f} of your ${start:.0f} deposit. No open positions — bot DEACTIVATED.\n"
                f"Equity: ${equity:.2f}. Re-enable from dashboard when ready.",
                critical=True)
            log(f"[W:{wid}] HARD STOP — drawdown ${drawdown:.2f} — no positions, deactivated immediately")
        else:
            send_alert(
                f"🛑 HARD STOP — {label}\n"
                f"Down ${drawdown:.0f} of your ${start:.0f} deposit. No new entries.\n"
                f"Letting {n_open} open position(s) run to their exits, then fully pausing.\n"
                f"Equity: ${equity:.2f}.",
                critical=True)
            log(f"[W:{wid}] HARD STOP — drawdown ${drawdown:.2f} — draining {n_open} position(s)")
        return

    # ── Tier 2: Serious warning at $50 loss ──────────────────────────────
    if drawdown >= 50 and not alerted.get("warning"):
        alerted["warning"] = True
        send_alert(
            f"🚨 WARNING — {label}\n"
            f"Down ${drawdown:.0f} of your ${start:.0f} deposit. Equity: ${equity:.2f}.\n"
            f"Hard stop triggers at $100 loss. Watch closely.",
            critical=True)
        log(f"[W:{wid}] WARNING — drawdown ${drawdown:.2f}")

    # ── Tier 1: Velocity — losing $15+ in 30 min ─────────────────────────
    now   = time.time()
    snaps = w.setdefault("vault_snaps", [])
    if not snaps or now - snaps[-1][0] > 300:          # snapshot every 5 min
        snaps.append((now, equity))
        if len(snaps) > 14:                             # keep ~70 min
            snaps[:] = snaps[-14:]

    old = [s for s in snaps if 1500 < now - s[0] < 2100]   # 25-35 min ago
    if old:
        velocity_loss = old[-1][1] - equity             # positive = lost money
        if velocity_loss >= 15 and not alerted.get("velocity"):
            alerted["velocity"] = True
            send_alert(
                f"⚡ VELOCITY — {label}\n"
                f"Lost ${velocity_loss:.0f} in the last 30 min. Total down: ${drawdown:.0f} of ${start:.0f}.\n"
                f"Equity: ${equity:.2f}. No action taken — heads up.",
                critical=True)
            log(f"[W:{wid}] VELOCITY WARNING — ${velocity_loss:.2f} in 30min")
        elif velocity_loss < 5:
            alerted.pop("velocity", None)   # reset once pace stabilises

    # ── Reset level alerts when equity recovers ───────────────────────────
    if drawdown < 15:
        alerted.clear()
    elif drawdown < 35:
        alerted.pop("watch", None)
    elif drawdown < 70:
        alerted.pop("warning", None)


def _manage_wallet_positions(wid: str, w: Dict, live_prices: Dict):
    """Run the exit loop for one wallet's open positions using its mode's params."""
    MS        = CONFIG["moonshot"]
    liq_prev  = w.setdefault("liq_prev", {})

    for symbol, pos in list(w.get("positions", {}).items()):
        if pos.get("units", 0) <= 0:
            continue
        px        = live_prices.get(symbol, _px_dict(pos.get("avg", 1.0) * 1.02))
        price     = px["price"]
        liq       = px["liq"]
        change_m5 = px.get("change_m5", 0)
        if price <= 0:
            continue

        # Track peak
        if price > pos.get("peak_price", 0):
            pos["peak_price"] = price
        peak = pos.get("peak_price", pos["avg"])

        # Liq history for drain detection
        liq_ticks: List[float] = pos.setdefault("liq_ticks", [])
        liq_ticks.append(liq)
        if len(liq_ticks) > MS["liq_drain_ticks"] + 1:
            liq_ticks[:] = liq_ticks[-(MS["liq_drain_ticks"] + 1):]

        mode_name = pos.get("entry_mode") or w.get("mode") or CONFIG["mode"]
        mode_cfg  = CONFIG["modes"].get(mode_name, CONFIG["modes"][CONFIG["mode"]])

        # Build TP ladder (same logic as main bot)
        ladder = mode_cfg.get("tp_ladder") or [[t, 0.5] for t in mode_cfg.get("tp", MS["tp"])]
        moonbag_frac = max(0.0, 1.0 - sum(f for _, f in ladder))
        in_moonbag   = pos.get("tp_index", 0) >= len(ladder) and moonbag_frac > 0
        trail_pct    = MS["trailing_stop_pct"]
        gain_now     = (price / max(pos.get("avg") or 1e-9, 1e-9)) - 1
        if   gain_now >= 1.0 and not in_moonbag: trail_pct = min(trail_pct, 0.10)
        elif gain_now >= 0.5 and not in_moonbag: trail_pct = min(trail_pct, 0.12)

        exit_reason: Optional[str] = None
        _sf = w.get("safety", {})  # per-wallet safety overrides

        # Dollar stop — wallet override → mode default → base $12
        # Use pos["usd"] (cost basis of REMAINING units, reduced on each partial sell)
        # NOT deployed_usd (which only resets at full close — fires stop every tick after TP rung 1).
        _dollar_stop = _sf.get("dollar_stop_usd") or _mode_dollar_stop(mode_name)
        _cost        = pos.get("usd", 0) or 1e-9
        _cur_val     = pos["units"] * price
        _eff_stop    = max(_dollar_stop, _cost * 0.10)
        if _cur_val < 0.01:
            w.get("positions", {}).pop(symbol, None)
            continue
        if _dollar_stop > 0 and (_cur_val - _cost) < -_eff_stop:
            exit_reason = f"DOLLAR STOP -${abs(_cur_val - _cost):.2f}"

        # Rug liq drop — wallet override → global
        _rug_drop = _sf.get("rug_liq_drop_pct") or MS["rug_liq_drop"]
        prev_liq = liq_prev.get(symbol, liq)
        if liq < prev_liq * (1 - _rug_drop):
            exit_reason = f"RUG liq {prev_liq:.0f}→{liq:.0f}"
        liq_prev[symbol] = liq

        # Velocity — wallet override → global
        _vel_pct = _sf.get("velocity_m5_pct") or MS["velocity_exit_pct"]
        if exit_reason is None and change_m5 < -(_vel_pct * 100):
            exit_reason = f"VELOCITY {change_m5:.1f}% in 5m"

        # Slow liq drain — wallet override → global
        _drain_pct = _sf.get("liq_drain_pct") or MS["liq_drain_pct"]
        if (exit_reason is None and len(liq_ticks) >= MS["liq_drain_ticks"]
                and all(liq_ticks[i] < liq_ticks[i-1] * (1 - _drain_pct)
                        for i in range(1, len(liq_ticks)))):
            exit_reason = f"LIQ DRAIN {liq_ticks[0]:.0f}→{liq:.0f}"

        # Trailing stop
        if exit_reason is None and price <= peak * (1 - trail_pct):
            exit_reason = f"TRAIL STOP {((price/peak)-1)*100:.1f}% from peak"

        if exit_reason:
            _wlt_sell(wid, w, symbol, price, exit_reason)
            continue

        # TP ladder (partial sells)
        deployed     = pos.get("deployed_usd", pos["usd"]) or pos["usd"]
        moonbag_floor = moonbag_frac * deployed
        tp_index      = pos.get("tp_index", 0)
        for i, (gain, frac) in enumerate(ladder):
            if i < tp_index:
                continue
            if price >= pos["avg"] * (1 + gain):
                sellable  = max(0.0, pos["usd"] - moonbag_floor)
                sell_usd  = min(frac * deployed, sellable)
                pos["tp_index"] = i + 1
                if sell_usd > 0.01:
                    _wlt_sell(wid, w, symbol, price, f"TP rung {i+1}", sell_usd=sell_usd)
                break

        # Fixed SL fallback
        sl_level = pos.get("sl_override") or mode_cfg.get("sl", 0.18)
        if pos.get("units", 0) > 0 and price <= pos["avg"] * (1 - sl_level):
            _wlt_sell(wid, w, symbol, price, "fixed_sl")

    # Periodic drawdown check — takes velocity snapshots even between sells
    _wallet_drawdown_check(wid, w)


def _wallets_offer_entry(symbol: str, chain: str, price: float, liq: float,
                         addr: str, scan_entered: Dict,
                         hype: Optional[int] = None, buy_ratio: Optional[float] = None):
    """After the main bot enters a candidate, offer it to each active additional wallet.

    Each wallet gets independent humanization so on-chain transactions don't look like
    a coordinated bot fleet: entry probability (not all wallets always enter), size jitter
    (±8% on ticket size), and a per-wallet random delay offset stored for reference.
    In live mode these delays must be honored by the real tx submission layer.
    """
    for wid, w in STATE.get("wallets", {}).items():
        if not w.get("active"):
            continue
        if w.get("paused_new_entries"):
            continue
        if symbol in scan_entered.get(wid, set()):
            continue
        if not _wlt_can_enter(w, symbol, chain):
            continue

        # Entry probability — each wallet independently decides whether to enter.
        # Default 85%: wallets don't always mirror each other, reducing on-chain correlation.
        entry_prob = float(w.get("entry_prob", 0.85))
        if random.random() > entry_prob:
            log(f"[W:{wid}] SKIP {symbol} — skipped by entry_prob ({entry_prob:.0%})")
            continue

        usd = _wlt_size(w, symbol, chain, liq, hype=hype, buy_ratio=buy_ratio)
        # ticket_cap_usd wallets bypass the global min-ticket check (cap itself is the floor)
        cap = float(w.get("ticket_cap_usd") or 0)
        _min_t = max(1.0, cap * 0.5) if cap > 0 else _mode_min_ticket(w.get("mode") or CONFIG["mode"])
        if usd < _min_t:
            continue

        # Size jitter ±8% so each wallet's position size differs (not identical clone bets).
        jitter = 1.0 + random.uniform(-0.08, 0.08)
        usd = max(_min_t, usd * jitter)
        # Hard ceiling AFTER jitter — ticket_cap_usd must never be exceeded
        if cap > 0:
            usd = min(usd, cap)

        _wlt_buy(wid, w, symbol, chain, usd, price, liq, addr)
        scan_entered.setdefault(wid, set()).add(symbol)


_followup_last_run: float = 0.0

def _run_price_followup():
    """Every 30 min, check what happened to recently-scanned tokens.
    Appends {type:'px_followup'} records to SCAN_LOG_FILE so the Audit tab can
    compute filter accuracy, missed gains, and exit-quality metrics over time."""
    global _followup_last_run
    if time.time() - _followup_last_run < 1800:
        return
    _followup_last_run = time.time()

    if not os.path.exists(SCAN_LOG_FILE):
        return

    now_ts   = time.time()
    cutoff6h = now_ts - 6 * 3600  # only follow up tokens seen in last 6h

    # Read initial scan entries (not follow-up records) from last 6 hours
    by_addr: Dict[str, Dict] = {}
    try:
        with open(SCAN_LOG_FILE) as f:
            for line in f:
                try:
                    e = json.loads(line)
                    if e.get("type") == "px_followup":
                        continue
                    addr = e.get("addr", "").strip()
                    if not addr or not e.get("px", 0):
                        continue
                    try:
                        ts_sec = datetime.fromisoformat(
                            e["ts"].replace("Z", "+00:00")).timestamp()
                    except Exception:
                        continue
                    if ts_sec < cutoff6h:
                        continue
                    # Keep earliest occurrence per address (first scan of token)
                    if addr not in by_addr or ts_sec < by_addr[addr]["ts_sec"]:
                        by_addr[addr] = {**e, "ts_sec": ts_sec}
                except Exception:
                    pass
    except Exception:
        return

    if not by_addr:
        return

    # Batch DexScreener lookups (30 addresses per call)
    addrs = list(by_addr.keys())
    lines_to_append: List[str] = []

    for i in range(0, len(addrs), 30):
        batch = addrs[i:i+30]
        try:
            data  = _get(DEXSCREENER_TOKEN + ",".join(batch)) or {}
            pairs = data.get("pairs") or []
            by_pair: Dict[str, Dict] = {}
            for pair in pairs:
                a = ((pair.get("baseToken") or {}).get("address") or "").lower()
                if a and a not in by_pair:
                    by_pair[a] = pair

            for addr in batch:
                orig = by_addr[addr]
                pair = by_pair.get(addr.lower())
                if not pair:
                    continue
                px_now = float(pair.get("priceUsd") or 0)
                if px_now <= 0:
                    continue
                px_orig = orig.get("px", 0)
                elapsed_min = (now_ts - orig["ts_sec"]) / 60
                pct = round((px_now / px_orig - 1) * 100, 2) if px_orig > 0 else 0
                liq_now = float((pair.get("liquidity") or {}).get("usd") or 0)
                record = {
                    "type":        "px_followup",
                    "ts":          now_utc().isoformat(),
                    "addr":        addr,
                    "sym":         orig.get("sym", ""),
                    "dec":         orig.get("dec", ""),
                    "rsn":         orig.get("rsn", ""),
                    "px_orig":     round(px_orig, 10),
                    "px_now":      round(px_now, 10),
                    "elapsed_min": round(elapsed_min, 1),
                    "pct":         pct,
                    "liq_now":     round(liq_now, 0),
                }
                lines_to_append.append(json.dumps(record, separators=(",", ":")))
        except Exception as exc:
            log(f"price_followup batch error: {exc}")
        time.sleep(0.3)

    if lines_to_append:
        try:
            with open(SCAN_LOG_FILE, "a") as f:
                f.write("\n".join(lines_to_append) + "\n")
        except Exception:
            pass


def _manage_arenas(live_prices: Dict):
    """Check every arena position for SL / trailing-stop exit each price tick."""
    arenas = STATE.get("arenas", {})
    trail_pct = CONFIG["moonshot"].get("trailing_stop_pct", 0.15)

    for mode_name, arena in arenas.items():
        params = _arena_mode_params(mode_name)
        if not params:
            continue
        sl           = params["sl"]
        dollar_stop  = params["dollar_stop"]

        for symbol, pos in list(arena["positions"].items()):
            px_data = live_prices.get(symbol)
            if not px_data:
                continue
            price = px_data["price"] if isinstance(px_data, dict) else float(px_data)

            entry = pos["entry_price"]
            if entry <= 0:
                continue
            change = (price - entry) / entry

            if price > pos.get("peak_price", 0):
                pos["peak_price"] = price
            peak = pos["peak_price"]

            exit_reason: Optional[str] = None
            # If the main bot already exited this token, close the arena position
            # at the main bot's exit price so we're not holding a stale position
            # with no live price feed. Each mode still got to apply its own params
            # up to this point — that's the comparison value.
            if pos.get("main_bot_exited"):
                price = pos.get("main_exit_price", price)
                exit_reason = "main_bot_exit"
            else:
                loss_usd = pos["cost"] - pos["units"] * price
                # Mirror the main bot's effective stop: max(dollar_stop, 10% of position).
                # Without this, a $20 arena position with dollar_stop=1 exits on a $1 dip
                # (5%), while the main bot on $84 uses max($1, $8.40) = $8.40 — a 10% stop.
                eff_dollar_stop = max(dollar_stop, pos["cost"] * 0.10)
                if sl and change <= -sl:
                    exit_reason = "sl"
                elif loss_usd >= eff_dollar_stop:
                    exit_reason = "dollar_stop"
                elif peak > entry and (price / peak - 1) < -trail_pct:
                    exit_reason = "trail"

            if exit_reason:
                sell_gas = _gas_usd(pos.get("chain", "sol"))
                proceeds = pos["units"] * price - sell_gas
                pnl      = proceeds - pos["cost"]
                arena["vault"] += proceeds
                arena["positions"].pop(symbol, None)
                arena["trades"].append({
                    "ts":          now_utc().isoformat(),
                    "symbol":      symbol,
                    "pnl":         pnl,
                    "exit_reason": exit_reason,
                    "entry_price": entry,
                    "exit_price":  price,
                })


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


def exec_sell(symbol: str, usd: float, price: float, liq_usd: float, exit_reason: str = "?") -> Dict[str, Any]:
    if SHADOW_MODE:
        return shadow_sell(symbol, usd, price, liq_usd, exit_reason)
    pos     = STATE["positions"].get(symbol, {})
    chain   = pos.get("chain", "sol")
    address = pos.get("address", "")
    result  = live_sell(symbol, chain, usd, price, liq_usd, address)
    if not result.get("error") and chain == "sol":
        _maybe_sweep()
    return result


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
# Take-home tracker — bank 50% of big wins above the vault baseline
# ---------------------------------------------------------------------------
_TAKE_HOME_MIN_PNL  = 15.0  # bank 50% of any win >= $15
_TAKE_HOME_PCT      = 0.50  # fraction of that trade's PnL to bank

def _maybe_take_home(symbol: str, pnl: float):
    """After a profitable sell: if pnl >= $15, move 50% to the take-home tracker.
    No vault-vs-baseline gate — bank gains regardless of overall vault level."""
    if pnl < _TAKE_HOME_MIN_PNL:
        return
    amount = round(pnl * _TAKE_HOME_PCT, 2)
    if amount < 1.0:
        return
    STATE["vault_usd"]    = round(STATE["vault_usd"] - amount, 4)
    STATE["take_home_usd"] = round(STATE.get("take_home_usd", 0.0) + amount, 2)
    log_list = STATE.setdefault("take_home_log", [])
    log_list.append({
        "ts":          now_utc().isoformat(),
        "symbol":      symbol,
        "trade_pnl":   round(pnl, 2),
        "banked":      amount,
        "vault_after": round(STATE["vault_usd"], 2),
        "total":       STATE["take_home_usd"],
    })
    if len(log_list) > 200:
        del log_list[:len(log_list) - 200]
    log(f"TAKE HOME +${amount:.2f} from {symbol} (trade pnl ${pnl:.2f}) → total ${STATE['take_home_usd']:.2f}")
    save_state()


# Profit sweep — transfer excess USDC to cold wallet after each sell
# ---------------------------------------------------------------------------
_SOL_TOKEN_PROG    = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
_SOL_ASSOC_PROG    = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJe1brs"
_SOL_SYSTEM_PROG   = "11111111111111111111111111111111"


def _find_sol_ata(wallet: str, mint: str) -> str:
    from solders.pubkey import Pubkey  # type: ignore
    tok  = Pubkey.from_string(_SOL_TOKEN_PROG)
    assoc = Pubkey.from_string(_SOL_ASSOC_PROG)
    seeds = [bytes(Pubkey.from_string(wallet)), bytes(tok), bytes(Pubkey.from_string(mint))]
    ata, _ = Pubkey.find_program_address(seeds, assoc)
    return str(ata)


def _sweep_usdc_sol(amount_usdc: float, cold_address: str, kp=None) -> str:
    """Send amount_usdc USDC from hot_wallet to cold_address. Returns tx signature.
    kp defaults to the main bot keypair; pass a wallet keypair to sweep from a sub-wallet."""
    import base64, struct
    from solders.pubkey import Pubkey          # type: ignore
    from solders.instruction import AccountMeta, Instruction  # type: ignore
    from solders.message import MessageV0      # type: ignore
    from solders.transaction import VersionedTransaction  # type: ignore
    from solders.hash import Hash              # type: ignore

    kp      = kp or _sol_keypair()
    hot_pk  = kp.pubkey()
    cold_pk = Pubkey.from_string(cold_address)
    mint_pk = Pubkey.from_string(USDC_MINT_SOL)
    tok_pk  = Pubkey.from_string(_SOL_TOKEN_PROG)
    assoc_pk = Pubkey.from_string(_SOL_ASSOC_PROG)
    sys_pk  = Pubkey.from_string(_SOL_SYSTEM_PROG)

    src_ata = Pubkey.from_string(_find_sol_ata(str(hot_pk), USDC_MINT_SOL))
    dst_ata = Pubkey.from_string(_find_sol_ata(cold_address, USDC_MINT_SOL))

    # Idempotent create destination ATA (no-ops if it already exists)
    create_ix = Instruction(
        program_id=assoc_pk,
        accounts=[
            AccountMeta(pubkey=hot_pk,   is_signer=True,  is_writable=True),
            AccountMeta(pubkey=dst_ata,  is_signer=False, is_writable=True),
            AccountMeta(pubkey=cold_pk,  is_signer=False, is_writable=False),
            AccountMeta(pubkey=mint_pk,  is_signer=False, is_writable=False),
            AccountMeta(pubkey=sys_pk,   is_signer=False, is_writable=False),
            AccountMeta(pubkey=tok_pk,   is_signer=False, is_writable=False),
        ],
        data=bytes([1]),  # 1 = CreateAssociatedTokenAccountIdempotent
    )

    # SPL Token Transfer
    units = int(amount_usdc * 10 ** USDC_DECIMALS)
    transfer_ix = Instruction(
        program_id=tok_pk,
        accounts=[
            AccountMeta(pubkey=src_ata, is_signer=False, is_writable=True),
            AccountMeta(pubkey=dst_ata, is_signer=False, is_writable=True),
            AccountMeta(pubkey=hot_pk,  is_signer=True,  is_writable=False),
        ],
        data=bytes([3]) + struct.pack("<Q", units),  # 3 = Transfer
    )

    rpc = _sol_rpc()
    bh  = requests.post(rpc, json={
        "jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash",
        "params": [{"commitment": "finalized"}],
    }, timeout=10).json()["result"]["value"]["blockhash"]

    msg = MessageV0.try_compile(
        payer=hot_pk,
        instructions=[create_ix, transfer_ix],
        address_lookup_table_accounts=[],
        recent_blockhash=Hash.from_string(bh),
    )
    tx      = VersionedTransaction(msg, [kp])
    encoded = base64.b64encode(bytes(tx)).decode()

    resp = requests.post(rpc, json={
        "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
        "params": [encoded, {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}],
    }, timeout=30).json()
    if "error" in resp:
        raise RuntimeError(resp["error"])
    return resp["result"]


def _maybe_sweep():
    """After a live sell: if USDC balance > SWEEP_ABOVE_USD, send excess to cold wallet."""
    if SHADOW_MODE:
        return
    cold_addr   = os.getenv("SWEEP_SOL_ADDRESS", "").strip()
    above_usd   = float(os.getenv("SWEEP_ABOVE_USD", "0").strip() or "0")
    keep_usd    = float(os.getenv("SWEEP_KEEP_USD",  "200").strip() or "200")
    if not cold_addr or above_usd <= 0:
        return
    balance = fetch_sol_balance()
    if balance <= above_usd:
        return
    amount = round(balance - keep_usd, 2)
    if amount < 1.0:
        return
    try:
        sig = _sweep_usdc_sol(amount, cold_addr)
        entry = {
            "ts": now_utc().isoformat(), "amount_usd": amount,
            "to": cold_addr, "sig": sig,
        }
        STATE.setdefault("sweep_log", []).append(entry)
        STATE["total_swept_usd"] = round(STATE.get("total_swept_usd", 0.0) + amount, 2)
        save_state()
        log(f"SWEEP ${amount:.2f} USDC → {cold_addr[:8]}… sig={sig[:16]}…")
    except Exception as e:
        log(f"SWEEP FAILED: {e}")


_WLT_TAKE_HOME_MIN_PNL = 15.0
_WLT_TAKE_HOME_PCT     = 0.50

def _wlt_take_home(wid: str, w: Dict, pnl: float):
    """On any live wallet sell with pnl >= $15, immediately sweep 50% of the profit
    to the cold wallet. Mirrors the main bot's _maybe_take_home logic but sends on-chain
    rather than tracking in a counter."""
    if not w.get("live"):
        return
    if pnl < _WLT_TAKE_HOME_MIN_PNL:
        return
    cold_addr = (w.get("sweep_address") or os.getenv("SWEEP_SOL_ADDRESS", "")).strip()
    if not cold_addr:
        return
    amount = round(pnl * _WLT_TAKE_HOME_PCT, 2)
    if amount < 1.0:
        return
    kp = _wallet_keypairs.get(wid)
    if not kp:
        log(f"[W:{wid}] TAKE_HOME SKIPPED — no keypair loaded")
        return
    try:
        sig = _sweep_usdc_sol(amount, cold_addr, kp=kp)
        w["vault_usd"]      = round(w.get("vault_usd", 0.0) - amount, 4)
        w["take_home_usd"]  = round(w.get("take_home_usd", 0.0) + amount, 2)
        entry = {
            "ts": now_utc().isoformat(), "amount_usd": amount,
            "pnl": pnl, "to": cold_addr, "sig": sig, "type": "take_home",
        }
        w.setdefault("sweep_log", []).append(entry)
        w["total_swept_usd"] = round(w.get("total_swept_usd", 0.0) + amount, 2)
        log(f"[W:{wid}] TAKE_HOME ${amount:.2f} (50% of ${pnl:.2f} win) → {cold_addr[:8]}… sig={sig[:16]}…")
        send_alert(
            f"💸 TAKE HOME — {w.get('label', wid)}\n"
            f"Win: +${pnl:.2f} → banking ${amount:.2f} (50%) to cold wallet.\n"
            f"Sig: {sig[:20]}…")
    except Exception as e:
        log(f"[W:{wid}] TAKE_HOME FAILED: {e}")


def _wlt_maybe_sweep(wid: str, w: Dict):
    """After a live wallet sell: if vault_usd > sweep threshold, send excess USDC to cold wallet.
    This is a balance-floor sweep — catches accumulated profits that didn't trigger take_home.
    Uses the wallet's own keypair — not the main bot's key."""
    if not w.get("live"):
        return
    cold_addr = (w.get("sweep_address") or os.getenv("SWEEP_SOL_ADDRESS", "")).strip()
    above_usd = float(w.get("sweep_above_usd") or os.getenv("SWEEP_ABOVE_USD", "0") or 0)
    keep_usd  = float(w.get("sweep_keep_usd")  or os.getenv("SWEEP_KEEP_USD",  "200") or 200)
    if not cold_addr or above_usd <= 0:
        return
    balance = w.get("vault_usd", 0.0)
    if balance <= above_usd:
        return
    amount = round(balance - keep_usd, 2)
    if amount < 1.0:
        return
    kp = _wallet_keypairs.get(wid)
    if not kp:
        log(f"[W:{wid}] SWEEP SKIPPED — no keypair loaded")
        return
    try:
        sig = _sweep_usdc_sol(amount, cold_addr, kp=kp)
        w["vault_usd"] = round(w["vault_usd"] - amount, 4)
        entry = {
            "ts": now_utc().isoformat(), "amount_usd": amount,
            "to": cold_addr, "sig": sig, "type": "sweep",
        }
        w.setdefault("sweep_log", []).append(entry)
        w["total_swept_usd"] = round(w.get("total_swept_usd", 0.0) + amount, 2)
        log(f"[W:{wid}] SWEEP ${amount:.2f} USDC → {cold_addr[:8]}… sig={sig[:16]}…")
        send_alert(f"💸 SWEEP — {w.get('label', wid)}\n${amount:.2f} USDC → cold wallet {cold_addr[:8]}…")
    except Exception as e:
        log(f"[W:{wid}] SWEEP FAILED: {e}")


# ---------------------------------------------------------------------------
# Position manager
# ---------------------------------------------------------------------------
def adaptive_no_pump_window(liq_usd: float) -> int:
    ms = CONFIG["moonshot"]["adaptive_timer"]
    return ms["low_liq_sec"] if liq_usd < 50000 else ms["high_liq_sec"]


def should_exit_no_pump(entry_ts: float, now_ts: float, entry_price: float, cur_price: float, liq_usd: float,
                        mode_name: str = "") -> bool:
    if now_ts - entry_ts < adaptive_no_pump_window(liq_usd):
        return False
    m        = mode_name or CONFIG["mode"]
    mode_cfg = CONFIG["modes"].get(m, {})
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
    add_usd = min(add_usd, per_chain_room(chain), per_token_cap_room(symbol))
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
                shadow_sell(sym, trim_usd, price, liq, "trusted_trim")
                log(f"TRUSTED trim {sym}: {trim_units:.0f} units @{price:.5f}")
                send_alert(
                    f"🐕 TRIMMED {sym} @ {price:.5f} — it rose into your take-profit zone, so the bot sold "
                    f"some to bank gains while keeping your core long-term bag.")

# ---------------------------------------------------------------------------
# Re-entry watch
# ---------------------------------------------------------------------------
# Exit reasons that mean the token FAILED (dumped) — don't chase these back in.
# Only cooled-off winners (TRAIL STOP) qualify for re-entry.
_HARD_EXIT_PREFIXES = ("RUG", "VELOCITY", "LIQ DRAIN", "fixed_sl", "DOLLAR STOP")


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


# Holder-concentration flags that are normal on brand-new pump.fun launches.
# These are overridden when hype >= 90 — concentrated early holders ≠ rug intent.
# Hard structural flags (mint auth, freeze auth, honeypot, LP not locked) are NOT here
# and will still block the trade regardless of hype.
_HOLDER_CONCENTRATION_FLAGS = {
    "top 10 holders high ownership",
    "single holder ownership",
    "high holder concentration",
    "holder concentration",
}

_rugcheck_cache: Dict[str, Any] = {}
_RUGCHECK_TTL = 300  # 5 minutes — results don't change that fast

def fetch_rugcheck(address: str) -> Dict[str, Any]:
    """rugcheck.xyz Solana rug/scam report. Flags danger-level risks or a high risk score."""
    now = time.time()
    cached = _rugcheck_cache.get(address)
    if cached and now - cached["_ts"] < _RUGCHECK_TTL:
        return cached
    out: Dict[str, Any] = {"flagged": False, "reason": "", "score": None, "dangers": []}
    try:
        r = requests.get(
            f"https://api.rugcheck.xyz/v1/tokens/{address}/report/summary",
            headers={"User-Agent": "Mozilla/5.0 (tothemoon-bot)"}, timeout=8)
        if r.status_code != 200:
            return out
        data = r.json() or {}
        out["score"] = data.get("score_normalised")
        dangers = [x.get("name") for x in (data.get("risks") or []) if x.get("level") == "danger"]
        out["dangers"] = [d for d in dangers if d]
        if dangers:
            out["flagged"] = True
            out["reason"]  = "rugcheck flags " + ", ".join(d for d in dangers[:2] if d)
        elif out["score"] is not None and out["score"] >= CONFIG["safety"]["rugcheck_score_max"]:
            out["flagged"] = True
            out["reason"]  = f"rugcheck risk score {out['score']}/100 (high)"
    except Exception:
        pass
    out["_ts"] = now
    _rugcheck_cache[address] = out
    return out


def fetch_onchain_safety(address: str, chain: str) -> Dict[str, Any]:
    """On-chain rug checks: Solana rugcheck.xyz + holder concentration, EVM honeypot/sell-tax."""
    out: Dict[str, Any] = {"flagged": False, "reason": "", "dangers": []}
    if not address:
        return out
    if chain == "sol":
        # rugcheck.xyz is the primary Solana scam/rug signal (LP lock, mint authority,
        # holder distribution, known scams). Fall back to raw holder concentration if it's down.
        rc = fetch_rugcheck(address)
        if rc["flagged"]:
            return {"flagged": True, "reason": rc["reason"], "dangers": rc.get("dangers", [])}
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


def safety_gate(symbol: str, address: str, chain: str, hype: int = 0) -> "tuple[bool, str]":
    """Combined scam/rug check for a token the bot is about to buy. Returns (ok, reason_if_blocked).
    When hype >= 90: holder-concentration-only flags are overridden — new pump.fun launches
    almost always have concentrated holders; high hype signals real community demand."""
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
            # Hype override: if the ONLY dangers are holder concentration flags and
            # hype is 90+, allow it through — concentrated holders are normal on new launches.
            if hype >= 90 and oc.get("dangers"):
                non_conc = [d for d in oc["dangers"]
                            if d.lower() not in _HOLDER_CONCENTRATION_FLAGS]
                if not non_conc:
                    log(f"SAFETY OVERRIDE {symbol}: hype {hype} overrides holder-concentration flags ({oc['dangers']})")
                    return True, ""
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
    "You are the risk-mode controller for a crypto memecoin scalping bot (paper-trading). "
    "You pick ONE of four builtin risk profiles for the next ~30 minutes.\n\n"
    "Modes, least to most aggressive (TP = take-profit, SL = stop-loss, liq = min pool liquidity):\n"
    "- safe:    TP +15%,       SL -7%,  liq $50k+. Smallest bets (0.7×). Strict entry filter. Caps winners early.\n"
    "- default: TP +28%,       SL -12%, liq $30k+. Standard bets (1.0×). Balanced.\n"
    "- hype:    TP +45%/+90%,  SL -18%, liq $20k+. Larger bets (1.3×). Lets winners run further.\n"
    "- degen:   TP +80%/+150%, SL -28%, liq $15k+. Large bets (1.4×). Rides volatility; more rugs.\n\n"
    "CRITICAL — how this strategy makes money: it is ASYMMETRIC. Most trades lose a little, a FEW win big. "
    "A 40-55% win rate is NORMAL and HEALTHY — do NOT go conservative just because win rate looks low. "
    "What matters is EXPECTANCY. If avg_win >> avg_loss and expectancy is positive, stay in hype/degen.\n\n"
    "Read exit_reason_breakdown carefully:\n"
    "- high fixed_sl rate + low avg_loss → noise stops (SL too tight for the market, move to hype/degen)\n"
    "- high dollar_stop rate + big avg_loss → rug events on thin tokens (liq filter helps; stay hype/degen)\n"
    "- low TP_hit rate → entries too early or market weak (consider default/safe)\n"
    "- positive expectancy + any win rate → stay aggressive\n\n"
    "'safe' mode's +15% TP CAPS the big winners this strategy depends on. Only choose 'safe' when "
    "expectancy is genuinely negative AND it's not explained by noise stops. "
    "Otherwise prefer default or hype/degen. Pull back to 'default' (not 'safe') when market is BTC-dominated "
    "or candidates are thin.\n\n"
    "Return strict JSON only: {recommended_mode, confidence (0-1), aggressive (bool), reasoning (one sentence)}"
)


def _scout_reason_summary(n: int = 40) -> Dict[str, int]:
    """Tally recent scout decisions so the AI can see what the scanner is finding."""
    out: Dict[str, int] = {"entered": 0, "suggested": 0, "rejected": 0}
    for e in STATE.get("scout_log", [])[-n:]:
        out[e.get("decision", "rejected")] = out.get(e.get("decision", "rejected"), 0) + 1
    return out


def _ai_exit_breakdown(n: int = 50) -> Dict[str, Any]:
    """Summarize recent exit reasons so the AI understands WHY trades are winning/losing."""
    log_list = STATE.get("trade_log", [])
    recent_sells = [r for r in log_list if r.get("side") == "sell"][-n:]
    breakdown: Dict[str, Dict] = {}
    for r in recent_sells:
        reason_raw = r.get("exit_reason", "unknown") or "unknown"
        # Bucket into clean categories
        if reason_raw.startswith("TP"):
            key = "TP_hit"
        elif reason_raw.startswith("RUG"):
            key = "RUG_guard"
        elif reason_raw.startswith("fixed_sl"):
            key = "fixed_sl"
        elif reason_raw.startswith("DOLLAR STOP"):
            key = "dollar_stop"
        elif reason_raw.startswith("VELOCITY"):
            key = "velocity"
        elif reason_raw.startswith("TRAIL"):
            key = "trail_stop"
        else:
            key = "other"
        b = breakdown.setdefault(key, {"count": 0, "total_pnl": 0.0})
        b["count"] += 1
        b["total_pnl"] = round(b["total_pnl"] + (r.get("pnl") or 0), 2)
    # Add avg_pnl per bucket
    for b in breakdown.values():
        b["avg_pnl"] = round(b["total_pnl"] / b["count"], 2) if b["count"] else 0
    return breakdown


def _ai_market_context() -> Dict[str, Any]:
    hist     = STATE.get("pnl_hist", [])[-30:]
    wins_l   = [x for x in hist if x > 0]
    losses_l = [x for x in hist if x <= 0]
    wr       = (len(wins_l) / len(hist)) if hist else 0.0
    avg_win  = (sum(wins_l) / len(wins_l)) if wins_l else 0.0
    avg_loss = (sum(losses_l) / len(losses_l)) if losses_l else 0.0
    expectancy = wr * avg_win + (1 - wr) * avg_loss
    return {
        "current_mode":         CONFIG["mode"],
        "allowed_modes":        CONFIG["ai"].get("allowed_modes", ["safe", "default", "hype", "degen"]),
        "btc_dominance":        STATE["signals"].get("btc_d"),
        "market_heat":          STATE["signals"].get("heat"),
        "vault_usd":            round(STATE.get("vault_usd", 0), 2),
        "vault_start":          round(STATE.get("vault_start", 0), 2),
        "take_home_usd":        round(STATE.get("take_home_usd", 0), 2),
        "open_positions":       sum(1 for p in STATE.get("positions", {}).values() if p.get("units", 0) > 0),
        "recent_trades":        len(hist),
        "recent_win_rate":      round(wr, 2),
        "avg_win":              round(avg_win, 2),
        "avg_loss":             round(avg_loss, 2),
        "expectancy_per_trade": round(expectancy, 2),
        "biggest_recent_win":   round(max(hist), 2) if hist else 0,
        "recent_pnl":           round(sum(hist), 2),
        "exit_reason_breakdown": _ai_exit_breakdown(50),
        "scout_last_40":        _scout_reason_summary(40),
        "drawdown_brake":       drawdown_brake_active(),
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
    _allowed = A.get("allowed_modes", ["safe", "default", "hype", "degen"])
    # Enforce whitelist — AI must not switch into custom modes with unknown/tight SL params
    if rec not in _allowed:
        log(f"AI recommended '{rec}' but it's not in allowed_modes {_allowed} — ignored")
        rec = CONFIG["mode"]   # stay put
        decision["recommended_mode"] = rec
        decision["applied"] = False
        return decision
    applied = False
    if (A.get("auto_apply") and rec in CONFIG["modes"]
            and rec != CONFIG["mode"] and decision["confidence"] >= A.get("min_confidence", 0.7)):
        old = CONFIG["mode"]
        CONFIG["mode"] = rec
        STATE["active_mode"] = rec
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
        "vault_usd":      STATE["vault_usd"],
        "vault_start":    STATE.get("vault_start", STATE["vault_usd"]),
        "take_home_usd":  round(STATE.get("take_home_usd", 0.0), 2),
        "deployable":     deployable_now(),
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
        **{k: v for k, v in STATE.items() if k not in ("trade_log", "liq_prev", "scout_log", "wallet_goal")},
        "wallet_goal": {**STATE.get("wallet_goal", {}), "payout_log": STATE.get("wallet_goal", {}).get("payout_log", [])[-20:]},
        "trade_count":     sum(1 for t in STATE.get("trade_log", []) if not _is_ghost_sell(t)),
        "deployable_usd":  deployable_now(),
        "mode":            CONFIG["mode"],
        "modes":           list(CONFIG["modes"].keys()),
        "mode_tp":         mode_cfg.get("tp", []),
        "mode_sl":         mode_cfg.get("sl", 0.0),
        "mode_size_mult":  mode_cfg.get("size_mult", 1.0),
        "loss_cooldown_min": CONFIG["moonshot"].get("loss_cooldown_min", 30),
        "seed_pct":        int(CONFIG["moonshot"].get("seed_pct", 0.40) * 100),
        "per_token_cap_pct": CONFIG.get("per_token_cap_pct", 0.12),
        "sweep_enabled":   bool(os.getenv("SWEEP_SOL_ADDRESS", "").strip() and float(os.getenv("SWEEP_ABOVE_USD", "0") or 0) > 0),
        "sweep_above_usd": float(os.getenv("SWEEP_ABOVE_USD", "0") or 0),
        "sweep_keep_usd":  float(os.getenv("SWEEP_KEEP_USD", "200") or 200),
        "total_swept_usd": STATE.get("total_swept_usd", 0.0),
        "last_sweep":      (STATE.get("sweep_log") or [None])[-1],
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
            "dollar_stop_usd": CONFIG["moonshot"].get("dollar_stop_usd", 12.0),
            "dynamic_sizing":  CONFIG["moonshot"].get("dynamic_sizing", False),
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


_history_cache: Dict[str, Any] = {}   # {n_trades, ts_last, payload}
_backtest_cache: Dict[str, Any] = {}  # same pattern

def _is_ghost_sell(t: Dict) -> bool:
    """Ghost sells: dollar_stop firing on an already-closed position (floating-point dust).
    Identified by near-zero proceeds (just gas deduction, no real units sold).
    These corrupt win_rate and avg_loss stats and must be excluded from all analytics."""
    return (t.get("side") == "sell"
            and abs(t.get("usd", 0) or 0) < 0.01
            and abs(t.get("pnl", 0) or 0) < 0.01)


@app.route("/api/history")
@_dash_auth
def api_history():
    trades  = STATE.get("trade_log", [])
    n = len(trades)
    if _history_cache.get("n") == n and _history_cache.get("payload"):
        return _history_cache["payload"]
    sells   = [t for t in trades if t.get("side") == "sell" and t.get("pnl") is not None
               and not _is_ghost_sell(t)]
    wins    = [t for t in sells if t["pnl"] > 0]
    losses  = [t for t in sells if t["pnl"] <= 0]
    running = 0.0
    enriched: List[Dict] = []
    for t in trades:
        if t.get("side") == "sell" and t.get("pnl") is not None and not _is_ghost_sell(t):
            running += t["pnl"]
        enriched.append({**t, "running_pnl": running, "ghost": _is_ghost_sell(t)})
    # $100 runner — replay every actual trade starting with $100.
    # Same dollar bet sizes as the real run. Every sell's proceeds go straight
    # back into the pool and fund the next bet — that's the compounding.
    # As the sim vault grows past the original vault, bets scale up too.
    _r_start     = 100.0
    _r_vault     = _r_start
    _r_min_cash  = _r_start
    _r_min_total = _r_start
    _r_max       = _r_start
    _r_skipped   = 0
    _r_hist: List[Dict] = []
    _r_open: Dict[str, float] = {}   # symbol -> sim cost basis
    _real_ref = max(STATE.get("vault_start") or 1000.0, 1.0)
    for t in trades:
        sym  = t.get("symbol", "")
        side = t.get("side")
        usd  = t.get("usd", 0) or 0
        pnl  = t.get("pnl", 0) or 0
        if side == "buy":
            # Fixed bet size while below original vault; scale up once above it.
            # Below $1000: bet same $ as real run (more aggressive % — that's the punt).
            # Above $1000: scale bet up with vault so wins keep compounding.
            total_sim = _r_vault + sum(_r_open.values())
            sim_usd   = usd if total_sim <= _real_ref else usd * (total_sim / _real_ref)
            if _r_vault >= sim_usd:
                _r_vault -= sim_usd
                _r_open[sym] = _r_open.get(sym, 0) + sim_usd
            else:
                _r_skipped += 1
                continue
        elif side == "sell":
            if sym not in _r_open:
                continue
            # Return proceeds scaled by how much sim cost vs real cost
            sim_cost  = _r_open.get(sym, usd)
            cost_real = usd - pnl   # original cost portion of this sell
            scale     = sim_cost / max(cost_real, 1e-9) if cost_real > 0 else 1.0
            sim_proc  = usd * min(scale, _real_ref)   # cap scale so we don't explode
            _r_vault += sim_proc
            remaining = max(0.0, _r_open.get(sym, 0) - sim_cost)
            if remaining <= 0:
                _r_open.pop(sym, None)
            else:
                _r_open[sym] = remaining
        _r_vault     = max(0.0, _r_vault)
        _total       = _r_vault + sum(_r_open.values())
        _r_min_cash  = min(_r_min_cash, _r_vault)
        _r_min_total = min(_r_min_total, _total)
        _r_max       = max(_r_max, _total)
        _r_hist.append({"ts": t.get("ts", ""), "vault": round(_total, 2)})

    resp = jsonify({
        "trades":    enriched,
        "total_pnl": running,
        "win_rate":  len(wins) / max(1, len(sells)),
        "avg_win":   sum(t["pnl"] for t in wins)   / max(1, len(wins)),
        "avg_loss":  sum(t["pnl"] for t in losses) / max(1, len(losses)),
        "runner_100": {
            "start":    _r_start,
            "end":      round(_r_vault + sum(_r_open.values()), 2),
            "min_cash": round(_r_min_cash, 2),
            "min":      round(_r_min_total, 2),
            "max":      round(_r_max, 2),
            "skipped":  _r_skipped,
            "hist":     _r_hist,
        },
    })
    _history_cache["n"]       = n
    _history_cache["payload"] = resp
    return resp


_sim100_cache: Dict[str, Any] = {}

@app.route("/api/sim100")
@_dash_auth
def api_sim100():
    """Simulate: what would have happened going live at trade[0] with $100 + current fixed code.

    Uses:
    - Seed mode sizing: bet = min(real_bet, avail_cash × 40%), $10 floor, 1.5× buffer gate
    - 30-min post-loss cooldown blocking re-entries on same token
    - Dollar stop: cap any loss at max($12, 10% of sim deployed cost)
    - Full compounding: all proceeds go back into the pool
    """
    trades = [t for t in STATE.get("trade_log", []) if not _is_ghost_sell(t)]
    n = len(trades)
    if _sim100_cache.get("n") == n and _sim100_cache.get("payload"):
        return _sim100_cache["payload"]

    dollar_stop = CONFIG["moonshot"].get("dollar_stop_usd", 12.0)

    def _ts(t):
        try:
            return float(t.get("ts_epoch") or 0) or \
                __import__("datetime").datetime.fromisoformat(
                    t["ts"].replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0

    START       = 100.0
    vault       = START
    open_pos: Dict[str, float] = {}   # sym -> sim cost basis currently deployed
    last_loss_exit: Dict[str, float] = {}  # sym -> exit ts (losses only)
    entries_today: Dict[str, int] = {}    # sym -> buy count today (anti-churn)
    last_day: str = ""
    running     = 0.0
    skipped     = 0
    blocked     = 0
    capped      = 0
    hist: List[Dict] = []
    peak        = START
    trough      = START
    trades_out: List[Dict] = []

    PER_TOKEN_CAP_PCT = CONFIG.get("per_token_cap_pct", 0.12)
    ENTRY_CAP = CONFIG["modes"].get(CONFIG["mode"], {}).get(
        "max_entries_per_token_day", CONFIG.get("max_entries_per_token_day", 4))

    for t in trades:
        sym  = t.get("symbol", "") or "?"
        side = t.get("side")
        usd  = float(t.get("usd") or 0)
        pnl  = t.get("pnl")
        ts   = _ts(t)

        # Null-symbol skip — mirrors scanner fix
        if sym == "?":
            if side == "buy":
                blocked += 1
            trades_out.append({**t, "sim_status": "blocked", "sim_usd": 0, "sim_pnl": None})
            continue

        # Daily reset for anti-churn counter
        trade_day = t.get("ts", "")[:10]
        if trade_day != last_day:
            entries_today = {}
            last_day = trade_day

        total_now = vault + sum(open_pos.values())

        if side == "buy":
            # 1. Post-loss cooldown check
            last_loss = last_loss_exit.get(sym, 0)
            if last_loss and ts - last_loss < 30 * 60:
                blocked += 1
                trades_out.append({**t, "sim_status": "blocked", "sim_usd": 0, "sim_pnl": None})
                continue

            # 2. Anti-churn: cap entries per token per day
            if entries_today.get(sym, 0) >= ENTRY_CAP:
                blocked += 1
                trades_out.append({**t, "sim_status": "blocked", "sim_usd": 0, "sim_pnl": None})
                continue

            # 3. Per-token cap: 12% of real-vault equivalent (sim × 10 since we start at $100 vs $1000).
            # Using raw sim total makes the cap $12 on a $100 start — too tight to allow even one buy.
            token_cap = PER_TOKEN_CAP_PCT * (total_now * 10)
            cap_room  = max(0.0, token_cap - open_pos.get(sym, 0.0))
            if cap_room < 10.0:
                blocked += 1
                trades_out.append({**t, "sim_status": "blocked", "sim_usd": 0, "sim_pnl": None})
                continue

            # 4. Seed mode sizing
            avail   = max(0.0, vault)
            sim_usd = min(usd, avail * 0.40, cap_room)
            if sim_usd < 10.0 or avail < sim_usd * 1.5:
                skipped += 1
                trades_out.append({**t, "sim_status": "skipped", "sim_usd": 0, "sim_pnl": None})
                continue

            entries_today[sym] = entries_today.get(sym, 0) + 1
            vault -= sim_usd
            open_pos[sym] = open_pos.get(sym, 0.0) + sim_usd
            total_now = vault + sum(open_pos.values())
            peak   = max(peak, total_now)
            trough = min(trough, total_now)
            hist.append({"ts": t.get("ts", ""), "vault": round(total_now, 2)})
            trades_out.append({**t, "sim_status": "kept", "sim_usd": round(sim_usd, 4), "sim_pnl": None})

        elif side == "sell" and pnl is not None:
            sim_cost = open_pos.get(sym)
            if sim_cost is None or sim_cost <= 0:
                trades_out.append({**t, "sim_status": "no_pos", "sim_usd": 0, "sim_pnl": None})
                continue

            # Scale pnl proportionally to sim position size vs real position size
            cost_real = max(usd - pnl, 1e-9)
            scale     = sim_cost / cost_real
            raw_pnl   = pnl * scale

            # 3. Dollar stop cap
            eff_stop = max(dollar_stop, sim_cost * 0.10)
            if raw_pnl < -eff_stop:
                sim_pnl = -eff_stop
                capped += 1
                status  = "capped"
            else:
                sim_pnl = raw_pnl
                status  = "kept"

            sim_proc = sim_cost + sim_pnl   # cash back into vault
            vault   += sim_proc
            running += sim_pnl
            open_pos.pop(sym, None)

            if pnl < 0:
                last_loss_exit[sym] = ts

            total_now = vault + sum(open_pos.values())
            peak   = max(peak, total_now)
            trough = min(trough, total_now)
            hist.append({"ts": t.get("ts", ""), "vault": round(total_now, 2)})
            trades_out.append({**t, "sim_status": status,
                                "sim_usd": round(sim_cost, 4),
                                "sim_pnl": round(sim_pnl, 4)})
        else:
            trades_out.append({**t, "sim_status": "kept", "sim_usd": 0, "sim_pnl": None})

    end_total = vault + sum(open_pos.values())
    sim_sells = [t for t in trades_out if t.get("side") == "sell" and t.get("sim_pnl") is not None
                 and t.get("sim_status") not in ("no_pos",)]
    sim_wins  = [t for t in sim_sells if t["sim_pnl"] > 0]
    sim_losses= [t for t in sim_sells if t["sim_pnl"] <= 0]

    resp = jsonify({
        "start":       START,
        "end":         round(end_total, 2),
        "peak":        round(peak, 2),
        "trough":      round(trough, 2),
        "pct":         round((end_total - START) / START * 100, 1),
        "running_pnl": round(running, 2),
        "n_blocked":   blocked,
        "n_skipped":   skipped,
        "n_capped":    capped,
        "win_rate":    len(sim_wins) / max(1, len(sim_sells)),
        "avg_win":     sum(t["sim_pnl"] for t in sim_wins)    / max(1, len(sim_wins)),
        "avg_loss":    sum(t["sim_pnl"] for t in sim_losses)  / max(1, len(sim_losses)),
        "hist":        hist,
        "trades":      trades_out,
    })
    _sim100_cache["n"]       = n
    _sim100_cache["payload"] = resp
    return resp


@app.route("/api/backtest")
@_dash_auth
def api_backtest():
    """Replay trade history with current bot fixes applied from day one.

    Fixes simulated:
      1. Post-exit cooldown: 30-min block after a loss exit; 15-min block after
         a profit exit (no pullback data so we conservatively block the re-entry).
      2. Dollar stop: any sell with loss worse than max($12, 10% of deployed)
         is capped — we can't know the exact tick, so we replace the loss with
         the dollar-stop limit.

    Returns per-trade status ('kept'/'blocked'/'capped') plus revised stats.
    """
    trades   = [t for t in STATE.get("trade_log", []) if not _is_ghost_sell(t)]
    n = len(trades)
    if _backtest_cache.get("n") == n and _backtest_cache.get("payload"):
        return _backtest_cache["payload"]
    dollar_stop = CONFIG["moonshot"].get("dollar_stop_usd", 12.0)
    cooldown_loss_sec   = 30 * 60
    cooldown_profit_sec = 15 * 60

    # ── pass 1: mark blocked buys (post-exit cooldown) ──────────────────────
    last_exit: Dict[str, Dict] = {}   # symbol -> {ts, pnl}
    blocked_buys: set = set()         # indices of blocked buys

    for i, t in enumerate(trades):
        sym  = t.get("symbol", "")
        side = t.get("side")
        try:
            ts = float(t.get("ts_epoch", 0)) or \
                 __import__("datetime").datetime.fromisoformat(
                     t["ts"].replace("Z", "+00:00")).timestamp()
        except Exception:
            ts = 0.0

        if side == "buy":
            ex = last_exit.get(sym)
            if ex and ex["pnl"] < 0:
                elapsed = ts - ex["ts"]
                if elapsed < cooldown_loss_sec:
                    blocked_buys.add(i)
            # Note: profit cooldown omitted — we can't distinguish a partial TP
            # (bot still holding) from a full exit in the trade log. Blocking on
            # profit exits incorrectly blocks scale-ins on hot winners.
        elif side == "sell" and t.get("pnl") is not None:
            # Only update last_exit if this looks like a loss (to avoid catching partial TPs)
            if t["pnl"] < 0:
                last_exit[sym] = {"ts": ts, "pnl": t["pnl"]}

    # ── pass 2: build revised trade list ────────────────────────────────────
    # Track which symbols have a blocked buy outstanding so we skip their sells too
    blocked_symbols_open: Dict[str, int] = {}  # symbol -> count of blocked open lots

    revised: List[Dict] = []
    sim_vault = STATE.get("vault_start") or 1000.0
    sim_running = 0.0
    deployed: Dict[str, float] = {}   # symbol -> sim deployed usd

    for i, t in enumerate(trades):
        sym   = t.get("symbol", "")
        side  = t.get("side")
        usd   = t.get("usd", 0) or 0
        pnl   = t.get("pnl")

        if side == "buy":
            if i in blocked_buys:
                blocked_symbols_open[sym] = blocked_symbols_open.get(sym, 0) + 1
                revised.append({**t, "sim_status": "blocked", "sim_pnl": 0, "sim_running": sim_running})
                continue
            deployed[sym] = deployed.get(sym, 0) + usd
            sim_vault -= usd
            revised.append({**t, "sim_status": "kept", "sim_pnl": 0, "sim_running": sim_running})

        elif side == "sell" and pnl is not None:
            # If the corresponding buy was blocked, skip this sell
            if blocked_symbols_open.get(sym, 0) > 0:
                blocked_symbols_open[sym] -= 1
                if blocked_symbols_open[sym] <= 0:
                    blocked_symbols_open.pop(sym, None)
                revised.append({**t, "sim_status": "blocked", "sim_pnl": 0, "sim_running": sim_running})
                continue

            dep = deployed.get(sym, usd - pnl)
            effective_stop = max(dollar_stop, dep * 0.10)
            if pnl < -effective_stop:
                # Dollar stop would have fired — cap the loss
                sim_pnl = -effective_stop
                status  = "capped"
            else:
                sim_pnl = pnl
                status  = "kept"

            sim_running += sim_pnl
            sim_vault   += (usd - pnl) + sim_pnl   # return cost + capped loss
            deployed[sym] = max(0.0, dep - (usd - pnl))
            revised.append({**t, "sim_status": status, "sim_pnl": round(sim_pnl, 4),
                             "sim_running": round(sim_running, 4)})
        else:
            revised.append({**t, "sim_status": "kept", "sim_pnl": 0, "sim_running": sim_running})

    # ── summary stats ────────────────────────────────────────────────────────
    sim_sells  = [r for r in revised if r.get("side") == "sell" and r["sim_status"] != "blocked"
                  and r.get("sim_pnl") is not None]
    sim_wins   = [r for r in sim_sells if r["sim_pnl"] > 0]
    sim_losses = [r for r in sim_sells if r["sim_pnl"] <= 0]
    n_blocked  = sum(1 for r in revised if r.get("sim_status") == "blocked" and r.get("side") == "buy")
    n_capped   = sum(1 for r in revised if r.get("sim_status") == "capped")
    # Capped: how much the dollar stop saved (positive = saved money)
    saved_by_cap = sum(r["sim_pnl"] - r["pnl"]
                       for r in revised
                       if r.get("sim_status") == "capped" and r.get("pnl") is not None)
    # Cooldown: losses dodged (blocked sells with negative real pnl) vs wins missed (positive real pnl)
    blocked_sells     = [r for r in revised if r.get("sim_status") == "blocked" and r.get("side") == "sell"
                         and r.get("pnl") is not None]
    losses_dodged_usd = sum(-r["pnl"] for r in blocked_sells if r["pnl"] < 0)   # positive = saved
    wins_missed_usd   = sum(r["pnl"]  for r in blocked_sells if r["pnl"] > 0)   # positive = cost
    saved             = round(losses_dodged_usd - wins_missed_usd + saved_by_cap, 2)

    real_total = sum(t["pnl"] for t in trades if t.get("side") == "sell" and t.get("pnl") is not None)

    resp = jsonify({
        "trades":        revised,
        "real_total":    round(real_total, 2),
        "sim_total":     round(sim_running, 2),
        "difference":    round(sim_running - real_total, 2),
        "win_rate":      len(sim_wins)   / max(1, len(sim_sells)),
        "avg_win":       sum(r["sim_pnl"] for r in sim_wins)   / max(1, len(sim_wins)),
        "avg_loss":      sum(r["sim_pnl"] for r in sim_losses) / max(1, len(sim_losses)),
        "real_win_rate": len([t for t in trades if t.get("side")=="sell" and t.get("pnl",0)>0])
                         / max(1, len([t for t in trades if t.get("side")=="sell" and t.get("pnl") is not None])),
        "n_blocked":          n_blocked,
        "n_capped":           n_capped,
        "losses_dodged_usd":  round(losses_dodged_usd, 2),
        "wins_missed_usd":    round(wins_missed_usd, 2),
        "saved_by_cap_usd":   round(saved_by_cap, 2),
        "saved_usd":          round(saved, 2),
    })
    _backtest_cache["n"]       = n
    _backtest_cache["payload"] = resp
    return resp


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
    STATE["active_mode"] = m   # persist so restarts don't revert to BOT_MODE env
    # If switching to a custom mode, apply its stored global params too
    custom = STATE.get("custom_modes", {}).get(m)
    if custom:
        _apply_custom_mode_globals(custom)
    save_state()
    return jsonify({"ok": True, "mode": m})


def _register_custom_mode(name: str, p: dict):
    """Build a full mode config from saved btParams and add to CONFIG["modes"]."""
    base = CONFIG["modes"].get("hype", {})
    CONFIG["modes"][name] = {
        "tp":                       list(base.get("tp", [0.45, 0.9])),
        "sl":                       float(p.get("sl_pct", 18)) / 100.0,
        "dollar_stop_usd":          float(p.get("dollar_stop", _BASE_DOLLAR_STOP)),
        "slip_bps":                 int(base.get("slip_bps", 150)),
        "liq_min":                  int(base.get("liq_min", 20000)),
        "max_age_min":              int(base.get("max_age_min", 120)),
        "max_entries_per_token_day": int(base.get("max_entries_per_token_day", 4)),
        "size_mult":                float(p.get("size_mult", 1.3)),
        "_custom": True,
    }


def _apply_custom_mode_globals(p: dict):
    """Apply the non-mode-dict params (cooldown, per_token_cap, etc.) from a custom mode.

    dollar_stop is intentionally NOT written to CONFIG["moonshot"]["dollar_stop_usd"] —
    exit logic uses _BASE_DOLLAR_STOP / _mode_dollar_stop(), and the arena reads it
    directly from the custom mode dict via _arena_mode_params(). Mutating the global
    would corrupt the dashboard display and the position cap for all other modes.
    """
    if "per_token_cap" in p:
        cap = float(p["per_token_cap"])
        CONFIG["per_token_cap_pct"] = (cap / (STATE.get("vault_usd", 1000) * 10)) if cap > 0 else 1.0
    if "cooldown_min" in p:
        CONFIG["moonshot"]["loss_cooldown_min"] = max(0, int(p["cooldown_min"]))
    if "seed_pct" in p:
        CONFIG["moonshot"]["seed_pct"] = float(p["seed_pct"]) / 100.0


@app.route("/api/custom_modes", methods=["GET"])
@_dash_auth
def api_list_custom_modes():
    return jsonify({"modes": STATE.get("custom_modes", {})})


@app.route("/api/custom_modes", methods=["POST"])
@_dash_auth
def api_save_custom_mode():
    data = flask_request.get_json() or {}
    name = (data.get("name") or "").strip()
    params = data.get("params")
    if not name or not params:
        return jsonify({"error": "name and params required"}), 400
    if len(name) > 40:
        return jsonify({"error": "name too long (max 40 chars)"}), 400
    if name in ("safe", "default", "hype", "degen", "seed"):
        return jsonify({"error": "cannot overwrite a built-in mode"}), 400
    STATE.setdefault("custom_modes", {})[name] = params
    _register_custom_mode(name, params)
    save_state()
    return jsonify({"ok": True, "name": name})


@app.route("/api/custom_modes/<name>", methods=["DELETE"])
@_dash_auth
def api_delete_custom_mode(name):
    modes = STATE.get("custom_modes", {})
    if name not in modes:
        return jsonify({"error": "not found"}), 404
    del modes[name]
    CONFIG["modes"].pop(name, None)
    if CONFIG["mode"] == name:
        CONFIG["mode"] = "hype"
    save_state()
    return jsonify({"ok": True})


@app.route("/api/mode/push", methods=["POST"])
@_dash_auth
def api_mode_push():
    """Push backtester parameters onto an existing built-in mode's config.
    Only touches fields that are explicitly sent — doesn't clobber unrelated settings."""
    data = request.get_json() or {}
    mode = data.get("mode", "").strip()
    BUILT_INS = {"hype", "degen", "default", "seed", "safe", "micro"}
    if mode not in BUILT_INS:
        return jsonify({"error": f"unknown mode '{mode}' — must be one of {sorted(BUILT_INS)}"}), 400

    m = CONFIG["modes"].setdefault(mode, {})
    ALLOWED = {
        "sl", "size_mult", "dollar_stop_usd", "loss_cooldown_min",
        "m5_min", "buy_ratio_min", "velocity_exit_pct", "rug_liq_drop",
    }
    updated = {}
    for key in ALLOWED:
        if key in data:
            val = data[key]
            try:
                val = float(val)
            except (TypeError, ValueError):
                continue
            m[key] = val
            updated[key] = val

    save_state()
    log(f"PUSH mode '{mode}': {updated}")
    return jsonify({"ok": True, "mode": mode, "updated": updated})


@app.route("/api/arenas", methods=["GET"])
@_dash_auth
def api_arenas():
    arenas  = STATE.get("arenas", {})
    result  = {}
    all_names = set(CONFIG["modes"].keys()) | set(STATE.get("custom_modes", {}).keys())
    for mode_name in all_names:
        arena  = arenas.get(mode_name, {})
        trades = arena.get("trades", [])
        wins   = [t for t in trades if t.get("pnl", 0) > 0]
        losses = [t for t in trades if t.get("pnl", 0) <= 0]
        net    = sum(t.get("pnl", 0) for t in trades)
        sv     = arena.get("start_vault", arena.get("vault", 0))
        open_p = len(arena.get("positions", {}))
        # unrealized PnL on open positions (approx: value at entry price since we don't have live price here)
        result[mode_name] = {
            "vault":         arena.get("vault", None),
            "start_vault":   sv,
            "net_pnl":       round(net, 2),
            "pct_return":    round(net / sv * 100, 2) if sv else 0,
            "trade_count":   len(trades),
            "win_count":     len(wins),
            "loss_count":    len(losses),
            "win_rate":      round(len(wins) / len(trades) * 100, 1) if trades else 0,
            "avg_win":       round(sum(t["pnl"] for t in wins)   / len(wins),   2) if wins   else 0,
            "avg_loss":      round(sum(t["pnl"] for t in losses) / len(losses), 2) if losses else 0,
            "open_positions": open_p,
            "started":       arena.get("started", None),
            "is_custom":     mode_name in STATE.get("custom_modes", {}),
        }
    return jsonify(result)


@app.route("/api/arenas/<name>/reset", methods=["POST"])
@_dash_auth
def api_arena_reset(name):
    arenas = STATE.setdefault("arenas", {})
    arenas.pop(name, None)
    save_state()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Manual override / reset controls
# ---------------------------------------------------------------------------

@app.route("/api/blocks", methods=["GET"])
@_dash_auth
def api_blocks():
    """Return status + reasoning for every active trading block."""
    now_ts = time.time()
    db     = CONFIG["drawdown_brake"]
    hist   = STATE["pnl_hist"][-db["lookback"]:]
    gains  = sum(x for x in hist if x > 0)
    losses = sum(x for x in hist if x < 0)
    net    = gains + losses
    brake_active = drawdown_brake_active()

    # Per-token cooldowns — main bot
    cooldown_sec = int(CONFIG["moonshot"].get("loss_cooldown_min", 30)) * 60
    main_cooldowns = []
    for sym, ex in STATE.get("recently_exited", {}).items():
        elapsed = now_ts - ex.get("ts", now_ts)
        remaining = cooldown_sec - elapsed
        if remaining > 0:
            main_cooldowns.append({
                "symbol":    sym,
                "pnl":       round(ex.get("pnl", 0), 2),
                "exit_ts":   ex.get("ts", 0),
                "remaining_sec": int(remaining),
                "reason":    ex.get("exit_reason", "loss exit"),
            })

    # Per-wallet cooldowns
    wallet_cooldowns = []
    for wid, w in STATE.get("wallets", {}).items():
        for sym, ex in w.get("recently_exited", {}).items():
            elapsed   = now_ts - ex.get("ts", now_ts)
            remaining = cooldown_sec - elapsed
            if remaining > 0:
                wallet_cooldowns.append({
                    "wid":     wid,
                    "label":   w.get("label", wid),
                    "symbol":  sym,
                    "pnl":     round(ex.get("pnl", 0), 2),
                    "remaining_sec": int(remaining),
                    "reason":  ex.get("exit_reason", "loss exit"),
                })

    # Vault floor
    vault_start = STATE.get("vault_start", STATE["vault_usd"])
    floor_usd   = vault_start * CONFIG.get("absolute_floor_pct", 0.25)
    at_floor    = STATE["vault_usd"] <= floor_usd
    wallet_floors = []
    for wid, w in STATE.get("wallets", {}).items():
        w_start = w.get("starting_usd", w["vault_usd"])
        w_floor = w_start * CONFIG.get("absolute_floor_pct", 0.25)
        if w["vault_usd"] <= w_floor:
            wallet_floors.append({
                "wid":         wid,
                "label":       w.get("label", wid),
                "vault_usd":   round(w["vault_usd"], 2),
                "floor_usd":   round(w_floor, 2),
                "topup_needed": round(w_floor - w["vault_usd"] + 1, 2),
            })

    return jsonify({
        "brake": {
            "active":       brake_active,
            "net_pnl":      round(net, 2),
            "gains":        round(gains, 2),
            "losses":       round(losses, 2),
            "threshold_pct": db["dd"] * 100,
            "lookback":     db["lookback"],
            "trades_in_window": len(hist),
            "reason": (
                f"Net loss ${abs(net):.2f} across last {len(hist)} trades "
                f"({abs(net)/max(gains,0.01)*100:.0f}% of gains — threshold {db['dd']*100:.0f}%)"
                if brake_active else
                f"Net P&L ${net:+.2f} across last {len(hist)} trades — below {db['dd']*100:.0f}% drawdown threshold"
            ),
        },
        "cooldowns": {
            "main_bot": sorted(main_cooldowns, key=lambda x: -x["remaining_sec"]),
            "wallets":  sorted(wallet_cooldowns, key=lambda x: -x["remaining_sec"]),
        },
        "vault_floors": {
            "main_at_floor": at_floor,
            "main_vault":    round(STATE["vault_usd"], 2),
            "main_floor":    round(floor_usd, 2),
            "wallets":       wallet_floors,
        },
    })


@app.route("/api/reset/brake", methods=["POST"])
@_dash_auth
def api_reset_brake():
    """Clear pnl_hist so the drawdown brake deactivates immediately."""
    STATE["pnl_hist"] = []
    for w in STATE.get("wallets", {}).values():
        w["pnl_hist"] = []
    save_state()
    log("MANUAL: drawdown brake reset — pnl_hist cleared on main bot + all wallets")
    return jsonify({"ok": True, "msg": "Drawdown brake cleared"})


@app.route("/api/reset/cooldowns", methods=["POST"])
@_dash_auth
def api_reset_cooldowns():
    """Clear all per-token loss cooldowns on the main bot and every wallet."""
    STATE["recently_exited"] = {}
    for w in STATE.get("wallets", {}).values():
        w["recently_exited"] = {}
    save_state()
    log("MANUAL: all loss cooldowns cleared (main bot + wallets)")
    return jsonify({"ok": True, "msg": "All cooldowns cleared"})


@app.route("/api/wallets/<wid>/reset/cooldowns", methods=["POST"])
@_dash_auth
def api_wallet_reset_cooldowns(wid):
    """Clear loss cooldowns for one wallet only."""
    w = STATE.get("wallets", {}).get(wid)
    if not w:
        return jsonify({"error": "wallet not found"}), 404
    w["recently_exited"] = {}
    save_state()
    log(f"MANUAL: cooldowns cleared for wallet {w.get('label', wid)}")
    return jsonify({"ok": True})


@app.route("/api/wallets/bulk", methods=["POST"])
@_dash_auth
def api_wallets_bulk():
    """Bulk actions applied to all wallets + main bot state at once."""
    data   = flask_request.get_json() or {}
    action = data.get("action", "")
    wallets = STATE.get("wallets", {})

    if action in ("clear_cooldowns", "clear_all"):
        STATE["recently_exited"]  = {}
        STATE["entries_today"]    = {}
        STATE["token_pnl_today"]  = {}
        for w in wallets.values():
            w["recently_exited"] = {}
            w["entries_today"]   = {}
        if action == "clear_all":
            STATE["reject_cache"] = {}
        save_state()
        log("MANUAL: all cooldowns + entry caps cleared (bulk)")
        return jsonify({"ok": True, "action": action})

    if action == "clear_entry_caps":
        # Reset daily entry counts and cumulative loss tally — lets the bot re-enter tokens
        # it hit the per-day cap on, without touching the loss cooldown timers.
        STATE["entries_today"]   = {}
        STATE["token_pnl_today"] = {}
        for w in wallets.values():
            w["entries_today"] = {}
        save_state()
        log("MANUAL: entry caps + loss tallies reset (bulk)")
        return jsonify({"ok": True, "action": action})

    if action == "clear_reject_cache":
        STATE["reject_cache"] = {}
        save_state()
        log("MANUAL: reject cache cleared — all tokens get a fresh look")
        return jsonify({"ok": True, "action": action})

    if action == "set_mode":
        mode = data.get("mode", "")
        if mode not in CONFIG["modes"]:
            return jsonify({"error": f"unknown mode '{mode}'"}), 400
        for w in wallets.values():
            w["mode"] = mode
        save_state()
        log(f"MANUAL: all wallet modes set to '{mode}' (bulk)")
        return jsonify({"ok": True, "action": action, "mode": mode})

    return jsonify({"error": f"unknown action '{action}'"}), 400


@app.route("/api/reset/vault_floor", methods=["POST"])
@_dash_auth
def api_reset_vault_floor():
    """Reset vault_start so the 25% hard floor recalculates from current balance.
    Use after topping up a wallet or after a code change that changes vault accounting."""
    data = flask_request.get_json() or {}
    wid  = data.get("wid")  # optional — if set, reset only that wallet
    if wid:
        w = STATE.get("wallets", {}).get(wid)
        if not w:
            return jsonify({"error": "wallet not found"}), 404
        w["starting_usd"] = w["vault_usd"]
        save_state()
        log(f"MANUAL: vault floor reset for {w.get('label', wid)} → starting_usd={w['vault_usd']:.2f}")
        return jsonify({"ok": True, "new_floor": w["vault_usd"] * 0.25})
    else:
        STATE["vault_start"] = STATE["vault_usd"]
        save_state()
        log(f"MANUAL: main-bot vault floor reset → vault_start={STATE['vault_usd']:.2f}")
        return jsonify({"ok": True, "new_floor": STATE["vault_usd"] * 0.25})


# ---------------------------------------------------------------------------
# Multi-wallet API
# ---------------------------------------------------------------------------

@app.route("/api/wallets", methods=["GET"])
@_dash_auth
def api_wallets_list():
    result = {}
    for wid, w in STATE.get("wallets", {}).items():
        open_pos = {s: p for s, p in w.get("positions", {}).items() if p.get("units", 0) > 0}
        sells    = [t for t in w.get("trade_log", []) if t.get("side") == "sell" and t.get("pnl") is not None]
        wins     = [t for t in sells if (t.get("pnl") or 0) > 0]
        net_pnl  = sum(t.get("pnl") or 0 for t in sells)
        floor_pct    = CONFIG.get("absolute_floor_pct", 0.25)
        at_floor     = w.get("vault_usd", 0) <= w.get("starting_usd", 1) * floor_pct
        result[wid] = {
            "wid":          wid,
            "label":        w.get("label", wid),
            "address":      w.get("address", ""),
            "sweep_address":  w.get("sweep_address", ""),
            "ticket_cap_usd": w.get("ticket_cap_usd", 0.0),
            "safety":         w.get("safety", {}),
            "mode":           w.get("mode") or CONFIG["mode"],
            "active":       w.get("active", True),
            "live":         w.get("live", False),
            "keypair_loaded": wid in _wallet_keypairs,
            "at_floor":     at_floor,
            "starting_usd": w.get("starting_usd", 0),
            "vault_usd":    round(w.get("vault_usd", 0), 2),
            "net_pnl":      round(net_pnl, 2),
            "trade_count":  len(sells),
            "win_rate":     round(len(wins) / max(1, len(sells)) * 100, 1),
            "open_positions": len(open_pos),
            "open_pos_names": list(open_pos.keys()),
            "open_pos_usd":  round(sum(p.get("usd", 0) for p in open_pos.values()), 2),
            "created":      w.get("created", ""),
            "take_home_usd":  round(w.get("take_home_usd", 0.0), 2),
            "total_swept_usd": round(w.get("total_swept_usd", 0.0), 2),
            "sweep_log":    (w.get("sweep_log") or [])[-20:],
        }
    return jsonify(result)


@app.route("/api/wallets", methods=["POST"])
@_dash_auth
def api_wallets_create():
    data         = flask_request.get_json() or {}
    label        = (data.get("label") or "Wallet").strip()[:40]
    starting_usd = float(data.get("starting_usd") or 100.0)
    mode         = data.get("mode") or None
    if mode and mode not in CONFIG["modes"]:
        mode = None
    address = (data.get("address") or "").strip()
    wid = f"w_{int(time.time())}_{random.randint(1000, 9999)}"
    STATE.setdefault("wallets", {})[wid] = _wlt_init(label, starting_usd, mode, address)
    save_state()
    log(f"Wallet created: {wid} '{label}' ${starting_usd} mode={mode or 'global'}")
    return jsonify({"wid": wid}), 201


@app.route("/api/wallets/<wid>", methods=["PATCH"])
@_dash_auth
def api_wallet_update(wid):
    w = STATE.get("wallets", {}).get(wid)
    if not w:
        return jsonify({"error": "not found"}), 404
    data = flask_request.get_json() or {}
    if "label" in data:
        w["label"]   = str(data["label"])[:40]
    if "mode" in data:
        m = data["mode"]
        w["mode"] = m if (m and m in CONFIG["modes"]) else None
    if "active" in data:
        w["active"]   = bool(data["active"])
        if w["active"]:
            w.pop("paused_new_entries", None)   # re-enable clears hard-stop block
    if "address" in data:
        w["address"] = str(data["address"]).strip()
    if "starting_usd" in data:
        w["starting_usd"] = float(data["starting_usd"])
    if "sweep_address" in data:
        w["sweep_address"] = str(data["sweep_address"]).strip()
    if "ticket_cap_usd" in data:
        cap = float(data["ticket_cap_usd"])
        w["ticket_cap_usd"] = max(0.0, cap)
    if "safety" in data and isinstance(data["safety"], dict):
        sf = w.setdefault("safety", {})
        allowed = {"dollar_stop_usd", "dollar_stop_pos_mult", "velocity_m5_pct",
                   "rug_liq_drop_pct", "liq_drain_pct", "reserve_pct", "drawdown_brake"}
        for k, v in data["safety"].items():
            if k in allowed:
                sf[k] = None if v in (None, "", "null") else (bool(v) if k == "drawdown_brake" else float(v))
    if "live" in data:
        want_live = bool(data["live"])
        if want_live and wid not in _wallet_keypairs:
            # Try loading the keypair now (user may have added the env var since startup)
            _load_wallet_keypairs()
        if want_live and wid not in _wallet_keypairs:
            return jsonify({"error": f"No keypair found. Add W_{wid}_PK to .env and restart."}), 400
        was_live = w.get("live", False)
        w["live"] = want_live
        # Going live → wipe shadow history so stats reflect real trades only
        if want_live and not was_live:
            w["trade_log"]       = []
            w["vault_usd"]       = float(w.get("starting_usd", 0))
            w["take_home_usd"]   = 0.0
            w["total_swept_usd"] = 0.0
            w["sweep_log"]       = []
            w["pnl_hist"]        = []
            w["open_today_usd"]  = 0.0
            w["cur_deployed_usd"] = 0.0
            w["entries_today"]   = {}
            w["recently_exited"] = {}
            w["dd_alerted"]      = {}
            w["vault_snaps"]     = []
            w["positions"]       = {}
            log(f"[W:{wid}] RESET — going live, shadow history cleared")
    save_state()
    return jsonify({"ok": True})


@app.route("/api/wallets/<wid>", methods=["DELETE"])
@_dash_auth
def api_wallet_delete(wid):
    w = STATE.get("wallets", {}).get(wid)
    if not w:
        return jsonify({"error": "not found"}), 404
    open_pos = [s for s, p in w.get("positions", {}).items() if p.get("units", 0) > 0]
    if open_pos:
        return jsonify({"error": f"close positions first: {open_pos}"}), 400
    STATE["wallets"].pop(wid, None)
    save_state()
    return jsonify({"ok": True})


@app.route("/api/wallets/<wid>/trades", methods=["GET"])
@_dash_auth
def api_wallet_trades(wid):
    w = STATE.get("wallets", {}).get(wid)
    if not w:
        return jsonify({"error": "not found"}), 404
    return jsonify(w.get("trade_log", []))


@app.route("/api/wallets/<wid>/positions", methods=["GET"])
@_dash_auth
def api_wallet_positions(wid):
    w = STATE.get("wallets", {}).get(wid)
    if not w:
        return jsonify({"error": "not found"}), 404
    return jsonify({s: p for s, p in w.get("positions", {}).items() if p.get("units", 0) > 0})


@app.route("/api/opportunity-audit", methods=["GET"])
@_dash_auth
def api_opportunity_audit():
    """Read SCAN_LOG_FILE and return filter accuracy + missed-gain analysis."""
    if not os.path.exists(SCAN_LOG_FILE):
        return jsonify({"stats": {}, "top_misses": [], "entries": []})

    initial: Dict[str, Dict]       = {}   # addr → first-scan record
    followups: Dict[str, List]     = {}   # addr → list of px_followup records

    try:
        with open(SCAN_LOG_FILE) as _f:
            for _line in _f:
                try:
                    e = json.loads(_line)
                    addr = (e.get("addr") or "").strip()
                    if not addr:
                        continue
                    if e.get("type") == "px_followup":
                        followups.setdefault(addr, []).append(e)
                    else:
                        ts = e.get("ts", "")
                        if addr not in initial or ts < initial[addr].get("ts", ""):
                            initial[addr] = e
                except Exception:
                    pass
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    # Merge initial + follow-up data
    results: List[Dict] = []
    for addr, e in initial.items():
        fps = sorted(followups.get(addr, []), key=lambda x: x.get("elapsed_min", 0))
        pcts    = [fp["pct"] for fp in fps if "pct" in fp]
        best    = max(pcts) if pcts else None
        worst   = min(pcts) if pcts else None
        latest  = fps[-1] if fps else {}
        results.append({
            "ts":      e.get("ts"),
            "sym":     e.get("sym"),
            "addr":    addr,
            "dec":     e.get("dec"),
            "rsn":     e.get("rsn", ""),
            "px":      e.get("px", 0),
            "liq":     e.get("liq", 0),
            "hype":    e.get("hype"),
            "age":     e.get("age"),
            "br":      e.get("br"),
            "best_pct":    round(best, 1)  if best  is not None else None,
            "worst_pct":   round(worst, 1) if worst is not None else None,
            "latest_pct":  round(latest.get("pct", 0), 1) if latest else None,
            "elapsed_min": latest.get("elapsed_min"),
            "fp_count":    len(fps),
        })

    rejected  = [r for r in results if r["dec"] == "rejected"]
    entered   = [r for r in results if r["dec"] == "entered"]
    suggested = [r for r in results if r["dec"] == "suggested"]

    def _pct_over(items: List, thr: float) -> Optional[float]:
        tracked = [r for r in items if r.get("best_pct") is not None]
        if not tracked:
            return None
        return round(len([r for r in tracked if r["best_pct"] > thr]) / len(tracked) * 100, 1)

    # Bucket rejections by filter category
    CATS = {
        "hype_low":    lambda r: "hype" in r["rsn"].lower() and "below" in r["rsn"].lower(),
        "liq_low":     lambda r: "liquid" in r["rsn"].lower() and "below" in r["rsn"].lower(),
        "liq_high":    lambda r: "liquid" in r["rsn"].lower() and "above" in r["rsn"].lower(),
        "too_old":     lambda r: "age" in r["rsn"].lower(),
        "cooldown":    lambda r: "cooldown" in r["rsn"].lower(),
        "pos_cap":     lambda r: "max open" in r["rsn"].lower(),
        "anti_churn":  lambda r: "anti-churn" in r["rsn"].lower() or "entered" in r["rsn"].lower(),
        "buy_ratio":   lambda r: "buy_ratio" in r["rsn"].lower() or "buy ratio" in r["rsn"].lower(),
        "safety":      lambda r: "safety" in r["rsn"].lower() or "scam" in r["rsn"].lower() or "rug" in r["rsn"].lower(),
    }
    by_cat: Dict[str, Dict] = {}
    for r in rejected:
        cat = "other"
        for name, test in CATS.items():
            try:
                if test(r):
                    cat = name
                    break
            except Exception:
                pass
        bucket = by_cat.setdefault(cat, {"count": 0, "tracked": 0, "o50": 0, "o100": 0, "o500": 0})
        bucket["count"] += 1
        bp = r.get("best_pct")
        if bp is not None:
            bucket["tracked"] += 1
            if bp > 50:   bucket["o50"]  += 1
            if bp > 100:  bucket["o100"] += 1
            if bp > 500:  bucket["o500"] += 1

    top_misses = sorted(
        [r for r in rejected if r.get("best_pct") is not None and r["best_pct"] > 20],
        key=lambda x: x["best_pct"], reverse=True
    )[:100]

    stats = {
        "total":      len(results),
        "entered":    len(entered),
        "rejected":   len(rejected),
        "suggested":  len(suggested),
        "rej_o50_pct":  _pct_over(rejected, 50),
        "rej_o100_pct": _pct_over(rejected, 100),
        "ent_o50_pct":  _pct_over(entered,  50),
        "ent_o100_pct": _pct_over(entered,  100),
        "by_filter": by_cat,
    }

    recent = sorted(results, key=lambda x: x.get("ts") or "", reverse=True)[:500]
    return jsonify({"stats": stats, "top_misses": top_misses, "entries": recent})


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
    STATE["moonshot_mode"] = m   # persist across restarts
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
    if "dollar_stop_usd" in data:
        CONFIG["moonshot"]["dollar_stop_usd"] = max(0.0, float(data["dollar_stop_usd"]))
    if "dynamic_sizing" in data:
        CONFIG["moonshot"]["dynamic_sizing"] = bool(data["dynamic_sizing"])
    if "blacklist" in data and isinstance(data["blacklist"], list):
        CONFIG["blacklist"] = [str(x).strip() for x in data["blacklist"] if str(x).strip()]
    return jsonify({"ok": True})


@app.route("/api/sim_hot", methods=["GET"])
@_dash_auth
def api_sim_hot():
    """Return a live DexScreener candidate matching current mode filters — for dashboard Quick Sim."""
    mode_cfg = CONFIG["modes"].get(CONFIG["mode"], CONFIG["modes"]["default"])
    liq_min  = mode_cfg.get("liq_min", 15000)
    age_max  = mode_cfg.get("max_age_min", 180)
    try:
        import time as _time
        data  = _get("https://api.dexscreener.com/token-profiles/latest/v1") or []
        addrs = [item["tokenAddress"] for item in (data if isinstance(data, list) else [])
                 if item.get("chainId") == "solana"][:20]
        if not addrs:
            return jsonify({"error": "no candidates from DexScreener"}), 503
        pairs_data = _get(DEXSCREENER_TOKEN + ",".join(addrs)) or {}
        pairs = pairs_data.get("pairs") or []
        now_ms = _time.time() * 1000
        best = None
        best_score = -9999
        for p in pairs:
            if p.get("chainId") != "solana":
                continue
            liq    = float((p.get("liquidity") or {}).get("usd") or 0)
            if liq < liq_min:
                continue
            age_ms = now_ms - (p.get("pairCreatedAt") or now_ms)
            age_min = age_ms / 60000
            if age_min > age_max:
                continue
            # skip tokens already in open positions or recently exited
            sym  = (p.get("baseToken") or {}).get("symbol", "")
            if sym in STATE.get("positions", {}) or sym in STATE.get("recently_exited", {}):
                continue
            vol_h1  = float((p.get("volume") or {}).get("h1") or 0)
            ch_m5   = float((p.get("priceChange") or {}).get("m5") or 0)
            ch_h1   = float((p.get("priceChange") or {}).get("h1") or 0)
            buys    = int((p.get("txns") or {}).get("h1", {}).get("buys") or 0)
            sells   = int((p.get("txns") or {}).get("h1", {}).get("sells") or 0)
            ratio   = buys / (buys + sells) if (buys + sells) > 0 else 0
            if ratio < 0.45 or ch_m5 < 0:
                continue
            score = ch_m5 + ratio * 20 + (vol_h1 / 10000)
            if score > best_score:
                best_score = score
                best = {
                    "symbol":  sym,
                    "address": (p.get("baseToken") or {}).get("address", ""),
                    "chain":   "sol",
                    "price":   float(p.get("priceUsd") or 0),
                    "liq":     liq,
                    "age_min": round(age_min),
                    "vol_h1":  round(vol_h1),
                    "ch_m5":   ch_m5,
                    "ch_h1":   ch_h1,
                    "buy_ratio": round(ratio, 2),
                }
        if not best:
            return jsonify({"error": "no token matched current mode filters"}), 404
        return jsonify(best)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
        STATE["open_today_usd"]    = 0.0
        STATE["entries_today"]     = {}
        STATE["recently_exited"]   = {}   # clear cooldowns at midnight — fresh day, fresh tokens
        STATE["reject_cache"]      = {}   # clear reject cache — tokens get a fresh look each day
        STATE["token_pnl_today"]   = {}   # cumulative PnL per symbol today (loss escalation)
        STATE["peak_deployed_usd"] = 0.0
        STATE["peak_open_count"]   = 0
        STATE["last_daily_reset"] = today
        # Reset daily counters in additional wallets too
        for w in STATE.get("wallets", {}).values():
            w["entries_today"]   = {}
            w["recently_exited"] = {}
            w["open_today_usd"]  = 0.0
        save_state()
        log("Daily reset: open_today_usd → 0, entries_today cleared, capital peaks reset")

    btc_d                     = fetch_btc_dominance()
    STATE["signals"]["btc_d"] = btc_d
    STATE["signals"]["heat"]  = compute_heat(btc_d)

    candidates = fetch_new_candidates()
    log(f"SCAN: {len(candidates)} candidates from feeds (probe_q={len(_PROBE_QUEUE)})")
    if CONFIG["stealth"]["candidate_shuffle"]:
        random.shuffle(candidates)

    # Drain on-chain probed tokens: these were detected at mint time, so their age
    # is already accurate and they front-run the 8s DexScreener poll.
    with _ONCHAIN_LOCK:
        probe_batch, _PROBE_QUEUE[:] = list(_PROBE_QUEUE), []
    existing_keys = {(c["symbol"], c["chain"]) for c in candidates}
    for pair in probe_batch:
        onchain_ts = pair.pop("_onchain_ts", None)
        if onchain_ts:
            pair["pairCreatedAt"] = int(onchain_ts * 1000)
        c = _pair_to_candidate(pair, "sol")
        if c and (c["symbol"], c["chain"]) not in existing_keys:
            c["_onchain"] = True
            candidates.insert(0, c)   # evaluate on-chain finds first
            existing_keys.add((c["symbol"], c["chain"]))

    # Track symbols entered this scan cycle so duplicate pool addresses for the
    # same symbol (e.g. two DexScreener pairs for TJR) don't stack two full-size
    # entries in one pass — that's what caused TJR/BULL to reach $280 deployed
    # when the per-token cap was supposed to stop them at ~$85.
    _entered_this_scan: set = set()
    _wallet_scan_entered: Dict[str, set] = {}  # wid -> set of symbols entered this cycle

    for c in candidates:
        symbol = c["symbol"]
        if not symbol or symbol == "?":
            continue  # skip tokens with no symbol — position dict collision risk
        if symbol in _entered_this_scan:
            _scout(symbol, c["chain"], "rejected", "duplicate pair in scan — already entered this cycle", None, c.get("address",""))
            continue
        # Don't re-enter tokens already in an open position — autoscale handles additions,
        # not the scanner. Without this the bot stacks a second full-size entry on the next
        # 20s scan cycle whenever the same hot token reappears in the feed.
        if STATE["positions"].get(symbol, {}).get("units", 0) > 0:
            continue
        chain  = c["chain"]
        price  = c["price"]
        liq    = c["liq"]
        sc     = Score(c.get("hype", 0), liq, c["age_min"], c.get("positive", True),
                       buy_ratio=c.get("buy_ratio"), price=price,
                       price_chg_m5=c.get("price_chg_m5", 0.0),
                       price_chg_h1=c.get("price_chg_h1", 0.0))
        is_new = c["age_min"] <= CONFIG["scan"]["new_max_age_min"]

        addr = c.get("address", "")
        link = _dex_link(chain, addr)

        # Reject cache: skip re-evaluation unless price has spiked enough since last reject.
        # Saves API calls and stops the scanner from asking the same question 20+ times.
        # TTL: safety flags expire after 8 h; liq/generic flags expire after 2 h.
        _rc = STATE.get("reject_cache", {}).get(symbol)
        if _rc and _rc.get("price", 0) > 0 and price > 0:
            _rc_age   = time.time() - _rc.get("ts", 0)
            _thr = _rc.get("threshold", 1.3)
            _rc_ttl   = _rc.get("ttl") or (14400 if _thr >= 3.0 else 7200 if _thr >= 2.0 else 600)
            if _rc_age < _rc_ttl:
                _price_ok = price >= _rc["price"] * _rc.get("threshold", 1.3)
                _liq_ok   = liq   >= _rc.get("min_liq", 0)
                if not (_price_ok and _liq_ok):
                    continue   # silent skip — price hasn't spiked enough AND cache not expired

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
        # Post-exit cooldown — applies to ALL tokens regardless of age.
        # Pattern A: after a loss exit, block re-entry for 30 min (token is still dumping).
        # Pattern B: after a profit exit, block re-entry unless price pulled back ≥8%.
        # BUG FIXED: was inside `if is_new:` so older tokens bypassed the cooldown entirely.
        _rex = STATE.get("recently_exited", {}).get(symbol)
        if _rex:
            _rex_elapsed = time.time() - _rex.get("ts", 0)
            _rex_pnl     = _rex.get("pnl", 0)
            _rex_price   = _rex.get("price", 0)
            if _rex_pnl < 0:
                # Use the worse of: single trade loss OR cumulative losses today on this token.
                # Prevents "$4 drain" — bot losing $4 repeatedly on the same token.
                # e.g. two -$4 trades = -$8 cumulative → escalates to 15-min tier automatically.
                _cumul_pnl = STATE.get("token_pnl_today", {}).get(symbol, _rex_pnl)
                _eff_pnl   = min(_rex_pnl, _cumul_pnl)   # whichever is more negative
                if _eff_pnl <= -50:
                    # Catastrophic loss ($50+): 8h hard stop. TJR lost $89 then re-entered
                    # at exactly 4h05m and lost $42 more — the 4h window was too narrow.
                    if _rex_elapsed < 8 * 3600:
                        _scout(symbol, chain, "rejected",
                               f"hard-stop: lost ${abs(_eff_pnl):.0f} here today, paused 8h ({_rex_elapsed/60:.0f}min ago)", sc, addr)
                        continue
                elif _eff_pnl <= -30:
                    # Major loss ($30–50): 4h hard stop.
                    if _rex_elapsed < 4 * 3600:
                        _scout(symbol, chain, "rejected",
                               f"hard-stop: lost ${abs(_eff_pnl):.0f} here today, paused 4h ({_rex_elapsed/60:.0f}min ago)", sc, addr)
                        continue
                elif _eff_pnl <= -15:
                    # Significant loss: 30 min cooldown
                    if _rex_elapsed < 30 * 60:
                        _scout(symbol, chain, "rejected",
                               f"post-loss cooldown ({_rex_elapsed/60:.0f}min ago, cumul=${_eff_pnl:.2f})", sc, addr)
                        continue
                elif _eff_pnl <= -5:
                    # Moderate loss: 15 min cooldown
                    if _rex_elapsed < 15 * 60:
                        _scout(symbol, chain, "rejected",
                               f"post-loss cooldown ({_rex_elapsed/60:.0f}min ago, cumul=${_eff_pnl:.2f})", sc, addr)
                        continue
                # eff_pnl in (-5, 0): no cooldown — small losses re-enter freely
            if _rex_pnl >= 0 and _rex_elapsed < 15 * 60:
                # Hard minimum: never re-enter within 60s of a profit exit regardless of price.
                # TOPDOG re-bought 0s after selling — no time for price to have pulled back.
                if _rex_elapsed < 60:
                    _scout(symbol, chain, "rejected",
                           f"profit-chase block: only {_rex_elapsed:.0f}s since exit (min 60s)", sc, addr)
                    continue
                if _rex_price > 0:
                    _pullback = (_rex_price - price) / _rex_price
                    if _pullback < 0.08:
                        _scout(symbol, chain, "rejected",
                               f"profit-chase block: price {_pullback*100:.1f}% below exit (need 8%)", sc, addr)
                        continue
        # Anti-churn: applies to ALL tokens regardless of age (same reasoning as cooldown fix).
        # Without this, tokens older than new_max_age_min had no per-day entry cap from the scanner.
        entry_cap = CONFIG["modes"].get(CONFIG["mode"], {}).get(
            "max_entries_per_token_day", CONFIG.get("max_entries_per_token_day", 4))
        # Anti-churn cap only blocks re-entries on net-losing tokens.
        # If the last exit was profitable, no daily cap — keep riding winners.
        _last_was_win = _rex is not None and _rex.get("pnl", -1) > 0
        if not _last_was_win and STATE.setdefault("entries_today", {}).get(symbol, 0) >= entry_cap:
            _scout(symbol, chain, "rejected",
                   f"already entered {entry_cap}x today (anti-churn)", sc, addr)
            continue
        if is_new:
            reject = moonshot_reject_reason(sc, chain)
            if reject:
                _scout(symbol, chain, "rejected", reject, sc, addr)
                continue

            # Escalating entry bar: as cumulative losses on this token grow today,
            # raise the hype/liq/buy_ratio floor before allowing another entry.
            # Keeps the bot from re-entering the same bad bet with loose filters.
            _cumul_today = STATE.get("token_pnl_today", {}).get(symbol, 0.0)
            if _cumul_today <= -15:
                _bar_hype = 30; _bar_liq_mult = 1.5; _bar_br = 0.58
            elif _cumul_today <= -5:
                _bar_hype = 20; _bar_liq_mult = 1.3; _bar_br = 0.52
            else:
                _bar_hype = 0; _bar_liq_mult = 1.0; _bar_br = 0.0
            if _bar_hype > 0:
                _mode_hype = max(50, CONFIG["moonshot"].get("hype_min", 50))
                _req_hype  = min(100, _mode_hype + _bar_hype)
                _mode_liq  = CONFIG["modes"].get(CONFIG["mode"], {}).get("liq_min", CONFIG["moonshot"].get("liq_min", 15000))
                _req_liq   = _mode_liq * _bar_liq_mult
                _label     = f"(${abs(_cumul_today):.0f} lost today — raised bar)"
                if sc.hype < _req_hype:
                    _scout(symbol, chain, "rejected",
                           f"hype {sc.hype} below elevated min {_req_hype} {_label}", sc, addr)
                    continue
                if sc.liq < _req_liq:
                    _scout(symbol, chain, "rejected",
                           f"liq ${sc.liq:,.0f} below elevated min ${_req_liq:,.0f} {_label}", sc, addr)
                    continue
                if sc.buy_ratio is not None and sc.buy_ratio < _bar_br:
                    _scout(symbol, chain, "rejected",
                           f"buy ratio {sc.buy_ratio*100:.0f}% below elevated min {_bar_br*100:.0f}% {_label}", sc, addr)
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
            # Scam / rug safety gate — on-chain rug checks
            safe, why = safety_gate(symbol, addr, chain, hype=sc.hype if sc else 0)
            if not safe:
                log(f"SAFETY GATE {symbol}: {why}")
                send_alert(
                    f"🛡️ SKIPPED {symbol} ({chain}) — {why}. The bot steered clear to protect you "
                    f"from a likely scam/rug.\n{link}")
                _scout(symbol, chain, "rejected", f"safety: {why}", sc, addr)
                continue
            if CONFIG["moonshot"]["mode"] == "enter":
                usd = size_ticket_usd(chain, hype=sc.hype, buy_ratio=sc.buy_ratio, symbol=symbol)
                _min_ticket = 10.0 if CONFIG["moonshot"].get("dynamic_sizing") else _mode_min_ticket()
                if CONFIG["moonshot"].get("dynamic_sizing"):
                    _avail = max(0.0, STATE["vault_usd"] - STATE.get("cur_deployed_usd", 0.0))
                    if _avail < usd * 1.5:
                        _scout(symbol, chain, "rejected",
                               f"cash buffer thin (avail ${_avail:.0f} < 1.5× bet ${usd:.0f})", sc, addr)
                        continue
                if usd >= _min_ticket and est_price_impact(usd, liq) <= CONFIG["moonshot"]["price_impact_max"]:
                    shadow_buy(symbol, chain, usd, price, liq, addr,
                               entry_m5=sc.price_chg_m5 if sc else None,
                               entry_br=sc.buy_ratio if sc else None,
                               entry_hype=sc.hype if sc else None)
                    _entered_this_scan.add(symbol)
                    _wallets_offer_entry(symbol, chain, price, liq, addr, _wallet_scan_entered,
                                         hype=sc.hype, buy_ratio=sc.buy_ratio)
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
                    usd = min(CONFIG["oldcoin"]["tiny_entry_usd"], size_ticket_usd(chain, symbol=symbol))
                    if usd >= CONFIG["moonshot"]["min_ticket_usd"]:
                        shadow_buy(symbol, chain, usd, price, liq, addr)
                        _entered_this_scan.add(symbol)
                        _wallets_offer_entry(symbol, chain, price, liq, addr, _wallet_scan_entered)
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
            usd = min(CONFIG["oldcoin"]["tiny_entry_usd"], size_ticket_usd(w_chain, symbol=sym))
            if usd >= CONFIG["moonshot"]["min_ticket_usd"]:
                shadow_buy(sym, w_chain, usd, w_price, w_liq, addr)
                _wallets_offer_entry(sym, w_chain, w_price, w_liq, addr, _wallet_scan_entered)
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
    for s, p in list(STATE["positions"].items()):
        if p.get("units", 0) <= 0:
            continue
        # Use the mode this position was entered under for all exit thresholds.
        # degen rides wider stops; hype exits fast; safe exits very fast. Each is independent.
        entry_mode = p.get("entry_mode", CONFIG["mode"])
        MS         = _effective_ms(entry_mode)
        px        = live_prices.get(s, _px_dict(p.get("avg", 1.0) * 1.02))
        price     = px["price"]
        liq       = px["liq"]
        vol_h1    = px["vol_h1"]
        change_m5 = px["change_m5"]
        entry_ts  = datetime.fromisoformat(p.get("time", now_utc().isoformat())).timestamp()
        _record_tick(s, p, px)   # forward recorder: real per-tick price/liq for backtests

        # Track peak and trough price
        if price > p.get("peak_price", 0):
            p["peak_price"] = price
            p["peak_ts"]    = time.time()
        if price < p.get("trough_price", float("inf")):
            p["trough_price"] = price
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
        elif gain_now >= 0.20 and not in_moonbag: trail_pct = min(trail_pct, 0.08)

        # ── EXIT PRIORITY ORDER ────────────────────────────────────────────
        exit_reason: Optional[str] = None

        # 0. Hard dollar stop — fires before everything else.
        _dollar_stop = _mode_dollar_stop(entry_mode)
        _cur_val     = p.get("units", 0) * price
        # Use p["usd"] (cost basis of REMAINING units, reduced on each partial sell)
        # NOT deployed_usd (which only resets at full close). Using deployed_usd caused
        # dollar_stop to fire after every TP rung because the moon-bag's current value
        # was compared against the full original cost → looked like a massive loss → 19k
        # ghost sells in one BITBOY trade.
        _cost        = p.get("usd", 0) or 1e-9
        _unrealized  = _cur_val - _cost
        # Ghost-position guard: if remaining value is < $0.01, the position is effectively
        # closed (floating-point dust after sequential partial TP sells). Purge it now
        # rather than letting dollar_stop fire on it every 0.1s forever.
        if _cur_val < 0.01:
            STATE["positions"].pop(s, None)
            STATE["cur_deployed_usd"] = sum(
                q.get("usd", 0.0) for q in STATE["positions"].values() if q.get("units", 0) > 0
            )
            continue
        # Scale the stop with position size: $12 floor, 10% ceiling.
        # A $40 entry stops at $12 (30%); a $400 scaled position stops at $40 (10%).
        # Flat $12 on large positions would fire on a 2-3% sneeze and kill runners.
        _effective_stop = max(_dollar_stop, _cost * 0.10)
        if _dollar_stop > 0 and _unrealized < -_effective_stop:
            exit_reason = f"DOLLAR STOP -${abs(_unrealized):.2f} (limit ${_effective_stop:.0f})"

        # 1. Instant rug guard — liq craters in one tick
        prev_liq = STATE["liq_prev"].get(s, liq)
        liq_drop_pct = (prev_liq - liq) / max(prev_liq, 1) if prev_liq > 0 else 0
        if liq < prev_liq * (1 - MS["rug_liq_drop"]):
            exit_reason = f"RUG liq {prev_liq:.0f}→{liq:.0f}"
        # Protect profitable positions: exit on a smaller liq drain (15%) when in the green.
        # No reason to hold through liquidity leaving when you're already up.
        elif gain_now > 0.05 and liq_drop_pct > 0.15:
            exit_reason = f"LIQ PROTECT +{gain_now*100:.0f}% gain, liq -{liq_drop_pct*100:.0f}%"
        STATE["liq_prev"][s] = liq

        # 2. Velocity exit — sharp price drop signals rug unfolding or panic sell.
        #    2a: DexScreener m5 drop (standard check)
        if exit_reason is None and change_m5 < -(MS["velocity_exit_pct"] * 100):
            exit_reason = f"VELOCITY {change_m5:.1f}% in 5m"
        #    2b: Internal 2-min drop check using tick-tracked price history.
        #        Data: 0.5s polling → ~240 ticks/2min. We store last 240 prices per position.
        #        If price dropped ≥ 12% vs the oldest of the last 240 ticks → exit fast.
        #        Catches slow rugs that haven't shown up in the 5-min window yet.
        if exit_reason is None:
            _price_hist = p.setdefault("price_hist_2m", [])
            _price_hist.append((time.time(), price))
            if len(_price_hist) > 240:
                _price_hist[:] = _price_hist[-240:]
            if len(_price_hist) >= 60:   # need ≥ 30s of data before firing
                _oldest_price = _price_hist[0][1]
                if _oldest_price > 0 and price < _oldest_price * (1 - MS.get("velocity_2m_pct", 0.12)):
                    _drop_pct = (_oldest_price - price) / _oldest_price * 100
                    exit_reason = f"VELOCITY2M -{_drop_pct:.1f}% in {len(_price_hist)/2:.0f}s"

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
            res = exec_sell(s, p["usd"], price, liq, exit_reason)
            pnl = res.get("pnl", 0.0)
            log(f"EXIT {s} [{exit_reason}] pnl ${pnl:.2f}")
            send_alert(
                f"🚨 SOLD {s} — {_exit_plain(exit_reason)}. Paper result: ${pnl:+.2f}.\n"
                f"{_dex_link(p.get('chain', 'sol'), p.get('address', ''))}", critical=True)
            _add_reentry_watch(s, p, liq, vol_h1, exit_reason)
            STATE["liq_prev"].pop(s, None)
            STATE.get("vol_dry_alerted", {}).pop(s, None)
            STATE.get("no_pump_alerted", {}).pop(s, None)
            save_state()
            continue

        # 5. Volume dry-up — soft alert only (not a standalone exit)
        entry_vol = p.get("entry_vol_h1", 0)
        if vol_h1 > 0 and entry_vol > 0 and vol_h1 < entry_vol * MS["vol_dry_pct"]:
            _vda = STATE.setdefault("vol_dry_alerted", {})
            if time.time() - _vda.get(s, 0) > 1800:  # max once per 30 min per symbol
                _vda[s] = time.time()
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
                    res = exec_sell(s, sell_usd, price, liq, f"TP rung {i+1} +{gain*100:.0f}%")
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
            res = exec_sell(s, p["usd"], price, liq, "fixed_sl")
            pnl = res.get("pnl", 0.0)
            log(f"SL {s}: exit pnl ${pnl:.2f}")
            send_alert(
                f"🔻 STOP-LOSS on {s} — it slid down to your max-loss line, so the bot sold to cap the "
                f"damage. Paper result: ${pnl:+.2f}. Better a small loss than a big one.", critical=True)
            _add_reentry_watch(s, p, liq, vol_h1, "fixed_sl")
            save_state()
            continue

        # No-pump soft flag — alert at most once per 60 min per symbol
        if p.get("units", 0) > 0 and should_exit_no_pump(
                entry_ts, time.time(), p["avg"], price, liq,
                mode_name=p.get("entry_mode", CONFIG["mode"])):
            _npa = STATE.setdefault("no_pump_alerted", {})
            if time.time() - _npa.get(s, 0) > 3600:
                _npa[s] = time.time()
                send_alert(
                    f"⏱ {s} has been flat since you bought it — it isn't taking off. Heads-up so you can "
                    f"decide whether to cut it loose; the bot is still holding for now.")

        # Autoscale after grace
        if p.get("units", 0) > 0 and time.time() - entry_ts >= CONFIG["autoscale"]["grace_sec"]:
            autoscale_maybe(s, p["chain"], price, liq, velocity_ok=(price > p["avg"]))

    # Run the exit loop for each additional wallet
    for _wid, _wlt in STATE.get("wallets", {}).items():
        if _wlt.get("active") and _wlt.get("positions"):
            _manage_wallet_positions(_wid, _wlt, live_prices)

    _manage_arenas(live_prices)
    _run_price_followup()


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
        # Position checks read WS_PRICES (in-memory) — no API cost.
        # 0.1s = 10 checks/sec; catches gap-downs ~5× faster than 0.5s.
        time.sleep(0.1)

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
    # Restore user-saved modes into CONFIG["modes"] so the bot can run them
    for name, params in STATE.get("custom_modes", {}).items():
        _register_custom_mode(name, params)
    if "shadow_mode" in STATE:
        SHADOW_MODE = bool(STATE["shadow_mode"])
    else:
        STATE["shadow_mode"] = SHADOW_MODE
        save_state()

    # Restore whichever mode the user last set via the dashboard — takes priority over BOT_MODE env.
    saved_mode = STATE.get("active_mode", "")
    if saved_mode and saved_mode in CONFIG["modes"]:
        CONFIG["mode"] = saved_mode
        if saved_mode in STATE.get("custom_modes", {}):
            _apply_custom_mode_globals(STATE["custom_modes"][saved_mode])
        log(f"Restored mode '{saved_mode}' from state")
    else:
        env_mode = os.getenv("BOT_MODE", "").strip().lower()
        if env_mode:
            if env_mode in CONFIG["modes"]:
                CONFIG["mode"] = env_mode
            else:
                log(f"WARN BOT_MODE='{env_mode}' invalid — keeping '{CONFIG['mode']}'")
    # Restore moonshot mode from STATE first (set via dashboard), then fall back to env.
    saved_moon = STATE.get("moonshot_mode", "")
    if saved_moon in ("enter", "suggest"):
        CONFIG["moonshot"]["mode"] = saved_moon
        log(f"Restored moonshot mode '{saved_moon}' from state")
    else:
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
    _load_wallet_keypairs()
    _start_ws_thread()
    _start_ws_create_thread()
    # Re-subscribe open SOL positions that existed before this restart.
    for sym, p in STATE.get("positions", {}).items():
        if p.get("chain") == "sol" and p.get("address") and p.get("units", 0) > 0:
            _ws_add(sym, p["address"])
    start_flask()
    threading.Thread(target=engine_loop, daemon=True).start()
    start_telegram()


if __name__ == "__main__":
    main()
