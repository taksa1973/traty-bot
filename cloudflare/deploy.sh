#!/usr/bin/env bash
# Передеплой Cloudflare-воркера напрямую через API (без wrangler).
# Требует переменную окружения CF_API_TOKEN (Cloudflare API token
# с правами Workers Scripts:Edit и Workers KV Storage:Edit).
# Остальное (KV_ID, секреты, имя воркера) читается из cloudflare/.env.
set -euo pipefail
cd "$(dirname "$0")"

: "${CF_API_TOKEN:?Задай CF_API_TOKEN=... (Cloudflare API token)}"
[ -f .env ] || { echo "Нет cloudflare/.env с секретами"; exit 1; }
set -a; . ./.env; set +a
ACC="$CF_ACCOUNT"

META="$(mktemp)"
python3 - > "$META" <<'PY'
import json, os
print(json.dumps({
    "main_module": "worker.js",
    "compatibility_date": "2024-11-01",
    "bindings": [
        {"type": "kv_namespace", "name": "KV", "namespace_id": os.environ["KV_ID"]},
        {"type": "secret_text", "name": "BOT_TOKEN", "text": os.environ["BOT_TOKEN"]},
        {"type": "secret_text", "name": "DB_KEY", "text": os.environ["DB_KEY"]},
        {"type": "secret_text", "name": "WEBHOOK_SECRET", "text": os.environ["WEBHOOK_SECRET"]},
    ],
}))
PY

echo "Заливаю воркер $WORKER_NAME…"
curl -s -X PUT "https://api.cloudflare.com/client/v4/accounts/$ACC/workers/scripts/$WORKER_NAME" \
  -H "Authorization: Bearer $CF_API_TOKEN" \
  -F "metadata=@$META;type=application/json" \
  -F "worker.js=@worker.js;type=application/javascript+module" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('deploy success:', d.get('success'), '| errors:', [e.get('message') for e in (d.get('errors') or [])])"
rm -f "$META"
echo "Готово. URL: $WORKER_URL"
