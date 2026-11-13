"""Клиент Keys.so API (https://apidoc.keys.so).

Лимит API: 10 запросов за 10 секунд — держим паузу ≥1.05 с между запросами
и повторяем запрос при 429/5xx.
"""

import logging
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx

log = logging.getLogger(__name__)

API_URL = "https://api.keys.so"
MIN_INTERVAL = 1.05  # сек между запросами
STATE_DONE, STATE_ERROR = 10, 2


class KeysoError(RuntimeError):
    pass


class KeysoClient:
    def __init__(self, token: str, *, transport: httpx.BaseTransport | None = None, min_interval: float = MIN_INTERVAL):
        if not token:
            raise KeysoError("Не задан токен Keys.so (ADS_LEADS_KEYSO_API_TOKEN в .env)")
        self._http = httpx.Client(
            base_url=API_URL, headers={"X-Keyso-TOKEN": token}, timeout=60, transport=transport
        )
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "KeysoClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- транспорт ---------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs) -> Any:
        for attempt in range(5):
            with self._lock:
                wait = self._last + self._min_interval - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._last = time.monotonic()
            try:
                resp = self._http.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                log.warning("Keys.so %s %s: %s", method, path, exc)
                time.sleep(5 * (attempt + 1))
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(10 * (attempt + 1))
                continue
            if resp.status_code == 401:
                raise KeysoError("Keys.so: неверный токен (401)")
            if resp.status_code == 404:
                return None
            if resp.status_code >= 400:
                raise KeysoError(f"Keys.so {resp.status_code}: {resp.text[:300]}")
            return resp.json()
        raise KeysoError(f"Keys.so не ответил после повторов: {method} {path}")

    def _pages(self, path: str, params: dict, *, per_page: int = 500, max_rows: int = 3000) -> Iterator[dict]:
        page = 1
        fetched = 0
        while True:
            data = self._request("GET", path, params={**params, "page": page, "per_page": per_page})
            rows = (data or {}).get("data") or []
            yield from rows
            fetched += len(rows)
            if not rows or page >= int((data or {}).get("last_page") or 1) or fetched >= max_rows:
                return
            page += 1

    # --- методы ------------------------------------------------------------
    def limits(self) -> dict:
        return self._request("GET", "/limits/all") or {}

    def advertisers_by_keywords(
        self,
        keywords: list[str],
        base: str,
        *,
        on_progress: Callable[[int], None] | None = None,
        max_rows: int = 3000,
    ) -> list[dict]:
        """«Доля конкурентов по фразам», режим контекста: кто рекламируется по списку фраз.

        Строка: name (домен), perc (доля фраз, %), cnt (фраз с рекламой), spec (премиум-показы), gar (остальные).
        """
        created = self._request(
            "POST", "/tools/concurents_by_keywords", json={"base": base, "top": 10, "list": keywords[:3000]}
        )
        uid = (created or {}).get("uid")
        if not uid:
            raise KeysoError(f"Keys.so не создал отчёт: {created}")
        deadline = time.monotonic() + 15 * 60
        while True:
            state = self._request("GET", f"/tools/concurents_by_keywords/state/{uid}") or {}
            if on_progress and state.get("progress") is not None:
                on_progress(int(state["progress"]))
            if state.get("state") == STATE_DONE:
                break
            if state.get("state") == STATE_ERROR:
                raise KeysoError("Keys.so: ошибка обработки отчёта по фразам")
            if time.monotonic() > deadline:
                raise KeysoError("Keys.so: отчёт по фразам не готов за 15 минут")
            time.sleep(4)
        return list(
            self._pages(
                f"/tools/concurents_by_keywords/{uid}",
                {"base": base, "view": "context", "sort": "cnt|desc"},
                max_rows=max_rows,
            )
        )

    def domains_batch(self, domains: list[str], base: str) -> list[dict]:
        """Пакетный анализ: adcost (оценка бюджета), adtraf, adkeyscnt, adscnt и SEO-метрики. До 1000 доменов."""
        rows: list[dict] = []
        for i in range(0, len(domains), 1000):
            chunk = domains[i : i + 1000]
            page = 1
            while True:  # per_page максимум 500
                data = self._request(
                    "POST",
                    "/tools/domains-batch",
                    params={"base": base},
                    json={"data": {"domains": chunk}, "params": {"per_page": 500, "page": page}},
                )
                rows.extend((data or {}).get("data") or [])
                if page >= int((data or {}).get("last_page") or 1):
                    break
                page += 1
        return rows
