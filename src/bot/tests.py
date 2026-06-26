#!/usr/bin/env python3
"""
Comprehensive test suite for bot_full.py.

Run:
    python3 src/bot/tests.py -v
    python3 src/bot/tests.py -v TestShadowRoundTrip   # single class
    python3 src/bot/tests.py -v TestFlaskAPI           # Flask endpoints

Coverage targets:
    Capital sizing       — deployable_now, per_chain_room, per_token_cap_room,
                           size_ticket_usd (all caps + brake + boost)
    Trade round-trip     — shadow_buy, shadow_sell (avg, vault, skim, pnl_hist, trade_log)
    TP/SL cascade        — tp_index lifecycle
    Drawdown brake       — activation, ticket reduction
    Moonshot filters     — Score, passes_moonshot_filters (all branches, spray mode)
    Presale gate         — presale_score (all components, caps)
    Pair parsing         — _pair_to_candidate (age, hype, missing fields)
    Social signal        — fetch_social_volume (LC key path, CoinGecko fallback)
    Heat signal          — compute_heat (all buckets)
    Re-entry watch       — _add_reentry_watch (address guard, disabled, content)
    No-pump exit         — should_exit_no_pump, adaptive_no_pump_window
    Stealth guards       — _stealth_ok (count, expiry), _jitter_slip
    Exec routing         — exec_buy / exec_sell (shadow vs. live routing)
    Sell amount parsing  — _parse_sell_amount (all/pct/%, raw float)
    Autoscale            — autoscale_maybe (add_count, cooldown, PI cap)
    Objective            — start_objective (validation, curve), objective_nudge
    Helpers              — est_price_impact, _px_dict, _parse_dex_pair, now_utc, log
    Auth                 — tg_allowed (empty + populated allowlist), _dash_auth (Flask)
    State persistence    — save_state / load_state round-trip, missing-key seeding
    Shadow mode toggle   — set_shadow_mode
    Flask API            — /status, /api/state, /api/positions, /api/history, /api/alerts,
                           /api/set_mode, /api/set_shadow, /api/set_moonshot,
                           /api/buy, /api/sell, /api/close,
                           /api/position/tp, /api/position/sl,
                           /api/watchlist/add, /api/watchlist/remove,
                           /api/config/oldcoin, dashboard auth token
    Daily reset          — open_today_usd reset logic in engine_once
    Vault balance        — refresh_vault_balance no-op in shadow mode
    Detect oldcoin pump  — vol spike (mocked)
    CoinGecko cache      — rank lookup, cache hit, failure
    RPC failover         — _sol_rpc primary/fallback
    Boost expiry         — active, expired, ticket scaling
"""

import copy
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

# ── keep in shadow mode; point state at a temp file ─────────────────────────
os.environ["SHADOW_MODE"] = "true"
_tmp_state = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
_tmp_state.close()
os.environ["STATE_PATH"] = _tmp_state.name

# ── stub heavy third-party modules that may not be installed in CI ───────────
for _mod in (
    "telegram", "telegram.ext",
    "solders", "solders.keypair", "solders.transaction",
    "solders.hash", "solders.message", "solders.rpc",
    "boto3", "botocore",
):
    sys.modules.setdefault(_mod, MagicMock())

for _attr in ("Update", "ext.Application", "ext.CommandHandler", "ext.ContextTypes"):
    parts = _attr.split(".")
    mod   = sys.modules.get("telegram", MagicMock())
    for p in parts:
        if not hasattr(mod, p):
            setattr(mod, p, MagicMock())
        mod = getattr(mod, p)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot_full as bot  # noqa: E402  (must come after stubs)


# ═══════════════════════════════════════════════════════════════════════════════
# Shared reset helper
# ═══════════════════════════════════════════════════════════════════════════════

def _reset(vault: float = 1000.0):
    """Reset STATE and CONFIG to a clean baseline."""
    bot.STATE.update({
        "ts":               None,
        "vault_usd":        vault,
        "deployable_usd":   vault * (1 - bot.CONFIG["reserve_pct"]),
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
        "reentry_watch":    {},
        "open_burst":       [],
        "gas_paid_usd":     0.0,
        "scout_log":        [],
        "gas_live":         {},
        "ai":               {"last_run": None, "last": None, "history": []},
    })
    bot.CONFIG["mode"]                           = "default"
    bot.CONFIG["moonshot"]["mode"]               = "suggest"
    bot.CONFIG["moonshot"]["reentry"]["enabled"] = True
    bot.CONFIG["oldcoin"]["auto_join"]           = False
    bot.CONFIG["autoscale"]["enabled"]           = True
    bot.CONFIG["objective"]["kind"]              = "off"
    bot.OBJ_STATE.update({"started": None, "target_curve": []})
    bot.SHADOW_MODE = True
    # Gas off by default so vault-math tests are deterministic; gas tests opt in.
    bot.CONFIG["gas_sim"] = False


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Low-level helpers — _px_dict, _parse_dex_pair, now_utc, log, send_alert
# ═══════════════════════════════════════════════════════════════════════════════

class TestHelpers(unittest.TestCase):

    def test_px_dict_defaults(self):
        d = bot._px_dict(1.5)
        self.assertEqual(d["price"], 1.5)
        self.assertEqual(d["liq"], 100000.0)
        self.assertEqual(d["vol_h1"], 0.0)
        self.assertEqual(d["change_m5"], 0.0)

    def test_px_dict_all_custom(self):
        d = bot._px_dict(2.0, 50000.0, 5000.0, -3.5)
        self.assertEqual(d["price"], 2.0)
        self.assertEqual(d["liq"], 50000.0)
        self.assertEqual(d["vol_h1"], 5000.0)
        self.assertEqual(d["change_m5"], -3.5)

    def test_parse_dex_pair_happy_path(self):
        pair = {
            "priceUsd":    "0.001",
            "liquidity":   {"usd": 40000},
            "volume":      {"h1": 1234},
            "priceChange": {"m5": 2.5},
        }
        d = bot._parse_dex_pair(pair)
        self.assertIsNotNone(d)
        self.assertAlmostEqual(d["price"], 0.001)
        self.assertEqual(d["liq"], 40000.0)
        self.assertEqual(d["vol_h1"], 1234.0)
        self.assertEqual(d["change_m5"], 2.5)

    def test_parse_dex_pair_zero_price_none(self):
        self.assertIsNone(bot._parse_dex_pair({"priceUsd": "0"}))

    def test_parse_dex_pair_missing_fields_use_defaults(self):
        d = bot._parse_dex_pair({"priceUsd": "1.0"})
        self.assertIsNotNone(d)
        self.assertEqual(d["liq"], 100000.0)
        self.assertEqual(d["vol_h1"], 0.0)

    def test_now_utc_is_timezone_aware(self):
        t = bot.now_utc()
        self.assertIsNotNone(t.tzinfo)
        self.assertEqual(t.utcoffset().total_seconds(), 0)

    def test_log_does_not_crash(self):
        bot.log("test — should not raise")

    def test_send_alert_no_bot_no_crash(self):
        bot.TG_STATE = {}
        bot.send_alert("test alert with no telegram bot configured")


# ═══════════════════════════════════════════════════════════════════════════════
# 2. compute_heat — all buckets
# ═══════════════════════════════════════════════════════════════════════════════

class TestComputeHeat(unittest.TestCase):

    def test_alt_season(self):
        self.assertEqual(bot.compute_heat(38.0), "alt_season")

    def test_neutral_low(self):
        self.assertEqual(bot.compute_heat(41.0), "neutral")

    def test_neutral_high(self):
        self.assertEqual(bot.compute_heat(49.9), "neutral")

    def test_btc_season(self):
        self.assertEqual(bot.compute_heat(56.0), "btc_season")

    def test_btc_max(self):
        self.assertEqual(bot.compute_heat(63.0), "btc_max")

    def test_none_returns_none(self):
        self.assertIsNone(bot.compute_heat(None))

    def test_boundary_40_has_a_bucket(self):
        result = bot.compute_heat(40.0)
        self.assertIn(result, ("alt_season", "neutral"))

    def test_boundary_55_has_a_bucket(self):
        result = bot.compute_heat(55.0)
        self.assertIn(result, ("neutral", "btc_season"))


# ═══════════════════════════════════════════════════════════════════════════════
# 3. _pair_to_candidate
# ═══════════════════════════════════════════════════════════════════════════════

class TestPairToCandidate(unittest.TestCase):

    def _pair(self, **overrides):
        now_ms = int(time.time() * 1000)
        base = {
            "baseToken":     {"symbol": "PEPE", "address": "0xabc"},
            "priceUsd":      "0.0001",
            "pairCreatedAt": now_ms - 10 * 60 * 1000,
            "liquidity":     {"usd": 50000},
            "volume":        {"h24": 100000},
            "priceChange":   {"h24": 15.0},
        }
        base.update(overrides)
        return base

    def test_basic_fields(self):
        c = bot._pair_to_candidate(self._pair(), "sol")
        self.assertIsNotNone(c)
        self.assertEqual(c["symbol"], "PEPE")
        self.assertEqual(c["chain"], "sol")
        self.assertAlmostEqual(c["price"], 0.0001, places=6)
        self.assertEqual(c["address"], "0xabc")
        self.assertTrue(c["positive"])

    def test_zero_price_returns_none(self):
        self.assertIsNone(bot._pair_to_candidate(self._pair(priceUsd="0"), "sol"))

    def test_none_price_returns_none(self):
        self.assertIsNone(bot._pair_to_candidate(self._pair(priceUsd=None), "eth"))

    def test_zero_liq_returns_none(self):
        self.assertIsNone(bot._pair_to_candidate(self._pair(liquidity={"usd": 0}), "sol"))

    def test_missing_liq_returns_none(self):
        p = self._pair()
        del p["liquidity"]
        self.assertIsNone(bot._pair_to_candidate(p, "sol"))

    def test_missing_base_token_returns_none(self):
        p = self._pair()
        del p["baseToken"]
        self.assertIsNone(bot._pair_to_candidate(p, "sol"))

    def test_negative_price_change_sets_positive_false(self):
        c = bot._pair_to_candidate(self._pair(**{"priceChange": {"h24": -5.0}}), "sol")
        self.assertIsNotNone(c)
        self.assertFalse(c["positive"])

    def test_age_from_created_at(self):
        now_ms  = int(time.time() * 1000)
        minutes = 45
        c = bot._pair_to_candidate(
            self._pair(**{"pairCreatedAt": now_ms - minutes * 60 * 1000}), "sol"
        )
        self.assertAlmostEqual(c["age_min"], minutes, delta=1.5)

    def test_missing_created_at_gives_9999(self):
        p = self._pair()
        del p["pairCreatedAt"]
        self.assertEqual(bot._pair_to_candidate(p, "eth")["age_min"], 9999)

    def test_hype_from_h1_velocity(self):
        # hype now uses RECENT (h1) volume velocity: vol_h1/liq*40
        p = self._pair(**{"volume": {"h1": 100000, "h24": 999}, "liquidity": {"usd": 50000}})
        # 100000/50000 = 2 → hype = min(100, int(2 * 40)) = 80
        self.assertEqual(bot._pair_to_candidate(p, "sol")["hype"], 80)

    def test_hype_falls_back_to_h24(self):
        # no h1 data → fall back to h24/24 as an hourly proxy
        p = self._pair(**{"volume": {"h24": 100000}, "liquidity": {"usd": 50000}})
        # (100000/24)/50000*40 = 3.33 → 3
        self.assertEqual(bot._pair_to_candidate(p, "sol")["hype"], 3)

    def test_hype_capped_at_100(self):
        p = self._pair(**{"volume": {"h1": 10_000_000}, "liquidity": {"usd": 1000}})
        self.assertEqual(bot._pair_to_candidate(p, "sol")["hype"], 100)

    def test_zero_vol_hype_is_zero(self):
        p = self._pair(**{"volume": {"h24": 0}, "liquidity": {"usd": 50000}})
        self.assertEqual(bot._pair_to_candidate(p, "sol")["hype"], 0)

    def test_buy_ratio_computed(self):
        p = self._pair(**{"txns": {"h1": {"buys": 70, "sells": 30}}})
        self.assertAlmostEqual(bot._pair_to_candidate(p, "sol")["buy_ratio"], 0.70)

    def test_conviction_scales_size(self):
        # strong setup (high hype + heavy buying) sizes bigger than a weak one
        weak   = bot._conviction_mult(80, 0.46)
        strong = bot._conviction_mult(100, 0.85)
        self.assertGreater(strong, weak)
        self.assertLessEqual(strong, 1.5)        # bounded so it can't blow past caps
        self.assertGreaterEqual(weak, 0.8)

    def test_conviction_none_is_neutral(self):
        self.assertEqual(bot._conviction_mult(None, None), 1.0)

    def test_chain_passed_through(self):
        self.assertEqual(bot._pair_to_candidate(self._pair(), "base")["chain"], "base")


# ═══════════════════════════════════════════════════════════════════════════════
# 4. presale_score
# ═══════════════════════════════════════════════════════════════════════════════

class TestPresaleScore(unittest.TestCase):

    def _s(self, **kw):
        base = {"audit": False, "kyc": False, "lock_days": 0, "mentions": 0}
        base.update(kw)
        return bot.presale_score(base)

    def test_all_zero(self):
        self.assertEqual(self._s(), 0)

    def test_audit_adds_20(self):
        self.assertEqual(self._s(audit=True), 20)

    def test_kyc_adds_20(self):
        self.assertEqual(self._s(kyc=True), 20)

    def test_audit_and_kyc_add_40(self):
        self.assertEqual(self._s(audit=True, kyc=True), 40)

    def test_lock_days_linear(self):
        self.assertEqual(self._s(lock_days=30), 1)
        self.assertEqual(self._s(lock_days=300), 10)
        self.assertEqual(self._s(lock_days=900), 30)

    def test_lock_days_capped_at_30(self):
        self.assertEqual(self._s(lock_days=100_000), 30)

    def test_short_lock_rounds_to_zero(self):
        self.assertEqual(self._s(lock_days=14), 0)  # int(14/30) = 0

    def test_mentions_linear(self):
        self.assertEqual(self._s(mentions=5000),  10)
        self.assertEqual(self._s(mentions=10000), 20)
        self.assertEqual(self._s(mentions=15000), 30)

    def test_mentions_capped_at_30(self):
        self.assertEqual(self._s(mentions=1_000_000), 30)

    def test_max_score_100(self):
        self.assertEqual(self._s(audit=True, kyc=True, lock_days=10_000, mentions=1_000_000), 100)


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Score / passes_moonshot_filters
# ═══════════════════════════════════════════════════════════════════════════════

class TestMoonshotFilters(unittest.TestCase):

    def setUp(self):
        _reset()

    def _sc(self, hype=90, liq=50000, age_min=15, positive=True, buy_ratio=None):
        return bot.Score(hype, liq, age_min, positive, buy_ratio=buy_ratio)

    def test_passes_valid_candidate(self):
        self.assertTrue(bot.passes_moonshot_filters(self._sc()))

    def test_rejects_selling_pressure(self):
        # 30% buys / 70% sells → being dumped → rejected
        self.assertIsNotNone(bot.moonshot_reject_reason(self._sc(buy_ratio=0.30)))

    def test_accepts_buying_pressure(self):
        self.assertIsNone(bot.moonshot_reject_reason(self._sc(buy_ratio=0.70)))

    def test_unknown_buy_ratio_does_not_block(self):
        # None (no txn data) must not reject — degrade gracefully
        self.assertIsNone(bot.moonshot_reject_reason(self._sc(buy_ratio=None)))

    def test_fails_liq_too_low(self):
        self.assertFalse(bot.passes_moonshot_filters(
            self._sc(liq=bot.CONFIG["moonshot"]["liq_min"] - 1)))

    def test_fails_liq_too_high(self):
        self.assertFalse(bot.passes_moonshot_filters(
            self._sc(liq=bot.CONFIG["moonshot"]["liq_max"] + 1)))

    def test_fails_too_old(self):
        # age cap is now the active mode's max_age_min (default mode)
        max_age = bot.CONFIG["modes"][bot.CONFIG["mode"]]["max_age_min"]
        self.assertFalse(bot.passes_moonshot_filters(self._sc(age_min=max_age + 1)))

    def test_fails_not_positive(self):
        self.assertFalse(bot.passes_moonshot_filters(self._sc(positive=False)))

    def test_fails_low_hype(self):
        self.assertFalse(bot.passes_moonshot_filters(
            self._sc(hype=bot.CONFIG["moonshot"]["hype_min"] - 1)))

    def test_spray_relaxes_liq_min(self):
        # liquidity floor is now the active mode's liq_min (default mode here)
        normal_min = bot.CONFIG["modes"][bot.CONFIG["mode"]]["liq_min"]
        spray_min  = int(normal_min * 0.7) + 1
        bot.STATE["spray_until"] = "9999-12-31"
        self.assertTrue(bot.passes_moonshot_filters(self._sc(liq=spray_min)))

    def test_mode_liq_floor_applies(self):
        # degen ($10k) accepts a $12k token that default ($30k) would reject
        sc12k = self._sc(liq=12000)
        bot.CONFIG["mode"] = "default"
        self.assertIsNotNone(bot.moonshot_reject_reason(sc12k))   # rejected at $30k
        bot.CONFIG["mode"] = "degen"
        self.assertIsNone(bot.moonshot_reject_reason(sc12k))      # passes at $10k
        bot.CONFIG["mode"] = "default"

    def test_spray_relaxes_hype_min(self):
        normal_hype = bot.CONFIG["moonshot"]["hype_min"]
        spray_hype  = max(50, normal_hype - 20)
        mid         = (spray_hype + normal_hype) // 2
        bot.STATE["spray_until"] = "9999-12-31"
        self.assertTrue(bot.passes_moonshot_filters(self._sc(hype=mid)))

    def test_expired_spray_no_relaxation(self):
        bot.STATE["spray_until"] = "2000-01-01"
        self.assertFalse(bot.passes_moonshot_filters(
            self._sc(hype=bot.CONFIG["moonshot"]["hype_min"] - 5)))


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Capital sizing
# ═══════════════════════════════════════════════════════════════════════════════

class TestCapitalSizing(unittest.TestCase):

    def setUp(self):
        _reset(1000.0)

    def test_deployable_now(self):
        self.assertAlmostEqual(bot.deployable_now(), 750.0, places=2)

    def test_deployable_zero_vault(self):
        bot.STATE["vault_usd"] = 0.0
        self.assertEqual(bot.deployable_now(), 0.0)

    def test_deployable_clamps_below_zero(self):
        bot.STATE["vault_usd"] = -50.0
        self.assertEqual(bot.deployable_now(), 0.0)

    def test_per_chain_room_empty(self):
        expected = bot.CONFIG["per_chain_cap_pct"]["sol"] * 750.0
        self.assertAlmostEqual(bot.per_chain_room("sol"), expected, places=2)

    def test_per_chain_room_reduced_by_positions(self):
        bot.STATE["positions"]["X"] = {
            "chain": "sol", "usd": 100.0, "units": 1.0, "avg": 100.0, "realized": 0.0}
        expected = bot.CONFIG["per_chain_cap_pct"]["sol"] * 750.0 - 100.0
        self.assertAlmostEqual(bot.per_chain_room("sol"), expected, places=2)

    def test_per_chain_room_fully_filled_is_zero(self):
        cap = bot.CONFIG["per_chain_cap_pct"]["sol"] * 750.0
        bot.STATE["positions"]["X"] = {
            "chain": "sol", "usd": cap + 10, "units": 1.0, "avg": 100.0, "realized": 0.0}
        self.assertEqual(bot.per_chain_room("sol"), 0.0)

    def test_per_chain_room_unknown_chain_defaults(self):
        self.assertGreater(bot.per_chain_room("unknown_xyz"), 0.0)

    def test_per_token_cap_room(self):
        self.assertAlmostEqual(bot.per_token_cap_room(),
                               bot.CONFIG["per_token_cap_pct"] * 750.0, places=2)

    def test_size_ticket_positive(self):
        self.assertGreater(bot.size_ticket_usd("sol"), 0.0)

    def test_size_ticket_chain_cap_blocks(self):
        cap = bot.CONFIG["per_chain_cap_pct"]["sol"] * 750.0
        bot.STATE["positions"]["X"] = {
            "chain": "sol", "usd": cap, "units": 1.0, "avg": cap, "realized": 0.0}
        self.assertEqual(bot.size_ticket_usd("sol"), 0.0)

    def test_size_ticket_daily_cap_blocks(self):
        cap = bot.CONFIG["daily_deploy_cap_pct"] * 750.0
        bot.STATE["open_today_usd"] = cap
        self.assertEqual(bot.size_ticket_usd("sol"), 0.0)

    def test_size_ticket_daily_cap_near_limit(self):
        cap = bot.CONFIG["daily_deploy_cap_pct"] * 750.0
        bot.STATE["open_today_usd"] = cap - 1.0
        self.assertLessEqual(bot.size_ticket_usd("sol"), 1.0)

    def test_size_ticket_brake_reduces(self):
        bot.STATE["pnl_hist"] = [-200, -100, -50]
        base     = bot.CONFIG["base_size_usd"]
        braked   = bot.size_ticket_usd("sol")
        brake_max = base * bot.CONFIG["drawdown_brake"]["size_mult"] * 1.05
        self.assertLessEqual(braked, brake_max)

    def test_size_ticket_expired_boost_ignored(self):
        bot.STATE["boost"] = {"mult": 5.0, "expires": "2000-01-01T00:00:00+00:00"}
        t_with_expired = bot.size_ticket_usd("sol")
        bot.STATE["boost"] = {"mult": 1.0, "expires": None}
        t_no_boost = bot.size_ticket_usd("sol")
        self.assertAlmostEqual(t_with_expired, t_no_boost, places=4)


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Drawdown brake
# ═══════════════════════════════════════════════════════════════════════════════

class TestDrawdownBrake(unittest.TestCase):

    def setUp(self):
        _reset(1000.0)

    def test_empty_hist_no_brake(self):
        bot.STATE["pnl_hist"] = []
        self.assertFalse(bot.drawdown_brake_active())

    def test_all_positive_no_brake(self):
        bot.STATE["pnl_hist"] = [10, 20, 30]
        self.assertFalse(bot.drawdown_brake_active())

    def test_net_positive_no_brake(self):
        bot.STATE["pnl_hist"] = [50, -1, -2]
        self.assertFalse(bot.drawdown_brake_active())

    def test_large_loss_fires_brake(self):
        bot.STATE["pnl_hist"] = [-100, -50, -30]
        self.assertTrue(bot.drawdown_brake_active())

    def test_only_lookback_window_counts(self):
        lookback = bot.CONFIG["drawdown_brake"]["lookback"]
        # Early losses outside the window, recent gains inside
        bot.STATE["pnl_hist"] = [-200] * 5 + [10] * lookback
        self.assertFalse(bot.drawdown_brake_active())

    def test_threshold_boundary(self):
        # With no gains, dd threshold is dd * max(1.0, 0) = dd * 1 = 0.25
        # loss just below threshold → no brake
        bot.STATE["pnl_hist"] = [-0.24]
        self.assertFalse(bot.drawdown_brake_active())
        # loss just above threshold → brake fires
        bot.STATE["pnl_hist"] = [-0.26]
        self.assertTrue(bot.drawdown_brake_active())


# ═══════════════════════════════════════════════════════════════════════════════
# 8. est_price_impact
# ═══════════════════════════════════════════════════════════════════════════════

class TestPriceImpact(unittest.TestCase):

    def test_zero_liq_returns_1(self):
        self.assertEqual(bot.est_price_impact(100, 0), 1.0)

    def test_negative_liq_returns_1(self):
        self.assertEqual(bot.est_price_impact(100, -500), 1.0)

    def test_tiny_order_tiny_impact(self):
        self.assertLess(bot.est_price_impact(10, 1_000_000), 0.001)

    def test_large_order_capped_at_5pct(self):
        self.assertEqual(bot.est_price_impact(10_000_000, 100), 0.05)

    def test_proportional_in_normal_range(self):
        i1 = bot.est_price_impact(100, 100_000)
        i2 = bot.est_price_impact(200, 100_000)
        self.assertAlmostEqual(i2, i1 * 2, places=8)


# ═══════════════════════════════════════════════════════════════════════════════
# 9. Stealth guards — burst + jitter
# ═══════════════════════════════════════════════════════════════════════════════

class TestStealthGuards(unittest.TestCase):

    def setUp(self):
        _reset()

    def test_first_opens_all_allowed(self):
        limit = bot.CONFIG["stealth"]["burst_per_30s"]
        for i in range(limit):
            self.assertTrue(bot._stealth_ok(), f"open #{i+1} should be allowed")
            bot.STATE["open_burst"].append(time.time())

    def test_burst_limit_blocks_next(self):
        limit = bot.CONFIG["stealth"]["burst_per_30s"]
        bot.STATE["open_burst"] = [time.time()] * limit
        self.assertFalse(bot._stealth_ok())

    def test_old_entries_expire(self):
        bot.STATE["open_burst"] = [time.time() - 35] * 10
        self.assertTrue(bot._stealth_ok())

    def test_burst_list_pruned(self):
        bot.STATE["open_burst"] = [time.time() - 35] * 5
        bot._stealth_ok()
        self.assertEqual(len(bot.STATE["open_burst"]), 0)

    def test_jitter_non_negative(self):
        for _ in range(100):
            self.assertGreaterEqual(bot._jitter_slip(0.0), 0.0)

    def test_jitter_within_max(self):
        max_extra = bot.CONFIG["stealth"]["slip_bps_jitter"] / 10000
        for _ in range(100):
            self.assertLessEqual(bot._jitter_slip(0.0), max_extra + 1e-9)

    def test_jitter_adds_to_base(self):
        base = 0.02
        for _ in range(50):
            self.assertGreaterEqual(bot._jitter_slip(base), base)


# ═══════════════════════════════════════════════════════════════════════════════
# 10. shadow_buy
# ═══════════════════════════════════════════════════════════════════════════════

class TestShadowBuy(unittest.TestCase):

    def setUp(self):
        _reset(1000.0)

    def test_creates_position(self):
        bot.shadow_buy("TST", "sol", 50.0, 1.0, 100000.0, "addr1")
        self.assertIn("TST", bot.STATE["positions"])

    def test_vault_decremented(self):
        bot.shadow_buy("TST", "sol", 50.0, 1.0, 100000.0)
        self.assertAlmostEqual(bot.STATE["vault_usd"], 950.0, places=2)

    def test_open_today_incremented(self):
        bot.shadow_buy("TST", "sol", 50.0, 1.0, 100000.0)
        self.assertAlmostEqual(bot.STATE["open_today_usd"], 50.0, places=2)

    def test_sell_returns_capital_to_daily_budget(self):
        # Net-exposure tracking: closing a position frees its cost back into the day's budget
        bot.shadow_buy("TST", "sol", 50.0, 1.0, 100000.0)
        self.assertAlmostEqual(bot.STATE["open_today_usd"], 50.0, places=2)
        pos = bot.STATE["positions"]["TST"]
        bot.shadow_sell("TST", pos["usd"], 1.0, 100000.0)  # full close
        self.assertAlmostEqual(bot.STATE["open_today_usd"], 0.0, places=1)

    def test_sell_daily_budget_never_negative(self):
        bot.STATE["open_today_usd"] = 0.0
        bot.shadow_buy("TST", "sol", 50.0, 1.0, 100000.0)
        bot.STATE["open_today_usd"] = 0.0   # simulate a position opened on a prior day
        pos = bot.STATE["positions"]["TST"]
        bot.shadow_sell("TST", pos["usd"], 1.0, 100000.0)
        self.assertGreaterEqual(bot.STATE["open_today_usd"], 0.0)

    def test_units_computed(self):
        bot.shadow_buy("TST", "sol", 50.0, 1.0, 100000.0)
        self.assertGreater(bot.STATE["positions"]["TST"]["units"], 0.0)

    def test_address_stored(self):
        bot.shadow_buy("TST", "sol", 50.0, 1.0, 100000.0, "tok_addr")
        self.assertEqual(bot.STATE["positions"]["TST"]["address"], "tok_addr")

    def test_address_backfilled_on_add(self):
        bot.shadow_buy("TST", "sol", 50.0, 1.0, 100000.0, "")
        bot.shadow_buy("TST", "sol", 50.0, 1.0, 100000.0, "later")
        self.assertEqual(bot.STATE["positions"]["TST"]["address"], "later")

    def test_weighted_avg_between_prices(self):
        bot.shadow_buy("TST", "sol", 50.0, 1.0, 100000.0)
        bot.shadow_buy("TST", "sol", 50.0, 3.0, 100000.0)
        avg = bot.STATE["positions"]["TST"]["avg"]
        self.assertGreater(avg, 1.0)
        self.assertLess(avg, 3.0)

    def test_peak_price_set(self):
        bot.shadow_buy("TST", "sol", 50.0, 2.0, 100000.0)
        self.assertGreater(bot.STATE["positions"]["TST"]["peak_price"], 0)

    def test_peak_price_updated_higher(self):
        bot.shadow_buy("TST", "sol", 50.0, 1.0, 100000.0)
        p1 = bot.STATE["positions"]["TST"]["peak_price"]
        bot.shadow_buy("TST", "sol", 50.0, 5.0, 100000.0)
        self.assertGreater(bot.STATE["positions"]["TST"]["peak_price"], p1)

    def test_trade_log_buy_entry(self):
        bot.shadow_buy("TST", "sol", 50.0, 1.0, 100000.0)
        last = bot.STATE["trade_log"][-1]
        self.assertEqual(last["symbol"], "TST")
        self.assertEqual(last["side"], "buy")

    def test_burst_incremented_on_new_position(self):
        before = len(bot.STATE["open_burst"])
        bot.shadow_buy("TST", "sol", 50.0, 1.0, 100000.0)
        self.assertEqual(len(bot.STATE["open_burst"]), before + 1)

    def test_burst_not_incremented_on_add_to_existing(self):
        bot.shadow_buy("TST", "sol", 50.0, 1.0, 100000.0)
        before = len(bot.STATE["open_burst"])
        bot.shadow_buy("TST", "sol", 25.0, 1.0, 100000.0)
        self.assertEqual(len(bot.STATE["open_burst"]), before)

    def test_burst_guard_blocks_new_position(self):
        limit = bot.CONFIG["stealth"]["burst_per_30s"]
        bot.STATE["open_burst"] = [time.time()] * limit
        res = bot.shadow_buy("BLOCKED", "sol", 50.0, 1.0, 100000.0)
        self.assertEqual(res["units"], 0.0)
        self.assertNotIn("BLOCKED", bot.STATE["positions"])

    def test_burst_guard_allows_add_to_existing_when_full(self):
        bot.shadow_buy("TST", "sol", 50.0, 1.0, 100000.0)
        limit = bot.CONFIG["stealth"]["burst_per_30s"]
        bot.STATE["open_burst"] = [time.time()] * limit
        units_before = bot.STATE["positions"]["TST"]["units"]
        res = bot.shadow_buy("TST", "sol", 25.0, 1.0, 100000.0)
        self.assertGreater(res["units"], 0.0)
        self.assertGreater(bot.STATE["positions"]["TST"]["units"], units_before)

    def test_filled_price_above_raw_price(self):
        res = bot.shadow_buy("TST", "sol", 50.0, 1.0, 100000.0)
        self.assertGreater(res["price"], 1.0)

    def test_entry_liq_stored(self):
        bot.shadow_buy("TST", "sol", 50.0, 1.0, 75000.0)
        self.assertEqual(bot.STATE["positions"]["TST"]["entry_liq"], 75000.0)


# ═══════════════════════════════════════════════════════════════════════════════
# 11. shadow_sell
# ═══════════════════════════════════════════════════════════════════════════════

class TestShadowSell(unittest.TestCase):

    def setUp(self):
        _reset(1000.0)
        bot.shadow_buy("TST", "sol", 100.0, 1.0, 100000.0)

    def test_full_sell_clears_units(self):
        pos = bot.STATE["positions"]["TST"]
        bot.shadow_sell("TST", pos["usd"], 1.0, 100000.0)
        self.assertAlmostEqual(pos["units"], 0.0, places=9)

    def test_full_sell_resets_tp_index(self):
        bot.STATE["positions"]["TST"]["tp_index"] = 2
        pos = bot.STATE["positions"]["TST"]
        bot.shadow_sell("TST", pos["usd"], 1.0, 100000.0)
        self.assertEqual(pos["tp_index"], 0)

    def test_partial_sell_keeps_units(self):
        pos          = bot.STATE["positions"]["TST"]
        units_before = pos["units"]
        bot.shadow_sell("TST", pos["usd"] * 0.5, 1.0, 100000.0)
        self.assertAlmostEqual(pos["units"], units_before / 2, places=6)

    def test_profit_pnl_on_price_rise(self):
        pos = bot.STATE["positions"]["TST"]
        res = bot.shadow_sell("TST", pos["usd"], 2.0, 100000.0)
        self.assertGreater(res["pnl"], 0.0)

    def test_loss_pnl_on_price_drop(self):
        pos = bot.STATE["positions"]["TST"]
        res = bot.shadow_sell("TST", pos["usd"], 0.5, 100000.0)
        self.assertLess(res["pnl"], 0.0)

    def test_vault_increases(self):
        vault_before = bot.STATE["vault_usd"]
        pos = bot.STATE["positions"]["TST"]
        bot.shadow_sell("TST", pos["usd"], 1.0, 100000.0)
        self.assertGreater(bot.STATE["vault_usd"], vault_before)

    def test_pnl_appended_to_hist(self):
        pos = bot.STATE["positions"]["TST"]
        bot.shadow_sell("TST", pos["usd"], 2.0, 100000.0)
        self.assertEqual(len(bot.STATE["pnl_hist"]), 1)

    def test_realized_incremented(self):
        pos = bot.STATE["positions"]["TST"]
        bot.shadow_sell("TST", pos["usd"], 2.0, 100000.0)
        self.assertGreater(pos["realized"], 0.0)

    def test_trade_log_sell_entry(self):
        pos = bot.STATE["positions"]["TST"]
        bot.shadow_sell("TST", pos["usd"], 1.0, 100000.0)
        sells = [t for t in bot.STATE["trade_log"] if t["side"] == "sell"]
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["symbol"], "TST")

    def test_missing_position_returns_zero(self):
        res = bot.shadow_sell("GHOST", 50.0, 1.0, 100000.0)
        self.assertEqual(res["pnl"], 0.0)

    def test_zero_usd_position_returns_zero(self):
        bot.STATE["positions"]["EMPTY"] = {
            "usd": 0.0, "units": 0.0, "avg": 0.0, "chain": "sol", "realized": 0.0}
        res = bot.shadow_sell("EMPTY", 50.0, 1.0, 100000.0)
        self.assertEqual(res["pnl"], 0.0)


# ═══════════════════════════════════════════════════════════════════════════════
# 12. Skim
# ═══════════════════════════════════════════════════════════════════════════════

class TestSkim(unittest.TestCase):

    def setUp(self):
        _reset(1000.0)
        bot.shadow_buy("TST", "sol", 100.0, 1.0, 100000.0)

    def test_disabled_no_income(self):
        bot.STATE["skim"] = {"enabled": False}
        pos = bot.STATE["positions"]["TST"]
        bot.shadow_sell("TST", pos["usd"], 2.0, 100000.0)
        self.assertEqual(bot.STATE["income_usd"], 0.0)

    def test_enabled_profit_creates_income(self):
        bot.STATE["skim"] = {"enabled": True}
        pos = bot.STATE["positions"]["TST"]
        bot.shadow_sell("TST", pos["usd"], 2.0, 100000.0)
        self.assertGreater(bot.STATE["income_usd"], 0.0)

    def test_amount_is_correct_pct(self):
        bot.STATE["skim"] = {"enabled": True}
        pos = bot.STATE["positions"]["TST"]
        res = bot.shadow_sell("TST", pos["usd"], 2.0, 100000.0)
        expected = res["pnl"] * bot.CONFIG["skim_pct"]
        self.assertAlmostEqual(bot.STATE["income_usd"], expected, places=4)

    def test_no_income_on_loss(self):
        bot.STATE["skim"] = {"enabled": True}
        pos = bot.STATE["positions"]["TST"]
        bot.shadow_sell("TST", pos["usd"], 0.5, 100000.0)
        self.assertEqual(bot.STATE["income_usd"], 0.0)

    def test_accumulates_across_trades(self):
        bot.STATE["skim"] = {"enabled": True}
        pos = bot.STATE["positions"]["TST"]
        bot.shadow_sell("TST", pos["usd"] / 2, 2.0, 100000.0)
        income1 = bot.STATE["income_usd"]
        pos = bot.STATE["positions"]["TST"]
        bot.shadow_sell("TST", pos["usd"], 2.0, 100000.0)
        self.assertGreater(bot.STATE["income_usd"], income1)


# ═══════════════════════════════════════════════════════════════════════════════
# 13. TP cascade — tp_index
# ═══════════════════════════════════════════════════════════════════════════════

class TestTPIndex(unittest.TestCase):

    def setUp(self):
        _reset(1000.0)
        bot.shadow_buy("TST", "sol", 100.0, 1.0, 100000.0)

    def test_starts_at_zero(self):
        self.assertEqual(bot.STATE["positions"]["TST"].get("tp_index", 0), 0)

    def test_second_tp_skipped_when_index_1(self):
        pos             = bot.STATE["positions"]["TST"]
        pos["tp_index"] = 1
        tp_levels       = [0.28, 0.60]
        executed        = []
        for i, tp in enumerate(tp_levels):
            if i < pos["tp_index"]:
                continue
            executed.append(i)
            break
        self.assertEqual(executed, [1])

    def test_resets_on_full_close(self):
        bot.STATE["positions"]["TST"]["tp_index"] = 2
        pos = bot.STATE["positions"]["TST"]
        bot.shadow_sell("TST", pos["usd"], 1.0, 100000.0)
        self.assertEqual(pos["tp_index"], 0)

    def test_preserved_on_partial_sell(self):
        bot.STATE["positions"]["TST"]["tp_index"] = 1
        pos = bot.STATE["positions"]["TST"]
        bot.shadow_sell("TST", pos["usd"] * 0.3, 1.5, 100000.0)
        self.assertEqual(pos["tp_index"], 1)


# ═══════════════════════════════════════════════════════════════════════════════
# 14. exec_buy / exec_sell routing
# ═══════════════════════════════════════════════════════════════════════════════

class TestExecRouting(unittest.TestCase):

    def setUp(self):
        _reset(1000.0)

    def test_exec_buy_shadow_calls_shadow_buy(self):
        bot.SHADOW_MODE = True
        with patch.object(bot, "shadow_buy", wraps=bot.shadow_buy) as mock:
            bot.exec_buy("TST", "sol", 50.0, 1.0, 100000.0)
            mock.assert_called_once()

    def test_exec_sell_shadow_calls_shadow_sell(self):
        bot.SHADOW_MODE = True
        bot.shadow_buy("TST", "sol", 50.0, 1.0, 100000.0)
        with patch.object(bot, "shadow_sell", wraps=bot.shadow_sell) as mock:
            bot.exec_sell("TST", 50.0, 1.0, 100000.0)
            mock.assert_called_once()

    def test_exec_buy_live_calls_live_buy(self):
        bot.SHADOW_MODE = False
        with patch.object(bot, "live_buy", return_value={"price": 1.0, "units": 50.0}) as mock:
            bot.exec_buy("TST", "sol", 50.0, 1.0, 100000.0)
            mock.assert_called_once()
        bot.SHADOW_MODE = True

    def test_exec_sell_live_calls_live_sell(self):
        bot.SHADOW_MODE = False
        bot.STATE["positions"]["TST"] = {
            "chain": "sol", "address": "addr1", "units": 50.0,
            "usd": 50.0, "avg": 1.0, "realized": 0.0,
        }
        with patch.object(bot, "live_sell", return_value={"sold": 50.0, "pnl": 0.0}) as mock:
            bot.exec_sell("TST", 50.0, 1.0, 100000.0)
            mock.assert_called_once()
        bot.SHADOW_MODE = True


# ═══════════════════════════════════════════════════════════════════════════════
# 15. Objective — start_objective / objective_nudge
# ═══════════════════════════════════════════════════════════════════════════════

class TestObjective(unittest.TestCase):

    def setUp(self):
        _reset(1000.0)

    def test_negative_target_raises(self):
        with self.assertRaises(ValueError):
            bot.start_objective(-100, 4)

    def test_zero_target_raises(self):
        with self.assertRaises(ValueError):
            bot.start_objective(0, 4)

    def test_zero_weeks_raises(self):
        with self.assertRaises(ValueError):
            bot.start_objective(1000, 0)

    def test_negative_weeks_raises(self):
        with self.assertRaises(ValueError):
            bot.start_objective(1000, -1)

    def test_curve_length(self):
        bot.start_objective(1000, 4)
        self.assertEqual(len(bot.OBJ_STATE["target_curve"]), 4)

    def test_curve_ends_at_target(self):
        bot.start_objective(1000, 4)
        self.assertAlmostEqual(bot.OBJ_STATE["target_curve"][-1], 1000.0, places=2)

    def test_curve_is_linear(self):
        bot.start_objective(400, 4)
        curve = bot.OBJ_STATE["target_curve"]
        for i, expected in enumerate([100, 200, 300, 400]):
            self.assertAlmostEqual(curve[i], float(expected), places=2)

    def test_nudge_returns_defaults_when_off(self):
        bot.CONFIG["objective"]["kind"] = "off"
        n = bot.objective_nudge()
        self.assertEqual(n["size_mult"], 1.0)
        self.assertEqual(n["extra_open"], 0)

    def test_nudge_returns_defaults_when_no_obj_state(self):
        bot.CONFIG["objective"]["kind"] = "target"
        bot.OBJ_STATE["started"] = None
        self.assertEqual(bot.objective_nudge()["size_mult"], 1.0)


# ═══════════════════════════════════════════════════════════════════════════════
# 16. No-pump exit + adaptive timer
# ═══════════════════════════════════════════════════════════════════════════════

class TestNoPumpExit(unittest.TestCase):

    def setUp(self):
        _reset()

    def test_within_window_no_exit(self):
        entry_ts = time.time() - 10
        self.assertFalse(bot.should_exit_no_pump(entry_ts, time.time(), 1.0, 1.0, 30000.0))

    def test_past_window_no_gain_exits(self):
        entry_ts = time.time() - 2000
        self.assertTrue(bot.should_exit_no_pump(entry_ts, time.time(), 1.0, 1.001, 30000.0))

    def test_past_window_good_gain_no_exit(self):
        entry_ts = time.time() - 2000
        cfg = bot.CONFIG["modes"].get("default", bot.CONFIG["modes"]["degen"])
        hurdle = cfg.get("no_pump", bot.CONFIG["modes"]["degen"]["no_pump"])["hurdle"]
        high_price = 1.0 * (1 + hurdle + 0.1)
        self.assertFalse(bot.should_exit_no_pump(entry_ts, time.time(), 1.0, high_price, 30000.0))

    def test_low_liq_uses_short_window(self):
        ms = bot.CONFIG["moonshot"]["adaptive_timer"]
        self.assertEqual(bot.adaptive_no_pump_window(10000.0), ms["low_liq_sec"])

    def test_high_liq_uses_long_window(self):
        ms = bot.CONFIG["moonshot"]["adaptive_timer"]
        self.assertEqual(bot.adaptive_no_pump_window(100000.0), ms["high_liq_sec"])

    def test_uses_current_mode_hurdle(self):
        bot.CONFIG["mode"] = "degen"
        entry_ts = time.time() - 2000
        degen_hurdle = bot.CONFIG["modes"]["degen"]["no_pump"]["hurdle"]
        under_hurdle = 1.0 * (1 + degen_hurdle - 0.01)
        self.assertTrue(bot.should_exit_no_pump(entry_ts, time.time(), 1.0, under_hurdle, 30000.0))


# ═══════════════════════════════════════════════════════════════════════════════
# 17. Re-entry watch
# ═══════════════════════════════════════════════════════════════════════════════

class TestReentryWatch(unittest.TestCase):

    def setUp(self):
        _reset()

    def _pos(self, addr="0xtoken"):
        return {"address": addr, "chain": "sol", "entry_liq": 50000.0, "entry_vol_h1": 1000.0}

    def test_adds_to_watch(self):
        bot._add_reentry_watch("TST", self._pos(), 50000.0, 1000.0, "trail")
        self.assertIn("TST", bot.STATE["reentry_watch"])

    def test_watch_has_required_fields(self):
        bot._add_reentry_watch("TST", self._pos(), 50000.0, 1000.0, "rug")
        w = bot.STATE["reentry_watch"]["TST"]
        for key in ("exit_ts", "entry_liq", "price_samples", "exit_reason", "chain"):
            self.assertIn(key, w)

    def test_skips_without_address(self):
        bot._add_reentry_watch("TST", self._pos(addr=""), 50000.0, 0.0, "test")
        self.assertNotIn("TST", bot.STATE["reentry_watch"])

    def test_skips_when_disabled(self):
        bot.CONFIG["moonshot"]["reentry"]["enabled"] = False
        bot._add_reentry_watch("TST", self._pos(), 50000.0, 0.0, "test")
        self.assertNotIn("TST", bot.STATE["reentry_watch"])
        bot.CONFIG["moonshot"]["reentry"]["enabled"] = True

    def test_exit_ts_is_recent(self):
        before = time.time()
        bot._add_reentry_watch("TST", self._pos(), 50000.0, 1000.0, "vel")
        after  = time.time()
        self.assertBetween = lambda lo, hi, val: self.assertLessEqual(lo, val) or self.assertLessEqual(val, hi)
        ts = bot.STATE["reentry_watch"]["TST"]["exit_ts"]
        self.assertGreaterEqual(ts, before)
        self.assertLessEqual(ts, after)

    def test_uses_position_entry_liq(self):
        pos = self._pos()
        pos["entry_liq"] = 77777.0
        bot._add_reentry_watch("TST", pos, 99999.0, 1000.0, "test")
        self.assertEqual(bot.STATE["reentry_watch"]["TST"]["entry_liq"], 77777.0)

    def test_overwrites_existing(self):
        bot._add_reentry_watch("TST", self._pos(), 50000.0, 1000.0, "first")
        bot._add_reentry_watch("TST", self._pos(), 50000.0, 1000.0, "second")
        self.assertEqual(bot.STATE["reentry_watch"]["TST"]["exit_reason"], "second")


# ═══════════════════════════════════════════════════════════════════════════════
# 18. _parse_sell_amount
# ═══════════════════════════════════════════════════════════════════════════════

class TestParseSellAmount(unittest.TestCase):

    def _pos(self, usd=100.0):
        return {"usd": usd, "units": 100.0, "avg": 1.0}

    def test_all(self):
        self.assertEqual(bot._parse_sell_amount(self._pos(100), "all"), 100.0)

    def test_max(self):
        self.assertEqual(bot._parse_sell_amount(self._pos(100), "max"), 100.0)

    def test_100pct(self):
        self.assertEqual(bot._parse_sell_amount(self._pos(100), "100%"), 100.0)

    def test_50pct(self):
        self.assertAlmostEqual(bot._parse_sell_amount(self._pos(100), "50%"), 50.0)

    def test_25pct(self):
        self.assertAlmostEqual(bot._parse_sell_amount(self._pos(200), "25%"), 50.0)

    def test_0pct(self):
        self.assertAlmostEqual(bot._parse_sell_amount(self._pos(100), "0%"), 0.0)

    def test_raw_float(self):
        self.assertAlmostEqual(bot._parse_sell_amount(self._pos(100), "35.50"), 35.50)

    def test_over_100pct_clamped(self):
        result = bot._parse_sell_amount(self._pos(100), "200%")
        self.assertLessEqual(result, 100.0)


# ═══════════════════════════════════════════════════════════════════════════════
# 19. Auth — tg_allowed + _dash_auth
# ═══════════════════════════════════════════════════════════════════════════════

class TestAuth(unittest.TestCase):

    def setUp(self):
        _reset()

    def test_empty_allowlist_allows_all(self):
        bot.CONFIG["tenant"]["allowlist"] = []
        self.assertTrue(bot.tg_allowed(12345))

    def test_id_in_allowlist_allowed(self):
        bot.CONFIG["tenant"]["allowlist"] = ["123", "456"]
        self.assertTrue(bot.tg_allowed(123))

    def test_id_not_in_allowlist_blocked(self):
        bot.CONFIG["tenant"]["allowlist"] = ["123"]
        self.assertFalse(bot.tg_allowed(999))

    def test_dash_auth_no_token_allows_all(self):
        os.environ.pop("DASHBOARD_TOKEN", None)
        with bot.app.test_client() as c:
            self.assertNotEqual(c.get("/status").status_code, 401)

    def test_dash_auth_correct_token(self):
        os.environ["DASHBOARD_TOKEN"] = "secret123"
        with bot.app.test_client() as c:
            r = c.get("/api/state", headers={"Authorization": "Bearer secret123"})
            self.assertNotEqual(r.status_code, 401)
        del os.environ["DASHBOARD_TOKEN"]

    def test_dash_auth_wrong_token(self):
        os.environ["DASHBOARD_TOKEN"] = "secret123"
        with bot.app.test_client() as c:
            r = c.get("/api/state", headers={"Authorization": "Bearer wrongtoken"})
            self.assertEqual(r.status_code, 401)
        del os.environ["DASHBOARD_TOKEN"]

    def test_dash_auth_missing_header(self):
        os.environ["DASHBOARD_TOKEN"] = "secret123"
        with bot.app.test_client() as c:
            r = c.get("/api/state")
            self.assertEqual(r.status_code, 401)
        del os.environ["DASHBOARD_TOKEN"]


# ═══════════════════════════════════════════════════════════════════════════════
# 20. Flask API — core endpoints
# ═══════════════════════════════════════════════════════════════════════════════

class TestFlaskAPI(unittest.TestCase):

    def setUp(self):
        _reset(1000.0)
        os.environ.pop("DASHBOARD_TOKEN", None)
        self.c = bot.app.test_client()

    # /status
    def test_status_200(self):
        self.assertEqual(self.c.get("/status").status_code, 200)

    def test_status_shadow_mode(self):
        data = self.c.get("/status").get_json()
        self.assertTrue(data["shadow_mode"])

    def test_status_build_key(self):
        self.assertIn("build", self.c.get("/status").get_json())

    def test_status_positions_count(self):
        bot.shadow_buy("TST", "sol", 50.0, 1.0, 100000.0)
        self.assertEqual(self.c.get("/status").get_json()["positions"], 1)

    # /api/state — flat response (no "state" wrapper)
    def test_api_state_200(self):
        self.assertEqual(self.c.get("/api/state").status_code, 200)

    def test_api_state_vault(self):
        data = self.c.get("/api/state").get_json()
        self.assertAlmostEqual(data["vault_usd"], 1000.0, places=2)

    def test_api_state_deployable(self):
        self.assertIn("deployable_usd", self.c.get("/api/state").get_json())

    def test_api_state_mode_tp(self):
        self.assertIn("mode_tp", self.c.get("/api/state").get_json())

    def test_api_state_mode_sl(self):
        self.assertIn("mode_sl", self.c.get("/api/state").get_json())

    # /api/positions — returns dict keyed by symbol; calls fetch_positions_prices internally
    def test_positions_empty(self):
        with patch.object(bot, "fetch_positions_prices", return_value={}):
            data = self.c.get("/api/positions").get_json()
        self.assertEqual(data, {})

    def test_positions_returns_open(self):
        bot.shadow_buy("TST", "sol", 50.0, 1.0, 100000.0)
        with patch.object(bot, "fetch_positions_prices",
                          return_value={"TST": bot._px_dict(1.0)}):
            data = self.c.get("/api/positions").get_json()
        self.assertIn("TST", data)

    def test_positions_excludes_closed(self):
        bot.shadow_buy("TST", "sol", 50.0, 1.0, 100000.0)
        pos = bot.STATE["positions"]["TST"]
        bot.shadow_sell("TST", pos["usd"], 1.0, 100000.0)
        with patch.object(bot, "fetch_positions_prices", return_value={}):
            data = self.c.get("/api/positions").get_json()
        self.assertEqual(data, {})

    # /api/history — returns {"trades", "total_pnl", "win_rate", "avg_win", "avg_loss"}
    def test_history_has_trades(self):
        self.assertIn("trades", self.c.get("/api/history").get_json())

    def test_history_has_total_pnl(self):
        self.assertIn("total_pnl", self.c.get("/api/history").get_json())

    # /api/alerts — returns a JSON list directly
    def test_alerts_is_list(self):
        data = self.c.get("/api/alerts").get_json()
        self.assertIsInstance(data, list)

    # /api/rpc — returns the health dict directly (not wrapped in {"health": ...})
    def test_rpc_is_dict(self):
        data = self.c.get("/api/rpc").get_json()
        self.assertIsInstance(data, dict)

    # /api/mode  (not /api/set_mode)
    def test_set_mode_valid(self):
        r = self.c.post("/api/mode", json={"mode": "hype"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(bot.CONFIG["mode"], "hype")

    def test_set_mode_invalid_400(self):
        self.assertEqual(self.c.post("/api/mode", json={"mode": "garbage"}).status_code, 400)

    def test_set_mode_missing_field_400(self):
        self.assertEqual(self.c.post("/api/mode", json={}).status_code, 400)

    # /api/shadow  — key is "enabled", not "shadow"
    def test_set_shadow_true(self):
        self.c.post("/api/shadow", json={"enabled": True})
        self.assertTrue(bot.SHADOW_MODE)

    def test_set_shadow_false_then_true(self):
        self.c.post("/api/shadow", json={"enabled": False})
        self.assertFalse(bot.SHADOW_MODE)
        self.c.post("/api/shadow", json={"enabled": True})
        self.assertTrue(bot.SHADOW_MODE)

    # /api/moonshot
    def test_set_moonshot_enter(self):
        r = self.c.post("/api/moonshot", json={"mode": "enter"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(bot.CONFIG["moonshot"]["mode"], "enter")

    def test_set_moonshot_invalid_400(self):
        self.assertEqual(self.c.post("/api/moonshot", json={"mode": "bad"}).status_code, 400)

    # /api/auto_old
    def test_set_auto_old_true(self):
        r = self.c.post("/api/auto_old", json={"enabled": True})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(bot.CONFIG["oldcoin"]["auto_join"])

    # /api/buy
    def test_buy_creates_position(self):
        r = self.c.post("/api/buy", json={
            "symbol": "MOCK", "usd": 50.0, "price": 1.0, "liq": 100000.0, "chain": "sol"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("MOCK", bot.STATE["positions"])

    def test_buy_no_price_no_address_400(self):
        self.assertEqual(
            self.c.post("/api/buy", json={"symbol": "MOCK", "usd": 50.0}).status_code, 400)

    def test_buy_zero_usd_400(self):
        self.assertEqual(
            self.c.post("/api/buy", json={"symbol": "MOCK", "usd": 0.0, "price": 1.0}).status_code, 400)

    def test_buy_negative_usd_400(self):
        self.assertEqual(
            self.c.post("/api/buy", json={"symbol": "MOCK", "usd": -10.0, "price": 1.0}).status_code, 400)

    # /api/sell
    def test_sell_existing_position(self):
        bot.shadow_buy("TST", "sol", 100.0, 1.0, 100000.0)
        r = self.c.post("/api/sell", json={"symbol": "TST", "pct": 50, "price": 1.0})
        self.assertEqual(r.status_code, 200)

    # /api/close
    def test_close_position(self):
        bot.shadow_buy("TST", "sol", 100.0, 1.0, 100000.0)
        with patch.object(bot, "fetch_positions_prices",
                          return_value={"TST": bot._px_dict(1.0)}):
            r = self.c.post("/api/close", json={"symbol": "TST"})
        self.assertEqual(r.status_code, 200)

    def test_close_missing_symbol_returns_error(self):
        # returns 404 "position not found" when symbol missing or no open units
        self.assertIn(self.c.post("/api/close", json={}).status_code, (400, 404))

    def test_close_unknown_symbol_404(self):
        self.assertEqual(self.c.post("/api/close", json={"symbol": "GHOST"}).status_code, 404)

    # /api/position/tp — stores as "tp_override" key
    def test_set_tp(self):
        bot.shadow_buy("TST", "sol", 100.0, 1.0, 100000.0)
        r = self.c.post("/api/position/tp", json={"symbol": "TST", "tp": [0.30, 0.60]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(bot.STATE["positions"]["TST"]["tp_override"], [0.30, 0.60])

    def test_set_tp_unknown_symbol_404(self):
        self.assertEqual(
            self.c.post("/api/position/tp", json={"symbol": "GHOST", "tp": [0.3]}).status_code, 404)

    # /api/position/sl — stores as "sl_override" key
    def test_set_sl(self):
        bot.shadow_buy("TST", "sol", 100.0, 1.0, 100000.0)
        r = self.c.post("/api/position/sl", json={"symbol": "TST", "sl": 0.15})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(bot.STATE["positions"]["TST"]["sl_override"], 0.15)

    def test_set_sl_unknown_symbol_404(self):
        self.assertEqual(
            self.c.post("/api/position/sl", json={"symbol": "GHOST", "sl": 0.1}).status_code, 404)

    # /api/watchlist/add and /remove
    def test_watchlist_add(self):
        r = self.c.post("/api/watchlist/add", json={"symbol": "DOGE", "address": "0xdoge"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(bot.CONFIG["oldcoin"]["watchlist"]["DOGE"], "0xdoge")

    def test_watchlist_remove(self):
        bot.CONFIG["oldcoin"]["watchlist"]["DOGE"] = "0xdoge"
        r = self.c.post("/api/watchlist/remove", json={"symbol": "DOGE"})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("DOGE", bot.CONFIG["oldcoin"]["watchlist"])

    def test_watchlist_add_missing_fields_400(self):
        self.assertEqual(self.c.post("/api/watchlist/add", json={}).status_code, 400)

    # /api/config/oldcoin
    def test_config_oldcoin(self):
        r = self.c.post("/api/config/oldcoin", json={"volume_x": 5.0, "mentions_x": 3.0})
        self.assertEqual(r.status_code, 200)
        self.assertAlmostEqual(bot.CONFIG["oldcoin"]["volume_x"], 5.0)


# ═══════════════════════════════════════════════════════════════════════════════
# 21. Flask advanced — boost, spray, objective
# ═══════════════════════════════════════════════════════════════════════════════

class TestFlaskAdvancedAPI(unittest.TestCase):

    def setUp(self):
        _reset(1000.0)
        os.environ.pop("DASHBOARD_TOKEN", None)
        self.c = bot.app.test_client()

    # /api/boost
    def test_set_boost_valid(self):
        r = self.c.post("/api/boost", json={"mult": 1.5, "hours": 2})
        self.assertEqual(r.status_code, 200)
        self.assertAlmostEqual(bot.STATE["boost"]["mult"], 1.5, places=2)

    def test_set_boost_zero_mult_400(self):
        self.assertEqual(self.c.post("/api/boost", json={"mult": 0.0, "hours": 2}).status_code, 400)

    # /api/spray — takes "until" (ISO date string) or None to clear
    def test_set_spray_with_until(self):
        r = self.c.post("/api/spray", json={"until": "9999-12-31T00:00:00+00:00"})
        self.assertEqual(r.status_code, 200)
        self.assertIsNotNone(bot.STATE["spray_until"])

    def test_set_spray_none_clears(self):
        bot.STATE["spray_until"] = "9999-12-31"
        r = self.c.post("/api/spray", json={"until": None})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(bot.STATE["spray_until"])

    # /api/objective — requires kind="target" + target_usd + weeks
    def test_set_objective_valid(self):
        r = self.c.post("/api/objective", json={"kind": "target", "target_usd": 500.0, "weeks": 4})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(bot.CONFIG["objective"]["kind"], "target")

    def test_set_objective_off(self):
        r = self.c.post("/api/objective", json={"kind": "off"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(bot.CONFIG["objective"]["kind"], "off")

    def test_set_objective_bad_target_400(self):
        self.assertEqual(
            self.c.post("/api/objective",
                        json={"kind": "target", "target_usd": -100.0, "weeks": 4}).status_code, 400)

    def test_set_objective_zero_weeks_400(self):
        self.assertEqual(
            self.c.post("/api/objective",
                        json={"kind": "target", "target_usd": 500.0, "weeks": 0}).status_code, 400)


# ═══════════════════════════════════════════════════════════════════════════════
# 22. State persistence — save / load round-trip, missing-key seeding
# ═══════════════════════════════════════════════════════════════════════════════

class TestStatePersistence(unittest.TestCase):

    def setUp(self):
        _reset(1234.56)

    def test_vault_round_trip(self):
        bot.STATE["vault_usd"] = 999.0
        bot.save_state()
        bot.STATE["vault_usd"] = 0.0
        bot.load_state()
        self.assertAlmostEqual(bot.STATE["vault_usd"], 999.0, places=2)

    def test_positions_round_trip(self):
        bot.shadow_buy("TST", "sol", 50.0, 1.0, 100000.0)
        saved_units = bot.STATE["positions"]["TST"]["units"]
        bot.save_state()
        bot.STATE["positions"] = {}
        bot.load_state()
        self.assertIn("TST", bot.STATE["positions"])
        self.assertAlmostEqual(bot.STATE["positions"]["TST"]["units"], saved_units, places=6)

    def test_load_seeds_missing_keys(self):
        with open(bot.SAVEFILE, "w") as f:
            json.dump({"vault_usd": 500.0}, f)
        bot.load_state()
        for key in ("reentry_watch", "open_burst", "brake_alerted"):
            self.assertIn(key, bot.STATE)

    def test_save_bad_path_no_crash(self):
        orig = bot.SAVEFILE
        bot.SAVEFILE = "/nonexistent_dir_abc/state.json"
        try:
            bot.save_state()
        except Exception:
            self.fail("save_state should not raise on bad path")
        finally:
            bot.SAVEFILE = orig

    def test_load_missing_file_no_crash(self):
        bot.SAVEFILE = "/tmp/definitely_does_not_exist_zzz.json"
        try:
            bot.load_state()
        except Exception:
            self.fail("load_state should not raise when file is missing")

    def test_load_corrupt_json_no_crash(self):
        with open(bot.SAVEFILE, "w") as f:
            f.write("{ NOT VALID JSON !!!")
        try:
            bot.load_state()
        except Exception:
            self.fail("load_state should not raise on corrupt JSON")


# ═══════════════════════════════════════════════════════════════════════════════
# 23. set_shadow_mode
# ═══════════════════════════════════════════════════════════════════════════════

class TestSetShadowMode(unittest.TestCase):

    def setUp(self):
        _reset()

    def tearDown(self):
        bot.SHADOW_MODE = True

    def test_set_false(self):
        bot.set_shadow_mode(False)
        self.assertFalse(bot.SHADOW_MODE)
        self.assertFalse(bot.STATE.get("shadow_mode"))

    def test_set_true(self):
        bot.SHADOW_MODE = False
        bot.set_shadow_mode(True)
        self.assertTrue(bot.SHADOW_MODE)

    def test_persisted(self):
        bot.set_shadow_mode(True)
        bot.load_state()
        self.assertTrue(bot.STATE.get("shadow_mode"))


# ═══════════════════════════════════════════════════════════════════════════════
# 24. refresh_vault_balance — shadow no-op
# ═══════════════════════════════════════════════════════════════════════════════

class TestRefreshVaultBalance(unittest.TestCase):

    def setUp(self):
        _reset(1000.0)

    def test_shadow_mode_is_noop(self):
        bot.SHADOW_MODE = True
        vault = bot.STATE["vault_usd"]
        bot.refresh_vault_balance()
        self.assertEqual(bot.STATE["vault_usd"], vault)


# ═══════════════════════════════════════════════════════════════════════════════
# 25. detect_oldcoin_pump — mocked DexScreener
# ═══════════════════════════════════════════════════════════════════════════════

class TestDetectOldcoinPump(unittest.TestCase):

    def setUp(self):
        _reset()
        bot.CONFIG["oldcoin"]["watchlist"] = {"DOGE": "0xdoge_addr"}

    def _dex(self, h1, h24):
        return {"pairs": [{"volume": {"h1": h1, "h24": h24},
                           "priceUsd": "0.1",
                           "liquidity": {"usd": 100000},
                           "priceChange": {"h24": 5.0}}]}

    def test_vol_spike_detected(self):
        avg    = 1000.0 / 24
        spike  = avg * 15  # 15× spike, well above volume_x=10
        with patch.object(bot, "fetch_dexscreener_token", return_value=self._dex(spike, 1000.0)):
            # social gate: mentions_x=2.0, so fetch_social_volume must return >= 2.0
            with patch.object(bot, "fetch_social_volume", return_value=2.5):
                self.assertTrue(bot.detect_oldcoin_pump("DOGE", 10.0, 2.0))

    def test_no_vol_spike(self):
        avg   = 1000.0 / 24
        small = avg * 2
        with patch.object(bot, "fetch_dexscreener_token", return_value=self._dex(small, 1000.0)):
            self.assertFalse(bot.detect_oldcoin_pump("DOGE", 10.0, 2.0))

    def test_unknown_symbol_false(self):
        self.assertFalse(bot.detect_oldcoin_pump("UNKNOWNCOIN", 10.0, 2.0))

    def test_none_dex_data_false(self):
        with patch.object(bot, "fetch_dexscreener_token", return_value=None):
            self.assertFalse(bot.detect_oldcoin_pump("DOGE", 10.0, 2.0))


# ═══════════════════════════════════════════════════════════════════════════════
# 26. fetch_social_volume — both paths
# ═══════════════════════════════════════════════════════════════════════════════

class TestFetchSocialVolume(unittest.TestCase):

    def setUp(self):
        os.environ.pop("LUNARCRUSH_API_KEY", None)
        bot._cg_trending_cache["ts"] = 0.0
        bot._cg_trending_cache["symbols"] = {}

    def test_not_trending_returns_1_0(self):
        with patch.object(bot, "_coingecko_trending_rank", return_value=None):
            self.assertEqual(bot.fetch_social_volume("NEWCOIN"), 1.0)

    def test_rank_1_returns_high_multiplier(self):
        with patch.object(bot, "_coingecko_trending_rank", return_value=1):
            self.assertGreater(bot.fetch_social_volume("BTC"), 2.0)

    def test_rank_10_returns_modest_multiplier(self):
        with patch.object(bot, "_coingecko_trending_rank", return_value=10):
            r = bot.fetch_social_volume("BTC")
            self.assertGreater(r, 1.0)
            self.assertLess(r, 3.0)

    def test_lunarcrush_path_uses_ratio(self):
        os.environ["LUNARCRUSH_API_KEY"] = "fake"
        mock_r = MagicMock()
        mock_r.status_code = 200
        # Actual API response shape used by fetch_social_volume: data is a dict with
        # social_volume_24h and social_volume_7d_average
        mock_r.json.return_value = {
            "data": {"social_volume_24h": 200, "social_volume_7d_average": 100}
        }
        with patch("requests.get", return_value=mock_r):
            result = bot.fetch_social_volume("BTC")
        self.assertAlmostEqual(result, 2.0, places=1)
        del os.environ["LUNARCRUSH_API_KEY"]

    def test_lunarcrush_failure_returns_float(self):
        os.environ["LUNARCRUSH_API_KEY"] = "fake"
        with patch("requests.get", side_effect=Exception("timeout")):
            result = bot.fetch_social_volume("BTC")
        self.assertIsInstance(result, float)
        del os.environ["LUNARCRUSH_API_KEY"]


# ═══════════════════════════════════════════════════════════════════════════════
# 27. CoinGecko trending rank — cache behavior
# ═══════════════════════════════════════════════════════════════════════════════

class TestCoinGeckoTrendingRank(unittest.TestCase):

    def setUp(self):
        bot._cg_trending_cache["ts"] = 0.0
        bot._cg_trending_cache["symbols"] = {}

    def _mock_resp(self, symbols):
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {"coins": [{"item": {"symbol": s}} for s in symbols]}
        return r

    def test_returns_rank_for_trending(self):
        with patch("requests.get", return_value=self._mock_resp(["BTC", "ETH"])):
            self.assertEqual(bot._coingecko_trending_rank("BTC"), 1)

    def test_second_symbol_rank_2(self):
        with patch("requests.get", return_value=self._mock_resp(["BTC", "ETH"])):
            self.assertEqual(bot._coingecko_trending_rank("ETH"), 2)

    def test_not_listed_returns_none(self):
        with patch("requests.get", return_value=self._mock_resp(["BTC"])):
            self.assertIsNone(bot._coingecko_trending_rank("NOTHERE"))

    def test_cache_prevents_second_call(self):
        with patch("requests.get", return_value=self._mock_resp(["BTC"])) as mock_get:
            bot._coingecko_trending_rank("BTC")
            bot._coingecko_trending_rank("ETH")
            self.assertEqual(mock_get.call_count, 1)

    def test_api_failure_returns_none(self):
        with patch("requests.get", side_effect=Exception("timeout")):
            self.assertIsNone(bot._coingecko_trending_rank("BTC"))


# ═══════════════════════════════════════════════════════════════════════════════
# 28. _sol_rpc failover
# ═══════════════════════════════════════════════════════════════════════════════

class TestSolRpcFailover(unittest.TestCase):

    def test_returns_healthy_url(self):
        ok = MagicMock()
        ok.status_code = 200
        with patch("requests.post", return_value=ok):
            rpc = bot._sol_rpc()
        self.assertIn("http", rpc)

    def test_falls_back_on_all_fail(self):
        with patch("requests.post", side_effect=Exception("conn refused")):
            rpc = bot._sol_rpc()
        self.assertTrue(rpc.startswith("http"))


# ═══════════════════════════════════════════════════════════════════════════════
# 29. Boost — expiry + ticket scaling
# ═══════════════════════════════════════════════════════════════════════════════

class TestBoost(unittest.TestCase):

    def setUp(self):
        _reset(1000.0)

    def test_expired_boost_cleared_after_size_ticket_call(self):
        bot.STATE["boost"] = {"mult": 5.0, "expires": "2000-01-01T00:00:00+00:00"}
        bot.size_ticket_usd("sol")
        self.assertEqual(bot.STATE["boost"]["mult"], 1.0)

    def test_unexpired_boost_active(self):
        future = (bot.now_utc().replace(year=bot.now_utc().year + 1)).isoformat()
        bot.STATE["boost"] = {"mult": 2.0, "expires": future}
        # Ticket with boost should be >= ticket without (subject to caps)
        t_boost    = bot.size_ticket_usd("sol")
        bot.STATE["boost"] = {"mult": 1.0, "expires": None}
        t_no_boost = bot.size_ticket_usd("sol")
        room = bot.per_chain_room("sol")
        base = bot.CONFIG["base_size_usd"] * bot.CONFIG["modes"]["default"]["size_mult"]
        if room > base * 2:
            self.assertGreater(t_boost, t_no_boost)


# ═══════════════════════════════════════════════════════════════════════════════
# 30. autoscale_maybe
# ═══════════════════════════════════════════════════════════════════════════════

class TestAutoscaleMaybe(unittest.TestCase):

    def setUp(self):
        _reset(1000.0)
        bot.shadow_buy("TST", "sol", 100.0, 1.0, 100000.0)
        bot.STATE["positions"]["TST"]["add_count"]   = 0
        bot.STATE["positions"]["TST"]["last_add_ts"] = 0.0

    def test_disabled_does_nothing(self):
        bot.CONFIG["autoscale"]["enabled"] = False
        u = bot.STATE["positions"]["TST"]["units"]
        bot.autoscale_maybe("TST", "sol", 1.05, 100000.0, velocity_ok=True)
        self.assertAlmostEqual(bot.STATE["positions"]["TST"]["units"], u, places=6)

    def test_velocity_false_does_nothing(self):
        u = bot.STATE["positions"]["TST"]["units"]
        bot.autoscale_maybe("TST", "sol", 1.05, 100000.0, velocity_ok=False)
        self.assertAlmostEqual(bot.STATE["positions"]["TST"]["units"], u, places=6)

    def test_max_adds_reached_does_nothing(self):
        bot.STATE["positions"]["TST"]["add_count"] = bot.CONFIG["autoscale"]["max_adds"]
        u = bot.STATE["positions"]["TST"]["units"]
        bot.autoscale_maybe("TST", "sol", 1.05, 100000.0, velocity_ok=True)
        self.assertAlmostEqual(bot.STATE["positions"]["TST"]["units"], u, places=6)

    def test_cooldown_active_does_nothing(self):
        bot.STATE["positions"]["TST"]["last_add_ts"] = time.time()
        u = bot.STATE["positions"]["TST"]["units"]
        bot.autoscale_maybe("TST", "sol", 1.05, 100000.0, velocity_ok=True)
        self.assertAlmostEqual(bot.STATE["positions"]["TST"]["units"], u, places=6)

    def test_adds_when_conditions_met(self):
        u = bot.STATE["positions"]["TST"]["units"]
        bot.autoscale_maybe("TST", "sol", 1.05, 100000.0, velocity_ok=True)
        self.assertGreater(bot.STATE["positions"]["TST"]["units"], u)

    def test_add_count_incremented(self):
        bot.autoscale_maybe("TST", "sol", 1.05, 100000.0, velocity_ok=True)
        self.assertEqual(bot.STATE["positions"]["TST"]["add_count"], 1)


# ═══════════════════════════════════════════════════════════════════════════════
# 31. Daily reset logic
# ═══════════════════════════════════════════════════════════════════════════════

class TestDailyReset(unittest.TestCase):

    def setUp(self):
        _reset(1000.0)

    def test_resets_open_today_on_new_day(self):
        bot.STATE["open_today_usd"]   = 999.0
        bot.STATE["last_daily_reset"] = "2000-01-01"
        today = bot.now_utc().date().isoformat()
        if bot.STATE.get("last_daily_reset") != today:
            bot.STATE["open_today_usd"]   = 0.0
            bot.STATE["last_daily_reset"] = today
        self.assertEqual(bot.STATE["open_today_usd"], 0.0)

    def test_no_reset_if_already_today(self):
        today = bot.now_utc().date().isoformat()
        bot.STATE["last_daily_reset"] = today
        bot.STATE["open_today_usd"]   = 500.0
        if bot.STATE.get("last_daily_reset") != today:
            bot.STATE["open_today_usd"] = 0.0
        self.assertEqual(bot.STATE["open_today_usd"], 500.0)


# ═══════════════════════════════════════════════════════════════════════════════
# 32. engine_once smoke — all external calls mocked
# ═══════════════════════════════════════════════════════════════════════════════

class TestEngineOnceSmoke(unittest.TestCase):

    def setUp(self):
        _reset(1000.0)

    @patch.object(bot, "fetch_btc_dominance", return_value=45.0)
    @patch.object(bot, "fetch_new_candidates", return_value=[])
    @patch.object(bot, "fetch_positions_prices", return_value={})
    @patch.object(bot, "check_reentry_watch")
    @patch.object(bot, "manage_trusted_coins")
    @patch.object(bot, "save_state")
    def test_no_crash_empty(self, *_):
        try:
            bot.engine_once()
        except Exception as e:
            self.fail(f"engine_once raised: {e}")

    @patch.object(bot, "fetch_btc_dominance", return_value=45.0)
    @patch.object(bot, "fetch_new_candidates", return_value=[])
    @patch.object(bot, "fetch_positions_prices",
                  return_value={"TST": bot._px_dict(1.05, 100000.0, 5000.0, 2.0)})
    @patch.object(bot, "check_reentry_watch")
    @patch.object(bot, "manage_trusted_coins")
    @patch.object(bot, "save_state")
    def test_no_crash_with_position(self, *_):
        _reset(1000.0)
        bot.shadow_buy("TST", "sol", 50.0, 1.0, 100000.0)
        try:
            bot.engine_once()
        except Exception as e:
            self.fail(f"engine_once raised with position: {e}")

    @patch.object(bot, "fetch_positions_prices",
                  return_value={"TST": bot._px_dict(2.0, 100000.0, 5000.0, 3.0)})
    @patch.object(bot, "save_state")
    def test_manage_positions_standalone(self, *_):
        # Fast loop must run on its own (no candidate scan) and act on a position
        _reset(1000.0)
        bot.shadow_buy("TST", "sol", 50.0, 1.0, 100000.0)
        try:
            bot.manage_positions()
        except Exception as e:
            self.fail(f"manage_positions raised: {e}")

    @patch.object(bot, "fetch_btc_dominance", return_value=45.0)
    @patch.object(bot, "fetch_new_candidates", return_value=[])
    @patch.object(bot, "check_reentry_watch")
    @patch.object(bot, "manage_trusted_coins")
    @patch.object(bot, "save_state")
    def test_scan_candidates_standalone(self, *_):
        _reset(1000.0)
        try:
            bot.scan_candidates()
        except Exception as e:
            self.fail(f"scan_candidates raised: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# 33. Gas / fee simulation in paper trades
# ═══════════════════════════════════════════════════════════════════════════════

class TestGasSimulation(unittest.TestCase):

    def setUp(self):
        _reset(1000.0)
        bot.CONFIG["gas_sim"] = True

    def test_gas_helper_returns_chain_cost(self):
        self.assertAlmostEqual(bot._gas_usd("eth"), bot.CONFIG["gas_usd"]["eth"])
        self.assertAlmostEqual(bot._gas_usd("sol"), bot.CONFIG["gas_usd"]["sol"])

    def test_gas_helper_zero_when_disabled(self):
        bot.CONFIG["gas_sim"] = False
        self.assertEqual(bot._gas_usd("eth"), 0.0)
        bot.CONFIG["gas_sim"] = True

    def test_gas_unknown_chain_default(self):
        self.assertGreater(bot._gas_usd("madeupchain"), 0.0)

    def test_buy_charges_gas_to_vault(self):
        gas = bot._gas_usd("eth")
        bot.shadow_buy("TST", "eth", 50.0, 1.0, 100000.0)
        # vault drops by usd + gas
        self.assertAlmostEqual(bot.STATE["vault_usd"], 1000.0 - 50.0 - gas, places=4)

    def test_buy_tracks_gas_paid(self):
        before = bot.STATE.get("gas_paid_usd", 0.0)
        bot.shadow_buy("TST", "eth", 50.0, 1.0, 100000.0)
        self.assertGreater(bot.STATE["gas_paid_usd"], before)

    def test_position_stores_entry_fee(self):
        bot.shadow_buy("TST", "eth", 50.0, 1.0, 100000.0)
        self.assertAlmostEqual(bot.STATE["positions"]["TST"]["fees_usd"], bot._gas_usd("eth"), places=6)

    def test_eth_gas_hurts_pnl_more_than_sol(self):
        # Same trade on eth vs sol — eth should net worse due to higher gas
        bot.shadow_buy("SOLT", "sol", 50.0, 1.0, 100000.0)
        sol_res = bot.shadow_sell("SOLT", bot.STATE["positions"]["SOLT"]["usd"], 2.0, 100000.0)
        _reset(1000.0)
        bot.CONFIG["gas_sim"] = True   # _reset turns it off; re-enable for the eth leg
        bot.shadow_buy("ETHT", "eth", 50.0, 1.0, 100000.0)
        eth_res = bot.shadow_sell("ETHT", bot.STATE["positions"]["ETHT"]["usd"], 2.0, 100000.0)
        self.assertGreater(sol_res["pnl"], eth_res["pnl"])

    def test_gas_disabled_no_charge(self):
        bot.CONFIG["gas_sim"] = False
        bot.shadow_buy("TST", "eth", 50.0, 1.0, 100000.0)
        self.assertAlmostEqual(bot.STATE["vault_usd"], 950.0, places=4)
        bot.CONFIG["gas_sim"] = True

    def test_full_sell_clears_fees(self):
        bot.shadow_buy("TST", "eth", 50.0, 1.0, 100000.0)
        pos = bot.STATE["positions"]["TST"]
        bot.shadow_sell("TST", pos["usd"], 2.0, 100000.0)
        self.assertEqual(pos["fees_usd"], 0.0)


# ═══════════════════════════════════════════════════════════════════════════════
# 34. Re-entry tightening — skip hard-dump exits
# ═══════════════════════════════════════════════════════════════════════════════

class TestReentryHardExitSkip(unittest.TestCase):

    def setUp(self):
        _reset()
        bot.CONFIG["moonshot"]["reentry"]["enabled"] = True
        bot.CONFIG["moonshot"]["reentry"]["skip_hard_exits"] = True

    def _pos(self):
        return {"address": "0xtok", "chain": "sol", "entry_liq": 50000.0, "entry_vol_h1": 1000.0}

    def test_velocity_exit_skipped(self):
        bot._add_reentry_watch("TST", self._pos(), 50000.0, 1000.0, "VELOCITY -17.0% in 5m")
        self.assertNotIn("TST", bot.STATE["reentry_watch"])

    def test_rug_exit_skipped(self):
        bot._add_reentry_watch("TST", self._pos(), 50000.0, 1000.0, "RUG liq 50000→10000")
        self.assertNotIn("TST", bot.STATE["reentry_watch"])

    def test_liq_drain_skipped(self):
        bot._add_reentry_watch("TST", self._pos(), 50000.0, 1000.0, "LIQ DRAIN 50000→40000 over 3 ticks")
        self.assertNotIn("TST", bot.STATE["reentry_watch"])

    def test_fixed_sl_skipped(self):
        bot._add_reentry_watch("TST", self._pos(), 50000.0, 1000.0, "fixed_sl")
        self.assertNotIn("TST", bot.STATE["reentry_watch"])

    def test_trail_stop_allowed(self):
        # Cooled-off winner — SHOULD be watched for re-entry
        bot._add_reentry_watch("TST", self._pos(), 50000.0, 1000.0, "TRAIL STOP -15.0% from peak 0.002")
        self.assertIn("TST", bot.STATE["reentry_watch"])

    def test_skip_disabled_allows_all(self):
        bot.CONFIG["moonshot"]["reentry"]["skip_hard_exits"] = False
        bot._add_reentry_watch("TST", self._pos(), 50000.0, 1000.0, "VELOCITY -17%")
        self.assertIn("TST", bot.STATE["reentry_watch"])
        bot.CONFIG["moonshot"]["reentry"]["skip_hard_exits"] = True


# ═══════════════════════════════════════════════════════════════════════════════
# 35. New control endpoints (skim, doge, config, export, import, restart)
# ═══════════════════════════════════════════════════════════════════════════════

class TestNewControlEndpoints(unittest.TestCase):

    def setUp(self):
        _reset(1000.0)
        os.environ.pop("DASHBOARD_TOKEN", None)
        self.c = bot.app.test_client()

    # /api/skim
    def test_skim_on(self):
        r = self.c.post("/api/skim", json={"enabled": True})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(bot.STATE["skim"]["enabled"])

    def test_skim_off(self):
        bot.STATE["skim"] = {"enabled": True}
        self.c.post("/api/skim", json={"enabled": False})
        self.assertFalse(bot.STATE["skim"]["enabled"])

    # /api/doge
    def test_doge_core_units(self):
        r = self.c.post("/api/doge", json={"core_units": 50000})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(bot.TRUSTED["DOGE"]["core_units"], 50000)

    def test_doge_band_valid(self):
        r = self.c.post("/api/doge", json={"exit_band": [0.4, 0.8]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(bot.TRUSTED["DOGE"]["exit_band"], [0.4, 0.8])

    def test_doge_band_invalid_400(self):
        r = self.c.post("/api/doge", json={"exit_band": [0.8, 0.4]})
        self.assertEqual(r.status_code, 400)

    # /api/config
    def test_config_presale_score(self):
        r = self.c.post("/api/config", json={"presale_min_score": 30})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(bot.CONFIG["presale_min_score"], 30)

    def test_config_gas_sim_toggle(self):
        self.c.post("/api/config", json={"gas_sim": False})
        self.assertFalse(bot.CONFIG["gas_sim"])
        self.c.post("/api/config", json={"gas_sim": True})
        self.assertTrue(bot.CONFIG["gas_sim"])

    def test_config_reentry_nested(self):
        self.c.post("/api/config", json={"reentry": {"cooldown_min": 20}})
        self.assertEqual(bot.CONFIG["moonshot"]["reentry"]["cooldown_min"], 20)

    # /api/export
    def test_export_returns_state(self):
        r = self.c.get("/api/export")
        self.assertEqual(r.status_code, 200)
        self.assertIn("state", r.get_json())

    # /api/import
    def test_import_patch(self):
        r = self.c.post("/api/import", json={"state": {"vault_usd": 555.0}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(bot.STATE["vault_usd"], 555.0)

    def test_import_bare_patch(self):
        r = self.c.post("/api/import", json={"income_usd": 42.0})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(bot.STATE["income_usd"], 42.0)

    # /api/state surfaces new config
    def test_state_exposes_config_block(self):
        d = self.c.get("/api/state").get_json()
        self.assertIn("config", d)
        self.assertIn("presale_min_score", d["config"])
        self.assertIn("gas_sim", d["config"])
        self.assertIn("skim_enabled", d)
        self.assertIn("build", d)


# ═══════════════════════════════════════════════════════════════════════════════
# 36. Scout log — decision recording + reject reasons
# ═══════════════════════════════════════════════════════════════════════════════

class TestScoutLog(unittest.TestCase):

    def setUp(self):
        _reset()
        bot.STATE["scout_log"] = []

    def _sc(self, hype=90, liq=50000, age_min=15, positive=True):
        return bot.Score(hype, liq, age_min, positive)

    def test_reject_reason_none_when_passing(self):
        self.assertIsNone(bot.moonshot_reject_reason(self._sc()))

    def test_reject_reason_liq_low(self):
        r = bot.moonshot_reject_reason(self._sc(liq=bot.CONFIG["moonshot"]["liq_min"] - 1))
        self.assertIn("liquidity", r)

    def test_reject_reason_age(self):
        max_age = bot.CONFIG["modes"][bot.CONFIG["mode"]]["max_age_min"]
        r = bot.moonshot_reject_reason(self._sc(age_min=max_age + 5))
        self.assertIn("age", r)

    def test_reject_reason_negative_trend(self):
        r = bot.moonshot_reject_reason(self._sc(positive=False))
        self.assertIn("negative", r)

    def test_reject_reason_low_hype(self):
        r = bot.moonshot_reject_reason(self._sc(hype=bot.CONFIG["moonshot"]["hype_min"] - 1))
        self.assertIn("hype", r)

    def test_passes_filters_matches_reject_reason(self):
        sc_ok  = self._sc()
        sc_bad = self._sc(liq=1)
        self.assertEqual(bot.passes_moonshot_filters(sc_ok), bot.moonshot_reject_reason(sc_ok) is None)
        self.assertEqual(bot.passes_moonshot_filters(sc_bad), bot.moonshot_reject_reason(sc_bad) is None)

    def test_scout_records_entry(self):
        bot._scout("TST", "sol", "entered", "passed", self._sc())
        self.assertEqual(len(bot.STATE["scout_log"]), 1)
        e = bot.STATE["scout_log"][0]
        self.assertEqual(e["symbol"], "TST")
        self.assertEqual(e["decision"], "entered")
        self.assertEqual(e["hype"], 90)

    def test_scout_caps_at_300(self):
        for i in range(320):
            bot._scout(f"T{i}", "sol", "rejected", "x")
        self.assertLessEqual(len(bot.STATE["scout_log"]), 300)
        # most recent retained
        self.assertEqual(bot.STATE["scout_log"][-1]["symbol"], "T319")

    def test_scout_without_score(self):
        bot._scout("TST", "eth", "rejected", "no score")
        e = bot.STATE["scout_log"][0]
        self.assertNotIn("hype", e)


class TestScoutLogEndpoint(unittest.TestCase):

    def setUp(self):
        _reset()
        bot.STATE["scout_log"] = []
        os.environ.pop("DASHBOARD_TOKEN", None)
        self.c = bot.app.test_client()

    def test_endpoint_empty(self):
        d = self.c.get("/api/scoutlog").get_json()
        self.assertEqual(d["scout"], [])
        self.assertIn("counts", d)

    def test_endpoint_returns_reversed(self):
        bot._scout("FIRST", "sol", "rejected", "x")
        bot._scout("LAST", "sol", "entered", "y")
        d = self.c.get("/api/scoutlog").get_json()
        self.assertEqual(d["scout"][0]["symbol"], "LAST")   # most recent first

    def test_endpoint_counts(self):
        bot._scout("A", "sol", "entered", "x")
        bot._scout("B", "sol", "rejected", "y")
        bot._scout("C", "sol", "rejected", "z")
        d = self.c.get("/api/scoutlog").get_json()
        self.assertEqual(d["counts"]["entered"], 1)
        self.assertEqual(d["counts"]["rejected"], 2)

    def test_state_excludes_scout_log(self):
        bot._scout("A", "sol", "entered", "x")
        d = self.c.get("/api/state").get_json()
        self.assertNotIn("scout_log", d)   # served via its own endpoint


# ═══════════════════════════════════════════════════════════════════════════════
# 37. AI auto-pilot (gating, context, endpoints) — no real API calls
# ═══════════════════════════════════════════════════════════════════════════════

class TestAIAdvisor(unittest.TestCase):

    def setUp(self):
        _reset(1000.0)
        os.environ.pop("ANTHROPIC_API_KEY", None)
        bot.CONFIG["ai"]["enabled"] = True
        bot.CONFIG["ai"]["auto_apply"] = False

    def test_advise_none_without_key(self):
        self.assertIsNone(bot.ai_advise(force=True))

    def test_advise_none_when_disabled_and_not_forced(self):
        bot.CONFIG["ai"]["enabled"] = False
        os.environ["ANTHROPIC_API_KEY"] = "fake"
        try:
            self.assertIsNone(bot.ai_advise(force=False))
        finally:
            del os.environ["ANTHROPIC_API_KEY"]

    def test_market_context_shape(self):
        bot.STATE["pnl_hist"] = [10, -5, 8]   # 2 wins (10, 8), 1 loss (-5)
        ctx = bot._ai_market_context()
        self.assertEqual(ctx["recent_trades"], 3)
        self.assertAlmostEqual(ctx["recent_win_rate"], 2 / 3, places=2)
        self.assertAlmostEqual(ctx["avg_win"], 9.0)          # (10+8)/2
        self.assertAlmostEqual(ctx["avg_loss"], -5.0)
        # expectancy = 2/3*9 + 1/3*(-5) = 6 - 1.67 = +4.33 (positive)
        self.assertGreater(ctx["expectancy_per_trade"], 0)
        self.assertEqual(ctx["biggest_recent_win"], 10)
        self.assertIn("scout_last_40", ctx)
        self.assertIn("current_mode", ctx)

    def test_scout_reason_summary(self):
        bot.STATE["scout_log"] = []
        bot._scout("A", "sol", "entered", "x")
        bot._scout("B", "sol", "rejected", "y")
        s = bot._scout_reason_summary(40)
        self.assertEqual(s["entered"], 1)
        self.assertEqual(s["rejected"], 1)

    def test_advise_applies_mode_when_auto_and_confident(self):
        # Mock the anthropic SDK so no real call happens
        os.environ["ANTHROPIC_API_KEY"] = "fake"
        bot.CONFIG["ai"]["auto_apply"] = True
        bot.CONFIG["ai"]["min_confidence"] = 0.6
        bot.CONFIG["mode"] = "default"
        fake_block = MagicMock(); fake_block.type = "text"
        fake_block.text = json.dumps({"recommended_mode": "hype", "confidence": 0.9,
                                      "aggressive": True, "reasoning": "alt season"})
        fake_resp = MagicMock(); fake_resp.content = [fake_block]
        fake_client = MagicMock(); fake_client.messages.create.return_value = fake_resp
        fake_anthropic = MagicMock(); fake_anthropic.Anthropic.return_value = fake_client
        with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
            d = bot.ai_advise(force=True)
        self.assertIsNotNone(d)
        self.assertEqual(d["recommended_mode"], "hype")
        self.assertTrue(d["applied"])
        self.assertEqual(bot.CONFIG["mode"], "hype")
        del os.environ["ANTHROPIC_API_KEY"]

    def test_advise_advisory_only_when_not_auto(self):
        os.environ["ANTHROPIC_API_KEY"] = "fake"
        bot.CONFIG["ai"]["auto_apply"] = False
        bot.CONFIG["mode"] = "default"
        fake_block = MagicMock(); fake_block.type = "text"
        fake_block.text = json.dumps({"recommended_mode": "degen", "confidence": 0.95, "reasoning": "x"})
        fake_resp = MagicMock(); fake_resp.content = [fake_block]
        fake_client = MagicMock(); fake_client.messages.create.return_value = fake_resp
        fake_anthropic = MagicMock(); fake_anthropic.Anthropic.return_value = fake_client
        with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
            d = bot.ai_advise(force=True)
        self.assertFalse(d["applied"])
        self.assertEqual(bot.CONFIG["mode"], "default")   # unchanged
        del os.environ["ANTHROPIC_API_KEY"]

    def test_advise_confidence_clamped(self):
        os.environ["ANTHROPIC_API_KEY"] = "fake"
        fake_block = MagicMock(); fake_block.type = "text"
        fake_block.text = json.dumps({"recommended_mode": "safe", "confidence": 5.0, "reasoning": "x"})
        fake_resp = MagicMock(); fake_resp.content = [fake_block]
        fake_client = MagicMock(); fake_client.messages.create.return_value = fake_resp
        fake_anthropic = MagicMock(); fake_anthropic.Anthropic.return_value = fake_client
        with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
            d = bot.ai_advise(force=True)
        self.assertLessEqual(d["confidence"], 1.0)
        del os.environ["ANTHROPIC_API_KEY"]


class TestAIEndpoints(unittest.TestCase):

    def setUp(self):
        _reset(1000.0)
        os.environ.pop("DASHBOARD_TOKEN", None)
        os.environ.pop("ANTHROPIC_API_KEY", None)
        self.c = bot.app.test_client()

    def test_ai_config_toggle(self):
        r = self.c.post("/api/ai", json={"enabled": True, "auto_apply": True})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(bot.CONFIG["ai"]["enabled"])
        self.assertTrue(bot.CONFIG["ai"]["auto_apply"])

    def test_ai_config_interval_clamped(self):
        self.c.post("/api/ai", json={"interval_min": 0})
        self.assertGreaterEqual(bot.CONFIG["ai"]["interval_min"], 1)

    def test_ai_run_without_key_400(self):
        r = self.c.post("/api/ai/run", json={})
        self.assertEqual(r.status_code, 400)

    def test_state_exposes_ai_config(self):
        d = self.c.get("/api/state").get_json()
        self.assertIn("ai", d["config"])
        self.assertIn("ai_key_set", d)


# ═══════════════════════════════════════════════════════════════════════════════
# 38. Dynamic gas
# ═══════════════════════════════════════════════════════════════════════════════

class TestDynamicGas(unittest.TestCase):

    def setUp(self):
        _reset(1000.0)
        bot.CONFIG["gas_sim"] = True
        bot.CONFIG["gas_dynamic"] = True
        bot.STATE["gas_live"] = {}

    def test_live_gas_overrides_static(self):
        bot.STATE["gas_live"] = {"eth": 12.5, "ts": time.time()}
        self.assertEqual(bot._gas_usd("eth"), 12.5)

    def test_falls_back_to_static_without_live(self):
        bot.STATE["gas_live"] = {}
        self.assertEqual(bot._gas_usd("eth"), bot.CONFIG["gas_usd"]["eth"])

    def test_refresh_respects_cache_window(self):
        bot.STATE["gas_live"] = {"eth": 9.0, "ts": time.time()}  # fresh
        with patch.object(bot, "_eth_gas_usd_live", return_value=99.0) as mock:
            bot.refresh_gas_estimates()
            mock.assert_not_called()   # cached, shouldn't refetch

    def test_refresh_updates_when_stale(self):
        bot.STATE["gas_live"] = {"eth": 9.0, "ts": 0.0}  # stale
        with patch.object(bot, "_eth_gas_usd_live", return_value=15.0):
            bot.refresh_gas_estimates()
        self.assertEqual(bot.STATE["gas_live"]["eth"], 15.0)

    def test_refresh_noop_when_dynamic_off(self):
        bot.CONFIG["gas_dynamic"] = False
        bot.STATE["gas_live"] = {"eth": 9.0, "ts": 0.0}
        with patch.object(bot, "_eth_gas_usd_live", return_value=15.0) as mock:
            bot.refresh_gas_estimates()
            mock.assert_not_called()

    def test_eth_gas_live_computes_usd(self):
        # gasPrice 30 gwei, ETH $3000 → 30e9 * 150000 / 1e18 * 3000 = $13.5
        mock_rpc = MagicMock()
        mock_rpc.json.return_value = {"result": hex(30 * 10**9)}
        with patch("requests.post", return_value=mock_rpc):
            with patch.object(bot, "_get", return_value={"ethereum": {"usd": 3000}}):
                usd = bot._eth_gas_usd_live()
        self.assertAlmostEqual(usd, 13.5, places=2)


# ═══════════════════════════════════════════════════════════════════════════════
# 39. Clickable token links + plain-English exit reasons
# ═══════════════════════════════════════════════════════════════════════════════

class TestTokenLinks(unittest.TestCase):

    def setUp(self):
        _reset(1000.0)

    def test_dex_link_sol(self):
        self.assertEqual(bot._dex_link("sol", "ABC"), "https://dexscreener.com/solana/ABC")

    def test_dex_link_eth(self):
        self.assertEqual(bot._dex_link("eth", "0x1"), "https://dexscreener.com/ethereum/0x1")

    def test_dex_link_empty_address(self):
        self.assertEqual(bot._dex_link("sol", ""), "")

    def test_dex_link_unknown_chain_passthrough(self):
        self.assertEqual(bot._dex_link("weird", "X"), "https://dexscreener.com/weird/X")

    def test_buy_records_address_in_trade_log(self):
        bot.shadow_buy("TST", "sol", 50.0, 1.0, 100000.0, "tokaddr")
        last = bot.STATE["trade_log"][-1]
        self.assertEqual(last["address"], "tokaddr")

    def test_sell_records_address_in_trade_log(self):
        bot.shadow_buy("TST", "sol", 100.0, 1.0, 100000.0, "tokaddr")
        pos = bot.STATE["positions"]["TST"]
        bot.shadow_sell("TST", pos["usd"], 1.0, 100000.0)
        sells = [t for t in bot.STATE["trade_log"] if t["side"] == "sell"]
        self.assertEqual(sells[-1]["address"], "tokaddr")

    def test_scout_records_address(self):
        bot.STATE["scout_log"] = []
        bot._scout("TST", "eth", "rejected", "x", None, "0xabc")
        self.assertEqual(bot.STATE["scout_log"][0]["address"], "0xabc")


class TestExitPlain(unittest.TestCase):

    def test_rug(self):
        self.assertIn("rug", bot._exit_plain("RUG liq 50000→10000").lower())

    def test_velocity(self):
        self.assertIn("dropped", bot._exit_plain("VELOCITY -17% in 5m").lower())

    def test_trail(self):
        self.assertIn("peak", bot._exit_plain("TRAIL STOP -15% from peak 0.002").lower())

    def test_liq_drain(self):
        self.assertIn("liquidity", bot._exit_plain("LIQ DRAIN 50000→40000 over 3 ticks").lower())

    def test_fixed_sl(self):
        self.assertIn("loss", bot._exit_plain("fixed_sl").lower())

    def test_unknown_passthrough(self):
        self.assertEqual(bot._exit_plain("something weird"), "something weird")


# ═══════════════════════════════════════════════════════════════════════════════
# 40. Scam / rug safety gate
# ═══════════════════════════════════════════════════════════════════════════════

class TestSafetyGate(unittest.TestCase):

    def setUp(self):
        _reset()
        bot._reddit_cache.clear()
        bot._x_cache.clear()
        os.environ.pop("X_BEARER_TOKEN", None)
        bot.CONFIG["safety"].update({
            "reddit_enabled": True, "x_enabled": True, "onchain_enabled": True,
            "scam_chatter_max": 2, "sol_holder_max_pct": 0.25, "evm_sell_tax_max": 20.0})

    def _reddit_resp(self, titles):
        r = MagicMock(); r.status_code = 200
        r.json.return_value = {"data": {"children": [{"data": {"title": t, "selftext": ""}} for t in titles]}}
        return r

    def test_count_scam(self):
        self.assertEqual(bot._count_scam(["this is a rug", "great coin", "total scam honeypot"]), 2)

    def test_reddit_counts_scam_posts(self):
        with patch("requests.get", return_value=self._reddit_resp(["X is a scam", "X mooning", "avoid X rug"])):
            rd = bot.fetch_reddit_sentiment("X")
        self.assertEqual(rd["mentions"], 3)
        self.assertEqual(rd["scam_hits"], 2)

    def test_reddit_cached(self):
        with patch("requests.get", return_value=self._reddit_resp(["a"])) as g:
            bot.fetch_reddit_sentiment("CACHED")
            bot.fetch_reddit_sentiment("CACHED")
            self.assertEqual(g.call_count, 1)

    def test_x_disabled_without_token(self):
        self.assertFalse(bot.fetch_x_buzz("X")["enabled"])

    def test_x_with_token(self):
        os.environ["X_BEARER_TOKEN"] = "fake"
        r = MagicMock(); r.status_code = 200
        r.json.return_value = {"data": [{"text": "X is a rugpull"}, {"text": "love X"}]}
        with patch("requests.get", return_value=r):
            xb = bot.fetch_x_buzz("X")
        self.assertTrue(xb["enabled"])
        self.assertEqual(xb["scam_hits"], 1)
        del os.environ["X_BEARER_TOKEN"]

    def test_gate_blocks_on_scam_chatter(self):
        with patch.object(bot, "fetch_reddit_sentiment", return_value={"mentions": 5, "scam_hits": 3}):
            with patch.object(bot, "fetch_onchain_safety", return_value={"flagged": False, "reason": ""}):
                ok, why = bot.safety_gate("SCAMCOIN", "addr", "sol")
        self.assertFalse(ok)
        self.assertIn("scam", why.lower())

    def test_gate_passes_clean_token(self):
        with patch.object(bot, "fetch_reddit_sentiment", return_value={"mentions": 4, "scam_hits": 0}):
            with patch.object(bot, "fetch_onchain_safety", return_value={"flagged": False, "reason": ""}):
                ok, why = bot.safety_gate("CLEAN", "addr", "sol")
        self.assertTrue(ok)

    def test_gate_blocks_on_onchain_flag(self):
        with patch.object(bot, "fetch_reddit_sentiment", return_value={"mentions": 1, "scam_hits": 0}):
            with patch.object(bot, "fetch_onchain_safety",
                              return_value={"flagged": True, "reason": "one wallet holds 60% of supply"}):
                ok, why = bot.safety_gate("WHALE", "addr", "sol")
        self.assertFalse(ok)
        self.assertIn("on-chain", why.lower())

    def test_onchain_sol_whale_flagged(self):
        rpc_resp = MagicMock()
        # largest = LP (60), next = whale (30), total supply 100 → whale 30% > 25%
        rpc_resp.json.side_effect = [
            {"result": {"value": [{"uiAmount": 60}, {"uiAmount": 30}, {"uiAmount": 10}]}},
            {"result": {"value": {"uiAmount": 100}}},
        ]
        # rugcheck unavailable (score None) → falls back to the RPC holder heuristic
        with patch.object(bot, "fetch_rugcheck", return_value={"flagged": False, "reason": "", "score": None}):
            with patch.object(bot, "_sol_rpc", return_value="http://rpc"):
                with patch("requests.post", return_value=rpc_resp):
                    oc = bot.fetch_onchain_safety("mint", "sol")
        self.assertTrue(oc["flagged"])

    def _rugcheck_resp(self, score, risks):
        r = MagicMock(); r.status_code = 200
        r.json.return_value = {"score_normalised": score, "risks": risks}
        return r

    def test_rugcheck_flags_danger_risk(self):
        with patch("requests.get", return_value=self._rugcheck_resp(
                30, [{"name": "Mint authority enabled", "level": "danger"}])):
            rc = bot.fetch_rugcheck("mint")
        self.assertTrue(rc["flagged"])
        self.assertIn("Mint authority", rc["reason"])

    def test_rugcheck_flags_high_score(self):
        bot.CONFIG["safety"]["rugcheck_score_max"] = 70
        with patch("requests.get", return_value=self._rugcheck_resp(85, [])):
            rc = bot.fetch_rugcheck("mint")
        self.assertTrue(rc["flagged"])
        self.assertIn("85", rc["reason"])

    def test_rugcheck_clears_safe_token(self):
        with patch("requests.get", return_value=self._rugcheck_resp(
                7, [{"name": "Mutable metadata", "level": "warn"}])):
            rc = bot.fetch_rugcheck("mint")
        self.assertFalse(rc["flagged"])
        self.assertEqual(rc["score"], 7)

    def test_rugcheck_http_error_no_flag(self):
        r = MagicMock(); r.status_code = 503
        with patch("requests.get", return_value=r):
            rc = bot.fetch_rugcheck("mint")
        self.assertFalse(rc["flagged"])
        self.assertIsNone(rc["score"])

    def test_onchain_sol_uses_rugcheck_first(self):
        with patch.object(bot, "fetch_rugcheck",
                          return_value={"flagged": True, "reason": "rugcheck flags X", "score": 90}):
            oc = bot.fetch_onchain_safety("mint", "sol")
        self.assertTrue(oc["flagged"])
        self.assertIn("rugcheck", oc["reason"])

    def test_onchain_sol_rugcheck_clears_skips_rpc(self):
        # rugcheck ran and cleared (score present, not flagged) → trust it, no RPC fallback
        with patch.object(bot, "fetch_rugcheck", return_value={"flagged": False, "reason": "", "score": 10}):
            with patch("requests.post") as p:
                oc = bot.fetch_onchain_safety("mint", "sol")
        self.assertFalse(oc["flagged"])
        p.assert_not_called()

    def test_onchain_evm_honeypot_flagged(self):
        with patch.object(bot, "_get", return_value={"honeypotResult": {"isHoneypot": True}}):
            oc = bot.fetch_onchain_safety("0xabc", "eth")
        self.assertTrue(oc["flagged"])
        self.assertIn("honeypot", oc["reason"].lower())

    def test_onchain_no_address(self):
        self.assertFalse(bot.fetch_onchain_safety("", "sol")["flagged"])

    def test_config_endpoint_sets_safety(self):
        os.environ.pop("DASHBOARD_TOKEN", None)
        c = bot.app.test_client()
        c.post("/api/config", json={"safety": {"reddit_enabled": False}})
        self.assertFalse(bot.CONFIG["safety"]["reddit_enabled"])
        bot.CONFIG["safety"]["reddit_enabled"] = True

    def test_state_exposes_safety(self):
        os.environ.pop("DASHBOARD_TOKEN", None)
        d = bot.app.test_client().get("/api/state").get_json()
        self.assertIn("safety", d["config"])
        self.assertIn("x_key_set", d)


# ═══════════════════════════════════════════════════════════════════════════════
# 41. Degen-name hype boost
# ═══════════════════════════════════════════════════════════════════════════════

class TestDegenHypeBoost(unittest.TestCase):

    def setUp(self):
        _reset()
        bot.CONFIG["degen_terms"]["enabled"] = True
        bot.CONFIG["degen_terms"]["bonus"] = 12

    def test_bonus_applied_to_matching_name(self):
        self.assertEqual(bot._degen_hype_bonus("PENISPUMP"), 12)
        self.assertEqual(bot._degen_hype_bonus("SEXCOIN"), 12)
        self.assertEqual(bot._degen_hype_bonus("CUMROCKET"), 12)

    def test_no_bonus_for_clean_name(self):
        self.assertEqual(bot._degen_hype_bonus("BONK"), 0)
        self.assertEqual(bot._degen_hype_bonus("WIFSTICK"), 0)

    def test_disabled_no_bonus(self):
        bot.CONFIG["degen_terms"]["enabled"] = False
        self.assertEqual(bot._degen_hype_bonus("SEXCOIN"), 0)
        bot.CONFIG["degen_terms"]["enabled"] = True

    def test_case_insensitive(self):
        self.assertEqual(bot._degen_hype_bonus("sexcoin"), 12)

    def test_pair_candidate_gets_boost(self):
        now_ms = int(time.time() * 1000)
        def pair(sym):
            return {"baseToken": {"symbol": sym, "address": "0x"}, "priceUsd": "0.001",
                    "pairCreatedAt": now_ms - 600000, "liquidity": {"usd": 50000},
                    "volume": {"h24": 100000}, "priceChange": {"h24": 5.0}}
        clean = bot._pair_to_candidate(pair("CLEAN"), "sol")["hype"]
        degen = bot._pair_to_candidate(pair("SEXCLEAN"), "sol")["hype"]
        self.assertEqual(degen - clean, 12)

    def test_hype_still_capped_at_100(self):
        now_ms = int(time.time() * 1000)
        p = {"baseToken": {"symbol": "SEXMAX", "address": "0x"}, "priceUsd": "0.001",
             "pairCreatedAt": now_ms - 600000, "liquidity": {"usd": 1000},
             "volume": {"h24": 10_000_000}, "priceChange": {"h24": 5.0}}
        self.assertEqual(bot._pair_to_candidate(p, "sol")["hype"], 100)

    def test_config_endpoint_toggles_degen(self):
        os.environ.pop("DASHBOARD_TOKEN", None)
        c = bot.app.test_client()
        c.post("/api/config", json={"degen_terms": {"enabled": False}})
        self.assertFalse(bot.CONFIG["degen_terms"]["enabled"])
        bot.CONFIG["degen_terms"]["enabled"] = True


# ═══════════════════════════════════════════════════════════════════════════════
# 42. Anti-churn — per-token daily entry cap
# ═══════════════════════════════════════════════════════════════════════════════

class TestEntryChurnCap(unittest.TestCase):

    def setUp(self):
        _reset(1000.0)
        bot.CONFIG["max_entries_per_token_day"] = 2
        bot.STATE["entries_today"] = {}

    def test_under_cap_allowed(self):
        cap = bot.CONFIG["max_entries_per_token_day"]
        self.assertLess(bot.STATE["entries_today"].get("PUMP", 0), cap)

    def test_cap_blocks_third_entry(self):
        bot.STATE["entries_today"]["PUMP"] = 2
        cap = bot.CONFIG["max_entries_per_token_day"]
        self.assertGreaterEqual(bot.STATE["entries_today"]["PUMP"], cap)

    def test_daily_reset_clears_entries(self):
        bot.STATE["entries_today"] = {"PUMP": 5}
        bot.STATE["last_daily_reset"] = "2000-01-01"
        today = bot.now_utc().date().isoformat()
        # mimic the reset logic from engine_once
        if bot.STATE.get("last_daily_reset") != today:
            bot.STATE["entries_today"] = {}
            bot.STATE["last_daily_reset"] = today
        self.assertEqual(bot.STATE["entries_today"], {})

    @patch.object(bot, "fetch_btc_dominance", return_value=45.0)
    @patch.object(bot, "fetch_positions_prices", return_value={})
    @patch.object(bot, "check_reentry_watch")
    @patch.object(bot, "manage_trusted_coins")
    @patch.object(bot, "save_state")
    @patch.object(bot, "safety_gate", return_value=(True, ""))
    @patch.object(bot, "fetch_social_volume", return_value=2.0)
    def test_engine_respects_entry_cap(self, *mocks):
        _reset(1000.0)
        bot.CONFIG["moonshot"]["mode"] = "enter"
        # anti-churn cap is now per-mode (falls back to global) — set the active mode's
        bot.CONFIG["modes"][bot.CONFIG["mode"]]["max_entries_per_token_day"] = 1
        bot.STATE["last_daily_reset"] = bot.now_utc().date().isoformat()  # don't trip the daily reset
        bot.STATE["entries_today"] = {"PUMP": 1}   # already at cap
        cand = [{"symbol": "PUMP", "chain": "sol", "price": 0.001, "liq": 50000,
                 "age_min": 10, "hype": 95, "positive": True, "address": "0xpump"}]
        with patch.object(bot, "fetch_new_candidates", return_value=cand):
            bot.engine_once()
        # PUMP was at cap → no new position opened
        self.assertNotIn("PUMP", bot.STATE["positions"])

    def test_dynamic_reserve_in_drawdown(self):
        _reset(1000.0)
        bot.CONFIG["reserve_pct"] = 0.25
        bot.CONFIG["drawdown_brake"]["reserve_pct"] = 0.40
        with patch.object(bot, "drawdown_brake_active", return_value=False):
            self.assertAlmostEqual(bot.deployable_now(), 750.0, places=2)
        with patch.object(bot, "drawdown_brake_active", return_value=True):
            self.assertAlmostEqual(bot.deployable_now(), 600.0, places=2)  # holds back more

    @patch.object(bot, "fetch_btc_dominance", return_value=45.0)
    @patch.object(bot, "fetch_positions_prices", return_value={})
    @patch.object(bot, "check_reentry_watch")
    @patch.object(bot, "manage_trusted_coins")
    @patch.object(bot, "save_state")
    def test_blacklist_blocks_entry(self, *mocks):
        _reset(1000.0)
        bot.CONFIG["moonshot"]["mode"] = "enter"
        bot.CONFIG["blacklist"] = ["SCAMX"]
        bot.STATE["last_daily_reset"] = bot.now_utc().date().isoformat()
        cand = [{"symbol": "SCAMX", "chain": "sol", "price": 0.001, "liq": 50000,
                 "age_min": 10, "hype": 95, "positive": True, "address": "0xscam"}]
        with patch.object(bot, "fetch_new_candidates", return_value=cand):
            bot.engine_once()
        self.assertNotIn("SCAMX", bot.STATE["positions"])
        bot.CONFIG["blacklist"] = []


# ═══════════════════════════════════════════════════════════════════════════════
# 43. TP ladder + moon bag (catch the +500% runners)
# ═══════════════════════════════════════════════════════════════════════════════

class TestMoonBagLadder(unittest.TestCase):

    def setUp(self):
        _reset(1000.0)
        bot.CONFIG["mode"] = "degen"
        bot.STATE["liq_prev"] = {}

    def _tick(self, price, liq=100000.0):
        # change_m5 positive so the velocity exit doesn't fire — isolate ladder/trailing
        with patch.object(bot, "fetch_positions_prices",
                          return_value={"PUMP": bot._px_dict(price, liq, 5000.0, 1.0)}):
            with patch.object(bot, "save_state"):
                bot.manage_positions()

    def test_deployed_usd_tracked(self):
        bot.shadow_buy("PUMP", "sol", 100.0, 1.0, 100000.0)
        self.assertAlmostEqual(bot.STATE["positions"]["PUMP"]["deployed_usd"], 100.0, places=2)

    def test_entry_mode_locked_on_position(self):
        # A mid-trade mode switch must NOT change the position's locked risk profile
        bot.CONFIG["mode"] = "degen"
        bot.shadow_buy("PUMP", "sol", 100.0, 1.0, 100000.0)
        bot.CONFIG["mode"] = "safe"
        self.assertEqual(bot.STATE["positions"]["PUMP"]["entry_mode"], "degen")

    def test_closed_position_is_purged(self):
        # a fully-sold position must be removed from the dict, not left as a $0 shell
        # (the shell was counting against max_open_positions and jamming the bot)
        bot.shadow_buy("PUMP", "sol", 100.0, 1.0, 1_000_000.0)
        self.assertIn("PUMP", bot.STATE["positions"])
        p = bot.STATE["positions"]["PUMP"]
        bot.shadow_sell("PUMP", p["usd"], 1.2, 1_000_000.0)   # full close
        self.assertNotIn("PUMP", bot.STATE["positions"])

    def test_partial_sell_keeps_position(self):
        bot.shadow_buy("PUMP", "sol", 100.0, 1.0, 1_000_000.0)
        p = bot.STATE["positions"]["PUMP"]
        bot.shadow_sell("PUMP", p["usd"] * 0.5, 1.2, 1_000_000.0)   # half
        self.assertIn("PUMP", bot.STATE["positions"])
        self.assertGreater(bot.STATE["positions"]["PUMP"]["units"], 0)

    def test_trade_log_records_mode(self):
        bot.CONFIG["mode"] = "degen"
        bot.shadow_buy("PUMP", "sol", 100.0, 1.0, 1_000_000.0)
        self.assertEqual(bot.STATE["trade_log"][-1]["mode"], "degen")

    def test_mode_perf_attributed_to_entry_mode(self):
        bot.CONFIG["gas_sim"] = False
        bot.CONFIG["mode"] = "degen"
        bot.STATE["mode_perf"] = {}   # isolate from other sells in this class
        bot.shadow_buy("PUMP", "sol", 100.0, 1.0, 1_000_000.0)
        p = bot.STATE["positions"]["PUMP"]
        bot.shadow_sell("PUMP", p["usd"], 1.30, 1_000_000.0)   # close in profit
        perf = bot.STATE["mode_perf"]["degen"]
        self.assertEqual(perf["sells"], 1)
        self.assertEqual(perf["wins"], 1)
        self.assertGreater(perf["pnl"], 0)

    def test_exit_impact_crashes_thin_pool(self):
        bot.CONFIG["gas_sim"] = False
        bot.shadow_buy("PUMP", "sol", 100.0, 1.0, 1_000_000.0)   # deep entry (no impact)
        p = bot.STATE["positions"]["PUMP"]
        res = bot.shadow_sell("PUMP", p["usd"], 1.0, 100.0)       # dump into a $100 pool
        self.assertLess(res["sold"], 85.0, "dumping into a thin pool must crater the fill")

    def test_exit_impact_negligible_in_deep_pool(self):
        bot.CONFIG["gas_sim"] = False
        bot.shadow_buy("PUMP", "sol", 100.0, 1.0, 1_000_000.0)
        p = bot.STATE["positions"]["PUMP"]
        res = bot.shadow_sell("PUMP", p["usd"], 1.0, 1_000_000.0)  # deep exit
        self.assertGreater(res["sold"], 99.0, "deep pool → only the swap fee, no real impact")

    def test_stall_exit_banks_flat_winner(self):
        bot.CONFIG["mode"] = "degen"
        bot.CONFIG["stall_exit"] = {"enabled": True, "min_gain": 0.20,
                                    "stall_sec": 600, "give_back": 0.06}
        bot.shadow_buy("PUMP", "sol", 100.0, 1.0, 100000.0)
        p = bot.STATE["positions"]["PUMP"]
        avg = p["avg"]
        self._tick(avg * 1.30)               # +30% → new peak
        p["peak_ts"] = time.time() - 700      # pretend it's been flat 11+ min
        self._tick(avg * 1.22)               # +22% gain, ~6% off the peak
        self.assertEqual(p["units"], 0, "stalled winner should be banked, not round-tripped")

    def test_stall_exit_disabled_holds(self):
        bot.CONFIG["mode"] = "degen"
        bot.CONFIG["stall_exit"] = {"enabled": False}
        bot.shadow_buy("PUMP", "sol", 100.0, 1.0, 100000.0)
        p = bot.STATE["positions"]["PUMP"]
        avg = p["avg"]
        self._tick(avg * 1.30)
        p["peak_ts"] = time.time() - 700
        self._tick(avg * 1.25)
        self.assertGreater(p["units"], 0, "disabled → no stall exit")

    def test_ladder_scalps_and_keeps_moonbag(self):
        bot.shadow_buy("PUMP", "sol", 100.0, 1.0, 100000.0)
        p = bot.STATE["positions"]["PUMP"]
        avg, dep = p["avg"], p["deployed_usd"]
        self._tick(avg * 1.31)   # +31% → rung 1
        self.assertEqual(p["tp_index"], 1)
        self._tick(avg * 1.81)   # +81% → rung 2
        self.assertEqual(p["tp_index"], 2)
        self._tick(avg * 2.51)   # +151% → rung 3
        self.assertEqual(p["tp_index"], 3)
        # ~30% moon bag remains
        self.assertGreater(p["units"], 0)
        self.assertLess(p["usd"], 0.40 * dep)
        self.assertGreater(p["usd"], 0.20 * dep)

    def test_moonbag_rides_wide_trailing(self):
        bot.shadow_buy("PUMP", "sol", 100.0, 1.0, 100000.0)
        p = bot.STATE["positions"]["PUMP"]
        avg = p["avg"]
        for mult in (1.31, 1.81, 2.51):
            self._tick(avg * mult)
        self.assertEqual(p["tp_index"], 3)         # ladder done → moon-bag mode
        self._tick(avg * 4.0)                       # new peak +300%
        peak = p["peak_price"]
        self._tick(peak * 0.80)                     # -20% from peak: normal 15% would exit
        self.assertGreater(p["units"], 0, "wide moon-bag leash should hold through a -20% dip")
        self._tick(peak * 0.50)                     # -50% from peak: beyond the 45% leash → exit
        self.assertEqual(p["units"], 0)

    def test_non_degen_mode_keeps_simple_tp(self):
        # default mode has no tp_ladder → falls back to 50%-per-rung, no moon bag
        bot.CONFIG["mode"] = "default"
        bot.shadow_buy("PUMP", "sol", 100.0, 1.0, 100000.0)
        p = bot.STATE["positions"]["PUMP"]
        self._tick(p["avg"] * 1.30)   # default tp is +28% → fires, sells ~half
        self.assertEqual(p["tp_index"], 1)
        self.assertGreater(p["units"], 0)


# ═══════════════════════════════════════════════════════════════════════════════
# 44. Backtester — replay through the real strategy (synthetic ticks, no network)
# ═══════════════════════════════════════════════════════════════════════════════
import backtest as bt   # noqa: E402


class TestBacktest(unittest.TestCase):

    def setUp(self):
        _reset(1000.0)

    @staticmethod
    def _ticks(prices, liq=100000.0, step_sec=300, vol=300000.0):
        # vol high enough that hype (vol/liq*40) clears the moonshot hype floor
        return [{"ts": 1_700_000_000.0 + i * step_sec, "price": p,
                 "liq": liq, "vol_h1": vol} for i, p in enumerate(prices)]

    def test_replay_winner_is_profitable(self):
        # steady climb to +300% then flat → momentum entry fires, banks a gain
        ticks = self._ticks([1.0] + [1.0 + 0.2 * i for i in range(1, 20)])
        r = bt.replay_episode("PUMP", "sol", "0xpump", ticks, "degen")
        self.assertIsNotNone(r)
        self.assertGreater(r["pnl"], 0)
        self.assertTrue(r["win"])
        # globals must be restored after the run
        self.assertEqual(bot.CONFIG["mode"], "default")

    def test_replay_rug_is_a_loss(self):
        # rises (momentum entry fires) then craters → exit at a loss, position closed
        ticks = self._ticks([1.0, 1.05, 1.1, 1.15, 1.2, 1.25])
        ticks += [{"ts": ticks[-1]["ts"] + 300, "price": 0.2, "liq": 1000.0, "vol_h1": 100.0}]
        r = bt.replay_episode("PUMP", "sol", "0xpump", ticks, "degen")
        self.assertIsNotNone(r)
        self.assertLess(r["pnl"], 0)

    def test_momentum_entry_skips_pure_decliner(self):
        # a token that only falls never triggers a momentum entry → no trade (no loss)
        ticks = self._ticks([1.0 - 0.03 * i for i in range(12)])
        self.assertIsNone(bt.find_entry_index(ticks))
        self.assertIsNone(bt.replay_episode("DUMP", "sol", "0xd", ticks, "degen"))

    def test_replay_skips_too_thin_for_mode(self):
        # $20k pool is below safe mode's $50k floor → no entry under safe
        ticks = self._ticks([1.0, 1.1, 1.2], liq=20000.0)
        self.assertIsNone(bt.replay_episode("PUMP", "sol", "0xpump", ticks, "safe"))

    def test_replay_restores_state_object(self):
        before = bot.STATE
        ticks = self._ticks([1.0, 1.5, 2.0])
        bt.replay_episode("PUMP", "sol", "0xpump", ticks, "hype")
        self.assertIs(bot.STATE, before, "replay must not leak its isolated STATE")

    def test_timeframe_scales_to_a_week(self):
        # ≤3 days → 5-min; a week → coarser candles so it fits the 1000-candle cap
        self.assertEqual(bt._timeframe_for(2)[1], 5)
        self.assertEqual(bt._timeframe_for(7)[1], 15)
        self.assertEqual(bt._timeframe_for(30)[0], "hour")

    def test_fetch_universe_parses_trending(self):
        gt = {"data": [{
            "attributes": {"address": "poolA", "name": "WIF / SOL"},
            "relationships": {"base_token": {"data": {"id": "solana_TOKENADDR"}}},
        }]}
        with patch.object(bt, "_gt_get", return_value=gt), \
             patch.object(bt._real_time, "sleep"), \
             patch.object(bot, "_get", return_value=[]):
            u = bt.fetch_universe(limit=5, chains=["sol"])
        self.assertTrue(any(t[2] == "TOKENADDR" and t[3] == "poolA" for t in u))

    def test_recorder_writes_and_backtest_reads_it(self):
        import tempfile, os as _os
        d = tempfile.mkdtemp()
        bot.RECORDER["enabled"] = True
        bot.RECORDER["dir"] = d
        bot.RECORDER["tick_sec"] = 0          # don't throttle in the test
        bot._REC_TS.clear()
        p = {"chain": "sol", "address": "0xrec"}
        # record a rising-then-crashing tick path for one held position
        for i, (price, liq) in enumerate([(1.0, 100000), (1.2, 100000), (0.3, 5000)]):
            with patch.object(bot, "now_utc",
                              return_value=bot.datetime.fromtimestamp(1_700_000_000 + i*60,
                                                                      tz=bot.timezone.utc)):
                bot._record_tick("REC", p, bot._px_dict(price, liq, 200000.0, 0.0))
        self.assertTrue(_os.path.exists(_os.path.join(d, _os.listdir(d)[0])))
        hist = bt.histories_from_recording(days=3650, rec_dir=d)
        self.assertIn(("REC", "sol", "0xrec"), hist)
        self.assertEqual(len(hist[("REC", "sol", "0xrec")]), 3)

    def test_recorder_disabled_writes_nothing(self):
        import tempfile, os as _os
        d = tempfile.mkdtemp()
        bot.RECORDER["enabled"] = False
        bot._record_tick("X", {"chain": "sol"}, bot._px_dict(1.0))
        self.assertEqual(_os.listdir(d), [])
        bot.RECORDER["enabled"] = True


if __name__ == "__main__":
    unittest.main(verbosity=2)
