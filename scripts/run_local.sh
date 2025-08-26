#!/usr/bin/env bash
set -euo pipefail

if [[ ! -f .env ]]; then
  echo "Copying .env.example -> .env (fill values before LIVE)"
  cp .env.example .env
fi

python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt

export $(grep -v '^#' .env | xargs) || true

echo "Starting bot in SHADOW MODE (paper) ..."
python bot_full.py
