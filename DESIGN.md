# Design decisions and the data behind them

Most of the non-obvious parameters in this bot were set from live shadow-mode
data, not guessed. This document records why each one is where it is, so the
reasoning isn't lost.

## Risk controls

### Dollar stop: `max($12, 10% of deployed)`

`CONFIG["moonshot"]["dollar_stop_usd"] = 12.0`

Early shadow runs showed positions running well past a sensible loss before any
exit fired — STAR lost $32.89 and BBP lost $29 where a $12 stop should have
caught them. In shadow mode the cap is applied retroactively on exit; in live
mode, gap risk (a rug between 2-second ticks) means real fills can still exceed
it, which is a live-execution problem rather than a config one.

### Post-loss cooldown: 30-minute block on the same token

`recently_exited[sym]` is checked in the scanner for 30 minutes after a losing
exit. Without it, tokens re-entered repeatedly within minutes of their own stop —
BOWIE re-entered six times in two minutes for a combined $84 loss, with YAPPR and
BBP showing the same pattern.

The check must sit **before** the `if is_new:` block in `scan_candidates()`.
`is_new` means "is this a freshly launched token," not "is this a new position" —
so when the cooldown check lived inside that block, established tokens skipped the
cooldown entirely.

### `liq_prev` seeded at buy time

`STATE["liq_prev"][symbol]` is set in `shadow_buy` when a position opens.
Otherwise the Birdeye price fallback (which reports a 100k default liquidity)
seeds `liq_prev`, and the next DexScreener tick reads real liquidity (~28k)
against that inflated baseline and fires a false rug exit.

### Birdeye liquidity fallback uses `entry_liq`, not a constant

`bd_liq = float(bd.get("liquidity") or 0) or p.get("entry_liq", 0) or 0`

The previous `or 100000.0` default poisoned the rug detector with a baseline the
token never had.

## Sizing

### Seed mode (small vault): dynamic sizing

`CONFIG["moonshot"]["dynamic_sizing"] = True` when the vault is under ~$500.
Bet size becomes `min(base, available × 40%)` with a $10 floor and a 1.5×
buffer gate. A $100 vault should not put $48 on a single degen bet; with dynamic
sizing its first bets are $4–10 and compound from wins. A $100 backtest returned
+143% against +13% for the same period at $1000, driven by the cooldown and the
capped per-bet loss.

### `base_size_usd = 30.0` (degen mode ×1.6 = ~$48/bet)

Reduced from $50 to spread capital across more tokens. At a $1000 vault that's
4.8% per position; 12 slots cap deployed capital near 57% of the vault.
`per_token_cap_room(symbol)` subtracts any existing position so total per-token
exposure stays under 12% of deployable — previously each buy was checked in
isolation, so TOPDOG reached 8 buys / $551 and another token 4 buys / $268, both
far past the intended cap.

### `max_open_positions = 12`

Memecoins flush together in market-wide moves, so the cap limits correlated
drawdown. In practice the bot rarely fills all 12 — DexScreener doesn't surface
that many qualifying candidates per tick.

## Entry filtering

### `buy_ratio_min = 0.40`

FRONT (+511%) and MASTERCOIN (+656%) were both blocked at the old 0.45 threshold,
with buy ratios of 40% and 38%, hype 100, liquidity $28–41k, ages 26–91 minutes.
Low buy ratio often reflects whale accumulation before a flip rather than a dump
setup, and dump risk is already covered by the dollar stop. Tokens in the 40–45%
band get a 25% smaller bet via the `_conviction_mult` penalty.

## Exits

### 2-minute velocity check: 12% drop in 2 minutes → exit

`velocity_2m_pct = 0.12`. Twenty-one slow-bleed losses were held 2–15 minutes for
a combined $37 because the DexScreener m5 window reacts too slowly. The internal
tick history (~240 ticks ≈ 2 minutes at 0.5s) exits before the drop compounds.

### Profit re-entry: 60-second minimum + 8% pullback

TOPDOG re-bought zero seconds after its own take-profit sell, chasing its exit
price. The 60-second floor prevents that regardless of pullback; the 8% pullback
check still applies after it.

### EVM chains disabled by default

`CONFIG["chains"] = ["sol"]`. Over June 25–27, Ethereum ran 0% win rate (-$84,
all BOWIE), Base 25% (-$44), BSC 0% (-$10); Solana was 47% at +$206. The EVM
chains lost $138 total and it looked structural rather than variance. Re-enabling
a specific EVM chain is worth it only with ≥40% win rate over 50+ trades and
positive total PnL.

## Live-trading prerequisites

These are unfinished and block flipping `SHADOW_MODE=false` with a funded wallet:

1. **Per-chain wallet balance.** `per_chain_room()` caps by percentage of total
   vault, but the SOL and EVM wallets hold separate real balances — a $1000
   vault with $800 on Solana cannot spend $300 on Base.
2. **Solana tx confirmation.** `_sol_submit_tx` is fire-and-forget; a tx that
   fails to land leaves the bot believing it sold. Needs confirmation polling or
   a `sell_pending` state.
3. **Non-blocking EVM sells.** `wait_for_transaction_receipt(timeout=120)` blocks
   the engine loop for up to two minutes, during which no other position is
   monitored. EVM sells should run on a background thread.

## Guiding principle

Each fix should improve behavior across every mode and vault size, not just the
case that triggered it. The cooldown, the dollar stop, and the velocity exit all
apply everywhere; seed sizing only activates under `dynamic_sizing` for small
vaults.
