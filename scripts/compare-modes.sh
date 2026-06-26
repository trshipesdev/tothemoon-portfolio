#!/usr/bin/env bash
# Compare the four mode-ensemble instances (see docker-compose.multi.yml).
# Prints each mode's paper balance + realized P&L so you can see the winner.
set -euo pipefail

declare -A PORTS=( [safe]=8801 [default]=8802 [hype]=8803 [degen]=8804 )

printf "%-9s %12s %12s %12s %8s\n" MODE BALANCE REALIZED UNREALIZED OPEN
printf -- "----------------------------------------------------------\n"
for mode in safe default hype degen; do
  port=${PORTS[$mode]}
  json=$(curl -s -m 5 "http://127.0.0.1:${port}/api/state" 2>/dev/null || echo "")
  if [ -z "$json" ]; then
    printf "%-9s %12s\n" "$mode" "(down)"
    continue
  fi
  echo "$json" | python3 -c "
import sys, json
d = json.load(sys.stdin)
vault = d.get('vault_usd', 0)
pos   = d.get('positions', {}) or {}
unreal = sum(p.get('pnl_usd', 0) for p in pos.values())
realized = sum(r.get('pnl', 0) for r in (d.get('mode_perf') or {}).values())
bal = vault + sum(p.get('value', 0) for p in pos.values()) + d.get('income_usd', 0)
print('%-9s %12.2f %12.2f %12.2f %8d' % ('$mode', bal, realized, unreal, len(pos)))
"
done
