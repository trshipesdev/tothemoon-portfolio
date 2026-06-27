#!/usr/bin/env bash
# deploy.sh — push code, rebuild container, reopen tunnel, confirm dashboard is up
set -euo pipefail

SERVER="ubuntu@150.136.106.188"
KEY="$HOME/Downloads/ssh-key-2026-06-25.key"
SSH="ssh -i $KEY -o StrictHostKeyChecking=no"
REMOTE_DIR="/home/ubuntu/tothemoon"
DASHBOARD="http://localhost:8787"
# Read token from local .env (never hardcode it here)
AUTH="${DASHBOARD_TOKEN:-$(grep DASHBOARD_TOKEN "$(dirname "$0")/../.env" 2>/dev/null | cut -d= -f2 | tr -d '"' || true)}"
if [[ -z "$AUTH" ]]; then echo "ERROR: DASHBOARD_TOKEN not set and not found in .env"; exit 1; fi

echo "==> Syncing code to server..."
rsync -az --exclude='.git' --exclude='data/' --exclude='.env' --exclude='__pycache__' \
  -e "ssh -i $KEY -o StrictHostKeyChecking=no" \
  ./ $SERVER:$REMOTE_DIR/

echo "==> Rebuilding container..."
$SSH $SERVER "cd $REMOTE_DIR && docker compose up --build -d 2>&1 | tail -5"

echo "==> Waiting for container to become healthy..."
for i in $(seq 1 30); do
  STATUS=$($SSH $SERVER "docker inspect --format '{{.State.Health.Status}}' tothemoon-cryptobot-1 2>/dev/null || echo 'none'")
  if [[ "$STATUS" == "healthy" ]]; then
    echo "    Container healthy after ${i}s"
    break
  fi
  if [[ $i -eq 30 ]]; then
    echo "    ERROR: container not healthy after 30s (status: $STATUS)"
    $SSH $SERVER "docker logs tothemoon-cryptobot-1 --tail 20"
    exit 1
  fi
  sleep 1
done

echo "==> Reopening SSH tunnel on :8787..."
# Kill any existing tunnel on port 8787
lsof -ti :8787 | xargs kill -9 2>/dev/null || true
sleep 1
ssh -i "$KEY" -o StrictHostKeyChecking=no -o ExitOnForwardFailure=yes \
    -fNL 8787:localhost:8787 $SERVER
echo "    Tunnel open."

echo "==> Confirming dashboard responds..."
for i in $(seq 1 10); do
  HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "Authorization: Bearer $AUTH" \
    "$DASHBOARD/api/state" 2>/dev/null || echo "000")
  if [[ "$HTTP" == "200" ]]; then
    echo "    Dashboard OK (HTTP 200)"
    break
  fi
  if [[ $i -eq 10 ]]; then
    echo "    ERROR: dashboard not responding (last HTTP $HTTP)"
    exit 1
  fi
  sleep 1
done

echo ""
echo "Deploy complete. Dashboard: $DASHBOARD"
