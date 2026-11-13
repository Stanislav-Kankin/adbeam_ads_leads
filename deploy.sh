#!/usr/bin/env bash
# Обновление на сервере: ./deploy.sh
set -euo pipefail
cd "$(dirname "$0")"
git pull --ff-only
docker compose up -d --build
sleep 3
curl -fsS "http://127.0.0.1:${ADS_LEADS_PUBLISH_PORT:-8021}/health" && echo && echo "Готово."
