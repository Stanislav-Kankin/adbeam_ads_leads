FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    ADS_LEADS_HOST=0.0.0.0 ADS_LEADS_DATA_DIR=/app/data \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# 1) Chromium для обхода JS-защиты сайтов + виртуальный дисплей (браузер «с окном» проходит больше защит).
#    Отдельный слой до копирования кода: кэшируется и не пересобирается при каждом деплое.
#    libatomic1 нужен встроенному в Playwright Node.js (в slim-образе его нет).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 xvfb xauth fonts-liberation \
    && pip install --no-cache-dir "playwright>=1.52,<2.0" \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# 2) Приложение
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

EXPOSE 8000
# Xvfb для браузера приложение поднимает само, когда он нужен (xvfb-run как PID 1 зависает)
CMD ["ads-leads", "web"]
