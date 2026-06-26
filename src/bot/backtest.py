#!/usr/bin/env python3
"""
Backtester — replay the past days through the bot's REAL entry/exit logic.

It pulls real historical price/liquidity/volume candles (GeckoTerminal, free, no
key) for the tokens the bot actually traded, then runs each one through the live
strategy code (the same TP ladder, trailing stop, stall exit, rug guard, exit
price-impact and sizing you trade with) under each risk mode — so you can see how
safe / default / hype / degen would have handled the same days.

It reuses the production functions (shadow_buy, shadow_sell, manage_positions,
size_ticket_usd, the moonshot filter) by driving them with a simulated clock and
mocked price feed, so the backtest behaves exactly like the live bot.

Usage (from the repo root):
    python src/bot/backtest.py --days 3                  # tokens from the live trade log
    python src/bot/backtest.py --days 3 --modes degen,hype
    python src/bot/backtest.py --tokens sol:So111...,eth:0xabc --days 2

Honest caveats (printed in the report too):
  • Entry is at the START of the fetched window, not the exact live trending-feed
    moment — this backtests EXIT + SIZING behaviour, which is where the big losses
    were, not the candidate-selection feed (we don't have a historical trending feed).
  • The buyer/seller gate is skipped (OHLCV has no per-trade buy/sell split).
  • Slippage, gas and exit price-impact ARE modelled (same code as live).
"""
from __future__ import annotations

import argparse
import os
import sys
import time as _real_time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


def _stub_optional_deps() -> None:
    """Stub heavy trading deps (telegram/solders/boto3) if they aren't installed, so
    the backtester runs on a plain laptop. No-op where they're real (e.g. the server
    container) — we only stub modules that genuinely fail to import."""
    import importlib
    from unittest.mock import MagicMock
    for mod in ("telegram", "telegram.ext", "solders", "solders.keypair",
                "solders.transaction", "solders.hash", "solders.message",
                "solders.rpc", "boto3", "botocore"):
        try:
            importlib.import_module(mod)
        except Exception:
            sys.modules.setdefault(mod, MagicMock())
    tg = sys.modules.get("telegram")
    if isinstance(tg, type(sys)) is False and tg is not None:
        for attr in ("Update", "ext"):
            if not hasattr(tg, attr):
                setattr(tg, attr, MagicMock())


_stub_optional_deps()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:                                  # as `python -m src.bot.backtest`
    from . import bot_full as bot
except ImportError:                   # as `python src/bot/backtest.py`
    import bot_full as bot


# ── GeckoTerminal network slugs ────────────────────────────────────────────────
GT_NET = {"sol": "solana", "eth": "eth", "base": "base", "bsc": "bsc", "polygon": "polygon_pos"}
GT_BASE = "https://api.geckoterminal.com/api/v2"


# ── Simulated clock ────────────────────────────────────────────────────────────
class _Clock:
    """Drives the bot's notion of 'now' during replay so time-based logic
    (stall exit, hold windows, daily reset) sees historical timestamps."""
    def __init__(self, start: float):
        self.now = float(start)

    def time(self) -> float:
        return self.now

    def sleep(self, _secs: float) -> None:   # bot never sleeps in replay
        return None


# ── Historical data (GeckoTerminal) ────────────────────────────────────────────
def _gt_get(path: str) -> Optional[dict]:
    import requests
    try:
        r = requests.get(GT_BASE + path, timeout=12,
                         headers={"accept": "application/json"})
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def fetch_history(chain: str, address: str, days: int) -> List[Dict[str, float]]:
    """Return a list of {ts, price, liq, vol_h1} candles (oldest→newest) for a token.
    Uses the token's top pool. 5-minute candles for intraday detail."""
    net = GT_NET.get(chain)
    if not net or not address:
        return []
    pools = _gt_get(f"/networks/{net}/tokens/{address}/pools")
    try:
        pool_addr = (pools["data"][0]["attributes"]["address"])
        pool_liq  = float(pools["data"][0]["attributes"].get("reserve_in_usd") or 0)
    except Exception:
        return []
    limit = min(1000, max(1, days) * 288)   # 288 five-min candles/day
    ohlcv = _gt_get(f"/networks/{net}/pools/{pool_addr}/ohlcv/minute"
                    f"?aggregate=5&limit={limit}&currency=usd")
    try:
        rows = ohlcv["data"]["attributes"]["ohlcv_list"]
    except Exception:
        return []
    out: List[Dict[str, float]] = []
    for ts, _o, _h, _l, close, vol in rows:
        out.append({"ts": float(ts), "price": float(close),
                    "liq": pool_liq, "vol_h1": float(vol or 0) * 12.0})  # 5-min vol → ~hourly
    out.sort(key=lambda r: r["ts"])
    return out


# ── Replay one token under one mode ─────────────────────────────────────────────
def replay_episode(symbol: str, chain: str, address: str,
                   ticks: List[Dict[str, float]], mode: str,
                   start_vault: float = 1000.0) -> Optional[Dict[str, Any]]:
    """Buy at the first tick, then feed every later tick through the REAL
    manage_positions() exit cascade. Returns the episode result, or None if the
    token never qualified for entry under this mode."""
    if len(ticks) < 2:
        return None

    clock = _Clock(ticks[0]["ts"])

    # Snapshot + neutralise the live globals we touch, then restore in finally.
    saved = {
        "STATE": bot.STATE, "mode": bot.CONFIG["mode"],
        "now_utc": bot.now_utc, "time": bot.time,
        "save_state": bot.save_state, "send_alert": bot.send_alert,
        "fetch_positions_prices": bot.fetch_positions_prices,
        "gas_dynamic": bot.CONFIG.get("gas_dynamic", True),
    }
    try:
        bot.CONFIG["mode"] = mode
        bot.CONFIG["gas_dynamic"] = False          # no live gas calls in replay
        bot.now_utc = lambda: datetime.fromtimestamp(clock.now, tz=timezone.utc)
        bot.time = clock                            # bot_full's `time.time()` → sim clock
        bot.save_state = lambda *a, **k: None
        bot.send_alert = lambda *a, **k: None
        # Fresh, isolated state for this single-token run
        bot.STATE = _fresh_state(start_vault)

        first = ticks[0]
        sc = bot.Score(95, first["liq"], 0.0, True)   # known-traded token → assume hype ok
        if bot.moonshot_reject_reason(sc) and bot.moonshot_reject_reason(sc).startswith("liquidity"):
            return None                                # too thin for this mode → no entry

        usd = bot.size_ticket_usd(chain, hype=95)
        if usd < bot.CONFIG["moonshot"]["min_ticket_usd"]:
            return None
        bot.shadow_buy(symbol, chain, usd, first["price"], first["liq"], address)
        entry_price = bot.STATE["positions"][symbol]["avg"]
        peak_gain = 0.0

        for i in range(1, len(ticks)):
            t = ticks[i]
            clock.now = t["ts"]
            prev = ticks[i - 1]["price"] or t["price"]
            change_m5 = ((t["price"] / prev) - 1.0) * 100.0 if prev else 0.0
            tickpx = bot._px_dict(t["price"], t["liq"], t["vol_h1"], change_m5)
            bot.fetch_positions_prices = (lambda s=symbol, px=tickpx: {s: px})
            peak_gain = max(peak_gain, (t["price"] / entry_price) - 1.0)
            bot.manage_positions()
            pos = bot.STATE["positions"].get(symbol)
            if not pos or pos.get("units", 0) <= 0:
                break

        # Force mark-to-market close on whatever's left at the last tick
        pos = bot.STATE["positions"].get(symbol)
        if pos and pos.get("units", 0) > 0:
            bot.shadow_sell(symbol, pos["usd"], ticks[-1]["price"], ticks[-1]["liq"])

        end_val = bot.STATE["vault_usd"] + bot.STATE.get("income_usd", 0.0)
        pnl = end_val - start_vault
        hold_min = (clock.now - ticks[0]["ts"]) / 60.0
        return {
            "symbol": symbol, "chain": chain, "mode": mode,
            "deployed": usd, "pnl": pnl, "peak_gain_pct": peak_gain * 100.0,
            "hold_min": hold_min, "win": pnl > 0,
        }
    finally:
        for k, v in saved.items():
            if k == "mode":
                bot.CONFIG["mode"] = v
            elif k == "gas_dynamic":
                bot.CONFIG["gas_dynamic"] = v
            else:
                setattr(bot, k, v)


def _fresh_state(vault: float) -> Dict[str, Any]:
    return {
        "vault_usd": vault, "open_today_usd": 0.0, "income_usd": 0.0,
        "gas_paid_usd": 0.0, "positions": {}, "liq_prev": {}, "pnl_hist": [],
        "trade_log": [], "entries_today": {}, "open_burst": [],
        "mode_perf": {}, "last_daily_reset": "2000-01-01",
    }


# ── Drive the whole backtest ────────────────────────────────────────────────────
def tokens_from_trade_log(days: int) -> List[Tuple[str, str, str]]:
    """Distinct (symbol, chain, address) the live bot bought in the last `days`."""
    cutoff = bot.now_utc().timestamp() - days * 86400
    seen, out = set(), []
    for t in bot.STATE.get("trade_log", []):
        if t.get("side") != "buy" or not t.get("address"):
            continue
        try:
            ts = datetime.fromisoformat(t["ts"]).timestamp()
        except Exception:
            ts = cutoff
        if ts < cutoff:
            continue
        key = (t["symbol"], t.get("chain", "sol"))
        if key in seen:
            continue
        seen.add(key)
        out.append((t["symbol"], t.get("chain", "sol"), t["address"]))
    return out


def run(modes: List[str], days: int,
        tokens: Optional[List[Tuple[str, str, str]]] = None) -> Dict[str, Dict[str, Any]]:
    if tokens is None:
        tokens = tokens_from_trade_log(days)
    if not tokens:
        print("No tokens to backtest — pass --tokens or run the bot first to build a trade log.")
        return {}

    print(f"Fetching {len(tokens)} token histories from GeckoTerminal "
          f"(real {days}-day price paths)…")
    histories: Dict[Tuple[str, str, str], List[Dict[str, float]]] = {}
    for sym, chain, addr in tokens:
        h = fetch_history(chain, addr, days)
        if h:
            histories[(sym, chain, addr)] = h
        print(f"  {sym:<12} {chain:<6} {len(h):>4} candles")
        _real_time.sleep(2.2)   # respect GeckoTerminal's free rate limit

    agg: Dict[str, Dict[str, Any]] = {
        m: {"pnl": 0.0, "wins": 0, "trades": 0, "peak": 0.0} for m in modes}
    for (sym, chain, addr), h in histories.items():
        for m in modes:
            r = replay_episode(sym, chain, addr, h, m)
            if not r:
                continue
            a = agg[m]
            a["pnl"]    += r["pnl"]
            a["trades"] += 1
            a["wins"]   += 1 if r["win"] else 0
            a["peak"]    = max(a["peak"], r["peak_gain_pct"])

    print("\n" + "=" * 64)
    print(f"BACKTEST — {len(histories)} tokens, {days}-day real price paths")
    print("=" * 64)
    print(f"{'MODE':<9}{'P&L':>12}{'TRADES':>9}{'WIN%':>8}{'BEST PEAK':>12}")
    print("-" * 64)
    for m in modes:
        a = agg[m]
        win = (a["wins"] / a["trades"] * 100) if a["trades"] else 0
        print(f"{m:<9}{('+$' if a['pnl']>=0 else '-$')+format(abs(a['pnl']),'.2f'):>12}"
              f"{a['trades']:>9}{win:>7.0f}%{('+'+format(a['peak'],'.0f')+'%'):>12}")
    print("-" * 64)
    print("Caveats: entry at start of window (this backtests EXIT + SIZING, where the")
    print("  big losses were). Buyer/seller gate skipped (no OHLCV trade split).")
    print("  Liquidity held constant (OHLCV has no historical liq) → models price exits")
    print("  (TP ladder, trailing, stall, velocity, SL) but NOT rug/liq-drain exits.")
    print("  Slippage, gas and exit price-impact ARE modelled (same code as live).")
    return agg


def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="Replay the past days through the bot's real strategy")
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--modes", default="safe,default,hype,degen")
    ap.add_argument("--tokens", default="", help="comma list of chain:address (overrides trade log)")
    args = ap.parse_args(argv)

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    tokens = None
    if args.tokens:
        tokens = []
        for item in args.tokens.split(","):
            chain, _, addr = item.partition(":")
            tokens.append((addr[:6].upper(), chain.strip(), addr.strip()))
    else:
        bot.load_state()   # so tokens_from_trade_log sees the live history
    run(modes, args.days, tokens)


if __name__ == "__main__":
    main()
