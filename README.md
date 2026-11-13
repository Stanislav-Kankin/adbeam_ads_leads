# AdBeam Leads

Лидогенерация под оффер «возьмите нас в параллель»: рекламодатели ниши (Keys.so) → сайт и ИНН → Контур.Продажи → таблица-досье.
Контекст и решения описаны в [CLAUDE.md](CLAUDE.md).

## Запуск локально (Windows)

Двойной клик по `run.bat`. При первом запуске создаётся `.venv` (нужен Python 3.12) и ставятся зависимости. Затем открывается браузер на http://127.0.0.1:8000.

Вручную:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\ads-leads web --open
```

Настройки и токен Keys.so: скопировать `.env.example` в `.env`.

## Запуск на сервере

```bash
docker compose up -d --build
```

Данные (SQLite и выгрузки) лежат в `./data`.

## Тесты

```powershell
.venv\Scripts\python -m pytest
```
