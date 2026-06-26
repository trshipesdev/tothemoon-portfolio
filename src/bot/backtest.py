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
    python src/bot/backtest.py                            # last 7 days, fresh scanner universe
    python src/bot/backtest.py --days 7 --limit 50 --verbose
    python src/bot/backtest.py --source tradelog --days 3   # only tokens the bot traded
    python src/bot/backtest.py --tokens sol:So111...,eth:0xabc --days 2

By default it pulls a fresh trending token UNIVERSE from the online scanners
(GeckoTerminal trending pools + DexScreener boosts), grabs each one's real price
history, and fast-forwards the whole week through every mode in seconds (a sim).

Honest caveats (printed in the report too):
  • Entry is at the START of the fetched window, not the exact live trending-feed
    moment — this backtests EXIT + SIZING behaviour, which is where the big losses
    were, not the candidate-selection feed (we don't have a historical trending feed).
  • The buyer/seller gate is skipped (OHLCV has no per-trade buy/sell split).
  • Slippage, gas and exit price-impact ARE modelled (same code as live).
"""
from __future__ import annotations

import argparse
import json
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

# pool_addr → reserve_in_usd, populated by fetch_universe so fetch_history doesn't
# have to re-fetch the pool just to learn its liquidity.
_POOL_LIQ: Dict[str, float] = {}


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


def _timeframe_for(days: int) -> Tuple[str, int, float]:
    """Pick a candle size that covers `days` within GeckoTerminal's 1000-candle cap.
    Returns (timeframe, aggregate, vol_to_hourly_factor)."""
    if days <= 3:   return ("minute", 5,  12.0)   # 5-min  → 3.5 days max
    if days <= 7:   return ("minute", 15, 4.0)    # 15-min → 10 days max
    return ("hour", 1, 1.0)                        # hourly → weeks


def fetch_history(chain: str, address: str, days: int,
                  pool_addr: str = "") -> List[Dict[str, float]]:
    """Return a list of {ts, price, liq, vol_h1} candles (oldest→newest) for a token.
    Candle size auto-scales so a full week fits. Pass pool_addr to skip the pool lookup."""
    net = GT_NET.get(chain)
    if not net or not address:
        return []
    pool_liq = 0.0
    if not pool_addr:
        pools = _gt_get(f"/networks/{net}/tokens/{address}/pools")
        try:
            pool_addr = pools["data"][0]["attributes"]["address"]
            pool_liq  = float(pools["data"][0]["attributes"].get("reserve_in_usd") or 0)
        except Exception:
            return []
    else:
        # pool address came from the universe — use its cached liquidity (the filter
        # needs it, otherwise liq=0 rejects every token). Fall back to a pool fetch.
        pool_liq = _POOL_LIQ.get(pool_addr, 0.0)
        if pool_liq <= 0:
            p = _gt_get(f"/networks/{net}/pools/{pool_addr}")
            try:
                pool_liq = float(p["data"]["attributes"].get("reserve_in_usd") or 0)
            except Exception:
                pool_liq = 0.0
    tf, agg, vol_factor = _timeframe_for(days)
    per_day = 1440 / (agg if tf == "minute" else agg * 60)
    limit = min(1000, int(max(1, days) * per_day) + 1)
    ohlcv = _gt_get(f"/networks/{net}/pools/{pool_addr}/ohlcv/{tf}"
                    f"?aggregate={agg}&limit={limit}&currency=usd")
    try:
        rows = ohlcv["data"]["attributes"]["ohlcv_list"]
    except Exception:
        return []
    out: List[Dict[str, float]] = []
    for ts, _o, _h, _l, close, vol in rows:
        out.append({"ts": float(ts), "price": float(close),
                    "liq": pool_liq, "vol_h1": float(vol or 0) * vol_factor})
    out.sort(key=lambda r: r["ts"])
    return out


def fetch_universe(limit: int = 30, chains: Optional[List[str]] = None
                   ) -> List[Tuple[str, str, str, str]]:
    """Pull a broad token universe from the online scanners — GeckoTerminal trending
    pools (across chains) + DexScreener boosts — so the backtest isn't limited to what
    the bot already traded. Returns (symbol, chain, token_addr, pool_addr)."""
    chains = chains or ["sol", "eth", "base", "bsc"]
    out: List[Tuple[str, str, str, str]] = []
    seen = set()

    # GeckoTerminal trending pools — gives base token + pool address directly
    rev_net = {v: k for k, v in GT_NET.items()}
    for chain in chains:
        net = GT_NET.get(chain)
        if not net:
            continue
        data = _gt_get(f"/networks/{net}/trending_pools?duration=24h")
        for pool in (data or {}).get("data", []) or []:
            try:
                attr = pool["attributes"]
                pool_addr = attr["address"]
                tok = pool["relationships"]["base_token"]["data"]["id"]   # "net_addr"
                token_addr = tok.split("_", 1)[1]
                sym = (attr.get("name", "") or "").split("/")[0].strip()[:12] or token_addr[:6]
            except Exception:
                continue
            key = (token_addr, chain)
            if key in seen:
                continue
            seen.add(key)
            _POOL_LIQ[pool_addr] = float(attr.get("reserve_in_usd") or 0)   # cache liq
            out.append((sym, chain, token_addr, pool_addr))
        _real_time.sleep(2.2)   # GeckoTerminal free rate limit

    # DexScreener boosts — currently-promoted tokens (pool resolved later by fetch_history)
    boosts = _gt_get  # noqa — kept import-light; use bot's DexScreener helper instead
    try:
        ds = bot._get(bot.DEXSCREENER_BOOSTS) or []
        for item in ds:
            chain_id = item.get("chainId", "")
            chain = {"solana": "sol", "ethereum": "eth", "base": "base",
                     "bsc": "bsc"}.get(chain_id)
            addr = item.get("tokenAddress", "")
            if not chain or not addr or (addr, chain) in seen:
                continue
            seen.add((addr, chain))
            out.append((addr[:6].upper(), chain, addr, ""))   # pool resolved on fetch
    except Exception:
        pass

    return out[:limit]


# ── Entry timing ────────────────────────────────────────────────────────────────
def _hype_of(tick: Dict[str, float]) -> int:
    """Same hype formula the live scanner uses: h1 volume velocity vs liquidity."""
    return min(100, int(tick["vol_h1"] / max(tick["liq"], 1) * 40))


def find_entry_index(ticks: List[Dict[str, float]], lookback: int = 3) -> Optional[int]:
    """First tick that passes the bot's REAL moonshot filter — i.e. momentum is
    building (price rising over `lookback` candles) AND volume velocity clears the
    hype floor. This stops the backtest from blindly buying a trending token at the
    top of the window; a token that only bleeds never triggers an entry → no trade.
    Uses the live filter, so the active mode's liq floor + hype_min apply."""
    for i in range(lookback, len(ticks)):
        t = ticks[i]
        positive = t["price"] > ticks[i - lookback]["price"]
        sc = bot.Score(_hype_of(t), t["liq"], 0.0, positive)   # age 0 = fresh; buy_ratio unknown
        if bot.moonshot_reject_reason(sc) is None:
            return i
    return None


# ── Replay one token under one mode ─────────────────────────────────────────────
def replay_episode(symbol: str, chain: str, address: str,
                   ticks: List[Dict[str, float]], mode: str,
                   start_vault: float = 1000.0,
                   entry: str = "momentum") -> Optional[Dict[str, Any]]:
    """Enter when a real momentum signal fires (default) or at the first tick
    (entry="start"), then feed every later tick through the REAL manage_positions()
    exit cascade. Returns the episode result, or None if the token never qualified."""
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

        # Pick the entry tick. "momentum" = first tick that passes the live filter
        # (don't buy a token that's only declining). "start" = old behaviour.
        e = 0 if entry == "start" else find_entry_index(ticks)
        if e is None or e >= len(ticks) - 1:
            return None                                # never built momentum → no trade
        first = ticks[e]
        clock.now = first["ts"]
        sc = bot.Score(_hype_of(first), first["liq"], 0.0, True)
        if entry == "start" and bot.moonshot_reject_reason(sc) and \
           bot.moonshot_reject_reason(sc).startswith("liquidity"):
            return None                                # too thin for this mode → no entry

        usd = bot.size_ticket_usd(chain, hype=_hype_of(first))
        if usd < bot.CONFIG["moonshot"]["min_ticket_usd"]:
            return None
        bot.shadow_buy(symbol, chain, usd, first["price"], first["liq"], address)
        entry_price = bot.STATE["positions"][symbol]["avg"]
        peak_gain = 0.0

        for i in range(e + 1, len(ticks)):
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
        hold_min = (clock.now - first["ts"]) / 60.0
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


# ── Replay from the live FORWARD RECORDING (most faithful) ──────────────────────
def histories_from_recording(days: int, rec_dir: str = ""
                             ) -> Dict[Tuple[str, str, str], List[Dict[str, float]]]:
    """Reconstruct per-token tick series from the bot's forward recording (the
    per-tick price/liq/vol it logged while holding each position). Because these are
    the REAL ticks of tokens it really entered, the replay has real entry timing AND
    real per-tick liquidity → rug/liq-drain exits get modelled too."""
    import glob
    rec_dir = rec_dir or os.path.join(os.path.dirname(bot.SAVEFILE) or ".", "recordings")
    cutoff = bot.now_utc().timestamp() - days * 86400
    series: Dict[Tuple[str, str, str], List[Dict[str, float]]] = {}
    for path in sorted(glob.glob(os.path.join(rec_dir, "*.jsonl"))):
        try:
            with open(path) as f:
                for line in f:
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    if ev.get("ev") != "tick" or ev.get("ts", 0) < cutoff:
                        continue
                    key = (ev.get("symbol", "?"), ev.get("chain", "sol"), ev.get("address", ""))
                    series.setdefault(key, []).append({
                        "ts": float(ev["ts"]), "price": float(ev["price"]),
                        "liq": float(ev.get("liq", 0)), "vol_h1": float(ev.get("vol_h1", 0)),
                    })
        except Exception:
            continue
    for k in series:
        series[k].sort(key=lambda r: r["ts"])
    return {k: v for k, v in series.items() if len(v) >= 2}


# ── Drive the whole backtest ────────────────────────────────────────────────────
def tokens_from_trade_log(days: int) -> List[Tuple[str, str, str, str]]:
    """Distinct (symbol, chain, token_addr, pool_addr) the live bot bought in `days`."""
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
        out.append((t["symbol"], t.get("chain", "sol"), t["address"], ""))
    return out


def run(modes: List[str], days: int,
        tokens: Optional[List[Tuple[str, str, str, str]]] = None,
        verbose: bool = False, entry: str = "momentum", recorded: bool = False,
        histories: Optional[Dict[Tuple[str, str, str], List[Dict[str, float]]]] = None
        ) -> Dict[str, Dict[str, Any]]:
    if histories is None:
        if tokens is None:
            tokens = tokens_from_trade_log(days)
        if not tokens:
            print("No tokens to backtest — pass --tokens, use --source scanner/recording, or run the bot first.")
            return {}
        print(f"Fetching {len(tokens)} token histories (real {days}-day price paths)…")
        histories = {}
        for sym, chain, addr, pool in tokens:
            h = fetch_history(chain, addr, days, pool_addr=pool)
            if h:
                histories[(sym, chain, addr)] = h
            print(f"  {sym:<12} {chain:<6} {len(h):>4} candles")
            _real_time.sleep(2.2)   # respect GeckoTerminal's free rate limit
    if not histories:
        print("No price history found for any token.")
        return {}

    # ── Replay (this is the fast 'sim': a whole week crunches in seconds) ──
    t0 = _real_time.time()
    agg: Dict[str, Dict[str, Any]] = {
        m: {"pnl": 0.0, "wins": 0, "trades": 0, "peak": 0.0} for m in modes}
    candle_count = 0
    for (sym, chain, addr), h in histories.items():
        candle_count += len(h)
        row = []
        for m in modes:
            r = replay_episode(sym, chain, addr, h, m, entry=entry)
            if not r:
                row.append(f"{m}: —")
                continue
            a = agg[m]
            a["pnl"]    += r["pnl"]
            a["trades"] += 1
            a["wins"]   += 1 if r["win"] else 0
            a["peak"]    = max(a["peak"], r["peak_gain_pct"])
            row.append(f"{m}: {'+' if r['pnl']>=0 else ''}{r['pnl']:.1f} (pk +{r['peak_gain_pct']:.0f}%)")
        if verbose:
            print(f"  ▸ {sym:<12} " + "  ".join(row))
    sim_sec = _real_time.time() - t0
    print(f"\nSimulated {candle_count:,} candles × {len(modes)} modes in {sim_sec:.2f}s "
          f"({int(candle_count*len(modes)/max(sim_sec,1e-3)):,} candle-runs/sec).")

    print("=" * 64)
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
    if recorded:
        print("Source: live RECORDING — real entry moments + real per-tick liquidity, so")
        print("  rug/liq-drain exits ARE modelled. This is the faithful backtest.")
    else:
        ent = "momentum (enters only when the live filter would)" if entry == "momentum" \
              else "start-of-window (buys the top of trending tokens)"
        print(f"Caveats: entry = {ent}. Buyer/seller gate skipped (no OHLCV trade split).")
        print("  Liquidity held constant (OHLCV has no historical liq) → models price exits")
        print("  but NOT rug/liq-drain. Run --source recording (after the bot records a few")
        print("  days) for real entry timing + liquidity. Slippage+gas+exit-impact modelled.")
    return agg


def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="Replay the past week through the bot's real strategy")
    ap.add_argument("--days", type=int, default=7, help="how many days of history (default 7)")
    ap.add_argument("--modes", default="safe,default,hype,degen")
    ap.add_argument("--source", choices=["scanner", "tradelog", "recording"], default="scanner",
                    help="scanner = fresh trending universe; tradelog = tokens the bot traded; "
                         "recording = the bot's own forward recording (most faithful)")
    ap.add_argument("--limit", type=int, default=30, help="max tokens to pull (scanner source)")
    ap.add_argument("--tokens", default="", help="comma list of chain:address (overrides --source)")
    ap.add_argument("--entry", choices=["momentum", "start"], default="momentum",
                    help="momentum = enter only when the live filter fires; start = buy at window open")
    ap.add_argument("--verbose", action="store_true", help="print each token's per-mode result")
    args = ap.parse_args(argv)

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    tokens = None
    if args.tokens:
        tokens = []
        for item in args.tokens.split(","):
            chain, _, addr = item.partition(":")
            tokens.append((addr[:6].upper(), chain.strip(), addr.strip(), ""))
    elif args.source == "recording":
        bot.load_state()
        hist = histories_from_recording(args.days)
        if not hist:
            print("No recording found yet. The bot records as it trades — give it a few "
                  "hours/days, then re-run with --source recording.")
            return
        # recorded ticks start at the real entry moment → enter at the first tick
        run(modes, args.days, verbose=args.verbose, entry="start",
            recorded=True, histories=hist)
        return
    elif args.source == "scanner":
        print(f"Pulling a {args.limit}-token trending universe from the online scanners…")
        tokens = fetch_universe(limit=args.limit)
    else:
        bot.load_state()   # so tokens_from_trade_log sees the live history
    run(modes, args.days, tokens, verbose=args.verbose, entry=args.entry)


if __name__ == "__main__":
    main()
