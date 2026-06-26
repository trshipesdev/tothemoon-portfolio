FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

# Install deps first so this layer caches across code changes
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy source
COPY src/bot/bot_full.py .
COPY src/bot/backtest.py .
COPY src/dashboard/ ./dashboard/

# State directory — mount ./data:/app/data in docker-compose to persist
RUN mkdir -p data

ENV SHADOW_MODE=true
ENV DASHBOARD_DIR=/app/dashboard
ENV STATE_PATH=/app/data/state_default.json

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8787/status', timeout=4)" || exit 1

CMD ["python", "bot_full.py"]
