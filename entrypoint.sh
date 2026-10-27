#!/usr/bin/env bash
set -euo pipefail
: "${TELEGRAM_API_ID:?Set TELEGRAM_API_ID from my.telegram.org}"
: "${TELEGRAM_API_HASH:?Set TELEGRAM_API_HASH from my.telegram.org}"
: "${TELEGRAM_BOT_TOKEN:?Set TELEGRAM_BOT_TOKEN from BotFather}"
: "${TELEGRAM_ADMIN_IDS:?Set TELEGRAM_ADMIN_IDS to your numeric Telegram user ID}"

mkdir -p /tmp/telegram-bot-api /tmp/telegram-bot-api-tmp
telegram-bot-api \
  --api-id="$TELEGRAM_API_ID" \
  --api-hash="$TELEGRAM_API_HASH" \
  --local \
  --http-port=8081 \
  --dir=/tmp/telegram-bot-api \
  --temp-dir=/tmp/telegram-bot-api-tmp &
API_PID=$!
cleanup() { kill "$API_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

python - <<'PY'
import os, time, urllib.request
url = "http://127.0.0.1:8081/bot" + os.environ["TELEGRAM_BOT_TOKEN"] + "/getMe"
for _ in range(90):
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            if response.status == 200:
                break
    except Exception:
        time.sleep(1)
else:
    raise SystemExit("Local Telegram Bot API did not become ready")
PY

python /app/bot.py &
BOT_PID=$!
wait -n "$API_PID" "$BOT_PID"
