#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

[ -f config.json ] || { echo "missing config.json: cp config.example.json config.json and fill in the keys" >&2; exit 1; }

mkdir -p data && chmod 700 data && chmod 600 config.json

echo "==> building and starting"
docker compose up -d --build

echo "==> health check"
for i in $(seq 1 30); do
  if curl -sf http://127.0.0.1:8317/healthz >/dev/null 2>&1; then
    curl -s http://127.0.0.1:8317/healthz | head -c 400; echo
    break
  fi
  sleep 2
  [ "$i" = "30" ] && { echo "failed to start:"; docker logs --tail 50 openai-free-guard; exit 1; }
done

BASE="http://127.0.0.1:8317"
if docker ps --format '{{.Names}}' | grep -qi 'new-api'; then
  NAME=$(docker ps --format '{{.Names}}' | grep -i 'new-api' | head -1)
  NET=$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}' "$NAME")
  docker network connect "$NET" openai-free-guard 2>/dev/null || true
  BASE="http://openai-free-guard:8317"
  echo "attached to New API container $NAME (network $NET)"
fi

echo
echo "=========================================="
echo " Point your gateway channel at:"
echo "   Base URL : $BASE"
echo "   Key      : $(python3 -c "import json;print(json.load(open('config.json'))['proxy_token'])" 2>/dev/null || echo '<proxy_token from config.json>')"
echo "=========================================="
