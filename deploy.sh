#!/usr/bin/env bash
# Обновление на сервере: ./deploy.sh
set -euo pipefail
cd "$(dirname "$0")"
git pull --ff-only
docker compose up -d --build

PORT="${ADS_LEADS_PUBLISH_PORT:-8021}"
echo "Жду запуска приложения на 127.0.0.1:${PORT}…"
for i in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${PORT}/health" 2>/dev/null; then
    echo && echo "Готово."
    exit 0
  fi
  sleep 2
done

echo "Приложение не ответило за 60 секунд. Последние логи:"
docker compose ps
docker compose logs --tail=60 app
exit 1
